#!/usr/bin/env python3
"""tool_loop.py — the bounded agent loop: model ↔ tools, with hard stops.

One task, one transcript, one budget. The loop is deliberately small and
deliberately paranoid:

```
messages -> model turn -> tool calls? -> execute -> append results -> repeat
                       \\-> no calls  -> done
```

Rules, all fail-closed:

- **bounded** — :data:`DEFAULT_MAX_STEPS` turns and :data:`DEFAULT_MAX_TOOL_CALLS`
  executions. A loop that exceeds either stops with ``stopped_reason`` naming
  the bound; it never runs until the money runs out.
- **no invented tools** — a call naming a gear outside the offered set is
  answered with an error result (the model may correct itself) and counted, so
  a model cannot widen its own authority by guessing a name.
- **refusals are data** — a gear refusing, or the contract rejecting an
  argument, becomes a ``role="tool"`` error message. Only
  :class:`capsule_engine.gateway.SpendCapExceeded` propagates: a budget refusal
  must stop the loop, never be re-planned around.
- **arguments are parsed once** — malformed JSON is attributed to that call and
  reported back, not raised at the turn level.
- **every turn is accountable** — usage is summed per turn and the transcript
  is returned whole, so a worker can persist it and a reviewer can read it.

The loop executes nothing itself: the model comes from
:mod:`capsule_engine.gateway` and the tools from
:mod:`capsule_engine.tool_bridge`.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from capsule_engine.bridge import BridgeProtocolError
from capsule_engine.gateway import AssistantTurn
from capsule_engine.manifest import USAGE_KEYS
from capsule_engine.tool_bridge import GearToolRunner, ToolCallOutcome, gear_for_tool

__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_TOOL_CALLS",
    "LoopResult",
    "ToolLoop",
]

#: Model turns per task. Four is enough for read → act → verify → answer, and
#: small enough that a spinning model costs pennies instead of dollars.
DEFAULT_MAX_STEPS = 4
#: Tool executions per task, summed across turns.
DEFAULT_MAX_TOOL_CALLS = 6


@dataclass
class LoopResult:
    """What the loop produced, and why it stopped.

    ``stopped_reason`` is one of ``"final"`` (the model answered without asking
    for a tool), ``"max_steps"``, ``"max_tool_calls"``, or ``"error"``.
    ``content`` is the last assistant text — empty when a bound was hit before
    the model produced one.
    """

    content: str
    stopped_reason: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    outcomes: list[ToolCallOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stopped_reason == "final" and bool(self.content.strip())

    @property
    def used_tools(self) -> bool:
        return self.tool_calls > 0

    def summary(self) -> str:
        """One log-safe line (never a credential, never a full transcript)."""
        return (
            f"tool_loop: stopped={self.stopped_reason} turns={self.turns} "
            f"tool_calls={self.tool_calls} chars={len(self.content)}"
        )


class ToolLoop:
    """Drive a model through at most ``max_steps`` tool-aware turns."""

    def __init__(
        self,
        gateway: Any,
        runner: GearToolRunner,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        allowed_gears: Sequence[str] | None = None,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_tool_calls < 0:
            raise ValueError("max_tool_calls must not be negative")
        self._gateway = gateway
        self._runner = runner
        self._max_steps = int(max_steps)
        self._max_tool_calls = int(max_tool_calls)
        # ``None`` means "whatever the tool list offered"; an explicit set is a
        # second, independent fence against a model naming a gear it was not
        # given (defence in depth: the runner would execute it otherwise).
        self._allowed = (
            frozenset(gear_for_tool(name) for name in allowed_gears)
            if allowed_gears is not None
            else None
        )
        self._on_event = on_event

    # ------------------------------------------------------------------ #

    def run(
        self,
        manifest: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float | None = None,
    ) -> LoopResult:
        """Run the loop; never raises except on a spend refusal or a broken model."""
        transcript = list(messages)
        outcomes: list[ToolCallOutcome] = []
        usage = {key: 0 for key in USAGE_KEYS}
        turns = 0
        executed = 0
        content = ""

        for step in range(self._max_steps):
            turns += 1
            kwargs: dict[str, Any] = {}
            if temperature is not None:
                kwargs["temperature"] = temperature
            turn: AssistantTurn = self._gateway.complete_turn(
                manifest, transcript, tools, **kwargs
            )
            _add(usage, turn.usage)
            self._emit("turn", {"step": step, "tool_calls": len(turn.tool_calls)})

            if not turn.wants_tools:
                content = turn.content
                self._emit("final", {"step": step})
                return LoopResult(
                    content=content,
                    stopped_reason="final",
                    messages=transcript,
                    turns=turns,
                    tool_calls=executed,
                    usage=usage,
                    outcomes=outcomes,
                )

            transcript.append(turn.as_message())
            content = turn.content or content

            for call in turn.tool_calls:
                if executed >= self._max_tool_calls:
                    self._emit("stop", {"reason": "max_tool_calls"})
                    return LoopResult(
                        content=content,
                        stopped_reason="max_tool_calls",
                        messages=transcript,
                        turns=turns,
                        tool_calls=executed,
                        usage=usage,
                        outcomes=outcomes,
                    )
                executed += 1
                outcome = self._execute(call, manifest)
                outcomes.append(outcome)
                transcript.append(outcome.as_message())
                self._emit(
                    "tool",
                    {
                        "gear": outcome.gear,
                        "ok": outcome.ok,
                        "status": outcome.status,
                        "latency_ms": outcome.latency_ms,
                    },
                )

        self._emit("stop", {"reason": "max_steps"})
        return LoopResult(
            content=content,
            stopped_reason="max_steps",
            messages=transcript,
            turns=turns,
            tool_calls=executed,
            usage=usage,
            outcomes=outcomes,
        )

    # ------------------------------------------------------------------ #

    def _execute(self, call: Mapping[str, Any], manifest: dict[str, Any]) -> ToolCallOutcome:
        """Parse and execute one call; every failure becomes a tool message."""
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        try:
            gear = gear_for_tool(name)
        except BridgeProtocolError as exc:  # unknown tool — the model may correct itself
            return ToolCallOutcome(
                gear="",
                tool_call_id=call_id,
                status="error",
                tool_name=name,
                error_code="unknown_tool",
                error_detail=str(exc),
            )
        if self._allowed is not None and gear not in self._allowed:
            return ToolCallOutcome(
                gear=gear,
                tool_call_id=call_id,
                status="error",
                tool_name=name,
                error_code="tool_not_allowed",
                error_detail=f"gear {gear!r} is not in this task's allowed set",
            )
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except (TypeError, ValueError) as exc:
            return ToolCallOutcome(
                gear=gear,
                tool_call_id=call_id,
                status="error",
                tool_name=name,
                error_code="invalid_arguments_json",
                error_detail=f"arguments are not valid JSON: {exc}",
            )
        if not isinstance(arguments, dict):
            return ToolCallOutcome(
                gear=gear,
                tool_call_id=call_id,
                status="error",
                tool_name=name,
                error_code="invalid_arguments",
                error_detail=f"arguments must be a JSON object, got {type(arguments).__name__}",
            )
        return self._runner.call(
            name, arguments, tool_call_id=call_id, manifest=manifest
        )

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # noqa: BLE001 - observers must never break the loop
            pass


def _add(total: dict[str, int], part: Mapping[str, Any] | None) -> None:
    """Accumulate token counters defensively (bad values are ignored)."""
    if not isinstance(part, Mapping):
        return
    for key in USAGE_KEYS:
        value = part.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        total[key] = total.get(key, 0) + value
