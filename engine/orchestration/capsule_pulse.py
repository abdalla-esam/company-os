#!/usr/bin/env python3
"""capsule_pulse.py — one pulse pass: route, execute, chain, stop.

The capsule dispatcher: scan the spool, then for every capsule chain worker
runs — read the manifest through :class:`capsule_engine.CapsuleRouter`, hand
the decision to the matching worker from the :data:`workers.WORKERS`
registry, execute ``worker.run(capsule_path)``, and route again — until the
capsule must wait (``HOLD``: human gate, live lease, terminal state), hits an
error, or reaches the per-pulse transition cap (default 5) that bounds
pathological loops.

Contract:
- **chaining**: one pulse advances a capsule through as many consecutive
  workers as the state machine allows, then stops at the first ``HOLD`` or
  when ``max_transitions`` is reached — the rest happens next pulse;
- **queue drain**: after the sweep, due work-queue items of kind
  ``capsule.advance`` are claimed and run in priority order, bounded by
  ``AGENCY_QUEUE_DRAIN_MAX``; the drain is reported under ``queue`` and can
  never kill the sweep (kill switch: ``AGENCY_QUEUE_DRAIN=off``);
- **resolution by name**: the router returns a worker *name*; the explicit
  ``workers.WORKERS`` registry maps that name to its class (dynamic dispatch
  without ``importlib`` magic or hardcoded if/else chains);
- **one bad capsule never stops the pass** — worker failures, lock timeouts,
  stale routing decisions, unknown worker names and dispatcher-level bugs
  are all bucketed in the report and the sweep continues;
- **lock-safe by construction** — the pulse itself never locks; every
  mutation happens inside ``BaseWorker.run`` under the capsule lock,
  atomically;
- **smart freeze (TTL)**: a capsule idling in ``pending_approval`` past
  :data:`PENDING_APPROVAL_TTL_SECONDS` (48 h) is routed to the internal
  ``ttl-watchdog`` worker, which re-checks the age under the lock and moves it
  to ``stalled`` with an ``auto-frozen due to TTL`` history note;
- **autonomy demotion sweep**: a capsule sitting in ``needs_revision`` whose
  newest history entry came from ``pending_approval`` (a human gate rejection)
  demotes its domain to L0 — the worker-side hook usually gets there first;
  this sweep catches rejections recorded outside a worker;
- **read-only scan** — discovery reuses the Watcher's recursive walker, so
  nested capsules (per client) are found, and nothing is touched while
  scanning.

Report buckets (all lists, deterministic order):
    executed    — worker ran; one entry per transition;
    held        — lawful wait: human gate, live lease, terminal state;
    capped      — max transitions reached; continues next pulse;
    quarantined — manifest unreadable/corrupt (human repair);
    missing     — capsule or manifest vanished mid-pass;
    skipped     — routing went stale (another pulse advanced it first);
    no_worker   — the router named a worker absent from the registry;
    locked      — another process holds the capsule; retry next pass;
    failed      — worker raised, or a dispatcher-level error occurred;
    demoted     — a gate rejection dropped its domain to L0 (governance).

A ``profile_rejected`` field on any report entry marks a B1 task card that
was refused (spec §4.5) — reported, then ignored: the capsule proceeded on
the default tables.

Standard library only: ``argparse``, ``json``, ``os``, ``pathlib``,
``typing``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping

# Bootstrap the import paths BEFORE resolving ``shared_kernel.paths``. When this
# file is executed as a script (``python engine/orchestration/capsule_pulse.py``),
# Python puts the *script's* directory (``engine/orchestration``) on sys.path[0],
# not ``engine/`` — so ``shared_kernel`` would be unimportable and the except
# fallback below would silently pin the *repository* spool, ignoring the
# operator's ``CAPSULES_SPOOL``/``DOMAINS_DIR`` overrides. Adding the engine root
# first (exactly as ``pulse.py``/``dispatcher.py`` do) makes the canonical
# resolver honor the sovereign env vars in every execution mode.
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ENGINE_ROOT.parent
for _p in (str(_ENGINE_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from shared_kernel.paths import CAPSULES_SPOOL, DOMAINS_DIR, ENGINE_ROOT, REPO_ROOT
except Exception:
    ENGINE_ROOT = _ENGINE_ROOT
    REPO_ROOT = _REPO_ROOT
    # Fail-safe fallback: even here the sovereign overrides win, so a script
    # execution can never silently bind the repository's real spool/domains.
    CAPSULES_SPOOL = Path(
        os.environ.get("CAPSULES_SPOOL", _REPO_ROOT / "data" / "capsules" / "spool")
    )
    DOMAINS_DIR = Path(
        os.environ.get("DOMAINS_DIR", _REPO_ROOT / "data" / "domains")
    )

ROOT = REPO_ROOT

try:  # normal import (repo root on sys.path)
    from capsule_engine import (
        DEFAULT_PENDING_APPROVAL_TTL,
        GOVERNOR_WORKER,
        STALL_WATCHDOG_WORKER,
        Action,
        BaseWorker,
        CapsuleError,
        CapsuleNotFoundError,
        CapsuleRouter,
        LockTimeoutError,
        State,
        StateMismatchError,
        WorkerExecutionError,
        load_manifest,
        state_age_seconds,
    )
    from capsule_engine.governance import apply_rejection
    from orchestration.watcher import find_manifests
    from orchestration.work_queue import QueueDefer, QueueItemError, drain as drain_work_queue
    from workers import WORKERS, domain_workers, GovernorWorker
except ImportError:  # script/odd-cwd execution — mirror workers/*.py
    sys.path.insert(0, str(ROOT))
    from capsule_engine import (
        DEFAULT_PENDING_APPROVAL_TTL,
        GOVERNOR_WORKER,
        STALL_WATCHDOG_WORKER,
        Action,
        BaseWorker,
        CapsuleError,
        CapsuleNotFoundError,
        CapsuleRouter,
        LockTimeoutError,
        State,
        StateMismatchError,
        WorkerExecutionError,
        load_manifest,
        state_age_seconds,
    )
    from capsule_engine.governance import apply_rejection
    from orchestration.watcher import find_manifests
    from orchestration.work_queue import QueueDefer, QueueItemError, drain as drain_work_queue
    from workers import WORKERS, domain_workers, GovernorWorker

logger = logging.getLogger("orchestration.capsule_pulse")

DEFAULT_SPOOL = CAPSULES_SPOOL

# --------------------------------------------------------------------------- #
# Incremental scan (Scaling Brick 2) — sweep only what changed + what the
# router's time-based rules must re-examine. Fail closed: any doubt = full
# scan. Kill switch: AGENCY_INCREMENTAL_PULSE=off (or run_pulse(scan="full")).
# --------------------------------------------------------------------------- #

SCAN_STATE_NAME = ".pulse_scan.json"
MAX_SCAN_STATE_BYTES = 8 * 1024 * 1024  # 8 MiB: absurd growth ⇒ rebuild (full)


def _incremental_enabled(scan: str | None) -> bool:
    if scan is not None:
        return scan != "full"
    return os.environ.get("AGENCY_INCREMENTAL_PULSE", "").strip().lower() != "off"


def _load_scan_state(spool: Path) -> dict[str, int]:
    """Read ``{capsule_path: mtime_ns}`` from the last pass; corrupt ⇒ {} (full)."""
    state_file = spool / SCAN_STATE_NAME
    try:
        if not state_file.is_file():
            return {}
        if state_file.stat().st_size > MAX_SCAN_STATE_BYTES:
            logger.warning("pulse scan-state oversized (%s) — full sweep", state_file)
            return {}
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pulse scan-state unreadable (%s) — full sweep: %s", state_file, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): int(v)
        for k, v in raw.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _save_scan_state(spool: Path, stamps: dict[str, int]) -> None:
    """Best-effort persist of the current mtimes; a write failure is a warning
    (next pass silently degrades to a full sweep — never an error)."""
    try:
        spool.mkdir(parents=True, exist_ok=True)
        state_file = spool / SCAN_STATE_NAME
        payload = json.dumps(stamps, ensure_ascii=False)
        tmp = state_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, state_file)
    except OSError as exc:
        logger.warning("pulse scan-state not saved — next pass is a full sweep: %s", exc)


def _time_sensitive(state_value: str | None) -> bool:
    """States whose routing depends on elapsed time even when untouched:
    pending_approval ages toward the TTL freeze; in_progress leases die; an
    approved capsule may be waiting for its scheduled_for moment."""
    return state_value in (
        State.PENDING_APPROVAL.value,
        State.IN_PROGRESS.value,
        State.APPROVED.value,
    )


def _plan_incremental(spool: Path) -> tuple[list[Path], dict[str, int]] | None:
    """Return (capsules to route, fresh mtimes) or None when a full sweep is
    the honest answer (first run, missing/corrupt state, unreadable entries)."""
    previous = _load_scan_state(spool)
    if not previous:
        return None

    fresh: dict[str, int] = {}
    selected: list[Path] = []
    try:
        manifest_paths = list(find_manifests(spool))
    except OSError:
        return None

    for manifest_path in manifest_paths:
        capsule = manifest_path.parent
        try:
            mtime_ns = capsule.stat().st_mtime_ns
        except OSError:
            return None  # a vanished/unreadable capsule: full sweep is safer
        fresh[str(capsule)] = mtime_ns
        if str(capsule) not in previous:
            selected.append(capsule)  # brand new capsule
            continue
        if previous[str(capsule)] != mtime_ns:
            selected.append(capsule)  # changed since the last pass
            continue
        try:
            manifest = load_manifest(capsule)
        except Exception:
            continue  # already counted: unchanged mtime, unchanged decision
        if _time_sensitive(manifest.get("state")):
            selected.append(capsule)  # TTL/lease rules must re-examine it

    return selected, fresh

# Safety valve: how many consecutive worker runs one capsule may receive in a
# single pulse. Every real transition advances the state machine toward a
# gate/terminal; the cap only exists so a pathological loop (e.g. a buggy
# worker that never changes state) cannot spin forever in one pass.
MAX_TRANSITIONS_PER_PULSE = 5

# Smart freeze: a pending_approval capsule older than this is auto-frozen to
# ``stalled`` by the watchdog worker. Override per pass with ``pending_ttl=``
# or per router with ``CapsuleRouter(stall_after=...)``.
PENDING_APPROVAL_TTL_SECONDS = DEFAULT_PENDING_APPROVAL_TTL

# Concurrency & Parallelism: maximum concurrent worker threads during a pulse sweep.
# Configurable via AGENCY_MAX_PARALLEL_WORKERS environment variable.
DEFAULT_MAX_PARALLEL_WORKERS = 8

# The one queue kind this module knows how to run: a chained processing pass
# over one capsule (enqueued by a human, the API, or a future dispatcher).
# Items of other kinds wait for their own consumer — the drain claims only
# kinds it can actually execute.
QUEUE_KIND_CAPSULE_ADVANCE = "capsule.advance"

# A capsule locked by another process is a transient condition: defer the item
# instead of burning one of its attempts.
QUEUE_LOCK_DEFER_SECONDS = 30.0


def _queue_drain_enabled(drain: bool | None) -> bool:
    """Queue drain kill switch: explicit arg, else AGENCY_QUEUE_DRAIN (default on)."""
    if drain is not None:
        return drain
    return os.environ.get("AGENCY_QUEUE_DRAIN", "").strip().lower() != "off"


def _resolve_max_workers(max_workers: int | None) -> int:
    """Resolve parallel worker pool size from explicit arg, env, or default."""
    if max_workers is not None:
        return max(1, int(max_workers))
    env_val = os.environ.get("AGENCY_MAX_PARALLEL_WORKERS", "").strip()
    if env_val:
        try:
            return max(1, int(env_val))
        except ValueError:
            logger.warning(
                "Invalid AGENCY_MAX_PARALLEL_WORKERS=%r — falling back to %d",
                env_val,
                DEFAULT_MAX_PARALLEL_WORKERS,
            )
    return DEFAULT_MAX_PARALLEL_WORKERS


def _record_report(
    report: dict[str, Any],
    bucket: str,
    entry: dict[str, Any],
    lock: threading.Lock | None = None,
) -> None:
    """Thread-safe append of an entry into a report bucket list."""
    if lock is not None:
        with lock:
            report[bucket].append(entry)
    else:
        report[bucket].append(entry)

BUCKETS = (
    "executed",
    "held",
    "capped",
    "quarantined",
    "missing",
    "skipped",
    "no_worker",
    "locked",
    "failed",
    "demoted",
)


def stall_watchdog(ttl_seconds: float) -> Callable[[], BaseWorker]:
    """Build a zero-arg factory for the TTL freeze watchdog.

    The watchdog is infrastructure, not a pipeline stage: the router summons
    it only for a ``pending_approval`` capsule past its TTL, and the worker
    re-measures the age *under the capsule lock* before transitioning
    (closing the route-vs-execute race). It is deliberately defined here, next
    to the TTL policy that spawns it.
    """
    ttl = max(0.0, float(ttl_seconds))

    class _StallWatchdog(BaseWorker):
        """Freeze a ``pending_approval`` capsule that outlived its TTL."""

        name: ClassVar[str] = STALL_WATCHDOG_WORKER
        handles: ClassVar[frozenset[State]] = frozenset({State.PENDING_APPROVAL})
        max_attempts: ClassVar[int] = 3
        ttl_seconds: ClassVar[float] = ttl

        def execute(
            self, capsule_path: str, manifest: dict[str, Any]
        ) -> dict[str, Any]:
            age = state_age_seconds(manifest)
            if age is None or age <= ttl:
                # Not stale under the lock (raced an unfreeze/re-entry): a
                # lawful no-op — the router re-decides from the live state.
                return manifest
            self.transition(
                manifest,
                State.STALLED,
                why=(
                    f"auto-frozen due to TTL: pending_approval for {int(age)}s "
                    f"> {int(ttl)}s — human unfreeze required"
                ),
            )
            return manifest

    return _StallWatchdog


def _demote_on_gate_rejection(
    capsule: Path,
    decision: Any,
    report: dict[str, Any],
    governance_file: str | os.PathLike[str] | None = None,
    lock: threading.Lock | None = None,
) -> None:
    """Sweep a gate rejection recorded outside a worker (see governance).

    The worker-side transition hook demotes at rejection time; this catches
    manifests rejected by hand. Idempotent: an already-L0 domain is a no-op,
    so a rejected capsule never rewrites the policy file on every pulse.
    """
    try:
        manifest = load_manifest(capsule)
    except CapsuleError:
        return  # the router already surfaced the unreadable capsule
    try:
        result = apply_rejection(manifest, path=governance_file)
    except OSError as exc:  # governance write failure: report, never crash
        logger.warning(
            "capsule_pulse: governance demotion failed for %s: %s", capsule, exc
        )
        return
    if result is None:
        return
    _record_report(
        report,
        "demoted",
        {
            "capsule_path": str(capsule),
            "capsule_id": decision.capsule_id,
            "domain": result.domain,
            "previous_level": result.previous_level,
            "reason": result.reason,
        },
        lock=lock,
    )
    logger.info(
        "capsule_pulse: domain %r demoted %s -> %s after gate rejection (%s)",
        result.domain,
        result.previous_level,
        result.level,
        capsule,
    )


def _process_capsule(
    capsule: Path,
    router: CapsuleRouter,
    registry: Mapping[str, Callable[[], BaseWorker]],
    report: dict[str, Any],
    max_transitions: int,
    lock: threading.Lock | None = None,
) -> None:
    """Chain workers for one capsule until HOLD / error / the cap."""
    transitions = 0
    while True:
        decision = router.route(capsule)
        base = {
            "capsule_path": str(capsule),
            "capsule_id": decision.capsule_id,
            "state": decision.state.value if decision.state else None,
        }

        if decision.state is State.NEEDS_REVISION:
            _demote_on_gate_rejection(
                capsule,
                decision,
                report,
                governance_file=router.governance_file,
                lock=lock,
            )

        if decision.action is Action.HOLD:
            entry = {**base, "reason": decision.reason}
            if decision.profile_rejected:
                entry["profile_rejected"] = decision.profile_rejected
            _record_report(report, "held", entry, lock=lock)
            return
        if decision.action is Action.QUARANTINE:
            _record_report(
                report,
                "quarantined",
                {"capsule_path": str(capsule), "reason": decision.reason},
                lock=lock,
            )
            return
        if decision.action is Action.MISSING:
            _record_report(
                report,
                "missing",
                {"capsule_path": str(capsule), "reason": decision.reason},
                lock=lock,
            )
            return

        # Action.EXECUTE from here on — should_execute guarantees a name.
        worker_name = decision.worker
        worker_cls = registry.get(worker_name) if worker_name else None
        if worker_cls is None:
            entry = {**base, "worker": worker_name, "reason": decision.reason}
            if decision.profile_rejected:
                entry["profile_rejected"] = decision.profile_rejected
            _record_report(report, "no_worker", entry, lock=lock)
            return

        entry = {**base, "worker": worker_name}
        if decision.profile_rejected:
            entry["profile_rejected"] = decision.profile_rejected
        try:
            worker = worker_cls()
            if router.governance_file is not None:
                # Bind this pass's policy file so transition-time hooks record
                # the autonomy decision in the same file the router reads.
                worker.governance_path = router.governance_file
            result = worker.run(str(capsule))
        except LockTimeoutError as exc:
            _record_report(report, "locked", {**entry, "reason": str(exc)}, lock=lock)
            return
        except StateMismatchError as exc:
            # Another pulse won the race and advanced the capsule first.
            _record_report(report, "skipped", {**entry, "reason": str(exc)}, lock=lock)
            return
        except CapsuleNotFoundError as exc:
            _record_report(report, "missing", {**entry, "reason": str(exc)}, lock=lock)
            return
        except WorkerExecutionError as exc:
            _record_report(
                report,
                "failed",
                {
                    **entry,
                    "error": str(exc),
                    "cause": type(exc.__cause__).__name__ if exc.__cause__ else None,
                },
                lock=lock,
            )
            return
        except CapsuleError as exc:  # any other engine-level refusal
            _record_report(
                report,
                "failed",
                {**entry, "error": f"{type(exc).__name__}: {exc}", "cause": None},
                lock=lock,
            )
            return
        except Exception as exc:  # defensive: a buggy worker never stops the pass
            _record_report(
                report,
                "failed",
                {
                    **entry,
                    "error": f"{type(exc).__name__}: {exc}",
                    "cause": type(exc).__name__,
                },
                lock=lock,
            )
            return

        _record_report(
            report,
            "executed",
            {**entry, "to_state": result.get("state"), "reason": decision.reason},
            lock=lock,
        )
        transitions += 1
        if transitions >= max_transitions:
            _record_report(
                report,
                "capped",
                {
                    **entry,
                    "reason": (
                        f"max {max_transitions} transition(s) per pulse reached "
                        "— continues next pulse"
                    ),
                },
                lock=lock,
            )
            return


def _build_registry(
    active_router: CapsuleRouter,
    workers: Mapping[str, Callable[[], BaseWorker]] | None,
    governance_file: str | os.PathLike[str] | None,
) -> dict[str, Callable[[], BaseWorker]]:
    """Layer the worker registry: explicit > branch-owned > core, plus infra.

    Core workers, then branch-owned workers discovered from
    ``domains/<name>/worker.py`` (a branch is the more specific owner and may
    override a same-named core tool); an explicit ``workers=`` argument wins
    over both. Infrastructure workers (TTL watchdog, governor) bind last.
    """
    if workers is None:
        registry: dict[str, Callable[[], BaseWorker]] = dict(WORKERS)
        for name, cls in domain_workers().items():
            registry.setdefault(name, cls)
    else:
        registry = dict(workers)
    registry.setdefault(
        STALL_WATCHDOG_WORKER, stall_watchdog(active_router.stall_after)
    )
    if governance_file is not None:
        registry[GOVERNOR_WORKER] = lambda: GovernorWorker(
            governance_path=governance_file
        )
    else:
        registry.setdefault(GOVERNOR_WORKER, GovernorWorker)
    return registry


def _queued_capsule_handler(
    router: CapsuleRouter,
    registry: Mapping[str, Callable[[], BaseWorker]],
    max_transitions: int,
    lock: threading.Lock,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Handler for ``capsule.advance``: one chained pass over one capsule.

    Success is "the pass executed or lawfully held" (queued work cannot do
    more than a pulse can); a worker failure raises so the queue retries with
    backoff; a capsule locked elsewhere defers without consuming an attempt;
    a vanished or unreadable capsule completes with a note, because retrying
    cannot repair what a human must fix first.
    """

    def handler(item: dict[str, Any]) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        raw = payload.get("capsule_path") or payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise QueueItemError("capsule.advance requires payload.capsule_path")
        capsule = Path(raw)
        sub: dict[str, Any] = {bucket: [] for bucket in BUCKETS}
        _process_capsule(capsule, router, registry, sub, max_transitions, lock=lock)

        capsule_id = next(
            (
                sub[bucket][0].get("capsule_id")
                for bucket in (
                    "executed",
                    "held",
                    "capped",
                    "no_worker",
                    "locked",
                    "failed",
                    "missing",
                    "quarantined",
                    "skipped",
                )
                if sub[bucket]
            ),
            None,
        )
        if sub["failed"]:
            raise QueueItemError(str(sub["failed"][0].get("error") or "worker failed"))
        if sub["locked"]:
            raise QueueDefer(
                QUEUE_LOCK_DEFER_SECONDS,
                reason="capsule is locked by another process",
            )
        return {
            "capsule_id": capsule_id,
            "capsule_path": str(capsule),
            "executed": len(sub["executed"]),
            "held": len(sub["held"]),
            "capped": len(sub["capped"]),
            "missing": len(sub["missing"]),
            "quarantined": len(sub["quarantined"]),
            "no_worker": len(sub["no_worker"]),
            "skipped": len(sub["skipped"]),
        }

    return handler


