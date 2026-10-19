"""The worker seam: ``BaseWorker.run_tools`` wires model, gears, money and trace.

This is the integration point a real worker calls, so the test asserts the
*whole* contract rather than the loop's internals:

- the tools published to the model are the safe gears only;
- the capsule + governance envelopes reach the gear (no bare calls);
- usage is folded into ``metadata.usage`` and the history line is written;
- a scripted tool cycle completes and its exchanges land on the manifest;
- a spend refusal escapes (never converted into a "recovered" answer);
- running offline through the real offline gateway ends on the first turn —
  the offline engine does not pretend to make tool decisions.

No network and no real capsule directory is needed: the worker is a minimal
subclass, and the capsule path is a scratch directory.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from capsule_engine import BaseWorker, State
from capsule_engine.gateway import AssistantTurn, OfflineModelGateway, SpendCapExceeded
from capsule_engine.tool_bridge import tool_name_for


class _Worker(BaseWorker):
    """A minimal worker: ``execute`` is never called by these tests."""

    name = "tool_seam_test"
    handles = frozenset({State.IN_PROGRESS})

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        return manifest


class _ScriptedGateway:
    """Replays fixed turns, recording what it was offered."""

    def __init__(self, *turns: AssistantTurn) -> None:
        self._turns = list(turns)
        self.offered: list[list[dict[str, Any]]] = []

    def complete_turn(self, manifest, messages, tools=None, *, temperature=None):
        self.offered.append(list(tools or []))
        if not self._turns:
            raise AssertionError("scripted gateway ran out of turns")
        return self._turns.pop(0)


def _turn(content: str = "", calls: list[dict[str, Any]] | None = None, **usage: int) -> AssistantTurn:
    return AssistantTurn(
        content,
        list(calls or []),
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, **usage},
    )


def _call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


class WorkerToolSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="so_tool_seam_")
        self.capsule = Path(self._tmp.name) / "cap_seam"
        self.capsule.mkdir()
        self.manifest: dict[str, Any] = {
            "capsule_id": "cap_seam",
            "state": "in_progress",
            "domain": "marketing",
            "attempt": 0,
            "history": [],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_offline_gateway_answers_without_tools(self) -> None:
        worker = _Worker(OfflineModelGateway())
        result = worker.run_tools(
            str(self.capsule),
            self.manifest,
            [{"role": "user", "content": "Write a launch note."}],
            offline=True,
        )
        self.assertEqual(result.stopped_reason, "final")
        self.assertTrue(result.content)
        self.assertEqual(result.tool_calls, 0)

    def test_safe_gears_are_offered_and_irreversible_are_not(self) -> None:
        gateway = _ScriptedGateway(_turn("done"))
        worker = _Worker(gateway)
        worker.run_tools(
            str(self.capsule), self.manifest, [{"role": "user", "content": "go"}], offline=True
        )
        names = {tool["function"]["name"] for tool in gateway.offered[0]}
        self.assertIn(tool_name_for("text:draft"), names)
        self.assertNotIn(tool_name_for("social:publish"), names)
        self.assertNotIn(tool_name_for("message:send"), names)

    def test_gear_restriction_is_honoured(self) -> None:
        gateway = _ScriptedGateway(_turn("done"))
        worker = _Worker(gateway)
        worker.run_tools(
            str(self.capsule),
            self.manifest,
            [{"role": "user", "content": "go"}],
            gears=["text:review"],
            offline=True,
        )
        names = [tool["function"]["name"] for tool in gateway.offered[0]]
        self.assertEqual(names, [tool_name_for("text:review")])

    def test_tool_cycle_records_exchange_usage_and_history(self) -> None:
        gateway = _ScriptedGateway(
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "A launch note."})]),
            _turn("Drafted.", prompt_tokens=11, completion_tokens=4, total_tokens=15),
        )
        worker = _Worker(gateway)
        result = worker.run_tools(
            str(self.capsule), self.manifest, [{"role": "user", "content": "go"}], offline=True
        )
        self.assertTrue(result.ok)
        self.assertEqual(self.manifest["metadata"]["gear_exchanges"][0]["gear"], "text:draft")
        self.assertEqual(self.manifest["metadata"]["usage"]["prompt_tokens"], 11)
        self.assertEqual(self.manifest["metadata"]["usage"]["total_tokens"], 15)
        self.assertTrue(any("tool_loop" in entry.get("why", "") for entry in self.manifest["history"]))

    def test_capsule_and_governance_envelopes_are_sent(self) -> None:
        seen: dict[str, Any] = {}

        class _CaptureRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                seen["capsule"] = kwargs.get("capsule")
                seen["governance"] = kwargs.get("governance")

        import capsule_engine.tool_bridge as tool_bridge

        original = tool_bridge.GearToolRunner
        tool_bridge.GearToolRunner = _CaptureRunner  # type: ignore[assignment]
        try:
            worker = _Worker(_ScriptedGateway(_turn("done")))
            worker.run_tools(
                str(self.capsule), self.manifest, [{"role": "user", "content": "go"}], offline=True
            )
        finally:
            tool_bridge.GearToolRunner = original  # type: ignore[assignment]
        self.assertEqual(seen["capsule"]["id"], "cap_seam")
        self.assertEqual(seen["capsule"]["domain"], "marketing")
        self.assertEqual(seen["governance"]["domain"], "marketing")
        self.assertIn("autonomy_level", seen["governance"])

    def test_spend_refusal_escapes(self) -> None:
        class _Broke:
            def complete_turn(self, *args: Any, **kwargs: Any) -> AssistantTurn:
                raise SpendCapExceeded("daily budget exhausted")

        worker = _Worker(_Broke())
        with self.assertRaises(SpendCapExceeded):
            worker.run_tools(
                str(self.capsule), self.manifest, [{"role": "user", "content": "go"}], offline=True
            )

    def test_bounds_are_forwarded(self) -> None:
        forever = [
            _turn("", [_call(tool_name_for("text:draft"), {"brief": "x"}, f"c{i}")])
            for i in range(4)
        ]
        worker = _Worker(_ScriptedGateway(*forever))
        result = worker.run_tools(
            str(self.capsule),
            self.manifest,
            [{"role": "user", "content": "go"}],
            offline=True,
            max_steps=2,
            max_tool_calls=1,
        )
        self.assertEqual(result.stopped_reason, "max_tool_calls")
        self.assertEqual(result.tool_calls, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
