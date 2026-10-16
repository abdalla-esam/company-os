#!/usr/bin/env python3
"""seed_gate_plans.py — repair planless pending capsules (fail-closed approve).

The unified approval service refuses to approve a capsule that carries no
gate plan while gates are enabled (see ``orchestration/approval.py`` — the
refusal names this very command). Capsules created before plan seeding became
mandatory, or whose seeding failed, are repaired here: the policy's gate
template is written into ``manifest["gates"]`` and nothing else changes.

Contract: this command **never changes a capsule's state**. It only writes
the gate plan (plus the history note ``ensure_plan`` appends). ``--dry-run``
touches nothing at all — it only counts and lists.

    # what would be seeded? (count + ids, zero writes)
    python3 ops/tools/seed_gate_plans.py --dry-run

    # one capsule (the remedy named in the approval refusal)
    python3 ops/tools/seed_gate_plans.py --capsule-id cap_123

    # every pending capsule without a plan
    python3 ops/tools/seed_gate_plans.py

Exit codes: 0 success (including "nothing to do"), 1 seeding failure.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
for _p in (str(REPO_ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capsule_engine import State  # noqa: E402
from capsule_engine import gates as gate_engine  # noqa: E402
from capsule_engine.locking import CapsuleLock  # noqa: E402
from capsule_engine.manifest import load_manifest, save_manifest  # noqa: E402
from capsule_engine.policy import load_policy  # noqa: E402
from shared_kernel.paths import CAPSULES_SPOOL  # noqa: E402


def candidates(spool: Path, capsule_id: str | None = None) -> list[Path]:
    """Pending capsules without a gate plan, sorted by name (stable output)."""
    found: list[Path] = []
    if not spool.is_dir():
        return found
    for entry in sorted(spool.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if capsule_id and entry.name != capsule_id:
            continue
        if not (entry / "manifest.json").is_file():
            continue
        try:
            manifest = load_manifest(entry)
        except Exception as exc:  # unreadable: report, never repair by guess
            print(f"  ! {entry.name}: unreadable manifest ({exc}) — skipped", file=sys.stderr)
            continue
        if str(manifest.get("state")) != State.PENDING_APPROVAL.value:
            continue  # only pending capsules are waiting on a plan
        if manifest.get("gates"):
            continue  # already governed
        found.append(entry)
    return found


def seed_plan(entry: Path) -> int:
    """Write the policy's gate plan into one capsule. Returns the gate count.

    The capsule's state is read before and after on purpose: this command's
    contract is that it can never move a capsule, and a violation is an error,
    not a silent surprise.

    Everything happens under the capsule's own lock: a busy capsule raises
    ``LockTimeoutError`` instead of seeding a plan unlocked (and instead of
    racing the pulse or another seed pass on the same manifest).
    """
    with CapsuleLock(entry, purpose="seed_gate_plans"):
        manifest = load_manifest(entry)
        state_before = str(manifest.get("state"))
        domain = str(manifest.get("domain") or "").strip() or None

        policy = load_policy()
        specs = gate_engine.specs_from_template(
            policy.gates.template_for(domain),
            max_gates=policy.gates.max_gates,
        )
        manifest["gates"] = []  # a planless record: seed fresh (replace_empty)
        result = gate_engine.ensure_plan(
            manifest, specs, by="seed_gate_plans", replace_empty=True
        )

        save_manifest(entry, manifest)
        if str(manifest.get("state")) != state_before:  # pragma: no cover - belt & braces
            raise RuntimeError(
                f"state changed from {state_before} to {manifest.get('state')} — "
                "this command must never do that"
            )
        return int(result.count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only count and list the affected capsules; write nothing",
    )
    parser.add_argument(
        "--capsule-id",
        default=None,
        help="restrict to one capsule (the remedy named in the approval refusal)",
    )
    parser.add_argument(
        "--spool",
        default=None,
        help="spool directory (default: CAPSULES_SPOOL / engine default)",
    )
    args = parser.parse_args(argv)

    spool = Path(args.spool) if args.spool else Path(CAPSULES_SPOOL)
    found = candidates(spool, args.capsule_id)
    print(f"pending capsules without a gate plan: {len(found)}  (spool: {spool})")
    for entry in found:
        print(f"  - {entry.name}")

    if args.dry_run:
        print("dry-run: nothing written.")
        return 0

    if not found:
        return 0

    if not load_policy().gates.enabled:
        print("gates are disabled by policy — no plans seeded.")
        return 0

    failures = 0
    for entry in found:
        try:
            count = seed_plan(entry)
            print(
                f"  seeded {entry.name}: {count} gate(s) "
                "(state untouched: pending_approval)"
            )
        except Exception as exc:
            failures += 1
            print(f"  ! {entry.name}: seeding failed: {exc}", file=sys.stderr)
    print(f"seeded {len(found) - failures} capsule(s); {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
