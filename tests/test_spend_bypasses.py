"""Session C: every migrated spend path is gated, metered, and kill-switched.

Three properties, five migrated paths (marketing copy, design image, the n8n
publish webhook, the Directus bridge, and ``HttpBridge`` ``social:publish``):

1. **The KILL SWITCH blocks every one of them** — with ``AGENCY_SPEND_KILL=1``
   each path refuses with ``SpendCapExceeded`` (or the handoff-level refusal
   that wraps it) and the transport stub records *zero* calls: the refusal
   happens before any socket opens.
2. **A cap refusal also precedes any network** — with the daily budget at $0
   the model, image and tool paths all refuse before the transport stub is
   touched, and the refusal itself is evidence in the ledger.
3. **The ledger records the migrated calls** — each path reserves and settles
   its own event with the right ``category`` / ``provider`` / ``service_name``
   (model, image, tool) through a *stubbed* transport: no real network, no
   model calls, per the session's ground rules.

Plus the sandbox lock (item 6): a ``live=True`` + ``auto_approve`` trial —
exactly what ``CapsuleSandbox.run_trial`` performs in its publish phase —
cannot reach a real publish bridge, proven by running the real
``PublishingWorker`` under the overlay with a spy on ``urlopen``.

Harness: the scratch config + ledger from ``tests.test_spend_control``
(``AGENCY_ENGINE_CONFIG`` / ``AGENCY_SPEND_LEDGER`` point into the vault, so
no test ever appends to the repository's real ``data/finance/``).
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import REPO_ROOT

import contextlib
import importlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from capsule_engine import spend_control
from capsule_engine.gateway import SpendCapExceeded

from tests.test_spend_control import (
    MODEL,
    SpendGateTestCase,
    _FakeHTTPResponse,
    _completion_payload,
    _spend_config,
)

#: Credentials/config the parent harness does not manage. Popped in setUp,
#: restored in tearDown — a leaked key would let a later test build a real
#: gateway or pick up the repository's .env values.
_EXTRA_ENV = (
    "AGENCY_LLM_API_KEY",
    "AGENCY_LLM_BASE_URL",
    "AGENCY_LLM_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "HF_TOKEN",
    "HF_IMAGE_MODEL",
    "N8N_SECRET",
    "OPERATIONS_WEBHOOK_SECRET",
    "N8N_WEBHOOK_URL",
    "AGENCY_BRIDGE_URL",
    "AGENCY_BRIDGE_URL_SOCIAL_PUBLISH",
    "AGENCY_BRIDGE_TOKEN",
    "AGENCY_BRIDGE_CONFIG",
    "AGENCY_IMAGE_PROVIDER",
)

#: Transport stub target: every gated path in this suite goes through
#: ``urllib.request.urlopen`` (module attribute lookup at call time).
URL_OPEN = "urllib.request.urlopen"

#: ``workers.design`` binds ``from urllib.request import urlopen`` at import
#: time, so its transport must be patched at THAT name — patching
#: ``urllib.request.urlopen`` would miss it and let a real request escape.
DESIGN_URL_OPEN = "workers.design.urlopen"


def _sector_worker(sector: str):
    """Import ``data/domains/<sector>/worker_script.py`` like the dispatcher runs it.

    Sector scripts are subprocess scripts, not packages: their own directory
    goes on ``sys.path`` first so the sibling ``agent_script`` import resolves
    (same convention as ``tests/test_ticket_writers_atomic.py``).
    """
    sector_dir = str(REPO_ROOT / "data" / "domains" / sector)
    if sector_dir not in sys.path:
        sys.path.insert(0, sector_dir)
    return importlib.import_module(f"data.domains.{sector}.worker_script")


def _gear_answer(request_bytes: bytes) -> bytes:
    """A contract-valid ``social:publish`` answer echoing the request envelope."""
    from capsule_engine.bridge import BRIDGE_VERSION, payload_sha256
    from capsule_engine.manifest import USAGE_KEYS

    request = json.loads(request_bytes.decode("utf-8"))
    channel = str(request["input"]["channel"])
    output = {"receipt_id": f"receipt-{channel}", "channel": channel}
    body = {
        "bridge_version": BRIDGE_VERSION,
        "gear": request["gear"],
        "request_id": request["request_id"],
        "status": "ok",
        "output": output,
        "usage": {key: 0 for key in USAGE_KEYS},
        "engine": {"tool": "test-gear", "version": "1", "latency_ms": 1},
        "output_sha256": payload_sha256(output),
    }
    return json.dumps(body).encode("utf-8")


class MigratedPathTestCase(SpendGateTestCase):
    """Scratch spend config/ledger + a clean credential environment per test."""

    def setUp(self) -> None:
        super().setUp()
        self._saved_extra = {key: os.environ.get(key) for key in _EXTRA_ENV}
        for key in _EXTRA_ENV:
            os.environ.pop(key, None)
        spend_control.reset_caches()

    def tearDown(self) -> None:
        for key, value in self._saved_extra.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    # -- helpers ---------------------------------------------------------- #

    def marketing_payload(self) -> dict:
        return {
            "ticket_id": "T-SPEND-1",
            "request": "Write a concise LinkedIn post about sovereign infrastructure",
            "client_id": "client_test",
        }

    def ledger_kinds(self) -> list[str]:
        return [event.get("event") for event in self.ledger_events()]

    def assert_refusal_recorded(self, *, category: str) -> None:
        """This test's own refusal landed in the ledger (cost 0) — evidence, not silence."""
        refusals = [
            event
            for event in self.ledger_events()
            if event.get("status") == "refused"
        ]
        self.assertTrue(
            any(refusal.get("category") == category for refusal in refusals),
            f"a {category!r} refusal must be recorded, got: {refusals}",
        )
        self.assertTrue(
            all(refusal.get("cost_usd", 0) == 0 for refusal in refusals),
            "refusals cost nothing — they never reached the provider",
        )


