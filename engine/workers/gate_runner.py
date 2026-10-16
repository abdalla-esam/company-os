#!/usr/bin/env python3
"""gate_runner.py — executes automated gates of a capsule's multi-gate plan.

Pipeline stage: given a capsule in ``in_review`` or ``pending_approval`` that
carries a gate plan (``manifest["gates"]``, seeded on gate entry from the
domain template), advance it through every consecutive **automated** gate
(``qa_review``, ``security_audit``, or any evaluator a branch registered):

1. open each gate — element-level SHA-256 binding of its declared targets
   (``deliverables/**/*`` and friends), so a later human approval can prove
   the bytes did not change (anti-TOCTOU, per element);
2. evaluate it deterministically — no network, no tokens, CPU-light;
3. write the verdict into the gate record (status + evidence), and on failure:
   persist ``feedback.txt`` for the author, then transition the capsule to
   ``needs_revision`` — the fail-closed rejection path. The worker's
   transition hook appends the lesson to the cumulative feedback ledger;
4. stop at the first **human** gate (``domain_lead_approval``,
   ``manual_edit``, ``sovereign_root_approval``) and record that the capsule
   is waiting on a capability-holding principal — the router then holds it.

When the last gate passes and the capsule was ``in_review``, the worker moves
it to ``pending_approval`` so the existing autonomy/human approval logic
(governor or manager) applies unchanged: gates decide *evidence*, the
approval layer decides *authority*. Nothing here approves a capsule itself.

A corrupt plan is never repaired by guessing: the worker records the refusal
in history and returns, and the router holds the capsule for human repair.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, ClassVar, Optional

try:  # normal import (package on sys.path)
    from capsule_engine import BaseWorker, State
    from capsule_engine import gates as gate_engine
    from capsule_engine import trace as trace_engine
    from capsule_engine.policy import load_policy
except ImportError:  # script/odd-cwd execution — mirror workers/qa.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import BaseWorker, State
    from capsule_engine import gates as gate_engine
    from capsule_engine import trace as trace_engine
    from capsule_engine.policy import load_policy

logger = logging.getLogger("workers.gate_runner")

__all__ = ["GateRunnerWorker", "GateRunnerError", "MAX_AUTOMATED_GATES_PER_RUN"]

FEEDBACK_FILE = "feedback.txt"
#: Bound one worker run: a plan may declare many gates, but a single run
#: evaluates at most this many automated gates and lets the next pulse
#: continue (keeps every run CPU-light and inspectable).
MAX_AUTOMATED_GATES_PER_RUN = 16


class GateRunnerError(Exception):
    """The gate layer could not execute — reported, never guessed."""


class GateRunnerWorker(BaseWorker):
    """Advance a capsule through its automated gate plan (fail closed)."""

    name: ClassVar[str] = "gate-runner"
    handles: ClassVar[frozenset[State]] = frozenset(
        {State.IN_REVIEW, State.PENDING_APPROVAL}
    )
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Evaluate consecutive automated gates; stop at human/complete/reject."""
        policy = load_policy()
        if not policy.gates.enabled:
            self.record(manifest, "gate layer disabled by policy — no action")
            return manifest

        try:
            records = gate_engine.load_records(manifest)
        except gate_engine.GateSchemaError as exc:
            # Fail closed without wedging: the router holds this capsule for a
            # human, and every pulse re-reports rather than re-writing state.
            self._note(
                manifest,
                f"gate plan unreadable — human repair required: {exc}",
            )
            return manifest

        if not records:
            # Defensive seeding: a capsule that reached the gate before the
            # plan existed (legacy data) gets its domain template now.
            declared = manifest.get("domain")
            domain = (
                declared.strip()
                if isinstance(declared, str) and declared.strip()
                else None
            )
            try:
                specs = gate_engine.specs_from_template(
                    policy.gates.template_for(domain),
                    max_gates=policy.gates.max_gates,
                )
                result = gate_engine.ensure_plan(
                    manifest, specs, by=self.name, replace_empty=True
                )
            except gate_engine.GateConfigError as exc:
                self._note(
                    manifest,
                    f"gate plan not initialized — template refused: {exc}",
                )
                return manifest
            if not result.created:
                self._note(manifest, "no gate plan configured for this capsule")
                return manifest
            records = gate_engine.load_records(manifest)

        evaluated = 0
        while evaluated < MAX_AUTOMATED_GATES_PER_RUN:
            active = gate_engine.active_record(records)
            if active is None:
                return self._finish_plan(capsule_path, manifest, records)
            if active.human:
                self._await_human(capsule_path, manifest, records, active)
                return manifest
            decision = self._run_automated(capsule_path, manifest, active)
            if decision is not None:
                return decision  # rejection: needs_revision already recorded
            evaluated += 1
            records = gate_engine.load_records(manifest)

        self._note(
            manifest,
            f"gate run capped at {MAX_AUTOMATED_GATES_PER_RUN} automated "
            "gate(s) — continues next pulse",
        )
        return manifest

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _note(self, manifest: dict[str, Any], message: str) -> None:
        """Record a history note once — identical consecutive notes collapse.

        A capsule that cannot progress (refused template, capped run) is
        re-routed every pulse; without this guard the history would grow one
        duplicate line per pulse.
        """
        from capsule_engine.manifest import last_history_entry

        tail = last_history_entry(manifest)
        if tail is not None and tail.get("why") == message:
            return
        self.record(manifest, message)

    def _run_automated(
        self,
        capsule_path: str,
        manifest: dict[str, Any],
        active: "gate_engine.GateRecord",
    ) -> Optional[dict[str, Any]]:
        """Evaluate one automated gate; returns the manifest on rejection."""
        step = self.trace_step(
            f"gate {active.gate_id}: {active.name}", gear=active.gate_type
        )
        try:
            record = active
            if not record.opened_at:
                record = gate_engine.open_gate(
                    manifest, record.gate_id, capsule_path, by=self.name
                )
            step.add_artifacts(record.artifacts)
            try:
                verdict = gate_engine.evaluate_gate(capsule_path, manifest, record)
            except gate_engine.GateEvaluationError as exc:
                verdict = gate_engine.GateVerdict(
                    passed=False,
                    reason=(
                        f"gate {record.gate_id} ({record.gate_type}) cannot be "
                        f"evaluated: {exc} — fail closed"
                    ),
                )
            if verdict.passed:
                gate_engine.decide(
                    manifest,
                    record.gate_id,
                    "approve",
                    by=self.name,
                    reason=verdict.reason,
                    capsule_path=capsule_path,
                    evidence=dict(verdict.evidence),
                )
                step.progress(100)
                step.finish(trace_engine.STATUS_SUCCESS)
                return None

            # Rejection: persist the instruction for the author, decide the
            # gate, then drop the capsule to needs_revision (lawful edge from
            # both in_review and pending_approval). The worker transition hook
            # appends the lesson to the cumulative feedback ledger.
            self.write_text(capsule_path, FEEDBACK_FILE, verdict.reason + "\n")
            gate_engine.decide(
                manifest,
                record.gate_id,
                "reject",
                by=self.name,
                reason=verdict.reason,
                evidence=dict(verdict.evidence),
            )
            step.finish(trace_engine.STATUS_NEEDS_REVISION)
            self.transition(
                manifest,
                State.NEEDS_REVISION,
                why=(
                    f"gate {record.gate_id} ({record.gate_type}) rejected: "
                    f"{verdict.reason} (feedback saved to {FEEDBACK_FILE})"
                ),
            )
            return manifest
        except Exception as exc:
            step.finish(
                trace_engine.STATUS_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _await_human(
        self,
        capsule_path: str,
        manifest: dict[str, Any],
        records: "tuple[gate_engine.GateRecord, ...]",
        active: "gate_engine.GateRecord",
    ) -> None:
        """Open a pending human gate and record that the capsule waits on it."""
        if not active.opened_at:
            try:
                gate_engine.open_gate(
                    manifest, active.gate_id, capsule_path, by=self.name
                )
            except Exception as exc:
                # Opening is best-effort: a human gate without a bound target
                # is still a lawful waiting state (the approval decision
                # re-verifies whatever is bound).
                logger.warning(
                    "gate-runner: could not open gate %s: %s", active.gate_id, exc
                )
        self._note(
            manifest,
            (
                f"awaiting human gate {active.gate_id} ({active.name}, "
                f"{active.gate_type}) — authority required: "
                f"{active.authority or 'capsule:approve'} | "
                f"{gate_engine.summary_line(records)}"
            ),
        )

    def _finish_plan(
        self,
        capsule_path: str,
        manifest: dict[str, Any],
        records: "tuple[gate_engine.GateRecord, ...]",
    ) -> dict[str, Any]:
        """Every gate is terminal: enter the approval gate, or record the wait."""
        current = State.parse(manifest["state"])
        failed = [
            record.gate_id
            for record in records
            if record.status == gate_engine.STATUS_FAILED
        ]
        if failed:
            # A failed gate with the capsule still in a gating state means an
            # out-of-band decision (API/CLI) rejected it without moving the
            # state machine: complete the rejection now.
            self.transition(
                manifest,
                State.NEEDS_REVISION,
                why=(
                    "gate(s) rejected: " + ", ".join(failed)
                    + " — returning the deliverable for rework"
                ),
            )
            return manifest

        if current is State.IN_REVIEW:
            self.transition(
                manifest,
                State.PENDING_APPROVAL,
                why=(
                    "all gates passed — entering the approval gate "
                    f"({gate_engine.summary_line(records)})"
                ),
            )
            return manifest
        self.record(
            manifest,
            (
                "all gates passed — awaiting the approval authority "
                f"({gate_engine.summary_line(records)})"
            ),
        )
        return manifest


if __name__ == "__main__":  # offline smoke: python3 workers/gate_runner.py
    import json
    import tempfile

    from capsule_engine.manifest import new_manifest, save_manifest

    demo_dir = Path(tempfile.mkdtemp()) / "cap-gate-runner-smoke"
    demo_dir.mkdir(parents=True)
    (demo_dir / "deliverables").mkdir()
    (demo_dir / "deliverables" / "copy.txt").write_text(
        "A complete, substantive announcement written for enterprise readers "
        "and delivered through the sovereign engine.\n",
        encoding="utf-8",
    )
    demo = new_manifest("cap-gate-runner-smoke", state=State.IN_REVIEW)
    demo["domain"] = "development"
    save_manifest(demo_dir, demo)
    result = GateRunnerWorker().run(str(demo_dir))
    print(
        json.dumps(
            {"state": result["state"], "capsule": str(demo_dir)},
            indent=2,
        )
    )
