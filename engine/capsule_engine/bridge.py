#!/usr/bin/env python3
"""bridge.py — the single **tool seam**: any AI tool behind one governed contract.

The kernel already has one seam for models (:mod:`capsule_engine.gateway`).
This module adds the seam for *everything else an AI tool might do* — drafting,
reviewing, distilling an image prompt, extracting fields, classifying risk,
summarising an audit trail — so a gear can be swapped (n8n today, Dify
tomorrow, a 40-line local service next week) without a single line of the
governance kernel changing.

The design rule the whole file follows:

    **closed verbs, open tools.**

- *Closed verbs*: :data:`GEAR_VOCABULARY` is the only set of ``resource:action``
  gears the engine may ask for. An unknown gear is refused — a tool can never
  widen its own job description (mirror of ``capabilities.ALL_CAPABILITIES``).
- *Open tools*: whoever answers is named freely in ``engine.tool``
  (``n8n``, ``dify``, ``langflow``, ``langgraph``, ``vllm``, ``reference-gear``).
  Nothing in the contract enumerates implementations, because enumerating them
  is exactly what would make them hard to replace.

What the contract guarantees (every rule fails closed):

- **strict JSON both ways** — a closed request envelope in, a closed response
  envelope out, no prose, no markdown fences, unknown keys refused;
- **correlation** — the response must echo ``gear`` and ``request_id``;
- **integrity** — the request carries ``input_sha256``; an optional
  ``output_sha256`` is re-computed and compared, so the audit chain can bind
  the answer's exact bytes;
- **refusal is first class** — ``status="refused"`` with a machine-readable
  ``code`` is a lawful answer that callers must act on
  (:meth:`BridgeResult.require_ok` raises instead of letting it pass as an
  empty success);
- **provenance** — every ``ok`` answer names the tool that produced it;
- **no credentials in flight** — the token is sent as an ``Authorization``
  header, never inside the envelope, and is never logged.

Cheap built-ins shipped here:

- :class:`HttpBridge` — the production client (stdlib ``urllib``, bearer token
  from the environment, hard timeout, retries with exponential backoff + jitter
  on 429/5xx/timeout, bounded response read);
- :class:`OfflineBridge` — a deterministic stand-in implementing every gear, so
  the test suite and dev runs never need a network;
- :func:`default_bridge` — the fail-closed factory;
- :func:`fold_usage` — folds the answer's token counters into
  ``metadata.usage`` (same numbers the gateway reports);
- :func:`resolve_endpoint` / :func:`env_var_for_gear` — per-gear endpoint and
  token resolution, so *one* gear can be swapped while the rest keep running.

The normative, language-agnostic form of this contract lives in
``shared_kernel/schemas/tool_bridge.request.schema.json`` and
``shared_kernel/schemas/tool_bridge.response.schema.json``; a framework-free
reference gear (the other side of the wire) lives in
``tools/bridge_reference_gear.py``. Standard library only; Python 3.10+.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from capsule_engine.exceptions import CapsuleError, ManifestSchemaError
from capsule_engine.manifest import MANIFEST_NAME, USAGE_KEYS, State, accumulate_usage, now_iso

logger = logging.getLogger("capsule_engine.bridge")

__all__ = [
    "ALLOWED_RESPONSE_KEYS",
    "BRIDGE_ENDPOINT_ENV",
    "BRIDGE_TOKEN_ENVS",
    "BRIDGE_VERSION",
    "DEFAULT_BASE_BACKOFF",
    "DEFAULT_DEADLINE_MS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "GEAR_CONTRACTS",
    "GEAR_VOCABULARY",
    "INPUT_BYTE_BUDGET",
    "INPUT_BYTE_RESERVE",
    "MEDIA_GEARS",
    "MAX_MEDIA_BYTES",
    "MAX_MEDIA_REQUEST_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "BridgeConfigError",
    "BridgeError",
    "BridgeHTTPError",
    "BridgeProtocolError",
    "BridgeRefusalError",
    "BridgeResult",
    "BridgeTimeoutError",
    "BridgeTransportError",
    "GearContract",
    "HttpBridge",
    "OfflineBridge",
    "build_request",
    "bridge_note",
    "canonical_bytes",
    "default_bridge",
    "env_var_for_gear",
    "fold_usage",
    "gear_contract",
    "parse_response",
    "payload_sha256",
    "record_exchange",
    "request_size_limit",
    "resolve_endpoint",
    "resolve_token",
    "validate_output",
]

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

BRIDGE_VERSION = 1

# Per-gear endpoint override wins over the shared default, so a single gear can
# be moved to another tool without touching its neighbours:
#   AGENCY_BRIDGE_URL_TEXT_REVIEW=https://dify.internal/v1/run/xyz
BRIDGE_ENDPOINT_ENV = "AGENCY_BRIDGE_URL"
# Bearer token for a gear. Explicitly not part of the envelope: the value never
# appears in a request body, a capsule, or a log line.
BRIDGE_TOKEN_ENVS = ("AGENCY_BRIDGE_TOKEN", "AGENCY_LLM_API_KEY")
OFFLINE_MODE_ENV = "AGENCY_OFFLINE_MODE"
#: Dedicated override for the gear-routing section (tests / multi-config hosts).
#: Falls back to the canonical ``AGENCY_ENGINE_CONFIG`` then the shipped
#: ``config/engine.json`` — the same file the spend gate reads.
BRIDGE_CONFIG_ENV = "AGENCY_BRIDGE_CONFIG"
#: The section of the engine config holding per-verb endpoints/tokens.
GEARS_CONFIG_KEY = "gears"
#: Gear-routing config is re-read at most this often, re-keyed on mtime+size,
#: so an operator edit (or a test override) lands immediately without a walk.
_GEAR_CONFIG_TTL = 1.0

DEFAULT_TIMEOUT = 30.0  # seconds — hard deadline per HTTP request
DEFAULT_DEADLINE_MS = 30_000  # the same deadline, declared to the gear
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 2.0  # seconds
MAX_REQUEST_BYTES = 512 * 1024  # bounded envelope: a request is JSON, not a file
# The envelope is measured in BYTES of canonical UTF-8, while every per-field
# limit in this file is measured in CHARACTERS — 500 log lines of 4000 chars is
# 2,000,000 characters and up to 8,000,000 bytes, so the two never agreed on
# what "allowed" means. Reserve a fixed margin for everything in the envelope
# that is not `input` (ids, timestamp, both digests, capsule and governance —
# each already individually capped and small), so the input budget below is
# provably safe: canonical_bytes(input) <= INPUT_BYTE_BUDGET => the envelope
# always fits, whatever the input is made of.
INPUT_BYTE_RESERVE = 4096
INPUT_BYTE_BUDGET = MAX_REQUEST_BYTES - INPUT_BYTE_RESERVE
MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # bounded read, mirror of the gateway
# Media gears are the only ones allowed to carry bytes (an approved image
# travelling to a channel, a scanned page handed to a vision/OCR tool), so they
# get a larger — still bounded — envelope. Every other gear stays at the small
# cap above: a text verb has no business shipping a file.
MEDIA_GEARS: frozenset[str] = frozenset({"social:publish", "image:read"})
MAX_MEDIA_BYTES = 4 * 1024 * 1024  # decoded media per request
MAX_MEDIA_REQUEST_BYTES = 6 * 1024 * 1024  # a media gear's envelope cap
MAX_ERROR_BODY_BYTES = 4096  # error bodies are diagnostics, not payloads
MAX_TEXT_CHARS = 60_000
MAX_LIST_ITEMS = 500
# The route log kept on a capsule: enough to see the path a deliverable took,
# bounded so a long-lived capsule never bloats its manifest.
MAX_GEAR_EXCHANGES = 25

_AUTONOMY_LEVELS = frozenset({"L0", "L1", "L2", "L3"})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REFUSAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
_INTENT_RE = re.compile(r"^[a-z][a-z0-9_:]{1,48}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME_RE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,64}$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
MAX_FILES_PER_PROPOSAL = 200
MAX_MEDIA_URLS = 10

# --------------------------------------------------------------------------- #
# errors (every one catchable as CapsuleError at the pulse boundary)
# --------------------------------------------------------------------------- #

class BridgeError(CapsuleError):
    """Base class for every deliberate bridge failure."""


class BridgeConfigError(BridgeError):
    """No endpoint/token/gear is configured (fail closed, never a silent no-op)."""


class BridgeProtocolError(BridgeError):
    """The request or the answer violates the contract — nothing is trusted."""


class BridgeTransportError(BridgeError):
    """The gear endpoint could not be reached (DNS, connection, TLS, proxy)."""


class BridgeTimeoutError(BridgeError):
    """The gear exceeded its hard deadline."""


class BridgeHTTPError(BridgeError):
    """The gear answered with a non-2xx status."""

    def __init__(self, status: int, detail: str, *, endpoint: str = "") -> None:
        self.status = status
        self.detail = detail
        self.endpoint = endpoint
        suffix = f" from {endpoint}" if endpoint else ""
        super().__init__(f"HTTP {status}{suffix}: {detail[:200]}")


class BridgeRefusalError(BridgeError):
    """The gear lawfully declined (``status="refused"``) — treat it as a refusal.

    Raised by :meth:`BridgeResult.require_ok`, so a caller can never mistake a
    refusal for success: the refusal code and detail travel with the error.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"gear refused (code={code}): {detail}" if detail else f"gear refused (code={code})")


