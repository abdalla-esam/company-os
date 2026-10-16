"""Spend gate tests: caps, reservations, kill switch, ledger, subscriptions.

Every test is hermetic: a scratch vault, a scratch ``engine.json`` (the spend
section only) and a scratch ledger, wired through ``AGENCY_ENGINE_CONFIG`` /
``AGENCY_SPEND_LEDGER``. No network, no model calls — the gateway's HTTP
transport is stubbed with a fake ``urlopen``, and the offline gateway proves
it stays free and unblocked.

Money model under test (see ``capsule_engine.spend_control``):
- ``authorize()`` before every attempt, ``record()`` after: a failed or
  timed-out call still counts at its estimate;
- caps: daily / monthly / per-domain daily / calls per day, all refused
  before the request (fail closed);
- the kill switch (file flag and ``AGENCY_SPEND_KILL``) blocks the gateway
  *and* the image paths, and the beat refuses to spawn anything;
- unknown models are estimated conservatively (never zero) with ``priced:false``;
- subscriptions (config + ``record_subscription``) amortize into the totals;
- concurrent appenders never lose a ledger line;
- an unreadable ledger refuses the call.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from capsule_engine import spend_control
from capsule_engine.gateway import (
    GatewayError,
    GatewayTimeoutError,
    ModelGateway,
    OfflineModelGateway,
    SpendCapExceeded,
)
from orchestration import access_control, api

# estimate = 1000 prompt tokens / 1k * 0.00015 + 500 completion / 1k * 0.0006
TEST_ESTIMATE_USD = 0.00045
MODEL = "gpt-4o-mini"

_ENV_KEYS = (
    "AGENCY_ENGINE_CONFIG",
    "AGENCY_SPEND_LEDGER",
    "AGENCY_SPEND_KILL",
    "HF_TOKEN",
    "HF_IMAGE_MODEL",
    "AGENCY_OFFLINE_MODE",
    "AGENCY_BRIDGE_URL",
    "AGENCY_BRIDGE_URL_SOCIAL_PUBLISH",
    "FB_PAGE_ID",
    "META_SYSTEM_TOKEN",
    "N8N_PUBLISH_WEBHOOK_URL",
)


def _spend_config(**overrides) -> dict:
    spend = {
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
            MODEL: {"prompt_per_1k": 0.00015, "completion_per_1k": 0.0006},
        },
        "image_costs": {
            "black-forest-labs/FLUX.1-schnell": 0.003,
            "pollinations-free": 0.0,
        },
        "subscriptions": [],
    }
    spend.update(overrides)
    return {"version": 1, "spend": spend}


class _FakeHTTPResponse:
    """Minimal urlopen stand-in: context manager with bounded read()."""

    status = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int = -1) -> bytes:
        if size is not None and size >= 0:
            return self._payload[:size]
        return self._payload

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _completion_payload(content: str = "generated copy", **usage) -> bytes:
    body = {"choices": [{"message": {"content": content}}]}
    if usage:
        body["usage"] = usage
    return json.dumps(body).encode("utf-8")


class SpendGateTestCase(unittest.TestCase):
    """Scratch config + ledger per test; env restored after every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="so_spend_test_")
        self.tmp = Path(self._tmp.name).resolve()
        self.ledger = self.tmp / "finance" / "usage_ledger.jsonl"
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self.write_config(_spend_config())
        os.environ["AGENCY_SPEND_LEDGER"] = str(self.ledger)
        spend_control.reset_caches()
        from capsule_engine import policy
        policy.reset_cache()

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        spend_control.reset_caches()
        from capsule_engine import policy
        policy.reset_cache()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------- #

    def write_config(self, config: dict) -> Path:
        path = self.tmp / "engine.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.environ["AGENCY_ENGINE_CONFIG"] = str(path)
        spend_control.reset_caches()
        from capsule_engine import policy
        policy.reset_cache()
        return path

    def ledger_events(self) -> list[dict]:
        if not self.ledger.is_file():
            return []
        return [
            json.loads(line)
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def gateway(**overrides) -> ModelGateway:
        defaults = dict(api_key="test-key", base_url="https://example.invalid/v1")
        defaults.update(overrides)
        return ModelGateway(**defaults)

    def authorize_ok(self, **kwargs):
        kwargs.setdefault("model", MODEL)
        return spend_control.authorize(**kwargs)


class TestCaps(SpendGateTestCase):
    """Each cap refuses before the request, with the refusal recorded."""

    def test_refuses_over_daily_cap(self) -> None:
        self.write_config(_spend_config(daily_usd=0.0004))
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("daily", str(ctx.exception))
        self.assertTrue(
            any(event.get("status") == "refused" for event in self.ledger_events()),
            "a refusal must leave evidence in the ledger",
        )

    def test_refuses_over_monthly_cap(self) -> None:
        self.write_config(_spend_config(daily_usd=10.0, monthly_usd=0.0004))
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("monthly", str(ctx.exception))

    def test_refuses_over_per_domain_daily_cap(self) -> None:
        # 0.0008 fits one call (0.00045) but not two on the same branch
        self.write_config(_spend_config(per_domain_daily_usd=0.0008))
        first = self.authorize_ok(domain="marketing")
        spend_control.record(first, status=spend_control.STATUS_SUCCEEDED)
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok(domain="marketing")
        self.assertIn("per-domain", str(ctx.exception))
        # a different branch still has its own budget
        reservation = self.authorize_ok(domain="design")
        self.assertEqual(reservation.domain, "design")
        spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)

    def test_refuses_over_max_calls_per_day(self) -> None:
        self.write_config(_spend_config(max_calls_per_day=2))
        for _ in range(2):
            reservation = self.authorize_ok()
            spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("call cap", str(ctx.exception))

    def test_unknown_domain_refused(self) -> None:
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok(domain="nonexistent-branch")
        self.assertIn("unknown domain", str(ctx.exception))
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok(domain="MARKETING!")

    def test_disabled_config_refuses(self) -> None:
        self.write_config(_spend_config(enabled=False))
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("disabled", str(ctx.exception))
        self.assertIsNotNone(spend_control.blocked_reason())

    def test_spend_cap_error_is_a_gateway_error(self) -> None:
        self.assertTrue(issubclass(SpendCapExceeded, GatewayError))

    def test_tool_calls_counted_separately_and_do_not_exhaust_model_budget(self) -> None:
        self.write_config(_spend_config(max_calls_per_day=2))
        # Authorize and record 5 tool calls
        for i in range(5):
            res = spend_control.authorize(
                model="directus-api",
                category=spend_control.CATEGORY_TOOL,
                domain="core",
            )
            spend_control.record(res, status=spend_control.STATUS_SUCCEEDED)

        snap = spend_control.status_snapshot()
        self.assertEqual(snap["today"]["tool_calls"], 5)
        self.assertEqual(snap["today"]["calls"], 0)

        # Model calls are still under their budget (0/2)
        r1 = self.authorize_ok()
        spend_control.record(r1, status=spend_control.STATUS_SUCCEEDED)
        r2 = self.authorize_ok()
        spend_control.record(r2, status=spend_control.STATUS_SUCCEEDED)

        snap2 = spend_control.status_snapshot()
        self.assertEqual(snap2["today"]["calls"], 2)
        self.assertEqual(snap2["today"]["tool_calls"], 5)

        # 3rd model call is refused by max_calls_per_day
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("call cap reached", str(ctx.exception))

        # Tool calls can still proceed (not blocked by model/image call cap)
        r_tool = spend_control.authorize(
            model="directus-api",
            category=spend_control.CATEGORY_TOOL,
            domain="core",
        )
        spend_control.record(r_tool, status=spend_control.STATUS_SUCCEEDED)

    def test_directus_api_priced_at_zero(self) -> None:
        res = spend_control.authorize(
            model="directus-api",
            category=spend_control.CATEGORY_TOOL,
            domain="core",
        )
        self.assertEqual(res.estimate_usd, 0.0)
        self.assertTrue(res.priced)
        settled = spend_control.record(res, status=spend_control.STATUS_SUCCEEDED)
        self.assertEqual(settled["cost_usd"], 0.0)

    def test_publish_category_zero_cost_and_bypasses_spend_caps(self) -> None:
        self.write_config(_spend_config(daily_usd=0.0, max_calls_per_day=1))
        # Model call is refused because daily cap is exhausted
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok()

        # Publish category is authorized even when daily_usd and calls_today are exhausted
        res = spend_control.authorize(
            model="social:publish",
            category=spend_control.CATEGORY_PUBLISH,
            domain="marketing",
        )
        self.assertEqual(res.category, spend_control.CATEGORY_PUBLISH)
        self.assertEqual(res.estimate_usd, 0.0)
        settled = spend_control.record(res, status=spend_control.STATUS_SUCCEEDED)
        self.assertEqual(settled["cost_usd"], 0.0)
        self.assertEqual(settled["category"], "publish")


