#!/usr/bin/env python3
"""envelope.py — the ``/v1`` response envelope and its page cursor.

Every ``/v1`` read answers the same three facts about itself, so the app never
has to guess how old a number is or where it came from:

``api_version``
    ``"v1"`` — the contract the body obeys.
``as_of``
    the UTC ISO instant this body was assembled. It is stamped once, here, so
    two blocks of one response can never disagree about "now".
``source``
    **where the data really came from.** ``"index"`` when the capsule / ticket /
    feedback views were served by the SQLite read index, ``"files"`` when they
    were re-derived from the vault files (index off, or the fallback path), and
    ``"principal"`` for ``GET /v1/me`` — identity comes from the verified
    credential, which is neither an index nor a file, and calling it either
    would be a lie in the one field whose entire job is to be true.

Lists are **cursor-paged**, never offset-paged: a concurrent insert would
otherwise shift every later page and silently duplicate or skip rows. The
cursor is an opaque base64url of the last row's sort key, and the sort key is
always ``(timestamp DESC, id DESC)`` with NULL timestamps last — one order, one
tiebreak, the same in SQL and in the file-scan twin. A cursor that cannot be
decoded is refused ``422 VALIDATION_ERROR`` rather than quietly ignored, because
a client that keeps sending a broken cursor must find out.

Standard library plus the ``/v1`` error envelope; Python 3.10+.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from orchestration.v1.errors import V1Error

__all__ = [
    "API_VERSION",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SOURCE_FILES",
    "SOURCE_INDEX",
    "SOURCE_PRINCIPAL",
    "decode_cursor",
    "encode_cursor",
    "envelope",
    "page",
    "parse_bool",
    "parse_cursor",
    "parse_limit",
    "utc_now_iso",
]

API_VERSION = "v1"

SOURCE_INDEX = "index"
SOURCE_FILES = "files"
SOURCE_PRINCIPAL = "principal"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def utc_now_iso() -> str:
    """The envelope timestamp: aware UTC, seconds precision, ``+00:00``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(*, source: str, **fields: Any) -> Dict[str, Any]:
    """Put ``api_version`` / ``as_of`` / ``source`` in front of ``fields``.

    Thin on purpose: the envelope is the first three keys of every body, so a
    caller cannot accidentally ship a response without them.
    """
    if source not in (SOURCE_INDEX, SOURCE_FILES, SOURCE_PRINCIPAL):
        raise ValueError(f"unknown envelope source {source!r}")
    return {"api_version": API_VERSION, "as_of": utc_now_iso(), "source": source, **fields}


def page(items: Sequence[Any], *, limit: int, next_cursor: Optional[str]) -> Dict[str, Any]:
    """The paged body: ``{items, page: {limit, next_cursor}}``."""
    return {
        "items": list(items),
        "page": {"limit": int(limit), "next_cursor": next_cursor},
    }


def encode_cursor(sort_key: Tuple[Any, Any]) -> str:
    """Opaque, URL-safe encoding of one row's ``(timestamp, id)`` sort key."""
    payload = json.dumps(
        {"at": sort_key[0], "id": sort_key[1]},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(raw: str) -> Tuple[Optional[str], str]:
    """Decode a cursor into ``(timestamp_or_None, id)``; refuse anything else.

    Every malformed shape — not base64url, not JSON, not an object, missing or
    mistyped members — is the same ``422 VALIDATION_ERROR``. The cursor is not
    signed because it carries no authority: it only names a position inside the
    result set the caller may already see, and the sector filter is applied in
    SQL on every request regardless of what the cursor says.
    """
    from orchestration.v1 import messages

    refusal = messages.text("error.cursor")
    if not isinstance(raw, str) or not raw.strip():
        raise V1Error(422, refusal, code="VALIDATION_ERROR")
    padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise V1Error(422, refusal, code="VALIDATION_ERROR") from exc
    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise V1Error(422, refusal, code="VALIDATION_ERROR") from exc
    if not isinstance(payload, dict):
        raise V1Error(422, refusal, code="VALIDATION_ERROR")
    at = payload.get("at")
    ident = payload.get("id")
    if at is not None and not isinstance(at, str):
        raise V1Error(422, refusal, code="VALIDATION_ERROR")
    if not isinstance(ident, str) or not ident:
        raise V1Error(422, refusal, code="VALIDATION_ERROR")
    return at, ident


def parse_cursor(raw: Optional[str]) -> Optional[Tuple[Optional[str], str]]:
    """``None`` when absent, else the decoded sort key (refusing a bad one)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return decode_cursor(raw)  # type: ignore[arg-type]


def parse_limit(raw: Optional[Any], *, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    """Validate ``limit``; a non-integer or out-of-range value is refused 422.

    Clamping silently would let a client believe it had seen everything, so an
    out-of-range value is a refusal, not a quiet adjustment.
    """
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise V1Error(
            422, "limit must be an integer", code="VALIDATION_ERROR"
        ) from exc
    if value < 1 or value > maximum:
        raise V1Error(
            422,
            f"limit must be within 1..{maximum} (got {value})",
            code="VALIDATION_ERROR",
        )
    return value


def parse_bool(raw: Optional[Any], *, name: str) -> Optional[bool]:
    """Parse a tri-state query flag: absent, ``true``/``false`` — or refuse."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        return raw
    token = str(raw).strip().lower()
    if token in ("true", "1", "yes", "on"):
        return True
    if token in ("false", "0", "no", "off"):
        return False
    raise V1Error(
        422,
        f"{name} must be true or false (got {raw!r})",
        code="VALIDATION_ERROR",
    )


def require_choice(raw: Optional[str], *, name: str, allowed: Iterable[str]) -> Optional[str]:
    """Validate an enum-ish query filter against its closed vocabulary."""
    if raw is None or not str(raw).strip():
        return None
    token = str(raw).strip()
    options = tuple(allowed)
    if token not in options:
        raise V1Error(
            422,
            f"{name} must be one of {', '.join(options)} (got {token!r})",
            code="VALIDATION_ERROR",
        )
    return token
