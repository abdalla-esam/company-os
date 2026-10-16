#!/usr/bin/env python3
"""dispatcher.py — the engine that wakes sleeping tickets. One pass, no daemon.

Contract: orchestration/architecture.md (Brick AA, approved).
Rules: the claim is the lock (atomic rename) · silence is not a result ·
rejections carry reasons · a claim is a lease · the dispatcher never
executes · fail closed on the unknown.

COST & LLM EXECUTION BOUNDARY:
The dispatcher itself is cost-neutral orchestration (scanning and atomic claims),
BUT each handoff calls a sector worker (``worker_script.py``). Sector workers
are COST-BEARING:
- Marketing worker calls Groq / LLM to draft copy.
- Design worker calls image generation model APIs.
- Ideation worker creates capsules and calls models.
When the dispatcher runs as step 3 of ``pulse.beat()``, every active ticket
in ``tickets/approved/`` with state in ``DISPATCHABLE`` (open, pending, needs_revision)
is executed immediately. Therefore, automated/scheduled dispatcher execution
MUST remain gated behind cost caps and kill switches.

Reading the worker's stdout is an external call: the JSON is parsed
with a Pydantic BaseModel (HandoffResult) so a malformed answer fails
closed instead of silently corrupting the ticket. Every external call
also carries the Tenacity retry profile; a flapping worker never kills
the pass — the ticket falls back to its prior dispatchable state.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

# Bootstrap the engine directory from the known layout (dispatcher lives in
# engine/orchestration/) so shared_kernel is importable without caller PYTHONPATH.
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

try:
    from shared_kernel.paths import DOMAINS_DIR as _CANONICAL_DOMAINS_DIR, REPO_ROOT as _CANONICAL_REPO_ROOT
    DOMAINS_DIR = str(_CANONICAL_DOMAINS_DIR)
    ROOT = str(_CANONICAL_REPO_ROOT)
except Exception:  # pragma: no cover - stripped environments
    _REPO_ROOT = os.path.dirname(_ENGINE_DIR)
    DOMAINS_DIR = os.environ.get("DOMAINS_DIR", os.path.join(_REPO_ROOT, "data", "domains"))
    ROOT = _REPO_ROOT

try:
    from shared_kernel.state_schema import HandoffResult
except ImportError:  # sandbox / copied layout
    from state_schema import HandoffResult

# Per-item fail closed: a ticket whose schema_version this build does not
# understand is SKIPPED and reported — it never crashes a listing, a reclaim
# pass or a whole dispatch run (unknown != corrupt != ours to rewrite).
try:
    from shared_kernel.schema_version import SchemaVersionError
except ImportError:  # pragma: no cover - stripped environments
    class SchemaVersionError(ValueError):
        """Placeholder mirroring shared_kernel.schema_version.SchemaVersionError."""

#: Everything that means "this file cannot be safely dispatched right now".
TICKET_READ_ERRORS = (OSError, json.JSONDecodeError, SchemaVersionError)

# Crash-safe writes come from the shared kernel's one helper.
try:
    from shared_kernel.atomic_io import atomic_json, update_json
except ImportError:  # pragma: no cover - stripped environments
    # atomic_io is pure standard library: load it by path so the write-safety
    # guarantee never depends on an installed wheel.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "shared_kernel_atomic_io",
        os.path.join(_ENGINE_DIR, "shared_kernel", "atomic_io.py"),
    )
    if _spec is None or _spec.loader is None:
        raise
    _atomic_io = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_atomic_io)
    atomic_json = _atomic_io.atomic_json
    update_json = _atomic_io.update_json
WORKER_SCRIPT = "agent_script.py"      # fallback: the classify script
WORKER_EXECUTOR = "worker_script.py"   # preferred: the executor (speaks the handoff)
ATTEMPT_CAP = 3                     # consecutive failed handoffs -> human
LEASE_TTL = 30 * 60                 # a claim older than this is stale
HANDOFF_TIMEOUT = 30                # seconds per worker call
DISPATCHABLE = ("pending", "needs_revision", "open")   # open = legacy pending


def now_utc():
    return datetime.now(timezone.utc)


def stamp():
    return now_utc().isoformat()


def parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---------- disk primitives: the claim IS the lock ----------

def grab(path):
    """Atomic claim: rename the ticket to its .claim name. None = lost the race."""
    claim = path + ".claim"
    try:
        os.rename(path, claim)
    except OSError:
        return None
    return claim


def load(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        from shared_kernel.schema_version import KIND_TICKET, check_schema_version

        check_schema_version(KIND_TICKET, data, path)
    except ImportError:
        pass
    return data


def put(claim, orig, ticket, base=None):
    """Write the mutated ticket back (atomic) and release the lock.

    The write is a lock-serialized read-modify-write: only the fields this
    writer actually changed (``ticket`` vs ``base``) win, so a context PATCH
    or tracker update that landed while we held the claim survives. The claim
    itself stays exactly what it is — an ``os.rename`` arbitration.
    """
    try:
        from shared_kernel.schema_version import KIND_TICKET, stamp_schema_version

        stamp_schema_version(KIND_TICKET, ticket)
    except ImportError:
        pass

    if base is None and claim and os.path.exists(claim):
        try:
            # The claim file still holds the untouched original we loaded,
            # making it the base of the three-way merge below.
            base = load(claim)
        except TICKET_READ_ERRORS:
            base = None
    update_json(orig, lambda disk: _merge_ticket(base, ticket, disk),
                trailing_newline=False)
    if claim and os.path.exists(claim):
        os.remove(claim)


def save(path, ticket, base=None):
    """Atomic write while already holding the lease (merge-safe)."""
    put(None, path, ticket, base=base)


def _merge_ticket(base, mine, theirs):
    """Field-level three-way merge of one ticket rewrite.

    ``base`` is the ticket as this writer read it, ``mine`` is that copy after
    this writer's mutation, ``theirs`` is what is on disk right now (``None``
    if the file is gone). Only the fields this writer changed win; everything
    another writer added, changed or removed since the read survives. A blind
    overwrite here is exactly how the API, the dispatcher and the trackers
    used to lose each other's updates.
    """
    if not isinstance(theirs, dict):
        return mine
    if not isinstance(base, dict):
        base = {}
    merged = dict(theirs)
    for key, value in mine.items():
        if key not in base or base.get(key) != value:
            merged[key] = value
    for key in base:
        if key not in mine:
            merged.pop(key, None)
    return merged


def add_history(ticket, frm, to, by, why):
    ticket.setdefault("history", []).append(
        {"at": stamp(), "from": frm, "to": to, "by": by, "why": why})


def release_lease(ticket):
    ticket.pop("claimed_by", None)
    ticket.pop("claimed_at", None)


# ---------- scanning ----------

def sectors():
    if not os.path.isdir(DOMAINS_DIR):
        return
    for name in sorted(os.listdir(DOMAINS_DIR)):
        tdir = os.path.join(DOMAINS_DIR, name, "tickets")
        if os.path.isdir(tdir):
            yield name, tdir


def ticket_files(tdir):
    try:
        return sorted(f for f in os.listdir(tdir)
                      if f.startswith("ticket_") and f.endswith(".json")
                      and "sync-conflict" not in f
                      and os.path.isfile(os.path.join(tdir, f)))
    except OSError:
        return []


def iter_tickets(stage="approved"):
    """Yield (sector, full_path) for tickets matching the requested stage.

    stage="approved" (production default):
      - checks tdir/approved/ (if exists)
      - checks legacy tdir/ (root of tickets, excluding subdirectories)
    stage="scratch":
      - checks tdir/scratch/ (if exists)
    stage="all":
      - checks approved, scratch, and root
    """
    for sector, tdir in sectors():
        approved_dir = os.path.join(tdir, "approved")
        scratch_dir = os.path.join(tdir, "scratch")

        if stage in ("approved", "all"):
            if os.path.isdir(approved_dir):
                for name in ticket_files(approved_dir):
                    yield sector, os.path.join(approved_dir, name)
            for name in ticket_files(tdir):
                p = os.path.join(tdir, name)
                if os.path.isfile(p):
                    yield sector, p

        if stage in ("scratch", "all"):
            if os.path.isdir(scratch_dir):
                for name in ticket_files(scratch_dir):
                    yield sector, os.path.join(scratch_dir, name)


def worker_path(sector):
    """Prefer the executor; fall back to the classify script (sectors upgrade one at a time)."""
    base = os.path.join(DOMAINS_DIR, sector)
    executor = os.path.join(base, WORKER_EXECUTOR)
    return executor if os.path.isfile(executor) else os.path.join(base, WORKER_SCRIPT)


# ---------- the handoff (one-way bridge: dispatcher -> sector worker) ----------

def handoff(sector, ticket):
    """Call the sector script with the handoff contract. Returns (result, error)."""
    payload = {"ticket_id": ticket["ticket_id"], "sector": sector,
               "client_id": ticket.get("client_id"),
               "request": ticket.get("request"),
               "via": ticket.get("via"),
               "attempt": ticket.get("attempts")}
    if ticket.get("revision_note"):
        payload["revision_note"] = ticket["revision_note"]
    if ticket.get("context"):
        payload["context"] = ticket["context"]
    worker = worker_path(sector)
    try:
        proc = subprocess.run(
            [sys.executable, worker],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, timeout=HANDOFF_TIMEOUT,
            cwd=os.path.dirname(worker))
    except subprocess.TimeoutExpired:
        return None, "worker timeout after %ss" % HANDOFF_TIMEOUT
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        tail = (" : " + lines[-1][:120]) if lines else ""
        return None, "worker exited %s%s" % (proc.returncode, tail)
    out = proc.stdout.strip()
    if not out:
        return None, "worker silence (no output) — silence is not a result"
    try:
        payload = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None, "worker spoke non-JSON"
    if not isinstance(payload, dict):
        return None, "worker spoke a JSON value that is not an object"
    try:
        result = HandoffResult(**payload)
    except Exception as exc:  # ValidationError and friends -> fail closed
        return None, "worker spoke nonconforming result: %s" % exc
    if result.result in ("done", "pending_approval", "approved", "published"):
        res = {"result": result.result, "notes": result.notes}
        if result.context is not None:
            res["context"] = result.context
        if result.published_at is not None:
            res["published_at"] = result.published_at
        return res, None
    if result.result == "needs_revision":
        return {"result": "needs_revision", "reason": result.reason,
                "notes": result.notes}, None
    return None, "nonconforming result (need result=done|needs_revision|pending_approval|approved|published, reason mandatory for needs_revision)"


# ---------- one ticket through the machine ----------

def dispatch_one(sector, orig, events):
    tid = os.path.basename(orig)
    if not os.path.isfile(worker_path(sector)):
        events.append({"sector": sector, "ticket": tid, "outcome": "skipped",
                       "detail": "no worker script — infra gap, ticket untouched"})
        return
    claim = grab(orig)
    if claim is None:
        events.append({"sector": sector, "ticket": tid, "outcome": "locked",
                       "detail": "another dispatcher holds the claim"})
        return
    try:
        ticket = load(claim)
    except SchemaVersionError as exc:
        os.rename(claim, orig)          # never destroy what we cannot read
        events.append({"sector": sector, "ticket": tid, "outcome": "skipped",
                       "detail": "unsupported schema_version — fail-closed, "
                                 "restored untouched: %s" % exc})
        return
    except (OSError, json.JSONDecodeError):
        os.rename(claim, orig)          # never destroy what we cannot read
        events.append({"sector": sector, "ticket": tid, "outcome": "skipped",
                       "detail": "unreadable ticket — restored untouched"})
        return
    if ticket.get("state") not in DISPATCHABLE:
        os.rename(claim, orig)          # race guard: state moved under us
        events.append({"sector": sector, "ticket": tid, "outcome": "skipped",
                       "detail": "state changed under the race: %s" % ticket.get("state")})
        return

    frm = ticket.get("state")
    ticket["state"] = "in_progress"
    ticket["claimed_by"] = "dispatcher"
    ticket["claimed_at"] = stamp()
    ticket["attempts"] = int(ticket.get("attempts") or 0) + 1
    add_history(ticket, frm, "in_progress", "dispatcher",
                "claim for handoff (attempt %s)" % ticket["attempts"])
    put(claim, orig, ticket)
    tid = ticket.get("ticket_id", tid)

    result, error = handoff(sector, ticket)
    on_disk = load(orig)
    on_disk_base = copy.deepcopy(on_disk)  # merge base for the writes below
    if error is not None:                        # failed attempt — rule 2
        attempts = int(on_disk.get("attempts") or 0)
        if attempts >= ATTEMPT_CAP:
            on_disk["state"] = "needs_revision"
            on_disk["revision_note"] = {"by": "dispatcher", "at": stamp(),
                                        "reason": "attempt cap %s reached: %s"
                                                  % (ATTEMPT_CAP, error)}
            on_disk["attempts"] = 0
            add_history(on_disk, "in_progress", "needs_revision", "dispatcher",
                        "fail-closed to a human: %s" % error)
            outcome = "needs_revision"
        else:
            back = "needs_revision" if frm == "needs_revision" else "pending"
            on_disk["state"] = back
            add_history(on_disk, "in_progress", back, "dispatcher",
                        "failed attempt %s: %s" % (attempts, error))
            outcome = "failed_attempt"
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid, "outcome": outcome,
                       "detail": error})
        return
    if result["result"] == "done":
        on_disk["state"] = "done"
        on_disk["done_at"] = stamp()
        on_disk["attempts"] = 0
        if result.get("context"):
            if "context" not in on_disk or not isinstance(on_disk["context"], dict):
                on_disk["context"] = {}
            on_disk["context"].update(result["context"])
        add_history(on_disk, "in_progress", "done", "worker:%s" % sector,
                    result.get("notes") or "worker reported done")
        if on_disk.get("spawn_next") and not on_disk.get("spawned"):
            spawn_event = process_spawn(sector, orig, on_disk, notes=result.get("notes"))
            if spawn_event:
                events.append(spawn_event)
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid, "outcome": on_disk["state"],
                       "detail": result.get("notes") or ""})
    elif result["result"] == "pending_approval":
        on_disk["state"] = "pending_approval"
        on_disk["attempts"] = 0
        if result.get("context"):
            if "context" not in on_disk or not isinstance(on_disk["context"], dict):
                on_disk["context"] = {}
            on_disk["context"].update(result["context"])
        add_history(on_disk, "in_progress", "pending_approval", "worker:%s" % sector,
                    result.get("notes") or "worker reported pending_approval")
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid, "outcome": on_disk["state"],
                       "detail": result.get("notes") or ""})
    elif result["result"] == "approved":
        on_disk["state"] = "approved"
        on_disk["attempts"] = 0
        if result.get("context"):
            if "context" not in on_disk or not isinstance(on_disk["context"], dict):
                on_disk["context"] = {}
            on_disk["context"].update(result["context"])
        add_history(on_disk, "in_progress", "approved", "worker:%s" % sector,
                    result.get("notes") or "worker reported approved")
        if on_disk.get("spawn_next") and not on_disk.get("spawned"):
            spawn_event = process_spawn(sector, orig, on_disk, notes=result.get("notes"))
            if spawn_event:
                events.append(spawn_event)
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid, "outcome": on_disk["state"],
                       "detail": result.get("notes") or ""})
    elif result["result"] == "published":
        on_disk["state"] = "published"
        on_disk["published_at"] = result.get("published_at") or stamp()
        on_disk["attempts"] = 0
        if result.get("context"):
            if "context" not in on_disk or not isinstance(on_disk["context"], dict):
                on_disk["context"] = {}
            on_disk["context"].update(result["context"])
        add_history(on_disk, "in_progress", "published", "worker:%s" % sector,
                    result.get("notes") or "worker reported published")
        if on_disk.get("spawn_next") and not on_disk.get("spawned"):
            spawn_event = process_spawn(sector, orig, on_disk, notes=result.get("notes"))
            if spawn_event:
                events.append(spawn_event)
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid, "outcome": on_disk["state"],
                       "detail": result.get("notes") or ""})
    else:                                        # worker demanded revision
        on_disk["state"] = "needs_revision"
        on_disk["revision_note"] = {"by": "worker", "at": stamp(),
                                    "reason": result["reason"]}
        on_disk["attempts"] = 0
        add_history(on_disk, "in_progress", "needs_revision",
                    "worker:%s" % sector, result["reason"])
        release_lease(on_disk)
        save(orig, on_disk, base=on_disk_base)
        events.append({"sector": sector, "ticket": tid,
                       "outcome": "needs_revision", "detail": result["reason"]})


# ---------- recovery: no ticket dies in progress ----------

def recover_orphans(events):
    """A .claim with no original is a stranded claim (crash mid-handoff)."""
    for sector, tdir in sectors():
        for name in sorted(os.listdir(tdir)):
            if not name.endswith(".json.claim"):
                continue
            claim = os.path.join(tdir, name)
            orig = claim[:-len(".claim")]
            if os.path.exists(orig):             # leftover lock; original is truth
                os.remove(claim)
                events.append({"sector": sector, "ticket": name,
                               "outcome": "recovered_orphan",
                               "detail": "stale .claim removed (original is truth)"})
                continue
            if time.time() - os.path.getmtime(claim) < LEASE_TTL:
                continue                          # possibly still in flight
            try:
                ticket = load(claim)              # rescue the stranded ticket
            except TICKET_READ_ERRORS as exc:
                # Fail closed per item: restore the claim to its original
                # name (never destroy) and report; the pass continues.
                os.rename(claim, orig)
                events.append({"sector": sector, "ticket": name,
                               "outcome": "skipped",
                               "detail": "stranded claim unreadable — restored "
                                         "untouched: %s" % exc})
                continue
            add_history(ticket, ticket.get("state"), ticket.get("state"),
                        "dispatcher", "recovered from a stranded claim file")
            put(claim, orig, ticket)
            events.append({"sector": sector, "ticket": name,
                           "outcome": "recovered_stranded",
                           "detail": "ticket rescued from .claim"})


def reclaim_stale(events):
    """A lease older than TTL is dead — its ticket goes back to pending."""
    for sector, path in iter_tickets():
        try:
            ticket = load(path)
        except TICKET_READ_ERRORS as exc:
            events.append({"sector": sector, "ticket": os.path.basename(path),
                           "outcome": "skipped",
                           "detail": "cannot reclaim (fail-closed): %s" % exc})
            continue
        if ticket.get("state") != "in_progress":
            continue
        claimed_at = parse_ts(ticket.get("claimed_at"))
        if claimed_at and (now_utc() - claimed_at).total_seconds() < LEASE_TTL:
            continue
        claim = grab(path)
        if claim is None:
            continue
        try:
            fresh = load(claim)
        except TICKET_READ_ERRORS as exc:
            os.rename(claim, path)                # never destroy what we cannot read
            events.append({"sector": sector, "ticket": os.path.basename(path),
                           "outcome": "skipped",
                           "detail": "claimed ticket unreadable — restored "
                                     "untouched: %s" % exc})
            continue
        ca = parse_ts(fresh.get("claimed_at"))
        if ca and (now_utc() - ca).total_seconds() < LEASE_TTL:
            os.rename(claim, path)                # someone alive holds it
            continue
        fresh["state"] = "pending"
        add_history(fresh, "in_progress", "pending", "dispatcher",
                    "lease expired%s — reclaimed"
                    % ("" if claimed_at else " (unparseable lease)"))
        release_lease(fresh)
        put(claim, path, fresh)
        events.append({"sector": sector, "ticket": fresh.get("ticket_id", path),
                       "outcome": "reclaimed", "detail": "stale lease expired"})


# ---------- pipeline: ticket spawning ----------

def resolve_deliverable(sector, ticket_id, notes=None):
    """Find the deliverable artifact produced by this ticket on disk."""
    deliv_dir = os.path.join(DOMAINS_DIR, sector, "deliverables")
    if not os.path.isdir(deliv_dir):
        return None
    if notes:
        for prefix in ("deliverable: ", "publish copy: "):
            if prefix in notes:
                name = notes.split(prefix, 1)[1].strip()
                p = os.path.join(deliv_dir, name)
                if os.path.isfile(p):
                    return p
    matches = sorted([
        os.path.join(deliv_dir, f)
        for f in os.listdir(deliv_dir)
        if ticket_id in f and not f.endswith(".tmp")
    ], reverse=True)
    return matches[0] if matches else None


def process_spawn(sector, orig_path, ticket, notes=None):
    """Evaluate and execute ticket spawning for a done or approved ticket.

    Fail-closed: malformed payload or missing sector aborts spawn, transitions
    the ticket to needs_revision with full history, and never fails silently.
    Returns an event dict.
    """
    cur_state = ticket.get("state") or "done"
    spawn_cfg = ticket.get("spawn_next")
    tid = ticket.get("ticket_id", os.path.basename(orig_path))

    if not isinstance(spawn_cfg, dict):
        err = "spawn_next must be a JSON object"
        ticket["state"] = "needs_revision"
        ticket["revision_note"] = {"by": "dispatcher", "at": stamp(), "reason": "spawn_next failed: %s" % err}
        add_history(ticket, cur_state, "needs_revision", "dispatcher", "spawn_next aborted: %s" % err)
        return {"sector": sector, "ticket": tid, "outcome": "needs_revision", "detail": err}

    target_sector = spawn_cfg.get("target_sector")
    request = spawn_cfg.get("request")
    pass_deliverable = bool(spawn_cfg.get("pass_deliverable", False))

    if not target_sector or not isinstance(target_sector, str):
        err = "target_sector is required and must be a string"
        ticket["state"] = "needs_revision"
        ticket["revision_note"] = {"by": "dispatcher", "at": stamp(), "reason": "spawn_next failed: %s" % err}
        add_history(ticket, cur_state, "needs_revision", "dispatcher", "spawn_next aborted: %s" % err)
        return {"sector": sector, "ticket": tid, "outcome": "needs_revision", "detail": err}

    if not request or not isinstance(request, str) or not request.strip():
        err = "request is required and must be a non-empty string"
        ticket["state"] = "needs_revision"
        ticket["revision_note"] = {"by": "dispatcher", "at": stamp(), "reason": "spawn_next failed: %s" % err}
        add_history(ticket, cur_state, "needs_revision", "dispatcher", "spawn_next aborted: %s" % err)
        return {"sector": sector, "ticket": tid, "outcome": "needs_revision", "detail": err}

    target_tickets_dir = os.path.join(DOMAINS_DIR, target_sector, "tickets")
    if not os.path.isdir(target_tickets_dir):
        err = "target sector does not exist or has no tickets directory: %s" % target_sector
        ticket["state"] = "needs_revision"
        ticket["revision_note"] = {"by": "dispatcher", "at": stamp(), "reason": "spawn_next failed: %s" % err}
        add_history(ticket, cur_state, "needs_revision", "dispatcher", "spawn_next aborted: %s" % err)
        return {"sector": sector, "ticket": tid, "outcome": "needs_revision", "detail": err}

    deliverable_ref = None
    if pass_deliverable:
        deliverable_ref = resolve_deliverable(sector, tid, notes)

    now = now_utc()
    new_id = "T-%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
    client_id = ticket.get("client_id") or "pipeline"

    new_request = request.strip()
    if pass_deliverable and deliverable_ref:
        new_request = "%s\n\n[Deliverable Reference: %s]" % (new_request, deliverable_ref)

    new_ticket = {
        "ticket_id": new_id,
        "client_id": client_id,
        "request": new_request,
        "decision": {
            "status": "accepted",
            "reason": "spawned from ticket %s via pipeline" % tid
        },
        "created_at": now.isoformat(),
        "state": "open",
        "attempts": 0,
        "via": ticket.get("via"),
        "history": [
            {
                "at": stamp(),
                "from": None,
                "to": "open",
                "by": "dispatcher:pipeline",
                "why": "spawned from %s ticket %s" % (cur_state, tid)
            }
        ]
    }
    new_context = {}
    if ticket.get("context") and isinstance(ticket["context"], dict):
        new_context.update(ticket["context"])
    if pass_deliverable and deliverable_ref:
        new_context["deliverable"] = deliverable_ref
    new_context["parent_ticket_id"] = tid
    if "context" in spawn_cfg and isinstance(spawn_cfg["context"], dict):
        new_context.update(spawn_cfg["context"])
    if new_context:
        new_ticket["context"] = new_context

    if "spawn_next" in spawn_cfg and isinstance(spawn_cfg["spawn_next"], dict):
        new_ticket["spawn_next"] = spawn_cfg["spawn_next"]

    new_path = os.path.join(target_tickets_dir, "ticket_%s_%s.json" % (client_id, new_id))
    try:
        from shared_kernel.schema_version import KIND_TICKET, stamp_schema_version

        stamp_schema_version(KIND_TICKET, new_ticket)
    except ImportError:
        pass
    try:
        atomic_json(new_path, new_ticket, trailing_newline=False)
    except OSError as exc:
        err = "failed to create spawned ticket: %s" % exc
        ticket["state"] = "needs_revision"
        ticket["revision_note"] = {"by": "dispatcher", "at": stamp(), "reason": "spawn_next failed: %s" % err}
        add_history(ticket, cur_state, "needs_revision", "dispatcher", "spawn_next aborted: %s" % err)
        return {"sector": sector, "ticket": tid, "outcome": "needs_revision", "detail": err}

    ticket["spawned"] = True
    ticket["spawned_ticket_id"] = new_id
    add_history(ticket, cur_state, cur_state, "dispatcher:pipeline",
                "spawned next ticket %s in %s" % (new_id, target_sector))
    return {"sector": sector, "ticket": tid, "outcome": "spawned",
            "detail": "spawned %s in %s" % (new_id, target_sector)}


def spawn_from_done(sector, path, events):
    """Safely claim and process an unspawned done or approved ticket."""
    claim = grab(path)
    if claim is None:
        return
    try:
        ticket = load(claim)
    except TICKET_READ_ERRORS:
        os.rename(claim, path)
        return
    if ticket.get("state") not in ("done", "approved") or not ticket.get("spawn_next") or ticket.get("spawned"):
        os.rename(claim, path)
        return
    event = process_spawn(sector, path, ticket)
    put(claim, path, ticket)
    if event:
        events.append(event)


# ---------- commands ----------

def cmd_resolve(args):
    """A human executed the work outside the system; stamp the ticket done."""
    by = (args.by or "").strip()
    if not by:
        sys.exit("resolve failed: --by is required (a human id)")
    if by.lower() == "system":
        sys.exit("resolve failed: --by must be a human, never 'system'")
    action = (args.action_taken or "").strip()
    if not action:
        sys.exit("resolve failed: --action-taken is mandatory — what exactly was done, by hand")
    found = None
    for sector, path in iter_tickets():
        try:
            ticket = load(path)
        except TICKET_READ_ERRORS:
            continue
        if ticket.get("ticket_id") == args.ticket_id:
            found = (sector, path, ticket)
            break
    if found is None:
        sys.exit("resolve failed: no such ticket: %s" % args.ticket_id)
    sector, path, ticket = found
    if ticket.get("state") == "done":
        sys.exit("resolve failed: ticket already done — never double-stamped")
    if ticket.get("state") == "in_progress":
        sys.exit("resolve failed: ticket is claimed (lease held) — wait for it to land or expire")
    base = copy.deepcopy(ticket)  # merge base: only our changes may win
    frm = ticket.get("state")
    ticket["state"] = "done"
    ticket["done_at"] = stamp()
    ticket["resolved_by"] = by
    ticket["resolution"] = action
    ticket["attempts"] = 0
    add_history(ticket, frm, "done", by,
                "human execution outside the system: %s" % action)
    if ticket.get("spawn_next") and not ticket.get("spawned"):
        process_spawn(sector, path, ticket, notes=action)
    release_lease(ticket)
    save(path, ticket, base=base)
    print(json.dumps({"resolved": ticket["ticket_id"], "sector": sector,
                      "state": "done", "resolved_by": by,
                      "resolution": action}, ensure_ascii=False, indent=2))


def cmd_archive(args):
    """Human ritual: done tickets exit the active board into monthly archives."""
    archived, skipped = [], []
    for sector, path in iter_tickets():
        try:
            ticket = load(path)
        except SchemaVersionError as exc:
            skipped.append({"ticket": os.path.basename(path),
                            "detail": "unsupported schema_version — fail-closed, "
                                      "untouched: %s" % exc})
            continue
        except (OSError, json.JSONDecodeError):
            skipped.append({"ticket": os.path.basename(path),
                            "detail": "corrupt ticket JSON — fail-closed, untouched"})
            continue
        if ticket.get("state") not in ("done", "published"):
            continue                                  # unfinished work stays visible
        done_at = parse_ts(ticket.get("done_at")) or parse_ts(ticket.get("published_at")) or parse_ts(ticket.get("created_at"))
        month = done_at.strftime("%Y-%m") if done_at else "unknown"
        dest_dir = os.path.join(DOMAINS_DIR, sector, "archive", month)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        if os.path.exists(dest):
            skipped.append({"ticket": ticket.get("ticket_id", os.path.basename(path)),
                            "detail": "archive target exists — never overwrite"})
            continue
        os.rename(path, dest)                         # atomic — the move IS the commit
        archived.append({"ticket": ticket.get("ticket_id", os.path.basename(path)),
                         "to": "%s/archive/%s" % (sector, month)})
    print(json.dumps({"archived_at": stamp(),
                      "counts": {"archived": len(archived), "skipped": len(skipped)},
                      "archived": archived, "skipped": skipped},
                     ensure_ascii=False, indent=2))


def cmd_run(args):
    events = []
    recover_orphans(events)
    reclaim_stale(events)
    for sector, path in iter_tickets():
        try:
            probe = load(path)
        except SchemaVersionError as exc:
            events.append({"sector": sector, "ticket": os.path.basename(path),
                           "outcome": "skipped",
                           "detail": "unsupported schema_version — fail-closed, "
                                     "untouched: %s" % exc})
            continue
        except (OSError, json.JSONDecodeError):
            events.append({"sector": sector, "ticket": os.path.basename(path),
                           "outcome": "skipped",
                           "detail": "corrupt ticket JSON — fail-closed, untouched"})
            continue
        if probe.get("state") in DISPATCHABLE:
            try:
                dispatch_one(sector, path, events)
            except Exception as exc:                  # one ticket never kills the pass
                events.append({"sector": sector, "ticket": os.path.basename(path),
                               "outcome": "error", "detail": str(exc)[:160]})
        elif probe.get("state") in ("done", "approved") and probe.get("spawn_next") and not probe.get("spawned"):
            try:
                spawn_from_done(sector, path, events)
            except Exception as exc:
                events.append({"sector": sector, "ticket": os.path.basename(path),
                               "outcome": "error", "detail": "spawn error: %s" % str(exc)[:160]})
    counts = {}
    for event in events:
        counts[event["outcome"]] = counts.get(event["outcome"], 0) + 1
    print(json.dumps({"ran_at": stamp(), "counts": counts, "events": events},
                     ensure_ascii=False, indent=2))


def cmd_list(args):
    rows, counts = [], {}
    for sector, path in iter_tickets():
        try:
            ticket = load(path)
        except SchemaVersionError as exc:
            rows.append("%-18s %-30s %-14s %s" % (sector[:18],
                        os.path.basename(path)[:30], "BAD-SCHEMA",
                        str(exc)[:44]))
            continue
        except (OSError, json.JSONDecodeError):
            rows.append("%-18s %-30s %-14s" % (sector[:18],
                        os.path.basename(path)[:30], "CORRUPT"))
            continue
        state = str(ticket.get("state") or "?")
        counts[state] = counts.get(state, 0) + 1
        note = "rev" if ticket.get("revision_note") else ""
        rows.append("%-18s %-30s %-14s %-2s %-3s %s" % (
            sector[:18], str(ticket.get("ticket_id") or "?")[:30], state,
            str(ticket.get("attempts") or ""), note,
            str(ticket.get("request") or "")[:44]))
    if rows:
        print("%-18s %-30s %-14s %-2s %-3s %s"
              % ("SECTOR", "TICKET", "STATE", "A", "N", "REQUEST"))
        print("\n".join(rows))
    else:
        print("board empty")
    print("states: " + (", ".join("%s=%s" % kv for kv in sorted(counts.items()))
                        if counts else "none"))
    archived_counts = {}
    for sector, tdir in sectors():
        for root, _dirs, files in os.walk(os.path.join(DOMAINS_DIR, sector, "archive")):
            archived_counts[sector] = archived_counts.get(sector, 0) \
                + len([f for f in files if f.endswith(".json")])
    if archived_counts:
        print("archived: " + ", ".join("%s=%s" % kv
                                       for kv in sorted(archived_counts.items())))


def cmd_revise(args):
    by = (args.by or "").strip()
    if not by:
        sys.exit("revise failed: --by is required (a human id)")
    if by.lower() == "system":
        sys.exit("revise failed: --by must be a human, never 'system'")
    if not args.reason or not args.reason.strip():
        sys.exit("revise failed: revisions always carry a reason")
    found = None
    for sector, path in iter_tickets():
        try:
            ticket = load(path)
        except TICKET_READ_ERRORS:
            continue
        if ticket.get("ticket_id") == args.ticket_id:
            found = (sector, path, ticket)
            break
    if found is None:
        sys.exit("revise failed: no such ticket: %s" % args.ticket_id)
    sector, path, ticket = found
    if ticket.get("state") == "in_progress":
        sys.exit("revise failed: ticket is claimed (lease held) — wait for it to land or expire")
    base = copy.deepcopy(ticket)  # merge base: only our changes may win
    frm = ticket.get("state")
    ticket["state"] = "needs_revision"
    ticket["revision_note"] = {"by": by, "at": stamp(),
                               "reason": args.reason.strip()}
    ticket["attempts"] = 0
    add_history(ticket, frm, "needs_revision", by, "human revision demand")
    release_lease(ticket)
    save(path, ticket, base=base)
    print(json.dumps({"revised": ticket["ticket_id"], "sector": sector,
                      "state": ticket["state"], "revision_note": ticket["revision_note"]},
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="one dispatch pass (no daemon)")
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="show the whole board")
    p_list.set_defaults(func=cmd_list)

    p_rev = sub.add_parser("revise", help="human demands a revision (reason mandatory)")
    p_rev.add_argument("--ticket-id", required=True)
    p_rev.add_argument("--by", required=True, help="human id")
    p_rev.add_argument("--reason", required=True)
    p_rev.set_defaults(func=cmd_revise)

    p_res = sub.add_parser("resolve",
                           help="human executed the work outside — stamp done (action mandatory)")
    p_res.add_argument("--ticket-id", required=True)
    p_res.add_argument("--by", required=True, help="human id")
    p_res.add_argument("--action-taken", required=True, dest="action_taken",
                       help="what exactly was done, by hand")
    p_res.set_defaults(func=cmd_resolve)

    p_arc = sub.add_parser("archive",
                           help="move done tickets to monthly archives (human ritual)")
    p_arc.set_defaults(func=cmd_archive)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