class TestReservations(SpendGateTestCase):
    """A reservation is written before the call and settled after it."""

    def test_failed_call_settles_at_its_estimate(self) -> None:
        gateway = self.gateway(max_retries=0, base_backoff=0.0)
        with mock.patch(
            "urllib.request.urlopen", side_effect=socket.timeout("stub transport")
        ):
            with self.assertRaises(GatewayTimeoutError):
                gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.0)
        events = self.ledger_events()
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds, ["reserved", "settled"])
        settled = events[1]
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["cost_usd"], TEST_ESTIMATE_USD)
        self.assertEqual(settled["category"], "model")
        self.assertEqual(settled["service_name"], MODEL)
        totals = spend_control.status_snapshot()
        self.assertEqual(totals["today"]["calls"], 1)
        self.assertAlmostEqual(totals["today"]["spent_usd"], TEST_ESTIMATE_USD)

    def test_successful_call_settles_at_actual_token_cost(self) -> None:
        gateway = self.gateway(max_retries=0, base_backoff=0.0)
        payload = _completion_payload(
            "written", prompt_tokens=2000, completion_tokens=0, total_tokens=2000
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)
        ):
            text = gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.0)
        self.assertEqual(text, "written")
        settled = self.ledger_events()[1]
        self.assertEqual(settled["status"], "succeeded")
        # actual tokens: 2000/1k * 0.00015 = 0.0003 (differs from the estimate)
        self.assertAlmostEqual(settled["cost_usd"], 0.0003)
        self.assertEqual(settled["tokens"]["prompt_tokens"], 2000)

    def test_unpriced_model_is_estimated_not_zero(self) -> None:
        reservation = self.authorize_ok(model="mystery-model-9000")
        self.assertFalse(reservation.priced)
        self.assertEqual(reservation.estimate_usd, 0.02)
        event = self.ledger_events()[0]
        self.assertFalse(event["priced"])
        self.assertGreater(event["estimate_usd"], 0.0)
        spend_control.record(reservation, status=spend_control.STATUS_FAILED)
        settled = self.ledger_events()[1]
        self.assertEqual(settled["cost_usd"], 0.02)  # the estimate stands

    def test_retries_authorize_per_attempt(self) -> None:
        gateway = self.gateway(max_retries=1, base_backoff=0.0)
        with mock.patch(
            "urllib.request.urlopen", side_effect=socket.timeout("stub transport")
        ):
            with self.assertRaises(GatewayTimeoutError):
                gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.0)
        events = self.ledger_events()
        self.assertEqual(
            [event["event"] for event in events],
            ["reserved", "settled", "reserved", "settled"],
        )


