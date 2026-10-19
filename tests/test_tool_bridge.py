"""Tool bridge: the gear vocabulary published to a model, and executed safely.

What is under test (see ``capsule_engine.tool_bridge``):

- the tool list is *derived* from ``GEAR_CONTRACTS`` — every required field
  appears as required, every allowed field as a property, and nothing else;
- irreversible gears (publish, send) are withheld unless explicitly enabled;
- name mapping is reversible and refuses unknown names;
- a gear refusal becomes a tool error, not an exception;
- an unknown tool / bad arguments never reach the bridge;
- a successful call is recorded on the capsule (``gear_exchanges``) and its
  token counters folded into ``metadata.usage``.

No network: every test injects ``offline=True`` or a stub bridge.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

import json
import unittest
from unittest import mock

from capsule_engine.bridge import (
    GEAR_CONTRACTS,
    BridgeProtocolError,
    BridgeRefusalError,
    BridgeResult,
)
from capsule_engine.tool_bridge import (
    FIELD_SCHEMAS,
    GEAR_TOOL_PREFIX,
    IRREVERSIBLE_GEARS,
    GearToolRunner,
    gear_for_tool,
    gear_tools,
    tool_name_for,
)


class ToolSchemaTests(unittest.TestCase):
    """The published schema is the contract, not a copy of it."""

    def test_every_gear_is_published_except_irreversible(self) -> None:
        tools = gear_tools()
        published = {gear_for_tool(t["function"]["name"]) for t in tools}
        expected = set(GEAR_CONTRACTS) - IRREVERSIBLE_GEARS
        self.assertEqual(published, expected)

    def test_irreversible_gears_withheld_by_default(self) -> None:
        names = {t["function"]["name"] for t in gear_tools()}
        for gear in IRREVERSIBLE_GEARS:
            self.assertNotIn(tool_name_for(gear), names)

    def test_irreversible_gears_available_on_request(self) -> None:
        names = {t["function"]["name"] for t in gear_tools(include_irreversible=True)}
        for gear in IRREVERSIBLE_GEARS:
            self.assertIn(tool_name_for(gear), names)

    def test_only_filter_restricts_and_refuses_unknown(self) -> None:
        tools = gear_tools(only=["text:draft", "text:review"])
        self.assertEqual(len(tools), 2)
        with self.assertRaises(BridgeProtocolError):
            gear_tools(only=["nope:nothing"])

    def test_required_and_allowed_fields_match_contract(self) -> None:
        for tool in gear_tools(include_irreversible=True):
            gear = gear_for_tool(tool["function"]["name"])
            contract = GEAR_CONTRACTS[gear]
            params = tool["function"]["parameters"]
            self.assertEqual(set(params["properties"]), set(contract.input_allowed), gear)
            self.assertEqual(tuple(params["required"]), contract.input_required, gear)
            self.assertFalse(params["additionalProperties"], gear)

    def test_every_published_property_has_a_schema(self) -> None:
        for tool in gear_tools(include_irreversible=True):
            for name, schema in tool["function"]["parameters"]["properties"].items():
                self.assertIn(name, FIELD_SCHEMAS)
                self.assertIn("type", schema)

    def test_schema_is_deterministic(self) -> None:
        self.assertEqual(gear_tools(), gear_tools())

    def test_tool_names_are_openai_safe(self) -> None:
        import re

        pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        for tool in gear_tools(include_irreversible=True):
            self.assertRegex(tool["function"]["name"], pattern)

    def test_name_mapping_round_trips(self) -> None:
        for gear in GEAR_CONTRACTS:
            self.assertEqual(gear_for_tool(tool_name_for(gear)), gear)

    def test_unknown_tool_name_is_refused(self) -> None:
        with self.assertRaises(BridgeProtocolError):
            gear_for_tool("delete_everything")
        with self.assertRaises(BridgeProtocolError):
            gear_for_tool(GEAR_TOOL_PREFIX + "not__a__gear")


class _StubBridge:
    """A bridge whose single answer is scripted by the test."""

    def __init__(self, result: BridgeResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    def invoke(self, input_data, **kwargs):
        self.calls.append({"input": dict(input_data), **kwargs})
        if self._error is not None:
            raise self._error
        return self._result


def _ok(gear: str, output: dict) -> BridgeResult:
    return BridgeResult(
        gear=gear,
        request_id="req_0123456789ab",
        status="ok",
        output=output,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        engine={"tool": "stub-gear"},
        output_sha256="a" * 64,
    )


class GearToolRunnerTests(unittest.TestCase):
    """Execution: one call, one outcome, no hidden exceptions."""

    def test_offline_call_succeeds_through_the_real_validator(self) -> None:
        runner = GearToolRunner(offline=True)
        outcome = runner.call(
            tool_name_for("text:draft"), {"brief": "Write a launch note for the new engine."}
        )
        self.assertTrue(outcome.ok, outcome.error_detail)
        self.assertEqual(outcome.gear, "text:draft")
        self.assertIn("text", outcome.output)
        self.assertEqual(outcome.tool, "offline-bridge")

    def test_refusal_is_a_tool_error_not_an_exception(self) -> None:
        bridge = _StubBridge(
            error=BridgeRefusalError("spend_cap", "daily budget exhausted")
        )
        runner = GearToolRunner(bridge_factory=lambda gear, offline=None: bridge)
        outcome = runner.call(tool_name_for("text:draft"), {"brief": "x"})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error_code, "bridge_refusal_error")
        self.assertIn("daily budget exhausted", outcome.error_detail)

    def test_unknown_tool_never_reaches_the_bridge(self) -> None:
        bridge = _StubBridge(result=_ok("text:draft", {"text": "x"}))
        runner = GearToolRunner(bridge_factory=lambda gear, offline=None: bridge)
        outcome = runner.call("delete_everything", {})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "bridge_protocol_error")
        self.assertEqual(bridge.calls, [])

    def test_bad_contract_input_is_reported_to_the_model(self) -> None:
        runner = GearToolRunner(offline=True)
        # `data:classify` needs at least two labels; one is a contract breach.
        outcome = runner.call(
            tool_name_for("data:classify"), {"text": "hello", "labels": ["only_one"]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "bridge_protocol_error")
        self.assertIn("labels", outcome.error_detail)

    def test_missing_required_field_is_reported(self) -> None:
        runner = GearToolRunner(offline=True)
        outcome = runner.call(tool_name_for("text:draft"), {})
        self.assertFalse(outcome.ok)
        self.assertIn("brief", outcome.error_detail)

    def test_non_object_arguments_are_refused(self) -> None:
        runner = GearToolRunner(offline=True)
        outcome = runner.call(tool_name_for("text:draft"), ["not", "an", "object"])  # type: ignore[arg-type]
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "bridge_protocol_error")

    def test_tool_message_shape_is_openai_compatible(self) -> None:
        runner = GearToolRunner(offline=True)
        outcome = runner.call(
            tool_name_for("text:draft"), {"brief": "hi"}, tool_call_id="call_1"
        )
        message = outcome.as_message()
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["tool_call_id"], "call_1")
        self.assertEqual(message["name"], tool_name_for("text:draft"))
        body = json.loads(message["content"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["gear"], "text:draft")

    def test_error_message_is_json_and_machine_readable(self) -> None:
        runner = GearToolRunner(offline=True)
        outcome = runner.call(tool_name_for("text:draft"), {})
        body = json.loads(outcome.as_message()["content"])
        self.assertFalse(body["ok"])
        self.assertIn("code", body["error"])
        self.assertTrue(body["error"]["detail"])

    def test_successful_call_is_recorded_on_the_capsule(self) -> None:
        manifest: dict = {"capsule_id": "cap_1", "state": "in_progress"}
        runner = GearToolRunner(offline=True)
        outcome = runner.call(
            tool_name_for("text:draft"),
            {"brief": "Record me."},
            manifest=manifest,
        )
        self.assertTrue(outcome.ok)
        exchanges = manifest["metadata"]["gear_exchanges"]
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0]["gear"], "text:draft")
        self.assertEqual(exchanges[0]["status"], "ok")
        self.assertEqual(exchanges[0]["by"], "tool_loop")
        # The offline gear costs nothing but the call is still counted: zeros
        # plus one call, never a fabricated token count.
        usage = manifest["metadata"]["usage"]
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["total_tokens"], 0)

    def test_reported_usage_is_folded_into_the_capsule(self) -> None:
        manifest: dict = {"capsule_id": "cap_1", "state": "in_progress"}
        bridge = _StubBridge(result=_ok("text:draft", {"text": "x"}))
        runner = GearToolRunner(bridge_factory=lambda gear, offline=None: bridge)
        runner.call(tool_name_for("text:draft"), {"brief": "x"}, manifest=manifest)
        usage = manifest["metadata"]["usage"]
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 15)

    def test_audit_failure_never_fails_the_call(self) -> None:
        manifest: dict = {"capsule_id": "cap_1", "metadata": "not-an-object"}
        runner = GearToolRunner(offline=True)
        outcome = runner.call(
            tool_name_for("text:draft"), {"brief": "still ok"}, manifest=manifest
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(manifest["metadata"], "not-an-object")

    def test_capsule_and_governance_travel_with_the_call(self) -> None:
        bridge = _StubBridge(result=_ok("text:draft", {"text": "x"}))
        runner = GearToolRunner(
            bridge_factory=lambda gear, offline=None: bridge,
            capsule={"id": "cap_9", "state": "in_progress", "domain": "marketing"},
            governance={"domain": "marketing", "autonomy_level": "L0"},
        )
        runner.call(tool_name_for("text:draft"), {"brief": "x"})
        sent = bridge.calls[0]
        self.assertEqual(sent["capsule"]["id"], "cap_9")
        self.assertEqual(sent["governance"]["domain"], "marketing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
