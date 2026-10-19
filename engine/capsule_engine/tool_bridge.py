#!/usr/bin/env python3
"""tool_bridge.py — the gear vocabulary, exposed as model-callable tools.

The kernel already has two seams: :mod:`capsule_engine.gateway` for models and
:mod:`capsule_engine.bridge` for tools. This module is the **join** between
them — it publishes the sixteen constitutional gears as JSON-schema tool
definitions a model can call, and executes a returned tool call through the
same governed bridge the workers use.

Design rules (each one fails closed):

- **one source of truth** — the tool list is *derived* from
  :data:`bridge.GEAR_CONTRACTS`. A gear that is not in the contract cannot be
  offered, and a required field cannot drift from the contract because it is
  read from it, not retyped here. Only the JSON *type* of each field name is
  declared (:data:`FIELD_SCHEMAS`); an allowed field with no declared type is
  an import-time error, so a new gear can never be published half-described.
- **irreversible actions are held back** — :data:`IRREVERSIBLE_GEARS` (publish,
  send) leave the host and cannot be undone. They are not offered unless the
  caller explicitly opts in, which is the model-facing mirror of the human gate.
- **a refusal is a tool result, not an exception** — when a gear refuses or the
  input violates its contract, the model receives a machine-readable error and
  may correct itself. Only the money gate
  (:class:`capsule_engine.gateway.SpendCapExceeded`) escapes: a budget refusal
  must stop the loop, never be re-planned around.
- **every call is recorded** — a successful or refused exchange is appended to
  ``metadata.gear_exchanges`` (bounded) and its token counters folded into
  ``metadata.usage``, exactly like a worker-initiated call.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from capsule_engine.bridge import (
    GEAR_CONTRACTS,
    BridgeError,
    BridgeProtocolError,
    BridgeResult,
    default_bridge,
    fold_usage,
    gear_contract,
    record_exchange,
)

__all__ = [
    "FIELD_SCHEMAS",
    "GEAR_TOOL_PREFIX",
    "IRREVERSIBLE_GEARS",
    "GearToolRunner",
    "ToolCallOutcome",
    "gear_for_tool",
    "gear_tools",
    "tool_name_for",
]

#: Prefix that keeps every published tool name inside the OpenAI-safe charset
#: (``^[a-zA-Z0-9_-]{1,64}$``): ``text:draft`` becomes ``gear_text__draft``.
GEAR_TOOL_PREFIX = "gear_"
_TOOL_SEPARATOR = "__"

#: Gears whose effect leaves the host and cannot be undone. Publishing and
#: messaging are the two; every other verb is read-only or produces a draft.
IRREVERSIBLE_GEARS: frozenset[str] = frozenset({"social:publish", "message:send"})


# --------------------------------------------------------------------------- #
# field schemas — the only thing not derived from the contracts
# --------------------------------------------------------------------------- #

_STRING: dict[str, Any] = {"type": "string"}
_TEXT_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    # prose
    "text": _STRING,
    "brief": _STRING,
    "topic": _STRING,
    "platform": _STRING,
    "language": _STRING,
    "context": _STRING,
    "request": _STRING,
    "customer": _STRING,
    "currency": _STRING,
    "due_date": _STRING,
    "scheduled_for": _STRING,
    # delivery targets
    "channel": _STRING,
    "to": _STRING,
    "media_urls": _TEXT_LIST,
    # closed lists the gear must pick from or work through
    "fields": _TEXT_LIST,
    "labels": {**_TEXT_LIST, "minItems": 2},
    "entries": _TEXT_LIST,
    "criteria": _TEXT_LIST,
    "constraints": _TEXT_LIST,
    "requirements": _TEXT_LIST,
    "checklist": _TEXT_LIST,
    # structured payloads
    "media": {
        "type": "object",
        "properties": {
            "mime": {"type": "string", "description": "e.g. image/jpeg"},
            "base64": {"type": "string", "description": "the bytes, base64-encoded"},
            "sha256": {"type": "string", "description": "SHA-256 of the decoded bytes"},
        },
        "required": ["mime", "base64", "sha256"],
        "additionalProperties": False,
    },
    "line_items": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "amount": {"type": "number", "minimum": 0},
            },
            "required": ["description", "amount"],
            "additionalProperties": False,
        },
        "minItems": 1,
    },
}

#: One line per field, so the model knows what to put in it. A field with no
#: hint still appears in the schema — the hint is help, never a gate.
FIELD_HINTS: dict[str, str] = {
    "text": "the text to work on",
    "brief": "what the deliverable should say, in one paragraph",
    "topic": "the subject",
    "platform": "where it will be published",
    "language": "output language tag, e.g. en or ar",
    "context": "extra context that should shape the image prompt",
    "request": "the change to propose",
    "customer": "who the invoice is for",
    "currency": "3-letter uppercase code, e.g. USD",
    "due_date": "payment due date, free text",
    "scheduled_for": "when to deliver, free text",
    "channel": "the destination channel, e.g. linkedin",
    "to": "the recipient handle",
    "media_urls": "hosted http(s) links to attach",
    "fields": "the exact field names to extract",
    "labels": "the only labels the answer may use (2 or more)",
    "entries": "log lines to summarise",
    "criteria": "the criteria to score against",
    "constraints": "limits the proposal must respect",
    "requirements": "the requirements to screen against",
    "checklist": "the checklist to review against",
    "media": "the page/image bytes with their SHA-256",
    "line_items": "the billed lines: description + amount",
}

# Every allowed field in every contract must be describable, or the vocabulary
# and this table have drifted apart. Checked once, at import, and loud.
_UNDECLARED = sorted(
    {
        name
        for contract in GEAR_CONTRACTS.values()
        for name in contract.input_allowed
        if name not in FIELD_SCHEMAS
    }
)
if _UNDECLARED:  # pragma: no cover - a guard, not a branch
    raise BridgeProtocolError(
        "tool_bridge: gear contract fields have no declared JSON schema: "
        + ", ".join(_UNDECLARED)
        + " — declare them in FIELD_SCHEMAS before publishing the gear as a tool"
    )


# --------------------------------------------------------------------------- #
# name mapping
# --------------------------------------------------------------------------- #

def tool_name_for(gear: str) -> str:
    """``text:draft`` -> ``gear_text__draft`` (OpenAI-safe, reversible)."""
    contract = gear_contract(gear)
    return GEAR_TOOL_PREFIX + contract.name.replace(":", _TOOL_SEPARATOR)


def gear_for_tool(name: str) -> str:
    """Inverse of :func:`tool_name_for`; unknown names are refused."""
    if not isinstance(name, str) or not name.startswith(GEAR_TOOL_PREFIX):
        raise BridgeProtocolError(f"unknown tool {name!r}: expected the {GEAR_TOOL_PREFIX!r} prefix")
    gear = name[len(GEAR_TOOL_PREFIX):].replace(_TOOL_SEPARATOR, ":")
    if gear not in GEAR_CONTRACTS:
        known = ", ".join(tool_name_for(g) for g in sorted(GEAR_CONTRACTS))
        raise BridgeProtocolError(
            f"unknown tool {name!r} (gear {gear!r}) — known tools: {known}"
        )
    return gear


# --------------------------------------------------------------------------- #
# schema publication
# --------------------------------------------------------------------------- #

def _parameters(gear: str) -> dict[str, Any]:
    """Build a closed JSON schema from the gear's own contract."""
    contract = gear_contract(gear)
    properties: dict[str, Any] = {}
    for name in contract.input_allowed:
        schema = dict(FIELD_SCHEMAS[name])
        hint = FIELD_HINTS.get(name)
        if hint:
            schema.setdefault("description", hint)
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": list(contract.input_required),
        "additionalProperties": False,
    }


