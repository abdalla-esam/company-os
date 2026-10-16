#!/usr/bin/env python3
"""governance.py — the dynamic autonomy ladder (local ``governance.json``).

The autonomy ladder decides *how much human supervision a business domain has
earned*. It is deliberately tiny, pure standard library, and fail-closed:

- **L0 (novice)** — 100% human audit: every capsule stops at
  ``pending_approval`` and waits for a person;
- **L1 (apprentice)** — still human-audited (the ladder only skips the gate at
  L2+, so an intermediate level never silently widens autonomy);
- **L2 (expert) / L3 (sovereign)** — skips ``pending_approval`` and routes
  straight to ``approved`` (the Governor worker verifies the artifact hash
  first), *except* for a random 1-in-50 draw (:data:`DEFAULT_AUDIT_RATE`,
  2%) that forces the capsule back through human audit;
- **unknown level / unknown domain / missing or corrupt file** — L0, fail
  closed.

The decision is drawn once, when a capsule enters ``pending_approval``, and
recorded in ``metadata.autonomy`` so routing is deterministic afterwards (a
re-route never re-rolls the dice). If a human rejects a gated capsule (moves it
back to ``needs_revision``), :func:`demote` instantly drops the domain to L0 —
trust is lost immediately, regained deliberately.

File layout (``governance.json``, all fields optional-safe)::

    {
      "version": 1,
      "updated_at": "2026-10-04T00:00:00+00:00",
      "domains": {
        "marketing": {"level": "L2", "updated_at": "...", "demotions": 1}
      }
    }

Reads never raise (missing/malformed input degrades to L0 with a log
warning); writes are atomic temp-file + fsync + ``os.replace`` and raise on
I/O failure so an operator never believes a demotion was recorded when it
was not. Use ``AGENCY_GOVERNANCE_FILE`` to point at a non-default file.

Standard library only: ``json``, ``logging``, ``os``, ``random``, ``dataclasses``,
``datetime``, ``pathlib``, ``typing``. Python 3.10+.
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from capsule_engine.exceptions import ManifestSchemaError
from capsule_engine.manifest import (
    State,
    _metadata_for_write,
    now_iso,
)
from shared_kernel.atomic_io import atomic_json, update_json

logger = logging.getLogger("capsule_engine.governance")

GOVERNANCE_NAME = "governance.json"
GOVERNANCE_ENV = "AGENCY_GOVERNANCE_FILE"
try:
    from shared_kernel.paths import REPO_ROOT
    DEFAULT_GOVERNANCE_FILE = REPO_ROOT / "config" / GOVERNANCE_NAME
except Exception:
    DEFAULT_GOVERNANCE_FILE = Path(__file__).resolve().parents[2] / "config" / GOVERNANCE_NAME
MAX_GOVERNANCE_BYTES = 256 * 1024  # a policy file, not a database

# The ladder. Only EXPERT_LEVELS skip the human gate; everything else — L0,
# L1, unknown, missing — is audited 100% (fail closed).
LEVEL_NOVICE = "L0"
LEVEL_APPRENTICE = "L1"
LEVEL_EXPERT = "L2"
LEVEL_SOVEREIGN = "L3"
KNOWN_LEVELS: tuple[str, ...] = (
    LEVEL_NOVICE,
    LEVEL_APPRENTICE,
    LEVEL_EXPERT,
    LEVEL_SOVEREIGN,
)
EXPERT_LEVELS: frozenset[str] = frozenset({LEVEL_EXPERT, LEVEL_SOVEREIGN})
LEVEL_LABELS: dict[str, str] = {
    LEVEL_NOVICE: "novice",
    LEVEL_APPRENTICE: "apprentice",
    LEVEL_EXPERT: "expert",
    LEVEL_SOVEREIGN: "sovereign",
}

DEFAULT_LEVEL = LEVEL_NOVICE
DEFAULT_DOMAIN = "unclassified"
DEFAULT_AUDIT_RATE = 1.0 / 50.0  # 2% random audit for expert domains


# --------------------------------------------------------------------------- #
# decisions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AutonomyDecision:
    """One autonomy verdict for one domain (immutable, JSON-friendly)."""

    domain: str
    level: str
    requires_human_approval: bool
    audit_sampled: bool
    reason: str

    @property
    def label(self) -> str:
        """Human-readable level name (``expert``, ``novice``, ...)."""
        return LEVEL_LABELS.get(self.level, "unknown")

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (stored in ``metadata.autonomy``)."""
        return {
            "domain": self.domain,
            "level": self.level,
            "label": self.label,
            "requires_human_approval": self.requires_human_approval,
            "audit_sampled": self.audit_sampled,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "AutonomyDecision | None":
        """Rebuild a recorded decision; ``None`` for anything unusable."""
        if not isinstance(payload, dict):
            return None
        level = payload.get("level")
        level = level if isinstance(level, str) and level in KNOWN_LEVELS else DEFAULT_LEVEL
        domain = payload.get("domain")
        domain = domain.strip() if isinstance(domain, str) and domain.strip() else DEFAULT_DOMAIN
        requires = payload.get("requires_human_approval")
        audit = payload.get("audit_sampled")
        audit = audit if isinstance(audit, bool) else False
        if not isinstance(requires, bool):
            # Missing/derived: expert levels skip the gate unless audit-sampled.
            requires = level not in EXPERT_LEVELS or audit
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason.strip() else "recorded autonomy decision"
        return cls(
            domain=domain,
            level=level,
            requires_human_approval=requires,
            audit_sampled=audit,
            reason=reason,
        )


@dataclass(frozen=True)
class DemotionResult:
    """Outcome of :func:`demote` / :func:`apply_rejection`."""

    domain: str
    previous_level: str
    level: str = DEFAULT_LEVEL
    changed: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "previous_level": self.previous_level,
            "level": self.level,
            "changed": self.changed,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# file resolution + I/O
# --------------------------------------------------------------------------- #

def governance_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the governance file: explicit > ``AGENCY_GOVERNANCE_FILE`` > default."""
    if path is not None:
        return Path(path).resolve()
    env = os.environ.get(GOVERNANCE_ENV, "").strip()
    if env:
        return Path(env).resolve()
    try:
        from shared_kernel.paths import REPO_ROOT
        repo_root = REPO_ROOT
    except Exception:
        repo_root = Path(__file__).resolve().parents[2]
    cfg_default = repo_root / "config" / GOVERNANCE_NAME
    if cfg_default.is_file():
        return cfg_default
    return DEFAULT_GOVERNANCE_FILE


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "updated_at": None, "domains": {}}


class GovernanceCorruptError(ValueError):
    """Raised when governance.json is corrupt or unreadable, refusing to write."""
    pass


def load_governance(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load and normalize ``governance.json``.

    A missing file is normal (every domain defaults to L0). An unreadable,
    oversized, malformed, or non-object file logs a warning and degrades to
    the empty state — i.e. to 100% human audit, which is the fail-closed
    direction.
    An unknown or higher schema version raises SchemaVersionError (fail closed).
    """
    from shared_kernel.schema_version import (
        KIND_GOVERNANCE,
        check_schema_version,
    )

    target = governance_path(path)
    if not target.is_file():
        return _empty_state()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("governance: %s unreadable (%s) — defaulting to L0", target, exc)
        return _empty_state()
    if len(raw.encode("utf-8")) > MAX_GOVERNANCE_BYTES:
        logger.warning(
            "governance: %s exceeds %d bytes — defaulting to L0",
            target,
            MAX_GOVERNANCE_BYTES,
        )
        return _empty_state()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "governance: %s invalid JSON at line %d column %d (%s) — defaulting to L0",
            target,
            exc.lineno,
            exc.colno,
            exc.msg,
        )
        return _empty_state()
    if not isinstance(decoded, dict):
        logger.warning(
            "governance: %s must contain a JSON object, got %s — defaulting to L0",
            target,
            type(decoded).__name__,
        )
        return _empty_state()

    # Fail closed on unknown or higher schema version
    check_schema_version(KIND_GOVERNANCE, decoded, target)

    state = dict(decoded)
    state.setdefault("version", 1)
    state.setdefault("updated_at", None)
    domains = state.get("domains")
    if not isinstance(domains, dict):
        if domains is not None:
            logger.warning(
                "governance: %s 'domains' must be an object — ignoring it", target
            )
        state["domains"] = {}
    else:
        state["domains"] = {
            name: dict(entry)
            for name, entry in domains.items()
            if isinstance(name, str) and isinstance(entry, dict)
        }
    return state


