#!/usr/bin/env python3
"""post_cycle_trial.py — full post lifecycle against the LIVE vault, OFFLINE.

This is a runbook, not a sandbox test: it drives the exact production scripts
end to end and leaves real artifacts (ticket, gate, capsule) in the vault so
the cockpit can show them. It performs **zero external side effects**:

  AGENCY_OFFLINE_MODE=1     -> deterministic offline ModelGateway (no LLM calls)
  FB_PAGE_ID / META_SYSTEM_TOKEN cleared -> no channel-side credentials in reach
  HF_TOKEN cleared          -> image comes from the keyless free engine
  delivery is answered by the deterministic offline gear: the publishing worker
  speaks `social:publish` over the bridge and AGENCY_OFFLINE_MODE makes
  capsule_engine.bridge.default_bridge() return the offline implementation, so
  the real delivery path runs with zero network.

Lifecycle exercised:
  tracker ticket -> gate -> human approve -> settle -> dispatcher -> capsule
  -> marketing -> qa -> [copy sign-off: the runbook answers as --reviewer]
  -> design -> pending_approval
  -> human approve (SHA-256 verified) -> publishing -> published

The copy sign-off is the one stage that waits on a person (the app is where a
person answers it). The runbook answers it itself and prints the step, so an
automated cycle still reaches ``published`` without hiding the hold.

Every step prints one JSON line. Exit 0 only when the capsule is `published`.
Run:  python3 tools/post_cycle_trial.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO / "engine"
for _p in (str(REPO), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PY = sys.executable

# The post we want the factory to produce.
REQUEST = (
    "Draft an executive thought-leadership post on technological sovereignty "
    "and autonomous self-governing architectures for enterprise leaders."
)

# Envelope inherited by every child process. Offline by default; `--live`
# inherits the real environment so publishing actually reaches Facebook.
CHILD_ENV: dict[str, str] = {}
LIVE = False


def build_child_env(live: bool) -> dict[str, str]:
    env = dict(os.environ)
    if live:
        # Real run: keep the bridge endpoint/token and model keys from .env, so
        # the delivery gear that answers `social:publish` is the real one.
        env.pop("AGENCY_OFFLINE_MODE", None)
        env["AGENCY_INCREMENTAL_PULSE"] = "off"
        return env
    env.update(
        {
            "AGENCY_OFFLINE_MODE": "1",
            "FB_PAGE_ID": "",
            "META_SYSTEM_TOKEN": "",
            "HF_TOKEN": "",
            "AGENCY_INCREMENTAL_PULSE": "off",
        }
    )
    return env


def run(args: list[str], cwd: Path, payload: dict | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        [PY, *args],
        cwd=str(cwd),
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=180,
        env=CHILD_ENV,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def step(name: str, ok: bool, **extra) -> None:
    print(json.dumps({"step": name, "ok": bool(ok), **extra}, ensure_ascii=False), flush=True)


def _spool_dir() -> Path:
    spool = REPO / "data" / "capsules" / "spool"
    if not spool.is_dir() and (REPO / "capsules" / "spool").is_dir():
        return REPO / "capsules" / "spool"
    return spool


def spool_dirs() -> set[str]:
    spool = _spool_dir()
    if not spool.is_dir():
        return set()
    return {p.name for p in spool.iterdir() if p.is_dir() and (p / "manifest.json").is_file()}


def read_state(capsule_id: str) -> str | None:
    mf = _spool_dir() / capsule_id / "manifest.json"
    if not mf.is_file():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8")).get("state")
    except Exception:
        return None


def sign_off_copy(capsule_id: str, reviewer: str) -> bool:
    """Sign the copy off so an automated cycle can still reach ``published``.

    The copy sign-off parks a capsule at ``pending_design`` until a person has
    read the words, and in the app a person is exactly who decides. This
    runbook is the automated one, so it decides as ``reviewer`` — through the
    same ``orchestration.copy_signoff`` the app's button calls, digest check
    and all — and reports the step, so the hold shows up in the run instead of
    being bypassed in silence.
    """
    from orchestration import copy_signoff  # noqa: E402

    try:
        outcome = copy_signoff.apply(
            _spool_dir() / capsule_id,
            decision="approved",
            reason="",
            reviewer=reviewer,
            audit_note="live trial sign-off" if LIVE else "offline trial sign-off",
        )
    except Exception as exc:  # a refusal is a result, not a crash
        step(
            "copy_signoff",
            False,
            state=read_state(capsule_id),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    step("copy_signoff", True, state=outcome.new_state, by=reviewer)
    return True


def main(argv: list[str] | None = None) -> int:
    global CHILD_ENV, LIVE
    parser = argparse.ArgumentParser(description="Full post lifecycle against the live vault.")
    parser.add_argument("--live", action="store_true",
                        help="publish for real (Facebook); requires --yes")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the live, irreversible external publish")
    parser.add_argument("--reviewer", default="trial_human", help="human id recorded in governance")
    args = parser.parse_args(argv)
    if args.live and not args.yes:
        print(json.dumps({"error": "refusing a live run without --yes", "hint": "add --live --yes"},
                         ensure_ascii=False))
        return 2
    LIVE = args.live
    CHILD_ENV = build_child_env(LIVE)

    print(json.dumps({"trial": "post_cycle", "offline": not LIVE, "request": REQUEST},
                     ensure_ascii=False))

    # 0. baseline
    before = spool_dirs()
    step("baseline", True, spool_capsules=len(before))

    # 1. ticket -> needs_gate (spend above cap forces a gate)
    ide = REPO / "data" / "domains" / "ideation"
    rc, out, err = run(
        ["tracker_script.py"],
        ide,
        {"request": REQUEST, "client_id": "trial", "budget_cap": 0, "estimated_spend": 5},
    )
    if rc != 0:
        step("create_ticket", False, rc=rc, err=err)
        return 1
    created = json.loads(out)
    gate_id = created.get("gate")
    step("create_ticket", created.get("status") == "needs_gate", status=created.get("status"), gate_id=gate_id)
    if not gate_id:
        return 1

    # 2. human approves the gate
    gates = REPO / "data" / "governance" / "approval_gates"
    rc, out, err = run(
        ["gate_script.py", "decide", "--gate-id", gate_id, "--by", args.reviewer,
         "--decision", "approved", "--reason",
         ("live post cycle trial" if LIVE else "offline post cycle trial")],
        gates,
    )
    step("approve_gate", rc == 0, rc=rc, err=err)
    if rc != 0:
        return 1

    # 3. settle: approved gate -> dispatchable ticket (carries `via`)
    rc, out, err = run(["tracker_script.py", "settle"], ide)
    settled = json.loads(out).get("settled", []) if rc == 0 else []
    step("settle", rc == 0 and len(settled) == 1, settled=settled, err=err)
    if rc != 0 or not settled:
        return 1

    # 4. dispatcher wakes the ticket -> ideation bridge spawns the capsule
    orch = REPO / "engine" / "orchestration"
    rc, out, err = run(["dispatcher.py", "run"], orch)
    after = spool_dirs()
    new = sorted(after - before)
    step("dispatch", rc == 0 and len(new) == 1, rc=rc, new_capsules=new, err=err)
    if len(new) != 1:
        return 1
    capsule_id = new[0]

    # 5. factory pulse: marketing -> qa -> [copy sign-off] -> design -> qa
    #    -> pending_approval. The sign-off is the one stage that waits on a
    #    person, so the runbook answers it as its reviewer and says so.
    from capsule_engine import State, load_manifest, save_manifest, transition  # noqa: E402
    from capsule_engine.locking import CapsuleLock  # noqa: E402

    pulse = str(REPO / "engine" / "orchestration" / "capsule_pulse.py")
    for attempt in range(1, 7):
        rc, out, err = run([pulse, "--max-transitions", "8"], REPO)
        state = read_state(capsule_id)
        step(f"pulse_{attempt}", rc == 0, state=state)
        if state == State.PENDING_DESIGN.value and not sign_off_copy(capsule_id, args.reviewer):
            return 1
        if state in ("pending_approval", "failed", "stalled"):
            break
        time.sleep(0.5)
    if read_state(capsule_id) != "pending_approval":
        step("reached_gate", False, state=read_state(capsule_id))
        return 1
    step("reached_gate", True, capsule_id=capsule_id)

    # 6. human approves the deliverable (TOCTOU hash verified by transition)
    cap = _spool_dir() / capsule_id
    with CapsuleLock(cap):
        manifest = load_manifest(cap)
        artifact_sha = (manifest.get("metadata") or {}).get("artifact_sha256")
        rules = manifest.get("publishing_rules") or {}
        scheduled = rules.get("scheduled_for")
        if scheduled:
            # Weekly/occasion posts are scheduled ahead (the router holds them
            # until due). A trial must complete now, so it forces an immediate
            # publish and reports the original slot for the record.
            step("scheduled", True, mode=rules.get("mode"), scheduled_for=scheduled,
                 note="trial forces immediate publish")
            manifest["publishing_rules"] = {"mode": "queue", "scheduled_for": None}
        transition(manifest, State.APPROVED, by=args.reviewer,
                   why=("live trial approval" if LIVE else "offline trial approval"),
                   capsule_path=cap)
        save_manifest(cap, manifest)
    step("approve_capsule", True, artifact_sha256=artifact_sha)

    # 7. publishing pulse -> published
    rc, out, err = run([pulse, "--max-transitions", "8"], REPO)
    final = read_state(capsule_id)
    step("publish", rc == 0 and final == "published", state=final, err=err)

    # 8. evidence
    manifest = json.loads((cap / "manifest.json").read_text(encoding="utf-8"))
    meta = manifest.get("metadata") or {}
    evidence = {
        "capsule_id": capsule_id,
        "state": manifest.get("state"),
        "published_via": manifest.get("published_via"),
        "published_at": manifest.get("published_at"),
        "artifact_sha256": meta.get("artifact_sha256"),
        "declared_channels": manifest.get("channels"),
        "publishing_receipts": meta.get("publishing_receipts"),
        "gear_exchanges": meta.get("gear_exchanges"),
        "has_copy": (cap / "copy.md").is_file(),
        "has_image": (cap / "image.jpg").is_file(),
        "transitions": len(manifest.get("history", [])),
    }
    step("evidence", manifest.get("state") == "published", **evidence)
    print(json.dumps({"trial": "post_cycle", "ok": manifest.get("state") == "published",
                      "capsule_id": capsule_id}, ensure_ascii=False))
    return 0 if manifest.get("state") == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
