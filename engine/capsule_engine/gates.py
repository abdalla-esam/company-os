#!/usr/bin/env python3
"""gates.py — the universal multi-gate / multi-checkpoint engine.

A capsule's approval phase is not one gate: it is a **plan** of 1..N gates,
declared per domain in ``config/engine.json`` (or supplied per capsule),
stored inside ``manifest.json`` under the ``gates`` key — one JSON array,
completely human-inspectable.

Design contract (nothing here is domain-specific):

- **Unbounded plans**: a plan can carry 1, 2, 5 or 10 gates; the only bound is
  the configurable ``gates.max_gates`` sanity cap. Gate identities are
  deterministic (``g1``..``gN``) so plans are stable across pulses.
- **Element-level precision**: every gate declares ``targets`` — exact
  relative paths (``deliverables/facebook/copy.txt``), globs
  (``deliverables/**/*``), or nothing (the capsule's primary artifact). When a
  gate opens, every resolved target is SHA-256 bound into the gate record; a
  human approval re-hashes the same bytes before the gate may pass (the same
  anti-TOCTOU rule the capsule-level binding enforces, now per element).
- **Closed typology vocabulary, open evaluators**: the engine ships five
  gate types — ``qa_review``, ``security_audit`` (automated),
  ``domain_lead_approval``, ``manual_edit``, ``sovereign_root_approval``
  (human/authority). A branch may register additional *automated* evaluators
  through :func:`register_evaluator`; an unknown type is refused, never
  guessed.
- **Fail-closed dual control**: a gate can only be decided when every earlier
  gate has passed (or was lawfully skipped); a rejection marks the gate
  ``failed`` and the caller drops the flow to ``needs_revision``. Nothing
  downstream is ever reachable while a gate is incomplete.
- **Corruption is reported**: a malformed plan raises :class:`GateSchemaError`
  (the router then holds the capsule for human repair); a decision that would
  violate ordering raises :class:`GateOrderError`; a target hash mismatch
  raises :class:`GateTargetError` (a :class:`SecurityError`), and nothing is
  mutated in either case.

This module is pure manifest logic + bounded file hashing. It never writes a
manifest, never takes a capsule lock, and never calls a network gear: callers
(workers, the API, the pulse) own persistence and locking. Persisting the
resulting dict is the caller's ``save_manifest`` call.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

from capsule_engine.editorial import BANNED_BUZZWORDS, BANNED_PHRASES
from capsule_engine.exceptions import CapsuleError, SecurityError
from capsule_engine.manifest import (
    ARTIFACT_FILE,
    append_history,
    artifact_sha256,
)

logger = logging.getLogger("capsule_engine.gates")

__all__ = [
    "GATE_TYPES",
    "AUTOMATED_GATE_TYPES",
    "HUMAN_GATE_TYPES",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_PASSED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "STATUSES",
    "TERMINAL_STATUSES",
    "MAX_EVIDENCE_CHARS",
    "GateError",
    "GateConfigError",
    "GateSchemaError",
    "GateOrderError",
    "GateTargetError",
    "GateEvaluationError",
    "GateSpec",
    "GateRecord",
    "GateVerdict",
    "EvaluationContext",
    "PlanResult",
    "specs_from_template",
    "template_for",
    "plan_from_specs",
    "load_records",
    "manifest_has_gates",
    "ensure_plan",
    "reset_plan",
    "active_record",
    "is_complete",
    "checklist",
    "progress",
    "summary_line",
    "bind_targets",
    "verify_targets",
    "resolve_targets",
    "decide",
    "register_evaluator",
    "unregister_evaluator",
    "registered_evaluators",
    "evaluate",
    "human_authority",
    "is_automated",
    "is_human",
]


# --------------------------------------------------------------------------- #
# vocabulary & statuses
# --------------------------------------------------------------------------- #

#: The closed vocabulary of gate typologies the engine understands.
GATE_TYPES: frozenset[str] = frozenset(
    {
        "qa_review",              # automated QA / syntax / lint validation
        "security_audit",         # automated secret & vulnerability scan
        "domain_lead_approval",   # domain lead authorization (capability)
        "manual_edit",            # human intervention, in-place edit allowed
        "sovereign_root_approval",  # sovereign root cryptographic sign-off
    }
)

#: Automated typologies: evaluated by a registered, deterministic evaluator.
AUTOMATED_GATE_TYPES: frozenset[str] = frozenset({"qa_review", "security_audit"})

#: Human/authority typologies: decided by a capability-holding principal.
HUMAN_GATE_TYPES: frozenset[str] = GATE_TYPES - AUTOMATED_GATE_TYPES

STATUS_PENDING = "pending"    # declared, not yet opened
STATUS_RUNNING = "running"    # opened; automated evaluation in flight
STATUS_PASSED = "passed"      # terminal: gate satisfied
STATUS_FAILED = "failed"      # terminal: gate rejected the deliverable
STATUS_SKIPPED = "skipped"    # terminal: optional gate lawfully skipped

STATUSES: frozenset[str] = frozenset(
    {STATUS_PENDING, STATUS_RUNNING, STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED}
)
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED}
)

#: Evidence/reason notes are bounded: a gate record is a verdict, not a log.
MAX_EVIDENCE_CHARS = 2000
#: A gate may bind at most this many target files (CPU-light by construction).
MAX_TARGET_FILES = 512
#: ... and at most this many total bytes while hashing a plan's targets.
MAX_TARGET_BYTES = 64 * 1024 * 1024  # 64 MiB

GATE_ID_PREFIX = "g"
MAX_TITLE_CHARS = 120


class GateError(CapsuleError):
    """Base class for every deliberate gate-layer failure."""


class GateConfigError(GateError):
    """A gate template/spec is malformed — refuse it, never guess."""


class GateSchemaError(GateError):
    """The manifest's ``gates`` array is structurally invalid or unknown."""


