"""Gateway tool turns: the request carries tools, the answer may carry calls.

The gateway is the only network seam, so these tests stub ``urlopen`` and read
the exact JSON body the model would receive. What they pin down:

- ``tools`` (and ``tool_choice="auto"``) are sent **only** when tools are
  offered — a plain completion request keeps its old shape byte for byte;
- a tool-call-only answer (``content: null``) is a success, not the old
  "empty content" error;
- calls are normalised to ``{id, name, arguments}`` with arguments kept as the
  raw JSON string (so a parse failure is attributable to one call);
- malformed or oversized call arrays fail closed;
- ``complete_turn`` reports usage and leaves ``complete`` behaviour intact.

No network: ``urlopen`` is replaced; the spend gate writes to the isolated
test vault, never the repository's ledger.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

import json
import unittest
from unittest import mock

from capsule_engine.gateway import (
    MAX_TOOL_CALLS_PER_TURN,
    AssistantTurn,
    GatewayResponseError,
    ModelGateway,
)

MODEL = "gpt-4o-mini"


class _FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _payload(**message) -> bytes:
    body = {"choices": [{"message": message}], "usage": {"prompt_tokens": 3, "total_tokens": 3}}
    return json.dumps(body).encode("utf-8")


def _call(name: str = "gear_text__draft", arguments: str = '{"brief": "x"}', call_id: str = "call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gear_text__draft",
            "description": "author a deliverable text",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


class ToolRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ModelGateway(
            api_key="test-key", base_url="https://example.invalid/v1", max_retries=0
        )
        self.sent: list[dict] = []

    def _capture(self, payload: bytes):
        def fake_urlopen(request, timeout=None):
            self.sent.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse(payload)

        return mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)

    def test_tools_are_sent_when_offered(self) -> None:
        with self._capture(_payload(content="done")):
            self.gateway._call_llm(
                [{"role": "user", "content": "hi"}], MODEL, 0.2, tools=TOOLS
            )
        self.assertEqual(self.sent[0]["tools"], TOOLS)
        self.assertEqual(self.sent[0]["tool_choice"], "auto")

    def test_no_tools_key_without_tools(self) -> None:
        with self._capture(_payload(content="done")):
            self.gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.2)
        self.assertNotIn("tools", self.sent[0])
        self.assertNotIn("tool_choice", self.sent[0])

    def test_existing_request_shape_is_unchanged(self) -> None:
        with self._capture(_payload(content="done")):
            self.gateway._call_llm([{"role": "user", "content": "hi"}], MODEL, 0.4)
        self.assertEqual(
            self.sent[0],
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.4,
            },
        )


class ToolAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ModelGateway(
            api_key="test-key", base_url="https://example.invalid/v1", max_retries=0
        )

    def _answer(self, payload: bytes, tools=TOOLS) -> str:
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            return self.gateway._call_llm(
                [{"role": "user", "content": "hi"}], MODEL, 0.2, tools=tools
            )

    def test_tool_only_answer_is_accepted(self) -> None:
        text = self._answer(_payload(content=None, tool_calls=[_call()]))
        self.assertEqual(text, "")
        self.assertEqual(len(self.gateway.last_tool_calls), 1)
        call = self.gateway.last_tool_calls[0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["name"], "gear_text__draft")
        self.assertEqual(call["arguments"], '{"brief": "x"}')

    def test_text_and_calls_together_are_kept(self) -> None:
        text = self._answer(_payload(content="working", tool_calls=[_call()]))
        self.assertEqual(text, "working")
        self.assertEqual(len(self.gateway.last_tool_calls), 1)

    def test_object_arguments_are_re_encoded_as_json(self) -> None:
        raw = {"id": "c1", "type": "function", "function": {"name": "x", "arguments": {"brief": "y"}}}
        self._answer(_payload(content=None, tool_calls=[raw]))
        self.assertEqual(
            json.loads(self.gateway.last_tool_calls[0]["arguments"]), {"brief": "y"}
        )

    def test_missing_arguments_become_an_empty_object(self) -> None:
        raw = {"id": "c1", "type": "function", "function": {"name": "x"}}
        self._answer(_payload(content=None, tool_calls=[raw]))
        self.assertEqual(self.gateway.last_tool_calls[0]["arguments"], "{}")

    def test_missing_call_id_gets_a_stable_one(self) -> None:
        raw = {"type": "function", "function": {"name": "x", "arguments": "{}"}}
        self._answer(_payload(content=None, tool_calls=[raw, raw]))
        ids = [call["id"] for call in self.gateway.last_tool_calls]
        self.assertEqual(ids, ["call_0", "call_1"])

    def test_empty_answer_without_tools_still_fails(self) -> None:
        with self.assertRaises(GatewayResponseError):
            self._answer(_payload(content=""), tools=None)

    def test_malformed_calls_fail_closed(self) -> None:
        cases = (
            {"content": None, "tool_calls": "not-a-list"},
            {"content": None, "tool_calls": [{"id": "c", "function": "nope"}]},
            {"content": None, "tool_calls": [{"id": "c", "function": {"name": ""}}]},
            {
                "content": None,
                "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": 42}}],
            },
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(GatewayResponseError):
                    self._answer(_payload(**case))

    def test_too_many_calls_fail_closed(self) -> None:
        calls = [_call(call_id=f"c{i}") for i in range(MAX_TOOL_CALLS_PER_TURN + 1)]
        with self.assertRaises(GatewayResponseError):
            self._answer(_payload(content=None, tool_calls=calls))

    def test_calls_are_reset_between_answers(self) -> None:
        self._answer(_payload(content=None, tool_calls=[_call()]))
        self._answer(_payload(content="plain"))
        self.assertEqual(self.gateway.last_tool_calls, [])


class CompleteTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ModelGateway(
            api_key="test-key", base_url="https://example.invalid/v1", max_retries=0
        )

    def test_turn_reports_content_calls_and_usage(self) -> None:
        payload = _payload(content=None, tool_calls=[_call()])
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            turn = self.gateway.complete_turn(
                {"capsule_id": "c1", "domain": "marketing"},
                [{"role": "user", "content": "hi"}],
                TOOLS,
            )
        self.assertIsInstance(turn, AssistantTurn)
        self.assertTrue(turn.wants_tools)
        self.assertEqual(turn.content, "")
        self.assertEqual(turn.usage["total_tokens"], 3)

    def test_turn_message_is_openai_shaped(self) -> None:
        turn = AssistantTurn("", [{"id": "c1", "name": "gear_x", "arguments": "{}"}], {})
        message = turn.as_message()
        self.assertEqual(message["role"], "assistant")
        self.assertIsNone(message["content"])
        self.assertEqual(message["tool_calls"][0]["type"], "function")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "gear_x")

    def test_plain_turn_has_no_tool_calls_key(self) -> None:
        turn = AssistantTurn("done", [], {})
        self.assertNotIn("tool_calls", turn.as_message())

    def test_complete_still_returns_text(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(_payload(content="copy"))
        ):
            text = self.gateway.complete(
                {"capsule_id": "c1", "domain": "marketing", "metadata": {"topic": "t"}},
                "write something",
            )
        self.assertEqual(text, "copy")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
