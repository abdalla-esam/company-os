#!/usr/bin/env python3
"""manifest.py — the file-as-database layer for a Deliverable Capsule.

A capsule is a directory; its ``manifest.json`` is the single source of truth
for the state machine that drives it. This module is the *only* place that
knows how the manifest is read, validated, mutated, and persisted:

- **atomic writes**: temp file + fsync + ``os.replace`` — a reader never sees
  a half-written manifest, even if a pulse dies mid-save;
- **fail-closed validation**: a manifest missing a required field, carrying an
  unknown state, or holding an unparseable timestamp raises
  :class:`ManifestSchemaError` before any business rule touches it;
- **closed transition matrix**: :func:`transition` refuses any edge that is not
  declared in :data:`TRANSITIONS` — states move only along lawful rails;
- **TOCTOU hash binding**: entering ``pending_approval`` records a SHA-256 of
  the artifact — ANY relative file, chosen by the task (default
  :data:`ARTIFACT_FILE`) — in ``metadata.artifact_sha256`` together with the
  recorded path in ``metadata.artifact_path``; leaving the gate toward
  ``approved``/``published`` re-hashes **the recorded file** and raises
  :class:`SecurityError` on any mismatch or missing binding (fail closed);
- **token accounting**: :func:`accumulate_usage` folds gateway usage dicts
  into ``metadata.usage`` with defensive defaults (missing keys = 0 tokens).

The engine is standard library only: ``json``, ``os``, ``pathlib``,
``datetime``, ``typing``, ``hashlib``, ``hmac``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, MutableMapping, Sequence

from capsule_engine.exceptions import (
    CapsuleNotFoundError,
    ManifestCorruptError,
    ManifestSchemaError,
    ManifestTooLargeError,
    SecurityError,
    StateTransitionError,
)
from shared_kernel.atomic_io import atomic_text

MANIFEST_NAME = "manifest.json"

# Maximum accepted size of a manifest (guards against runaway/corrupted files).
MAX_MANIFEST_BYTES = 1 * 1024 * 1024  # 1 MiB

# The finalized deliverable the approval gate is bound to.
#
# B0 — the primary artifact is ANY file: ``bind_artifact``/``verify_artifact``
# hash and re-verify whatever relative path is given, and the recorded
# ``metadata.artifact_path`` is the lawful authority for the approval edge
# (see tests/unit/test_artifact_generalization.py). This constant is the ONE
# default used only when a manifest records no path — legacy capsules keep
# working unchanged. It is deliberately NOT duplicated elsewhere: workers
# that produce a copy.md-stage artifact import this name.
ARTIFACT_FILE = "copy.md"
HASH_CHUNK_BYTES = 64 * 1024
# Refuse to bind/verify absurdly large artifacts (a manifest lives beside them).
MAX_HASH_BYTES = 32 * 1024 * 1024  # 32 MiB

# Token counters folded into ``metadata.usage`` (missing keys default to 0).
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class State(str, Enum):
    """Closed lifecycle of a Deliverable Capsule.

    ``str`` mixin keeps the enum JSON-serialisable and comparable to raw
    strings read off the disk (``manifest["state"] == State.DRAFT`` works for
    both a validated and a lazily-parsed value).
    """

    DRAFT = "draft"                    # capsule created, no work performed yet
    IN_PROGRESS = "in_progress"        # a worker holds the lock and is executing
    IN_REVIEW = "in_review"            # artifact produced, waiting for a gate
    PENDING_QA = "pending_qa"          # copy produced, queued for automated QA
    PENDING_DESIGN = "pending_design"  # copy passed QA, handed to the design stage
    PENDING_APPROVAL = "pending_approval"  # delivered to the human manager (HITL)
    STALLED = "stalled"                # auto-frozen pending approval (TTL exceeded)
    NEEDS_REVISION = "needs_revision"  # gate rejected; author must rework
    APPROVED = "approved"              # gate approved; awaiting publication
    PUBLISHED = "published"            # delivered to the world
    FAILED = "failed"                  # attempt cap reached — resurrect() only
    ARCHIVED = "archived"              # terminal: frozen for the record

    @classmethod
    def parse(cls, raw: str) -> "State":
        """Parse a raw on-disk string into a State, or raise ``ValueError``."""
        try:
            return cls(raw)
        except (ValueError, TypeError):
            known = ", ".join(member.value for member in cls)
            raise ValueError(f"unknown capsule state {raw!r} (known: {known})") from None


# The transition matrix: the only lawful edges of the capsule state machine.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.DRAFT: frozenset({State.IN_PROGRESS, State.FAILED}),
    State.IN_PROGRESS: frozenset({
        State.IN_REVIEW, State.PENDING_QA, State.PENDING_DESIGN,
        State.PENDING_APPROVAL, State.NEEDS_REVISION, State.FAILED, State.DRAFT,
        State.PUBLISHED,  # delivery attempt in flight -> webhook accepted
    }),
    State.IN_REVIEW: frozenset({
        State.APPROVED,
        State.PENDING_APPROVAL,  # all gates complete — seat at the gate
        State.NEEDS_REVISION,
    }),
    State.PENDING_QA: frozenset({
        State.NEEDS_REVISION,    # QA rejected — author must rework
        State.PENDING_DESIGN,    # QA passed and an image is required
        State.PENDING_APPROVAL,  # QA passed and no image is required
        State.FAILED,            # attempt cap reached — human reset
    }),
    State.PENDING_DESIGN: frozenset({
        State.IN_PROGRESS,        # design stage actively working
        State.PENDING_QA,         # design finished -> automated qa
        State.PENDING_APPROVAL,   # image delivered (or skipped) — human gate
        State.NEEDS_REVISION,     # design gate rejected the copy
        State.APPROVED,           # design signed off
        State.FAILED,             # attempt cap reached
    }),
    State.PENDING_APPROVAL: frozenset({
        State.APPROVED,           # manager signed off (hash verified — fail closed)
        State.NEEDS_REVISION,     # manager demanded revisions (demotes the domain)
        State.FAILED,             # attempt cap reached
        State.STALLED,            # TTL exceeded — auto-frozen for a human
    }),
    State.STALLED: frozenset({
        State.PENDING_APPROVAL,   # human unfreeze — back to the approval gate
        State.DRAFT,              # abandon the gate and rework from scratch
    }),
    State.NEEDS_REVISION: frozenset({State.IN_PROGRESS, State.FAILED}),
    State.APPROVED: frozenset({State.PUBLISHED, State.IN_PROGRESS}),
    State.PUBLISHED: frozenset({State.ARCHIVED}),
    State.FAILED: frozenset({
        State.IN_PROGRESS,      # human reset — resume the same worker
        State.NEEDS_REVISION,   # human reset — hand the copy back for rework
        State.DRAFT,            # human reset — start the capsule over
        State.ARCHIVED,         # write the capsule off
    }),  # resurrect()/human only
    State.ARCHIVED: frozenset(),                                   # terminal
}

# Edges out of the approval gate that demand a verified artifact digest.
# ``published`` is not reachable from ``pending_approval`` today; it is listed
# defensively so the guard cannot be bypassed if the matrix ever grows that edge.
_APPROVAL_TARGETS: frozenset[State] = frozenset({State.APPROVED, State.PUBLISHED})

TERMINAL_STATES: frozenset[State] = frozenset(
    state for state, targets in TRANSITIONS.items() if not targets
)

REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"capsule_id", "state", "created_at", "updated_at", "attempt", "history"}
)

#: Default per-capsule revision cap (``spend.max_revisions_per_capsule`` in
#: ``config/engine.json``): how many times a capsule may enter
#: ``needs_revision`` while still being dispatched to a worker. The counter
#: may *reach* the cap; the rejection that would push it *over* escalates the
#: capsule to ``failed`` (a human position — resurrect/rework/archive) instead
#: of looping back to a worker. Old manifests without the field count as 0.
DEFAULT_REVISION_CAP = 3


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """Return an aware UTC timestamp in strict ISO-8601 (``+00:00`` offset)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return ``None`` instead of raising.

    Tolerates a trailing ``Z`` (common in hand-written manifests) so the same
    validator works on every supported interpreter.
    """
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def state_age_seconds(manifest: Mapping[str, Any]) -> float | None:
    """Seconds since the manifest entered its *current* state, or ``None``.

    Measured from the newest history entry whose ``to_state`` equals the
    current state (falling back to ``updated_at``), so a re-entry into a state
    (e.g. unfreezing ``stalled`` back to ``pending_approval``) starts a fresh
    clock. Returns ``None`` when nothing usable can be parsed — callers decide
    the policy; this function never guesses.
    """
    try:
        current = State.parse(manifest["state"])
    except (KeyError, ValueError, TypeError):
        return None

    entered: datetime | None = None
    history = manifest.get("history")
    if isinstance(history, list):
        for entry in reversed(history):
            if not isinstance(entry, dict) or entry.get("to_state") != current.value:
                continue
            entered = parse_ts(entry.get("at"))
            if entered is not None:
                break
    if entered is None:
        entered = parse_ts(manifest.get("updated_at"))
    if entered is None:
        return None
    return (datetime.now(timezone.utc) - entered).total_seconds()


def revision_count(manifest: Mapping[str, Any]) -> int:
    """How many times this capsule has been sent back for rework.

    Defensive by design: a pre-cap manifest (no field), a bool, or any
    mistyped value answers ``0`` — the reader never raises, and
    :func:`validate_manifest` already fails closed on load for anything
    present-but-wrong.
    """
    raw = manifest.get("revision_count")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def revision_cap() -> int:
    """Configured per-capsule revision cap (``spend.max_revisions_per_capsule``).

    Fail-safe on purpose: when the spend config cannot be read or parsed, the
    default (:data:`DEFAULT_REVISION_CAP`) applies — a broken file must never
    *lift* the cap (nor block a transition). Mirrored stdlib-only by the
    Watcher, which must not depend on this package.
    """
    try:
        from capsule_engine.spend_control import revision_cap as configured

        return configured()
    except Exception:
        return DEFAULT_REVISION_CAP


# --------------------------------------------------------------------------- #
# TOCTOU artifact binding (SHA-256)
# --------------------------------------------------------------------------- #

def _artifact_target(
    capsule_path: str | os.PathLike[str], relative: str | os.PathLike[str]
) -> Path:
    """Resolve ``relative`` inside the capsule, refusing any escape (fail closed)."""
    capsule = Path(capsule_path).resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SecurityError(
            f"artifact path must be relative to the capsule: {relative!r}"
        )
    target = (capsule / candidate).resolve()
    if target != capsule and capsule not in target.parents:
        raise SecurityError(
            f"artifact path escapes capsule {capsule}: {relative} -> {target}"
        )
    return target


def artifact_sha256(
    capsule_path: str | os.PathLike[str], relative: str | os.PathLike[str] = ARTIFACT_FILE
) -> str:
    """Return the SHA-256 hex digest of a capsule artifact.

    Fail-closed by design: missing, unreadable, oversized, or escaping paths
    raise :class:`SecurityError` instead of returning a guessed digest.
    """
    target = _artifact_target(capsule_path, relative)
    if not target.is_file():
        raise SecurityError(
            f"artifact missing: {target} — cannot compute a binding digest "
            "(fail closed)"
        )
    try:
        size = target.stat().st_size
        if size > MAX_HASH_BYTES:
            raise SecurityError(
                f"artifact {target} is {size} bytes (> {MAX_HASH_BYTES}) — "
                "refusing to hash it"
            )
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except SecurityError:
        raise
    except OSError as exc:
        raise SecurityError(f"artifact {target} is unreadable: {exc}") from exc
    return digest.hexdigest()


def bound_digest(manifest: Mapping[str, Any]) -> str | None:
    """Return ``metadata.artifact_sha256`` when it is a non-empty string."""
    metadata = manifest.get("metadata") if isinstance(manifest, Mapping) else None
    if not isinstance(metadata, dict):
        return None
    digest = metadata.get("artifact_sha256")
    if isinstance(digest, str) and digest.strip():
        return digest.strip().lower()
    return None


def bind_artifact(
    manifest: MutableMapping[str, Any],
    capsule_path: str | os.PathLike[str],
    relative: str | os.PathLike[str] = ARTIFACT_FILE,
) -> str:
    """Hash an artifact and record the digest in ``metadata.artifact_sha256``.

    Also stores ``metadata.artifact_path`` (the relative path that was bound)
    and ``metadata.artifact_bound_at``. Nothing is written to disk by this
    function — the caller persists the manifest atomically as usual.

    Raises:
        SecurityError: the artifact is missing/unreadable/oversized, or the
            relative path escapes the capsule.
        ManifestSchemaError: ``metadata`` exists but is not an object (the
            manifest is corrupt — reported, never silently repaired).
    """
    digest = artifact_sha256(capsule_path, relative)
    metadata = _metadata_for_write(manifest, action="bind the artifact hash")
    metadata["artifact_sha256"] = digest
    metadata["artifact_path"] = Path(relative).as_posix()
    metadata["artifact_bound_at"] = now_iso()
    return digest


def try_bind_artifact(
    manifest: MutableMapping[str, Any],
    capsule_path: str | os.PathLike[str],
    relative: str | os.PathLike[str] = ARTIFACT_FILE,
    *,
    by: str = "system",
) -> str | None:
    """Best-effort :func:`bind_artifact` for the entry into the approval gate.

    Binding failure is *not* allowed to block a lawful state transition (a
    capsule may legitimately reach the gate code paths without an artifact in
    tests or legacy data); it is recorded in ``history`` instead and the
    digest stays unbound — :func:`verify_artifact` then refuses the approval
    edge, which is where fail-closed belongs.
    """
    try:
        digest = bind_artifact(manifest, capsule_path, relative)
    except SecurityError as exc:
        append_history(
            manifest,
            by=by,
            why=(
                f"artifact hash not bound ({Path(relative).as_posix()}): {exc} "
                "— approval will be refused until it is bound (fail closed)"
            ),
        )
        return None
    append_history(
        manifest,
        by=by,
        why=(
            f"artifact hash bound: {Path(relative).as_posix()} "
            f"sha256={digest[:12]}…"
        ),
    )
    return digest


def verify_artifact(
    manifest: Mapping[str, Any],
    capsule_path: str | os.PathLike[str],
    relative: str | os.PathLike[str] | None = None,
) -> str:
    """Re-hash the bound artifact and compare it to the recorded digest.

    Raises:
        SecurityError: no digest is bound, the artifact is missing/unreadable,
            or the bytes on disk no longer match — the TOCTOU refusal.
    """
    expected = bound_digest(manifest)
    if expected is None:
        raise SecurityError(
            "no artifact hash bound in metadata.artifact_sha256 — refusing to "
            "proceed without an integrity proof (fail closed)"
        )
    if relative is None:
        metadata = manifest.get("metadata")
        recorded = (
            metadata.get("artifact_path") if isinstance(metadata, dict) else None
        )
        relative = recorded if isinstance(recorded, str) and recorded.strip() else ARTIFACT_FILE
    actual = artifact_sha256(capsule_path, relative)
    if not hmac.compare_digest(expected, actual):
        raise SecurityError(
            f"artifact {Path(relative).as_posix()} was altered after it entered "
            f"the approval gate (bound sha256 {expected[:12]}…, found "
            f"{actual[:12]}…) — refusing (TOCTOU)"
        )
    return actual


# --------------------------------------------------------------------------- #
# token accounting
# --------------------------------------------------------------------------- #

def _token_count(value: object) -> int:
    """Coerce a token counter defensively: non-negative int, else 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def accumulate_usage(
    manifest: MutableMapping[str, Any], usage: Mapping[str, Any] | None
) -> dict[str, int]:
    """Fold a gateway ``usage`` mapping into ``metadata.usage``.

    Defensive contract: a missing/non-mapping ``usage`` contributes zero, each
    missing or malformed counter defaults to 0, and unknown extra keys in the
    existing usage record are preserved. ``calls`` counts how many non-empty
    usage records were folded in (one per gateway invocation).

    Raises:
        ManifestSchemaError: ``metadata`` exists but is not an object — a
            corrupt manifest is reported, never silently overwritten.
    """
    if not isinstance(usage, Mapping):
        return {}
    metadata = _metadata_for_write(manifest, action="record token usage")
    current = metadata.get("usage")
    current = current if isinstance(current, dict) else {}
    totals: dict[str, int] = dict(current)
    for key in USAGE_KEYS:
        totals[key] = _token_count(current.get(key)) + _token_count(usage.get(key))
    calls = _token_count(current.get("calls")) + 1
    totals["calls"] = calls
    metadata["usage"] = totals
    return totals