class KillSwitchBlocksEveryMigratedPath(MigratedPathTestCase):
    """AGENCY_SPEND_KILL=1 stops all five paths with zero bytes on the wire."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["AGENCY_SPEND_KILL"] = "1"

    def _deliverable(self, name: str, copy_text: str = "# approved copy\n") -> Path:
        directory = self.tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "copy.md").write_text(copy_text, encoding="utf-8")
        return directory

    def test_marketing_copy_refuses_before_network(self) -> None:
        mkt = _sector_worker("marketing")
        with mock.patch.object(mkt, "classify", return_value="reversible"):
            with mock.patch.object(mkt, "DELIVERABLES", str(self.tmp / "deliv")):
                with mock.patch.dict(os.environ, {"AGENCY_LLM_API_KEY": "test-key"}):
                    with mock.patch(URL_OPEN) as urlopen:
                        res = mkt.execute(self.marketing_payload())
        self.assertEqual(res["result"], "needs_revision")
        self.assertIn("spend gate refused", res["reason"])
        self.assertIn("kill switch", res["reason"])
        urlopen.assert_not_called()
        self.assert_refusal_recorded(category="model")

    def test_design_image_refuses_before_network(self) -> None:
        des = _sector_worker("design")
        directory = self._deliverable("design_kill")
        payload = {
            "ticket_id": "T-SPEND-DESIGN",
            "context": {"deliverable_dir": str(directory)},
        }
        with mock.patch.dict(
            os.environ, {"AGENCY_OFFLINE_MODE": "1", "HF_TOKEN": "hf_test_token"}
        ):
            with mock.patch(DESIGN_URL_OPEN) as urlopen:
                res = des.execute(payload)
        self.assertEqual(res["result"], "needs_revision")
        self.assertIn("spend gate refused image generation", res["reason"])
        self.assertIn("kill switch", res["reason"])
        urlopen.assert_not_called()
        self.assert_refusal_recorded(category="image")

    def test_operations_webhook_refuses_before_network(self) -> None:
        ops = _sector_worker("operations")
        directory = self._deliverable("ops_kill")
        handoff = json.dumps(
            {"ticket_id": "T-SPEND-OPS", "context": {"deliverable_dir": str(directory)}}
        )
        out = io.StringIO()
        # Delivery goes through the social:publish gear: configure it, then the
        # KILL SWITCH must refuse before any socket opens.
        with mock.patch.dict(
            os.environ,
            {
                "AGENCY_BRIDGE_URL_SOCIAL_PUBLISH": "http://127.0.0.1:8799/social",
                "AGENCY_BRIDGE_TOKEN": "test-token",
            },
        ):
            with mock.patch.object(sys, "argv", ["worker_script.py", handoff]):
                with mock.patch(URL_OPEN) as urlopen:
                    with contextlib.redirect_stdout(out):
                        ops.main()
        result = json.loads(out.getvalue())
        self.assertEqual(result["result"], "needs_revision")
        self.assertIn("spend gate refused publishing", result["reason"])
        self.assertIn("kill switch", result["reason"])
        urlopen.assert_not_called()
        self.assert_refusal_recorded(category="publish")

    def test_directus_client_refuses_before_network(self) -> None:
        from orchestration import directus_bridge

        client = directus_bridge.DirectusClient(base_url="http://127.0.0.1:9")
        with mock.patch(URL_OPEN) as urlopen:
            with self.assertRaises(SpendCapExceeded) as ctx:
                client.login()
        self.assertIn("kill switch", str(ctx.exception))
        urlopen.assert_not_called()
        self.assert_refusal_recorded(category="tool")

    def test_directus_sweep_is_refused_before_constructing_a_client(self) -> None:
        from orchestration import directus_bridge

        with mock.patch(URL_OPEN) as urlopen:
            stats = directus_bridge.sync_directus_cycle()
        self.assertEqual(stats, {"produced": 0, "published": 0, "errors": 0})
        urlopen.assert_not_called()

    def test_social_publish_refuses_before_network(self) -> None:
        from capsule_engine.bridge import HttpBridge

        bridge = HttpBridge(
            "http://127.0.0.1:9/bridge", gear="social:publish", token="test-token"
        )
        with mock.patch(URL_OPEN) as urlopen:
            with self.assertRaises(SpendCapExceeded) as ctx:
                bridge.invoke(
                    {"channel": "linkedin", "text": "approved post"},
                    capsule={"id": "cap-1", "state": "approved"},
                    governance={"domain": "marketing", "autonomy_level": "L2"},
                )
        self.assertIn("kill switch", str(ctx.exception))
        urlopen.assert_not_called()
        self.assert_refusal_recorded(category="publish")


class CapRefusalPrecedesAnyNetwork(MigratedPathTestCase):
    """Exhausted budget refuses before the transport, for model, image and tool."""

    def test_daily_cap_zero_blocks_model_image_and_tool_paths(self) -> None:
        self.write_config(_spend_config(daily_usd=0.0))

        # model path: the marketing worker through the real gateway
        mkt = _sector_worker("marketing")
        with mock.patch.object(mkt, "classify", return_value="reversible"):
            with mock.patch.object(mkt, "DELIVERABLES", str(self.tmp / "deliv")):
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENCY_LLM_API_KEY": "test-key",
                        "AGENCY_LLM_BASE_URL": "https://example.invalid/v1",
                        "AGENCY_LLM_MODEL": MODEL,
                    },
                ):
                    with mock.patch(URL_OPEN) as urlopen:
                        res = mkt.execute(self.marketing_payload())
        self.assertEqual(res["result"], "needs_revision")
        self.assertIn("daily spend cap", res["reason"])
        urlopen.assert_not_called()

        # image path: the design worker's gated fetcher
        des = _sector_worker("design")
        directory = self.tmp / "design_cap"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "copy.md").write_text("# approved copy\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"AGENCY_OFFLINE_MODE": "1", "HF_TOKEN": "hf_test_token"}
        ):
            with mock.patch(DESIGN_URL_OPEN) as urlopen:
                res = des.execute(
                    {"ticket_id": "T-CAP", "context": {"deliverable_dir": str(directory)}}
                )
        self.assertEqual(res["result"], "needs_revision")
        self.assertIn("daily spend cap", res["reason"])
        urlopen.assert_not_called()

        # tool path: message:send through the bridge (unpriced gear with estimate > 0)
        from capsule_engine.bridge import HttpBridge

        bridge = HttpBridge(
            "http://127.0.0.1:9/bridge", gear="message:send", token="test-token"
        )
        with mock.patch(URL_OPEN) as urlopen:
            with self.assertRaises(SpendCapExceeded) as ctx:
                bridge.invoke(
                    {"channel": "whatsapp", "to": "+123456789", "text": "status report"},
                    capsule={"id": "cap-1", "state": "approved"},
                    governance={"domain": "marketing", "autonomy_level": "L2"},
                )
        self.assertIn("daily", str(ctx.exception))
        urlopen.assert_not_called()

        refusals = [
            event for event in self.ledger_events() if event.get("status") == "refused"
        ]
        self.assertGreaterEqual(len(refusals), 3, "each path's refusal is recorded")
        self.assertTrue(all(refusal.get("cost_usd", 0) == 0 for refusal in refusals))


class LedgerRecordsMigratedCalls(MigratedPathTestCase):
    """Each migrated path reserves and settles its own event (stubbed transport)."""

    def test_marketing_copy_call_is_recorded_as_model_spend(self) -> None:
        mkt = _sector_worker("marketing")
        payload = _completion_payload(
            "Fresh generated copy", prompt_tokens=111, completion_tokens=22,
            total_tokens=133,
        )
        with mock.patch.object(mkt, "classify", return_value="reversible"):
            with mock.patch.object(mkt, "DELIVERABLES", str(self.tmp / "deliv")):
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENCY_LLM_API_KEY": "test-key",
                        "AGENCY_LLM_BASE_URL": "https://example.invalid/v1",
                        "AGENCY_LLM_MODEL": MODEL,
                    },
                ):
                    with mock.patch(
                        URL_OPEN, return_value=_FakeHTTPResponse(payload)
                    ) as urlopen:
                        res = mkt.execute(self.marketing_payload())
        self.assertEqual(res["result"], "done", res)
        urlopen.assert_called_once()

        events = self.ledger_events()
        self.assertEqual(self.ledger_kinds(), ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "model")
        self.assertEqual(settled["service_name"], MODEL)
        self.assertEqual(settled["domain"], "marketing")
        self.assertEqual(settled["provider"], "example.invalid")
        self.assertEqual(settled["tokens"]["prompt_tokens"], 111)
        self.assertGreater(settled["cost_usd"], 0.0)

        copy_file = Path(self.tmp / "deliv" / "client_test" / "post_T-SPEND-1" / "copy.md")
        self.assertTrue(copy_file.is_file(), "the deliverable was written")

    def test_design_image_fetch_is_recorded_as_image_spend(self) -> None:
        des = _sector_worker("design")
        directory = self.tmp / "design_ledger"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "copy.md").write_text("# approved copy for the visual", encoding="utf-8")
        image_bytes = b"\xff\xd8\xff\xe0stub-jpeg-bytes"
        with mock.patch.dict(os.environ, {"AGENCY_OFFLINE_MODE": "1"}):
            with mock.patch(
                DESIGN_URL_OPEN, return_value=_FakeHTTPResponse(image_bytes)
            ) as urlopen:
                res = des.execute(
                    {"ticket_id": "T-IMG", "context": {"deliverable_dir": str(directory)}}
                )
        self.assertEqual(res["result"], "pending_approval", res)
        urlopen.assert_called_once()

        events = self.ledger_events()
        self.assertEqual(self.ledger_kinds(), ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "image")
        self.assertEqual(settled["service_name"], "pollinations-free")
        self.assertEqual(settled["provider"], "pollinations")
        self.assertTrue(settled["priced"])
        self.assertEqual(settled["cost_usd"], 0.0)
        self.assertTrue((directory / "image.jpg").is_file())

    def test_operations_publish_is_recorded_as_publish_spend(self) -> None:
        ops = _sector_worker("operations")
        directory = self.tmp / "ops_ledger"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "copy.md").write_text("Approved copy for n8n delivery.\n", encoding="utf-8")
        handoff = json.dumps(
            {"ticket_id": "T-OPS", "context": {"deliverable_dir": str(directory)}}
        )
        out = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "AGENCY_BRIDGE_URL_SOCIAL_PUBLISH": "http://127.0.0.1:9/social",
                "AGENCY_BRIDGE_TOKEN": "test-token",
            },
        ):
            with mock.patch.object(sys, "argv", ["worker_script.py", handoff]):
                with mock.patch(
                    URL_OPEN,
                    side_effect=lambda req, timeout=None: _FakeHTTPResponse(_gear_answer(req.data)),
                ) as urlopen:
                    with contextlib.redirect_stdout(out):
                        ops.main()
        result = json.loads(out.getvalue())
        self.assertEqual(result["result"], "published", result)
        urlopen.assert_called_once()

        events = self.ledger_events()
        self.assertEqual(self.ledger_kinds(), ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "publish")
        self.assertEqual(settled["service_name"], "social:publish")
        self.assertEqual(settled["provider"], "127.0.0.1:9")
        self.assertEqual(settled["domain"], "operations")

    def test_directus_calls_are_recorded_as_tool_spend(self) -> None:
        from orchestration import directus_bridge

        client = directus_bridge.DirectusClient(base_url="http://127.0.0.1:9")
        token_payload = json.dumps(
            {"data": {"access_token": "test-token"}}
        ).encode("utf-8")
        with mock.patch(
            URL_OPEN, return_value=_FakeHTTPResponse(token_payload)
        ) as urlopen:
            logged_in = client.login()
        self.assertTrue(logged_in)
        urlopen.assert_called_once()

        events = self.ledger_events()
        self.assertEqual(self.ledger_kinds(), ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "tool")
        self.assertEqual(settled["service_name"], "directus-api")
        self.assertEqual(settled["provider"], "directus")
        self.assertEqual(settled["domain"], "operations")

    def test_social_publish_exchange_is_recorded_as_publish_category(self) -> None:
        from capsule_engine.bridge import HttpBridge

        bridge = HttpBridge(
            "http://127.0.0.1:9/bridge", gear="social:publish", token="test-token"
        )
        with mock.patch(
            URL_OPEN,
            side_effect=lambda req, timeout=None: _FakeHTTPResponse(_gear_answer(req.data)),
        ) as urlopen:
            answer = bridge.invoke(
                {"channel": "linkedin", "text": "approved post"},
                capsule={"id": "cap-1", "state": "approved"},
                governance={"domain": "marketing", "autonomy_level": "L2"},
            ).require_ok()
        urlopen.assert_called_once()
        self.assertEqual(answer.output["channel"], "linkedin")

        events = self.ledger_events()
        self.assertEqual(self.ledger_kinds(), ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["category"], "publish")
        self.assertEqual(settled["cost_usd"], 0.0)
        self.assertEqual(settled["service_name"], "social:publish")
        self.assertEqual(settled["provider"], "127.0.0.1:9")
        self.assertEqual(settled["domain"], "marketing")

    def test_daily_cap_exhausted_approved_capsule_still_publishes(self) -> None:
        """With the daily cap exhausted, an approved capsule still publishes."""
        from capsule_engine.bridge import HttpBridge
        from capsule_engine.manifest import (
            State,
            artifact_sha256,
            load_manifest,
            save_manifest,
            transition,
        )
        from workers.publishing import PublishingWorker

        # Exhaust the daily cap
        self.write_config(_spend_config(daily_usd=0.0))

        cap_dir = self.tmp / "cap_publish_daily_cap_exhausted"
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / "copy.md").write_bytes(b"Ready to publish content.\n")
        manifest = {
            "capsule_id": "cap_publish_daily_cap_exhausted",
            "state": "pending_approval",
            "domain": "marketing",
            "channels": ["linkedin"],
            "created_at": "2026-10-15T00:00:00+00:00",
            "updated_at": "2026-10-15T00:00:00+00:00",
            "attempt": 0,
            "history": [],
            "metadata": {
                "domain": "marketing",
                "artifact_sha256": artifact_sha256(cap_dir, "copy.md"),
            },
        }
        save_manifest(cap_dir, manifest)
        transition(
            manifest,
            State.APPROVED,
            by="human_approver",
            why="approved for delivery",
            capsule_path=cap_dir,
        )
        save_manifest(cap_dir, manifest)

        bridge = HttpBridge(
            "http://127.0.0.1:9/bridge", gear="social:publish", token="test-token"
        )
        worker = PublishingWorker(bridge=bridge)
        with mock.patch(
            URL_OPEN,
            side_effect=lambda req, timeout=None: _FakeHTTPResponse(_gear_answer(req.data)),
        ) as urlopen:
            result = worker.run(str(cap_dir))

        urlopen.assert_called_once()
        self.assertEqual(result["state"], "published")
        self.assertEqual(load_manifest(cap_dir)["state"], "published")

    def test_kill_switch_blocks_approved_capsule_from_publishing(self) -> None:
        """With the kill switch ON, approved capsule publishing is blocked."""
        from capsule_engine.bridge import HttpBridge
        from capsule_engine.exceptions import WorkerExecutionError
        from capsule_engine.manifest import (
            State,
            artifact_sha256,
            save_manifest,
            transition,
        )
        from workers.publishing import PublishingWorker

        self.write_config(_spend_config(daily_usd=10.0, kill=True))

        cap_dir = self.tmp / "cap_publish_kill_switch_blocked"
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / "copy.md").write_bytes(b"Ready to publish content.\n")
        manifest = {
            "capsule_id": "cap_publish_kill_switch_blocked",
            "state": "pending_approval",
            "domain": "marketing",
            "channels": ["linkedin"],
            "created_at": "2026-10-15T00:00:00+00:00",
            "updated_at": "2026-10-15T00:00:00+00:00",
            "attempt": 0,
            "history": [],
            "metadata": {
                "domain": "marketing",
                "artifact_sha256": artifact_sha256(cap_dir, "copy.md"),
            },
        }
        save_manifest(cap_dir, manifest)
        transition(
            manifest,
            State.APPROVED,
            by="human_approver",
            why="approved for delivery",
            capsule_path=cap_dir,
        )
        save_manifest(cap_dir, manifest)

        bridge = HttpBridge(
            "http://127.0.0.1:9/bridge", gear="social:publish", token="test-token"
        )
        worker = PublishingWorker(bridge=bridge)
        with mock.patch(URL_OPEN) as urlopen:
            with self.assertRaises(WorkerExecutionError) as ctx:
                worker.run(str(cap_dir))
        urlopen.assert_not_called()
        self.assertIn("kill switch", str(ctx.exception))
        self.assert_refusal_recorded(category="publish")


class SandboxPublishLock(MigratedPathTestCase):
    """A live + auto_approve sandbox trial can never reach a real publish bridge."""

    def _approved_by_sandbox(self, cap_dir: Path) -> None:
        """Mirror ``CapsuleSandbox.run_trial``'s auto_approve phase, under the overlay."""
        from capsule_engine import State
        from capsule_engine.manifest import load_manifest, save_manifest, transition

        manifest = load_manifest(cap_dir)
        transition(
            manifest,
            State.APPROVED,
            by="sandbox_reviewer",
            why="sandbox trial auto-approval verification",
            capsule_path=cap_dir,
        )
        save_manifest(cap_dir, manifest)

    def test_live_auto_approve_trial_cannot_open_a_socket(self) -> None:
        from capsule_engine.exceptions import WorkerExecutionError
        from capsule_engine.manifest import save_manifest
        from capsule_engine.sandbox import trial_env_overrides
        from workers.publishing import PublishingWorker

        cap_dir = self.tmp / "cap_sandbox_publish_lock"
        cap_dir.mkdir(parents=True, exist_ok=True)
        copy_bytes = b"Approved copy for delivery.\n"
        (cap_dir / "copy.md").write_bytes(copy_bytes)
        from capsule_engine.manifest import artifact_sha256

        save_manifest(
            cap_dir,
            {
                "capsule_id": "cap_sandbox_publish_lock",
                "state": "pending_approval",
                "domain": "marketing",
                "channels": ["linkedin"],
                "created_at": "2026-10-15T00:00:00+00:00",
                "updated_at": "2026-10-15T00:00:00+00:00",
                "attempt": 0,
                "history": [],
                "metadata": {
                    "domain": "marketing",
                    # The approval transition re-hashes the bound artifact
                    # (TOCTOU), so the capsule carries its digest exactly like
                    # a real one.
                    "artifact_sha256": artifact_sha256(cap_dir, "copy.md"),
                },
            },
        )

        # A real-looking bridge is configured, then the live overlay blanks it.
        saved = dict(os.environ)
        os.environ["AGENCY_BRIDGE_URL"] = "http://127.0.0.1:8799/bridge"
        os.environ["AGENCY_BRIDGE_URL_SOCIAL_PUBLISH"] = "http://127.0.0.1:8799/social"
        try:
            os.environ.update(trial_env_overrides(live=True))
            from capsule_engine.bridge import resolve_endpoint
            from workers.publishing import GEAR

            self.assertIsNone(resolve_endpoint(GEAR))

            self._approved_by_sandbox(cap_dir)  # what auto_approve does first
            with mock.patch(URL_OPEN) as urlopen:
                with self.assertRaises(WorkerExecutionError) as ctx:
                    PublishingWorker().run(str(cap_dir))
            urlopen.assert_not_called()
        finally:
            os.environ.clear()
            os.environ.update(saved)

        self.assertIn("no delivery gear configured", str(ctx.exception))
        # The refusal is recorded on the capsule: a human can read why.
        from capsule_engine.manifest import load_manifest

        history = load_manifest(cap_dir).get("history") or []
        self.assertTrue(
            any("no delivery gear configured" in str(entry.get("why", "")) for entry in history),
            f"publish failure must be recorded on the capsule, got: {history[-3:]}",
        )


if __name__ == "__main__":
    unittest.main()