class GateOrderError(GateError):
    """A decision tried to jump its position in the fail-closed sequence."""


class GateTargetError(SecurityError):
    """A gate target could not be bound/verified (missing, escaped, altered).

    Derives from :class:`SecurityError` so every existing fail-closed handler
    treats it as an integrity refusal: nothing is mutated when it is raised.
    """


class GateEvaluationError(GateError):
    """An automated evaluator refused to run (unknown type, unusable input)."""


# --------------------------------------------------------------------------- #
# specs & records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GateSpec:
    """A validated gate declaration (the *plan*, before any decision)."""

    gate_id: str
    gate_type: str
    name: str
    targets: Tuple[str, ...] = ()
    required: bool = True
    authority: Optional[str] = None

    @property
    def automated(self) -> bool:
        return self.gate_type in AUTOMATED_GATE_TYPES

    def to_manifest(self, *, status: str = STATUS_PENDING) -> Dict[str, Any]:
        """The on-disk record seeded for this spec."""
        record: Dict[str, Any] = {
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "name": self.name,
            "targets": list(self.targets),
            "required": self.required,
            "status": status,
            "opened_at": None,
            "closed_at": None,
            "decided_by": None,
            "reason": None,
            "artifacts": {},
            "evidence": {},
        }
        if self.authority:
            record["authority"] = self.authority
        return record


@dataclass(frozen=True)
class GateRecord:
    """One gate of a plan as read from the manifest (validated)."""

    gate_id: str
    gate_type: str
    name: str
    targets: Tuple[str, ...]
    required: bool
    status: str
    authority: Optional[str]
    opened_at: Optional[str]
    closed_at: Optional[str]
    decided_by: Optional[str]
    reason: Optional[str]
    artifacts: Mapping[str, str]
    evidence: Mapping[str, Any]

    @property
    def automated(self) -> bool:
        return self.gate_type in AUTOMATED_GATE_TYPES

    @property
    def human(self) -> bool:
        return not self.automated

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def to_manifest(self) -> Dict[str, Any]:
        """Round-trip back to the manifest record shape."""
        record: Dict[str, Any] = {
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "name": self.name,
            "targets": list(self.targets),
            "required": self.required,
            "status": self.status,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "artifacts": dict(self.artifacts),
            "evidence": dict(self.evidence),
        }
        if self.authority:
            record["authority"] = self.authority
        return record

    def to_view(self) -> Dict[str, Any]:
        """The compact radar/preview view of this gate."""
        view = {
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "passed": self.passed,
        }
        if self.closed_at:
            view["closed_at"] = self.closed_at
        if self.decided_by:
            view["decided_by"] = self.decided_by
        if self.reason:
            view["reason"] = self.reason
        return view


