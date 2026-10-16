#!/usr/bin/env python3
"""governor.py — GovernorWorker: the autonomy ladder's gate-skipping stage.

Pipeline stage: a ``pending_approval`` capsule whose domain has *earned*
autonomy (L2/L3) and did **not** hit the random 1-in-50 audit is approved
without a human — but never blindly:

1. **Recorded decision**: the router only summons this worker when the autonomy
   decision recorded at gate entry says "no human required". A missing,
   malformed, or human-audit decision defers back to the manager gate (the
   router then holds the capsule) — no exception, no state change.
2. **Fresh level check**: the domain's level is re-read from
   ``governance.json``. If it was demoted to L0/L1 while the capsule waited,
   auto-approval is refused (fail closed) and the capsule is held.
3. **Gate plan completeness**: the capsule's gate plan must be complete —
   every gate terminal, none failed — checked HERE, not only by the router's
   dispatch (defence in depth; same rule as the seat guard in
   ``manifest.transition()``). An incomplete or unreadable plan defers back
   to the human gate: recorded, no state change.
4. **TOCTOU verification**: the bound artifact (any recorded file — default
   ``copy.md``) is re-hashed and compared to the
   digest bound when the capsule entered the gate. A mismatch, missing
   artifact, or missing binding raises :class:`SecurityError` — the transition
   itself is guarded too (defence in depth), so a tampered artifact can never
   reach ``approved``.
5. **State transition**: ``pending_approval -> approved`` through the lawful
   matrix, recording the domain, level, and truncated digest in ``history``.
   The same pulse then chains into the publishing stage.

Class contract (see :class:`capsule_engine.BaseWorker`): ``handles`` is exactly
``{PENDING_APPROVAL}``; the worker never claims a stale ``in_progress`` lease
(nothing here writes before approving).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, ClassVar

try:  # normal import (package on sys.path)
    from capsule_engine import BaseWorker, State
    from capsule_engine import gates as gate_engine
    from capsule_engine.governance import (
        EXPERT_LEVELS,
        current_level,
        read_autonomy_decision,
    )
    from capsule_engine.manifest import verify_artifact
    from capsule_engine.policy import load_policy
except ImportError:  # script/odd-cwd execution — mirror orchestration/dispatcher.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import BaseWorker, State
    from capsule_engine import gates as gate_engine
    from capsule_engine.governance import (
        EXPERT_LEVELS,
        current_level,
        read_autonomy_decision,
    )
    from capsule_engine.manifest import verify_artifact
    from capsule_engine.policy import load_policy

__all__ = ["GovernorWorker"]


class GovernorWorker(BaseWorker):
    """Auto-approve expert-domain capsules after verifying their artifact.

    Args:
        governance_path: optional ``governance.json`` override (tests and
            multi-vault deployments); ``None`` uses the module/env default.

    The worker is deliberately *not* given a gateway: it performs no model
    calls and can never generate content — it only checks what already exists.
    """

    name: ClassVar[str] = "governor"
    handles: ClassVar[frozenset[State]] = frozenset({State.PENDING_APPROVAL})
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def __init__(
        self, *, governance_path: str | os.PathLike[str] | None = None
    ) -> None:
        self.governance_path = governance_path

    # ------------------------------------------------------------------ #
    # the worker contract
    # ------------------------------------------------------------------ #

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Approve the capsule when autonomy and integrity both allow it.

        Returns the manifest untouched (a lawful no-op) when humanity must
        decide; raises :class:`SecurityError` (surfaced as
        ``WorkerExecutionError`` by ``run()``) when the artifact fails
        verification.

        Raises:
            SecurityError: the bound artifact is missing, unbound, or altered since it
                entered the gate — fail closed.
        """
        decision = read_autonomy_decision(manifest)
        if decision is None:
            self.record(
                manifest,
                "no autonomy decision recorded — refusing auto-approval "
                "(fail-closed human audit)",
            )
            return manifest
        if decision.requires_human_approval:
            self.record(
                manifest,
                f"autonomy {decision.level} ({decision.label}) requires human "
                f"audit — deferring to the manager gate: {decision.reason}",
            )
            return manifest

        level = current_level(decision.domain, path=self.governance_path)
        if level not in EXPERT_LEVELS:
            self.record(
                manifest,
                f"domain {decision.domain!r} is now {level} — autonomy revoked; "
                "refusing auto-approval (fail-closed human audit)",
            )
            return manifest

        # The gate plan must be complete BEFORE autonomy may approve — the
        # router's dispatch alone is not enough (defence in depth; same rule
        # as the seat guard in manifest.transition()). Gates disabled by
        # policy keep the legacy hash-only approval.
        if load_policy().gates.enabled:
            try:
                records = gate_engine.load_records(manifest)
            except gate_engine.GateSchemaError as exc:
                self.record(
                    manifest,
                    f"gate plan unreadable — refusing auto-approval: {exc}",
                )
                return manifest
            if not gate_engine.is_complete(records):
                progress = gate_engine.progress(records)
                self.record(
                    manifest,
                    "gate plan incomplete ("
                    f"{progress['passed']} of {progress['total']} gates passed) "
                    "— refusing auto-approval (fail-closed human audit)",
                )
                return manifest

        digest = verify_artifact(manifest, capsule_path)  # raises on tamper
        self.transition(
            manifest,
            State.APPROVED,
            why=(
                f"autonomy {level} auto-approval for domain {decision.domain!r} "
                f"(2% audit rate); artifact sha256={digest[:12]}… verified"
            ),
        )
        return manifest


if __name__ == "__main__":  # offline smoke: python3 workers/governor.py
    import json

    from capsule_engine import load_manifest, save_manifest
    from capsule_engine.governance import record_autonomy_decision

    demo = {
        "capsule_id": "cap-governor-demo",
        "state": "pending_approval",
        "created_at": "2026-10-04T00:00:00+00:00",
        "updated_at": "2026-10-04T00:00:00+00:00",
        "attempt": 0,
        "history": [],
        "metadata": {"domain": "smoke-domain"},
    }
    tmp = Path(f"/tmp/governor_smoke_{Path(__file__).stem}")
    tmp.mkdir(exist_ok=True)
    (tmp / "copy.md").write_text("# Demo\n\nverified artifact\n", encoding="utf-8")
    from capsule_engine.manifest import bind_artifact

    bind_artifact(demo, tmp)
    record_autonomy_decision(demo, sample=0.99)
    save_manifest(tmp, demo)
    result = GovernorWorker().run(str(tmp))
    print(
        json.dumps(
            {
                "state": result["state"],
                "capsule": str(tmp),
                "history_tail": result["history"][-1]["why"],
            },
            indent=2,
        )
    )