def _drain_queued_capsules(
    active_router: CapsuleRouter,
    registry: Mapping[str, Callable[[], BaseWorker]],
    *,
    max_transitions: int,
    concurrency: int,
    limit: int | None = None,
    lease_seconds: float | None = None,
) -> dict[str, Any]:
    """One queue drain pass over ``capsule.advance`` (see orchestration.work_queue)."""
    handler = _queued_capsule_handler(
        active_router, registry, max_transitions, threading.Lock()
    )
    return drain_work_queue(
        {QUEUE_KIND_CAPSULE_ADVANCE: handler},
        limit=limit,
        lease_seconds=lease_seconds,
        concurrency=concurrency,
    )


def _queue_step(
    active_router: CapsuleRouter,
    registry: Mapping[str, Callable[[], BaseWorker]],
    *,
    enabled: bool,
    max_transitions: int,
    concurrency: int,
    limit: int | None,
) -> dict[str, Any] | None:
    """The drain step as the pulse uses it: bounded, and never fatal.

    A queue-side failure (a full disk, a corrupted root) is reported in the
    pass report, never allowed to kill the spool sweep — the same rule every
    other step follows.
    """
    if not enabled:
        return None
    try:
        return _drain_queued_capsules(
            active_router,
            registry,
            max_transitions=max_transitions,
            concurrency=concurrency,
            limit=limit,
        )
    except Exception as exc:  # defensive: the queue must never stop the pass
        logger.warning("capsule_pulse: queue drain failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def drain_capsule_queue(
    *,
    router: CapsuleRouter | None = None,
    workers: Mapping[str, Callable[[], BaseWorker]] | None = None,
    governance_file: str | os.PathLike[str] | None = None,
    pending_ttl: float = PENDING_APPROVAL_TTL_SECONDS,
    max_transitions: int = MAX_TRANSITIONS_PER_PULSE,
    limit: int | None = None,
    lease_seconds: float | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Run only the queue drain — no spool sweep (the API's "run due work now").

    Builds the same router/registry a pulse would use, then runs up to
    ``limit`` due ``capsule.advance`` items in priority order.
    """
    if max_transitions < 1:
        raise ValueError("max_transitions must be >= 1")
    active_router = (
        router
        if router is not None
        else CapsuleRouter(stall_after=pending_ttl, governance_file=governance_file)
    )
    registry = _build_registry(active_router, workers, governance_file)
    return _drain_queued_capsules(
        active_router,
        registry,
        max_transitions=max_transitions,
        concurrency=_resolve_max_workers(max_workers),
        limit=limit,
        lease_seconds=lease_seconds,
    )


def run_pulse(
    spool: str | os.PathLike[str] = DEFAULT_SPOOL,
    *,
    router: CapsuleRouter | None = None,
    workers: Mapping[str, Callable[[], BaseWorker]] | None = None,
    max_transitions: int = MAX_TRANSITIONS_PER_PULSE,
    pending_ttl: float = PENDING_APPROVAL_TTL_SECONDS,
    governance_file: str | os.PathLike[str] | None = None,
    scan: str | None = None,
    max_workers: int | None = None,
    drain: bool | None = None,
    queue_limit: int | None = None,
) -> dict[str, Any]:
    """Run one routing + execution pass over ``spool`` and return the report.

    Each capsule is chained through consecutive workers until it holds,
    fails, or reaches ``max_transitions``.

    Args:
        scan: ``"full"`` forces the complete sweep; ``"incremental"`` (or any
            non-``full`` value) routes only new, changed, or time-sensitive
            capsules (pending_approval TTL / in_progress lease rules),
            failing closed to a full sweep whenever the last-pass state is
            missing or suspect. ``None`` follows the
            ``AGENCY_INCREMENTAL_PULSE`` env (default: incremental).

    Args:
        spool: capsule spool root — recursively scanned for ``manifest.json``.
        router: optional pre-configured :class:`CapsuleRouter` (custom stale
            lease TTL or routing-table overrides). When given, its own
            ``stall_after``/``governance_file`` win over the arguments below.
        workers: optional registry override; defaults to ``workers.WORKERS``.
            Values may be worker classes or zero-arg factories.
        max_transitions: per-capsule safety cap for one pulse (>= 1).
        pending_ttl: seconds a capsule may sit in ``pending_approval`` before
            the TTL watchdog freezes it (default 48 h).
        governance_file: optional ``governance.json`` override for autonomy;
            when set, it also binds the governor worker for this pass (the
            governor verifies the same policy the router read).
        max_workers: maximum parallel worker threads for this pass. Defaults to
            ``AGENCY_MAX_PARALLEL_WORKERS`` or 8. If set to 1, executes serially.
        drain: run the work-queue drain step for this pass (``capsule.advance``
            items in priority order). ``None`` follows ``AGENCY_QUEUE_DRAIN``
            (default: on); ``False`` skips it for this pass.
        queue_limit: cap on items run by the drain step (default: the queue's
            ``AGENCY_QUEUE_DRAIN_MAX``).

    Never raises for a capsule-level problem — everything is bucketed. A
    misconfigured ``max_transitions`` is a programming error and does raise.
    """
    if max_transitions < 1:
        raise ValueError("max_transitions must be >= 1")

    concurrency = _resolve_max_workers(max_workers)
    spool_path = Path(spool)
    active_router = (
        router
        if router is not None
        else CapsuleRouter(stall_after=pending_ttl, governance_file=governance_file)
    )
    registry = _build_registry(active_router, workers, governance_file)
    queue_enabled = _queue_drain_enabled(drain)

    # --- Incremental planning (after router/registry exist): only new,
    # --- changed, or time-sensitive capsules are routed; any doubt (first
    # --- run, missing/corrupt scan state, unreadable capsule) falls through
    # --- to the honest full sweep below.
    incremental = _incremental_enabled(scan)
    if incremental:
        planned = _plan_incremental(spool_path)
        if planned is not None:
            selected, fresh = planned
            inc_report: dict[str, Any] = {
                "spool": str(spool_path),
                "scanned": len(selected),
                "scan_mode": "incremental",
                "total_capsules": len(fresh),
                "max_workers": concurrency,
            }
            for bucket in BUCKETS:
                inc_report[bucket] = []

            if concurrency <= 1 or len(selected) <= 1:
                for capsule in selected:
                    try:
                        _process_capsule(
                            capsule,
                            active_router,
                            registry,
                            inc_report,
                            max_transitions,
                        )
                    except Exception as exc:  # same dispatcher-level guard as full
                        inc_report["failed"].append(
                            {
                                "capsule_path": str(capsule),
                                "capsule_id": None,
                                "state": None,
                                "worker": None,
                                "error": f"dispatcher error: {type(exc).__name__}: {exc}",
                                "cause": type(exc).__name__,
                            }
                        )
            else:
                rep_lock = threading.Lock()

                def _inc_task(cap: Path) -> None:
                    try:
                        _process_capsule(
                            cap,
                            active_router,
                            registry,
                            inc_report,
                            max_transitions,
                            lock=rep_lock,
                        )
                    except Exception as exc:
                        _record_report(
                            inc_report,
                            "failed",
                            {
                                "capsule_path": str(cap),
                                "capsule_id": None,
                                "state": None,
                                "worker": None,
                                "error": f"dispatcher error: {type(exc).__name__}: {exc}",
                                "cause": type(exc).__name__,
                            },
                            lock=rep_lock,
                        )

                with ThreadPoolExecutor(
                    max_workers=concurrency, thread_name_prefix="pulse-worker"
                ) as executor:
                    list(executor.map(_inc_task, selected))

            _save_scan_state(spool_path, fresh)
            inc_report["queue"] = _queue_step(
                active_router,
                registry,
                enabled=queue_enabled,
                max_transitions=max_transitions,
                concurrency=concurrency,
                limit=queue_limit,
            )
            return inc_report
        # planned is None → first run / suspect state: the honest answer is
        # the full sweep, and its result seeds the scan state below.

    root = spool_path

    manifest_paths = list(find_manifests(root))
    report: dict[str, Any] = {
        "spool": str(root),
        "scanned": len(manifest_paths),
        "scan_mode": "full",
        "max_workers": concurrency,
    }
    for bucket in BUCKETS:
        report[bucket] = []

    stamps: dict[str, int] = {}
    capsules: list[Path] = []
    for manifest_path in manifest_paths:
        capsule = manifest_path.parent
        capsules.append(capsule)
        try:
            stamps[str(capsule)] = capsule.stat().st_mtime_ns
        except OSError:
            pass

    if concurrency <= 1 or len(capsules) <= 1:
        for capsule in capsules:
            try:
                _process_capsule(
                    capsule, active_router, registry, report, max_transitions
                )
            except Exception as exc:  # dispatcher-level guard: one capsule can never
                # kill the pass, even if routing/instantiation itself explodes.
                report["failed"].append(
                    {
                        "capsule_path": str(capsule),
                        "capsule_id": None,
                        "state": None,
                        "worker": None,
                        "error": f"dispatcher error: {type(exc).__name__}: {exc}",
                        "cause": type(exc).__name__,
                    }
                )
    else:
        rep_lock = threading.Lock()

        def _full_task(cap: Path) -> None:
            try:
                _process_capsule(
                    cap,
                    active_router,
                    registry,
                    report,
                    max_transitions,
                    lock=rep_lock,
                )
            except Exception as exc:
                _record_report(
                    report,
                    "failed",
                    {
                        "capsule_path": str(cap),
                        "capsule_id": None,
                        "state": None,
                        "worker": None,
                        "error": f"dispatcher error: {type(exc).__name__}: {exc}",
                        "cause": type(exc).__name__,
                    },
                    lock=rep_lock,
                )

        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="pulse-worker"
        ) as executor:
            list(executor.map(_full_task, capsules))

    if incremental:
        _save_scan_state(spool_path, stamps)
    report["queue"] = _queue_step(
        active_router,
        registry,
        enabled=queue_enabled,
        max_transitions=max_transitions,
        concurrency=concurrency,
        limit=queue_limit,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: run one pass and print the JSON report."""
    parser = argparse.ArgumentParser(
        description=(
            "Run one capsule pulse pass: route every capsule in the spool, "
            "chain consecutive workers, stop at gates."
        )
    )
    parser.add_argument(
        "--spool",
        default=str(DEFAULT_SPOOL),
        help="capsule spool root (default: %(default)s)",
    )
    parser.add_argument(
        "--max-transitions",
        type=int,
        default=MAX_TRANSITIONS_PER_PULSE,
        help="max worker runs per capsule in this pass (default: %(default)s)",
    )
    parser.add_argument(
        "--pending-ttl",
        type=float,
        default=PENDING_APPROVAL_TTL_SECONDS,
        help=(
            "seconds a capsule may wait in pending_approval before auto-freeze "
            "(default: %(default)s = 48 h)"
        ),
    )
    parser.add_argument(
        "--governance-file",
        default=None,
        help="governance.json path (default: module/env resolution)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="max concurrent workers for parallel pulse (default: from AGENCY_MAX_PARALLEL_WORKERS or 8)",
    )
    parser.add_argument(
        "--no-drain-queue",
        action="store_true",
        help="skip the work-queue drain step for this pass",
    )
    parser.add_argument(
        "--drain-only",
        action="store_true",
        help="run only the work-queue drain (no spool sweep)",
    )
    parser.add_argument(
        "--queue-limit",
        type=int,
        default=None,
        help="max queue items to run this pass (default: AGENCY_QUEUE_DRAIN_MAX or 16)",
    )
    args = parser.parse_args(argv)
    if args.drain_only:
        report = drain_capsule_queue(
            governance_file=args.governance_file,
            max_transitions=args.max_transitions,
            limit=args.queue_limit,
            max_workers=args.max_workers,
        )
        print(json.dumps(report, indent=2))
        return 0
    report = run_pulse(
        args.spool,
        max_transitions=args.max_transitions,
        pending_ttl=args.pending_ttl,
        governance_file=args.governance_file,
        max_workers=args.max_workers,
        drain=False if args.no_drain_queue else None,
        queue_limit=args.queue_limit,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
