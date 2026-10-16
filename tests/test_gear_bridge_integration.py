#!/usr/bin/env python3
"""End-to-end gear/bridge integration (hermetic — stdlib ``http.server`` only).

Proves the P0 gear-registry + Tool Bridge wiring with **no external installs**
and **no outbound network**: the only socket in play is a loopback listener the
test itself owns.

The flow, each step a separate assertion:

1. A *reference gear* (``http.server`` in a daemon thread, the same envelope
   logic as ``ops/tools/bridge_reference_gear.py``) binds a loopback port and is
   declared as a gear in ``config/engine.json -> gears`` — so routing comes from
   the config, not only the environment.
2. ``get_gear_info`` reports ``is_configured is True`` and the config endpoint
   wins over a decoy ``AGENCY_BRIDGE_URL`` (config first, environment fallback).
3. ``probe_gear`` and ``bridge.invoke`` both reach the gear and answer ``ok``.
4. The spend ledger records exactly one ``reserved`` + one ``settled`` event for
   the call, attributed to the gear category and the governance domain.
5. ``AGENCY_SPEND_KILL=1`` refuses with ``SpendCapExceeded`` **before the socket
   opens** — the gear server sees zero requests and a refusal is in the ledger.
6. A retryable 5xx settles the failed attempt before re-authorizing, so the
   ledger carries one reserve/settle pair per HTTP attempt.
7. ``design.image_provider`` in the same config decides the image backend ahead
   of a present ``HF_TOKEN`` (config wins over the credential heuristic).
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

from tests._bootstrap import REPO_ROOT

import functools
import hmac
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from capsule_engine import spend_control
from capsule_engine.bridge import HttpBridge, default_bridge, resolve_endpoint
from capsule_engine.gateway import SpendCapExceeded
from capsule_engine.gear_registry import get_gear_info, probe_gear

GEAR = "text:review"

#: Environment this suite manages absolutely: snapshotted in setUp, wiped before
#: the test body, and fully restored in tearDown (a leaked key would let a later
#: test build a real bridge or reach the repository's .env).
_MANAGED_ENV = (
    "AGENCY_ENGINE_CONFIG",
    "AGENCY_BRIDGE_CONFIG",
    "AGENCY_SPEND_LEDGER",
    "AGENCY_GEAR_PROBE_CACHE",
    "AGENCY_SPEND_KILL",
    "AGENCY_OFFLINE_MODE",
    "AGENCY_BRIDGE_URL",
    "AGENCY_BRIDGE_TOKEN",
    "AGENCY_LLM_API_KEY",
    "HF_TOKEN",
    "AGENCY_IMAGE_PROVIDER",
)


@functools.lru_cache(maxsize=1)
def _reference_gear():
    """Load the shipped reference gear by path (it is not an importable package)."""
    path = REPO_ROOT / "ops" / "tools" / "bridge_reference_gear.py"
    spec = importlib.util.spec_from_file_location("bridge_reference_gear", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_handler(server: "_MockGear"):
    """A loopback handler reusing the reference gear's own envelope logic."""
    reference = _reference_gear()

    class _BridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # keep the test output clean
            pass

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            server.requests += 1
            if self.path.rstrip("/") not in ("", "/bridge"):
                self._send(404, {"error": "not_found"})
                return
            presented = self.headers.get("Authorization", "")
            if not hmac.compare_digest(presented, f"Bearer {server.token}"):
                self._send(401, {"error": "unauthorized"})
                return
            if server.fail_first > 0:
                server.fail_first -= 1
                self._send(503, {"error": "temporarily_unavailable"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send(400, {"error": "bad_length"})
                return
            raw = self.rfile.read(length)
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "invalid_json"})
                return
            status, response = reference.handle_envelope(envelope)
            self._send(status, response)

    return _BridgeHandler


class _MockGear:
    """A reference gear bound to a loopback port, served from a daemon thread."""

    def __init__(self, token: str, fail_first: int = 0) -> None:
        self.token = token
        self.fail_first = fail_first
        self.requests = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}/bridge"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _spend_section() -> dict:
    return {
        "enabled": True,
        "kill": False,
        "daily_usd": 10.0,
        "monthly_usd": 100.0,
        "per_domain_daily_usd": 10.0,
        "max_calls_per_day": 1000,
        "default_domain": "core",
        "known_domains": ["core", "marketing", "design", "operations"],
        "unpriced_estimate_usd": 0.02,
        "estimate_tokens": {"prompt": 1000, "completion": 500},
        "model_prices": {
            "gpt-4o-mini": {"prompt_per_1k": 0.00015, "completion_per_1k": 0.0006},
        },
        "image_costs": {"pollinations-free": 0.0, "black-forest-labs/FLUX.1-schnell": 0.003},
        "subscriptions": [],
    }


