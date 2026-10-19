"""Tool loop: a model and the gears, bounded at both ends.

What is under test (see ``capsule_engine.tool_loop``):

- a turn with no tool calls ends the loop immediately;
- a tool call is executed through the real runner, appended as a
  ``role="tool"`` message, and the model is asked again;
- an unknown tool, a non-allowed gear, and malformed arguments are answered
  with an error result — the model may correct itself, the loop never crashes;
- the two hard bounds (turns, tool calls) stop the loop and name themselves;
- usage is summed across turns and the transcript is returned whole;
- a spend refusal propagates: a budget stop must never be re-planned around;
- the caller's message list is not mutated.

No network: the gateway is scripted and the gears run on ``OfflineBridge``.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

import json
import unittest
from typing import Any

from capsule_engine.gateway import AssistantTurn, GatewayError, SpendCapExceeded
from capsule_engine.tool_bridge import GearToolRunner, gear_tools, tool_name_for
from capsule_engine.tool_loop import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOOL_CALLS,
    LoopResult,
    ToolLoop,
)

TOOLS = gear_tools()


def _call(name: str, arguments: dict[str, Any] | str, call_id: str = "call_1") -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "name": name, "arguments": raw}


def _turn(content: str = "", calls: list[dict[str, Any]] | None = None, **usage: int) -> AssistantTurn:
    return AssistantTurn(
        content,
        list(calls or []),
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, **usage},
    )


class ScriptedGateway:
    """A gateway that replays a fixed list of turns and records each request."""

    def __init__(self, *turns: AssistantTurn) -> None:
        self._turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]] | None] = []

    def complete_turn(self, manifest, messages, tools=None, *, temperature=None):
        self.seen.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        if not self._turns:
            raise AssertionError("ScriptedGateway ran out of turns")
        return self._turns.pop(0)


class FailingGateway:
    """A gateway that fails on the first turn with a given exception."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def complete_turn(self, manifest, messages, tools=None, *, temperature=None):
        raise self._error


def _loop(gateway: Any, **kwargs: Any) -> ToolLoop:
    return ToolLoop(gateway, GearToolRunner(offline=True), **kwargs)


class FinalAnswerTests(unittest.TestCase):
    """The simple path: the model answers without asking for a tool."""

    def test_single_turn_ends_the_loop(self) -> None:
        gateway = ScriptedGateway(_turn("Here is the answer."))
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.stopped_reason, "final")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "Here is the answer.")
        self.assertEqual(result.turns, 1)
        self.assertEqual(result.tool_calls, 0)
        self.assertFalse(result.used_tools)

    def test_tools_are_published_to_the_model(self) -> None:
        gateway = ScriptedGateway(_turn("done"))
        _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(gateway.tools_seen[0], TOOLS)
        self.assertTrue(any(t["function"]["name"] == tool_name_for("text:draft") for t in TOOLS))

    def test_caller_messages_are_not_mutated(self) -> None:
        messages = [{"role": "user", "content": "go"}]
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "Write it."})]),
            _turn("done"),
        )
        _loop(gateway).run({"capsule_id": "c1"}, messages, TOOLS)
        self.assertEqual(messages, [{"role": "user", "content": "go"}])

    def test_empty_final_answer_is_not_a_success(self) -> None:
        gateway = ScriptedGateway(_turn(""))
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.stopped_reason, "final")
        self.assertFalse(result.ok)