@dataclass(frozen=True)
class GateVerdict:
    """An automated evaluator's verdict (deterministic, evidence-carrying)."""

    passed: bool
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise GateEvaluationError("verdict.passed must be a bool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise GateEvaluationError("verdict.reason must be a non-empty string")


@dataclass(frozen=True)
class EvaluationContext:
    """Everything an evaluator may look at (bounded, read-only)."""

    capsule_path: str
    manifest: Mapping[str, Any]
    gate: GateRecord
    files: Tuple[str, ...]           # resolved relative target paths (text-ish)
    texts: Mapping[str, str]         # relative path -> content (bounded)
    binary_files: Tuple[str, ...]    # resolved targets that are non-textual
    missing_targets: Tuple[str, ...]  # declared patterns that matched nothing


@dataclass(frozen=True)
class PlanResult:
    """Outcome of :func:`ensure_plan`."""

    created: bool
    count: int
    gate_ids: Tuple[str, ...]


# --------------------------------------------------------------------------- #
# template → specs
# --------------------------------------------------------------------------- #

_SAFE_NAME = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(value: object, *, what: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateConfigError(f"{what} must be a non-empty string")
    cleaned = _SAFE_NAME.sub(" ", value.strip())[:max_chars]
    return cleaned


def _validate_target(target: object) -> str:
    """A target is a relative path/glob; absolute or escaping paths refused."""
    if not isinstance(target, str) or not target.strip():
        raise GateConfigError("gate target must be a non-empty string")
    candidate = target.strip()
    path = Path(candidate)
    if path.is_absolute():
        raise GateConfigError(f"gate target must be relative: {candidate!r}")
    if ".." in path.parts:
        raise GateConfigError(f"gate target must stay inside the capsule: {candidate!r}")
    return candidate


def specs_from_template(
    template: Sequence[Mapping[str, Any]],
    *,
    prefix: str = GATE_ID_PREFIX,
    max_gates: int = 64,
) -> Tuple[GateSpec, ...]:
    """Validate a template into ordered :class:`GateSpec` objects.

    Refuses: unknown gate types, empty/oversized plans, duplicates targets,
    absolute or escaping target paths, and malformed authority/name fields.
    """
    if not isinstance(template, Sequence) or isinstance(template, (str, bytes)):
        raise GateConfigError("gate template must be a list of gate objects")
    if not template:
        raise GateConfigError("gate template must declare at least one gate")
    if len(template) > max_gates:
        raise GateConfigError(
            f"gate template declares {len(template)} gates (> max_gates {max_gates})"
        )

    specs: list[GateSpec] = []
    seen_targets: set[str] = set()
    for index, entry in enumerate(template, start=1):
        if not isinstance(entry, Mapping):
            raise GateConfigError(f"gate #{index} must be an object")
        gate_type = entry.get("gate_type")
        if not isinstance(gate_type, str) or gate_type.strip() not in GATE_TYPES:
            known = ", ".join(sorted(GATE_TYPES))
            raise GateConfigError(
                f"gate #{index}: unknown gate_type {gate_type!r} (known: {known})"
            )
        gate_type = gate_type.strip()

        name = entry.get("name")
        if name is None:
            name = gate_type.replace("_", " ").title()
        display = _clean_text(name, what=f"gate #{index} name", max_chars=MAX_TITLE_CHARS)

        targets_raw = entry.get("targets") or ()
        if isinstance(targets_raw, (str, bytes)):
            raise GateConfigError(f"gate #{index}: targets must be a list, not a string")
        targets = tuple(_validate_target(t) for t in targets_raw)
        for target in targets:
            if target in seen_targets:
                # Duplicate patterns across gates are a config smell, but not
                # a security problem; keep them (a gate may re-check an element
                # after an in-place manual edit) — no refusal.
                continue
            seen_targets.add(target)

        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise GateConfigError(f"gate #{index}: required must be a boolean")

        authority = entry.get("authority")
        if authority is not None and (
            not isinstance(authority, str) or not authority.strip()
        ):
            raise GateConfigError(f"gate #{index}: authority must be a non-empty string")

        specs.append(
            GateSpec(
                gate_id=f"{prefix}{index}",
                gate_type=gate_type,
                name=display,
                targets=targets,
                required=required,
                authority=authority.strip() if isinstance(authority, str) else None,
            )
        )
    return tuple(specs)


def template_for(
    domain: Optional[str],
    policy: Optional[Any] = None,
) -> Tuple[Mapping[str, Any], ...]:
    """Return the raw template governing ``domain`` from the engine policy."""
    if policy is None:
        from capsule_engine.policy import load_policy

        policy = load_policy()
    return tuple(policy.gates.template_for(domain))


def plan_from_specs(specs: Sequence[GateSpec]) -> Tuple[Dict[str, Any], ...]:
    """Seed the manifest records for a validated spec list (all ``pending``)."""
    return tuple(spec.to_manifest() for spec in specs)


def human_authority(
    gate_type: str,
    explicit: Optional[str] = None,
    policy: Optional[Any] = None,
) -> Optional[str]:
    """The capability a human gate demands (explicit → policy map → None)."""
    if explicit:
        return explicit
    if policy is None:
        from capsule_engine.policy import load_policy

        policy = load_policy()
    return policy.gates.authority_for(gate_type)


def is_automated(gate_type: str) -> bool:
    """True when ``gate_type`` is evaluated deterministically by the engine."""
    return gate_type in AUTOMATED_GATE_TYPES


def is_human(gate_type: str) -> bool:
    """True when ``gate_type`` requires a capability-holding principal."""
    return gate_type in HUMAN_GATE_TYPES


# --------------------------------------------------------------------------- #
# manifest I/O (pure: parse + validate; callers persist)
# --------------------------------------------------------------------------- #

def manifest_has_gates(manifest: Mapping[str, Any]) -> bool:
    """True when the manifest carries a non-empty gate plan."""
    raw = manifest.get("gates")
    return isinstance(raw, list) and bool(raw)


def _parse_record(raw: object, *, index: int) -> GateRecord:
    if not isinstance(raw, Mapping):
        raise GateSchemaError(f"gates[{index}] must be an object")
    gate_id = raw.get("gate_id")
    if not isinstance(gate_id, str) or not gate_id.strip():
        raise GateSchemaError(f"gates[{index}].gate_id must be a non-empty string")
    gate_type = raw.get("gate_type")
    if not isinstance(gate_type, str) or gate_type.strip() not in GATE_TYPES:
        known = ", ".join(sorted(GATE_TYPES))
        raise GateSchemaError(
            f"gate {gate_id!r}: unknown gate_type {gate_type!r} (known: {known})"
        )
    status = raw.get("status", STATUS_PENDING)
    if not isinstance(status, str) or status not in STATUSES:
        raise GateSchemaError(
            f"gate {gate_id!r}: unknown status {status!r} "
            f"(known: {', '.join(sorted(STATUSES))})"
        )
    targets_raw = raw.get("targets") or ()
    if isinstance(targets_raw, (str, bytes)) or not isinstance(
        targets_raw, (list, tuple)
    ):
        raise GateSchemaError(f"gate {gate_id!r}: targets must be a list")
    try:
        targets = tuple(_validate_target(t) for t in targets_raw)
    except GateConfigError as exc:
        raise GateSchemaError(f"gate {gate_id!r}: {exc}") from exc

    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise GateSchemaError(f"gate {gate_id!r}: required must be a boolean")

    artifacts_raw = raw.get("artifacts") or {}
    if not isinstance(artifacts_raw, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in artifacts_raw.items()
    ):
        raise GateSchemaError(
            f"gate {gate_id!r}: artifacts must map target paths to sha256 strings"
        )

    evidence_raw = raw.get("evidence") or {}
    if not isinstance(evidence_raw, Mapping):
        raise GateSchemaError(f"gate {gate_id!r}: evidence must be an object")

    def _opt_str(key: str) -> Optional[str]:
        value = raw.get(key)
        return value if isinstance(value, str) and value.strip() else None

    name = raw.get("name")
    return GateRecord(
        gate_id=gate_id.strip(),
        gate_type=gate_type.strip(),
        name=name.strip() if isinstance(name, str) and name.strip() else gate_type,
        targets=targets,
        required=required,
        status=status,
        authority=_opt_str("authority"),
        opened_at=_opt_str("opened_at"),
        closed_at=_opt_str("closed_at"),
        decided_by=_opt_str("decided_by"),
        reason=_opt_str("reason"),
        artifacts={str(k): str(v) for k, v in artifacts_raw.items()},
        evidence=dict(evidence_raw),
    )


def load_records(manifest: Mapping[str, Any]) -> Tuple[GateRecord, ...]:
    """Read and validate ``manifest["gates"]``.

    Returns an empty tuple when no plan exists. Raises
    :class:`GateSchemaError` when the plan is structurally invalid — callers
    hold the capsule for repair rather than guessing (fail closed).
    """
    raw = manifest.get("gates")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GateSchemaError("'gates' must be a list of gate objects")
    records = tuple(_parse_record(entry, index=i) for i, entry in enumerate(raw))
    ids = [record.gate_id for record in records]
    if len(set(ids)) != len(ids):
        raise GateSchemaError("gate_id values must be unique within a plan")
    return records


def ensure_plan(
    manifest: MutableMapping[str, Any],
    specs: Sequence[GateSpec],
    *,
    by: str = "system",
    replace_empty: bool = False,
) -> PlanResult:
    """Idempotently seed ``manifest["gates"]`` from a validated plan.

    Never rewinds: an existing valid plan is returned untouched, decisions and
    all. An existing *empty* list is seeded when ``replace_empty`` is set
    (the lawful repair path for a hand-truncated plan). ``by`` stamps the
    history note for auditability.
    """
    raw = manifest.get("gates")
    if raw is not None and not isinstance(raw, list):
        raise GateSchemaError("'gates' must be a list of gate objects")
    if isinstance(raw, list) and raw:
        existing = load_records(manifest)  # validate in place, fail closed
        return PlanResult(
            created=False,
            count=len(existing),
            gate_ids=tuple(record.gate_id for record in existing),
        )
    if isinstance(raw, list) and not raw and not replace_empty and raw is not None:
        # An explicitly empty plan might be a deliberate "no gates" — refuse to
        # silently re-add gates unless the caller asked for the repair.
        return PlanResult(created=False, count=0, gate_ids=())

    records = plan_from_specs(specs)
    manifest["gates"] = [dict(record) for record in records]
    append_history(
        manifest,
        by=by,
        why=f"gate plan initialized: {len(records)} gate(s) — "
        + ", ".join(f"{r['gate_id']}:{r['gate_type']}" for r in records),
    )
    return PlanResult(
        created=True,
        count=len(records),
        gate_ids=tuple(str(record["gate_id"]) for record in records),
    )


def reset_plan(
    manifest: MutableMapping[str, Any],
    specs: Optional[Sequence[GateSpec]] = None,
    *,
    by: str = "system",
    reason: str = "new revision",
) -> PlanResult:
    """Reset an existing gate plan back to pending for a new revision.

    All gates are restored to STATUS_PENDING with fresh empty artifacts,
    clearing previous verdicts so the new revision is fully and independently
    audited across all gates. Previous decisions remain permanently recorded
    in manifest history, the feedback ledger, and the execution trace.
    """
    raw = manifest.get("gates")
    if not isinstance(raw, list) or not raw:
        return PlanResult(created=False, count=0, gate_ids=())

    if specs is None:
        records = load_records(manifest)
        specs = tuple(
            GateSpec(
                gate_id=r.gate_id,
                gate_type=r.gate_type,
                name=r.name,
                targets=r.targets,
                required=r.required,
                authority=r.authority,
            )
            for r in records
        )

    records = plan_from_specs(specs)
    manifest["gates"] = [dict(record) for record in records]
    append_history(
        manifest,
        by=by,
        why=f"gate plan reset ({reason}): {len(records)} gate(s) restored to pending",
    )
    return PlanResult(
        created=True,
        count=len(records),
        gate_ids=tuple(str(record["gate_id"]) for record in records),
    )


# --------------------------------------------------------------------------- #
# walking the plan
# --------------------------------------------------------------------------- #

def active_record(records: Sequence[GateRecord]) -> Optional[GateRecord]:
    """The first non-terminal gate — the only gate that may be acted on now.

    Fail-closed dual control: if ANY gate in the plan has failed, the plan is
    halted; no downstream gate may ever be returned as active while a prior
    gate has failed.
    """
    for record in records:
        if record.status == STATUS_FAILED:
            return None
        if not record.terminal:
            return record
    return None


def is_complete(records: Sequence[GateRecord]) -> bool:
    """True when every gate reached a terminal status *and* none failed."""
    if not records:
        return False
    return all(record.terminal for record in records) and not any(
        record.status == STATUS_FAILED for record in records
    )


def checklist(records: Sequence[GateRecord]) -> list[Dict[str, Any]]:
    """Compact ordered checklist for the radar/API (one entry per gate)."""
    return [record.to_view() for record in records]


def progress(records: Sequence[GateRecord]) -> Dict[str, Any]:
    """Gate-plan counters + completion percentage (0–100)."""
    total = len(records)
    if total == 0:
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "pending": 0,
            "percent": 0.0,
            "active_gate": None,
            "active_gate_type": None,
            "complete": False,
        }
    passed = sum(1 for r in records if r.status == STATUS_PASSED)
    failed = sum(1 for r in records if r.status == STATUS_FAILED)
    skipped = sum(1 for r in records if r.status == STATUS_SKIPPED)
    pending = total - passed - failed - skipped
    active = active_record(records)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pending": pending,
        "percent": round((passed + skipped) / total * 100.0, 1),
        "active_gate": active.gate_id if active else None,
        "active_gate_type": active.gate_type if active else None,
        "complete": is_complete(records),
    }