class TestKillSwitch(SpendGateTestCase):
    """The kill switch blocks the gateway AND the image paths, before any HTTP."""

    def test_blocks_gateway_before_http(self) -> None:
        os.environ["AGENCY_SPEND_KILL"] = "1"
        gateway = self.gateway(max_retries=0, base_backoff=0.0)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(SpendCapExceeded):
                gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.0)
            urlopen.assert_not_called()

    def test_blocks_image_paths(self) -> None:
        from workers import design

        os.environ["AGENCY_SPEND_KILL"] = "1"
        os.environ["HF_TOKEN"] = "hf_test_token"
        with mock.patch.object(design, "urlopen") as urlopen:
            with self.assertRaises(SpendCapExceeded):
                design.fetch_huggingface("a corporate skyline", 5.0)
            with self.assertRaises(SpendCapExceeded):
                design.fetch_pollinations("a corporate skyline", 5.0)
            with self.assertRaises(SpendCapExceeded):
                # no silent fall-through to the free provider when the gate says stop
                design.fetch_free_image("a corporate skyline", 5.0)
            urlopen.assert_not_called()

    def test_beat_refuses_before_spawning_anything(self) -> None:
        from orchestration import pulse

        os.environ["AGENCY_SPEND_KILL"] = "1"
        with mock.patch.object(
            pulse, "run_cmd", side_effect=AssertionError("must not spawn")
        ):
            result = pulse.beat()
        self.assertEqual(result.get("status"), "refused")
        self.assertIn("kill switch", str(result.get("reason")))

    def test_file_flag_and_env_reported_in_status(self) -> None:
        result = spend_control.set_kill_switch(True, by="unit-test")
        self.assertTrue(result["kill"])
        snapshot = spend_control.status_snapshot()
        self.assertTrue(snapshot["kill_switch"])
        self.assertIsNotNone(snapshot["blocked_reason"])
        spend_control.set_kill_switch(False, by="unit-test")
        self.assertIsNone(spend_control.blocked_reason())

    def test_out_of_band_flag_flip_is_picked_up_without_reset(self) -> None:
        """An external edit of ``spend.kill`` lands on the next check (mtime re-key).

        This is another process editing the file — no ``reset_caches()`` and no
        ``set_kill_switch`` in between: the config cache is keyed on
        (path, mtime, size), so the flip is visible immediately (the contract
        is "within one second").
        """
        path = Path(os.environ["AGENCY_ENGINE_CONFIG"])
        raw = json.loads(path.read_text(encoding="utf-8"))

        # warm the config cache with kill off
        reservation = self.authorize_ok()
        spend_control.record(reservation, status="failed")
        self.assertFalse(spend_control.kill_active())

        raw["spend"]["kill"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")  # out-of-band edit
        self.assertTrue(spend_control.kill_active())
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok()

        raw["spend"]["kill"] = False
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertFalse(spend_control.kill_active())
        self.authorize_ok()  # paid calls resume


class TestLedgerIntegrity(SpendGateTestCase):
    """Concurrency, corruption and the offline path."""

    def test_concurrent_writers_lose_no_entries(self) -> None:
        self.write_config(_spend_config(max_calls_per_day=1000, daily_usd=10.0))
        workers = 12
        barrier = threading.Barrier(workers)
        failures: list[BaseException] = []

        def one_call() -> None:
            try:
                barrier.wait(timeout=10)
                reservation = self.authorize_ok()
                spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)
            except BaseException as exc:  # surfaced below, never silently lost
                failures.append(exc)

        threads = [threading.Thread(target=one_call) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(failures, [], f"worker threads failed: {failures!r}")
        events = self.ledger_events()
        kinds = [event["event"] for event in events]
        self.assertEqual(len(events), workers * 2, "no line may be lost or interleaved")
        self.assertEqual(kinds.count("reserved"), workers)
        self.assertEqual(kinds.count("settled"), workers)
        snapshot = spend_control.status_snapshot()
        self.assertEqual(snapshot["today"]["calls"], workers)
        self.assertAlmostEqual(
            snapshot["today"]["spent_usd"], workers * TEST_ESTIMATE_USD, places=6
        )

    def test_unreadable_ledger_fails_closed(self) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()  # a directory is an unreadable, unwritable ledger
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok()

    def test_corrupt_ledger_fails_closed(self) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("not json at all\n", encoding="utf-8")
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok()

    def test_offline_gateway_unaffected(self) -> None:
        os.environ["AGENCY_SPEND_KILL"] = "1"
        os.environ["AGENCY_OFFLINE_MODE"] = "1"
        gateway = OfflineModelGateway()
        text = gateway.complete({"metadata": {"topic": "Sovereign"}}, "first line")
        self.assertIn("Dummy copy", text)
        self.assertFalse(self.ledger.exists(), "a free call must not touch the ledger")


class TestSubscriptions(SpendGateTestCase):
    """Recurring costs (config + ledger) feed the daily/monthly totals."""

    def test_record_subscription_blocks_spend_until_budget_fits(self) -> None:
        self.write_config(_spend_config(daily_usd=1.0, monthly_usd=100.0))
        record = spend_control.record_subscription(
            "VPS Oracle", 60.8, provider="oracle", domain="operations"
        )
        self.assertEqual(record["category"], "subscription")
        self.assertAlmostEqual(record["monthly_usd"], 60.8)
        # $60.80 / 30.4 days = $2.00/day > $1.00 daily cap
        with self.assertRaises(SpendCapExceeded) as ctx:
            self.authorize_ok()
        self.assertIn("daily", str(ctx.exception))

    def test_config_subscriptions_are_amortized_and_filtered(self) -> None:
        self.write_config(
            _spend_config(
                daily_usd=1.0,
                monthly_usd=100.0,
                subscriptions=[
                    {"name": "Directus", "provider": "directus", "monthly_usd": 5.0,
                     "domain": "operations"},
                    {"name": "n8n", "cost_usd": 8.0, "interval": "weekly",
                     "domain": "operations", "active": False},
                ],
            )
        )
        snapshot = spend_control.status_snapshot()
        subs = snapshot["subscriptions"]
        self.assertAlmostEqual(subs["monthly_usd"], 5.0)
        self.assertAlmostEqual(subs["daily_amortized_usd"], 5.0 / 30.4, places=6)
        # the call fits: $5/30.4 + $0.00045 < $1.00
        reservation = self.authorize_ok()
        spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)

    def test_newest_ledger_record_wins_per_service(self) -> None:
        spend_control.record_subscription("VPS", 60.8, provider="oracle", domain="operations")
        spend_control.record_subscription("VPS", 12.0, provider="oracle", domain="operations")
        snapshot = spend_control.status_snapshot()
        self.assertAlmostEqual(snapshot["subscriptions"]["monthly_usd"], 12.0)
        items = [
            item for item in snapshot["subscriptions"]["items"] if item["name"] == "VPS"
        ]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "ledger")

    def test_ledger_subscription_event_shape(self) -> None:
        spend_control.record_subscription(
            "n8n hosting", 4.0, interval="monthly", provider="n8n", domain="operations"
        )
        event = self.ledger_events()[0]
        self.assertEqual(event["event"], "subscription")
        self.assertEqual(event["category"], "subscription")
        self.assertEqual(event["provider"], "n8n")
        self.assertEqual(event["service_name"], "n8n hosting")
        self.assertEqual(event["interval"], "monthly")
        self.assertEqual(event["cost_usd"], 4.0)

    def test_invalid_subscription_arguments_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            spend_control.record_subscription("", 5.0)
        with self.assertRaises(ValueError):
            spend_control.record_subscription("X", -1.0)
        with self.assertRaises(ValueError):
            spend_control.record_subscription("X", 5.0, interval="sometimes")