def _metadata_for_write(
    manifest: MutableMapping[str, Any], *, action: str
) -> dict[str, Any]:
    """Return a writable ``metadata`` object, creating it when absent."""
    metadata = manifest.get("metadata")
    if metadata is None:
        metadata = {}
        manifest["metadata"] = metadata
    if not isinstance(metadata, dict):
        raise ManifestSchemaError(
            MANIFEST_NAME,
            f"field 'metadata' must be an object to {action}, got "
            f"{type(metadata).__name__}",
        )
    return metadata


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def validate_manifest(data: object, *, source: str = MANIFEST_NAME) -> dict[str, Any]:
    """Validate a decoded manifest and return it narrowed to ``dict``.

    Raises:
        ManifestCorruptError: the payload is not a JSON object.
        ManifestSchemaError: a required field is missing, mistyped, or holds
            an unknown state / unparseable timestamp.
    """
    if not isinstance(data, dict):
        raise ManifestCorruptError(
            source, f"manifest must be a JSON object, got {type(data).__name__}"
        )

    # Normalization: allow hand-written manifests with 'id' or 'prompt'
    if "id" in data and "capsule_id" not in data:
        data["capsule_id"] = data["id"]
    if "prompt" in data and "raw_prompt" not in data:
        data["raw_prompt"] = data["prompt"]

    from shared_kernel.schema_version import (
        KIND_MANIFEST,
        check_schema_version,
    )

    check_schema_version(KIND_MANIFEST, data, source)

    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise ManifestSchemaError(source, f"missing required fields: {sorted(missing)}")

    capsule_id = data["capsule_id"]
    if not isinstance(capsule_id, str) or not capsule_id.strip():
        raise ManifestSchemaError(source, "field 'capsule_id' must be a non-empty string")

    try:
        State.parse(data["state"])
    except ValueError as exc:
        raise ManifestSchemaError(source, str(exc)) from exc

    for field in ("created_at", "updated_at"):
        if parse_ts(data[field]) is None:
            raise ManifestSchemaError(
                source, f"field {field!r} is not an ISO-8601 timestamp: {data[field]!r}"
            )

    attempt = data["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ManifestSchemaError(source, "field 'attempt' must be a non-negative integer")

    # Optional (pre-cap capsules carry none = 0): stamped on every write that
    # enters needs_revision; a present-but-mistyped counter fails closed here.
    if "revision_count" in data:
        revisions = data["revision_count"]
        if isinstance(revisions, bool) or not isinstance(revisions, int) or revisions < 0:
            raise ManifestSchemaError(
                source, "field 'revision_count' must be a non-negative integer"
            )

    history = data["history"]
    if not isinstance(history, list) or not all(isinstance(entry, dict) for entry in history):
        raise ManifestSchemaError(source, "field 'history' must be a list of objects")

    _validate_outputs(data, source)

    return data


#: A capsule may declare up to this many typed deliverables. The legacy model
#: (one primary artifact + optional image) stays valid and is derived when the
#: list is absent, so old capsules keep loading unchanged.
MAX_OUTPUTS = 32


def _safe_output_path(relative: str) -> str:
    """Structurally refuse an output path that is absolute or escapes the capsule.

    Mirrors :func:`_artifact_target`'s rules, but without a capsule directory:
    the filesystem confinement is re-proved by the reader that resolves the
    path (``_artifact_target`` when a file is actually opened).
    """
    text = str(relative).strip()
    if not text:
        raise ManifestSchemaError(
            MANIFEST_NAME, "output 'path' must be a non-empty string"
        )
    if "\x00" in text:
        raise ManifestSchemaError(MANIFEST_NAME, "output 'path' contains a NUL byte")
    pure = PurePosixPath(text.replace("\\", "/"))
    if pure.is_absolute() or text.startswith("/"):
        raise ManifestSchemaError(
            MANIFEST_NAME, f"output 'path' must be relative to the capsule: {text!r}"
        )
    if ".." in pure.parts:
        raise ManifestSchemaError(
            MANIFEST_NAME, f"output 'path' may not escape the capsule: {text!r}"
        )
    return text


def _validate_outputs(data: Mapping[str, Any], source: str) -> None:
    """Validate the optional ``outputs`` list (typed multi-deliverable model).

    Absent or ``None`` is fine (legacy capsule). Present must be a list of
    objects, each with a unique non-empty ``name`` and a capsule-relative
    ``path``; at most one may be ``primary``.
    """
    raw = data.get("outputs")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ManifestSchemaError(source, "field 'outputs' must be a list of objects")
    if len(raw) > MAX_OUTPUTS:
        raise ManifestSchemaError(
            source, f"field 'outputs' declares {len(raw)} entries (> {MAX_OUTPUTS})"
        )
    seen_names: set[str] = set()
    primaries = 0
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ManifestSchemaError(source, f"output #{index} must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManifestSchemaError(
                source, f"output #{index} 'name' must be a non-empty string"
            )
        name = name.strip()
        if name in seen_names:
            raise ManifestSchemaError(source, f"output name {name!r} is duplicated")
        seen_names.add(name)
        _safe_output_path(entry.get("path"))
        for key in ("kind", "sha256"):
            value = entry.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ManifestSchemaError(
                    source, f"output {name!r} '{key}' must be a non-empty string"
                )
        primary = entry.get("primary")
        if primary is not None and not isinstance(primary, bool):
            raise ManifestSchemaError(
                source, f"output {name!r} 'primary' must be a boolean"
            )
        size = entry.get("size_bytes")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, int) or size < 0
        ):
            raise ManifestSchemaError(
                source, f"output {name!r} 'size_bytes' must be a non-negative integer"
            )
        if primary:
            primaries += 1
    if primaries > 1:
        raise ManifestSchemaError(source, "at most one output may be marked 'primary'")