def summary_line(records: Sequence[GateRecord]) -> str:
    """One-line checklist for holds and history notes, e.g. ``Gates: QA OK | Lead ..``"""
    parts = []
    for record in records:
        label = record.name.split(" ")[0].upper()
        mark = {
            STATUS_PASSED: "OK",
            STATUS_FAILED: "REJ",
            STATUS_SKIPPED: "SKIP",
            STATUS_RUNNING: "...",
            STATUS_PENDING: "..",
        }.get(record.status, "?")
        parts.append(f"{label} {mark}")
    return "Gates: " + " | ".join(parts) if parts else "Gates: none"


# --------------------------------------------------------------------------- #
# element-level target resolution & TOCTOU binding
# --------------------------------------------------------------------------- #

_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".yaml",
        ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".py", ".js",
        ".ts", ".tsx", ".jsx", ".css", ".scss", ".sh", ".bash", ".sql", ".log",
        ".env", ".rst", ".tex", ".java", ".go", ".rs", ".rb", ".php", ".c",
        ".h", ".cpp", ".hpp",
    }
)
MAX_TEXT_FILE_BYTES = 512 * 1024  # per-file read cap for evaluators
MAX_TEXT_TOTAL_BYTES = 4 * 1024 * 1024  # per-context total cap


def resolve_targets(
    capsule_path: str | os.PathLike[str],
    targets: Sequence[str],
    *,
    max_files: int = MAX_TARGET_FILES,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Expand a gate's targets to relative file paths inside the capsule.

    Returns ``(files, missing_patterns)``. Exact paths must exist; globs may
    legitimately match nothing (recorded as a missing pattern for the
    evaluator to judge — never silently rounded to "fine"). Symlinks that
    resolve outside the capsule are refused by the hashing layer, not here.
    """
    capsule = Path(capsule_path).resolve()
    if not capsule.is_dir():
        raise GateTargetError(f"capsule directory does not exist: {capsule}")

    found: list[str] = []
    missing: list[str] = []
    surfaced: set[str] = set()

    def _append(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved != capsule and capsule not in resolved.parents:
            return  # escaped (symlink) — never a lawful target
        rel = path.relative_to(capsule).as_posix()
        if rel not in surfaced:
            surfaced.add(rel)
            found.append(rel)

    for pattern in targets:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        clean = pattern.strip()
        if any(ch in clean for ch in "*?["):
            matched = False
            try:
                for candidate in sorted(capsule.glob(clean)):
                    if candidate.is_file():
                        _append(candidate)
                        matched = True
            except OSError:
                matched = False
            if not matched:
                missing.append(clean)
            continue
        candidate = capsule / clean
        if candidate.is_file():
            _append(candidate)
        else:
            missing.append(clean)
        if len(found) >= max_files:
            break

    return tuple(found[:max_files]), tuple(missing)


def _fallback_artifacts(manifest: Mapping[str, Any]) -> Tuple[str, ...]:
    """Legacy/no-target fallback: the capsule's bound artifact, else copy.md."""
    metadata = manifest.get("metadata")
    if isinstance(metadata, Mapping):
        recorded = metadata.get("artifact_path")
        if isinstance(recorded, str) and recorded.strip():
            return (recorded.strip(),)
    return (ARTIFACT_FILE,)


def bind_targets(
    manifest: MutableMapping[str, Any],
    capsule_path: str | os.PathLike[str],
    record: GateRecord,
    *,
    opened_at: str,
) -> GateRecord:
    """Open a gate: SHA-256 bind every resolved target into its record.

    The returned record replaces the on-disk one (caller persists). No targets
    declared → the gate is bound to the capsule's primary artifact when one
    exists; if nothing exists, the gate opens with an empty binding and the
    evaluator/verifier judges that honestly (a human gate still relies on the
    capsule-level TOCTOU binding enforced by ``manifest.transition``).
    """
    files, missing = resolve_targets(capsule_path, record.targets)
    artifacts: Dict[str, str] = {}
    total_bytes = 0
    for relative in files:
        if len(artifacts) >= MAX_TARGET_FILES:
            break
        try:
            size = Path(capsule_path, relative).stat().st_size
        except OSError:
            continue
        if total_bytes + size > MAX_TARGET_BYTES:
            break
        total_bytes += size
        artifacts[relative] = artifact_sha256(capsule_path, relative)

    if not artifacts:
        for fallback in _fallback_artifacts(manifest):
            try:
                artifacts[fallback] = artifact_sha256(capsule_path, fallback)
                break
            except (OSError, SecurityError):
                continue

    evidence = dict(record.evidence)
    if missing:
        evidence["missing_targets"] = list(missing)
    evidence["bound_bytes"] = total_bytes

    return GateRecord(
        gate_id=record.gate_id,
        gate_type=record.gate_type,
        name=record.name,
        targets=record.targets,
        required=record.required,
        status=STATUS_RUNNING if record.automated else record.status,
        authority=record.authority,
        opened_at=opened_at,
        closed_at=record.closed_at,
        decided_by=record.decided_by,
        reason=record.reason,
        artifacts=artifacts,
        evidence=evidence,
    )


def verify_targets(
    manifest: Mapping[str, Any],
    capsule_path: str | os.PathLike[str],
    record: GateRecord,
) -> None:
    """Re-hash every bound target; refuse on any mismatch or disappearance.

    Raises:
        GateTargetError: a bound artifact is missing, unreadable, or no longer
            byte-identical to the digest taken when the gate opened (TOCTOU).
    """
    if not record.opened_at:
        raise GateTargetError(
            f"gate {record.gate_id!r}: gate has not been opened yet — target "
            "artifacts were never bound for review (fail closed)"
        )
    if not record.artifacts:
        targets_to_check = record.targets or _fallback_artifacts(manifest)
        files, _ = resolve_targets(capsule_path, targets_to_check)
        if files:
            raise GateTargetError(
                f"gate {record.gate_id!r}: targets exist on disk ({', '.join(files)}) "
                "but were not bound when the gate opened — refusing (TOCTOU)"
            )
    for relative, expected in record.artifacts.items():
        try:
            actual = artifact_sha256(capsule_path, relative)
        except SecurityError as exc:
            raise GateTargetError(
                f"gate {record.gate_id!r}: target {relative!r} cannot be "
                f"verified: {exc}"
            ) from exc
        if actual != expected:
            raise GateTargetError(
                f"gate {record.gate_id!r}: target {relative!r} was altered "
                f"after the gate opened (bound {expected[:12]}..., found "
                f"{actual[:12]}...) — refusing (TOCTOU)"
            )


# --------------------------------------------------------------------------- #
# decisions
# --------------------------------------------------------------------------- #

def _replace_record(
    manifest: MutableMapping[str, Any], updated: GateRecord
) -> None:
    raw = manifest.get("gates")
    if not isinstance(raw, list):
        raise GateSchemaError("'gates' must be a list of gate objects")
    for index, entry in enumerate(raw):
        if isinstance(entry, Mapping) and entry.get("gate_id") == updated.gate_id:
            raw[index] = updated.to_manifest()
            return
    raise GateSchemaError(f"gate {updated.gate_id!r} vanished from the plan")


def _bounded_note(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    return text.strip()[:limit]


def open_gate(
    manifest: MutableMapping[str, Any],
    gate_id: str,
    capsule_path: str | os.PathLike[str],
    *,
    by: str = "system",
) -> GateRecord:
    """Bind a gate's targets and mark it opened (idempotent).

    Ordering is enforced: the gate must be the active one. Re-opening an
    already-opened gate is a no-op, so a crashed pulse can always retry.
    """
    records = load_records(manifest)
    active = active_record(records)
    if active is None:
        raise GateOrderError("no active gate: the plan is complete")
    if active.gate_id != gate_id:
        raise GateOrderError(
            f"gate {gate_id!r} is not the active gate (active: "
            f"{active.gate_id!r}) — fail-closed dual control"
        )
    if active.opened_at:
        return active

    from capsule_engine.manifest import now_iso

    updated = bind_targets(
        manifest, capsule_path, active, opened_at=now_iso()
    )
    _replace_record(manifest, updated)
    append_history(
        manifest,
        by=by,
        why=(
            f"gate {updated.gate_id} ({updated.gate_type}) opened: "
            f"{len(updated.artifacts)} target(s) bound"
            + (
                f"; unresolved: {', '.join(updated.evidence.get('missing_targets', []))}"
                if updated.evidence.get("missing_targets")
                else ""
            )
        ),
    )
    return updated


def decide(
    manifest: MutableMapping[str, Any],
    gate_id: str,
    decision: str,
    *,
    by: str,
    reason: str,
    capsule_path: Optional[str | os.PathLike[str]] = None,
    verify: bool = True,
    evidence: Optional[Mapping[str, Any]] = None,
) -> GateRecord:
    """Record a gate decision (approve / reject / skip) with full audit.

    Fail-closed rules, in order:

    1. the gate must exist and be the **active** gate (no jumping ahead);
    2. a rejection requires a non-empty reason (the reviewer's instruction);
    3. only optional gates may be skipped;
    4. an approval of a gate with bound targets re-hashes them first when
       ``capsule_path`` is given (the per-element TOCTOU guard);
    5. nothing is mutated when any rule refuses.

    Returns the updated record. The caller persists and, on rejection, is
    responsible for dropping the flow to ``needs_revision`` and appending the
    feedback ledger entry.
    """
    decision_key = decision.strip().lower()
    if decision_key not in ("approve", "reject", "skip"):
        raise GateConfigError(
            f"unknown gate decision {decision!r} (approve/reject/skip)"
        )

    records = load_records(manifest)
    active = active_record(records)
    if active is None:
        raise GateOrderError("no active gate: the plan is already complete")
    if active.gate_id != gate_id:
        raise GateOrderError(
            f"gate {gate_id!r} is not the active gate (active: "
            f"{active.gate_id!r}) — fail-closed dual control"
        )

    from capsule_engine.manifest import now_iso

    note = _bounded_note(str(reason))
    if decision_key == "reject" and not note:
        raise GateConfigError("a gate rejection requires a non-empty reason")
    if decision_key == "skip" and active.required:
        raise GateConfigError(
            f"gate {gate_id!r} is required and cannot be skipped"
        )

    if decision_key == "approve":
        if not active.opened_at:
            raise GateTargetError(
                f"gate {gate_id!r} has not been opened yet — target artifacts were "
                "never bound for review (fail closed)"
            )
        if verify and capsule_path is not None:
            verify_targets(manifest, capsule_path, active)
        status = STATUS_PASSED
    elif decision_key == "skip":
        status = STATUS_SKIPPED
    else:
        status = STATUS_FAILED

    merged_evidence: Dict[str, Any] = dict(active.evidence)
    if evidence:
        merged_evidence.update(dict(evidence))

    updated = GateRecord(
        gate_id=active.gate_id,
        gate_type=active.gate_type,
        name=active.name,
        targets=active.targets,
        required=active.required,
        status=status,
        authority=active.authority,
        opened_at=active.opened_at or now_iso(),
        closed_at=now_iso(),
        decided_by=str(by)[:120] or "unknown",
        reason=note or None,
        artifacts=dict(active.artifacts),
        evidence=merged_evidence,
    )
    _replace_record(manifest, updated)
    append_history(
        manifest,
        by=str(by),
        why=(
            f"gate {updated.gate_id} ({updated.gate_type}) {status}"
            + (f": {note}" if note else "")
            + f" — {summary_line(load_records(manifest))}"
        ),
    )
    return updated


# --------------------------------------------------------------------------- #
# automated evaluators (closed verbs, open tools)
# --------------------------------------------------------------------------- #

Evaluator = Callable[[EvaluationContext], GateVerdict]

_EVALUATORS: Dict[str, Evaluator] = {}


def register_evaluator(gate_type: str, evaluator: Evaluator) -> None:
    """Register a deterministic evaluator for an automated gate type.

    Refuses human typologies (they are decided by principals, not code) and
    requires the gate type to exist in the closed vocabulary.
    """
    if gate_type not in GATE_TYPES:
        raise GateConfigError(f"cannot register unknown gate type {gate_type!r}")
    if gate_type in HUMAN_GATE_TYPES:
        raise GateConfigError(
            f"{gate_type!r} is a human gate type; it is decided by a "
            "capability holder, never by an evaluator"
        )
    if not callable(evaluator):
        raise GateConfigError("evaluator must be callable")
    _EVALUATORS[gate_type] = evaluator


def unregister_evaluator(gate_type: str) -> None:
    """Remove a previously registered evaluator (tests, branch teardown)."""
    _EVALUATORS.pop(gate_type, None)


def registered_evaluators() -> Tuple[str, ...]:
    """Automated gate types that currently have an evaluator."""
    return tuple(sorted(_EVALUATORS))


def _read_context(
    capsule_path: str,
    manifest: Mapping[str, Any],
    record: GateRecord,
) -> EvaluationContext:
    """Read the gate's bound/resolved targets into a bounded context."""
    files, missing = resolve_targets(capsule_path, record.targets)
    if not files and not record.targets:
        files, _ = resolve_targets(capsule_path, _fallback_artifacts(manifest))
        missing = ()
    texts: Dict[str, str] = {}
    binary_files: list[str] = []
    remaining = MAX_TEXT_TOTAL_BYTES
    for relative in files:
        suffix = Path(relative).suffix.lower()
        if suffix and suffix not in _TEXT_SUFFIXES:
            binary_files.append(relative)
            continue
        target = Path(capsule_path) / relative
        try:
            size = target.stat().st_size
        except OSError:
            continue
        if size > MAX_TEXT_FILE_BYTES or size > remaining:
            binary_files.append(relative)
            continue
        try:
            texts[relative] = target.read_text(encoding="utf-8")
            remaining -= size
        except (OSError, UnicodeDecodeError):
            binary_files.append(relative)
    return EvaluationContext(
        capsule_path=capsule_path,
        manifest=manifest,
        gate=record,
        files=tuple(files),
        texts=texts,
        binary_files=tuple(binary_files),
        missing_targets=tuple(missing),
    )


def _evaluate_qa_review(context: EvaluationContext) -> GateVerdict:
    """Automated QA: structural sanity of every reviewable text artifact.

    Deterministic and offline — no gear, no network, no tokens. Checks:
    non-empty content, minimum substance, placeholder markers, and the
    agency's closed banned-buzzword vocabulary (:mod:`capsule_engine.editorial`,
    shared with the marketing reviewer's prompt so a text that QA rejects here
    also fails the LLM reviewer's contract).
    """
    if not context.files:
        return GateVerdict(
            passed=False,
            reason=(
                "no reviewable artifact: none of the declared targets exist "
                f"({', '.join(context.gate.targets) or 'primary artifact'})"
            ),
        )
    if not context.texts:
        return GateVerdict(
            passed=False,
            reason=(
                "no text artifact to review — all declared targets are binary "
                f"({', '.join(context.binary_files)})"
            ),
        )

    placeholders = ("lorem ipsum", "todo:", "tbd", "xxx", "<insert", "placeholder")
    failures: list[str] = []
    checked: Dict[str, int] = {}
    for relative, text in sorted(context.texts.items()):
        checked[relative] = len(text)
        lowered = text.lower()
        if len(text.strip()) < 40:
            failures.append(f"{relative}: content shorter than 40 characters")
            continue
        for marker in placeholders:
            if marker in lowered:
                failures.append(f"{relative}: unreviewed placeholder {marker!r}")
        for phrase in BANNED_PHRASES:
            if phrase in lowered:
                failures.append(f"{relative}: banned phrase {phrase!r}")
        words = set(re.findall(r"[a-z0-9']+", lowered))
        hits = sorted(words & BANNED_BUZZWORDS)
        if hits:
            failures.append(f"{relative}: banned buzzword(s) {', '.join(hits)}")

    # A declared target that matched nothing is recorded as evidence, not as a
    # failure: targets legitimately combine several artifact layouts
    # (``deliverables/**/*`` *and* the legacy ``copy.md``) and only one will
    # exist. The gate judges the content it found; a gate whose targets
    # resolve to nothing at all already failed above.
    evidence: Dict[str, Any] = {
        "checked": checked,
        "failures": failures[:20],
        "missing_targets": list(context.missing_targets),
    }
    if failures:
        return GateVerdict(
            passed=False,
            reason="QA rejected: " + "; ".join(failures[:5]),
            evidence=evidence,
        )
    note = (
        "; unresolved declared target(s): " + ", ".join(context.missing_targets)
        if context.missing_targets
        else ""
    )
    return GateVerdict(
        passed=True,
        reason=f"QA passed: {len(context.texts)} artifact(s) reviewed{note}",
        evidence=evidence,
    )


_SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("assigned secret", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9\-._/+]{12,}"
    )),
)
_DANGEROUS_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("shell evaluation", re.compile(r"(?i)\beval\s*\(")),
    ("dynamic execution", re.compile(r"(?i)\bexec\s*\(")),
    ("shell=True", re.compile(r"shell\s*=\s*True")),
    ("absolute home path", re.compile(r"(?<![A-Za-z0-9])/(Users|home)/[A-Za-z0-9._-]+")),
    ("path traversal", re.compile(r"\.\./\.\./")),
)