class TestSandboxPublishLock(SpendGateTestCase):
    """A sandbox trial — live or not, auto_approve or not — never reaches a real publish bridge."""

    def test_live_trials_still_see_model_credentials(self) -> None:
        from capsule_engine.sandbox import trial_env_overrides

        overlay = trial_env_overrides(live=True)
        self.assertNotIn("AGENCY_OFFLINE_MODE", overlay)  # live keeps the real gateway
        self.assertNotIn("HF_TOKEN", overlay)             # ...and the real image path
        self.assertEqual(overlay.get("FB_PAGE_ID"), "")
        self.assertEqual(overlay.get("META_SYSTEM_TOKEN"), "")
        self.assertEqual(overlay.get("N8N_PUBLISH_WEBHOOK_URL"), "")

    def test_offline_trials_force_the_offline_gateway(self) -> None:
        from capsule_engine.sandbox import trial_env_overrides

        overlay = trial_env_overrides(live=False)
        self.assertEqual(overlay.get("AGENCY_OFFLINE_MODE"), "1")
        self.assertEqual(overlay.get("HF_TOKEN"), "")

    def test_publish_bridge_is_unresolvable_under_the_overlay(self) -> None:
        from capsule_engine.bridge import resolve_endpoint
        from capsule_engine.sandbox import trial_env_overrides
        from workers.publishing import GEAR

        os.environ["AGENCY_BRIDGE_URL"] = "http://127.0.0.1:8799/bridge"
        os.environ["AGENCY_BRIDGE_URL_SOCIAL_PUBLISH"] = "http://127.0.0.1:8799/social"
        saved = dict(os.environ)
        try:
            os.environ.update(trial_env_overrides(live=True))
            self.assertIsNone(resolve_endpoint(GEAR))
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestPolicyWiring(SpendGateTestCase):
    """Capabilities, route map and the shipped config agree."""

    def test_finance_capability_and_routes_declared(self) -> None:
        from capsule_engine.capabilities import ALL_CAPABILITIES, CAPABILITY_FINANCE_CONTROL
        from orchestration.access_control import declared_rules

        self.assertIn(CAPABILITY_FINANCE_CONTROL, ALL_CAPABILITIES)
        rules = declared_rules()
        self.assertEqual(rules[("POST", "/api/finance/kill")], "finance:control")
        self.assertEqual(rules[("GET", "/api/finance/status")], "factory:read")

    def test_shipped_config_parses(self) -> None:
        from tests._bootstrap import REPO_ROOT

        real = json.loads((REPO_ROOT / "config" / "engine.json").read_text("utf-8"))
        os.environ["AGENCY_ENGINE_CONFIG"] = str(REPO_ROOT / "config" / "engine.json")
        spend_control.reset_caches()
        config = spend_control.load_config(force=True)
        self.assertTrue(config.enabled)
        self.assertEqual(config.daily_usd, 1.0)
        self.assertEqual(config.monthly_usd, 15.0)
        self.assertEqual(config.max_calls_per_day, 300)
        self.assertIn(MODEL, config.model_prices)
        self.assertEqual(real["spend"]["per_domain_daily_usd"], 1.0)