def gear_tools(
    *,
    include_irreversible: bool = False,
    only: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Publish the gear vocabulary as OpenAI-style tool definitions.

    Args:
        include_irreversible: offer ``social:publish`` / ``message:send`` too.
            Off by default: those two leave the host and cannot be undone, so a
            model must be explicitly trusted with them.
        only: restrict to these gears (unknown names raise). ``None`` = all.

    Returns:
        A deterministic, sorted list of ``{"type": "function", ...}`` objects.
    """
    if only is not None:
        names = [gear_contract(g).name for g in only]
    else:
        names = sorted(GEAR_CONTRACTS)
    tools: list[dict[str, Any]] = []
    for gear in names:
        if gear in IRREVERSIBLE_GEARS and not include_irreversible:
            continue
        contract = GEAR_CONTRACTS[gear]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name_for(gear),
                    "description": f"{contract.purpose} (gear {gear})",
                    "parameters": _parameters(gear),
                },
            }
        )
    return tools


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolCallOutcome:
    """One executed (or refused) tool call, shaped for the model transcript."""

    gear: str
    tool_call_id: str
    status: str
    tool_name: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_detail: str = ""
    tool: str = ""
    request_id: str = ""
    output_sha256: str = ""
    latency_ms: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def _name(self) -> str:
        """The tool name to echo back — preserved even when it was unknown."""
        if self.tool_name:
            return self.tool_name
        return tool_name_for(self.gear) if self.gear else "unknown_tool"

    def content(self) -> str:
        """The JSON string a ``role="tool"`` message carries."""
        if self.ok:
            payload: dict[str, Any] = {
                "ok": True,
                "gear": self.gear,
                "tool": self.tool,
                "output": self.output,
            }
        else:
            payload = {
                "ok": False,
                "gear": self.gear,
                "error": {"code": self.error_code or self.status, "detail": self.error_detail},
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def as_message(self) -> dict[str, Any]:
        """An OpenAI-compatible ``role="tool"`` message."""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self._name(),
            "content": self.content(),
        }


class GearToolRunner:
    """Execute model tool calls through the governed bridge.

    The runner owns no loop and no policy: it executes exactly one call, meters
    it (the bridge already authorizes every paid attempt), records it on the
    capsule when a manifest is supplied, and returns a :class:`ToolCallOutcome`.
    """

    def __init__(
        self,
        *,
        bridge_factory: Callable[..., Any] = default_bridge,
        capsule: Mapping[str, Any] | None = None,
        governance: Mapping[str, Any] | None = None,
        deadline_ms: int | None = None,
        offline: bool | None = None,
    ) -> None:
        self._bridge_factory = bridge_factory
        self._capsule = dict(capsule) if capsule is not None else None
        self._governance = dict(governance) if governance is not None else None
        self._deadline_ms = deadline_ms
        self._offline = offline

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        tool_call_id: str = "",
        manifest: dict[str, Any] | None = None,
        stage: str = "tool_loop",
    ) -> ToolCallOutcome:
        """Execute one tool call. Never raises for a gear-level refusal."""
        started = time.perf_counter()
        gear = ""
        try:
            gear = gear_for_tool(name)
            if arguments is not None and not isinstance(arguments, Mapping):
                raise BridgeProtocolError(
                    f"tool {name!r} arguments must be an object, got {type(arguments).__name__}"
                )
            bridge = self._bridge_factory(gear, offline=self._offline)
            result: BridgeResult = bridge.invoke(
                dict(arguments or {}),
                gear=gear,
                capsule=self._capsule,
                governance=self._governance,
                deadline_ms=self._deadline_ms,
            )
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            outcome = self._outcome(result, tool_call_id, elapsed, tool_name=name)
        except BridgeError as exc:
            # A refusal is a lawful answer: hand it to the model as data.
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            outcome = ToolCallOutcome(
                gear=gear,
                tool_call_id=tool_call_id,
                status="error",
                tool_name=name,
                error_code=_error_code(exc),
                error_detail=str(exc),
                latency_ms=elapsed,
            )
        if manifest is not None and outcome.request_id:
            self._record(manifest, outcome, stage=stage)
        return outcome

    # ------------------------------------------------------------------ #

    @staticmethod
    def _outcome(
        result: BridgeResult, tool_call_id: str, elapsed: float, *, tool_name: str = ""
    ) -> ToolCallOutcome:
        if result.ok:
            return ToolCallOutcome(
                gear=result.gear,
                tool_call_id=tool_call_id,
                status="ok",
                tool_name=tool_name,
                output=dict(result.output),
                tool=result.tool,
                request_id=result.request_id,
                output_sha256=result.output_sha256,
                latency_ms=elapsed,
                usage=dict(result.usage),
            )
        refusal = result.refusal if isinstance(result.refusal, dict) else {}
        return ToolCallOutcome(
            gear=result.gear,
            tool_call_id=tool_call_id,
            status=result.status,
            tool_name=tool_name,
            error_code=str(refusal.get("code") or result.status),
            error_detail=str(refusal.get("detail") or ""),
            request_id=result.request_id,
            output_sha256=result.output_sha256,
            latency_ms=elapsed,
        )

    @staticmethod
    def _record(manifest: dict[str, Any], outcome: ToolCallOutcome, *, stage: str) -> None:
        """Best-effort audit: never let telemetry fail the call."""
        try:
            result = BridgeResult(
                gear=outcome.gear,
                request_id=outcome.request_id,
                status=outcome.status,
                output=dict(outcome.output),
                usage=dict(outcome.usage),
                engine={"tool": outcome.tool} if outcome.tool else {},
                refusal=(
                    {"code": outcome.error_code, "detail": outcome.error_detail}
                    if not outcome.ok
                    else None
                ),
                output_sha256=outcome.output_sha256,
            )
            record_exchange(manifest, result, stage=stage, by="tool_loop")
            fold_usage(manifest, result)
        except Exception:  # noqa: BLE001 - audit is best effort by contract
            pass


def _error_code(exc: BaseException) -> str:
    """A stable, log-safe code for a bridge failure."""
    name = type(exc).__name__
    return "".join(
        ("_" + ch.lower()) if ch.isupper() and index else ch.lower()
        for index, ch in enumerate(name)
    ) or "bridge_error"