def _evaluate_security_audit(context: EvaluationContext) -> GateVerdict:
    """Automated security audit: secrets, dangerous constructs, leaks.

    Scans every reviewable text target. Any finding fails the gate closed —
    the finding is named (pattern class + file + line) without ever echoing
    the secret value into the manifest.
    """
    if not context.files:
        return GateVerdict(
            passed=False,
            reason="security audit: no declared target exists to scan",
        )
    findings: list[Dict[str, Any]] = []
    scanned: Dict[str, int] = {}
    for relative, text in sorted(context.texts.items()):
        scanned[relative] = len(text)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for label, pattern in _SECRET_PATTERNS + _DANGEROUS_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {"path": relative, "line": line_no, "finding": label}
                    )
                if len(findings) >= 50:
                    break
            if len(findings) >= 50:
                break
        if len(findings) >= 50:
            break

    evidence = {
        "scanned": scanned,
        "findings": findings,
        "missing_targets": list(context.missing_targets),
    }
    if findings:
        first = findings[0]
        return GateVerdict(
            passed=False,
            reason=(
                f"security audit failed: {len(findings)} finding(s), first at "
                f"{first['path']}:{first['line']} ({first['finding']})"
            ),
            evidence=evidence,
        )
    return GateVerdict(
        passed=True,
        reason=f"security audit passed: {len(scanned)} artifact(s) scanned, no findings",
        evidence=evidence,
    )


