#!/usr/bin/env python3
"""pulse.py — one beat of the self-managing factory. Called by the gear (cron); never a daemon.

Beat contract (architecture.md §6):
  1. settle: pull each sector's approved gates into dispatchable tickets.
     [COST: FREE] Pure filesystem state transition; no LLM or external API.
  2. auto-ideator: propose an idea every cadence window (48 h guard).
     [COST: BEARING] Calls LLM (Groq/Cerebras) to generate idea; raises gate.
  3. dispatcher: wake sleeping tickets (ticket -> worker handoff).
     [COST: BEARING] Handoff to sector worker_script.py. E.g., marketing
     worker calls LLM (Groq) for post copy; design worker calls image API.
  4. factory pulse: advance capsules through marketing/qa/design to gate.
     [COST: BEARING] Sequential worker execution. Marketing/QA call LLM;
     Design calls image model API; Publishing calls social platform API.
  5. directus: synchronize Directus Kanban studio cards.
     [COST: BEARING] Spawns capsules and runs worker chain if cards exist.

CRITICAL COST GUARD:
Because steps 2, 3, 4, and 5 can consume model tokens and API credits automatically,
neither cron nor scheduled beats may be enabled in production until a centralized
spend cap (UsageLedger / spend_cap) and kill-switch are enforced (Phase 4/5).
Manual single-ticket execution must be used for testing until then.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_DIR = os.path.dirname(HERE)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

try:
    from shared_kernel.paths import DOMAINS_DIR as _CANONICAL_DOMAINS_DIR, REPO_ROOT as _CANONICAL_REPO_ROOT
    DOMAINS_DIR = str(_CANONICAL_DOMAINS_DIR)
    ROOT = str(_CANONICAL_REPO_ROOT)
except Exception:
    _REPO_ROOT = os.path.dirname(_ENGINE_DIR)
    DOMAINS_DIR = os.environ.get("DOMAINS_DIR", os.path.join(_REPO_ROOT, "data", "domains"))
    ROOT = _REPO_ROOT
DISPATCHER = os.path.join(HERE, "dispatcher.py")
CAPSULE_PULSE = os.path.join(HERE, "capsule_pulse.py")
AUTO_IDEATOR = os.path.join(DOMAINS_DIR, "ideation", "auto_ideator.py")
LOG = os.path.join(HERE, "pulse.log")
LOG_MAX_BYTES = 1024 * 1024  # rotate the evidence log at 1 MB
STEP_TIMEOUT = 180  # seconds per command (the factory may fetch an image)


def _load_env() -> None:
    """Load .env so the beat's children (models, publishing) see the keys.

    The cron line does not source the file; doing it here keeps one source of
    truth. A missing python-dotenv or .env is fine — the process manager may
    already provide the environment.
    """
    try:
        import dotenv

        dotenv.load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def sectors():
    """Every domain with a tracker joins the pulse automatically."""
    if not os.path.isdir(DOMAINS_DIR):
        return []
    return sorted(
        name for name in os.listdir(DOMAINS_DIR)
        if os.path.isfile(os.path.join(DOMAINS_DIR, name, "tracker_script.py")))


def run_cmd(args, cwd):
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=STEP_TIMEOUT, cwd=cwd)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss: %s" % (STEP_TIMEOUT, " ".join(args))


def _short(out):
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return {"raw": (out or "").strip()[-160:]} if (out or "").strip() else None


def _spend_preflight() -> str | None:
    """Why the beat must not spend right now, or None. Checked before any spawn.

    The spend gate lives in the engine (capsule_engine.spend_control); the
    beat honors it one level up so the *subprocesses* (auto_ideator,
    dispatcher, capsule_pulse, directus) never even start when the kill
    switch is on, spend is disabled, or the spend config is unusable.
    """
    try:
        from capsule_engine import spend_control

        return spend_control.blocked_reason()
    except Exception as exc:  # a broken gate must stop the beat, not the beat's report
        return f"spend control unavailable: {type(exc).__name__}: {exc}"


def beat():
    """One pass of the whole loop. A failing step is recorded, never fatal."""
    _load_env()
    settled, errors = {}, []

    # 0. spend gate: refuse BEFORE spawning anything that can spend (the
    #    refusal itself is evidence, so it is returned and logged like a beat).
    spend_block = _spend_preflight()
    if spend_block:
        errors.append({"sector": None, "step": "spend_control", "detail": spend_block})
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "status": "refused",
            "reason": f"spend pre-flight refused the beat: {spend_block}",
            "settled": {}, "auto_ideator": None, "run": None, "factory": None,
            "errors": errors,
        }

    # 1. settle: pull each sector's approved gates into dispatchable tickets
    for sector in sectors():
        base = os.path.join(DOMAINS_DIR, sector)
        code, out, err = run_cmd([sys.executable, os.path.join(base, "tracker_script.py"),
                                  "settle"], cwd=base)
        if code != 0:
            errors.append({"sector": sector, "step": "settle",
                           "detail": (err or "").strip()[-160:]})
            continue
        try:
            settled[sector] = len(json.loads(out).get("settled", []))
        except json.JSONDecodeError:
            errors.append({"sector": sector, "step": "settle",
                           "detail": "non-JSON settle output"})

    # 2. auto-ideator: propose an idea every cadence window (48 h guard). It
    #    only raises a gate — publication still needs a human.
    auto_ideator = None
    if os.path.isfile(AUTO_IDEATOR):
        code, out, err = run_cmd([sys.executable, AUTO_IDEATOR],
                                 cwd=os.path.dirname(AUTO_IDEATOR))
        auto_ideator = _short(out)
        if code != 0:
            errors.append({"sector": "ideation", "step": "auto_ideator",
                           "detail": (err or "").strip()[-160:]})

    # 3. dispatcher: wake sleeping tickets (ticket -> capsule bridge)
    code, out, err = run_cmd([sys.executable, DISPATCHER, "run"], cwd=HERE)
    run_counts = None
    if code != 0:
        errors.append({"sector": None, "step": "run",
                       "detail": (err or "").strip()[-160:]})
    else:
        try:
            run_counts = json.loads(out).get("counts", {})
        except json.JSONDecodeError:
            errors.append({"sector": None, "step": "run",
                           "detail": "non-JSON run output"})

    # 4. factory: advance every capsule (marketing -> qa -> design -> gate) and
    #    publish whatever is already approved and due.
    factory = None
    if os.path.isfile(CAPSULE_PULSE):
        code, out, err = run_cmd([sys.executable, CAPSULE_PULSE], cwd=ROOT)
        if code != 0:
            errors.append({"sector": None, "step": "capsule_pulse",
                           "detail": (err or "").strip()[-160:]})
        else:
            report = _short(out) or {}
            queue_report = report.get("queue") if isinstance(report.get("queue"), dict) else None
            factory = {
                "executed": len(report.get("executed", [])),
                "held": len(report.get("held", [])),
                "failed": len(report.get("failed", [])),
                "queue": (
                    {key: queue_report.get(key) for key in (
                        "claimed", "completed", "retried", "dead", "deferred", "reclaimed"
                    )}
                    if queue_report is not None else None
                ),
            }

    # 5. directus: synchronize Directus Kanban studio cards with sovereign factory
    directus_stat = None
    directus_bridge = os.path.join(HERE, "directus_bridge.py")
    if os.path.isfile(directus_bridge):
        code, out, err = run_cmd([sys.executable, directus_bridge], cwd=ROOT)
        if code != 0:
            errors.append({"sector": None, "step": "directus_bridge",
                           "detail": (err or "").strip()[-160:]})

    return {"at": datetime.now(timezone.utc).isoformat(),
            "settled": settled, "auto_ideator": auto_ideator,
            "run": run_counts, "factory": factory, "errors": errors}


def rotate_if_needed(path, max_bytes):
    """Size-based rotation: timestamped rename, never overwrite, race-safe."""
    try:
        if os.path.getsize(path) < max_bytes:
            return None
    except OSError:
        return None
    rotated = "%s.%s" % (path, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    try:
        os.rename(path, rotated)
        return os.path.basename(rotated)
    except OSError:
        return None                      # a concurrent beat rotated it first — just append


def main():
    rotate_if_needed(LOG, LOG_MAX_BYTES)
    result = beat()
    with open(LOG, "a", encoding="utf-8") as fh:   # the disk stays the witness
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "refused":
        # Loud, non-zero, machine-visible: a refused beat is not a quiet no-op.
        sys.stderr.write("beat refused: %s\n" % result.get("reason"))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