class ToolCycleTests(unittest.TestCase):
    """A model asks, a gear answers, the model finishes."""

    def test_one_tool_call_then_final(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "Launch note."})]),
            _turn("Drafted."),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.content, "Drafted.")
        self.assertEqual(result.turns, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertTrue(result.used_tools)
        self.assertTrue(result.outcomes[0].ok)
        self.assertEqual(result.outcomes[0].gear, "text:draft")

    def test_transcript_carries_the_tool_exchange(self) -> None:
        gateway = ScriptedGateway(
            _turn("working", [_call(tool_name_for("text:draft"), {"brief": "x"}, "call_7")]),
            _turn("done"),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        roles = [message["role"] for message in result.messages]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        tool_message = result.messages[-1]
        self.assertEqual(tool_message["tool_call_id"], "call_7")
        self.assertEqual(tool_message["name"], tool_name_for("text:draft"))
        self.assertTrue(json.loads(tool_message["content"])["ok"])

    def test_model_sees_the_tool_result_on_the_next_turn(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"})]),
            _turn("done"),
        )
        _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        second_request = gateway.seen[1]
        self.assertEqual(second_request[-1]["role"], "tool")

    def test_usage_is_summed_across_turns(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"})], prompt_tokens=10, total_tokens=10),
            _turn("done", prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.usage["prompt_tokens"], 17)
        self.assertEqual(result.usage["completion_tokens"], 3)
        self.assertEqual(result.usage["total_tokens"], 20)

    def test_exchange_is_recorded_on_the_manifest(self) -> None:
        manifest: dict[str, Any] = {"capsule_id": "c1", "state": "in_progress"}
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"})]),
            _turn("done"),
        )
        _loop(gateway).run(manifest, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(manifest["metadata"]["gear_exchanges"][0]["gear"], "text:draft")

    def test_events_are_emitted_in_order(self) -> None:
        events: list[str] = []
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"})]),
            _turn("done"),
        )
        loop = ToolLoop(
            gateway,
            GearToolRunner(offline=True),
            on_event=lambda event, payload: events.append(event),
        )
        loop.run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(events, ["turn", "tool", "turn", "final"])

    def test_a_broken_observer_never_breaks_the_loop(self) -> None:
        def explode(event: str, payload: Any) -> None:
            raise RuntimeError("observer bug")

        gateway = ScriptedGateway(_turn("done"))
        loop = ToolLoop(gateway, GearToolRunner(offline=True), on_event=explode)
        result = loop.run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertTrue(result.ok)


class RefusalTests(unittest.TestCase):
    """Bad calls come back as data, never as a crash."""

    def test_unknown_tool_is_reported_and_the_loop_continues(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call("delete_everything", {})]),
            _turn("recovered"),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.outcomes[0].error_code, "unknown_tool")
        body = json.loads(result.messages[-1]["content"])
        self.assertFalse(body["ok"])

    def test_gear_outside_the_allowed_set_is_refused(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("social:publish"), {"channel": "x", "text": "y"})]),
            _turn("recovered"),
        )
        loop = _loop(
            gateway,
            allowed_gears=[tool_name_for("text:draft")],
        )
        result = loop.run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.outcomes[0].error_code, "tool_not_allowed")
        self.assertEqual(result.outcomes[0].gear, "social:publish")

    def test_malformed_arguments_are_attributed_to_the_call(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), "{not json")]),
            _turn("recovered"),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.outcomes[0].error_code, "invalid_arguments_json")

    def test_non_object_arguments_are_refused(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), '["a", "b"]')]),
            _turn("recovered"),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.outcomes[0].error_code, "invalid_arguments")

    def test_contract_breach_is_reported_to_the_model(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {})]),
            _turn("recovered"),
        )
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertFalse(result.outcomes[0].ok)
        self.assertIn("brief", result.outcomes[0].error_detail)


class BoundTests(unittest.TestCase):
    """The loop stops on its own terms."""

    def test_max_steps_stops_a_model_that_never_answers(self) -> None:
        forever = [
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"}, f"call_{i}")])
            for i in range(DEFAULT_MAX_STEPS)
        ]
        gateway = ScriptedGateway(*forever)
        result = _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(result.turns, DEFAULT_MAX_STEPS)
        self.assertFalse(result.ok)

    def test_max_tool_calls_stops_fan_out(self) -> None:
        calls = [_call(tool_name_for("text:draft"), {"brief": "x"}, f"call_{i}") for i in range(5)]
        gateway = ScriptedGateway(_turn("", calls), _turn("done"))
        result = _loop(gateway, max_tool_calls=2).run(
            {"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS
        )
        self.assertEqual(result.stopped_reason, "max_tool_calls")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(len(result.outcomes), 2)

    def test_custom_max_steps_is_honoured(self) -> None:
        forever = [
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"}, f"c{i}")])
            for i in range(3)
        ]
        gateway = ScriptedGateway(*forever)
        result = _loop(gateway, max_steps=2, max_tool_calls=9).run(
            {"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS
        )
        self.assertEqual(result.turns, 2)
        self.assertEqual(result.stopped_reason, "max_steps")

    def test_zero_tool_budget_allows_an_answer_but_no_tools(self) -> None:
        gateway = ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"})]),
        )
        result = _loop(gateway, max_tool_calls=0).run(
            {"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS
        )
        self.assertEqual(result.stopped_reason, "max_tool_calls")
        self.assertEqual(result.tool_calls, 0)

    def test_invalid_bounds_are_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            _loop(ScriptedGateway(), max_steps=0)
        with self.assertRaises(ValueError):
            _loop(ScriptedGateway(), max_tool_calls=-1)


class MoneyGateTests(unittest.TestCase):
    """A budget refusal must stop the loop, not be re-planned around."""

    def test_spend_cap_propagates(self) -> None:
        gateway = FailingGateway(SpendCapExceeded("daily budget exhausted"))
        with self.assertRaises(SpendCapExceeded):
            _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)

    def test_gateway_error_propagates(self) -> None:
        gateway = FailingGateway(GatewayError("provider down"))
        with self.assertRaises(GatewayError):
            _loop(gateway).run({"capsule_id": "c1"}, [{"role": "user", "content": "go"}], TOOLS)


class SummaryTests(unittest.TestCase):
    """The log line stays short and credential-free."""

    def test_summary_names_the_stop_reason(self) -> None:
        result = LoopResult(content="x" * 500, stopped_reason="max_steps", turns=3, tool_calls=4)
        line = result.summary()
        self.assertIn("stopped=max_steps", line)
        self.assertIn("turns=3", line)
        self.assertIn("tool_calls=4", line)
        self.assertLess(len(line), 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