register_evaluator("qa_review", _evaluate_qa_review)
register_evaluator("security_audit", _evaluate_security_audit)


def evaluate(
    gate_type: str,
    context: EvaluationContext,
) -> GateVerdict:
    """Run the evaluator for an automated gate type (fail closed).

    Raises:
        GateEvaluationError: the type is unknown or human (never evaluated by
            code), or no evaluator is registered for it.
    """
    if gate_type not in GATE_TYPES:
        raise GateEvaluationError(f"unknown gate type {gate_type!r}")
    if gate_type in HUMAN_GATE_TYPES:
        raise GateEvaluationError(
            f"gate type {gate_type!r} is human-decided; no evaluator applies"
        )
    evaluator = _EVALUATORS.get(gate_type)
    if evaluator is None:
        raise GateEvaluationError(
            f"no evaluator registered for automated gate type {gate_type!r}"
        )
    try:
        return evaluator(context)
    except GateEvaluationError:
        raise
    except Exception as exc:  # a buggy evaluator must not corrupt the plan
        raise GateEvaluationError(
            f"evaluator for {gate_type!r} raised {type(exc).__name__}: {exc}"
        ) from exc


def build_context(
    capsule_path: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    record: GateRecord,
) -> EvaluationContext:
    """Public wrapper around the bounded context reader (for callers/tests)."""
    return _read_context(str(capsule_path), manifest, record)


def evaluate_gate(
    capsule_path: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    record: GateRecord,
) -> GateVerdict:
    """Read a gate's targets and evaluate it (convenience for callers)."""
    return evaluate(record.gate_type, build_context(capsule_path, manifest, record))


