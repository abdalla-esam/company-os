#!/usr/bin/env python3
"""router.py — reads a capsule's manifest and decides the next action.

The router is the *brain, not the hands*: it never mutates a capsule, never
takes the write lock, and never executes a worker — it reads an **unlocked**
manifest and returns a :class:`RouteDecision` for the caller (the
pulse/dispatcher) to act on. Reading unlocked is safe because:

- writers persist via ``os.replace`` (atomic rename), so the router always
  observes a complete manifest — the old one or the new one, never a torn one;
- the decision is advisory; the worker still re-validates state *under the
  lock* in :meth:`capsule_engine.worker.BaseWorker.run`, closing any gap
  between deciding and executing (TOCTOU).

Fail-closed policy for the unknown:
- corrupt / schema-invalid manifest  -> ``QUARANTINE`` (human repair, never guess);
- capsule directory or manifest gone -> ``MISSING`` (informational, no action);
- state waiting on a human/gate      -> ``HOLD`` (with the reason);
- stale ``in_progress``              -> re-routed to the worker recorded in
  history (the lease died with its process); if no owner is recorded, ``HOLD``;
- ``pending_approval`` past its TTL  -> ``EXECUTE`` the TTL watchdog (freeze);
- ``pending_approval`` with a recorded expert-domain autonomy decision that
  was not audit-sampled (and whose domain is still expert) -> ``EXECUTE`` the
  governor (auto-approval; the governor still verifies the artifact hash);
- ``draft`` and ``needs_revision`` follow the capsule's own declared domain:
  a capsule is born in, and returns for rework to, the factory that owns it —
  whatever branch that is; without a declared domain the routing table
  decides (legacy capsules) and the router never guesses a domain;
- ``needs_revision`` past the revision cap (``spend.max_revisions_per_capsule``,
  counted in ``manifest['revision_count']``) is **never dispatched**: the
  decision is a HOLD naming the cap, and the over-cap ``failed`` position
  reports the same reason — the loop is a human's call from there on;
- anything else routable              -> ``EXECUTE`` with the registered worker's name.

Standard library only: ``dataclasses``, ``datetime``, ``enum``, ``pathlib``,
``typing``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping

from capsule_engine.exceptions import CapsuleError
from capsule_engine.governance import (
    EXPERT_LEVELS,
    current_level,
    read_autonomy_decision,
)
from capsule_engine.manifest import (
    MANIFEST_NAME,
    State,
    last_history_entry,
    load_manifest,
    parse_ts,
    revision_cap,
    revision_count,
    state_age_seconds,
)
from capsule_engine import task_profile

import logging

logger = logging.getLogger("capsule_engine.router")

# An in_progress capsule older than this is a dead lease (its worker crashed
# after the kernel released the flock): route it again instead of waiting
# forever. 30 minutes matches the dispatcher's lease TTL philosophy.
DEFAULT_STALE_AFTER = 1800.0

# Smart freeze: a capsule idling in pending_approval longer than this is
# auto-frozen to ``stalled`` so it stops silently aging on a human's desk.
DEFAULT_PENDING_APPROVAL_TTL = 48 * 60 * 60  # 48 hours

# Names of the governance-stage workers the router can summon. All must
# exist in the registry the caller passes to the pulse (the pulse wires them).
STALL_WATCHDOG_WORKER = "ttl-watchdog"
GOVERNOR_WORKER = "governor"
GATE_RUNNER_WORKER = "gate-runner"


class Action(str, Enum):
    """What the caller should do with a capsule after routing it."""

    EXECUTE = "execute"        # run the named worker against the capsule
    HOLD = "hold"              # lawful waiting state — take no action now
    QUARANTINE = "quarantine"  # manifest is broken — human repair required
    MISSING = "missing"        # capsule directory/manifest does not exist


@dataclass(frozen=True)
class RouteDecision:
    """The immutable outcome of routing one capsule."""

    capsule_path: str
    action: Action
    reason: str
    capsule_id: str | None = None
    state: State | None = None
    worker: str | None = None  # worker name; set only when action is EXECUTE
    profile_rejected: str | None = None  # B1: why a task card was ignored

    @property
    def should_execute(self) -> bool:
        """True only for a routable ``EXECUTE`` decision."""
        return self.action is Action.EXECUTE and self.worker is not None

    def as_dict(self) -> dict[str, str | None]:
        """JSON-friendly view for pulse logs."""
        return {
            "capsule_path": self.capsule_path,
            "capsule_id": self.capsule_id,
            "state": self.state.value if self.state else None,
            "action": self.action.value,
            "worker": self.worker,
            "reason": self.reason,
            "profile_rejected": self.profile_rejected,
        }


# Default routing table: which registered worker owns each routable state.
# Every name here must exist in the ``workers.WORKERS`` registry — the pulse
# reports an unknown name instead of guessing. States absent here are decided
# by the lease/gate/human/autonomy rules in route() and are not unconditionally
# worker-claimed: in_progress (lease), pending_approval (manager gate, with the
# TTL watchdog and autonomy governor summoned conditionally), published (no
# archiver exists yet), failed (human reset), stalled (human unfreeze),
# archived (terminal). Draft and needs_revision additionally follow the
# capsule's declared domain before this table is consulted (see route()).
DEFAULT_WORKER_BY_STATE: dict[State, str] = {
    State.DRAFT: "marketing",
    State.NEEDS_REVISION: "marketing",
    State.PENDING_QA: "qa",
    State.PENDING_DESIGN: "design",
    State.APPROVED: "publishing",
}


class CapsuleRouter:
    """Decides the next action for a capsule from its manifest state.

    Args:
        workers: state -> worker name overrides merged over
            :data:`DEFAULT_WORKER_BY_STATE`.
        stale_after: seconds after which an ``in_progress`` manifest is
            treated as a dead lease and re-routed for execution.

    The router is stateless and safe to share across threads; it only reads.
    """

    def __init__(
        self,
        *,
        workers: Mapping[State, str] | None = None,
        stale_after: float = DEFAULT_STALE_AFTER,
        stall_after: float = DEFAULT_PENDING_APPROVAL_TTL,
        governance_file: str | os.PathLike[str] | None = None,
    ) -> None:
        if stale_after < 0:
            raise ValueError("stale_after must be >= 0")
        if stall_after < 0:
            raise ValueError("stall_after must be >= 0")
        table = dict(DEFAULT_WORKER_BY_STATE)
        if workers:
            table.update(workers)
        self._workers = table
        self._stale_after = stale_after
        self._stall_after = stall_after
        self._governance_file = governance_file

    @property
    def stale_after(self) -> float:
        """Seconds after which an ``in_progress`` lease is considered dead."""
        return self._stale_after

    @property
    def stall_after(self) -> float:
        """Seconds a capsule may sit in ``pending_approval`` before freezing."""
        return self._stall_after

    @property
    def governance_file(self) -> str | os.PathLike[str] | None:
        """Governance file override (``None`` = module/env default)."""
        return self._governance_file

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #

    def discover(self, root: str | os.PathLike[str]) -> list[Path]:
        """Return every capsule directory under ``root`` (sorted, recursive).

        A capsule is any directory containing ``manifest.json`` — nested
        layouts (e.g. per client) are found at every depth, matching the
        Watcher's scan. Unreadable entries are skipped, never raised: one
        broken capsule must not kill a pulse pass.
        """
        base = Path(root)
        if not base.is_dir():
            return []
        capsules: list[Path] = []
        try:
            for entry in base.rglob(MANIFEST_NAME):
                try:
                    if entry.is_file():  # skip a *directory* named manifest.json
                        capsules.append(entry.parent)
                except OSError:
                    continue  # vanished or unreadable entry mid-scan
        except OSError:
            pass  # walk aborted (permissions) — report the partial sweep
        capsules.sort()
        return capsules

    def route_all(self, root: str | os.PathLike[str]) -> list[RouteDecision]:
        """Route every capsule under ``root``; one bad capsule never kills the pass."""
        return [self.route(path) for path in self.discover(root)]

    # ------------------------------------------------------------------ #
    # the decision
    # ------------------------------------------------------------------ #

    def route(self, capsule_path: str | os.PathLike[str]) -> RouteDecision:
        """Read an unlocked capsule manifest and decide its next action.

        Never raises for a broken capsule — corruption is reported as a
        ``QUARANTINE`` decision (the only exceptions are programming errors).
        """
        capsule = Path(capsule_path)
        path_str = str(capsule)

        if not capsule.is_dir():
            return RouteDecision(
                capsule_path=path_str,
                action=Action.MISSING,
                reason="capsule directory does not exist",
            )

        try:
            manifest = load_manifest(capsule)
            capsule_id = str(manifest["capsule_id"])
            state = State.parse(manifest["state"])  # validated — cannot fail
        except CapsuleError as exc:
            return RouteDecision(
                capsule_path=path_str,
                action=Action.QUARANTINE,
                reason=str(exc),
            )

        return self._decide(capsule, capsule_id, state, manifest)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _decide(
        self, capsule: Path, capsule_id: str, state: State, manifest: dict
    ) -> RouteDecision:
        """Apply the routing rules for a validated manifest.

        B1 task card: when the capsule ships a valid ``task_profile.json``
        declaring this state, its worker wins over the domain/table rules —
        still only for card-declarable (routable) states. A broken card is
        recorded as ``profile_rejected`` in the decision reason context and
        ignored: the default tables decide, and a malformed card can never
        wedge a capsule. Constitutional states never consult the card.
        """
        base = {
            "capsule_path": str(capsule),
            "capsule_id": capsule_id,
            "state": state,
        }

        # Revision cap first, before any card/table/domain rule may name a
        # worker: a capsule whose rework counter is over the cap goes to a
        # human, never back to a factory (defence in depth — transition()
        # already escalates the rejection that crosses the line).
        if state is State.NEEDS_REVISION:
            revisions = revision_count(manifest)
            cap = revision_cap()
            if revisions > cap:
                return RouteDecision(
                    **base,
                    action=Action.HOLD,
                    reason=(
                        f"revision cap exceeded ({revisions} > {cap}) — no "
                        "worker dispatch; human decision required (rework "
                        "manually, move to failed, or archive)"
                    ),
                )

        profile = None
        profile_error: str | None = None
        if state.value in task_profile.ROUTABLE_STATES:
            try:
                profile = task_profile.load_task_profile(capsule)
            except Exception as exc:
                # Spec §4.5 + the router's no-raise contract: a broken card is
                # *reported*, then ignored — the capsule proceeds on the
                # default tables, never wedged. This catches TaskProfileError
                # and any OSError/UnicodeError while reading the card file.
                profile_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "router: task profile rejected for %s: %s", capsule_id, exc
                )

        if state is State.IN_PROGRESS:
            return self._decide_in_progress(base, manifest)
        if state is State.IN_REVIEW:
            gate_decision = self._decide_gates(base, state, manifest)
            if gate_decision is not None:
                return gate_decision
            tail = last_history_entry(manifest)
            detail = f" ({tail.get('why')})" if tail and tail.get("why") else ""
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=f"awaiting approval gate{detail}",
            )
        if state is State.PENDING_APPROVAL:
            return self._decide_pending_approval(base, manifest)
        if state is State.STALLED:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=(
                    "stalled — auto-frozen pending approval; a human must "
                    "unfreeze (-> pending_approval) or restart (-> draft)"
                ),
            )
        if state is State.FAILED:
            revisions = revision_count(manifest)
            cap = revision_cap()
            reason = (
                f"revision cap exceeded ({revisions} > {cap}) — human decision "
                "required (resurrect for a fresh budget, rework, or archive)"
                if revisions > cap
                else f"attempt cap reached (attempt={manifest['attempt']}) — "
                "human reset required"
            )
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=reason,
            )
        if state is State.ARCHIVED:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason="archived — terminal state",
            )
        if state is State.APPROVED:
            # A weekly/occasion post carries a strict schedule: hold it (no
            # worker run, no history noise) until its moment arrives, then let
            # the publishing worker deliver it.
            rules = manifest.get("publishing_rules")
            if isinstance(rules, dict) and rules.get("mode") == "strict":
                due = parse_ts(rules.get("scheduled_for"))
                if due is not None and datetime.now(timezone.utc) < due:
                    return RouteDecision(
                        **base,
                        action=Action.HOLD,
                        reason=(
                            "approved and scheduled for "
                            f"{due.isoformat()} — not due yet"
                        ),
                    )

        # B1: a valid card's declaration for this state wins first.
        if profile is not None:
            declared = profile.worker_for(state.value)
            if declared:
                note = profile.acceptance_for(state.value)
                reason = f"task profile: stage {state.value!r} -> worker {declared!r}"
                if note:
                    reason += f" (acceptance: {note})"
                return RouteDecision(**base, action=Action.EXECUTE, worker=declared, reason=reason)
        elif profile_error:
            # Fail closed, non-wedging: ignore the card, note the rejection.
            if state is State.DRAFT or state is State.NEEDS_REVISION:
                pass  # fall through to domain/table rules below

        # Domain-following states. A capsule's declared domain is the lawful
        # owner of its birth (draft) and its rework (needs_revision): a
        # rejected capsule returns to the factory that made it, whatever
        # branch that is. Without a declared domain the configured table
        # decides (legacy capsules) — the router never guesses a domain.
        if state is State.DRAFT or state is State.NEEDS_REVISION:
            domain = manifest.get("domain")
            if isinstance(domain, str) and domain.strip():
                worker = domain.strip()
                reason = f"state {state.value!r} of domain '{worker}' -> worker '{worker}'"
                if state is State.DRAFT:
                    reason = f"draft domain '{worker}' -> worker '{worker}'"
            else:
                worker = self._workers.get(state)
                reason = f"state {state.value!r} -> worker {worker!r}"
        else:
            worker = self._workers.get(state)
            reason = f"state {state.value!r} -> worker {worker!r}"

        if worker is None:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=f"no worker registered for state {state.value!r}",
                profile_rejected=profile_error,
            )
        return RouteDecision(
            **base,
            action=Action.EXECUTE,
            worker=worker,
            reason=reason,
            profile_rejected=profile_error,
        )

    def _decide_pending_approval(self, base: dict, manifest: dict) -> RouteDecision:
        """The gate: freeze past TTL, auto-approve expert autonomy, else HOLD.

        Order matters: the TTL freeze wins over autonomy so a capsule that sat
        forgotten past the window goes to a human rather than silently
        shipping on the next pulse. Autonomy only short-circuits the gate when
        a decision was recorded at entry (no re-roll of the audit sample), the
        decision did not hit the 1-in-50 audit, and the domain is *still*
        expert (a demotion while the capsule waited wins — fail closed).
        """
        # Dynamic TTL: explicit ttl_hours or event_date calculation (الطوبة 2)
        effective_ttl = self._stall_after
        ttl_hours = manifest.get("ttl_hours")
        if isinstance(ttl_hours, (int, float)) and ttl_hours > 0:
            effective_ttl = float(ttl_hours) * 3600.0
        elif manifest.get("event_date"):
            event_dt = parse_ts(str(manifest.get("event_date")))
            if event_dt is not None:
                seconds_to_event = (event_dt - datetime.now(timezone.utc)).total_seconds()
                if seconds_to_event > 0:
                    effective_ttl = max(3600.0, seconds_to_event - 4 * 3600.0)

        age = state_age_seconds(manifest)
        if age is not None and age > effective_ttl:
            return RouteDecision(
                **base,
                action=Action.EXECUTE,
                worker=STALL_WATCHDOG_WORKER,
                reason=(
                    f"pending_approval for {int(age)}s exceeds the "
                    f"{int(effective_ttl)}s TTL — auto-freeze"
                ),
            )

        # The universal gate layer decides next, inside the TTL window: while
        # a gate is incomplete nothing downstream may proceed; an automated
        # gate is executed, a human gate holds with its checklist in the
        # reason, and a complete (or absent) plan falls through to the legacy
        # autonomy/manager logic below.
        gate_decision = self._decide_gates(base, State.PENDING_APPROVAL, manifest)
        if gate_decision is not None:
            return gate_decision

        decision = read_autonomy_decision(manifest)
        if decision is None:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=(
                    "awaiting manager approval gate (pending_approval; no "
                    "autonomy decision recorded — fail-closed human audit)"
                ),
            )
        if decision.requires_human_approval:
            detail = (
                "random 1-in-50 audit"
                if decision.audit_sampled
                else f"{decision.level} requires human audit"
            )
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=f"awaiting manager approval gate ({detail})",
            )

        level = current_level(decision.domain, path=self._governance_file)
        if level not in EXPERT_LEVELS:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=(
                    f"awaiting manager approval gate (domain "
                    f"{decision.domain!r} is now {level} — human audit)"
                ),
            )
        return RouteDecision(
            **base,
            action=Action.EXECUTE,
            worker=GOVERNOR_WORKER,
            reason=(
                f"autonomy {level} for domain {decision.domain!r} — "
                "auto-approval (artifact hash verified by the governor)"
            ),
        )

    def _decide_gates(
        self, base: dict, state: State, manifest: dict
    ) -> "RouteDecision | None":
        """Route on the capsule's gate plan (``None`` = no gate rule applies).

        Fail-closed semantics:

        - a **corrupt** plan is a HOLD for human repair — never guessed at,
          never re-written by the router (which stays read-only);
        - an **automated** active gate is executed by the ``gate-runner``;
        - a **human** active gate is a HOLD naming the gate and the capability
          its decision demands — after one runner pass has opened it, since a
          signature needs bound artifacts (an unopened gate refuses approval);
        - a **complete** plan returns ``None`` so the caller's legacy logic
          decides (governor / manager / transition into the approval gate when
          the capsule is still ``in_review``);
        - **no plan** returns ``None`` for ``pending_approval`` (legacy
          capsules keep working) but routes ``in_review`` to the gate runner
          only when a plan exists — an unplanned ``in_review`` holds as before.
        """
        try:
            from capsule_engine import gates as gate_engine

            records = gate_engine.load_records(manifest)
        except ImportError:  # pragma: no cover - gates module always ships
            return None
        except Exception as exc:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=(
                    "gate plan unreadable — human repair required: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        if not records:
            if state is State.PENDING_APPROVAL and self._gates_policy_enabled():
                # A capsule that entered the approval gate without a plan
                # (created directly by the cockpit, restored, or legacy) is
                # seeded by the gate runner from its domain template — the
                # gate layer is a policy guarantee, not an opt-in per capsule.
                return RouteDecision(
                    **base,
                    action=Action.EXECUTE,
                    worker=GATE_RUNNER_WORKER,
                    reason="gate plan not initialized — seeding from the domain template",
                )
            return None

        active = gate_engine.active_record(records)
        checklist = gate_engine.summary_line(records)
        if active is None:
            if state is State.IN_REVIEW:
                return RouteDecision(
                    **base,
                    action=Action.EXECUTE,
                    worker=GATE_RUNNER_WORKER,
                    reason=(
                        "all gates satisfied — entering the approval gate "
                        f"({checklist})"
                    ),
                )
            return None  # complete plan on pending_approval: legacy authority

        if active.automated:
            return RouteDecision(
                **base,
                action=Action.EXECUTE,
                worker=GATE_RUNNER_WORKER,
                reason=(
                    f"gate plan: evaluating {active.gate_id} "
                    f"({active.gate_type}) — {checklist}"
                ),
            )
        if not active.opened_at:
            # The runner opens each gate and stops at the first human one, so a
            # human gate that follows another human gate becomes active without
            # ever being opened. Holding it there deadlocks the plan: approving
            # it needs bound artifacts (the TOCTOU guard refuses an unopened
            # gate), and nothing else ever binds them. Route the runner once to
            # open it; the next pulse holds on it as usual.
            return RouteDecision(
                **base,
                action=Action.EXECUTE,
                worker=GATE_RUNNER_WORKER,
                reason=(
                    f"gate plan: opening human gate {active.gate_id} "
                    f"({active.gate_type}) for review — {checklist}"
                ),
            )
        return RouteDecision(
            **base,
            action=Action.HOLD,
            reason=(
                f"awaiting human gate {active.gate_id} '{active.name}' "
                f"({active.gate_type}); authority {active.authority or 'capsule:approve'}"
                f" — {checklist}"
            ),
        )

    @staticmethod
    def _gates_policy_enabled() -> bool:
        """True when ``config/engine.json`` enables the universal gate layer.

        Fail-safe: an unreadable policy is treated as *enabled* only when the
        built-in defaults say so; a policy layer that cannot load at all
        degrades to the legacy (plan-less) routing rather than guessing.
        """
        try:
            from capsule_engine.policy import load_policy

            return bool(load_policy().gates.enabled)
        except Exception:
            return False

    def _decide_in_progress(self, base: dict, manifest: dict) -> RouteDecision:
        """A claimed capsule is held unless its lease looks dead.

        A live lease means another pulse owns the capsule right now. A dead
        lease is re-routed to the worker recorded as ``by`` in the newest
        history entry — the process that claimed it is gone (the kernel
        released its flock), so that worker is the lawful owner to resume.
        Fail closed when history cannot name an owner.
        """
        updated = parse_ts(manifest["updated_at"])
        if updated is None:
            return RouteDecision(
                **base,
                action=Action.QUARANTINE,
                reason="in_progress with unparseable updated_at — cannot prove "
                "the lease is alive",
            )
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age <= self._stale_after:
            return RouteDecision(
                **base,
                action=Action.HOLD,
                reason=f"claimed by a live pulse (age {int(age)}s)",
            )

        tail = last_history_entry(manifest)
        owner = tail.get("by") if tail else None
        if isinstance(owner, str) and owner.strip() and owner != "system":
            return RouteDecision(
                **base,
                action=Action.EXECUTE,
                worker=owner,
                reason=f"stale in_progress lease (age {int(age)}s > "
                f"{int(self._stale_after)}s) — resume worker {owner!r}",
            )
        return RouteDecision(
            **base,
            action=Action.HOLD,
            reason=f"stale in_progress lease (age {int(age)}s) with no recorded "
            "worker owner — human required",
        )