class GearBridgeIntegrationTest(unittest.TestCase):
    """Config-driven gear routing, governed delivery, and the spend ledger."""

    def setUp(self) -> None:
        self._saved_env = dict(os.environ)
        for key in _MANAGED_ENV:
            os.environ.pop(key, None)
        # Any per-verb bridge override from the real environment is dropped too.
        for key in list(os.environ):
            if key.startswith(("AGENCY_BRIDGE_URL", "AGENCY_BRIDGE_TOKEN")):
                os.environ.pop(key, None)

        self.tmp = Path(tempfile.mkdtemp(prefix="so_gear_bridge_")).resolve()
        self.ledger = self.tmp / "usage_ledger.jsonl"
        self.probe_cache = self.tmp / "gear_probes.json"
        self.token = "integration-bridge-token"
        self.server = _MockGear(self.token)
        self.config_path = self.write_config(self.server.endpoint, self.token)

        os.environ["AGENCY_ENGINE_CONFIG"] = str(self.config_path)
        os.environ["AGENCY_SPEND_LEDGER"] = str(self.ledger)
        os.environ["AGENCY_GEAR_PROBE_CACHE"] = str(self.probe_cache)
        spend_control.reset_caches()
        from capsule_engine import policy

        policy.reset_cache()

    def tearDown(self) -> None:
        self.server.stop()
        os.environ.clear()
        os.environ.update(self._saved_env)
        spend_control.reset_caches()
        from capsule_engine import policy

        policy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------- #

    def write_config(self, endpoint: str, token: str | None) -> Path:
        """Write the scratch engine config (spend + gears + design) and return it."""
        entry: dict = {"endpoint": endpoint}
        if token:
            entry["token"] = token
        config = {
            "version": 1,
            "spend": _spend_section(),
            "design": {"image_provider": "pollinations"},
            "gears": {
                "default": {"endpoint": None, "token_env": "AGENCY_BRIDGE_TOKEN"},
                GEAR: entry,
            },
        }
        path = self.tmp / "engine.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        spend_control.reset_caches()
        from capsule_engine import policy

        policy.reset_cache()
        return path

    def events(self) -> list[dict]:
        if not self.ledger.is_file():
            return []
        return [
            json.loads(line)
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _capsule() -> dict:
        return {"id": "cap-integration", "state": "pending_qa", "domain": "marketing"}

    @staticmethod
    def _governance() -> dict:
        return {"domain": "marketing", "autonomy_level": "L1"}

    @staticmethod
    def _review_input() -> dict:
        return {"text": "the engine routes every capsule through a governed pipeline today"}

    # -- tests ------------------------------------------------------------ #

    def test_gear_is_configured_from_the_engine_config(self) -> None:
        info = get_gear_info(GEAR)
        self.assertTrue(info.is_configured)
        self.assertEqual(info.transport, "http")
        self.assertEqual(info.endpoint, self.server.endpoint)

    def test_config_endpoint_wins_over_environment(self) -> None:
        os.environ["AGENCY_BRIDGE_URL"] = "http://127.0.0.1:1/decoy"
        # The configured verb keeps its own endpoint ("config first").
        self.assertEqual(resolve_endpoint(GEAR), self.server.endpoint)
        # A verb with no config entry falls back to the environment.
        self.assertEqual(resolve_endpoint("message:send"), "http://127.0.0.1:1/decoy")

    def test_probe_gear_and_invoke_reach_the_real_gear(self) -> None:
        probe = probe_gear(GEAR, timeout_seconds=5.0)
        self.assertTrue(probe.get("ok"), probe)
        self.assertEqual(probe["status"], "ok")
        self.assertEqual(probe["tool"], "reference-gear")

        answer = default_bridge(GEAR).invoke(
            self._review_input(),
            gear=GEAR,
            capsule=self._capsule(),
            governance=self._governance(),
        ).require_ok()
        self.assertEqual(answer.gear, GEAR)
        self.assertEqual(answer.status, "ok")
        self.assertIsInstance(answer.output.get("passed"), bool)
        self.assertTrue(answer.output.get("feedback"))
        self.assertGreaterEqual(self.server.requests, 2, "probe and invoke both hit the gear")

    def test_ledger_records_reserved_and_settled_for_the_category(self) -> None:
        default_bridge(GEAR).invoke(
            self._review_input(),
            gear=GEAR,
            capsule=self._capsule(),
            governance=self._governance(),
        ).require_ok()

        events = self.events()
        kinds = [event.get("event") for event in events]
        self.assertEqual(kinds, ["reserved", "settled"], events)
        reserved, settled = events
        self.assertEqual(reserved["category"], "tool")
        self.assertEqual(reserved["service_name"], GEAR)
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "tool")
        self.assertEqual(settled["domain"], "marketing")

    def test_kill_switch_blocks_the_socket(self) -> None:
        os.environ["AGENCY_SPEND_KILL"] = "1"
        spend_control.reset_caches()

        bridge = default_bridge(GEAR)
        with self.assertRaises(SpendCapExceeded) as ctx:
            bridge.invoke(
                self._review_input(),
                gear=GEAR,
                capsule=self._capsule(),
                governance=self._governance(),
            )
        self.assertIn("kill switch", str(ctx.exception))
        self.assertEqual(self.server.requests, 0, "the kill switch refused before any socket opened")

        refusals = [event for event in self.events() if event.get("status") == "refused"]
        self.assertTrue(any(event.get("category") == "tool" for event in refusals), refusals)

    def test_retry_settles_each_failed_attempt_before_reauthorizing(self) -> None:
        self.server.fail_first = 1  # one 503, then a valid answer
        bridge = HttpBridge(
            self.server.endpoint,
            gear=GEAR,
            token=self.token,
            max_retries=1,
            base_backoff=0.0,  # no sleeping in the test
        )
        bridge.invoke(
            self._review_input(),
            gear=GEAR,
            capsule=self._capsule(),
            governance=self._governance(),
        ).require_ok()

        events = self.events()
        kinds = [event.get("event") for event in events]
        self.assertEqual(kinds, ["reserved", "settled", "reserved", "settled"], events)
        self.assertEqual(events[1]["status"], "failed", "the refused attempt is settled")
        self.assertEqual(events[3]["status"], "succeeded")

    def test_design_image_provider_comes_from_config_not_hf_token(self) -> None:
        from workers import design

        os.environ["HF_TOKEN"] = "hf_present_but_ignored"
        self.assertEqual(design.resolve_image_provider(), "pollinations")

        # Fliping the config switches the backend (config is authoritative).
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["design"]["image_provider"] = "huggingface"
        self.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self.assertEqual(design.resolve_image_provider(), "huggingface")


if __name__ == "__main__":
    unittest.main()