class TestFinanceEndpoints(SpendGateTestCase):
    """The finance routes invoked exactly as the API layer invokes them.

    Auth is the middleware's job (the route -> capability map is asserted
    above); these tests bind the endpoint *functions* to the live module: the
    status route must render the truth, and the kill route must flip the real
    flag in the scratch config and refuse paid calls afterwards.
    """

    @staticmethod
    def _request(path: str) -> Request:
        """A request carrying a root principal (identity from the credential)."""
        principal = access_control.Principal.legacy_root("spend-endpoint-tester")
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "query_string": b"",
                "state": {"principal": principal},
            }
        )

    def test_status_endpoint_reports_the_live_snapshot(self) -> None:
        snap = asyncio.run(api.get_finance_status())
        self.assertEqual(snap["status"], "success")
        self.assertTrue(snap["available"])
        self.assertIsNone(snap["blocked_reason"])
        self.assertEqual(snap["caps"]["daily_usd"], 10.0)
        self.assertEqual(snap["today"]["calls"], 0)
        self.assertEqual(snap["subscriptions"]["items"], [])

        reservation = self.authorize_ok(domain="marketing")
        # the gateway's real failure path: no usage block survives, only the estimate
        spend_control.record(reservation, status="failed")
        snap = asyncio.run(api.get_finance_status())
        # a failed call still counts at its estimate — visible through the route
        self.assertEqual(snap["today"]["calls"], 1)
        self.assertAlmostEqual(
            snap["today"]["spent_usd"], TEST_ESTIMATE_USD, places=6
        )
        self.assertEqual(snap["today"]["by_domain_usd"]["marketing"],
                         TEST_ESTIMATE_USD)

    def test_unrecognized_token_keys_never_settle_at_zero(self) -> None:
        """A token mapping without prompt_tokens/completion_tokens keeps the estimate."""
        reservation = self.authorize_ok()
        spend_control.record(
            reservation, status="succeeded", tokens={"prompt": 50000, "completion": 50000}
        )
        settled = self.ledger_events()[-1]
        self.assertEqual(settled["cost_usd"], TEST_ESTIMATE_USD)
        snap = asyncio.run(api.get_finance_status())
        self.assertAlmostEqual(snap["today"]["spent_usd"], TEST_ESTIMATE_USD, places=6)

    def test_kill_endpoint_flips_the_flag_and_refuses_paid_calls(self) -> None:
        on = asyncio.run(
            api.set_finance_kill_switch(
                api.FinanceKillRequest(on=True),
                self._request("/api/finance/kill"),
            )
        )
        self.assertEqual(on["status"], "success")
        self.assertTrue(on["kill"])
        with self.assertRaises(SpendCapExceeded):
            self.authorize_ok()
        self.assertTrue(asyncio.run(api.get_finance_status())["kill_switch"])

        off = asyncio.run(
            api.set_finance_kill_switch(
                api.FinanceKillRequest(on=False),
                self._request("/api/finance/kill"),
            )
        )
        self.assertFalse(off["kill"])
        # paid calls resume, and the flag really landed in the scratch config
        self.authorize_ok()
        self.assertFalse(
            json.loads(Path(os.environ["AGENCY_ENGINE_CONFIG"]).read_text("utf-8"))
            ["spend"]["kill"]
        )


if __name__ == "__main__":
    unittest.main()