# --------------------------------------------------------------------------- #
# the closed gear vocabulary
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GearContract:
    """One gear: the verb the engine may ask for, and the exact shapes allowed.

    ``input_allowed``/``output_allowed`` are closed sets: an unknown key is a
    protocol violation rather than a field that is quietly ignored. Extra
    semantics (a label must come from the offered list, an extracted field must
    have been requested) live in :func:`validate_output` next to the contract.
    """

    name: str
    purpose: str
    input_required: tuple[str, ...]
    input_allowed: tuple[str, ...]
    output_required: tuple[str, ...]
    output_allowed: tuple[str, ...]


GEAR_CONTRACTS: dict[str, GearContract] = {
    "text:draft": GearContract(
        name="text:draft",
        purpose="author a deliverable text from a brief",
        input_required=("brief",),
        input_allowed=("brief", "topic", "platform", "language"),
        output_required=("text",),
        output_allowed=("text",),
    ),
    "text:review": GearContract(
        name="text:review",
        purpose="strict quality verdict on a text",
        input_required=("text",),
        input_allowed=("text",),
        output_required=("passed", "feedback"),
        output_allowed=("passed", "feedback"),
    ),
    "image:distill": GearContract(
        name="image:distill",
        purpose="distil a text into a single image-generation prompt",
        input_required=("text",),
        input_allowed=("text", "context"),
        output_required=("prompt",),
        output_allowed=("prompt",),
    ),
    "image:read": GearContract(
        name="image:read",
        purpose="read a page: OCR/vision extraction of its text and structure",
        input_required=("media",),
        input_allowed=("media", "language"),
        output_required=("text", "confidence"),
        output_allowed=("text", "confidence", "blocks"),
    ),
    "data:extract": GearContract(
        name="data:extract",
        purpose="pull named fields out of a document into structured JSON",
        input_required=("text", "fields"),
        input_allowed=("text", "fields"),
        output_required=("fields",),
        output_allowed=("fields",),
    ),
    "data:classify": GearContract(
        name="data:classify",
        purpose="classify a text into one of the offered labels",
        input_required=("text", "labels"),
        input_allowed=("text", "labels"),
        output_required=("label", "confidence"),
        output_allowed=("label", "confidence"),
    ),
    "log:summarize": GearContract(
        name="log:summarize",
        purpose="summarise an audit/log slice and surface findings",
        input_required=("entries",),
        input_allowed=("entries",),
        output_required=("summary", "findings"),
        output_allowed=("summary", "findings"),
    ),
    "social:publish": GearContract(
        name="social:publish",
        purpose="deliver an approved post to a social channel",
        input_required=("channel", "text"),
        input_allowed=("channel", "text", "media_urls", "media", "scheduled_for"),
        output_required=("receipt_id", "channel"),
        output_allowed=("receipt_id", "channel", "published_at"),
    ),
    "message:send": GearContract(
        name="message:send",
        purpose="send one outbound message (WhatsApp/Telegram/SMS) to a contact",
        input_required=("channel", "to", "text"),
        input_allowed=("channel", "to", "text"),
        output_required=("message_id", "channel"),
        output_allowed=("message_id", "channel"),
    ),
    "message:interpret": GearContract(
        name="message:interpret",
        purpose="read one inbound message and propose the reply",
        input_required=("channel", "text"),
        input_allowed=("channel", "text"),
        output_required=("intent", "reply_text", "escalate"),
        output_allowed=("intent", "reply_text", "escalate"),
    ),
    "lead:qualify": GearContract(
        name="lead:qualify",
        purpose="score a prospect against declared criteria and explain why",
        input_required=("text", "criteria"),
        input_allowed=("text", "criteria"),
        output_required=("score", "qualified", "reasons"),
        output_allowed=("score", "qualified", "reasons"),
    ),
    "code:propose": GearContract(
        name="code:propose",
        purpose="propose a change set as hashed artifacts for the human gate",
        input_required=("request",),
        input_allowed=("request", "constraints"),
        output_required=("summary", "files"),
        output_allowed=("summary", "files"),
    ),
    "finance:invoice": GearContract(
        name="finance:invoice",
        purpose="draft an invoice from declared line items (never an invisible total)",
        input_required=("customer", "line_items", "currency"),
        input_allowed=("customer", "line_items", "currency", "due_date"),
        output_required=("invoice_id", "total", "currency"),
        output_allowed=("invoice_id", "total", "currency"),
    ),
    "people:screen": GearContract(
        name="people:screen",
        purpose="screen an application against declared requirements and name the gaps",
        input_required=("text", "requirements"),
        input_allowed=("text", "requirements"),
        output_required=("score", "gaps", "summary"),
        output_allowed=("score", "gaps", "summary"),
    ),
    "support:triage": GearContract(
        name="support:triage",
        purpose="triage a support request: severity, category, escalation",
        input_required=("text",),
        input_allowed=("text",),
        output_required=("severity", "category", "escalate"),
        output_allowed=("severity", "category", "escalate", "reply_text"),
    ),
    "docs:review": GearContract(
        name="docs:review",
        purpose="review a document against a checklist and flag what it says",
        input_required=("text", "checklist"),
        input_allowed=("text", "checklist"),
        output_required=("summary", "flags"),
        output_allowed=("summary", "flags"),
    ),
}

# Severity vocabularies stay closed: "how bad is it" must be comparable across
# gears, so a tool cannot invent a level nobody can rank.
SEVERITIES = frozenset({"info", "warning", "critical"})
TRIAGE_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
MAX_FLAGS = 50
MAX_LINE_ITEMS = 50

GEAR_VOCABULARY: frozenset[str] = frozenset(GEAR_CONTRACTS)

REQUEST_KEYS: tuple[str, ...] = (
    "bridge_version",
    "gear",
    "request_id",
    "issued_at",
    "deadline_ms",
    "input_sha256",
    "capsule",
    "governance",
    "input",
)
REQUEST_REQUIRED: tuple[str, ...] = REQUEST_KEYS  # every key of the envelope is mandatory

ALLOWED_RESPONSE_KEYS: tuple[str, ...] = (
    "bridge_version",
    "gear",
    "request_id",
    "status",
    "output",
    "usage",
    "engine",
    "refusal",
    "output_sha256",
)
RESPONSE_REQUIRED: tuple[str, ...] = ("bridge_version", "gear", "request_id", "status", "output")

STATUSES: frozenset[str] = frozenset({"ok", "refused", "error"})

USAGE_NOTE = (
    "unknown keys inside `usage` are ignored (provider noise); a malformed known "
    "counter is a protocol violation"
)


# --------------------------------------------------------------------------- #
# canonical serialization + digests
# --------------------------------------------------------------------------- #

def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes of a mapping: sorted keys, compact, UTF-8, no NaN.

    Deterministic by construction, so ``sha256(canonical_bytes(x))`` is a stable
    integrity proof for the same logical value. ``NaN``/``Infinity`` are refused
    here (they are not JSON) rather than serialized as bare literals.
    """
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BridgeProtocolError(f"payload is not canonical-JSON serializable: {exc}") from exc
    return text.encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of :func:`canonical_bytes` — the integrity proof."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def request_size_limit(gear: str) -> int:
    """The envelope cap for ``gear``: larger only for the media gears.

    Keeps the bounded-read guarantee meaningful: widening the cap for every
    verb would let any gear ask the engine to serialize megabytes of text.
    """
    return MAX_MEDIA_REQUEST_BYTES if gear in MEDIA_GEARS else MAX_REQUEST_BYTES


def _require_mapping(value: object, *, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BridgeProtocolError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _require_text(value: object, *, what: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeProtocolError(f"{what} must be a non-empty string")
    if len(value) > max_chars:
        raise BridgeProtocolError(f"{what} exceeds {max_chars} characters")
    return value


def _closed_keys(
    payload: Mapping[str, Any],
    *,
    allowed: Sequence[str],
    required: Sequence[str],
    what: str,
) -> None:
    """Enforce a closed, mandatory key set (the whole no-downgrade rule)."""
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise BridgeProtocolError(
            f"{what} carries unknown keys {unknown} — closing over the schema is what "
            "keeps a swapped tool inside its contract"
        )
    missing = sorted(set(required) - set(payload))
    if missing:
        raise BridgeProtocolError(f"{what} is missing required keys {missing}")


def _token_count(value: object, *, what: str) -> int:
    """Coerce a provider counter: non-negative int, or a protocol violation."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BridgeProtocolError(f"{what} must be a non-negative integer, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# per-gear semantic validation
# --------------------------------------------------------------------------- #