# --------------------------------------------------------------------------- #
# read / write
# --------------------------------------------------------------------------- #

def manifest_path(capsule_path: str | os.PathLike[str]) -> Path:
    """Return the absolute path of ``manifest.json`` inside a capsule."""
    return Path(capsule_path) / MANIFEST_NAME


def load_manifest(capsule_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate a capsule manifest from disk.

    Raises:
        CapsuleNotFoundError: the capsule directory or manifest is absent.
        ManifestCorruptError: unreadable JSON, oversized file, or non-object.
        ManifestSchemaError: structurally invalid manifest.
    """
    capsule = Path(capsule_path)
    path = capsule / MANIFEST_NAME
    if not capsule.is_dir():
        raise CapsuleNotFoundError(f"capsule directory does not exist: {capsule}")
    if not path.is_file():
        raise CapsuleNotFoundError(f"capsule has no manifest: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestCorruptError(str(path), f"unreadable: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestCorruptError(str(path), f"file exceeds {MAX_MANIFEST_BYTES} bytes")

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestCorruptError(
            str(path), f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc

    return validate_manifest(decoded, source=str(path))


def save_manifest(
    capsule_path: str | os.PathLike[str],
    manifest: MutableMapping[str, Any],
) -> None:
    """Atomically persist a manifest: validate → size gate → temp → replace.

    The temp file lives in the same directory (so ``os.replace`` is a rename,
    not a cross-device copy) and carries the same permissions. A crash at any
    point leaves either the old manifest or the new one — never a torn file.

    The size gate runs *before anything is written*: a payload the reader
    would reject (``load_manifest`` refuses > :data:`MAX_MANIFEST_BYTES`) is
    refused here instead, so a capsule can never brick itself with a manifest
    it can no longer load. The previous manifest stays intact.

    Raises:
        ManifestSchemaError: structurally invalid manifest (via validation).
        ManifestTooLargeError: serialized payload exceeds the reader's cap.
    """
    from shared_kernel.schema_version import (
        KIND_MANIFEST,
        stamp_schema_version,
    )

    stamp_schema_version(KIND_MANIFEST, manifest)
    validate_manifest(manifest)  # never write a manifest we would refuse to read
    capsule = Path(capsule_path)
    target = capsule / MANIFEST_NAME
    payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )
    size = len(payload.encode("utf-8"))
    if size > MAX_MANIFEST_BYTES:
        raise ManifestTooLargeError(str(target), size, MAX_MANIFEST_BYTES)

    # Same proven temp+fsync+replace dance as always, via the shared kernel
    # helper (same-dir .<name>.<pid>.tmp, chmod 0o644, os.replace, dir fsync).
    atomic_text(target, payload)


# --------------------------------------------------------------------------- #
# construction and mutation
# --------------------------------------------------------------------------- #

def new_manifest(
    capsule_id: str,
    *,
    state: State = State.DRAFT,
    meta: Mapping[str, Any] | None = None,
    tickets: Sequence[str] | None = None,
    bundle_type: str = "standard",
    channels: Sequence[str] | None = None,
    company_context: Mapping[str, Any] | None = None,
    event_date: str | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Create a fresh, schema-valid manifest for a new capsule."""
    if not isinstance(capsule_id, str) or not capsule_id.strip():
        raise ManifestSchemaError(MANIFEST_NAME, "capsule_id must be a non-empty string")
    now = now_iso()
    from capsule_engine.audit import append_chained
    history: list[dict[str, Any]] = []
    append_chained(
        history,
        {
            "at": now,
            "from_state": None,
            "to_state": state.value,
            "by": "system",
            "why": "capsule created",
        },
    )
    manifest: dict[str, Any] = {
        "capsule_id": capsule_id.strip(),
        "state": state.value,
        "created_at": now,
        "updated_at": now,
        "attempt": 0,
        "revision_count": 0,
        "history": history,
    }
    if meta:
        manifest["meta"] = dict(meta)
    if tickets:
        manifest["tickets"] = [str(t).strip() for t in tickets if str(t).strip()]
    if bundle_type and bundle_type != "standard":
        manifest["bundle_type"] = str(bundle_type)
    if channels:
        manifest["channels"] = [str(c).strip() for c in channels if str(c).strip()]
    if company_context:
        manifest["company_context"] = dict(company_context)
    if event_date:
        manifest["event_date"] = str(event_date).strip()
    if ttl_hours is not None:
        manifest["ttl_hours"] = int(ttl_hours)
    return manifest


def append_history(
    manifest: MutableMapping[str, Any],
    *,
    by: str,
    why: str,
    from_state: State | str | None = None,
    to_state: State | str | None = None,
    secret: bytes | None = None,
) -> None:
    """Append an auditable history entry and bump ``updated_at``."""
    history = manifest.setdefault("history", [])
    if not isinstance(history, list):
        raise ManifestSchemaError(MANIFEST_NAME, "field 'history' must be a list")
    from capsule_engine.audit import append_chained
    append_chained(
        history,
        {
            "at": now_iso(),
            "from_state": State.parse(from_state).value if from_state is not None else None,
            "to_state": State.parse(to_state).value if to_state is not None else None,
            "by": str(by),
            "why": str(why),
        },
        secret=secret,
    )
    manifest["updated_at"] = now_iso()


def transition(
    manifest: MutableMapping[str, Any],
    new_state: State | str,
    *,
    by: str,
    why: str,
    capsule_path: str | os.PathLike[str] | None = None,
) -> State:
    """Move ``manifest`` to ``new_state`` along a lawful edge, recording it.

    One table edge is conditional and enforced here: ``in_review ->
    pending_approval`` (the seat at the approval gate, used by the gate
    runner's finish and the cockpit's decision route) additionally requires
    a **complete gate plan** — every gate terminal, none failed — and
    refuses otherwise, so no caller can seat an ungoverned capsule.

    When ``capsule_path`` is supplied, entering ``pending_approval`` binds
    the artifact (SHA-256) into ``metadata.artifact_sha256`` — any relative
    file the task declares, defaulting to :data:`ARTIFACT_FILE` (best
    effort: failure is recorded, approval later refuses an unbound capsule).

    **Every edge into** ``approved``/``published`` is an approval boundary,
    whatever its source state: the bound digest must re-verify against the
    live artifact (:class:`SecurityError` on a missing binding, a missing
    artifact, or a mismatch — nothing is mutated), and a call without
    ``capsule_path`` is refused outright: there is nothing to verify, so it
    can never lawfully approve or publish.

    **Revision cap**: every entry into ``needs_revision`` increments
    ``manifest['revision_count']`` (old manifests start at 0) — the single
    chokepoint every rejection source (QA, human gate, design gate, gate
    runner) moves through, so no rework loop can dodge the counter. When the
    new count would push the counter *over* ``spend.max_revisions_per_capsule``
    (default 3), the capsule does not stay in ``needs_revision`` waiting for a
    worker: a second lawful edge (``needs_revision -> failed``) escalates it to
    the human-reset position with a clear reason, and this function returns
    ``State.FAILED``. The caller persists the same manifest either way — the
    counter and the escalation are written together, atomically.

    Raises:
        StateTransitionError: the edge is not declared in :data:`TRANSITIONS`.
        ManifestSchemaError: the current state on the manifest is unknown.
        SecurityError: the approval edge failed its integrity guard.
    """
    target = new_state if isinstance(new_state, State) else State.parse(new_state)
    current = State.parse(manifest["state"])
    if target not in TRANSITIONS[current]:
        legal = ", ".join(sorted(s.value for s in TRANSITIONS[current])) or "none (terminal)"
        raise StateTransitionError(
            f"illegal transition {current.value} -> {target.value} (legal from "
            f"{current.value}: {legal})"
        )

    if current is State.IN_REVIEW and target is State.PENDING_APPROVAL:
        # The seat at the approval gate is lawful ONLY with a complete gate
        # plan: every declared gate terminal, none failed (fail closed —
        # gate_runner's finish and the decision route's seat both rely on
        # this edge, and neither may seat an ungoverned capsule).
        # Lazy import: capsule_engine.gates imports this module at load time.
        from capsule_engine import gates as gate_engine

        try:
            records = gate_engine.load_records(manifest)
        except gate_engine.GateSchemaError as exc:
            raise StateTransitionError(
                "illegal transition in_review -> pending_approval: gate plan "
                f"unreadable — human repair required ({exc})"
            ) from exc
        if not gate_engine.is_complete(records):
            progress = gate_engine.progress(records)
            raise StateTransitionError(
                "illegal transition in_review -> pending_approval: the gate "
                f"plan is incomplete ({progress['passed']} of "
                f"{progress['total']} gates passed) — every gate must reach a "
                "terminal verdict first"
            )

    if target in _APPROVAL_TARGETS:
        # EVERY edge into approved/published is an approval boundary,
        # whatever its source state: the artifact digest bound at the gate
        # must re-verify against the live bytes, or nothing is mutated. A
        # path-less call has nothing to verify, so it can never lawfully
        # approve or publish (the old unbound-legacy carve-out is gone).
        if capsule_path is None:
            raise SecurityError(
                f"refusing {current.value} -> {target.value} without a capsule "
                "path: the bound artifact cannot be verified (fail closed)"
            )
        verify_artifact(manifest, capsule_path)

    if capsule_path is not None:
        if current is not State.PENDING_APPROVAL and target is State.PENDING_APPROVAL:
            # Unfreezing must not launder a tampered artifact: keep the digest
            # taken while the capsule sat frozen.
            if not (current is State.STALLED and bound_digest(manifest) is not None):
                try_bind_artifact(manifest, capsule_path, by=by)

    append_history(
        manifest, by=by, why=why, from_state=current, to_state=target
    )
    manifest["state"] = target.value

    if target is State.NEEDS_REVISION:
        count = revision_count(manifest) + 1
        manifest["revision_count"] = count
        cap = revision_cap()
        if count > cap:
            # Revision cap exceeded: a HUMAN decision now, never a worker.
            # ``failed`` is the constitutional human-reset position (the same
            # one the attempt cap uses): the router holds it, the Watcher
            # reports it, and ``resurrect()`` is the defined way out. Written
            # here — inside the transition, under the caller's lock — so the
            # counter and the escalation persist together or not at all.
            append_history(
                manifest,
                by=by,
                why=(
                    f"revision cap exceeded ({count} > {cap}) — human decision "
                    "required (resurrect / rework / archive); no worker dispatch"
                ),
                from_state=State.NEEDS_REVISION,
                to_state=State.FAILED,
            )
            manifest["state"] = State.FAILED.value
            return State.FAILED
    return target


def seat_at_approval_gate(
    manifest: MutableMapping[str, Any],
    capsule_path: str | os.PathLike[str],
    *,
    by: str,
    why: str,
) -> State:
    """Seat a capsule at the production approval gate: fresh plan, bound digest.

    Every record that enters the *production* spool is seated here, and only
    here — a direct cockpit creation, a sandbox promotion. Seating is a
    **birth**, not a table edge: the record's production life starts at
    ``pending_approval`` whatever state it held elsewhere (a sandbox capsule
    may arrive as ``approved`` or ``published``, and no lawful edge leads back
    to the gate), while :data:`TRANSITIONS` keeps governing every edge *out* of
    it. No caller may seat a capsule at any other state.

    Nothing is persisted by this function — the caller saves (or refuses) the
    manifest afterwards. Order matters:

    1. a chained history entry ``None -> pending_approval`` records the seat
       and the state moves to ``pending_approval``;
    2. a **fresh** gate plan is seeded from the policy template; any plan the
       record already carries (sandbox verdicts included) is discarded first,
       so no pre-existing approval can survive the seat. Gates disabled by
       policy means no plan; when gates are enabled and seeding fails, the
       error propagates and the caller must persist nothing (fail closed);
    3. :func:`try_bind_artifact` binds the artifact digest — approval later
       refuses an unbound digest, so a bind failure surfaces at the edge it
       protects.

    Returns:
        ``State.PENDING_APPROVAL`` — the only lawful seat.

    Raises:
        GateConfigError/GateSchemaError: the fresh plan could not be seeded.
        ValueError: the policy template for this capsule's domain is invalid.
    """
    # Lazy imports: capsule_engine.gates imports this module at load time.
    from capsule_engine import gates as gate_engine
    from capsule_engine.policy import load_policy

    # 1. seat at the gate (a birth — see the docstring for why not transition()).
    append_history(
        manifest,
        by=by,
        why=why,
        from_state=None,
        to_state=State.PENDING_APPROVAL,
    )
    manifest["state"] = State.PENDING_APPROVAL.value

    # 2. fresh gate plan: validate the template first, then replace whatever
    #    the record carried — and let any seeding error propagate to the caller.
    policy = load_policy()
    if policy.gates.enabled:
        domain = str(manifest.get("domain") or "").strip() or None
        specs = gate_engine.specs_from_template(
            policy.gates.template_for(domain),
            max_gates=policy.gates.max_gates,
        )
        manifest["gates"] = []  # discard any pre-existing plan, verdicts and all
        gate_engine.ensure_plan(manifest, specs, by=by, replace_empty=True)

    # 3. bind the artifact digest (best effort by design — see try_bind_artifact).
    try_bind_artifact(manifest, capsule_path, by=by)
    return State.PENDING_APPROVAL


def resurrect(
    manifest: dict[str, Any],
    target_state: State | str = State.NEEDS_REVISION,
    why: str = "human intervention: capsule resurrected",
    *,
    by: str = "human",
) -> dict[str, Any]:
    """Return a failed capsule to the pipeline: attempt reset, lawful move.

    The human escape hatch of the attempt-cap policy — a ``failed`` capsule
    moves to ``target_state`` (a lawful edge out of ``FAILED``:
    ``needs_revision``, ``draft``, ``in_progress`` or ``archived``) and its
    ``attempt`` counter resets to 0 so the next worker run starts fresh. The
    ``revision_count`` resets for the same reason: the cap exists to stop the
    *automated* loop, and a resurrection is exactly the human decision it
    demanded — the human re-opens a fresh budget of :data:`DEFAULT_REVISION_CAP`
    rounds. (Reset before the move: entering ``needs_revision`` itself counts
    as entry 1 of the fresh budget.) The
    move is recorded in ``history`` like every other state change; call
    :func:`save_manifest` afterwards to persist it.

    Args:
        manifest: a loaded manifest whose current state is ``failed``.
        target_state: where the human restarts the capsule.
        why: the audit note appended to ``history``.
        by: who performed the resurrection (default ``"human"``).

    Raises:
        StateTransitionError: the capsule is not ``failed``, or
            ``target_state`` is not a lawful edge out of ``FAILED`` — nothing
            is mutated in either case.
    """
    current = State.parse(manifest["state"])
    if current is not State.FAILED:
        raise StateTransitionError(
            f"resurrect() requires a failed capsule, found {current.value!r}"
        )
    manifest["revision_count"] = 0  # the human re-opens a fresh revision budget
    transition(manifest, target_state, by=by, why=why)
    manifest["attempt"] = 0
    return manifest


def last_history_entry(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the newest history entry, or ``None`` if history is empty/unusable."""
    history = manifest.get("history")
    if isinstance(history, Sequence) and not isinstance(history, str) and history:
        tail = history[-1]
        if isinstance(tail, dict):
            return tail
    return None


def set_capsule_bundle(
    manifest: MutableMapping[str, Any],
    ticket_ids: Sequence[str],
    *,
    bundle_type: str = "standard",
    channels: Sequence[str] | None = None,
    company_context: Mapping[str, Any] | None = None,
    event_date: str | None = None,
    ttl_hours: int | None = None,
) -> None:
    """Associate a list of ticket IDs and bundle parameters with this capsule."""
    manifest["tickets"] = [str(t).strip() for t in ticket_ids if str(t).strip()]
    manifest["bundle_type"] = str(bundle_type or "standard")
    if channels is not None:
        manifest["channels"] = [str(c).strip() for c in channels if str(c).strip()]
    if company_context is not None:
        manifest["company_context"] = dict(company_context)
    if event_date is not None:
        manifest["event_date"] = str(event_date).strip()
    if ttl_hours is not None:
        manifest["ttl_hours"] = int(ttl_hours)


def get_capsule_tickets(manifest: Mapping[str, Any]) -> list[str]:
    """Return the list of ticket IDs bundled into this capsule."""
    raw = manifest.get("tickets")
    if isinstance(raw, list):
        return [str(t) for t in raw if isinstance(t, (str, int))]
    return []


def get_capsule_channels(manifest: Mapping[str, Any]) -> list[str]:
    """Return the target channels declared on this capsule."""
    raw = manifest.get("channels")
    if isinstance(raw, list):
        return [str(c) for c in raw if isinstance(c, str)]
    return []


def get_capsule_company_context(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the company context dictionary, or empty dict if none."""
    raw = manifest.get("company_context")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def get_capsule_outputs(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the capsule's declared typed outputs (normalized copies).

    Historically a capsule carried exactly one deliverable
    (``metadata.artifact_path``) plus an optional image. This returns the
    explicit ``outputs`` list when declared; otherwise it derives the implicit
    single-entry list from the artefact binding so *every* capsule answers the
    same shape. Callers that can touch the disk (the API) may append detected
    files (e.g. ``image.jpg``) on top.
    """
    raw = manifest.get("outputs")
    if isinstance(raw, list) and raw:
        return [dict(entry) for entry in raw if isinstance(entry, dict)]

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    artifact_rel = metadata.get("artifact_path") or ARTIFACT_FILE
    return [
        {
            "name": "primary",
            "path": str(artifact_rel),
            "kind": metadata.get("artifact_kind") or "text/markdown",
            "sha256": metadata.get("artifact_sha256"),
            "primary": True,
        }
    ]


def set_capsule_outputs(
    manifest: MutableMapping[str, Any], outputs: Sequence[Mapping[str, Any]]
) -> None:
    """Replace ``manifest['outputs']`` with a validated list of deliverables.

    Validation runs through :func:`validate_manifest`, so an invalid entry
    raises :class:`ManifestSchemaError` here rather than at save time.
    """
    manifest["outputs"] = [dict(entry) for entry in outputs]
    _validate_outputs(manifest, MANIFEST_NAME)