def save_governance(
    state: Mapping[str, Any], path: str | os.PathLike[str] | None = None
) -> Path:
    """Atomically persist the governance state; raises ``OSError`` on failure."""
    from shared_kernel.schema_version import (
        KIND_GOVERNANCE,
        stamp_schema_version,
    )

    target = governance_path(path)
    state_dict = dict(state)
    stamp_schema_version(KIND_GOVERNANCE, state_dict)
    size = len((json.dumps(state_dict, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    if size > MAX_GOVERNANCE_BYTES:
        raise ValueError(
            f"governance file too large to save: {size} bytes > {MAX_GOVERNANCE_BYTES}"
        )
    return atomic_json(target, state_dict, ensure_ascii=False, indent=2)


def _update_state(
    target: Path,
    path: str | os.PathLike[str] | None,
    apply: Any,
) -> Any:
    """Lock-serialized read-modify-write of governance.json.

    The read-check-write runs under the file's sidecar ``.lock``, so
    concurrent demotions/set_levels for different domains never lose an
    update.

    If the file exists and is corrupt, unreadable, or carries an unsupported
    schema version, write is REFUSED (GovernanceCorruptError / SchemaVersionError)
    leaving the file untouched. Rebuilding from empty on corruption is strictly
    forbidden.
    """
    from shared_kernel.atomic_io import sidecar_lock
    from shared_kernel.schema_version import (
        KIND_GOVERNANCE,
        check_schema_version,
        stamp_schema_version,
    )

    with sidecar_lock(target, purpose="governance:_update_state"):
        if not target.is_file():
            state = _empty_state()
        else:
            try:
                raw = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise GovernanceCorruptError(
                    f"governance file at {target} is unreadable: {exc}"
                ) from exc

            if not raw.strip():
                state = _empty_state()
            else:
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise GovernanceCorruptError(
                        f"governance file at {target} contains invalid JSON: {exc}"
                    ) from exc

                if not isinstance(decoded, dict):
                    raise GovernanceCorruptError(
                        f"governance file at {target} must be a JSON object, got {type(decoded).__name__}"
                    )

                # Check schema version: raises SchemaVersionError if higher/unknown
                check_schema_version(KIND_GOVERNANCE, decoded, target)
                state = decoded

        new_state = apply(state)
        if new_state is None:
            return state

        stamp_schema_version(KIND_GOVERNANCE, new_state)
        save_governance(new_state, target)
        return new_state


# --------------------------------------------------------------------------- #
# reading the ladder
# --------------------------------------------------------------------------- #

def _entry(state: Mapping[str, Any], domain: str) -> dict[str, Any] | None:
    domains = state.get("domains")
    if not isinstance(domains, dict):
        return None
    entry = domains.get(domain)
    return entry if isinstance(entry, dict) else None


def level_for(
    domain: str,
    *,
    state: Mapping[str, Any] | None = None,
    path: str | os.PathLike[str] | None = None,
) -> str:
    """Return the recorded level for ``domain`` — L0 for anything unusable."""
    domain = _normalize_domain(domain)
    active = load_governance(path) if state is None else state
    entry = _entry(active, domain)
    if entry is None:
        return DEFAULT_LEVEL
    level = entry.get("level")
    if isinstance(level, str) and level in KNOWN_LEVELS:
        return level
    if level is not None:
        logger.warning(
            "governance: domain %r has unknown level %r — treating as %s",
            domain,
            level,
            DEFAULT_LEVEL,
        )
    return DEFAULT_LEVEL


def current_level(
    domain: str, *, path: str | os.PathLike[str] | None = None
) -> str:
    """Convenience: :func:`level_for` straight from disk (or the env file)."""
    return level_for(domain, path=path)


def _normalize_domain(domain: object) -> str:
    if isinstance(domain, str) and domain.strip():
        return domain.strip()
    return DEFAULT_DOMAIN


def domain_of(manifest: Mapping[str, Any]) -> str:
    """Best-effort domain lookup from a capsule manifest.

    Order: ``metadata.domain`` > ``routing_context.domain`` >
    ``routing_context.sector`` > ``metadata.sector`` > ``"unclassified"``.
    A missing domain is not an error — it is L0 by default (fail closed).
    """
    if not isinstance(manifest, Mapping):
        return DEFAULT_DOMAIN
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    routing = manifest.get("routing_context")
    routing = routing if isinstance(routing, dict) else {}
    for candidate in (
        metadata.get("domain"),
        routing.get("domain"),
        routing.get("sector"),
        metadata.get("sector"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return DEFAULT_DOMAIN


def evaluate_autonomy(
    domain: str,
    *,
    state: Mapping[str, Any] | None = None,
    sample: float | None = None,
    path: str | os.PathLike[str] | None = None,
) -> AutonomyDecision:
    """Evaluate the ladder for ``domain`` (pure, fail-closed).

    Args:
        domain: business domain / sector name (empty -> ``"unclassified"``).
        state: optional preloaded governance state (avoids a file read).
        sample: deterministic audit draw in ``[0, 1)``; defaults to
            ``random.random()``. Values below :data:`DEFAULT_AUDIT_RATE`
            (1-in-50) force human audit for expert domains — tests inject it.
        path: governance file override (else the env/default path).

    Returns:
        An :class:`AutonomyDecision`; L2/L3 skip the gate unless the audit
        sample hits (2%), everything else requires human approval.
    """
    domain = _normalize_domain(domain)
    active = load_governance(path) if state is None else state
    entry = _entry(active, domain)
    raw_level = entry.get("level") if entry is not None else None
    level = raw_level if isinstance(raw_level, str) and raw_level in KNOWN_LEVELS else DEFAULT_LEVEL

    if level not in EXPERT_LEVELS:
        if entry is not None and raw_level is not None and raw_level not in KNOWN_LEVELS:
            reason = (
                f"unknown level {raw_level!r} for domain {domain!r} — defaulting to "
                f"{DEFAULT_LEVEL} (fail closed)"
            )
        elif entry is None:
            reason = f"no governance entry for domain {domain!r} — audit at {DEFAULT_LEVEL}"
        else:
            reason = f"domain {domain!r} is {level} ({LEVEL_LABELS[level]}) — 100% human audit"
        return AutonomyDecision(
            domain=domain,
            level=level,
            requires_human_approval=True,
            audit_sampled=False,
            reason=reason,
        )

    draw = random.random() if sample is None else float(sample)
    audited = draw < DEFAULT_AUDIT_RATE
    if audited:
        reason = (
            f"domain {domain!r} is {level} ({LEVEL_LABELS[level]}) but hit the "
            f"1-in-{int(round(1.0 / DEFAULT_AUDIT_RATE))} random audit — human review"
        )
    else:
        reason = (
            f"domain {domain!r} is {level} ({LEVEL_LABELS[level]}) — autonomy "
            "auto-approval (2% audit rate)"
        )
    return AutonomyDecision(
        domain=domain,
        level=level,
        requires_human_approval=audited,
        audit_sampled=audited,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# recording decisions on capsules
# --------------------------------------------------------------------------- #

def record_autonomy_decision(
    manifest: MutableMapping[str, Any],
    *,
    sample: float | None = None,
    path: str | os.PathLike[str] | None = None,
    by: str = "system",
) -> AutonomyDecision:
    """Draw and store the autonomy decision in ``metadata.autonomy``.

    Written when a capsule enters ``pending_approval``; the router then reads
    it instead of re-rolling, so the sampled audit outcome is stable.
    """
    domain = domain_of(manifest)
    decision = evaluate_autonomy(domain, sample=sample, path=path)
    metadata = _metadata_for_write(manifest, action="record the autonomy decision")
    payload = decision.as_dict()
    payload["decided_at"] = now_iso()
    payload["by"] = str(by)
    metadata["autonomy"] = payload
    return decision


def read_autonomy_decision(manifest: Mapping[str, Any]) -> AutonomyDecision | None:
    """Read the recorded decision; ``None`` when absent or unusable."""
    if not isinstance(manifest, Mapping):
        return None
    metadata = manifest.get("metadata")
    record = metadata.get("autonomy") if isinstance(metadata, dict) else None
    return AutonomyDecision.from_dict(record)


# --------------------------------------------------------------------------- #
# demotion (governance write path)
# --------------------------------------------------------------------------- #

def set_level(
    domain: str,
    level: str,
    *,
    path: str | os.PathLike[str] | None = None,
) -> str:
    """Explicitly set a domain's level (operator action); returns the level."""
    domain = _normalize_domain(domain)
    if level not in KNOWN_LEVELS:
        raise ValueError(
            f"unknown autonomy level {level!r} (known: {', '.join(KNOWN_LEVELS)})"
        )
    target = governance_path(path)

    def _apply(state):
        domains = state.setdefault("domains", {})
        entry = domains.get(domain)
        entry = dict(entry) if isinstance(entry, dict) else {}
        previous = level_for(domain, state=state)
        entry["level"] = level
        entry["updated_at"] = now_iso()
        if previous != level:
            entry["last_change"] = {
                "from": previous,
                "to": level,
                "at": now_iso(),
            }
        domains[domain] = entry
        state["updated_at"] = now_iso()
        return state

    _update_state(target, path, _apply)
    return level


def demote(
    domain: str,
    *,
    path: str | os.PathLike[str] | None = None,
    reason: str = "human rejection at the approval gate",
) -> DemotionResult:
    """Instantly drop ``domain`` to L0. Idempotent.

    A domain already at L0 is left untouched (no write, ``changed=False``), so
    repeated pulses over a rejected capsule never rewrite the policy file.
    Returns a :class:`DemotionResult`; write failures raise ``OSError``.
    """
    domain = _normalize_domain(domain)
    target = governance_path(path)
    outcome: dict[str, Any] = {}

    def _apply(state):
        previous = level_for(domain, state=state)
        if previous == DEFAULT_LEVEL:
            outcome["result"] = DemotionResult(
                domain=domain,
                previous_level=previous,
                changed=False,
                reason=f"domain {domain!r} is already {DEFAULT_LEVEL}",
            )
            return None  # no change — do not rewrite the policy file
        domains = state.setdefault("domains", {})
        entry = domains.get(domain)
        entry = dict(entry) if isinstance(entry, dict) else {}
        demotions = entry.get("demotions")
        demotions = demotions if isinstance(demotions, int) and not isinstance(demotions, bool) else 0
        entry.update(
            {
                "level": DEFAULT_LEVEL,
                "updated_at": now_iso(),
                "demoted_at": now_iso(),
                "demoted_from": previous,
                "demotions": demotions + 1,
                "last_demotion_reason": str(reason),
            }
        )
        domains[domain] = entry
        state["updated_at"] = now_iso()
        outcome["result"] = DemotionResult(
            domain=domain,
            previous_level=previous,
            level=DEFAULT_LEVEL,
            changed=True,
            reason=str(reason),
        )
        return state

    _update_state(target, path, _apply)
    result: DemotionResult = outcome["result"]
    if result.changed:
        logger.info(
            "governance: demoted domain %r from %s to %s (%s)",
            domain,
            result.previous_level,
            DEFAULT_LEVEL,
            reason,
        )
    return result


def apply_rejection(
    manifest: Mapping[str, Any],
    *,
    path: str | os.PathLike[str] | None = None,
) -> DemotionResult | None:
    """Demote the manifest's domain if it shows a gate rejection.

    A rejection is a history tail of ``pending_approval -> needs_revision``
    (an automated QA rejection comes from ``pending_qa`` and never demotes).
    Returns ``None`` when the capsule is not a gate rejection or the domain is
    already L0 (no change); the write failure propagates as ``OSError``.
    """
    if not isinstance(manifest, Mapping):
        return None
    history = manifest.get("history")
    tail = (
        history[-1]
        if isinstance(history, list) and history and isinstance(history[-1], dict)
        else None
    )
    if tail is None:
        return None
    if (
        tail.get("to_state") != State.NEEDS_REVISION.value
        or tail.get("from_state") != State.PENDING_APPROVAL.value
    ):
        return None
    result = demote(
        domain_of(manifest),
        path=path,
        reason=f"gate rejection recorded by {tail.get('by') or 'unknown'}",
    )
    return result if result.changed else None