def _check_text_list(
    value: object, *, what: str, minimum: int, maximum: int, item_max: int = 64
) -> list[str]:
    if not isinstance(value, list) or isinstance(value, (str, bytes)):
        raise BridgeProtocolError(f"{what} must be a list of strings")
    if not (minimum <= len(value) <= maximum):
        raise BridgeProtocolError(f"{what} must hold {minimum}..{maximum} items (got {len(value)})")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BridgeProtocolError(f"{what} must hold non-empty strings")
        if len(item) > item_max:
            raise BridgeProtocolError(f"{what} items are capped at {item_max} characters")
        out.append(item.strip())
    if len(set(out)) != len(out):
        raise BridgeProtocolError(f"{what} must not repeat an item")
    return out


def _validate_input(contract: GearContract, input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a gear's input against its closed contract; return a clean copy."""
    _closed_keys(
        input_data,
        allowed=contract.input_allowed,
        required=contract.input_required,
        what=f"input for gear {contract.name!r}",
    )
    clean: dict[str, Any] = dict(input_data)

    if "text" in contract.input_allowed:
        _require_text(clean.get("text"), what="input.text")
    if contract.name == "text:draft":
        _require_text(clean.get("brief"), what="input.brief")
        for optional in ("topic", "platform", "language"):
            if optional in clean:
                _require_text(clean[optional], what=f"input.{optional}", max_chars=300)
    if contract.name == "image:distill" and "context" in clean:
        _require_text(clean["context"], what="input.context", max_chars=1000)
    if contract.name == "data:extract":
        fields = _check_text_list(clean.get("fields"), what="input.fields", minimum=1, maximum=24)
        clean["fields"] = fields
    if contract.name == "data:classify":
        labels = _check_text_list(clean.get("labels"), what="input.labels", minimum=2, maximum=32)
        clean["labels"] = labels
    if contract.name == "log:summarize":
        entries = clean.get("entries")
        if not isinstance(entries, list) or isinstance(entries, (str, bytes)):
            raise BridgeProtocolError("input.entries must be a list of log lines")
        if not (1 <= len(entries) <= MAX_LIST_ITEMS):
            raise BridgeProtocolError(f"input.entries must hold 1..{MAX_LIST_ITEMS} lines")
        for line in entries:
            _require_text(line, what="input.entries[]", max_chars=4000)
    if contract.name == "image:read":
        # The kernel hands over the page it already holds (the capsule artifact):
        # no URL fetching, no second copy of the source of truth.
        clean["media"] = _validate_media(clean.get("media"))
        if "language" in clean:
            _require_text(clean["language"], what="input.language", max_chars=32)
    if contract.name in ("social:publish", "message:send", "message:interpret"):
        _require_text(clean.get("channel"), what="input.channel", max_chars=64)
    if contract.name == "message:send":
        _require_text(clean.get("to"), what="input.to", max_chars=64)
    if contract.name == "social:publish":
        if "media" in clean and "media_urls" in clean:
            raise BridgeProtocolError(
                "send either media (bytes) or media_urls (hosted links), not both — "
                "one delivery form per request"
            )
        if "media" in clean:
            clean["media"] = _validate_media(clean["media"])
        if "media_urls" in clean:
            media = clean["media_urls"]
            if not isinstance(media, list) or len(media) > MAX_MEDIA_URLS:
                raise BridgeProtocolError(f"input.media_urls must be a list of at most {MAX_MEDIA_URLS} URLs")
            for url in media:
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise BridgeProtocolError("input.media_urls[] must be http(s) URLs")
        if "scheduled_for" in clean:
            _require_text(clean["scheduled_for"], what="input.scheduled_for", max_chars=40)
    if contract.name == "lead:qualify":
        clean["criteria"] = _check_text_list(
            clean.get("criteria"), what="input.criteria", minimum=1, maximum=16
        )
    if contract.name == "code:propose":
        _require_text(clean.get("request"), what="input.request")
        if "constraints" in clean:
            clean["constraints"] = _check_text_list(
                clean["constraints"], what="input.constraints", minimum=1, maximum=16, item_max=300
            )
    if contract.name in ("people:screen", "docs:review"):
        key = "requirements" if contract.name == "people:screen" else "checklist"
        clean[key] = _check_text_list(clean.get(key), what=f"input.{key}", minimum=1, maximum=24)
    if contract.name == "finance:invoice":
        _require_text(clean.get("customer"), what="input.customer", max_chars=200)
        currency = clean.get("currency")
        if not isinstance(currency, str) or not re.match(r"^[A-Z]{3}$", currency):
            raise BridgeProtocolError("input.currency must be a 3-letter uppercase code (e.g. EGP, USD)")
        items = clean.get("line_items")
        if not isinstance(items, list) or not (1 <= len(items) <= MAX_LINE_ITEMS):
            raise BridgeProtocolError(f"input.line_items must hold 1..{MAX_LINE_ITEMS} items")
        normalised_items: list[dict[str, Any]] = []
        for item in items:
            entry = _require_mapping(item, what="input.line_items[]")
            _closed_keys(entry, allowed=("description", "amount"), required=("description", "amount"), what="input.line_items[]")
            amount = entry.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
                raise BridgeProtocolError("input.line_items[].amount must be a non-negative number")
            normalised_items.append(
                {
                    "description": _require_text(entry.get("description"), what="input.line_items[].description", max_chars=300),
                    "amount": float(amount),
                }
            )
        clean["line_items"] = normalised_items
        if "due_date" in clean:
            _require_text(clean["due_date"], what="input.due_date", max_chars=40)
    # The budget that actually binds, checked last but reported first.
    # Refusing here names the gear and the number to fix; `build_request` would
    # otherwise reject the same request later with only "envelope too large",
    # leaving a caller to guess which of its field limits was misleading.
    total_bytes = len(canonical_bytes(clean))
    if total_bytes > INPUT_BYTE_BUDGET:
        raise BridgeProtocolError(
            f"input for gear {contract.name!r} is {total_bytes} bytes of canonical "
            f"UTF-8 but the budget is {INPUT_BYTE_BUDGET} bytes "
            f"(MAX_REQUEST_BYTES {MAX_REQUEST_BYTES} less {INPUT_BYTE_RESERVE} reserved "
            f"for ids, digests, capsule and governance) — send fewer or shorter items; "
            "per-field character limits do not bound the total"
        )
    return clean


def _validate_media(media: object) -> dict[str, Any]:
    """Validate in-flight media: shape, size, and its **own** digest.

    The kernel accepts media only when the declared SHA-256 matches the decoded
    bytes. That makes the digest a fact rather than a claim: a drifted or
    swapped image is refused here, before it leaves the host, and a gear that
    re-checks the digest is verifying something already true.

    Raises:
        BridgeProtocolError: unknown/missing keys, a bad mime type, a malformed
            digest, undecodable base64, an oversized payload, or a digest that
            does not describe the bytes.
    """
    payload = _require_mapping(media, what="input.media")
    _closed_keys(
        payload,
        allowed=("mime", "base64", "sha256"),
        required=("mime", "base64", "sha256"),
        what="input.media",
    )
    mime = payload.get("mime")
    if not isinstance(mime, str) or not _MIME_RE.match(mime):
        raise BridgeProtocolError("input.media.mime must look like 'image/jpeg'")
    digest = payload.get("sha256")
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        raise BridgeProtocolError("input.media.sha256 must be a 64-character lowercase hex digest")
    encoded = payload.get("base64")
    # base64 is ~4/3 of the bytes; anything longer than that ceiling cannot
    # decode to a media payload inside the cap below.
    ceiling = (MAX_MEDIA_BYTES // 3 + 1) * 4
    if not isinstance(encoded, str) or not encoded.strip() or len(encoded) > ceiling:
        raise BridgeProtocolError(f"input.media.base64 is missing or exceeds {ceiling} characters")
    compact = "".join(encoded.split())
    if not _B64_RE.match(compact):
        raise BridgeProtocolError("input.media.base64 is not valid base64")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BridgeProtocolError(f"input.media.base64 cannot be decoded: {exc}") from exc
    if len(raw) > MAX_MEDIA_BYTES:
        raise BridgeProtocolError(f"input.media decodes to {len(raw)} bytes (> {MAX_MEDIA_BYTES}) — refused")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, digest):
        raise BridgeProtocolError(
            f"input.media.sha256 {digest[:12]}… does not match the decoded bytes "
            f"({actual[:12]}…) — refusing drifted media"
        )
    return {"mime": mime, "base64": compact, "sha256": digest}


def _safe_rel_path(value: object, *, what: str) -> str:
    """Accept a capsule-relative artifact path; refuse anything that escapes.

    The same rule the manifest layer enforces when it hashes an artifact: a
    proposal may name a file inside the deliverable, never an absolute path and
    never a walk upwards.
    """
    path = _require_text(value, what=what, max_chars=300)
    if path.startswith(("/", "~")):
        raise BridgeProtocolError(f"{what} must be relative to the deliverable: {path!r}")
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise BridgeProtocolError(f"{what} escapes the deliverable: {path!r}")
    return "/".join(parts)


def validate_output(gear: str, output: Mapping[str, Any], input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a gear's answer against its contract; return a clean copy.

    Beyond shape, the semantics that make an answer *usable* are enforced here:

    - ``text:review`` — ``passed`` is a real boolean and the feedback is non-empty
      (one actionable sentence, not a shrug);
    - ``data:classify`` — the label MUST be one of the offered labels and the
      confidence MUST be a 0..1 number: a gear cannot invent a category or a
      fabricated certainty;
    - ``data:extract`` — the returned field names must be exactly the requested
      ones: no invented field, no silently missing one, only scalar values;
    - ``log:summarize`` — severities come from a closed set.

    Raises:
        BridgeProtocolError: any breach; nothing is trusted, nothing is repaired.
    """
    contract = gear_contract(gear)
    payload = _require_mapping(output, what=f"output for gear {gear!r}")
    _closed_keys(
        payload, allowed=contract.output_allowed, required=contract.output_required, what=f"output for gear {gear!r}"
    )
    clean: dict[str, Any] = dict(payload)

    if gear == "text:review":
        passed = clean.get("passed")
        if not isinstance(passed, bool):
            raise BridgeProtocolError("output.passed must be a boolean (a verdict, not a string)")
        clean["feedback"] = _require_text(clean.get("feedback"), what="output.feedback", max_chars=4000)
    elif gear == "data:classify":
        offered = [str(item) for item in input_data.get("labels", [])]
        label = clean.get("label")
        if not isinstance(label, str) or label not in offered:
            raise BridgeProtocolError(
                f"output.label {label!r} is not one of the offered labels {offered} — a gear "
                "may not invent a category"
            )
        confidence = clean.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise BridgeProtocolError("output.confidence must be a number")
        if not 0.0 <= float(confidence) <= 1.0:
            raise BridgeProtocolError("output.confidence must be within 0..1")
        clean["confidence"] = float(confidence)
    elif gear == "data:extract":
        requested = [str(item) for item in input_data.get("fields", [])]
        fields_payload = _require_mapping(clean.get("fields"), what="output.fields")
        unknown = sorted(set(fields_payload) - set(requested))
        if unknown:
            raise BridgeProtocolError(
                f"output.fields invented {unknown} — only the requested fields may be returned"
            )
        missing = sorted(set(requested) - set(fields_payload))
        if missing:
            raise BridgeProtocolError(f"output.fields is missing requested fields {missing}")
        for name, value in fields_payload.items():
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise BridgeProtocolError(f"output.fields[{name!r}] must be a scalar or null")
    elif gear == "log:summarize":
        clean["summary"] = _require_text(clean.get("summary"), what="output.summary", max_chars=8000)
        findings = clean.get("findings")
        if not isinstance(findings, list) or len(findings) > 50:
            raise BridgeProtocolError("output.findings must be a list of at most 50 items")
        normalised: list[dict[str, Any]] = []
        for item in findings:
            entry = _require_mapping(item, what="output.findings[]")
            _closed_keys(entry, allowed=("severity", "detail"), required=("severity", "detail"), what="output.findings[]")
            severity = entry.get("severity")
            if severity not in ("info", "warning", "critical"):
                raise BridgeProtocolError(f"output.findings[].severity {severity!r} is outside the closed set")
            normalised.append(
                {
                    "severity": severity,
                    "detail": _require_text(entry.get("detail"), what="output.findings[].detail", max_chars=2000),
                }
            )
        clean["findings"] = normalised
    elif gear in ("social:publish", "message:send"):
        requested_channel = str(input_data.get("channel") or "")
        if clean.get("channel") != requested_channel:
            raise BridgeProtocolError(
                f"output.channel {clean.get('channel')!r} does not echo the requested channel "
                f"{requested_channel!r} — a gear may not redirect a delivery"
            )
        key = "receipt_id" if gear == "social:publish" else "message_id"
        clean[key] = _require_text(clean.get(key), what=f"output.{key}", max_chars=200)
        if "published_at" in clean:
            clean["published_at"] = _require_text(clean["published_at"], what="output.published_at", max_chars=40)
    elif gear == "message:interpret":
        intent = clean.get("intent")
        if not isinstance(intent, str) or not _INTENT_RE.match(intent):
            raise BridgeProtocolError(
                "output.intent must be a machine-readable token matching [a-z][a-z0-9_:]{1,48}"
            )
        clean["reply_text"] = _require_text(clean.get("reply_text"), what="output.reply_text")
        escalate = clean.get("escalate")
        if not isinstance(escalate, bool):
            raise BridgeProtocolError("output.escalate must be a boolean (who decides is not a suggestion)")
    elif gear == "lead:qualify":
        score = clean.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= float(score) <= 1.0:
            raise BridgeProtocolError("output.score must be a number within 0..1")
        clean["score"] = float(score)
        if not isinstance(clean.get("qualified"), bool):
            raise BridgeProtocolError("output.qualified must be a boolean")
        reasons = _check_text_list(
            clean.get("reasons"), what="output.reasons", minimum=0, maximum=16, item_max=500
        )
        clean["reasons"] = reasons
    elif gear == "image:read":
        clean["text"] = _require_text(clean.get("text"), what="output.text")
        confidence = clean.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            raise BridgeProtocolError("output.confidence must be a number within 0..1")
        clean["confidence"] = float(confidence)
        if "blocks" in clean:
            blocks = clean["blocks"]
            if not isinstance(blocks, list) or len(blocks) > 100:
                raise BridgeProtocolError("output.blocks must be a list of at most 100 items")
            normalised_blocks: list[dict[str, Any]] = []
            for item in blocks:
                entry = _require_mapping(item, what="output.blocks[]")
                _closed_keys(entry, allowed=("kind", "text"), required=("kind", "text"), what="output.blocks[]")
                kind = entry.get("kind")
                if kind not in ("header", "paragraph", "table", "figure"):
                    raise BridgeProtocolError(f"output.blocks[].kind {kind!r} is outside the closed set")
                normalised_blocks.append(
                    {"kind": kind, "text": _require_text(entry.get("text"), what="output.blocks[].text", max_chars=4000)}
                )
            clean["blocks"] = normalised_blocks
    elif gear == "code:propose":
        clean["summary"] = _require_text(clean.get("summary"), what="output.summary", max_chars=8000)
        files = clean.get("files")
        if not isinstance(files, list) or len(files) > MAX_FILES_PER_PROPOSAL:
            raise BridgeProtocolError(f"output.files must be a list of at most {MAX_FILES_PER_PROPOSAL} files")
        normalised_files: list[dict[str, Any]] = []
        for entry in files:
            item = _require_mapping(entry, what="output.files[]")
            _closed_keys(
                item, allowed=("path", "sha256", "bytes"), required=("path", "sha256", "bytes"), what="output.files[]"
            )
            digest = item.get("sha256")
            if not isinstance(digest, str) or not _HEX64_RE.match(digest):
                raise BridgeProtocolError("output.files[].sha256 must be a 64-character lowercase hex digest")
            normalised_files.append(
                {
                    "path": _safe_rel_path(item.get("path"), what="output.files[].path"),
                    "sha256": digest,
                    "bytes": _token_count(item.get("bytes"), what="output.files[].bytes"),
                }
            )
        clean["files"] = normalised_files
    elif gear == "finance:invoice":
        clean["invoice_id"] = _require_text(clean.get("invoice_id"), what="output.invoice_id", max_chars=200)
        currency = clean.get("currency")
        if currency != input_data.get("currency"):
            raise BridgeProtocolError(
                f"output.currency {currency!r} does not echo the requested {input_data.get('currency')!r}"
            )
        total = clean.get("total")
        if isinstance(total, bool) or not isinstance(total, (int, float)) or not 0 <= float(total) <= 1e9:
            raise BridgeProtocolError("output.total must be a number within 0..1e9")
        clean["total"] = float(total)
    elif gear == "people:screen":
        score = clean.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= float(score) <= 1.0:
            raise BridgeProtocolError("output.score must be a number within 0..1")
        clean["score"] = float(score)
        clean["gaps"] = _check_text_list(clean.get("gaps"), what="output.gaps", minimum=0, maximum=16, item_max=300)
        clean["summary"] = _require_text(clean.get("summary"), what="output.summary", max_chars=8000)
    elif gear == "support:triage":
        severity = clean.get("severity")
        if severity not in TRIAGE_SEVERITIES:
            raise BridgeProtocolError(f"output.severity {severity!r} is outside {sorted(TRIAGE_SEVERITIES)}")
        category = clean.get("category")
        if not isinstance(category, str) or not _INTENT_RE.match(category):
            raise BridgeProtocolError("output.category must be a machine-readable token matching [a-z][a-z0-9_:]{1,48}")
        if not isinstance(clean.get("escalate"), bool):
            raise BridgeProtocolError("output.escalate must be a boolean")
        if "reply_text" in clean:
            clean["reply_text"] = _require_text(clean["reply_text"], what="output.reply_text")
    elif gear == "docs:review":
        clean["summary"] = _require_text(clean.get("summary"), what="output.summary", max_chars=8000)
        flags = clean.get("flags")
        if not isinstance(flags, list) or len(flags) > MAX_FLAGS:
            raise BridgeProtocolError(f"output.flags must be a list of at most {MAX_FLAGS} items")
        normalised: list[dict[str, Any]] = []
        for item in flags:
            entry = _require_mapping(item, what="output.flags[]")
            _closed_keys(entry, allowed=("severity", "note"), required=("severity", "note"), what="output.flags[]")
            severity = entry.get("severity")
            if severity not in SEVERITIES:
                raise BridgeProtocolError(f"output.flags[].severity {severity!r} is outside {sorted(SEVERITIES)}")
            normalised.append(
                {"severity": severity, "note": _require_text(entry.get("note"), what="output.flags[].note", max_chars=2000)}
            )
        clean["flags"] = normalised
    else:  # text:draft, image:distill
        for key, cap in (("text", MAX_TEXT_CHARS), ("prompt", 1000)):
            if key in clean:
                clean[key] = _require_text(clean[key], what=f"output.{key}", max_chars=cap)
    return clean


# --------------------------------------------------------------------------- #
# request envelope
# --------------------------------------------------------------------------- #

def gear_contract(gear: object) -> GearContract:
    """Return the contract for ``gear``; refuse anything outside the vocabulary."""
    if not isinstance(gear, str) or gear not in GEAR_CONTRACTS:
        known = ", ".join(sorted(GEAR_VOCABULARY))
        raise BridgeProtocolError(
            f"unknown gear {gear!r} — refusing it rather than forwarding it "
            f"(known gears: {known})"
        )
    return GEAR_CONTRACTS[str(gear)]


def build_request(
    gear: str,
    input_data: Mapping[str, Any],
    *,
    capsule: Mapping[str, Any] | None = None,
    governance: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    issued_at: str | None = None,
    deadline_ms: int = DEFAULT_DEADLINE_MS,
) -> dict[str, Any]:
    """Build a contract-valid request envelope (or refuse to build one).

    Args:
        gear: a verb from :data:`GEAR_VOCABULARY`.
        input_data: gear-specific payload; its keys are closed per gear.
        capsule: ``{"id": ..., "state": ...}`` (+ optional ``attempt``/``domain``).
            ``state`` must be a real capsule state — the envelope describes where
            the work sits, and a made-up state is refused.
        governance: ``{"domain": ..., "autonomy_level": "L0".."L3"}``
            (+ optional ``decided_by`` provenance note).
        request_id: correlation id; defaults to a random 32-hex id.
        issued_at: ISO-8601 UTC timestamp; defaults to now.
        deadline_ms: hard deadline declared to the gear (100..600000).

    Raises:
        BridgeProtocolError: unknown gear, unknown/missing input keys, wrong
            types, an unknown capsule state, an unknown autonomy level, or an
            envelope that exceeds :data:`MAX_REQUEST_BYTES`.
    """
    contract = gear_contract(gear)
    clean_input = _validate_input(contract, _require_mapping(input_data, what="input"))

    if capsule is None:
        raise BridgeProtocolError(
            "capsule is required: {'id': ..., 'state': ...} — the envelope must say which "
            "capsule the work belongs to"
        )
    if governance is None:
        raise BridgeProtocolError(
            "governance is required: {'domain': ..., 'autonomy_level': 'L0'..'L3'} — an "
            "answer with no declared autonomy context cannot be governed"
        )
    capsule_payload = _require_mapping(capsule, what="capsule")
    _closed_keys(
        capsule_payload,
        allowed=("id", "state", "attempt", "domain"),
        required=("id", "state"),
        what="capsule",
    )
    capsule_id = _require_text(capsule_payload.get("id"), what="capsule.id", max_chars=128)
    try:
        capsule_state = State.parse(capsule_payload.get("state")).value
    except (ValueError, TypeError) as exc:
        raise BridgeProtocolError(f"capsule.state: {exc}") from exc
    clean_capsule: dict[str, Any] = {"id": capsule_id, "state": capsule_state}
    if "attempt" in capsule_payload:
        clean_capsule["attempt"] = _token_count(capsule_payload["attempt"], what="capsule.attempt")
    if "domain" in capsule_payload:
        clean_capsule["domain"] = _require_text(capsule_payload["domain"], what="capsule.domain", max_chars=64)

    governance_payload = _require_mapping(governance, what="governance")
    _closed_keys(
        governance_payload,
        allowed=("domain", "autonomy_level", "decided_by"),
        required=("domain", "autonomy_level"),
        what="governance",
    )
    level = governance_payload.get("autonomy_level")
    if not isinstance(level, str) or level not in _AUTONOMY_LEVELS:
        raise BridgeProtocolError(
            f"governance.autonomy_level {level!r} is not one of {sorted(_AUTONOMY_LEVELS)}"
        )
    clean_governance: dict[str, Any] = {
        "domain": _require_text(governance_payload.get("domain"), what="governance.domain", max_chars=64),
        "autonomy_level": level,
    }
    if "decided_by" in governance_payload:
        clean_governance["decided_by"] = _require_text(
            governance_payload["decided_by"], what="governance.decided_by", max_chars=200
        )

    if request_id is None:
        identifier = secrets.token_hex(16)
    else:
        identifier = request_id if isinstance(request_id, str) and _ID_RE.match(request_id.strip()) else ""
        if not identifier:
            raise BridgeProtocolError(
                "request_id must match [A-Za-z0-9_-]{8,64} (correlation is not optional)"
            )
        identifier = request_id.strip()

    if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int) or not 100 <= deadline_ms <= 600_000:
        raise BridgeProtocolError("deadline_ms must be an integer within 100..600000")

    envelope: dict[str, Any] = {
        "bridge_version": BRIDGE_VERSION,
        "gear": contract.name,
        "request_id": identifier,
        "issued_at": issued_at or now_iso(),
        "deadline_ms": deadline_ms,
        "input_sha256": payload_sha256(clean_input),
        "capsule": clean_capsule,
        "governance": clean_governance,
        "input": clean_input,
    }
    limit = request_size_limit(contract.name)
    size = len(canonical_bytes(envelope))
    if size > limit:
        raise BridgeProtocolError(
            f"request envelope for gear {contract.name!r} is {size} bytes (> {limit}) — refused"
        )
    return envelope


# --------------------------------------------------------------------------- #
# response envelope
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BridgeResult:
    """One validated, contract-compliant answer from a gear.

    Immutable and audit-friendly: :attr:`output_sha256` is always populated (the
    gear's own digest is verified when supplied, otherwise it is computed here),
    so a history entry can bind the exact bytes the gear returned.
    """

    gear: str
    request_id: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)
    refusal: dict[str, Any] | None = None
    output_sha256: str = ""

    @property
    def ok(self) -> bool:
        """True only for a contract-valid ``status="ok"`` answer."""
        return self.status == "ok"

    @property
    def tool(self) -> str:
        """Which implementation answered (``""`` when the answer was not ok)."""
        tool = self.engine.get("tool")
        return tool if isinstance(tool, str) else ""

    def require_ok(self) -> "BridgeResult":
        """Return self when the gear answered ``ok``; otherwise raise.

        A refusal becomes :class:`BridgeRefusalError` (code + detail preserved):
        a gear declining is lawful, and the caller must act on it rather than
        proceed with an empty result.
        """
        if self.ok:
            return self
        if self.status == "refused":
            code = str(self.refusal.get("code")) if isinstance(self.refusal, dict) else "refused"
            detail = str(self.refusal.get("detail", "")) if isinstance(self.refusal, dict) else ""
            raise BridgeRefusalError(code, detail)
        raise BridgeProtocolError(f"gear {self.gear!r} answered status={self.status!r} (no output to use)")


def _decode(payload: object) -> Mapping[str, Any]:
    """Decode a response payload from bytes/str/dict with a bounded read."""
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BridgeProtocolError(f"response exceeds {MAX_RESPONSE_BYTES} bytes — refused")
        try:
            decoded = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeProtocolError(f"response is not valid UTF-8 JSON: {exc}") from exc
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise BridgeProtocolError(f"response exceeds {MAX_RESPONSE_BYTES} bytes — refused")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError(f"response is not valid JSON: {exc}") from exc
    else:
        raise BridgeProtocolError(f"response must be bytes, str or dict, got {type(payload).__name__}")
    return _require_mapping(decoded, what="response")


def parse_response(
    payload: object,
    *,
    gear: str,
    request_id: str,
    input_data: Mapping[str, Any] | None = None,
) -> BridgeResult:
    """Validate an answer against the contract; return a :class:`BridgeResult`.

    Enforced here: closed top-level keys, the echoed ``gear``/``request_id``, a
    known ``status``, an empty ``output`` unless ``ok``, the gear's own output
    contract, ``engine.tool`` provenance on success, a machine-readable
    ``refusal.code`` on refusal, and an ``output_sha256`` that matches when the
    gear supplies one.

    Raises:
        BridgeProtocolError: any breach — a non-conforming answer is never
            partially accepted.
    """
    contract = gear_contract(gear)
    body = _decode(payload)
    _closed_keys(
        body, allowed=ALLOWED_RESPONSE_KEYS, required=RESPONSE_REQUIRED, what="response envelope"
    )

    if body.get("bridge_version") != BRIDGE_VERSION:
        raise BridgeProtocolError(
            f"response declares bridge_version {body.get('bridge_version')!r}, expected {BRIDGE_VERSION}"
        )
    echoed_gear = body.get("gear")
    if echoed_gear != contract.name:
        raise BridgeProtocolError(
            f"response answers gear {echoed_gear!r} but {contract.name!r} was asked — refusing a crossed answer"
        )
    if body.get("request_id") != request_id:
        raise BridgeProtocolError("response request_id does not match the request — correlation broken")

    status = body.get("status")
    if status not in STATUSES:
        raise BridgeProtocolError(f"response status {status!r} is not one of {sorted(STATUSES)}")

    raw_output = _require_mapping(body.get("output"), what="output")
    if status != "ok" and raw_output:
        raise BridgeProtocolError("output must be empty unless status is 'ok' (no partial answers)")

    usage: dict[str, int] = {key: 0 for key in USAGE_KEYS}
    if "usage" in body:
        raw_usage = _require_mapping(body["usage"], what="usage")
        for key in USAGE_KEYS:
            if key in raw_usage:
                usage[key] = _token_count(raw_usage[key], what=f"usage.{key}")
        if usage["total_tokens"] == 0:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    engine: dict[str, Any] = {}
    if "engine" in body:
        raw_engine = _require_mapping(body["engine"], what="engine")
        _closed_keys(
            raw_engine,
            allowed=("tool", "model", "version", "latency_ms"),
            required=("tool",),
            what="engine",
        )
        engine["tool"] = _require_text(raw_engine.get("tool"), what="engine.tool", max_chars=64)
        for optional in ("model", "version"):
            if optional in raw_engine:
                engine[optional] = _require_text(raw_engine[optional], what=f"engine.{optional}", max_chars=200)
        if "latency_ms" in raw_engine:
            engine["latency_ms"] = _token_count(raw_engine["latency_ms"], what="engine.latency_ms")

    refusal: dict[str, Any] | None = None
    if "refusal" in body:
        raw_refusal = _require_mapping(body["refusal"], what="refusal")
        _closed_keys(raw_refusal, allowed=("code", "detail"), required=("code",), what="refusal")
        code = raw_refusal.get("code")
        if not isinstance(code, str) or not _REFUSAL_CODE_RE.match(code):
            raise BridgeProtocolError(
                "refusal.code must be a machine-readable token matching [a-z][a-z0-9_]{2,48}"
            )
        refusal = {"code": code}
        if "detail" in raw_refusal:
            refusal["detail"] = _require_text(raw_refusal["detail"], what="refusal.detail", max_chars=2000)

    if status == "ok" and not engine:
        raise BridgeProtocolError("an 'ok' answer must name its engine (provenance is how a swap is audited)")
    if status == "refused" and refusal is None:
        raise BridgeProtocolError("a refusal must carry refusal.code — a bare 'no' is not auditable")

    output: dict[str, Any] = {}
    if status == "ok":
        # Semantics that depend on what was asked (offered labels, requested
        # fields) can only be proven with the original input in hand, so those
        # two gears insist on it instead of accepting an unverifiable answer.
        semantic_input = {} if input_data is None else _require_mapping(input_data, what="input")
        if not semantic_input and contract.name in ("data:classify", "data:extract"):
            raise BridgeProtocolError(
                f"parsing gear {contract.name!r} requires the original input to verify the "
                "offered labels / requested fields"
            )
        output = validate_output(contract.name, raw_output, semantic_input)

    digest = payload_sha256(output) if status == "ok" else ""
    claimed = body.get("output_sha256")
    if claimed is not None:
        if not isinstance(claimed, str) or not _HEX64_RE.match(claimed):
            raise BridgeProtocolError("output_sha256 must be a 64-character lowercase hex digest")
        if status != "ok":
            raise BridgeProtocolError("output_sha256 is meaningless without an 'ok' output")
        if claimed != digest:
            raise BridgeProtocolError(
                f"output_sha256 mismatch (claimed {claimed[:12]}…, computed {digest[:12]}…) — "
                "refusing an answer whose bytes do not match its proof"
            )

    return BridgeResult(
        gear=contract.name,
        request_id=str(request_id),
        status=str(status),
        output=output,
        usage=usage,
        engine=engine,
        refusal=refusal,
        output_sha256=digest,
    )


def bridge_note(result: BridgeResult) -> str:
    """A short, log-safe history line for a gear exchange (never a credential)."""
    if result.ok:
        tool = result.tool or "unknown-tool"
        model = result.engine.get("model")
        label = f"{tool}/{model}" if isinstance(model, str) and model else tool
        return (
            f"gear {result.gear} answered ok via {label} "
            f"(request {result.request_id}, output sha256={result.output_sha256[:12]}…)"
        )
    if result.status == "refused":
        code = result.refusal.get("code") if result.refusal else "unknown"
        return f"gear {result.gear} refused (code={code}, request {result.request_id})"
    return f"gear {result.gear} answered status={result.status} (request {result.request_id})"


def fold_usage(manifest: MutableMapping[str, Any], result: BridgeResult) -> dict[str, int] | None:
    """Fold a gear's token counters into ``metadata.usage``.

    Returns the running totals, or ``None`` when the answer carried no usage at
    all — the caller decides whether that is worth recording.
    """
    if not result.usage:
        return None
    return accumulate_usage(manifest, result.usage)


def record_exchange(
    manifest: MutableMapping[str, Any],
    result: BridgeResult,
    *,
    stage: str,
    by: str = "bridge",
) -> list[dict[str, Any]]:
    """Append one gear exchange to ``metadata.gear_exchanges`` (the route log).

    This is what makes a swapped tool *visible*: the cockpit can show which
    implementation answered each stage of a capsule's path, which request paid
    for it, and which answers were refusals. The log is append-only and bounded
    to the newest :data:`MAX_GEAR_EXCHANGES` entries, so a long-lived capsule
    can never bloat its manifest with its own history.

    Raises:
        ManifestSchemaError: ``metadata`` exists but is not an object (a corrupt
            manifest is reported, never silently overwritten).
    """
    metadata = manifest.get("metadata")
    if metadata is None:
        metadata = {}
        manifest["metadata"] = metadata
    if not isinstance(metadata, dict):
        raise ManifestSchemaError(
            MANIFEST_NAME,
            f"field 'metadata' must be an object to record a gear exchange, got {type(metadata).__name__}",
        )
    log = metadata.get("gear_exchanges")
    entries = list(log) if isinstance(log, list) else []
    entries.append(
        {
            "at": now_iso(),
            "stage": str(stage)[:64],
            "gear": result.gear,
            "status": result.status,
            "request_id": result.request_id,
            "tool": result.tool or None,
            "output_sha256": result.output_sha256 or None,
            "refusal_code": (result.refusal or {}).get("code") if isinstance(result.refusal, dict) else None,
            "by": str(by)[:120],
        }
    )
    metadata["gear_exchanges"] = entries[-MAX_GEAR_EXCHANGES:]
    return metadata["gear_exchanges"]


def settle_exchange(
    manifest: MutableMapping[str, Any],
    result: BridgeResult,
    *,
    stage: str,
    by: str = "bridge",
) -> str:
    """Settle one gear exchange: fold usage, record the route log, return a note.

    The one call a worker makes after invoking a gear — token counters land in
    ``metadata.usage`` (when the tool reported any), the exchange lands in
    ``metadata.gear_exchanges``, and the returned string is ready for
    ``BaseWorker.record``.
    """
    fold_usage(manifest, result)
    record_exchange(manifest, result, stage=stage, by=by)
    return bridge_note(result)


# --------------------------------------------------------------------------- #
# per-gear routing (swapping one gear without touching its neighbours)
# --------------------------------------------------------------------------- #

def env_var_for_gear(gear: str, prefix: str = BRIDGE_ENDPOINT_ENV) -> str:
    """The per-gear environment variable name: ``text:review`` -> ``..._TEXT_REVIEW``."""
    return f"{prefix}_{gear.replace(':', '_').replace('-', '_').upper()}"


def _gear_config_path() -> Path:
    """Where the gear-routing section lives.

    ``AGENCY_BRIDGE_CONFIG`` wins (a dedicated override for tests and hosts
    that keep several configs), then the canonical ``AGENCY_ENGINE_CONFIG``,
    then the shipped ``shared_kernel.paths.ENGINE_CONFIG_PATH``.
    """
    for env in (BRIDGE_CONFIG_ENV, "AGENCY_ENGINE_CONFIG"):
        value = os.environ.get(env, "").strip()
        if value:
            return Path(value)
    try:
        from shared_kernel.paths import ENGINE_CONFIG_PATH

        return Path(ENGINE_CONFIG_PATH)
    except Exception:  # standalone execution (engine/ on sys.path only)
        return Path(__file__).resolve().parents[2] / "config" / "engine.json"


_GEAR_CONFIG_CACHE: dict[str, Any] = {"key": None, "loaded_at": 0.0, "gears": {}}


def _load_gear_config() -> dict[str, Any]:
    """Read the ``gears`` section of the engine config (cached, fail-soft).

    A missing/unreadable/malformed section is *not* an error here — the bridge
    then falls back to the environment, exactly as it did before this section
    existed. The bytes are never trusted: only a JSON object is accepted, and
    only string ``endpoint``/``token``/``token_env`` values are ever used.
    """
    path = _gear_config_path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        _GEAR_CONFIG_CACHE.update({"key": None, "loaded_at": 0.0, "gears": {}})
        return {}
    now = time.monotonic()
    cached = _GEAR_CONFIG_CACHE.get("gears")
    if (
        _GEAR_CONFIG_CACHE.get("key") == key
        and isinstance(cached, dict)
        and now - float(_GEAR_CONFIG_CACHE.get("loaded_at") or 0.0) <= _GEAR_CONFIG_TTL
    ):
        return cached
    gears: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        section = raw.get(GEARS_CONFIG_KEY) if isinstance(raw, Mapping) else None
        if isinstance(section, Mapping):
            gears = {str(name): entry for name, entry in section.items()}
    except (OSError, ValueError):
        gears = {}
    _GEAR_CONFIG_CACHE.update({"key": key, "loaded_at": now, "gears": gears})
    return gears


def _gear_config_entry(gear: str) -> Mapping[str, Any]:
    """The config entry for ``gear``: its own key, else the shared ``default``."""
    gears = _load_gear_config()
    entry = gears.get(gear)
    if isinstance(entry, Mapping):
        return entry
    default = gears.get("default")
    return default if isinstance(default, Mapping) else {}


def resolve_endpoint(gear: str, explicit: str | None = None) -> str | None:
    """Resolve a gear's endpoint: explicit > config > per-gear env > shared env.

    ``config/engine.json -> gears`` is authoritative when it names an endpoint
    (per-verb key first, then ``default``); the environment is the fallback so a
    deployment can route a gear without editing the shipped config.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    configured = _gear_config_entry(gear).get("endpoint")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    for name in (env_var_for_gear(gear), BRIDGE_ENDPOINT_ENV):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_token(gear: str, explicit: str | None = None) -> str | None:
    """Resolve a gear's bearer token: explicit > config > named env > standard env.

    The config may carry a literal ``token`` (a *private*, uncommitted file) or,
    preferably, ``token_env`` — the **name** of an environment variable holding
    the secret — so credentials never have to be written into
    ``config/engine.json`` (FOUNDATION.md, ثابت 7: Zero-Secret Leakage).
    """
    if explicit is not None:
        return explicit.strip() or None
    entry = _gear_config_entry(gear)
    literal = entry.get("token")
    if isinstance(literal, str) and literal.strip():
        return literal.strip()
    names: list[str] = []
    token_env = entry.get("token_env")
    if isinstance(token_env, str) and token_env.strip():
        names.append(token_env.strip())
    names.append(env_var_for_gear(gear, "AGENCY_BRIDGE_TOKEN"))
    names.extend(BRIDGE_TOKEN_ENVS)
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


#: Backwards-compatible private alias (older call sites use ``_resolve_token``).
_resolve_token = resolve_token


# --------------------------------------------------------------------------- #
# the real client
# --------------------------------------------------------------------------- #

class HttpBridge:
    """Production gear client: one HTTP POST per invocation, JSON both ways.

    Args:
        endpoint: gear URL (``http://`` or ``https://``); defaults to the
            per-gear/shared environment variables when omitted.
        gear: the gear this instance serves. Omit to serve any gear and pass
            ``gear=`` per :meth:`invoke`.
        token: bearer token; defaults to the environment. A missing token is a
            configuration error, never an anonymous call (fail closed).
        timeout / max_retries / base_backoff: per-request deadline and the retry
            policy for 429, 5xx and timeouts (same shape as the model gateway).

    Swapping a gear is: point its environment variable at another tool. No
    kernel change, no code change — the envelope is the only interface.

    Every HTTP attempt goes through the spend gate first
    (:mod:`capsule_engine.spend_control`, category ``tool``, service = the gear
    verb): the KILL SWITCH blocks delivery before the socket opens, and each
    attempt lands in ``data/finance/usage_ledger.jsonl``.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        gear: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        deadline_ms: int = DEFAULT_DEADLINE_MS,
        capsule: Mapping[str, Any] | None = None,
        governance: Mapping[str, Any] | None = None,
    ) -> None:
        self._gear = gear_contract(gear).name if gear is not None else None
        resolved = resolve_endpoint(self._gear or "", endpoint)
        if resolved is None:
            raise BridgeConfigError(
                f"no endpoint: pass endpoint= or set {BRIDGE_ENDPOINT_ENV} "
                f"(or {env_var_for_gear(gear)} for a single gear)"
            )
        if not (resolved.startswith("http://") or resolved.startswith("https://")):
            raise BridgeConfigError(f"endpoint must be an http(s) URL: {resolved!r}")
        self._endpoint = resolved
        self._token = _resolve_token(self._gear or "", token)
        if self._token is None:
            raise BridgeConfigError(
                "no bearer token: pass token= or set " + " / ".join(BRIDGE_TOKEN_ENVS)
                + " (fail closed: the bridge never calls a gear anonymously)"
            )
        if timeout <= 0:
            raise BridgeConfigError(f"timeout must be > 0, got {timeout}")
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._base_backoff = max(0.0, float(base_backoff))
        self._deadline_ms = deadline_ms
        self._capsule = dict(capsule) if capsule is not None else None
        self._governance = dict(governance) if governance is not None else None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def gear(self) -> str | None:
        return self._gear

    def invoke(
        self,
        input_data: Mapping[str, Any],
        *,
        gear: str | None = None,
        capsule: Mapping[str, Any] | None = None,
        governance: Mapping[str, Any] | None = None,
        deadline_ms: int | None = None,
    ) -> BridgeResult:
        """Send one gear request and return its validated answer.

        Raises:
            BridgeConfigError: no gear could be determined for this call.
            BridgeProtocolError: the request or the answer violates the contract.
            BridgeTimeoutError / BridgeHTTPError / BridgeTransportError: the
                endpoint did not answer within the contract.
        """
        effective_gear = gear_contract(gear).name if gear is not None else self._gear
        if effective_gear is None:
            raise BridgeConfigError("no gear: construct HttpBridge(gear=...) or pass gear= per call")
        request = build_request(
            effective_gear,
            input_data,
            capsule=capsule or self._capsule,
            governance=governance or self._governance,
            deadline_ms=self._deadline_ms if deadline_ms is None else deadline_ms,
        )
        body = canonical_bytes(request)
        governance_context = governance or self._governance
        spend_domain = (
            governance_context.get("domain")
            if isinstance(governance_context, Mapping)
            else None
        )
        raw = self._post(
            self._endpoint,
            body,
            request["request_id"],
            gear=effective_gear,
            domain=spend_domain if isinstance(spend_domain, str) else None,
        )
        return parse_response(raw, gear=effective_gear, request_id=request["request_id"], input_data=request["input"])

    # ------------------------------------------------------------------ #
    # transport internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calculate_backoff(attempt: int, base_backoff: float) -> float:
        """Exponential backoff with jitter (disabled when base_backoff is 0)."""
        if base_backoff <= 0.0:
            return 0.0
        return base_backoff * (2 ** attempt) + random.uniform(0.01, 0.5)

    def _sleep(self, seconds: float) -> None:
        """Hook for ``time.sleep``, overridable in tests."""
        time.sleep(seconds)

    def _post(
        self,
        endpoint: str,
        body: bytes,
        request_id: str,
        *,
        gear: str | None = None,
        domain: str | None = None,
    ) -> bytes:
        """POST the envelope; retry transient failures; never log the token.

        The request id travels as a header too, so an operator can correlate the
        exchange in the gear's own logs.

        The money gate: one ``spend_control.authorize()`` per HTTP attempt
        (retries included) BEFORE the socket opens, settled with ``record()``
        after — category ``tool``, service name = the gear verb, so a
        ``social:publish`` delivery is metered like every other paid path and
        the KILL SWITCH refuses it with zero bytes on the wire. A refusal
        (``SpendCapExceeded``) propagates to the caller untouched; a failed or
        timed-out attempt still settles at its estimate.
        """
        # Lazy import, exactly like gateway._call_llm: spend_control imports
        # the gateway for its error base, so the dependency stays one-way at
        # import time.
        from capsule_engine import spend_control

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "sovereign-agency-os/1.0 (+capsule-engine bridge)",
            "X-Bridge-Request-Id": request_id,
        }
        try:
            provider = urllib.parse.urlsplit(endpoint).netloc or endpoint
        except Exception:  # defensive: never block a call on provider labelling
            provider = endpoint
        last_detail = ""
        for attempt in range(self._max_retries + 1):
            category = (
                spend_control.CATEGORY_PUBLISH
                if gear == "social:publish"
                else spend_control.CATEGORY_TOOL
            )
            reservation = spend_control.authorize(
                model=gear or "bridge",
                category=category,
                provider=provider,
                domain=domain,
                context={
                    "op": "bridge._post",
                    "gear": gear or "",
                    "request_id": request_id,
                    "attempt": attempt,
                },
            )
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                    raise BridgeProtocolError(f"gear response exceeded {MAX_RESPONSE_BYTES} bytes — refused")
                spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)
                return raw
            except (socket.timeout, TimeoutError) as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                if attempt < self._max_retries:
                    delay = self._calculate_backoff(attempt, self._base_backoff)
                    logger.warning(
                        "bridge: %s timeout on attempt %d/%d — retrying in %.2fs",
                        endpoint, attempt + 1, self._max_retries + 1, delay,
                    )
                    self._sleep(delay)
                    continue
                raise BridgeTimeoutError(f"{endpoint} timed out after {self._timeout}s") from exc
            except urllib.error.HTTPError as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                detail = ""
                try:
                    detail = exc.read(MAX_ERROR_BODY_BYTES).decode("utf-8", "replace").strip()
                except Exception:  # noqa: BLE001 - diagnostics only, never fatal
                    detail = str(getattr(exc, "reason", ""))
                last_detail = detail or last_detail
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self._max_retries:
                    delay = self._calculate_backoff(attempt, self._base_backoff)
                    logger.warning(
                        "bridge: %s HTTP %d on attempt %d/%d (%s) — retrying in %.2fs",
                        endpoint, exc.code, attempt + 1, self._max_retries + 1, detail[:100], delay,
                    )
                    self._sleep(delay)
                    continue
                raise BridgeHTTPError(exc.code, last_detail, endpoint=endpoint) from exc
            except urllib.error.URLError as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    if attempt < self._max_retries:
                        delay = self._calculate_backoff(attempt, self._base_backoff)
                        logger.warning(
                            "bridge: %s transport timeout on attempt %d/%d — retrying in %.2fs",
                            endpoint, attempt + 1, self._max_retries + 1, delay,
                        )
                        self._sleep(delay)
                        continue
                    raise BridgeTimeoutError(f"{endpoint} timed out: {exc.reason}") from exc
                raise BridgeTransportError(f"{endpoint} transport failure: {exc.reason}") from exc
        raise BridgeTransportError(f"{endpoint} exhausted its retries")  # pragma: no cover


# --------------------------------------------------------------------------- #
# the deterministic offline gear (dev runs + the whole test suite)
# --------------------------------------------------------------------------- #

class OfflineBridge:
    """Deterministic, network-free implementation of every gear.

    Same seam, same envelopes, same validation — the answers are simply
    computed from the input with no model and no network, so tests and dev runs
    are reproducible. Honesty rules: a placeholder value is never presented as
    a finding, and offline classification reports confidence ``0.0`` rather than
    inventing a number it cannot support.
    """

    def __init__(
        self, gear: str | None = None, *, image_prompt_suffix: str = ""
    ) -> None:
        self._gear = gear_contract(gear).name if gear is not None else None
        # Injected by callers that want the offline ``image:distill`` gear to
        # append a branded aesthetic tail. The core carries no domain default:
        # the marketing package owns the aesthetic and passes it in.
        self._image_prompt_suffix = str(image_prompt_suffix or "")

    @property
    def gear(self) -> str | None:
        return self._gear

    def invoke(
        self,
        input_data: Mapping[str, Any],
        *,
        gear: str | None = None,
        capsule: Mapping[str, Any] | None = None,
        governance: Mapping[str, Any] | None = None,
        deadline_ms: int | None = None,
    ) -> BridgeResult:
        """Answer ``input_data`` deterministically through the real validator."""
        effective_gear = gear_contract(gear).name if gear is not None else self._gear
        if effective_gear is None:
            raise BridgeConfigError("no gear: construct OfflineBridge(gear=...) or pass gear= per call")
        request = build_request(
            effective_gear,
            input_data,
            capsule=capsule or {"id": "offline", "state": "draft"},
            governance=governance or {"domain": "offline", "autonomy_level": "L0"},
            deadline_ms=DEFAULT_DEADLINE_MS if deadline_ms is None else deadline_ms,
        )
        output = self._answer(effective_gear, request["input"])
        response: dict[str, Any] = {
            "bridge_version": BRIDGE_VERSION,
            "gear": effective_gear,
            "request_id": request["request_id"],
            "status": "ok",
            "output": output,
            "usage": {key: 0 for key in USAGE_KEYS},
            "engine": {"tool": "offline-bridge", "version": "1", "latency_ms": 0},
            "output_sha256": payload_sha256(output),
        }
        return parse_response(
            response, gear=effective_gear, request_id=request["request_id"], input_data=request["input"]
        )

    def _answer(self, gear: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
        """Deterministic, gear-appropriate placeholder output."""
        if gear == "text:draft":
            topic = str(input_data.get("topic") or input_data["brief"]).strip()
            return {
                "text": (
                    f"# {topic}\n\n"
                    "Draft produced by the offline gear bridge; point the gear's endpoint at a "
                    "real tool (n8n, Dify, Langflow, a local model) to author real copy.\n"
                )
            }
        if gear == "text:review":
            words = len(str(input_data["text"]).split())
            passed = words >= 10
            feedback = (
                f"PASSED: {words} words, above the offline minimum." if passed
                else f"REJECTED: {words} words, below the offline minimum of 10."
            )
            return {"passed": passed, "feedback": feedback}
        if gear == "image:distill":
            subject = " ".join(str(input_data["text"]).split()[:12]) or "executive command center"
            suffix = self._image_prompt_suffix.strip()
            return {"prompt": f"{subject}, {suffix}" if suffix else subject}
        if gear == "image:read":
            # A deterministic stand-in cannot see pixels: it reads what it is
            # given and reports honest, low confidence rather than inventing a
            # transcription it never made.
            raw = base64.b64decode(input_data["media"]["base64"])
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            return {
                "text": text.strip() or "(offline read: no text could be decoded without a vision tool)",
                "confidence": 0.0,
            }
        if gear == "data:extract":
            # Empty strings, never invented values: absence is honest.
            return {"fields": {str(name): "" for name in input_data["fields"]}}
        if gear == "data:classify":
            return {"label": str(input_data["labels"][0]), "confidence": 0.0}
        if gear == "log:summarize":
            return {
                "summary": f"{len(input_data['entries'])} log lines summarised by the offline bridge.",
                "findings": [],
            }
        if gear in ("social:publish", "message:send"):
            key = "receipt_id" if gear == "social:publish" else "message_id"
            fingerprint = payload_sha256(
                {"channel": input_data["channel"], "text": input_data["text"], "kind": gear}
            )[:12]
            return {key: f"offline-{fingerprint}", "channel": str(input_data["channel"])}
        if gear == "message:interpret":
            # The offline gear never pretends an unattended reply to a real
            # customer is safe: it escalates every inbound message to a human.
            return {
                "intent": "inbound_inquiry",
                "reply_text": "Thanks for reaching out — a human will confirm the details shortly.",
                "escalate": True,
            }
        if gear == "lead:qualify":
            return {"score": 0.0, "qualified": False, "reasons": []}
        if gear == "code:propose":
            return {
                "summary": f"Offline proposal for: {str(input_data['request']).strip()[:200]}",
                "files": [],
            }
        if gear == "finance:invoice":
            # Arithmetic, not guesswork: the total is the sum of the declared
            # line items, and the invoice id is derived from the same payload.
            total = round(sum(float(item["amount"]) for item in input_data["line_items"]), 2)
            fingerprint = payload_sha256(
                {"customer": input_data["customer"], "total": total, "currency": input_data["currency"]}
            )[:12]
            return {"invoice_id": f"offline-{fingerprint}", "total": total, "currency": str(input_data["currency"])}
        if gear == "people:screen":
            return {
                "score": 0.0,
                "gaps": list(input_data["requirements"]),
                "summary": "Offline screening: no requirement could be verified without a real tool.",
            }
        if gear == "support:triage":
            return {"severity": "low", "category": "unclassified", "escalate": True}
        return {
            "summary": "Offline review: nothing was read without a real tool.",
            "flags": [],
        }


def default_bridge(
    gear: str | None = None,
    *,
    offline: bool | None = None,
    endpoint: str | None = None,
    **overrides: Any,
) -> HttpBridge | OfflineBridge:
    """Strict factory: a real :class:`HttpBridge` when a gear is configured.

    Raises :class:`BridgeConfigError` when neither an endpoint/token nor an
    explicit offline mode is available — a missing tool is a refusal, never a
    silent no-op.
    """
    if offline is True:
        return OfflineBridge(gear)
    env_offline = os.environ.get(OFFLINE_MODE_ENV, "").strip().lower()
    if offline is None and env_offline in ("1", "true", "yes", "on", "offline"):
        return OfflineBridge(gear)
    has_endpoint = resolve_endpoint(gear or "", endpoint) is not None
    has_token = _resolve_token(gear or "", overrides.get("token")) is not None
    if has_endpoint and has_token:
        return HttpBridge(endpoint, gear=gear, **overrides)
    raise BridgeConfigError(
        "no gear configured and offline mode not enabled: pass endpoint=/token=, set "
        f"{BRIDGE_ENDPOINT_ENV} and {' / '.join(BRIDGE_TOKEN_ENVS)}, or pass offline=True "
        f"/ set {OFFLINE_MODE_ENV}=1"
    )


# Reserved for callers that want to type-annotate a gear table without
# depending on this module's concrete classes.
BridgeLike = Callable[..., BridgeResult]
