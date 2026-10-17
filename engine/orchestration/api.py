#!/usr/bin/env python3
"""api.py — FastAPI router and cockpit server for Sovereign Agency OS.

Unified Orchestration Hub:
- Serves the Sovereign Executive OS cockpit from orchestration/cockpit/
  (index.html + css/ design tokens + modular vanilla-JS engine in js/).
- Provides API endpoints for Ideation Domain Governance (Tickets, Approval Gates, Auto-Ideator, Settle Engine).
- Provides API endpoints for Capsule Factory Governance (Spool scan, TOCTOU-protected approval, Rejection feedback).
- Live KPI Metrics and statistics aggregation across sectors and spool.
- Branch registry (domains/*) with governance autonomy levels for the cockpit,
  so a sector exists the moment its directory does — before any ticket.
- Capability-based access control: every guarded route declares the capability
  it requires (orchestration/access_control.py), undeclared routes fail closed,
  and identity is derived from the verified credential — never from the body.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Ensure company-engine and engine roots are in sys.path
try:
    from shared_kernel.paths import (
        CAPSULES_SANDBOX,
        CAPSULES_SPOOL,
        DOMAINS_DIR,
        ENGINE_ROOT,
        GATE_QUEUE_DIR,
        READ_INDEX_PATH,
        REPO_ROOT,
    )
except Exception:
    ENGINE_ROOT = Path(__file__).resolve().parents[1]
    REPO_ROOT = ENGINE_ROOT.parent
    DOMAINS_DIR = REPO_ROOT / "data" / "domains"
    CAPSULES_SPOOL = REPO_ROOT / "data" / "capsules" / "spool"
    CAPSULES_SANDBOX = REPO_ROOT / "data" / "capsules" / "sandbox"
    GATE_QUEUE_DIR = REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_queue"
    READ_INDEX_PATH = REPO_ROOT / "data" / "index" / ".read_index.sqlite3"

# ``data/`` is a source root too: the domain package tree lives at
# ``data/domains/`` (``data/domains/ideation/auto_ideator.py``), so importing
# ``domains.ideation.*`` requires ``data/`` itself on the path — not just the
# repo root. Without it ``GET /api/engine/gears`` died with a 500
# (ModuleNotFoundError: No module named 'domains').
for _p in (str(ENGINE_ROOT), str(REPO_ROOT), str(REPO_ROOT / "data")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# NOTE: this module intentionally does NOT load .env. Doing so on import would
# leak real provider keys / publishing credentials into the test process and
# change routing behaviour. The server is started through orchestration/
# run_cockpit.sh, which sources .env before launching uvicorn; the pulse loads
# .env for its own children (orchestration/pulse.py).

# Configure stdout logging (AGENCY_LOG_FORMAT=json switches to structured logs;
# AGENCY_LOG_LEVEL sets the level). See orchestration/observability.py.
# Imported here rather than with the block below because the sys.path setup
# above must run first, and logging has to be configured before anything logs.
from orchestration import observability  # noqa: E402

observability.install_logging()
logger = logging.getLogger("orchestration.api")

from capsule_engine import (
    State,
    load_manifest,
    save_manifest,
    transition,
)
from capsule_engine import task_type as task_type_engine
from capsule_engine.exceptions import (
    CapabilityError,
    CapsuleError,
    CapsuleNotFoundError,
    LockTimeoutError,
    SecurityError,
    StateTransitionError,
)
from capsule_engine.governance import (
    KNOWN_LEVELS,
    LEVEL_LABELS,
    domain_of,
    governance_path,
    level_for,
    load_governance,
    save_governance,
)
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import ARTIFACT_FILE, now_iso, seat_at_approval_gate
from capsule_engine.bridge import BRIDGE_ENDPOINT_ENV, resolve_endpoint
from capsule_engine.capabilities import resolve_secret
from workers.publishing import GEAR as PUBLISHING_GEAR
from orchestration import access_control
from orchestration import approval
from orchestration.v1 import errors as v1_errors
from orchestration import read_index
from orchestration import work_queue
from orchestration.capsule_pulse import drain_capsule_queue, run_pulse
from shared_kernel.atomic_io import (
    atomic_bytes,
    atomic_json,
    atomic_text,
    update_json,
)
from shared_kernel.state_schema import TICKET_ACTIVE_STATES
from shared_kernel.users import (
    authenticate_user,
    mint_user_token,
    get_user_by_username,
    load_users,
)
from shared_kernel.schema_version import (
    KIND_COMPANY_PROFILE,
    KIND_MARKETING_CONFIG,
    KIND_TICKET,
    stamp_schema_version,
)


try:
    from ops.tools.ticket_mgr import find_ticket, promote_ticket, demote_ticket
except ImportError:
    from tools.ticket_mgr import find_ticket, promote_ticket, demote_ticket

# System Paths
# Resolved: the spool is compared against resolved capsule paths by the
# confinement guard, and any symlink in CAPSULES_SPOOL (e.g. macOS
# /var -> /private/var) would otherwise make a legitimate capsule look like it
# escaped the spool and be refused with a 404.
SPOOL_DIR = CAPSULES_SPOOL
GATE_DIR = (
    GATE_QUEUE_DIR.parent
    if (GATE_QUEUE_DIR.parent / "gate_script.py").is_file()
    else REPO_ROOT / "data" / "governance" / "approval_gates"
)
DOMAINS_DIR = DOMAINS_DIR

# Security configuration (fail closed — see auth_middleware below):
#   AGENCY_CAPABILITY_SECRET  signing secret for capability keys; the primary
#                             credential (per-caller grants, not one master key)
#   AGENCY_API_TOKEN          optional; while AGENCY_LEGACY_ROOT_TOKEN allows
#                             it, this legacy shared token is accepted as an
#                             explicit ROOT principal and recorded as such
#                             ("legacy-token") in every audit line. It is the
#                             migration bridge, not the destination.
#   AGENCY_LEGACY_ROOT_TOKEN  kill switch for the line above (unset = enabled)
#   AGENCY_OPERATOR           name recorded when the legacy token is used, and
#                             the display fallback for the cockpit
#   AGENCY_CORS_ORIGINS       comma-separated allowlist; empty = same-origin only
#
# Which capability each route requires lives in orchestration/access_control.py.
API_TOKEN = os.environ.get("AGENCY_API_TOKEN", "").strip()
OPERATOR = os.environ.get("AGENCY_OPERATOR", "owner").strip() or "owner"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("AGENCY_CORS_ORIGINS", "").split(",")
    if origin.strip()
]

router = APIRouter()


def _domain_dirs() -> List[str]:
    """Real branch directories under domains/ — the source of truth for sectors.

    A branch *is* a folder: the moment ``domains/hr/`` exists it is a branch
    the cockpit must see, with zero code change anywhere. Discovery reads the
    directory straight off disk with ``os.listdir()`` and fails closed on
    anything that is not a branch:

    - names starting with ``__`` (``__pycache__``), ``_`` (``_scratch``) or
      ``.`` (``.DS_Store``) are internal, never branches;
    - loose files (``domains/__init__.py``) are not directories;
    - an unreadable/missing root yields ``[]`` — reported as an empty company,
      never guessed.
    """
    try:
        names = os.listdir(DOMAINS_DIR)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []

    branches: List[str] = []
    for name in names:
        if name.startswith((".", "_")):  # covers every __* name, e.g. __pycache__
            continue
        if not (DOMAINS_DIR / name).is_dir():
            continue
        branches.append(name)
    return sorted(branches)


def _domain_scripts(domain: str) -> Dict[str, Path]:
    """Executable contract scripts of a domain, discovered not assumed.

    A domain is free to ship any subset: ``tracker_script.py`` (gate settle),
    ``worker_script.py`` (ticket→capsule bridge), ``auto_ideator.py`` (idea
    generation). A script that does not exist simply takes no part in the
    cycle — the engine reports what ran instead of pretending.
    """
    base = DOMAINS_DIR / domain
    return {
        "tracker": base / "tracker_script.py",
        "worker": base / "worker_script.py",
    }


# --------------------------------------------------------------------------- #
# Async script execution (Scaling Brick 1) — the API never blocks on a child
# --------------------------------------------------------------------------- #

# Wall-clock cap for one script invocation. Governance/ideation scripts may
# call models; ten minutes is generous. Override: AGENCY_SCRIPT_TIMEOUT_S.
SCRIPT_TIMEOUT_S = float(os.environ.get("AGENCY_SCRIPT_TIMEOUT_S", "600") or 600)


class ScriptTimeoutError(RuntimeError):
    """A child script outlived the timeout and was killed — reported, not frozen."""


async def _run_subprocess(
    cmd: List[str], *, input_text: Optional[str] = None
) -> tuple[int, str, str]:
    """Run a domain/gate script without blocking the event loop.

    Every ``subprocess.run`` in this module used to stall the whole server
    (health checks, decisions, everything) until the child exited. This
    helper yields to the loop while the child runs, enforces a wall-clock
    timeout (kill + report instead of freeze), and returns
    ``(returncode, stdout, stderr)`` so each caller keeps its own
    error-status semantics.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(
            proc.communicate(
                input_text.encode("utf-8") if input_text is not None else None
            ),
            timeout=SCRIPT_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise ScriptTimeoutError(
            f"script timed out after {SCRIPT_TIMEOUT_S:g}s: {cmd[-1]}"
        ) from exc
    return (
        proc.returncode or 0,
        raw_out.decode("utf-8", "replace"),
        raw_err.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #

# NOTE: these models deliberately carry no `reviewer`/actor field. A body-
# supplied identity is a forgery waiting to be trusted; the actor is derived
# from the verified credential in the middleware (see _decision_identity).
# Clients that still send a `reviewer` are ignored — extra fields are dropped.


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Rejection reason or required revisions")


class ApproveGateRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Optional approval notes")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Username or email")
    password: str = Field(..., min_length=1, description="Raw password")


class UpdateTicketContextRequest(BaseModel):
    context: Dict[str, Any] = Field(default_factory=dict, description="Fields to update or merge into ticket context")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Operational metadata to update")
    note: Optional[str] = Field(None, description="Optional audit note explaining the change")


class CreateTicketRequest(BaseModel):
    sector: str = Field("ideation", description="Originating domain sector")
    title: str = Field(..., min_length=1, description="Idea topic or title")
    content: str = Field(..., min_length=1, description="Idea vision, summary, or prompt")
    cadence: str = Field("weekly_routine", description="Scheduling cadence: 'weekly_routine' or 'occasion'")
    stage: str = Field("approved", description="Ticket stage: 'approved' (for manufacturing) or 'scratch' (draft/experimental)")
    context: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Arbitrary dynamic domain payload")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Custom operational metadata")
    deliverable_type: Optional[str] = Field("auto", description="Type of expected deliverable: post, code_patch, invoice, screen, report, etc.")



class EnqueueWorkRequest(BaseModel):
    kind: str = Field("capsule.advance", description="Queue kind (capsule.advance = one chained processing pass)")
    capsule_id: Optional[str] = Field(None, description="Capsule to advance — builds the payload; 404 when unknown")
    payload: Optional[Dict[str, Any]] = Field(None, description="Explicit payload (advanced callers; wins over capsule_id)")
    priority: int = Field(0, description="Higher runs first; ties are FIFO by enqueue time")
    delay_seconds: float = Field(0.0, ge=0, description="Seconds before the item becomes due")
    max_attempts: Optional[int] = Field(None, ge=1, description="Retry budget (default: 3)")
    idempotency_key: Optional[str] = Field(None, max_length=200, description="De-duplicate against active items")


class CreateCapsuleRequest(BaseModel):
    prompt: Optional[str] = Field(None, description="Prompt or topic for the capsule")
    text: Optional[str] = Field(None, description="Direct text payload for the capsule")
    capsule_id: Optional[str] = Field(None, description="Custom capsule ID")
    domain: Optional[str] = Field(None, description="Target domain worker")
    sector: Optional[str] = Field(None, description="Target sector (alias for domain)")
    target_platform: str = Field("facebook", description="Target social platform")
    scheduled_for: Optional[str] = Field(None, description="ISO-8601 date string for future release")
    requires_image: Optional[bool] = Field(None, description="Explicit visual-output flag; default derives from the domain, not a branch name")
    task_type: Optional[str] = Field(None, description="Task type id from config/task_types/ (e.g. 'social_post'); omit for legacy behaviour (no card written)")


class PostBuildRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="Post topic / concept")
    angle: Optional[str] = Field(None, description="Strategic angle")
    audience: Optional[str] = Field(None, description="Target audience")
    occasion: Optional[str] = Field(None, description="Occasion name (Hijri / Gregorian)")
    channels: List[str] = Field(default_factory=lambda: ["linkedin", "facebook", "x"])
    headline: Optional[str] = Field(None, description="Post headline")
    linkedin_copy: Optional[str] = Field(None, description="LinkedIn tailored copy")
    facebook_copy: Optional[str] = Field(None, description="Facebook tailored copy")
    x_copy: Optional[str] = Field(None, description="X (Twitter) tailored copy")
    unified_copy: Optional[str] = Field(None, description="Unified copy")
    hashtags: List[str] = Field(default_factory=list)
    visual_concept: Optional[str] = Field(None, description="Visual concept")
    image_prompt: Optional[str] = Field(None, description="Image generation prompt")
    aspect_ratios: List[str] = Field(default_factory=lambda: ["1:1", "4:5", "16:9"])
    include_logo: bool = True


class CreateDomainRequest(BaseModel):
    domain_id: str = Field(..., min_length=2, max_length=50, description="Machine ID for branch directory e.g. hr, support")
    title: Optional[str] = Field(None, description="Human readable name e.g. Human Resources")
    level: str = Field("L0", description="Autonomy level (L0, L1, L2, L3)")


class QAInspectRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Copy text to inspect for QA compliance")


class PromptDistillRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text to distill into a compliant visual prompt")


class TestImageRequest(BaseModel):
    prompt: Optional[str] = Field(None, description="Visual prompt to test image generation for")


class SandboxTrialRequest(BaseModel):
    ticket_id: str = Field(..., min_length=1, description="Ticket ID to execute trial for")
    sector: Optional[str] = Field(None, description="Optional domain sector")
    live: bool = Field(False, description="Whether to execute live external network calls")
    auto_approve: bool = Field(False, description="Whether to auto-sign approval gate to test full publishing")


class SandboxPromoteRequest(BaseModel):
    note: Optional[str] = Field("", description="Audit note documenting reasons for promotion")


class SandboxCleanRequest(BaseModel):
    keep_recent: int = Field(0, ge=0, description="Number of newest trials to preserve")


# --------------------------------------------------------------------------- #
# Sovereign API Root & Live Event Stream
# --------------------------------------------------------------------------- #

@router.api_route("/", methods=["GET", "HEAD"])
async def api_root() -> Dict[str, Any]:
    """Sovereign Agency OS — Central Engine API Root."""
    return {
        "engine": "Sovereign Agency OS Core",
        "status": "online",
        "version": "1.0.0",
        "docs_url": "/docs",
        "health_url": "/api/health",
        "events_url": "/api/events",
    }


@router.get("/api/events")
async def event_stream(request: Request):
    """Server-Sent Events (SSE) stream for live capsule and governance updates.

    Subscribed by external web (Next.js) and mobile (Flutter) applications.

    Additive: besides the heartbeat, the stream forwards every decision event
    published by the unified approval service (``approval.approved`` /
    ``approval.rejected``), so a cockpit hears about an approve or reject the
    moment it lands.
    """
    import asyncio
    from fastapi.responses import StreamingResponse

    async def _generator():
        yield f"event: connected\ndata: {json.dumps({'status': 'connected', 'timestamp': now_iso()})}\n\n"
        subscriber = approval.subscribe_events()
        last_ping = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                for item in approval.drain_events(subscriber):
                    yield (
                        f"event: {item['event']}\n"
                        f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    )
                if time.monotonic() - last_ping >= 15:
                    last_ping = time.monotonic()
                    yield f"event: ping\ndata: {json.dumps({'ping': now_iso()})}\n\n"
                await asyncio.sleep(0.5)
        finally:
            approval.unsubscribe_events(subscriber)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Health (open) & KPI Metrics (authenticated)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Task types (config/task_types/) — read-only catalogue + startup worker check
# --------------------------------------------------------------------------- #

#: Computed once per process: the startup check for task types whose steps name
#: a worker the live registry does not provide. Cheap to hold, and liveness
#: probes should not re-discover the worker catalogue on every call.
_TASK_TYPE_HEALTH: Optional[Dict[str, Any]] = None


def _known_worker_names() -> set:
    """Every worker name the live registry provides (core + branch-owned).

    ``engine/workers`` sits *above* the core, so the catalogue is handed to the
    loader from here rather than imported by ``capsule_engine.task_type`` — the
    core never reaches upward (see tests/test_dependency_direction.py).
    """
    names: set = set()
    try:
        from workers import WORKERS, domain_workers

        names.update(WORKERS)
        try:
            names.update(domain_workers())
        except Exception as exc:  # a broken branch worker is reported elsewhere
            logger.warning("task types: domain worker discovery failed: %s", exc)
    except Exception as exc:  # pragma: no cover - the registry always ships
        logger.warning("task types: worker registry unavailable: %s", exc)
    return names


def _task_type_health() -> Dict[str, Any]:
    """The startup check: catalogue size + task types with unregistered workers."""
    global _TASK_TYPE_HEALTH
    if _TASK_TYPE_HEALTH is not None:
        return _TASK_TYPE_HEALTH
    try:
        catalogue = task_type_engine.list_task_types()
    except task_type_engine.TaskTypeError as exc:
        logger.error("task types: catalogue unreadable: %s", exc)
        _TASK_TYPE_HEALTH = {
            "count": 0,
            "unregistered_workers": [],
            "error": str(exc),
        }
        return _TASK_TYPE_HEALTH
    gaps = task_type_engine.unregistered_workers(
        _known_worker_names(), types=catalogue
    )
    if gaps:
        logger.warning(
            "task types: %d step(s) name an unregistered worker: %s",
            len(gaps),
            "; ".join(f"{g['task_type']}/{g['step']} -> {g['worker']}" for g in gaps),
        )
    _TASK_TYPE_HEALTH = {
        "count": len(catalogue),
        "types": [task_type.task_type for task_type in catalogue],
        "unregistered_workers": gaps,
    }
    return _TASK_TYPE_HEALTH


@router.get("/api/health")
async def health() -> Dict[str, Any]:
    """Open liveness probe: no auth, no data — everything else needs the token.

    Carries exactly one configuration diagnostic: task types whose steps name a
    worker the live registry does not provide (docs/TASK_TYPES.md §8.6). That is
    a worker-name gap in committed configuration, never a secret or vault datum.
    """
    return {
        "status": "ok",
        "service": "sovereign-agency-os",
        "task_types": _task_type_health(),
    }


@router.get("/api/task-types")
async def list_task_types_endpoint() -> Dict[str, Any]:
    """Read-only catalogue of the declared task types (``config/task_types/``).

    Deploy-time configuration: there is no write API, and an edited file takes
    effect on the next process start. A malformed document refuses the whole
    catalogue loudly rather than rendering it half-broken (fail closed).
    """
    try:
        catalogue = task_type_engine.list_task_types()
    except task_type_engine.TaskTypeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Task type catalogue unreadable (fail closed): {exc}",
        ) from exc
    return {
        "count": len(catalogue),
        "task_types": [task_type.to_view() for task_type in catalogue],
    }


@router.get("/api/task-types/{task_type_id}")
async def get_task_type_endpoint(task_type_id: str) -> Dict[str, Any]:
    """One task type: its declared pipeline, plus the B1 card it renders to."""
    if not task_type_engine.is_valid_id(task_type_id):
        raise HTTPException(
            status_code=404, detail=f"Unknown task type: {task_type_id!r}"
        )
    path = task_type_engine.task_types_dir() / f"{task_type_id.strip()}.json"
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"Unknown task type: {task_type_id!r}"
        )
    try:
        task_type = task_type_engine.load_task_type(task_type_id)
    except task_type_engine.TaskTypeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Task type unreadable (fail closed): {exc}",
        ) from exc
    return {
        "task_type": task_type.to_view(),
        "task_profile": task_type.to_task_profile(),
    }


@router.get("/api/metrics")
async def get_metrics() -> Response:
    """Prometheus exposition: HTTP counters plus live vault gauges.

    Guarded like every read (``factory:read``): scrape with a capability key
    (``Authorization: Bearer …`` or ``?token=``). The vault gauges come from
    the rebuildable read index; when the index is off they are omitted and the
    HTTP counters still render.
    """
    gauges: List[Any] = []
    stats = read_index.stats() or {}
    caps = stats.get("capsules") or {}
    tix = stats.get("tickets") or {}
    if caps or tix:
        gauges.append(("agency_capsules", "Capsules by state.", [
            ({"state": "total"}, caps.get("total", 0)),
            ({"state": "pending_approval"}, caps.get("pending", 0)),
            ({"state": "published"}, caps.get("published", 0)),
            ({"state": "stalled"}, caps.get("stalled", 0)),
        ]))
        gauges.append(("agency_tickets", "Tickets by stage.", [
            ({"stage": "total"}, tix.get("total", 0)),
            ({"stage": "approved"}, tix.get("approved", 0)),
            ({"stage": "scratch"}, tix.get("scratch", 0)),
            ({"stage": "settled"}, tix.get("settled", 0)),
        ]))

    open_gates = 0
    if GATE_QUEUE_DIR.is_dir():
        for f in GATE_QUEUE_DIR.glob("gate_*.json"):
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("state") == "open":
                    open_gates += 1
            except Exception:
                continue
    gauges.append(("agency_open_gates", "Approval gates currently open.", [({}, open_gates)]))

    return Response(
        content=observability.render_metrics(gauges),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# --------------------------------------------------------------------------- #
# Sovereign Person Authentication & Identity API
# --------------------------------------------------------------------------- #

@router.post("/api/auth/login")
async def login_endpoint(req: LoginRequest) -> Dict[str, Any]:
    """Sovereign person login: authenticates against local registry and issues capability token."""
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = mint_user_token(user)
    return {
        "status": "success",
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 30 * 86400,
        "user": {
            "username": user["username"],
            "full_name": user.get("full_name") or user["username"].capitalize(),
            "role": user.get("role", "operator"),
            "authorized_sectors": user.get("authorized_sectors", []),
            "capabilities": user.get("capabilities", []),
        },
    }


@router.get("/api/auth/me")
async def get_current_user_endpoint(request: Request) -> Dict[str, Any]:
    """Retrieve identity, active grants, and profile of the authenticated caller."""
    principal = getattr(request.state, "principal", None)
    if not principal:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    uname = getattr(principal, "subject", "unknown")
    user = get_user_by_username(uname)
    is_root = getattr(principal, "is_root", False)
    return {
        "status": "success",
        "principal": principal.as_dict() if hasattr(principal, "as_dict") else {"subject": uname},
        "user_profile": {
            "username": user["username"] if user else uname,
            "full_name": user.get("full_name") if user else uname.capitalize(),
            "role": user.get("role") if user else ("admin" if is_root else "operator"),
            "authorized_sectors": user.get("authorized_sectors", ["*"] if is_root else []),
            "capabilities": user.get("capabilities", ["*"] if is_root else []),
        } if user or principal else None,
    }


@router.post("/api/auth/logout")
async def logout_endpoint(request: Request) -> Dict[str, Any]:
    """Sovereign session logout."""
    return {"status": "success", "message": "Logged out successfully."}


@router.get("/api/stats")
async def get_system_stats() -> Dict[str, Any]:
    """Calculate live metrics across all domains, gates, and factory spool.

    Optionally accelerated by the read index (Scaling Brick 3): when it is
    fresh, aggregates come from SQLite; the fingerprint check guarantees the
    numbers still describe *these files right now*. ``AGENCY_INDEX=off``
    disables the layer entirely and everything falls back to files.
    """
    indexed = read_index.stats()

    # 1. Pending approval gates
    pending_gates = 0
    if GATE_QUEUE_DIR.is_dir():
        for f in GATE_QUEUE_DIR.glob("gate_*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("state") == "open":
                    pending_gates += 1
            except Exception:
                continue

    # 2. Spool status (skipped entirely when the fresh index already knows it)
    pending_capsules = 0
    published_capsules = 0
    stalled_capsules = 0
    if not indexed:
        if SPOOL_DIR.is_dir():
            for entry in SPOOL_DIR.iterdir():
                if not entry.is_dir():
                    continue
                manifest_file = entry / "manifest.json"
                if not manifest_file.is_file():
                    continue
                try:
                    manifest = load_manifest(entry)
                    state = manifest.get("state")
                    if state == State.PENDING_APPROVAL.value:
                        pending_capsules += 1
                    elif state == State.PUBLISHED.value:
                        published_capsules += 1
                    elif state == State.STALLED.value:
                        stalled_capsules += 1
                except Exception:
                    continue

    # 3. Domain tickets count (also served by the fresh index when present)
    total_tickets = 0
    if not indexed:
        if DOMAINS_DIR.is_dir():
            # Mirror the read index's coverage exactly (read_index._scan_files):
            # legacy root tickets plus the approved/ and scratch/ subdirs that
            # create_ticket writes into. The old one-level glob counted only
            # root files and undercounted the dashboard when the index was off.
            for pattern in (
                "*/tickets/ticket_*.json",
                "*/tickets/approved/ticket_*.json",
                "*/tickets/scratch/ticket_*.json",
            ):
                for ticket_file in DOMAINS_DIR.glob(pattern):
                    total_tickets += 1

    # Provider-neutral: the cockpit asks whether the delivery gear is reachable,
    # not which vendor answers it. The tool's own name is reported per exchange
    # (metadata.gear_exchanges), never as a routing decision here.
    publishing_endpoint = resolve_endpoint(PUBLISHING_GEAR) or ""
    return {
        "pending_gates": pending_gates,
        "pending_capsules": indexed["capsules"]["pending"] if indexed else pending_capsules,
        "published_capsules": indexed["capsules"]["published"] if indexed else published_capsules,
        "stalled_capsules": indexed["capsules"]["stalled"] if indexed else stalled_capsules,
        "total_tickets": indexed["tickets"]["total"] if indexed else total_tickets,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "publishing_gear_configured": bool(publishing_endpoint),
        "publishing_gear_endpoint": publishing_endpoint or None,
        "active_branches": _domain_dirs(),
        "index": "sqlite" if indexed else "files",
    }


# --------------------------------------------------------------------------- #
# Telemetry, Cost Accounting & Reviewer Auditing API
# --------------------------------------------------------------------------- #

@router.get("/api/telemetry/costs")
async def get_telemetry_costs() -> Dict[str, Any]:
    """Granular cost accounting in USD and token telemetry across sectors and deliverables."""
    stats = read_index.stats() or {}
    caps_stats = stats.get("capsules", {})
    total_cost_usd = caps_stats.get("total_cost_usd", 0.0)
    total_tokens = caps_stats.get("total_tokens", 0)
    total_calls = caps_stats.get("total_calls", 0)

    sector_costs: Dict[str, float] = {}
    if read_index.enabled() and read_index.ensure():
        conn = read_index._connect()
        try:
            rows = conn.execute("SELECT domain, COALESCE(SUM(cost_usd), 0.0) FROM capsules GROUP BY domain").fetchall()
            sector_costs = {r[0]: round(r[1], 4) for r in rows}
        finally:
            conn.close()

    return {
        "status": "success",
        "total_cost_usd": round(total_cost_usd, 4),
        "total_tokens": total_tokens,
        "total_llm_calls": total_calls,
        "cost_by_sector": sector_costs,
        "currency": "USD",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class FinanceKillRequest(BaseModel):
    on: bool = Field(..., description="True flips the spend kill switch ON (all paid calls stop)")


@router.get("/api/finance/status")
async def get_finance_status() -> Dict[str, Any]:
    """The spend gate's live state: today/month spend, caps, subscriptions, switch.

    Backed by ``spend_control.status_snapshot`` — the same ledger the gateway
    and the image fetchers write to before/after every paid attempt, so these
    numbers include model calls, image generations and amortized
    subscriptions (config + newest ledger record per service).
    """
    from capsule_engine import spend_control

    try:
        snapshot = await asyncio.to_thread(spend_control.status_snapshot)
    except Exception as exc:
        logger.exception("finance status failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Finance status failed: {exc}") from exc
    return {"status": "success", **snapshot}


@router.post("/api/finance/kill")
async def set_finance_kill_switch(req: FinanceKillRequest, request: Request) -> Dict[str, Any]:
    """Flip the spend kill switch (capability ``finance:control``).

    ON means every paid call is refused at the choke point *before* the
    request (gateway retries included, image fetchers included), and the beat
    refuses to spawn ideator/dispatcher/directus. The environment flag
    ``AGENCY_SPEND_KILL=1`` is a separate, stronger switch that this endpoint
    can only report, never flip.
    """
    from capsule_engine import spend_control

    operator, audit_note = _decision_identity(request)
    try:
        result = await asyncio.to_thread(
            spend_control.set_kill_switch, bool(req.on), by=audit_note
        )
    except spend_control.SpendCapExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.warning("finance: kill switch %s by %s", result.get("kill"), operator)
    return {"status": "success", **result}


@router.get("/api/telemetry/reviewers")
async def get_telemetry_reviewers() -> Dict[str, Any]:
    """Audit scorecard for gate decisions, human reviewers, and acceptance rates."""
    stats = read_index.stats() or {}
    tickets_stats = stats.get("tickets", {})
    acceptance_rate = tickets_stats.get("acceptance_rate_percent", 100.0)

    reviewers_map: Dict[str, Dict[str, Any]] = {}
    total_reviews = 0
    if GATE_QUEUE_DIR.is_dir():
        for gf in GATE_QUEUE_DIR.glob("gate_*.json"):
            try:
                gdata = json.loads(gf.read_text(encoding="utf-8"))
            except Exception:
                continue
            reviewer = gdata.get("decided_by") or gdata.get("reviewer")
            if not reviewer:
                continue
            total_reviews += 1
            st = reviewers_map.setdefault(
                reviewer,
                {
                    "approved": 0,
                    "rejected": 0,
                    "total": 0,
                    "last_decision_at": None,
                    "last_gate_id": None,
                    "last_decision": None,
                },
            )
            st["total"] += 1
            gstate = gdata.get("state")
            if gstate == "approved":
                st["approved"] += 1
            elif gstate == "rejected":
                st["rejected"] += 1

            # Recency: newest decision by this reviewer, so the cockpit can tell
            # an idle reviewer from an active one (and show *which* gate).
            decision_at = gdata.get("decided_at") or gdata.get("opened_at")
            if decision_at and (
                st["last_decision_at"] is None or str(decision_at) > str(st["last_decision_at"])
            ):
                st["last_decision_at"] = decision_at
                st["last_gate_id"] = gdata.get("gate_id")
                st["last_decision"] = gdata.get("decision")

    return {
        "status": "success",
        "acceptance_rate_percent": acceptance_rate,
        "total_human_decisions": total_reviews,
        "reviewers": reviewers_map,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/engine/gears/health")
async def get_gears_health() -> Dict[str, Any]:
    """Health and probe inspection for the 16 constitutional gears."""
    from capsule_engine.gear_registry import registry_summary
    summary = registry_summary()
    return {
        "status": "success",
        **summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Executive Macro-Pillars API
# --------------------------------------------------------------------------- #

@router.get("/api/pillars")
async def get_pillars_endpoint() -> Dict[str, Any]:
    """Provide the 4 executive macro-pillars computed from live disk state."""
    from dataclasses import asdict
    from orchestration.pillars import get_executive_pillars

    tickets_res = await list_tickets()
    capsules_res = await list_capsules()
    gates_res = await list_gates()
    domains_res = await list_domains()

    pillars = get_executive_pillars(
        tickets=tickets_res,
        capsules=capsules_res,
        gates=gates_res,
        domains_data=domains_res,
    )

    return {
        "status": "success",
        "pillars": [asdict(p) for p in pillars],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Cryptographic Audit Verification API (Brick 4)
# --------------------------------------------------------------------------- #

@router.get("/api/audit/spool")
async def get_audit_spool_endpoint() -> Dict[str, Any]:
    """Verify cryptographic integrity of the entire capsule spool and audit ledger."""
    from capsule_engine.audit import verify_spool

    report = await asyncio.to_thread(verify_spool, SPOOL_DIR)
    return {
        "status": "success" if report.ok else "tampered",
        "ok": report.ok,
        "checked_capsules": report.checked_capsules,
        "findings": [
            {
                "kind": f.kind,
                "severity": f.severity,
                "capsule_id": f.capsule_id,
                "detail": f.detail,
            }
            for f in report.findings
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Read index endpoints (Scaling Brick 3) — views over a rebuildable cache
# --------------------------------------------------------------------------- #

@router.post("/api/index/rebuild")
async def rebuild_read_index() -> Dict[str, Any]:
    """Force a full rebuild of the read index from the files (the truth)."""
    count = await asyncio.to_thread(read_index.rebuild)
    return {
        "status": "success",
        "capsules_indexed": count,
        "message": f"Read index rebuilt from files: {count} capsules.",
    }


# --------------------------------------------------------------------------- #
# Branch registry: domains/* with real load and governance autonomy level
# --------------------------------------------------------------------------- #

@router.get("/api/domains")
async def list_domains() -> List[Dict[str, Any]]:
    """Declared branches with their live load and autonomy level.

    The directory *is* the branch: it exists before any ticket does, which is
    what lets a brand-new sector appear in the cockpit on day one. An empty
    domain is reported as an empty domain — never as fake activity.
    """
    try:
        governance = load_governance()
    except Exception as exc:  # corrupt/unreadable file → levels simply unknown
        logger.warning("domains: governance.json unreadable: %s", exc)
        governance = {}
    records = governance.get("domains") if isinstance(governance.get("domains"), dict) else {}

    domains: List[Dict[str, Any]] = []

    for name in _domain_dirs():
        record = records.get(name) if isinstance(records.get(name), dict) else {}
        level = level_for(name, state=governance)
        tickets_dir = DOMAINS_DIR / name / "tickets"
        ticket_count = sum(1 for _ in tickets_dir.glob("ticket_*.json")) if tickets_dir.is_dir() else 0
        domains.append({
            "id": name,
            # `name` mirrors `id`: a plain branch name for consumers that just
            # need the list (the cockpit's board builder reads `id`).
            "name": name,
            "tickets": ticket_count,
            "level": level,
            "level_label": LEVEL_LABELS.get(level, level),
            "demotions": int(record.get("demotions") or 0),
            "autonomy_note": record.get("last_demotion_reason"),
        })

    return domains


def _branch_doc_stub(title: str) -> str:
    """Default architecture doc seeded into a freshly founded branch."""
    return (
        f"# {title}\n\n"
        "> Branch Contract — see `docs/repo_structure.md`.\n\n"
        "## Responsibilities\n- (What value does this branch deliver?)\n\n"
        "## Task Stages (State → Worker)\n- (Declared lifecycle stages in task profile)\n\n"
        "## Scripts\n"
        "| File | Role |\n|---|---|\n"
        "| `agent_script.py` | Generate / evaluate requests |\n"
        "| `tracker_script.py` | Settle approved tickets |\n"
        "| `worker_script.py` | Bridge ticket to capsule |\n\n"
        "## External Interfaces\n- (Delivery gears / Email / CRM / Webhooks)\n"
    )


@router.post("/api/domains")
async def create_domain(req: CreateDomainRequest) -> Dict[str, Any]:
    """Create a new company branch directory dynamically on disk (Expansion Brick)."""
    clean_id = req.domain_id.strip().lower()
    if clean_id.startswith((".", "_")) or not clean_id.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid domain ID: alphanumeric and underscores only.")

    target_dir = DOMAINS_DIR / clean_id
    if target_dir.is_dir():
        raise HTTPException(status_code=409, detail=f"Domain '{clean_id}' already exists on disk.")

    # Create the branch directory structure
    (target_dir / "tickets").mkdir(parents=True, exist_ok=True)
    (target_dir / "deliverables").mkdir(parents=True, exist_ok=True)
    (target_dir / "archive").mkdir(parents=True, exist_ok=True)
    init_file = target_dir / "__init__.py"
    init_file.write_text(f'"""Domain package for {clean_id}."""\n', encoding="utf-8")

    # Git-visible skeleton (Branch Contract): empty folders vanish on clone, so
    # each queue keeps a whitelisted .gitkeep, and documentation is placed in docs/domains/
    for _sub in ("tickets", "deliverables", "archive"):
        (target_dir / _sub / ".gitkeep").touch()
    _docs_domains = REPO_ROOT / "docs" / "domains"
    _docs_domains.mkdir(parents=True, exist_ok=True)
    _arch = _docs_domains / f"{clean_id}.md"
    if not _arch.exists():
        _arch.write_text(_branch_doc_stub(req.title or clean_id), encoding="utf-8")

    # Record initial autonomy level in governance.json
    try:
        gov = load_governance()
        domains = gov.setdefault("domains", {})
        domains[clean_id] = {
            "level": req.level if req.level in KNOWN_LEVELS else "L0",
            "updated_at": now_iso(),
            "demotions": 0,
            "created_by": "cockpit_ceo",
        }
        gov["updated_at"] = now_iso()
        save_governance(gov)
    except Exception as exc:
        logger.warning("create_domain: could not update governance.json: %s", exc)

    return {
        "status": "success",
        "domain_id": clean_id,
        "message": f"Branch '{clean_id}' successfully founded.",
    }


# --------------------------------------------------------------------------- #
# Governance: Tickets API
# --------------------------------------------------------------------------- #

def _rel_path(path: Path) -> str:
    """Repo-relative path when possible, else absolute.

    ``DOMAINS_DIR``/``CAPSULES_SPOOL`` may point outside the repo (test or
    ops override); the path projection must not turn that into a 500.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _load_gate_status_map() -> Dict[str, Dict[str, Any]]:
    """Every gate-queue entry keyed by ``gate_id`` (for ticket correlation)."""
    gates: Dict[str, Dict[str, Any]] = {}
    if not GATE_QUEUE_DIR.is_dir():
        return gates
    for f in GATE_QUEUE_DIR.glob("gate_*.json"):
        try:
            gdata = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        gid = gdata.get("gate_id")
        if gid:
            gates[gid] = gdata
    return gates


def _allowed_sectors(request: Request) -> Tuple[bool, Optional[List[str]]]:
    """Resolve a caller's authorised sectors for SQL-side scoping.

    Returns ``(expressible, allowed)``:
      * ``(True, None)``  — unrestricted (root, or ``*`` in the profile);
      * ``(True, [..])``  — limited to those sectors;
      * ``(False, None)`` — the restriction cannot be expressed as a sector
        list (a bare capability principal), so the caller must fall back to
        the per-file scan that checks ``has()`` row by row.
    """
    principal = getattr(request.state, "principal", None) if request else None
    if principal is None or getattr(principal, "is_root", False):
        return True, None
    profile = (
        get_user_by_username(principal.subject) if hasattr(principal, "subject") else None
    )
    if profile is not None:
        secs = [s for s in profile.get("authorized_sectors", []) if isinstance(s, str) and s]
        if "*" in secs:
            return True, None
        return True, secs
    return False, None


def _ticket_scope_allows(request: Request, sec: str) -> bool:
    """Per-row scope check for the fallback file scan (mirrors the SQL filter)."""
    principal = getattr(request.state, "principal", None) if request else None
    if not principal or getattr(principal, "is_root", False):
        return True
    profile = (
        get_user_by_username(principal.subject) if hasattr(principal, "subject") else None
    )
    if profile is not None:
        secs = profile.get("authorized_sectors", [])
        return "*" in secs or sec in secs
    if hasattr(principal, "has"):
        return principal.has("ticket:create", resource=sec) or principal.has(
            "capsule:approve", resource=sec
        )
    return True


def _capsule_scope_allows(request: Request, domain: str) -> bool:
    """Per-row scope check for the fallback capsule scan (mirrors the SQL filter)."""
    principal = getattr(request.state, "principal", None) if request else None
    if not principal or getattr(principal, "is_root", False):
        return True
    profile = (
        get_user_by_username(principal.subject) if hasattr(principal, "subject") else None
    )
    if profile is not None:
        secs = profile.get("authorized_sectors", [])
        return "*" in secs or domain in secs
    if hasattr(principal, "has"):
        return principal.has("capsule:approve", resource=domain) or principal.has(
            "capsule:create", resource=domain
        )
    return True


def _project_ticket(
    tpath: Path, tstage: str, tdata: Dict[str, Any], gate_status_map: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Normalize one ticket record into the list projection (no I/O, no scope)."""
    parent_name = tpath.parent.name
    sec = tdata.get("sector") or (
        tpath.parent.parent.parent.name
        if parent_name in ("scratch", "approved", "archive")
        else tpath.parent.parent.name
    )
    tid = tdata.get("ticket_id") or tdata.get("id") or tpath.stem
    req = tdata.get("request") or tdata.get("content") or ""
    decision = tdata.get("decision") if isinstance(tdata.get("decision"), dict) else {}
    metadata = tdata.get("metadata") if isinstance(tdata.get("metadata"), dict) else {}

    gate_id = tdata.get("gate_id") or tdata.get("via") or metadata.get("gate_id")
    if not gate_id and "via gate " in str(decision.get("reason") or ""):
        parts = str(decision["reason"]).split("via gate ")
        if len(parts) > 1:
            gate_id = parts[1].split()[0].strip()

    gate_info = gate_status_map.get(gate_id) if gate_id else None
    gate_status = gate_info.get("state") if gate_info else tdata.get("state", "open")
    if gate_info and gate_info.get("decision") == "approved":
        gate_status = "approved"
    elif gate_info and gate_info.get("decision") == "rejected":
        gate_status = "rejected"

    title = metadata.get("title") or tdata.get("title") or tdata.get("topic")
    if not title and req:
        lines = [line.strip() for line in str(req).split("\n") if line.strip()]
        title = lines[0].replace("#", "").strip() if lines else tid

    return {
        "id": tid,
        "ticket_id": tid,
        "sector": sec,
        "title": title or tid,
        "content": req,
        "stage": tdata.get("stage") or tstage,
        "state": tdata.get("state", "open"),
        "status": tdata.get("state", "open"),
        "created_at": tdata.get("created_at") or metadata.get("timestamp") or "Unknown",
        "cadence": metadata.get("cadence") or tdata.get("cadence") or "weekly_routine",
        "deliverable_type": tdata.get("deliverable_type") or metadata.get("deliverable_type") or "auto",
        "context": tdata.get("context") or {},
        "metadata": metadata,
        "gate_id": gate_id,
        "gate_status": gate_status,
        "settled": bool(tdata.get("via") or tdata.get("settled") or tdata.get("spool_capsule")),
        "decision_by": gate_info.get("decided_by") if gate_info else tdata.get("decision_by"),
        "claimed_by": tdata.get("claimed_by"),
        "resolved_by": tdata.get("resolved_by"),
        "decision": decision,
        "spool_capsule": tdata.get("spool_capsule") or metadata.get("spool_capsule"),
        "file_path": _rel_path(tpath),
    }


@router.get("/api/tickets")
async def list_tickets(
    request: Request = None,
    sector: Optional[str] = None,
    state: Optional[str] = None,
    stage: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Scan all domain tickets and return them with normalized governance metadata.

    Pagination (Scaling Brick 4): no ``limit`` returns the legacy full list by
    reading every file; with ``limit`` (1–500) the SQLite read index selects
    the page and **only those files are read** (order: ``created_at`` DESC — the
    index's ordering, which may differ from the legacy path's). ``state``
    filters on the RAW on-disk ``state`` value; ``stage`` is 'scratch',
    'approved', 'archive', or 'all'.
    """
    if limit is not None and not (1 <= int(limit) <= 500):
        raise HTTPException(status_code=400, detail="limit must be within 1..500")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    gate_status_map = _load_gate_status_map()

    # --- Index fast path (paged reads only) ---------------------------------
    if limit is not None and read_index.enabled():
        usable, allowed = _allowed_sectors(request)
        # Mirror the fallback scan's stage coverage exactly: default =
        # approved + scratch (no archive), 'archive' = archived only,
        # 'all' = every stage.
        if stage == "scratch":
            index_stages: Optional[List[str]] = ["scratch"]
        elif stage == "archive":
            index_stages = ["archived"]
        elif stage == "all":
            index_stages = None
        else:  # 'approved' or None
            index_stages = ["approved", "scratch"]
        if usable and read_index.ensure():
            rows = read_index.query_tickets(
                sector=sector,
                sectors=allowed,
                stages=index_stages,
                state=state,
                limit=int(limit),
                offset=int(offset),
            )
            page: List[Dict[str, Any]] = []
            for row in rows:
                tpath = Path(str(row.get("path") or ""))
                try:
                    tdata = json.loads(tpath.read_text(encoding="utf-8"))
                except Exception:
                    continue
                page.append(
                    _project_ticket(
                        tpath, str(row.get("stage") or "approved"), tdata, gate_status_map
                    )
                )
            return page

    # --- Fallback: full file scan (no limit, index off, or opaque scope) ----
    tickets: List[Dict[str, Any]] = []
    candidate_files: List[Tuple[Path, str]] = []
    sec_glob = sector if sector else "*"
    if DOMAINS_DIR.is_dir():
        if not stage or stage in ("approved", "all"):
            for p in sorted(DOMAINS_DIR.glob(f"{sec_glob}/tickets/approved/ticket_*.json"), reverse=True):
                if "sync-conflict" not in p.name and not p.name.endswith(".tmp"):
                    candidate_files.append((p, "approved"))
            # Legacy root tickets
            for p in sorted(DOMAINS_DIR.glob(f"{sec_glob}/tickets/ticket_*.json"), reverse=True):
                if "sync-conflict" not in p.name and not p.name.endswith(".tmp"):
                    candidate_files.append((p, "approved"))

        if not stage or stage in ("scratch", "all"):
            for p in sorted(DOMAINS_DIR.glob(f"{sec_glob}/tickets/scratch/ticket_*.json"), reverse=True):
                if "sync-conflict" not in p.name and not p.name.endswith(".tmp"):
                    candidate_files.append((p, "scratch"))

        if stage in ("archive", "all"):
            for p in sorted(DOMAINS_DIR.glob(f"{sec_glob}/tickets/archive/ticket_*.json"), reverse=True):
                if "sync-conflict" not in p.name and not p.name.endswith(".tmp"):
                    candidate_files.append((p, "archived"))

    seen_tids = set()
    for tpath, tstage in candidate_files:
        try:
            tdata = json.loads(tpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if state and tdata.get("state") != state:
            continue

        parent_name = tpath.parent.name
        sec = tdata.get("sector") or (
            tpath.parent.parent.parent.name
            if parent_name in ("scratch", "approved", "archive")
            else tpath.parent.parent.name
        )
        if not _ticket_scope_allows(request, sec):
            continue

        tid = tdata.get("ticket_id") or tdata.get("id") or tpath.stem
        if tid in seen_tids:
            continue
        seen_tids.add(tid)
        tickets.append(_project_ticket(tpath, tstage, tdata, gate_status_map))

    if limit is not None:
        return tickets[int(offset):int(offset) + int(limit)]
    return tickets


@router.post("/api/tickets")
async def create_ticket(req: CreateTicketRequest, request: Request) -> Dict[str, Any]:
    """Create a new manual ticket in scratch or approved stage."""
    target_sector = req.sector.strip().lower()
    _require_scope(request, "ticket:create", target_sector)
    sector_dir = DOMAINS_DIR / target_sector
    if not sector_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Sector '{target_sector}' does not exist.")

    tickets_dir = sector_dir / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    now_dt = datetime.now(timezone.utc)
    ts_str = now_dt.strftime("%Y%m%dT%H%M%SZ")
    import uuid
    uid_str = uuid.uuid4().hex[:8]
    prefix = "human" if req.stage == "approved" else "scratch"
    ticket_id = f"T-{prefix.upper()}-{ts_str}-{uid_str}"

    full_content = f"# {req.title.strip()}\n\n{req.content.strip()}"

    meta_scratch = {
        "title": req.title.strip(),
        "cadence": req.cadence,
        "stage": "scratch",
        "deliverable_type": req.deliverable_type or "auto",
        "source": "cockpit_ui",
        "timestamp": now_dt.isoformat(),
        **(req.metadata or {}),
    }

    if req.stage == "scratch":
        # Scratch / draft tickets do not raise gates immediately
        scratch_dir = tickets_dir / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        ticket_data = {
            "ticket_id": ticket_id,
            "client_id": "human_cockpit",
            "sector": target_sector,
            "stage": "scratch",
            "title": req.title.strip(),
            "request": full_content,
            "state": "draft",
            "created_at": now_dt.isoformat(),
            "cadence": req.cadence,
            "deliverable_type": req.deliverable_type or "auto",
            "context": req.context or {},
            "metadata": meta_scratch,
        }
        stamp_schema_version(KIND_TICKET, ticket_data)
        ticket_file = scratch_dir / f"ticket_{ticket_id}.json"
        atomic_json(ticket_file, ticket_data, trailing_newline=False)
        return {
            "status": "success",
            "ticket_id": ticket_id,
            "stage": "scratch",
            "deliverable_type": req.deliverable_type or "auto",
            "message": f"Draft ticket {ticket_id} created in scratch stage.",
        }

    # Approved stage: raise Approval Gate via gate_script
    gate_script = GATE_DIR / "gate_script.py"
    gate_entry_payload = {
        "source": {
            "sector": target_sector,
            "client_id": "human_cockpit",
            "request": full_content,
        },
        "risk_tier": "external",
        "reason": f"Manual idea ticket '{req.title[:40]}' requires human approval",
    }

    try:
        rc, out, err = await _run_subprocess(
            [sys.executable, str(gate_script), "raise"],
            input_text=json.dumps(gate_entry_payload, ensure_ascii=False),
        )
        if rc != 0:
            raise RuntimeError(err.strip() or f"exit {rc}")
        gate_res = json.loads(out)
        gate_id = gate_res.get("gate_id")
    except ScriptTimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Gate raise timed out: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to raise governance gate: {exc}")

    approved_dir = tickets_dir / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)

    meta_approved = {
        "title": req.title.strip(),
        "cadence": req.cadence,
        "stage": "approved",
        "deliverable_type": req.deliverable_type or "auto",
        "source": "cockpit_ui",
        "timestamp": now_dt.isoformat(),
        **(req.metadata or {}),
    }

    ticket_data = {
        "ticket_id": ticket_id,
        "client_id": "human_cockpit",
        "sector": target_sector,
        "stage": "approved",
        "title": req.title.strip(),
        "request": full_content,
        "state": "pending_approval",
        "created_at": now_dt.isoformat(),
        "gate_id": gate_id,
        "cadence": req.cadence,
        "deliverable_type": req.deliverable_type or "auto",
        "context": req.context or {},
        "metadata": meta_approved,
        "decision": {
            "status": "needs_gate",
            "risk_tier": "external",
            "reason": f"Gate {gate_id} raised for human authorization",
        },
    }
    stamp_schema_version(KIND_TICKET, ticket_data)
    ticket_file = approved_dir / f"ticket_human_{ticket_id}.json"
    atomic_json(ticket_file, ticket_data, trailing_newline=False)

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "gate_id": gate_id,
        "stage": "approved",
        "message": f"Ticket {ticket_id} created and gate {gate_id} opened successfully.",
    }


@router.patch("/api/tickets/{ticket_id}/context")
async def update_ticket_context(ticket_id: str, req: UpdateTicketContextRequest, request: Request, sector: Optional[str] = None) -> Dict[str, Any]:
    """Update dynamic context dictionary and metadata of an existing ticket with changelog auditing."""
    clean_id = Path(ticket_id).name.strip()
    found = find_ticket(clean_id, sector)
    if not found:
        raise HTTPException(status_code=404, detail=f"Ticket '{clean_id}' not found.")
    target_sector, stage, ticket_path, tdata = found

    _require_scope(request, "ticket:create", target_sector)

    principal = getattr(request.state, "principal", None)
    updater = getattr(principal, "subject", "unknown")
    changelog_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": updater,
        "updated_keys": list(req.context.keys()),
        "note": req.note,
    }

    def _apply_context(state):
        """Merge into whatever is on disk *now*, under the sidecar lock.

        Re-reading here is what keeps this PATCH from clobbering a dispatcher
        rewrite or a settle-bridge update that landed after ``find_ticket``.
        """
        if not isinstance(state, dict):
            return None  # ticket vanished (claimed/moved) — write nothing
        cur_context = state.setdefault("context", {})
        if not isinstance(cur_context, dict):
            cur_context = {}
            state["context"] = cur_context
        cur_context.update(req.context)

        if req.metadata:
            cur_meta = state.setdefault("metadata", {})
            if isinstance(cur_meta, dict):
                cur_meta.update(req.metadata)

        changelog = state.setdefault("changelog", [])
        if isinstance(changelog, list):
            changelog.append(changelog_entry)
        stamp_schema_version(KIND_TICKET, state)
        return state

    updated = update_json(ticket_path, _apply_context, default=None, trailing_newline=False)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Ticket '{clean_id}' not found.")
    tdata = updated

    return {
        "status": "success",
        "ticket_id": clean_id,
        "sector": target_sector,
        "stage": stage,
        "context": tdata.get("context"),
        "changelog_entries": len(tdata.get("changelog", [])),
        "message": f"Context for ticket {clean_id} updated successfully.",
    }


@router.post("/api/tickets/{ticket_id}/promote")
async def promote_ticket_endpoint(ticket_id: str, request: Request, sector: Optional[str] = None) -> Dict[str, Any]:
    """Promote a ticket from scratch/draft to approved stage."""
    clean_id = Path(ticket_id).name.strip()
    found = find_ticket(clean_id, sector)
    if not found:
        raise HTTPException(status_code=404, detail=f"Ticket '{clean_id}' not found.")
    sec = found[0]
    _require_scope(request, "ticket:create", sec)
    try:
        return promote_ticket(clean_id, sec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/tickets/{ticket_id}/demote")
async def demote_ticket_endpoint(ticket_id: str, request: Request, sector: Optional[str] = None) -> Dict[str, Any]:
    """Demote a ticket from approved to scratch stage."""
    clean_id = Path(ticket_id).name.strip()
    found = find_ticket(clean_id, sector)
    if not found:
        raise HTTPException(status_code=404, detail=f"Ticket '{clean_id}' not found.")
    sec = found[0]
    _require_scope(request, "ticket:create", sec)
    try:
        return demote_ticket(clean_id, sec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/tickets/{ticket_id}")
async def get_ticket_endpoint(
    ticket_id: str, request: Request, sector: Optional[str] = None
) -> Dict[str, Any]:
    """Full detail for a single ticket — content, context, steps, decisions.

    Fetches one ticket by id across every domain (optionally narrowed with
    ``?sector=``) without pulling a whole sector list: its full request body,
    normalized governance metadata, ``history``/``changelog`` decision log, and
    the correlated gate + capsule link. ``raw`` carries the untouched on-disk
    record for anything not projected above.
    """
    clean_id = Path(ticket_id).name.strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="ticket_id is required.")
    found = find_ticket(clean_id, sector)
    if not found:
        raise HTTPException(status_code=404, detail=f"Ticket '{clean_id}' not found.")
    sec, stage, tpath, tdata = found
    # Branch scope is authoritative once the ticket's sector is known.
    _require_scope(request, "factory:read", sec)

    metadata = tdata.get("metadata") if isinstance(tdata.get("metadata"), dict) else {}
    decision = tdata.get("decision") if isinstance(tdata.get("decision"), dict) else {}
    context = tdata.get("context") if isinstance(tdata.get("context"), dict) else {}
    req = tdata.get("request") or tdata.get("content") or ""

    gate_id = tdata.get("gate_id") or tdata.get("via") or metadata.get("gate_id")
    if not gate_id and "via gate " in str(decision.get("reason") or ""):
        gate_id = str(decision["reason"]).split("via gate ")[1].split()[0].strip()

    gate_info: Optional[Dict[str, Any]] = None
    if gate_id:
        gate_file = GATE_QUEUE_DIR / f"gate_{gate_id}.json"
        try:
            gate_info = json.loads(gate_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gate_info = None

    gate_status = gate_info.get("state") if gate_info else tdata.get("state", "open")
    if gate_info and gate_info.get("decision") == "approved":
        gate_status = "approved"
    elif gate_info and gate_info.get("decision") == "rejected":
        gate_status = "rejected"

    title = metadata.get("title") or tdata.get("title") or tdata.get("topic")
    if not title and req:
        lines = [line.strip() for line in str(req).split("\n") if line.strip()]
        title = lines[0].replace("#", "").strip() if lines else clean_id

    # Repo-relative when possible; an out-of-tree DOMAINS_DIR would otherwise
    # turn a readable ticket into a 500 on the path projection alone.
    try:
        rel_path = str(tpath.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(tpath)

    return {
        "status": "success",
        "id": tdata.get("ticket_id") or tdata.get("id") or tpath.stem,
        "ticket_id": tdata.get("ticket_id") or tdata.get("id") or tpath.stem,
        "sector": sec,
        "stage": tdata.get("stage") or stage,
        "title": title or clean_id,
        "content": req,
        "state": tdata.get("state", "open"),
        "created_at": tdata.get("created_at") or metadata.get("timestamp") or "Unknown",
        "cadence": metadata.get("cadence") or tdata.get("cadence") or "weekly_routine",
        "deliverable_type": tdata.get("deliverable_type") or metadata.get("deliverable_type") or "auto",
        "context": context,
        "metadata": metadata,
        "steps": tdata.get("steps") or [],
        "history": tdata.get("history") or [],
        "changelog": tdata.get("changelog") or [],
        "decision": decision,
        "gate_id": gate_id,
        "gate_status": gate_status,
        "gate": gate_info,
        "decided_by": gate_info.get("decided_by") if gate_info else tdata.get("decision_by"),
        "claimed_by": tdata.get("claimed_by"),
        "resolved_by": tdata.get("resolved_by"),
        "capsule_id": tdata.get("spool_capsule") or tdata.get("capsule_id") or metadata.get("capsule_id"),
        "spool_capsule": tdata.get("spool_capsule") or metadata.get("spool_capsule"),
        "file_path": rel_path,
        "raw": tdata,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Governance: Approval Gates API
# --------------------------------------------------------------------------- #

@router.get("/api/gates")
async def list_gates() -> List[Dict[str, Any]]:
    """Return all governance approval gates.

    Each gate is linked to the capsule it holds up: ``capsule_id`` (null until
    a capsule has been spawned from the gated ticket) and ``capsule_title``
    (the human title, always present when the gate blocks a known ticket).
    ``ticket_id`` is the join key the link was resolved from. This lets the
    cockpit show *which capsule is waiting* without matching gate ids by hand.
    """
    gates: List[Dict[str, Any]] = []
    if not GATE_QUEUE_DIR.is_dir():
        return []

    link_index = _gate_link_index()

    for f in sorted(GATE_QUEUE_DIR.glob("gate_*.json"), reverse=True):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            link = link_index.get(str(entry.get("gate_id"))) or {}
            gates.append({
                "id": entry.get("gate_id"),
                "gate_id": entry.get("gate_id"),
                "state": entry.get("state"),
                "decision": entry.get("decision"),
                "reason": entry.get("reason"),
                "risk_tier": entry.get("risk_tier"),
                "opened_at": entry.get("opened_at"),
                "decided_by": entry.get("decided_by"),
                "decided_at": entry.get("decided_at"),
                "decision_reason": entry.get("decision_reason"),
                "source": entry.get("source"),
                "capsule_id": entry.get("capsule_id") or link.get("capsule_id"),
                "capsule_title": (
                    entry.get("capsule_title")
                    or link.get("capsule_title")
                    or (source.get("title") if isinstance(source, dict) else None)
                ),
                "ticket_id": entry.get("ticket_id") or link.get("ticket_id"),
            })
        except Exception:
            continue

    return gates


@router.post("/api/gates/{gate_id}/approve")
async def approve_gate(gate_id: str, req: ApproveGateRequest, request: Request) -> Dict[str, Any]:
    """Explicit human approval of a governance gate (invokes gate_script.py decide)."""
    clean_id = gate_id.strip()
    # Identity comes from the verified credential, never the request body.
    reviewer, _ = _decision_identity(request)
    _require_gate_scope(request, clean_id, "gate:approve")
    gate_script = GATE_DIR / "gate_script.py"

    cmd = [
        sys.executable,
        str(gate_script),
        "decide",
        "--gate-id",
        clean_id,
        "--by",
        reviewer,
        "--decision",
        "approved",
    ]
    if req.reason:
        cmd.extend(["--reason", req.reason.strip()])

    try:
        rc, out, err = await _run_subprocess(cmd)
    except ScriptTimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Gate execution timed out: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gate execution error: {exc}")
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"Gate approval failed: {err.strip() or rc}")
    try:
        gate_res = json.loads(out)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gate execution error: {exc}")

    return {
        "status": "success",
        "gate_id": clean_id,
        "decision": "approved",
        "decided_by": gate_res.get("decided_by"),
        "message": f"Gate {clean_id} approved by {gate_res.get('decided_by')}.",
    }


@router.post("/api/gates/{gate_id}/reject")
async def reject_gate(gate_id: str, req: RejectRequest, request: Request) -> Dict[str, Any]:
    """Explicit human rejection of a governance gate with mandatory reason."""
    clean_id = gate_id.strip()
    # Identity comes from the verified credential, never the request body.
    reviewer, _ = _decision_identity(request)
    _require_gate_scope(request, clean_id, "gate:reject")
    gate_script = GATE_DIR / "gate_script.py"

    cmd = [
        sys.executable,
        str(gate_script),
        "decide",
        "--gate-id",
        clean_id,
        "--by",
        reviewer,
        "--decision",
        "rejected",
        "--reason",
        req.reason.strip(),
    ]

    try:
        rc, out, err = await _run_subprocess(cmd)
    except ScriptTimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Gate execution timed out: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gate execution error: {exc}")
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"Gate rejection failed: {err.strip() or rc}")
    try:
        gate_res = json.loads(out)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gate execution error: {exc}")

    return {
        "status": "rejected",
        "gate_id": clean_id,
        "decision": "rejected",
        "reason": req.reason.strip(),
        "decided_by": gate_res.get("decided_by"),
        "message": f"Gate {clean_id} rejected with reason: {req.reason.strip()}",
    }


# --------------------------------------------------------------------------- #
# Governance: Settle Engine & Auto-Ideator
# --------------------------------------------------------------------------- #

def _mark_ticket_bridged(ticket_path: Path, capsule_id: str) -> None:
    """Settle-bridge state update: a lock-serialized read-modify-write.

    The ticket is read at the top of the settle loop, *before* the worker
    subprocess runs, so writing that stale copy back would silently drop a
    context PATCH, a dispatcher rewrite or a tracker update that landed in
    the meantime. Re-read under the ticket's sidecar lock and apply only the
    bridge's own two fields; if the ticket vanished mid-flight, write nothing.
    """
    def _mark(state):
        if not isinstance(state, dict):
            return None
        state["spool_capsule"] = capsule_id
        state["state"] = "settled"
        stamp_schema_version(KIND_TICKET, state)
        return state

    update_json(ticket_path, _mark, trailing_newline=False)


@router.post("/api/tickets/settle")
async def settle_tickets() -> Dict[str, Any]:
    """Pull approved gates across *all* domains and bridge them to the spool.

    Domain-agnostic by design: every domain directory is visited, and each
    contributes whatever scripts it actually ships (tracker → settle approved
    gates, worker → bridge accepted tickets into the spool). A domain without
    scripts is skipped and reported, never simulated. Ideation is just one
    branch of this loop — not the whole engine.
    """
    settled_results: List[Dict[str, Any]] = []
    bridged_capsules: List[str] = []
    skipped: List[str] = []

    for domain in _domain_dirs():
        scripts = _domain_scripts(domain)
        tracker, worker = scripts["tracker"], scripts["worker"]

        # 1. Domain settle: pull approved gates into settled tickets
        if tracker.is_file():
            try:
                rc, out, err = await _run_subprocess(
                    [sys.executable, str(tracker), "settle"]
                )
                if rc != 0:
                    raise RuntimeError(err.strip() or f"exit {rc}")
                raw = out.strip()
                if raw:
                    try:
                        settled_results.append(json.loads(raw))
                    except Exception:
                        settled_results.append({"output": raw})
            except Exception as exc:
                logger.warning("%s tracker settle failed: %s", domain, exc)
        elif worker.is_file():
            skipped.append(domain)  # has a bridge but no tracker: nothing to settle

        # 2. Bridge newly accepted tickets of this domain to the spool
        tickets_dir = DOMAINS_DIR / domain / "tickets"
        if tickets_dir.is_dir() and worker.is_file():
            for tf in tickets_dir.glob("ticket_*.json"):
                try:
                    tdata = json.loads(tf.read_text(encoding="utf-8"))
                except Exception:
                    continue

                # If ticket was settled from gate and has not yet created a capsule
                if tdata.get("state") == "open" and tdata.get("via") and not tdata.get("spool_capsule"):
                    decision = tdata.get("decision", {})
                    if decision.get("status") == "accepted":
                        try:
                            bridge_rc, bridge_out, bridge_err = await _run_subprocess(
                                [sys.executable, str(worker)],
                                input_text=json.dumps(tdata, ensure_ascii=False),
                            )
                            if bridge_rc != 0:
                                raise RuntimeError(bridge_err.strip() or f"exit {bridge_rc}")
                            worker_res = json.loads(bridge_out)
                            if worker_res.get("result") == "done":
                                capsule_id = worker_res.get("context", {}).get("capsule_id")
                                _mark_ticket_bridged(tf, capsule_id)
                                bridged_capsules.append(capsule_id)
                        except Exception as w_exc:
                            logger.warning("Ticket bridge failed for %s/%s: %s", domain, tf.name, w_exc)

    # 3. Automatically advance newly spawned capsules through copy and design stages
    pulse_summary = ""
    if bridged_capsules:
        try:
            pulse_report = await asyncio.to_thread(run_pulse, SPOOL_DIR, scan="incremental")
            pulse_summary = f" (Factory progressed: {len(pulse_report.get('executed', []))} stages, {len(pulse_report.get('held', []))} pending approval)"
            await asyncio.to_thread(read_index.apply_pulse_report, pulse_report)
        except Exception as p_exc:
            logger.warning("Auto-pulse after settle failed: %s", p_exc)

    return {
        "status": "success",
        "settled_batches": settled_results,
        "bridged_capsules": bridged_capsules,
        "domains_without_tracker": skipped,
        "message": f"Settled across {_domain_dirs().__len__()} domains: {len(bridged_capsules)} new capsules in factory{pulse_summary}.",
    }


@router.post("/api/pulse/run")
async def trigger_factory_pulse(scan: str = "incremental") -> Dict[str, Any]:
    """Execute one factory pulse pass across spool deliverables.

    Runs in a worker thread so the event loop keeps serving requests, and
    defaults to the incremental scanner (Scaling Brick 2): only changed, new,
    or time-sensitive capsules are routed. Kill switch:
    ``AGENCY_INCREMENTAL_PULSE=off`` (or ``?scan=full``) restores the sweep.
    """
    try:
        report = await asyncio.to_thread(run_pulse, SPOOL_DIR, scan=scan)
        await asyncio.to_thread(read_index.apply_pulse_report, report)
        executed_count = len(report.get("executed", []))
        held_count = len(report.get("held", []))
        return {
            "status": "success",
            "report": report,
            "message": f"Factory pulse completed: {executed_count} stages executed, {held_count} capsules pending review or publishing.",
        }
    except Exception as exc:
        logger.exception("Pulse execution error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Pulse failed: {exc}")


@router.get("/api/queue")
async def get_work_queue(
    state: str = Query("active", description="active | pending | claimed | done | failed | cancelled | all"),
    kind: Optional[str] = Query(None, description="Filter by kind (e.g. capsule.advance)"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """The durable work queue: depth, due-vs-delayed, and the items themselves.

    This is the explicit scheduling channel beside the pulse sweep — priority,
    delay and retry budget, one JSON item per unit of work (data/queue).
    """
    try:
        queue_stats = work_queue.stats()
        items = work_queue.list_items(state=state, kind=kind, limit=limit)
    except work_queue.QueueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "stats": queue_stats,
        "items": items,
        "state": state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/queue")
async def enqueue_work(body: EnqueueWorkRequest) -> Dict[str, Any]:
    """Schedule one unit of work (priority, delay, retry budget, idempotency).

    ``capsule_id`` is the ergonomic path — it builds the ``capsule.advance``
    payload from the served spool; an explicit ``payload`` wins for advanced
    callers. Re-posting the same ``idempotency_key`` hands back the live item
    instead of duplicating it.
    """
    payload = dict(body.payload or {})
    if body.capsule_id and not payload.get("capsule_path"):
        clean_id = Path(body.capsule_id).name.strip()
        capsule_path = _confined_capsule_path(clean_id)
        payload["capsule_path"] = str(capsule_path)
        payload.setdefault("capsule_id", clean_id)
    try:
        item = work_queue.enqueue(
            body.kind,
            payload,
            priority=body.priority,
            delay_seconds=body.delay_seconds,
            max_attempts=body.max_attempts,
            idempotency_key=body.idempotency_key,
        )
    except work_queue.QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except work_queue.QueueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "status": "queued",
        "item": item,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/queue/drain")
async def drain_queued_work(limit: Optional[int] = Query(None, ge=1, le=200)) -> Dict[str, Any]:
    """Run due queue items now (``capsule.advance``) — no spool sweep.

    The pulse drains the queue on its own clock; this endpoint is the
    on-demand version for a cockpit "run now" button.
    """
    try:
        report = await asyncio.to_thread(drain_capsule_queue, limit=limit)
    except Exception as exc:
        logger.exception("Queue drain error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Queue drain failed: {exc}")
    return {
        "status": "success",
        "report": report,
        "message": (
            f"Queue drained: {report.get('claimed', 0)} claimed, "
            f"{report.get('completed', 0)} done, {report.get('retried', 0)} retry, "
            f"{report.get('dead', 0)} dead, {report.get('deferred', 0)} deferred."
        ),
    }


@router.post("/api/queue/{item_id}/cancel")
async def cancel_queued_work(item_id: str) -> Dict[str, Any]:
    """Cancel a pending or claimed queue item.

    Final for pending work; for a claimed item it prevents every future
    attempt but cannot un-run a handler that already started.
    """
    item = work_queue.cancel(Path(item_id).name.strip())
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"Queue item '{item_id}' not found (or already terminal).",
        )
    return {
        "status": "cancelled",
        "item": item,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/ideation/run")
async def trigger_auto_ideation() -> Dict[str, Any]:
    """Execute the auto_ideator engine to generate a high-impact idea and raise a gate."""
    auto_ideator_path = DOMAINS_DIR / "ideation" / "auto_ideator.py"
    if not auto_ideator_path.is_file():
        raise HTTPException(status_code=404, detail="auto_ideator.py not found in domains/ideation/")

    try:
        rc, out, err = await _run_subprocess(
            [sys.executable, str(auto_ideator_path), "--force"]
        )
        if rc != 0:
            raise RuntimeError(err.strip() or f"exit {rc}")
        res = json.loads(out)
    except ScriptTimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"auto_ideator execution timed out: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error running auto_ideator: {exc}")

    return {
        "status": "success",
        "ticket_id": res.get("ticket_id"),
        "gate_id": res.get("gate_id"),
        "topic": res.get("topic"),
        "message": f"Idea generated: '{res.get('topic')}'. Gate {res.get('gate_id')} raised.",
    }


# --------------------------------------------------------------------------- #
# Cadence, AI Agents, Engine Gears & Executive Tools
# --------------------------------------------------------------------------- #

@router.get("/api/cadence")
async def get_cadence_status() -> Dict[str, Any]:
    """Real 48-hour cadence guard, cron heartbeat, and weekly schedule status."""
    try:
        from domains.ideation.auto_ideator import check_cadence, DEFAULT_CADENCE_HOURS
        can_proceed, elapsed_hours, latest_id, latest_dt = check_cadence(DEFAULT_CADENCE_HOURS)
    except Exception as exc:
        logger.warning("get_cadence_status error: %s", exc)
        can_proceed, elapsed_hours, latest_id, latest_dt = True, None, None, None

    threshold = 48.0
    progress_pct = 100.0
    next_due_iso = None
    if elapsed_hours is not None and elapsed_hours != float("inf"):
        progress_pct = min(100.0, round((elapsed_hours / threshold) * 100.0, 1))
        if latest_dt and not can_proceed:
            from datetime import timedelta
            next_due_iso = (latest_dt + timedelta(hours=threshold)).isoformat()

    # Read latest beat from pulse.log
    pulse_log_path = ENGINE_ROOT / "orchestration" / "pulse.log"
    last_beat = None
    if pulse_log_path.is_file():
        try:
            lines = pulse_log_path.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    last_beat = json.loads(line)
                    break
        except Exception:
            pass

    # Read crontab status
    cron_installed = False
    cron_lines = []
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "company-engine" in line and not line.strip().startswith("#"):
                    cron_installed = True
                    cron_lines.append(line.strip())
    except Exception:
        pass

    return {
        "cadence_guard": {
            "threshold_hours": threshold,
            "elapsed_hours": round(elapsed_hours, 2) if elapsed_hours not in (None, float("inf")) else None,
            "progress_pct": progress_pct,
            "can_proceed": bool(can_proceed),
            "latest_ticket_id": latest_id,
            "latest_ticket_time": latest_dt.isoformat() if latest_dt else None,
            "next_due_at": next_due_iso,
        },
        "weekly_schedule": {
            "routine_label": "Weekly Strategic Leadership Post",
            "recommended_target": "Friday 11:00 AM (UTC+3 / Cairo/Riyadh)",
            "channel": "LinkedIn / Facebook via the social:publish gear",
        },
        "cron": {
            "active": cron_installed,
            "matched_entries": cron_lines,
            "last_beat": last_beat,
        },
    }


@router.get("/api/agents")
async def list_agents() -> List[Dict[str, Any]]:
    """Directory of all AI agents in the company with real metrics and activity."""
    from workers import WORKERS, domain_workers

    core_agent_defs = {
        "ideator": {
            "name_ar": "Chief Strategy Officer (Auto-Ideator)",
            "name_en": "Chief Strategy Officer",
            "domain": "ideation",
            "icon": "💡",
            "description": "Explores strategic angles and generates thought-leadership ideas under archive memory control.",
            "stages": ["propose_idea"],
        },
        "marketing": {
            "name_ar": "Lead Content Copywriting Agent",
            "name_en": "Lead Content & Marketing Worker",
            "domain": "marketing",
            "icon": "📢",
            "description": "Crafts high-impact executive marketing copy tailored for decision makers without buzzwords.",
            "stages": ["draft", "in_progress", "pending_qa"],
        },
        "qa": {
            "name_ar": "Zero-Waste Quality Assurance Gate",
            "name_en": "Zero-Waste Quality Assurance Gate",
            "domain": "governance",
            "icon": "🛡️",
            "description": "Enforces zero-waste copy standards, verifies tech signals, and binds SHA-256 cryptographic hashes.",
            "stages": ["pending_qa"],
        },
        "design": {
            "name_ar": "Visual Identity & Design Agent",
            "name_en": "Visual Identity & Design Worker",
            "domain": "design",
            "icon": "🎨",
            "description": "Generates photographic image prompts and composites 1080px branded visual cards.",
            "stages": ["pending_design"],
        },
        "publishing": {
            "name_ar": "Cloud Publishing & Distribution Agent",
            "name_en": "Publishing & Webhook Worker",
            "domain": "operations",
            "icon": "🚀",
            "description": "Dispatches approved deliverables to the declared channels through the social:publish gear; the tool behind it can be swapped without touching the kernel.",
            "stages": ["approved", "in_progress"],
        },
        "governor": {
            "name_ar": "Constitutional Governor Agent",
            "name_en": "Constitutional Governor Worker",
            "domain": "governance",
            "icon": "⚖️",
            "description": "Monitors L0-L3 autonomy levels, runs 2% random audits, and enforces instant demotions.",
            "stages": ["pending_approval"],
        },
    }

    agent_counts: Dict[str, int] = {k: 0 for k in core_agent_defs}
    agent_tokens: Dict[str, int] = {k: 0 for k in core_agent_defs}
    agent_cost: Dict[str, float] = {k: 0.0 for k in core_agent_defs}
    recent_actions: Dict[str, List[Dict[str, Any]]] = {k: [] for k in core_agent_defs}
    active_now: Dict[str, int] = {k: 0 for k in core_agent_defs}
    # Newest observed activity per agent (ISO timestamp), derived from capsule
    # history so the cockpit can distinguish a live agent from a dormant one —
    # `status` alone only says whether work is queued *right now*.
    last_active: Dict[str, Optional[str]] = {k: None for k in core_agent_defs}

    # Count tickets created by auto-ideator
    ideator_tickets = 0
    for name in _domain_dirs():
        tdir = DOMAINS_DIR / name / "tickets"
        if tdir.is_dir():
            for tfile in tdir.glob("ticket_*.json"):
                try:
                    tdata = json.loads(tfile.read_text(encoding="utf-8"))
                    if tdata.get("context", {}).get("generated_by") == "auto_ideator" or "auto_ideator" in str(tdata.get("history")):
                        ideator_tickets += 1
                except Exception:
                    pass
    agent_counts["ideator"] = ideator_tickets

    # Aggregate real counts and tokens from spool manifests
    if SPOOL_DIR.is_dir():
        for cap_dir in SPOOL_DIR.iterdir():
            if not cap_dir.is_dir():
                continue
            mf_path = cap_dir / "manifest.json"
            if not mf_path.is_file():
                continue
            try:
                mdata = json.loads(mf_path.read_text(encoding="utf-8"))
                cap_state = mdata.get("state")
                hist = mdata.get("history", [])
                usage = mdata.get("metadata", {}).get("usage", {})
                toks = int(usage.get("total_tokens") or 0)
                img_cost = float(usage.get("image_cost_usd") or 0.0)

                for h in hist:
                    by = str(h.get("by") or "").strip()
                    at = str(h.get("at") or "")
                    if by in agent_counts:
                        agent_counts[by] += 1
                        if at and (last_active.get(by) is None or at > str(last_active[by])):
                            last_active[by] = at
                        if len(recent_actions[by]) < 5:
                            recent_actions[by].append({
                                "at": h.get("at"),
                                "why": h.get("why"),
                                "capsule_id": mdata.get("capsule_id"),
                                "to_state": h.get("to_state"),
                            })
                    if by == "ideation_bridge":
                        agent_counts["ideator"] += 1
                        if at and (last_active.get("ideator") is None or at > str(last_active["ideator"])):
                            last_active["ideator"] = at

                agent_tokens["marketing"] += toks
                agent_cost["design"] += img_cost

                for ag_id, meta in core_agent_defs.items():
                    if cap_state in meta["stages"] and cap_state not in ("published", "archived", "failed"):
                        active_now[ag_id] += 1

            except Exception:
                continue

    # Discovered branch-owned workers
    branch_workers = domain_workers(DOMAINS_DIR)
    for b_name, b_cls in branch_workers.items():
        if b_name not in core_agent_defs:
            core_agent_defs[b_name] = {
                "name_ar": f"{b_name.title()} Domain Agent",
                "name_en": f"{b_name.title()} Domain Worker",
                "domain": b_name,
                "icon": "🤖",
                "description": f"Domain agent dynamically discovered from {b_name}/worker.py.",
                "stages": ["in_progress"],
            }
            agent_counts[b_name] = 0
            agent_tokens[b_name] = 0
            agent_cost[b_name] = 0.0
            recent_actions[b_name] = []
            active_now[b_name] = 0
            last_active[b_name] = None

    agents: List[Dict[str, Any]] = []
    for ag_id, meta in core_agent_defs.items():
        agents.append({
            "id": ag_id,
            "name_ar": meta["name_ar"],
            "name_en": meta["name_en"],
            "domain": meta["domain"],
            "icon": meta["icon"],
            "description": meta["description"],
            "stages": meta["stages"],
            "status": "active" if active_now.get(ag_id, 0) > 0 else "idle",
            "last_active_at": last_active.get(ag_id),
            "active_tasks": active_now.get(ag_id, 0),
            "processed_count": agent_counts.get(ag_id, 0),
            "total_tokens": agent_tokens.get(ag_id, 0),
            "cost_usd": round(agent_cost.get(ag_id, 0.0), 4),
            "recent_actions": recent_actions.get(ag_id, []),
        })

    return agents


@router.get("/api/engine/gears")
async def get_gears_status() -> Dict[str, Any]:
    """Status and telemetry for the 5 Sovereign Engine Gears."""
    pulse_status = "healthy"
    can_proceed = False
    elapsed_hours = float("inf")
    try:
        from domains.ideation.auto_ideator import check_cadence, DEFAULT_CADENCE_HOURS
        can_proceed, elapsed_hours, latest_id, latest_dt = check_cadence(DEFAULT_CADENCE_HOURS)
    except Exception as exc:
        logger.warning("pulse_gear cadence check failed: %s", exc)
        pulse_status = "error"

    pulse_script = ENGINE_ROOT / "orchestration" / "pulse.py"
    if pulse_status == "healthy" and not pulse_script.is_file():
        pulse_status = "warn"

    # Hugging Face tracking ($0.10 USD Free Tier)
    hf_token_set = bool(os.environ.get("HF_TOKEN", "").strip())
    hf_budget_total = 0.10  # Free tier $0.10 USD
    hf_cost_per_image = 0.003
    total_images_generated = 0
    total_hf_spent = 0.0

    if SPOOL_DIR.is_dir():
        for cap_dir in SPOOL_DIR.iterdir():
            if not cap_dir.is_dir():
                continue
            mf_path = cap_dir / "manifest.json"
            if mf_path.is_file():
                try:
                    mdata = json.loads(mf_path.read_text(encoding="utf-8"))
                    usage = mdata.get("metadata", {}).get("usage", {})
                    img_gens = int(usage.get("image_generations") or 0)
                    total_images_generated += img_gens
                    prov = str(usage.get("image_provider") or "")
                    if "huggingface" in prov.lower():
                        total_hf_spent += float(usage.get("image_cost_usd") or (img_gens * hf_cost_per_image))
                except Exception:
                    pass

    hf_remaining = max(0.0, round(hf_budget_total - total_hf_spent, 4))
    hf_pct_remaining = round((hf_remaining / hf_budget_total) * 100, 1)

    llm_model = os.environ.get("AGENCY_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    llm_base = os.environ.get("AGENCY_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    llm_key_set = bool(os.environ.get("AGENCY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    offline_mode = os.environ.get("AGENCY_OFFLINE_MODE") == "1"
    model_status = "healthy" if (llm_key_set or offline_mode) else "warn"

    capsules_count = sum(1 for p in SPOOL_DIR.iterdir() if p.is_dir() and (p / "manifest.json").is_file()) if SPOOL_DIR.is_dir() else 0
    active_locks = sum(1 for p in SPOOL_DIR.glob(".lock.*")) if SPOOL_DIR.is_dir() else 0
    if not SPOOL_DIR.is_dir():
        factory_status = "error"
    elif not os.access(SPOOL_DIR, os.W_OK):
        factory_status = "warn"
    else:
        factory_status = "healthy"

    gov_file = governance_path()
    if not gov_file.is_file():
        gov_status = "warn"
    else:
        try:
            load_governance(gov_file)
            gov_status = "healthy"
        except Exception as exc:
            logger.warning("governance_gear load failed: %s", exc)
            gov_status = "error"

    return {
        "gears": {
            "pulse_gear": {
                "name": "Pulse & Scheduling Gear",
                "cadence_hours_elapsed": round(elapsed_hours, 2) if elapsed_hours != float("inf") else None,
                "threshold_hours": DEFAULT_CADENCE_HOURS if "DEFAULT_CADENCE_HOURS" in locals() else 48,
                "can_proceed": bool(can_proceed),
                "status": pulse_status,
            },
            "governance_gear": {
                "name": "Governance & Autonomy Gear (L0-L3)",
                "governance_file": str(gov_file.resolve()),
                "toctou_protection": "Active (SHA-256 bound at gate)",
                "status": gov_status,
            },
            "factory_gear": {
                "name": "Atomic Capsules Factory Gear",
                "spool_path": str(SPOOL_DIR),
                "total_capsules": capsules_count,
                "active_locks": active_locks,
                "status": factory_status,
            },
            "model_gear": {
                "name": "Model Gateway & AI Routing Gear",
                "llm_model": llm_model,
                "llm_base_url": llm_base,
                "llm_key_configured": llm_key_set,
                "offline_mode": offline_mode,
                "huggingface": {
                    "token_configured": hf_token_set,
                    "model": "black-forest-labs/FLUX.1-schnell",
                    "free_tier_budget_usd": hf_budget_total,
                    "cost_per_image_usd": hf_cost_per_image,
                    "total_images_generated": total_images_generated,
                    "total_spent_usd": total_hf_spent,
                    "remaining_balance_usd": hf_remaining,
                    "pct_remaining": hf_pct_remaining,
                    "status_label": "Active Free Tier" if hf_remaining > 0.01 else "Low / Depleted",
                },
                "status": model_status,
            },
            "publishing_gear": {
                "name": "Publishing & Delivery Gear",
                "contract": PUBLISHING_GEAR,
                "configured": bool(resolve_endpoint(PUBLISHING_GEAR)),
                "endpoint": resolve_endpoint(PUBLISHING_GEAR) or None,
                "endpoint_env": BRIDGE_ENDPOINT_ENV,
                "status": "healthy" if resolve_endpoint(PUBLISHING_GEAR) else "warn",
            },
        },
        "constitutional_registry": __import__("capsule_engine.gear_registry", fromlist=["registry_summary"]).registry_summary(),
    }


@router.post("/api/engine/beat")
async def trigger_full_pulse_beat() -> Dict[str, Any]:
    """Execute the full engine beat (settle -> auto_ideator 48h guard -> dispatcher -> factory pulse)."""
    pulse_script = ENGINE_ROOT / "orchestration" / "pulse.py"
    if not pulse_script.is_file():
        raise HTTPException(status_code=404, detail="pulse.py not found in orchestration/")
    try:
        rc, out, err = await _run_subprocess([sys.executable, str(pulse_script)])
        if rc != 0:
            raise RuntimeError(err.strip() or f"exit code {rc}")
        res = json.loads(out)
    except Exception as exc:
        logger.exception("Full beat execution error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Full beat failed: {exc}")

    return {
        "status": "success",
        "beat": res,
        "message": f"Full engine pulse cycle completed successfully at {res.get('at')}.",
    }


@router.post("/api/tools/qa_inspect")
async def qa_inspect_endpoint(req: QAInspectRequest) -> Dict[str, Any]:
    """Inspect copy text for banned buzzwords/phrases and length compliance.

    This is the deterministic half of the reviewer's rubric — the half that can
    be answered without a model. Whether the copy sounds like the company is
    the reviewer's judgement, not this endpoint's, so it is not guessed here.
    """
    from data.domains.marketing.gateway_ops import (
        BANNED_BUZZWORDS,
        BANNED_PHRASES,
        MIN_REVIEW_WORDS,
    )
    text = req.text.strip()
    words = text.split()
    tokens = set(re.findall(r"[\w-]+", text.lower()))

    found_buzzwords = sorted(list(tokens & BANNED_BUZZWORDS))
    found_phrases = [p for p in BANNED_PHRASES if p in text.lower()]

    passed = (
        (not found_buzzwords)
        and (not found_phrases)
        and (len(words) >= MIN_REVIEW_WORDS)
    )

    reasons = []
    if found_buzzwords:
        reasons.append(f"Contains banned buzzwords: {', '.join(found_buzzwords)}")
    if found_phrases:
        reasons.append(f"Contains banned phrases: {', '.join(found_phrases)}")
    if len(words) < MIN_REVIEW_WORDS:
        reasons.append(f"Text too short ({len(words)} words; need {MIN_REVIEW_WORDS})")

    return {
        "status": "success",
        "passed": passed,
        "banned_buzzwords": found_buzzwords,
        "banned_phrases": found_phrases,
        "word_count": len(words),
        "feedback": "Passed inspection successfully!" if passed else " · ".join(reasons),
    }


@router.post("/api/tools/prompt_distill")
async def prompt_distill_endpoint(req: PromptDistillRequest) -> Dict[str, Any]:
    """Distill copy text into an image prompt complying with the flevo aesthetic."""
    from capsule_engine import default_gateway
    from data.domains.marketing.gateway_ops import image_prompt
    try:
        gw = default_gateway(offline=True)
        distilled = image_prompt(gw, req.text)
        word_count = len(distilled.split())
        return {
            "status": "success",
            "distilled_prompt": distilled,
            "word_count": word_count,
            "compliant": word_count < 60,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Distillation failed: {exc}")


@router.post("/api/tools/test_image")
async def test_image_endpoint(req: TestImageRequest) -> Dict[str, Any]:
    """Test image generation through Hugging Face FLUX.1 or Pollinations."""
    from workers.design import fetch_free_image
    prompt = (req.prompt or "a tidy modern office desk with a laptop and a notebook, soft natural daylight, photorealistic").strip()
    try:
        raw_bytes, ctype = await asyncio.to_thread(fetch_free_image, prompt, 35.0)
        import workers.design as dmod
        provider = getattr(dmod, "LAST_IMAGE_PROVIDER", "unknown")
        cost = getattr(dmod, "LAST_IMAGE_COST_USD", 0.0)
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return {
            "status": "success",
            "provider": provider,
            "cost_usd": cost,
            "size_bytes": len(raw_bytes),
            "content_type": ctype or "image/jpeg",
            "image_data_uri": f"data:{ctype or 'image/jpeg'};base64,{b64}",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {exc}")


# --------------------------------------------------------------------------- #
# Sandbox & Ticket Trials API
# --------------------------------------------------------------------------- #

@router.get("/api/sandbox/capsules")
async def list_sandbox_capsules_endpoint() -> List[Dict[str, Any]]:
    """List all trial capsules currently held in the isolated sandbox spool."""
    from capsule_engine.sandbox import CapsuleSandbox
    sandbox = CapsuleSandbox()
    return sandbox.list_sandbox_capsules()


@router.get("/api/sandbox/capsules/{capsule_id}")
async def inspect_sandbox_capsule_endpoint(capsule_id: str) -> Dict[str, Any]:
    """Deep inspect a specific trial capsule, its artifacts, and audit chain."""
    from capsule_engine.sandbox import CapsuleSandbox, CapsuleNotFoundError
    sandbox = CapsuleSandbox()
    try:
        return sandbox.inspect_sandbox_capsule(capsule_id)
    except CapsuleNotFoundError:
        raise HTTPException(status_code=404, detail=f"Sandbox capsule '{capsule_id}' not found.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Inspection failed: {exc}")


@router.post("/api/sandbox/trial")
async def run_sandbox_trial_endpoint(req: SandboxTrialRequest, request: Request) -> Dict[str, Any]:
    """Execute an isolated factory trial for a specific ticket in capsules_sandbox/."""
    from capsule_engine.sandbox import CapsuleSandbox, TicketNotFoundError
    operator, audit_note = _decision_identity(request)
    sandbox = CapsuleSandbox()
    try:
        res = await asyncio.to_thread(
            sandbox.run_trial,
            ticket_id=req.ticket_id,
            sector=req.sector,
            live=req.live,
            reviewer=audit_note,
            auto_approve=req.auto_approve,
        )
        return res
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sandbox trial failed: {exc}")


@router.post("/api/sandbox/promote/{capsule_id}")
async def promote_sandbox_capsule_endpoint(
    capsule_id: str, request: Request, req: Optional[SandboxPromoteRequest] = None
) -> Dict[str, Any]:
    """Promote a verified sandbox capsule into the production capsules_spool/."""
    from capsule_engine.sandbox import CapsuleSandbox, PromotionError
    from capsule_engine.exceptions import CapsuleNotFoundError, SecurityError
    operator, audit_note = _decision_identity(request)
    sandbox = CapsuleSandbox()
    note = req.note if req and req.note else ""
    try:
        return sandbox.promote(capsule_id, by=audit_note, note=note)
    except CapsuleNotFoundError:
        raise HTTPException(status_code=404, detail=f"Sandbox capsule '{capsule_id}' not found.")
    except (PromotionError, SecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Promotion failed: {exc}")


@router.post("/api/sandbox/clean")
async def clean_sandbox_endpoint(req: Optional[SandboxCleanRequest] = None) -> Dict[str, Any]:
    """Clean up trial capsules from the sandbox spool while preserving .gitkeep."""
    from capsule_engine.sandbox import CapsuleSandbox
    sandbox = CapsuleSandbox()
    keep = req.keep_recent if req else 0
    deleted = sandbox.clean_sandbox(keep_recent=keep)
    return {"status": "success", "cleaned": deleted, "kept": keep}


# --------------------------------------------------------------------------- #
# Factory: Capsules API
# --------------------------------------------------------------------------- #

def _confined_output_path(capsule_path: Path, relative: str) -> Path:
    """Resolve an output path inside the capsule, or 404 (never escapes)."""
    capsule = capsule_path.resolve()
    candidate = Path(str(relative))
    if candidate.is_absolute():
        raise HTTPException(status_code=404, detail=f"Output '{relative}' not found.")
    target = (capsule / candidate).resolve()
    if target != capsule and capsule not in target.parents:
        raise HTTPException(status_code=404, detail=f"Output '{relative}' not found.")
    return target


def _capsule_outputs_projection(entry_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Typed deliverables of a capsule with on-disk facts (no content read).

    Starts from declared ``outputs`` (or the derived primary). Any image file
    actually present but undeclared is appended, so the projection never hides
    a real deliverable. Metadata only — content is fetched per-output.
    """
    from capsule_engine import get_capsule_outputs

    capsule_id = manifest.get("capsule_id") or entry_dir.name
    projected: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in get_capsule_outputs(manifest):
        rel = str(item.get("path") or "")
        if not rel:
            continue
        seen_paths.add(rel)
        try:
            target = _confined_output_path(entry_dir, rel)
            exists = target.is_file()
        except HTTPException:
            exists = False
        size = None
        if exists:
            try:
                size = target.stat().st_size
            except OSError:
                size = None
        projected.append({
            "name": item.get("name"),
            "path": rel,
            "kind": item.get("kind") or "application/octet-stream",
            "primary": bool(item.get("primary")),
            "sha256": item.get("sha256"),
            "exists": exists,
            "size_bytes": size,
            "url": f"/api/capsules/{capsule_id}/outputs/{item.get('name')}",
        })

    for ext, kind in (("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")):
        rel = f"image.{ext}"
        if rel in seen_paths:
            continue
        f = entry_dir / rel
        if f.is_file():
            try:
                size = f.stat().st_size
            except OSError:
                size = None
            projected.append({
                "name": "hero", "path": rel, "kind": kind, "primary": False,
                "sha256": None, "exists": True, "size_bytes": size,
                "url": f"/api/capsules/{capsule_id}/outputs/hero",
            })
            break
    return projected


def _read_artifact(entry_dir: Path, artifact_rel: str) -> str:
    """Read one capsule's deliverable text (best effort, never raises)."""
    artifact_file = entry_dir / artifact_rel
    if artifact_file.is_file():
        try:
            return artifact_file.read_text(encoding="utf-8")
        except Exception:
            return "[Unreadable deliverable]"
    return ""


def _build_capsule_entry(
    entry_dir: Path, manifest: Dict[str, Any], *, include_payload: bool
) -> Dict[str, Any]:
    """Project one capsule into the list shape.

    ``include_payload`` gates the expensive part — reading the deliverable file
    (``copy.md``). List pages pass it only for the rows actually returned, so a
    page costs O(page) artifact reads instead of O(N).
    """
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    cap_domain = manifest.get("domain") or metadata.get("domain") or "marketing"
    artifact_rel = str(metadata.get("artifact_path") or ARTIFACT_FILE)
    has_image = (entry_dir / "image.jpg").is_file() or (entry_dir / "image.png").is_file()
    return {
        "id": manifest.get("capsule_id") or entry_dir.name,
        "capsule_id": manifest.get("capsule_id") or entry_dir.name,
        "sector": cap_domain,
        "domain": cap_domain,
        "status": manifest.get("state"),
        "state": manifest.get("state"),
        "prompt": manifest.get("raw_prompt") or manifest.get("prompt"),
        "payload_text": _read_artifact(entry_dir, artifact_rel) if include_payload else "",
        "artifact_path": artifact_rel,
        "artifact_sha256": metadata.get("artifact_sha256"),
        "sha256": metadata.get("artifact_sha256"),
        "has_image": has_image,
        "outputs": _capsule_outputs_projection(entry_dir, manifest),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "manifest": manifest,
    }


@router.get("/api/capsules")
async def list_capsules(
    request: Request = None,
    state: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Scan spool and return deliverables with manifest metadata.

    Pagination (Scaling Brick 4): no ``limit`` returns the legacy full list and
    reads every deliverable; with ``limit`` (1–500) the SQLite read index
    selects the page, only those manifests are loaded, and the deliverable text
    is read for the page alone (order: ``updated_at`` DESC).
    """
    if limit is not None and not (1 <= int(limit) <= 500):
        raise HTTPException(status_code=400, detail="limit must be within 1..500")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    if not SPOOL_DIR.is_dir():
        return []

    # --- Index fast path (paged reads only) ---------------------------------
    if limit is not None and read_index.enabled():
        usable, allowed = _allowed_sectors(request)
        if usable and read_index.ensure():
            rows = read_index.query_capsules(
                state=state, domains=allowed, limit=int(limit), offset=int(offset)
            )
            page: List[Dict[str, Any]] = []
            for row in rows:
                entry_dir = Path(str(row.get("path") or ""))
                if not (entry_dir / "manifest.json").is_file():
                    continue
                try:
                    manifest = load_manifest(entry_dir)
                except Exception:
                    continue
                page.append(_build_capsule_entry(entry_dir, manifest, include_payload=True))
            return page

    # --- Fallback: full file scan -------------------------------------------
    built: List[Tuple[Path, Dict[str, Any]]] = []
    for entry in sorted(SPOOL_DIR.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "manifest.json").is_file():
            continue
        try:
            manifest = load_manifest(entry)
        except Exception:
            continue
        cap_state = manifest.get("state")
        if state and cap_state != state:
            continue
        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        cap_domain = manifest.get("domain") or metadata.get("domain") or "marketing"
        if not _capsule_scope_allows(request, cap_domain):
            continue
        built.append((entry, _build_capsule_entry(entry, manifest, include_payload=False)))

    if limit is not None:
        built = built[int(offset):int(offset) + int(limit)]
    # Read each deliverable only for the rows actually returned.
    for entry_dir, row in built:
        row["payload_text"] = _read_artifact(entry_dir, row["artifact_path"])
    return [row for _, row in built]


@router.get("/api/capsules/pending")
async def list_pending_capsules() -> List[Dict[str, Any]]:
    """Backwards-compatible endpoint: scan spool for pending_approval deliverables with image_base64."""
    if not SPOOL_DIR.is_dir():
        return []

    pending: List[Dict[str, Any]] = []

    for entry in sorted(SPOOL_DIR.iterdir()):
        if not entry.is_dir():
            continue
        manifest_file = entry / "manifest.json"
        if not manifest_file.is_file():
            continue

        try:
            manifest = load_manifest(entry)
        except Exception:
            continue

        if manifest.get("state") != State.PENDING_APPROVAL.value:
            continue

        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        artifact_rel = metadata.get("artifact_path") or ARTIFACT_FILE
        artifact_file = entry / artifact_rel

        artifact_content = ""
        if artifact_file.is_file():
            try:
                artifact_content = artifact_file.read_text(encoding="utf-8")
            except Exception as exc:
                artifact_content = f"[Unreadable artifact: {exc}]"

        image_file = entry / "image.jpg"
        image_base64: Optional[str] = None
        if image_file.is_file():
            try:
                raw_bytes = image_file.read_bytes()
                encoded = base64.b64encode(raw_bytes).decode("ascii")
                image_base64 = f"data:image/jpeg;base64,{encoded}"
            except Exception:
                image_base64 = None

        pending.append({
            "capsule_id": manifest.get("capsule_id") or entry.name,
            "domain": manifest.get("domain") or "marketing",
            "state": manifest.get("state"),
            "prompt": manifest.get("raw_prompt") or manifest.get("prompt"),
            "artifact_path": str(artifact_rel),
            "artifact_content": artifact_content,
            "artifact_sha256": metadata.get("artifact_sha256"),
            "image_base64": image_base64,
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
        })

    return pending


@router.get("/api/capsules/{capsule_id}/image")
async def get_capsule_image(capsule_id: str) -> FileResponse:
    """Serve the capsule preview image directly."""
    clean_id = Path(capsule_id).name.strip()
    capsule_dir = SPOOL_DIR / clean_id

    for ext, mime in [("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")]:
        img = capsule_dir / f"image.{ext}"
        if img.is_file():
            return FileResponse(img, media_type=mime)

    raise HTTPException(status_code=404, detail="No preview image found for capsule.")


@router.get("/api/capsules/{capsule_id}")
async def get_capsule_details(capsule_id: str, request: Request) -> Dict[str, Any]:
    """Retrieve deep, comprehensive single-capsule inspection details & telemetry."""
    clean_id = Path(capsule_id).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    artifact_rel = metadata.get("artifact_path") or ARTIFACT_FILE
    artifact_file = capsule_path / artifact_rel

    artifact_content = ""
    if artifact_file.is_file():
        try:
            artifact_content = artifact_file.read_text(encoding="utf-8")
        except Exception as exc:
            artifact_content = f"[Unreadable artifact: {exc}]"

    image_file = capsule_path / "image.jpg"
    has_image = image_file.is_file()
    image_size_kb = round(image_file.stat().st_size / 1024, 1) if has_image else 0

    platform_copies = {}
    for p_name in ("linkedin", "facebook", "x"):
        p_file = capsule_path / f"copy_{p_name}.md"
        if p_file.is_file():
            try:
                platform_copies[p_name] = p_file.read_text(encoding="utf-8")
            except Exception:
                pass

    prompt_file = capsule_path / "image_prompt.txt"
    image_prompt = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.is_file() else ""

    feedback_file = capsule_path / "feedback.txt"
    current_feedback = feedback_file.read_text(encoding="utf-8").strip() if feedback_file.is_file() else ""

    cost_data = metadata.get("cost", {})
    usage_data = metadata.get("usage", {})

    return {
        "capsule_id": clean_id,
        "domain": manifest.get("domain") or "marketing",
        "state": manifest.get("state"),
        "bundle_type": manifest.get("bundle_type") or "post_capsule",
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "artifact_path": str(artifact_rel),
        "artifact_content": artifact_content,
        "artifact_sha256": metadata.get("artifact_sha256"),
        "outputs": _capsule_outputs_projection(capsule_path, manifest),
        "asset_vault_sha256": metadata.get("asset_vault_sha256"),
        "has_image": has_image,
        "image_url": f"/api/capsules/{clean_id}/image" if has_image else None,
        "image_size_kb": image_size_kb,
        "image_prompt": image_prompt,
        "platform_copies": platform_copies,
        "current_feedback": current_feedback,
        "bundle_breakdown": metadata.get("bundle_breakdown", {}),
        "cost_accounting": {
            "image_generation_usd": cost_data.get("image_generation_usd", 0.0),
            "image_provider": cost_data.get("image_provider", "unknown"),
            "image_dimensions": cost_data.get("image_dimensions", "1080x1080"),
            "currency": cost_data.get("currency", "USD"),
        },
        "telemetry": {
            "tokens": {
                "prompt_tokens": usage_data.get("prompt_tokens", 0),
                "completion_tokens": usage_data.get("completion_tokens", 0),
                "total_tokens": usage_data.get("total_tokens", 0),
            },
            "image_generations": usage_data.get("image_generations", 0),
            "attempts": manifest.get("attempts", 0),
        },
        "viewers": metadata.get("viewers", []),
        "history": manifest.get("history", []),
        "company_context": manifest.get("company_context", {}),
    }


@router.get("/api/capsules/{capsule_id}/outputs")
async def list_capsule_outputs(capsule_id: str) -> Dict[str, Any]:
    """The capsule's typed deliverables (metadata only; fetch one for content)."""
    clean_id = Path(capsule_id).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)
    return {
        "capsule_id": clean_id,
        "state": manifest.get("state"),
        "outputs": _capsule_outputs_projection(capsule_path, manifest),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/capsules/{capsule_id}/outputs/{output_name}")
async def get_capsule_output(capsule_id: str, output_name: str) -> Response:
    """Fetch one deliverable by its declared name (text inline, binary as a file)."""
    clean_id = Path(capsule_id).name.strip()
    wanted = Path(output_name).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)

    from capsule_engine import get_capsule_outputs

    match = next(
        (o for o in get_capsule_outputs(manifest) if str(o.get("name")) == wanted),
        None,
    )
    if match is None and wanted == "hero":
        match = {"name": "hero", "path": "image.jpg", "kind": "image/jpeg"}
    if match is None:
        raise HTTPException(
            status_code=404, detail=f"Output '{wanted}' is not declared on capsule '{clean_id}'."
        )

    rel = str(match.get("path") or "")
    target = _confined_output_path(capsule_path, rel)
    if not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"Output '{wanted}' is declared but not present on disk."
        )

    kind = str(match.get("kind") or "")
    if kind.startswith("text/") or target.suffix.lower() in (".md", ".txt", ".json", ".csv", ".html"):
        try:
            body = target.read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(status_code=500, detail=f"Output '{wanted}' is unreadable.")
        return Response(content=body, media_type=kind or "text/plain; charset=utf-8")
    return FileResponse(target, media_type=kind or "application/octet-stream")


@router.post("/api/capsules/{capsule_id}/viewed")
async def record_capsule_viewed(capsule_id: str, request: Request) -> Dict[str, Any]:
    """Record an audit trail entry for who viewed this capsule."""
    clean_id = Path(capsule_id).name.strip()
    reviewer, audit_note = _decision_identity(request)
    capsule_path = _confined_capsule_path(clean_id)

    with CapsuleLock(capsule_path):
        manifest = load_manifest(capsule_path)
        metadata = manifest.setdefault("metadata", {})
        viewers = metadata.setdefault("viewers", [])
        now_str = datetime.now(timezone.utc).isoformat()
        viewers.append({
            "viewer": reviewer,
            "at": now_str,
            "note": audit_note,
        })
        metadata["viewers"] = viewers[-20:]
        save_manifest(capsule_path, manifest)

    return {"status": "ok", "capsule_id": clean_id, "viewer": reviewer, "at": now_str}


@router.get("/api/config/marketing")
async def get_marketing_config_endpoint() -> Dict[str, Any]:
    """Retrieve dynamic white-label marketing and cadence configuration."""
    from data.domains.marketing.marketing_config import load_marketing_config
    return load_marketing_config()


@router.post("/api/config/marketing")
async def update_marketing_config_endpoint(req: Request) -> Dict[str, Any]:
    """Update dynamic white-label marketing and cadence configuration."""
    from data.domains.marketing.marketing_config import (
        DEFAULT_MARKETING_CONFIG,
        marketing_config_path,
    )
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid config payload; must be JSON object")

    def _mutate(existing: Any) -> Any:
        current = dict(DEFAULT_MARKETING_CONFIG)
        if isinstance(existing, dict):
            current.update(existing)
        current.update(body)
        stamp_schema_version(KIND_MARKETING_CONFIG, current)
        return current

    path = marketing_config_path()
    updated = update_json(path, _mutate, default={}, trailing_newline=True)
    return {"status": "ok", "config": updated}


@router.get("/api/config/brand")
@router.get("/api/config/company")
async def get_brand_config_endpoint() -> Dict[str, Any]:
    """Retrieve dynamic white-label brand guidelines and company context."""
    from shared_kernel.brand_profile import load_company_profile
    return load_company_profile()


@router.get("/api/config/company/schema")
async def get_company_schema_endpoint() -> Dict[str, Any]:
    """Retrieve the pristine unpopulated company profile schema."""
    from shared_kernel.brand_profile import DEFAULT_PROFILE
    return DEFAULT_PROFILE


@router.post("/api/config/brand")
@router.patch("/api/config/brand")
@router.post("/api/config/company")
@router.patch("/api/config/company")
async def update_brand_config_endpoint(req: Request) -> Dict[str, Any]:
    """Dynamically update white-label company profile and brand settings at runtime."""
    import copy
    from shared_kernel.brand_profile import (
        DEFAULT_PROFILE,
        company_profile_path,
        deep_merge,
    )
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid brand payload; must be JSON object")

    def _mutate(existing: Any) -> Any:
        current = copy.deepcopy(DEFAULT_PROFILE)
        if isinstance(existing, dict):
            deep_merge(current, existing)
        deep_merge(current, body)
        stamp_schema_version(KIND_COMPANY_PROFILE, current)
        return current

    path = company_profile_path()
    updated = update_json(path, _mutate, default={}, trailing_newline=True)
    return {"status": "ok", "company": updated, "brand": updated}


@router.post("/api/capsules/{capsule_id}/approve")
async def approve_capsule(capsule_id: str, request: Request) -> Dict[str, Any]:
    """Approve a deliverable, enforcing TOCTOU cryptographic integrity.

    Thin wrapper over the unified service :func:`orchestration.approval.approve`
    — identity, response shaping and the post-approval factory pulse stay
    here; the decision itself (permissions, gate plan, transition table,
    trace entry, event) lives in the service, the single source of truth.
    """
    clean_id = Path(capsule_id).name.strip()
    reviewer, audit_note = _decision_identity(request)
    capsule_path = _confined_capsule_path(clean_id)

    try:
        outcome = approval.approve(
            capsule_path,
            principal=getattr(request.state, "principal", None),
            reviewer=reviewer,
            audit_note=audit_note,
            reason=f"approved via cockpit — {audit_note}",
        )
    except approval.ApprovalRefused as refused:
        raise HTTPException(
            status_code=refused.status_code, detail=refused.detail
        ) from refused
    except SecurityError as sec_exc:
        raise HTTPException(
            status_code=400,
            detail=f"TOCTOU Integrity Violation: {sec_exc}",
        ) from sec_exc
    except LockTimeoutError as lock_exc:
        raise HTTPException(
            status_code=409,
            detail=f"Capsule is locked by another process: {lock_exc}",
        ) from lock_exc
    except StateTransitionError as trans_exc:
        raise HTTPException(
            status_code=400,
            detail=f"Illegal state transition: {trans_exc}",
        ) from trans_exc
    except CapsuleError as cap_exc:
        raise HTTPException(
            status_code=400,
            detail=f"Capsule error: {cap_exc}",
        ) from cap_exc

    if not outcome.final:
        # A gate was consumed; the capsule itself is not approved yet.
        plan = outcome.plan
        return {
            "status": "success",
            "capsule_id": clean_id,
            "gate_id": outcome.gate_id,
            "decision": "approve",
            "new_state": outcome.state,
            "gates": {**plan, "checklist": outcome.checklist},
            "message": (
                f"Gate '{outcome.gate_id}' approved ({plan['passed']}/{plan['total']}) "
                f"— awaiting gate '{plan['active_gate']}'"
            ),
        }

    # Trigger a factory pulse so the approved capsule reaches the delivery gear
    # (this route only — no other decision route pulses).
    final_state = outcome.state
    try:
        pulse_report = run_pulse(SPOOL_DIR)
        updated_manifest = load_manifest(capsule_path)
        final_state = updated_manifest.get("state", outcome.state)
        logger.info("Auto-pulse after approval: %s (state: %s)", pulse_report, final_state)
    except Exception as p_exc:
        logger.warning("Auto-pulse after approval had error: %s", p_exc)

    published = (final_state == State.PUBLISHED.value)
    msg = "Capsule approved and delivered through the social:publish gear." if published else "Capsule approved and cryptographic anti-TOCTOU hash bound."
    return {
        "status": "success",
        "capsule_id": clean_id,
        "new_state": final_state,
        "published": published,
        "message": msg,
    }


@router.post("/api/capsules/{capsule_id}/reject")
async def reject_capsule(capsule_id: str, req: RejectRequest, request: Request) -> Dict[str, Any]:
    """Reject a deliverable and transition to needs_revision with reason.

    Thin wrapper over the unified service :func:`orchestration.approval.reject`
    — identity, request validation and response shaping stay here; the
    decision (permissions, gate failure, feedback, ledger, immediate
    demotion, trace entry, event) lives in the service.
    """
    clean_id = Path(capsule_id).name.strip()
    reviewer, audit_note = _decision_identity(request)
    capsule_path = _confined_capsule_path(clean_id)

    reason = req.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Rejection reason cannot be empty")

    try:
        outcome = approval.reject(
            capsule_path,
            principal=getattr(request.state, "principal", None),
            reviewer=reviewer,
            audit_note=audit_note,
            reason=reason,
            why=f"Rejected: {reason} — {audit_note}",
        )
    except approval.ApprovalRefused as refused:
        raise HTTPException(
            status_code=refused.status_code, detail=refused.detail
        ) from refused
    except LockTimeoutError as lock_exc:
        raise HTTPException(
            status_code=409,
            detail=f"Capsule is locked by another process: {lock_exc}",
        ) from lock_exc
    except StateTransitionError as trans_exc:
        raise HTTPException(
            status_code=400,
            detail=f"Illegal state transition: {trans_exc}",
        ) from trans_exc
    except CapsuleError as cap_exc:
        raise HTTPException(
            status_code=400,
            detail=f"Capsule error: {cap_exc}",
        ) from cap_exc

    escalated = outcome.state != State.NEEDS_REVISION.value
    return {
        "status": "rejected",
        "capsule_id": clean_id,
        "new_state": outcome.state,
        "reason": reason,
        "message": (
            f"Revision cap reached — capsule moved to {outcome.state} for a "
            f"human decision: {reason}"
            if escalated
            else f"Capsule rejected and returned for revision: {reason}"
        ),
    }


@router.post("/api/capsules")
async def create_capsule_endpoint(req: CreateCapsuleRequest, request: Request) -> Dict[str, Any]:
    """Create a new capsule in the spool, seated at the approval gate.

    The record is born through the shared creation helper
    (:func:`capsule_engine.manifest.seat_at_approval_gate`): the state/history
    move is recorded, a **fresh** gate plan is seeded from the policy
    template, and the artifact digest is bound (``try_bind_artifact``). If
    gates are enabled and the plan cannot be seeded, creation is refused and
    nothing is persisted (fail closed) — the capsule never enters the spool
    ungoverned.
    """
    import time
    ts = int(time.time())
    target_domain = (req.sector or req.domain or "marketing").strip()
    # The resource is named in the body, so it is known before any lock — and
    # authorised before anything is written.
    _require_scope(request, "capsule:create", target_domain)

    # Resolve the declared task type BEFORE anything is written: an unknown or
    # malformed type is a 400 that leaves no capsule directory, no artifact and
    # no card behind. Omitting `task_type` keeps the legacy behaviour exactly
    # (no card is written and no field is added).
    declared_type: Optional[task_type_engine.TaskType] = None
    if isinstance(req.task_type, str) and req.task_type.strip():
        try:
            declared_type = task_type_engine.load_task_type(req.task_type.strip())
        except task_type_engine.TaskTypeError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown or invalid task type {req.task_type.strip()!r}: {exc}"
                ),
            ) from exc

    raw_text = (req.text or req.prompt or "Direct capsule from Cockpit").strip()
    clean_id = req.capsule_id.strip() if req.capsule_id else f"capsule_{ts}_{target_domain}"

    capsule_dir = SPOOL_DIR / clean_id
    pre_existing = capsule_dir.exists()
    capsule_dir.mkdir(parents=True, exist_ok=True)

    now_ts = now_iso()
    manifest_data: Dict[str, Any] = {
        "capsule_id": clean_id,
        "domain": target_domain,
        "raw_prompt": raw_text,
        "attempt": 0,
        "created_at": now_ts,
        "updated_at": now_ts,
        "metadata": {
            "domain": target_domain,
            "topic": raw_text[:50],
            "target_platform": req.target_platform.strip(),
            "artifact_path": ARTIFACT_FILE,
        },
        "routing_context": {
            "domain": target_domain,
            "flags": {
                "requires_copy": True,
                "requires_image": _wants_image(target_domain, req.requires_image),
            },
        },
    }
    if declared_type is not None:
        manifest_data["task_type"] = declared_type.task_type

    if req.scheduled_for and req.scheduled_for.strip():
        manifest_data["publishing_rules"] = {
            "mode": "strict",
            "scheduled_for": req.scheduled_for.strip(),
        }

    # Creation takes the capsule's own lock before touching anything inside
    # the directory: a busy capsule raises LockTimeoutError here and nothing
    # is ever written unlocked (the workers, approval and pulse paths all
    # take this same lock).
    with CapsuleLock(capsule_dir, purpose="api:create_capsule"):
        # Write initial deliverable payload; the helper binds its digest next.
        deliverable_file = capsule_dir / ARTIFACT_FILE
        atomic_text(deliverable_file, raw_text, encoding="utf-8")

        # The B1 task card is written here — by the creation path, atomically,
        # inside this same lock, so a busy capsule writes nothing at all. The
        # card is router metadata: not an output, never hash-bound.
        if declared_type is not None:
            atomic_text(
                capsule_dir / task_type_engine.CARD_NAME,
                json.dumps(
                    declared_type.to_task_profile(), ensure_ascii=False, indent=2
                )
                + "\n",
                encoding="utf-8",
            )

        # Seat at the approval gate: history + fresh gate plan + bound digest.
        # A failure refuses the creation — no manifest is ever written.
        try:
            seat_at_approval_gate(
                manifest_data,
                capsule_dir,
                by="human_cockpit",
                why="direct capsule injected via cockpit UI",
            )
            save_manifest(capsule_dir, manifest_data)
        except Exception as seat_exc:
            if not pre_existing and not (capsule_dir / "manifest.json").is_file():
                # Refuse = nothing persisted: drop the shell we just created.
                shutil.rmtree(capsule_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Capsule creation refused: {seat_exc}",
            ) from seat_exc

    return {
        "status": "success",
        "capsule_id": clean_id,
        "state": State.PENDING_APPROVAL.value,
        "message": f"Capsule {clean_id} spawned successfully in spool.",
    }


@router.post("/api/capsules/build_post")
async def build_post_endpoint(req: PostBuildRequest) -> Dict[str, Any]:
    """Create atomic tickets T1, T2, T3 and assemble a real Deliverable Capsule manifest on disk."""
    from data.domains.marketing.post_builder import (
        IdeationSpec,
        CopywritingSpec,
        VisualDesignSpec,
        assemble_post_capsule_manifest,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_topic = re.sub(r'[^a-zA-Z0-9_\u0600-\u06FF]+', '_', req.topic).strip('_')[:30]
    capsule_id = f"cap_post_{ts}_{clean_topic}" if clean_topic else f"cap_post_{ts}"

    idea = IdeationSpec(
        topic=req.topic.strip(),
        angle=req.angle.strip() if req.angle else req.topic.strip(),
        audience=req.audience.strip() if req.audience else "Enterprise Decision Makers",
        occasion=req.occasion.strip() if req.occasion else None,
        channels=req.channels or ["linkedin", "facebook", "x"],
    )

    copy = None
    if req.headline or req.linkedin_copy or req.unified_copy or req.facebook_copy or req.x_copy:
        copy = CopywritingSpec(
            headline=req.headline.strip() if req.headline else req.topic.strip(),
            linkedin_copy=req.linkedin_copy.strip() if req.linkedin_copy else None,
            facebook_copy=req.facebook_copy.strip() if req.facebook_copy else None,
            x_copy=req.x_copy.strip() if req.x_copy else None,
            unified_copy=req.unified_copy.strip() if req.unified_copy else (req.linkedin_copy or req.headline or req.topic),
            hashtags=req.hashtags,
        )

    design = None
    if req.visual_concept or req.image_prompt:
        design = VisualDesignSpec(
            visual_concept=req.visual_concept.strip() if req.visual_concept else req.topic.strip(),
            image_prompt=req.image_prompt.strip() if req.image_prompt else f"Professional visual for {req.topic}",
            aspect_ratios=req.aspect_ratios or ["1:1", "4:5", "16:9"],
            include_logo=req.include_logo,
        )

    manifest = assemble_post_capsule_manifest(
        capsule_id=capsule_id,
        idea=idea,
        copy=copy,
        design=design,
    )

    # The post builder *is* the social_post task type: declare it and render the
    # same B1 card the generic creation path writes. A broken catalogue is a
    # server misconfiguration, so it refuses rather than shipping a typeless
    # capsule (fail closed).
    try:
        post_task_type = task_type_engine.load_task_type("social_post")
    except task_type_engine.TaskTypeError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Built-in task type 'social_post' is unavailable — the catalogue "
                f"under config/task_types/ is broken (fail closed): {exc}"
            ),
        ) from exc
    manifest["task_type"] = post_task_type.task_type

    capsule_dir = SPOOL_DIR / capsule_id
    capsule_dir.mkdir(parents=True, exist_ok=True)

    # Write initial copy.md + manifest under the capsule's own lock: creation
    # must not race a worker or another pulse (a busy capsule raises instead
    # of writing unlocked).
    payload_body = req.unified_copy or req.linkedin_copy or f"# {req.topic}\n\n{req.angle or ''}"
    with CapsuleLock(capsule_dir, purpose="api:build_post"):
        atomic_text(capsule_dir / ARTIFACT_FILE, payload_body, encoding="utf-8")
        atomic_text(
            capsule_dir / task_type_engine.CARD_NAME,
            json.dumps(post_task_type.to_task_profile(), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        save_manifest(capsule_dir, manifest)

    # Persist tickets to marketing domain
    mkt_tickets_dir = DOMAINS_DIR / "marketing" / "tickets"
    mkt_tickets_dir.mkdir(parents=True, exist_ok=True)
    for t_id in manifest.get("tickets", []):
        t_file = mkt_tickets_dir / f"ticket_{t_id}.json"
        t_data = {
            "ticket_id": t_id,
            "sector": "marketing",
            "capsule_id": capsule_id,
            "spool_capsule": capsule_id,
            "title": req.topic,
            "state": "settled",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        stamp_schema_version(KIND_TICKET, t_data)
        atomic_json(t_file, t_data, trailing_newline=False)

    if hasattr(read_index, "rebuild"):
        read_index.rebuild()

    return {
        "status": "success",
        "capsule_id": capsule_id,
        "manifest": manifest,
        "capsule_path": str(capsule_dir),
    }


# --------------------------------------------------------------------------- #
# Visual-domain policy — data, not a branch list
# --------------------------------------------------------------------------- #

# Domains that produce visual output by default. Override without code:
# set AGENCY_VISUAL_DOMAINS="design,marketing,brand" (comma-separated), or
# pass an explicit requires_image flag per capsule. An empty/missing var
# keeps this default; the special value "none" disables all defaults.
_DEFAULT_VISUAL_DOMAINS = ("marketing", "design")


def _visual_domains() -> frozenset[str]:
    raw = os.environ.get("AGENCY_VISUAL_DOMAINS")
    if raw is None:
        return frozenset(_DEFAULT_VISUAL_DOMAINS)
    names = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return frozenset() if "none" in names else frozenset(names)


def _wants_image(target_domain: str, explicit: Optional[bool]) -> bool:
    """Decide image production from data, never from a hard-coded branch name.

    Priority: an explicit client flag wins; else the domain's membership in
    the visual-domain set (env-overridable); branch names in this repo are
    samples, not contracts.
    """
    if explicit is not None:
        return bool(explicit)
    return target_domain.strip().lower() in _visual_domains()

app = FastAPI(title="Sovereign Agency OS Cockpit", version="2.0.0")

# CORS — same-origin by default. Cross-origin access is explicit opt-in via
# AGENCY_CORS_ORIGINS; credentials are only enabled when an allowlist exists
# (never combined with a wildcard, which browsers reject anyway).
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=bool(CORS_ORIGINS),
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Agency-Token", "Authorization"],
)


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    """Request id, latency timing, and Prometheus counters for every request.

    The id honours an inbound ``X-Request-ID`` (so a proxy or client can
    correlate) and is echoed back; it travels in a contextvar so downstream log
    lines carry it. Counters feed ``GET /api/metrics``.
    """
    incoming = request.headers.get("x-request-id", "").strip()
    request_id = incoming[:64] if incoming else observability.new_request_id()
    request.state.request_id = request_id
    token = observability.request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - started
        observability.METRICS.record(request.method, request.url.path, 500, duration)
        logger.exception(
            "request failed: %s %s (%.1f ms)",
            request.method, request.url.path, duration * 1000,
        )
        observability.request_id_var.reset(token)
        raise
    duration = time.perf_counter() - started
    observability.METRICS.record(request.method, request.url.path, response.status_code, duration)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s [%s] %.1f ms", request.method, request.url.path, response.status_code, duration * 1000
    )
    observability.request_id_var.reset(token)
    # Cockpit UI assets must revalidate on every load so edits to HTML/CSS/JS
    # appear immediately after a browser refresh (304 when unchanged).
    if request.url.path.startswith("/cockpit/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _bearer_token(header_value: Optional[str]) -> Optional[str]:
    """Accept `Authorization: Bearer <t>` or a raw token value."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return header_value.strip()


def _refusal_content(
    path: str,
    *,
    message: str,
    code: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The body of a middleware refusal, shaped for the namespace it guards.

    ``/v1/`` callers get the mobile envelope (machine-readable ``code`` plus a
    top-level ``detail``); every other namespace keeps the legacy ``{"detail":
    …}`` shape it has always had, byte for byte.
    """
    if path.startswith(v1_errors.V1_PREFIX):
        return v1_errors.v1_error_content(
            code=code, message=message, details=details
        )
    return {"detail": message}


def _refuse(
    path: str,
    *,
    status_code: int,
    message: str,
    code: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """A refusal returned straight from the middleware (handlers do not apply)."""
    return JSONResponse(
        status_code=status_code,
        content=_refusal_content(path, message=message, code=code, details=details),
    )


# The guarded prefixes, the public paths, and the route→capability map live in
# one place: orchestration/access_control.py. Keeping the policy out of this
# module means the coverage test can compare it against app.routes without
# importing (or executing) the handlers.


def _confined_capsule_path(capsule_id: str) -> Path:
    """The capsule directory, or a 404 — never a path outside the spool.

    Both sides are resolved *here*, at call time. Comparing a resolved capsule
    against an unresolved spool would refuse a legitimate capsule whenever the
    spool path contains a symlink (macOS ``/var`` is ``/private/var``), and
    resolution is what makes the confinement check meaningful in the first
    place: a capsule directory that is a symlink out of the spool resolves
    outside it and is refused before any lock is taken.
    """
    capsule_path = (SPOOL_DIR / capsule_id).resolve()
    if not capsule_path.is_dir() or SPOOL_DIR.resolve() not in capsule_path.parents:
        raise HTTPException(
            status_code=404, detail=f"Capsule '{capsule_id}' not found in spool"
        )
    return capsule_path


def _gate_link_index() -> Dict[str, Dict[str, Any]]:
    """Map ``gate_id`` -> the ticket/capsule waiting on that gate.

    An approval gate blocks a *ticket* before any capsule exists, so a gate
    record alone cannot name the capsule it is holding up. This index closes
    that gap for ``/api/gates`` without writing to the queue: every domain
    ticket already carries the ``gate_id`` it was raised under, plus wherever
    it has since been promoted (``spool_capsule``). The scan mirrors
    :func:`list_tickets` (scratch, approved, legacy root, archive) and keeps the
    richest entry per gate (one with a capsule id wins).
    """
    index: Dict[str, Dict[str, Any]] = {}
    if not DOMAINS_DIR.is_dir():
        return index

    candidates: List[Path] = []
    for pattern in (
        "*/tickets/approved/ticket_*.json",
        "*/tickets/ticket_*.json",
        "*/tickets/scratch/ticket_*.json",
        "*/tickets/archive/ticket_*.json",
    ):
        for p in DOMAINS_DIR.glob(pattern):
            if "sync-conflict" not in p.name and not p.name.endswith(".tmp"):
                candidates.append(p)

    for tpath in candidates:
        try:
            tdata = json.loads(tpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = tdata.get("metadata") if isinstance(tdata.get("metadata"), dict) else {}
        gate_id = tdata.get("gate_id") or tdata.get("via") or metadata.get("gate_id")
        decision = tdata.get("decision") if isinstance(tdata.get("decision"), dict) else {}
        if not gate_id and "via gate " in str(decision.get("reason") or ""):
            gate_id = str(decision["reason"]).split("via gate ")[1].split()[0].strip()
        if not gate_id:
            continue

        capsule_id = (
            tdata.get("spool_capsule")
            or tdata.get("capsule_id")
            or metadata.get("spool_capsule")
            or metadata.get("capsule_id")
        )
        title = metadata.get("title") or tdata.get("title") or tdata.get("topic")
        if not title:
            req = tdata.get("request") or tdata.get("content") or ""
            lines = [line.strip() for line in str(req).split("\n") if line.strip()]
            title = lines[0].replace("#", "").strip() if lines else None

        entry = {
            "ticket_id": tdata.get("ticket_id") or tdata.get("id") or tpath.stem,
            "capsule_id": capsule_id,
            "capsule_title": title,
        }
        prior = index.get(str(gate_id))
        # Prefer a linked capsule, and enrich a title-only entry with the id.
        if prior and prior.get("capsule_id") and not entry.get("capsule_id"):
            if not prior.get("capsule_title"):
                prior["capsule_title"] = entry["capsule_title"]
            continue
        index[str(gate_id)] = entry

    return index


def _gate_sector(gate_id: str) -> Optional[str]:
    """The branch a gate belongs to — ``source.sector``, or ``None``.

    Read from the gate record itself, never from the request: the sector is
    written once when the gate is raised and is the authoritative resource for
    the decision. There is no lock on the gate queue (the gate script writes
    the entry), but the sector is immutable for the life of the gate.
    """
    entry_file = GATE_QUEUE_DIR / f"gate_{gate_id}.json"
    try:
        entry = json.loads(entry_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source = entry.get("source") if isinstance(entry, dict) else None
    sector = source.get("sector") if isinstance(source, dict) else None
    return sector.strip() if isinstance(sector, str) and sector.strip() else None


def _require_scope(request: Request, capability: str, resource: str) -> None:
    """The fine check: 403 unless the principal is scoped for ``resource``.

    Call this where the resource is authoritative — inside ``CapsuleLock`` for
    a capsule, from the gate record for a gate, from the request body for a
    creation — never from a header or any client-claimed field. Checking the
    resource outside the lock it protects would be a TOCTOU on the permission
    itself, which is the one class of bug this engine exists to prevent.
    """
    denial = access_control.scope_denial(
        getattr(request.state, "principal", None), capability, resource
    )
    if denial:
        raise HTTPException(status_code=403, detail=denial)


def _require_gate_scope(request: Request, gate_id: str, capability: str) -> None:
    """Authorise a gate decision against the gate's own branch.

    A gate whose record cannot be read has no resolvable branch, and an
    unscopable decision is refused: 404 here, never a silent pass.
    """
    sector = _gate_sector(gate_id)
    if sector is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Gate '{gate_id}' not found or unreadable — cannot resolve its "
                "branch, so the decision is refused (fail closed)."
            ),
        )
    _require_scope(request, capability, sector)


def _decision_identity(request: Request) -> tuple[str, str]:
    """Who is deciding, plus a log-safe provenance label.

    Identity is derived *only* from the verified credential the middleware
    resolved — never from the request body or a header, because a client can
    claim anything. Every guarded endpoint is unreachable without a principal,
    so a missing one is a programming error and refuses rather than inventing
    an actor.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="Unauthenticated: no verified principal on this request.",
        )
    return principal.subject, principal.audit_note()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Fail-closed capability gate for every guarded /api/* route.

    Each step refuses rather than guesses:

    1. unguarded paths (the cockpit shell, static assets) pass through — the
       login page must load before a credential exists;
    2. public paths (health probes) and CORS preflight pass without a
       credential;
    3. the credential is resolved into a principal — a signed capability key,
       or the legacy shared token while its kill switch allows it. Forged,
       expired, or unrecognised credentials get 401;
    4. a path missing from the route map is refused (403), because the map is
       exhaustive by contract: a gap closes a door instead of opening one.
       This is checked *after* authentication, so an unauthenticated caller
       cannot probe which paths are misdeclared;
    5. the principal must hold the route's declared capability, else 403 naming
       the missing grant (default deny, reads included);
    6. only then is the identity injected into ``request.state``.

    With no credential mechanism configured at all, guarded routes answer 503
    rather than run open.
    """
    path = request.url.path
    if not access_control.is_guarded(path):
        return await call_next(request)

    if path in access_control.PUBLIC_API_PATHS:
        return await call_next(request)

    requirement = access_control.resolve(request.method, path)
    if requirement.is_public:  # declared, needs no capability (CORS preflight)
        return await call_next(request)

    if not API_TOKEN and resolve_secret() is None:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Auth not configured: set AGENCY_API_TOKEN, "
                    "AGENCY_CAPABILITY_SECRET, or both on the server."
                )
            },
        )

    credential = (
        request.headers.get("x-agency-token")
        or _bearer_token(request.headers.get("authorization"))
        or request.query_params.get("token")
    )
    try:
        principal = access_control.authenticate(
            credential, expected_token=API_TOKEN, legacy_subject=OPERATOR
        )
    except CapabilityError as exc:
        logger.warning("auth: rejected %s %s: %s", request.method, path, exc)
        return _refuse(
            path,
            status_code=401,
            message=f"Unauthorized: {exc}",
            code="UNAUTHENTICATED",
        )
    except Exception as exc:  # defensive: a bug here must never open the door
        logger.error(
            "auth: unexpected verification failure for %s %s: %s",
            request.method,
            path,
            exc,
        )
        return _refuse(
            path,
            status_code=401,
            message="Unauthorized: credential could not be verified.",
            code="UNAUTHENTICATED",
        )

    # Declaration is checked *after* authentication on purpose: an unknown
    # path from an unauthenticated caller must keep answering 401 like every
    # other guarded route, rather than turning 403 and signalling "this route
    # is misdeclared" to someone with no credential at all.
    if not requirement.declared:
        logger.error(
            "auth: refusing undeclared route %s %s — declare it in "
            "orchestration/access_control.py (fail closed)",
            request.method,
            path,
        )
        return _refuse(
            path,
            status_code=403,
            message=(
                f"No capability is declared for {request.method} {path} — "
                "refused (fail closed). Declare it in "
                "orchestration/access_control.py."
            ),
            code="UNDECLARED_ROUTE",
        )

    # Coarse gate only: does this credential hold the verb *anywhere*? For a
    # branch-scoped capability that is not permission — the endpoint proves the
    # resource under its own lock — but a credential holding it nowhere can be
    # refused here and now, without the expensive lookup.
    if not access_control.may_attempt(principal, requirement.capability):
        logger.warning(
            "auth: insufficient capability for %s %s — needs %r, credential grants %s",
            request.method,
            path,
            requirement.capability,
            sorted(principal.capabilities),
        )
        return _refuse(
            path,
            status_code=403,
            message=(
                f"Insufficient capability: '{requirement.capability}' is "
                f"required for {request.method} {path}, and this credential "
                f"grants {sorted(principal.capabilities)}. Mint a key that "
                f"includes it (add --scope <branch> for a branch-scoped "
                f"decision): python3 tools/mint_capability.py mint "
                f"--subject <who> --capabilities {requirement.capability}"
            ),
            code="INSUFFICIENT_CAPABILITY",
            details={"required": requirement.capability},
        )

    request.state.principal = principal
    request.state.operator = principal.subject
    return await call_next(request)


async def _warn_migration_debt() -> None:
    """Nag on every boot while an omnipotent credential is still accepted.

    The legacy token cannot be retired by a commit — it is a deployment
    decision — so the code's job is to keep the debt visible rather than let it
    become permanent by silence.
    """
    if access_control.legacy_root_enabled() and API_TOKEN:
        logger.warning(
            "SECURITY: the legacy shared token is still accepted as a ROOT "
            "credential (AGENCY_LEGACY_ROOT_TOKEN is not 'off'). It satisfies "
            "every capability check on every route, including branch-scoped "
            "ones. Mint capability keys, paste one into the cockpit, then set "
            "AGENCY_LEGACY_ROOT_TOKEN=off to finish the migration."
        )


# --------------------------------------------------------------------------- #
# Universal gate layer, in-flight telemetry and delivery previews
# --------------------------------------------------------------------------- #

class GateDecisionRequest(BaseModel):
    """A reviewer's decision on one specific gate of a capsule's plan."""

    decision: str = Field(..., description="approve | reject | skip")
    reason: Optional[str] = Field(
        None, description="Reviewer instruction / audit note (required on reject)"
    )
    reviewer: Optional[str] = Field(
        None, description="Ignored: identity is derived from the verified credential"
    )


#: States with work running or still ahead of them (terminal states excluded).
ACTIVE_CAPSULE_STATES: frozenset = frozenset(
    {
        State.DRAFT.value,
        State.IN_PROGRESS.value,
        State.NEEDS_REVISION.value,
        State.PENDING_QA.value,
        State.PENDING_DESIGN.value,
        State.IN_REVIEW.value,
        State.PENDING_APPROVAL.value,
        State.STALLED.value,
        State.APPROVED.value,
    }
)

#: Ticket counterpart of ``ACTIVE_CAPSULE_STATES`` — the value form of
#: ``shared_kernel.state_schema.TICKET_ACTIVE_STATES`` (every non-terminal
#: TicketState). One vocab per entity: a ticket never filters on capsule
#: states and vice versa.
ACTIVE_TICKET_STATES: frozenset = frozenset(
    state.value for state in TICKET_ACTIVE_STATES
)

#: How many spool manifests one telemetry sweep will read (CPU-light guard).
MAX_TELEMETRY_CAPSULES = 2000


def _gate_records_safe(manifest: Dict[str, Any]) -> List[Any]:
    """Read a capsule's gate plan; corruption degrades to an empty list."""
    try:
        from capsule_engine import gates as gate_engine

        return list(gate_engine.load_records(manifest))
    except Exception:
        return []


def _gate_projection(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The radar's gate summary: counts + ordered checklist + progress."""
    try:
        from capsule_engine import gates as gate_engine
        from capsule_engine import trace as trace_engine

        records = gate_engine.load_records(manifest)
        plan = gate_engine.progress(records)
        return {
            "total": plan["total"],
            "passed": plan["passed"],
            "failed": plan["failed"],
            "skipped": plan["skipped"],
            "pending": plan["pending"],
            "percent": plan["percent"],
            "complete": plan["complete"],
            "active_gate": plan["active_gate"],
            "active_gate_type": plan["active_gate_type"],
            "checklist": gate_engine.checklist(records),
            "summary": gate_engine.summary_line(records),
            "progress_percent": trace_engine.progress_percent(
                manifest, gate_records=records
            ),
        }
    except Exception:
        return {
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "pending": 0,
            "percent": 0.0, "complete": False, "active_gate": None,
            "active_gate_type": None, "checklist": [], "summary": "Gates: none",
            "progress_percent": 0.0,
        }


def _capsule_domain(manifest: Dict[str, Any]) -> str:
    """Best-effort domain label (top-level wins, then metadata/routing)."""
    for candidate in (
        manifest.get("domain"),
        (manifest.get("metadata") or {}).get("domain")
        if isinstance(manifest.get("metadata"), dict)
        else None,
        (manifest.get("routing_context") or {}).get("domain")
        if isinstance(manifest.get("routing_context"), dict)
        else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "marketing"


def _usage_projection(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Tokens/cost carried on the manifest (missing counters default to 0)."""
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    cost = metadata.get("estimated_cost_usd", usage.get("estimated_cost_usd"))
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "calls": int(usage.get("calls") or 0),
        "estimated_cost_usd": round(float(cost), 6) if isinstance(cost, (int, float)) else 0.0,
    }


def _task_projection(capsule_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """One row of the executive radar / in-flight matrix (read-only)."""
    from capsule_engine import trace as trace_engine

    gates = _gate_projection(manifest)
    live = trace_engine.in_flight(capsule_dir, capsule_id=str(manifest.get("capsule_id") or capsule_dir.name))
    trace_summary = trace_engine.summary(capsule_dir, limit=200)
    last = trace_summary.last_step or {}
    return {
        "capsule_id": manifest.get("capsule_id") or capsule_dir.name,
        "domain": _capsule_domain(manifest),
        "state": manifest.get("state"),
        "updated_at": manifest.get("updated_at"),
        "created_at": manifest.get("created_at"),
        "running": live is not None,
        "active_step": live.step_name if live else last.get("step_name"),
        "step_id": live.step_id if live else last.get("step_id"),
        "active_gear": live.active_gear if live else last.get("gear_invoked"),
        "progress_percent": live.progress_percent if live else gates["progress_percent"],
        "overall_progress_percent": gates["progress_percent"],
        "elapsed_seconds": round(live.elapsed_seconds, 1) if live else (
            round(float(last.get("duration_seconds") or 0.0), 1) if last else 0.0
        ),
        "started_at": live.started_at if live else last.get("started_at"),
        "last_step_status": last.get("status"),
        "gates": gates,
        "gates_total": gates["total"],
        "gates_passed": gates["passed"],
        "active_gate": gates["active_gate"],
        "active_gate_type": gates["active_gate_type"],
        "tokens": _usage_projection(manifest),
        "trace_steps": trace_summary.steps,
        "path": str(capsule_dir),
    }


def _spool_manifests() -> List[Path]:
    """Bounded, recursive spool scan (one broken capsule never kills the pass)."""
    if not SPOOL_DIR.is_dir():
        return []
    found: List[Path] = []
    try:
        for manifest_path in sorted(SPOOL_DIR.rglob("manifest.json")):
            if manifest_path.is_file():
                found.append(manifest_path.parent)
            if len(found) >= MAX_TELEMETRY_CAPSULES:
                break
    except OSError:
        pass
    return found


@router.get("/api/tasks/in-flight")
async def list_in_flight_tasks(limit: int = 100) -> Dict[str, Any]:
    """Live in-flight telemetry for the web cockpit and the Flutter app.

    Every active capsule is reported with its instantaneous execution fields
    (``active_step``, ``progress_percent``, ``active_gear``,
    ``elapsed_seconds``), its gate checklist, and its cumulative token/cost
    accounting. ``running: true`` marks a capsule whose telemetry sidecar is
    currently fresh (a step is executing right now); the rest are queued at a
    gate or waiting on a human.
    """
    bounded = max(1, min(int(limit), 500))
    tasks: List[Dict[str, Any]] = []
    for capsule_dir in _spool_manifests():
        try:
            manifest = load_manifest(capsule_dir)
        except Exception:
            continue
        if manifest.get("state") not in ACTIVE_CAPSULE_STATES:
            continue
        try:
            tasks.append(_task_projection(capsule_dir, manifest))
        except Exception as exc:  # one bad capsule never kills the sweep
            logger.warning("task projection failed for %s: %s", capsule_dir, exc)

    tasks.sort(
        key=lambda t: (
            0 if t.get("running") else 1,
            -(float(t.get("elapsed_seconds") or 0.0)),
            str(t.get("updated_at") or ""),
        )
    )
    running = [t for t in tasks if t.get("running")]
    return {
        "generated_at": now_iso(),
        "count": len(tasks),
        "running_count": len(running),
        "tasks": tasks[:bounded],
    }


def _ticket_radar_row(
    *,
    ticket_id: str,
    domain: str,
    state: Any,
    updated_at: Any,
    path: Any,
    active_gate: Any = None,
    capsule_id: Any = None,
) -> Dict[str, Any]:
    """One ticket row of the executive radar — same shape as a capsule row.

    Additive by contract: capsule rows keep every field they had, and a
    ticket arrives beside them with ``kind: "ticket"`` plus honest zeroed
    telemetry (a ticket carries no tokens/cost/progress of its own).
    """
    return {
        "kind": "ticket",
        "ticket_id": ticket_id,
        "capsule_id": capsule_id,
        "domain": domain,
        "state": state,
        "updated_at": updated_at,
        "created_at": updated_at,
        "running": False,
        "active_step": None,
        "active_gear": None,
        "progress_percent": 0.0,
        "gates_total": 0,
        "gates_passed": 0,
        "active_gate": active_gate,
        "active_gate_type": None,
        "checklist": [],
        "tokens": {"total_tokens": 0, "calls": 0},
        "estimated_cost_usd": 0.0,
        "path": path,
    }


@router.get("/api/radar/tasks")
async def executive_radar_tasks(limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """Brick: executive radar matrix — task identity, gates, tokens and cost.

    Index-backed when the read cache is available (fast at scale), with an
    honest file-scan fallback. Each row carries the domain categorization,
    live execution percentage, gate checklist summary and cumulative usage.

    Both entities: every row is tagged with ``kind`` ("capsule" | "ticket")
    and filtered by *its own* vocabulary (``ACTIVE_CAPSULE_STATES`` /
    ``ACTIVE_TICKET_STATES``). Capsule rows keep their historical order;
    active tickets follow them, newest first.
    """
    bounded = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    rows: List[Dict[str, Any]] = []
    ticket_rows: List[Dict[str, Any]] = []

    source = "files"
    if read_index.enabled() and read_index.ensure():
        source = "index"
        for row in read_index.query_capsules(limit=10_000, offset=0):
            rows.append(
                {
                    "kind": "capsule",
                    "capsule_id": row.get("capsule_id"),
                    "domain": row.get("domain") or "(none)",
                    "state": row.get("state"),
                    "updated_at": row.get("updated_at"),
                    "running": False,
                    "active_step": None,
                    "active_gear": None,
                    "progress_percent": float(row.get("progress_percent") or 0.0),
                    "gates_total": int(row.get("gates_total") or 0),
                    "gates_passed": int(row.get("gates_passed") or 0),
                    "active_gate": row.get("active_gate"),
                    "active_gate_type": row.get("active_gate_type"),
                    "checklist": [],
                    "tokens": {
                        "total_tokens": int(row.get("total_tokens") or 0),
                        "calls": int(row.get("calls") or 0),
                    },
                    "estimated_cost_usd": float(row.get("cost_usd") or 0.0),
                    "path": row.get("path"),
                }
            )
        for row in read_index.query_tickets(limit=10_000, offset=0, stages=None):
            ticket_rows.append(
                _ticket_radar_row(
                    ticket_id=str(row.get("ticket_id") or ""),
                    domain=str(row.get("sector") or "(none)"),
                    state=row.get("state"),
                    updated_at=row.get("created_at"),
                    path=row.get("path"),
                    active_gate=row.get("gate_id"),
                )
            )
    else:
        for capsule_dir in _spool_manifests():
            try:
                manifest = load_manifest(capsule_dir)
            except Exception:
                continue
            try:
                projection = _task_projection(capsule_dir, manifest)
                projection["kind"] = "capsule"
                rows.append(projection)
            except Exception:
                continue
        for ticket in await list_tickets():
            ticket_rows.append(
                _ticket_radar_row(
                    ticket_id=str(ticket.get("ticket_id") or ticket.get("id") or ""),
                    domain=str(ticket.get("sector") or "(none)"),
                    state=ticket.get("state"),
                    updated_at=ticket.get("created_at"),
                    path=ticket.get("file_path"),
                    active_gate=ticket.get("gate_id"),
                    capsule_id=ticket.get("spool_capsule"),
                )
            )

    active = [
        row for row in rows
        if str(row.get("state")) in ACTIVE_CAPSULE_STATES
    ]
    active.sort(
        key=lambda r: (
            -(float(r.get("progress_percent") or 0.0)),
            str(r.get("updated_at") or ""),
        )
    )

    # Additive: capsule ordering above is untouched; active tickets follow.
    active_tickets = [
        row for row in ticket_rows
        if str(row.get("state")) in ACTIVE_TICKET_STATES
    ]
    active_tickets.sort(
        key=lambda r: str(r.get("updated_at") or ""),
        reverse=True,
    )
    active = active + active_tickets

    def _row_tokens(row: Dict[str, Any]) -> int:
        tokens = row.get("tokens")
        if isinstance(tokens, dict):
            return int(tokens.get("total_tokens") or 0)
        return int(row.get("total_tokens") or 0)

    def _row_cost(row: Dict[str, Any]) -> float:
        cost = row.get("estimated_cost_usd", row.get("cost_usd"))
        return float(cost) if isinstance(cost, (int, float)) else 0.0

    totals = {
        "tasks": len(active),
        "gated": sum(1 for r in active if int(r.get("gates_total") or 0) > 0),
        "total_tokens": sum(_row_tokens(r) for r in active),
        "estimated_cost_usd": round(sum(_row_cost(r) for r in active), 6),
        "by_domain": {},
    }
    for row in active:
        key = str(row.get("domain") or "(none)")
        totals["by_domain"][key] = totals["by_domain"].get(key, 0) + 1
    return {
        "generated_at": now_iso(),
        "source": source,
        "totals": totals,
        "tasks": active[safe_offset : safe_offset + bounded],
    }


@router.get("/api/capsules/{capsule_id}/gates")
async def get_capsule_gates(
    capsule_id: str,
    open_active: bool = Query(
        True,
        description=(
            "Open the active gate while inspecting the plan, binding the "
            "baseline SHA-256 digests of every declared target (anti-TOCTOU). "
            "This is a state change on a GET: pass open_active=false for a "
            "pure read that never mutates the capsule."
        ),
    ),
) -> Dict[str, Any]:
    """The capsule's full gate plan: checklist, statuses, evidence, progress.

    NOTE — this route is not read-only by default. Inspecting a plan with an
    unopened active gate **opens** that gate (``by="cockpit:inspect"``) to
    establish the anti-TOCTOU baseline SHA-256 digests, then persists the
    manifest. That side effect is deliberate (the baseline must exist before a
    human reviews), but it makes the default a state-changing GET. Pass
    ``?open_active=false`` when you need an inspection that changes nothing.
    """
    clean_id = Path(capsule_id).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)

    # When the gate plan is inspected for review, open the active gate to establish
    # the baseline SHA-256 digests of all declared targets (anti-TOCTOU).
    if open_active:
        try:
            from capsule_engine import gates as gate_engine
            records = gate_engine.load_records(manifest)
            active = gate_engine.active_record(records)
            if active and not active.opened_at:
                with CapsuleLock(capsule_path):
                    manifest = load_manifest(capsule_path)
                    records = gate_engine.load_records(manifest)
                    active = gate_engine.active_record(records)
                    if active and not active.opened_at:
                        gate_engine.open_gate(
                            manifest, active.gate_id, capsule_path, by="cockpit:inspect"
                        )
                        save_manifest(capsule_path, manifest)
        except Exception as exc:
            logger.warning("get_capsule_gates: could not auto-open active gate for %s: %s", clean_id, exc)

    projection = _gate_projection(manifest)
    return {
        "capsule_id": manifest.get("capsule_id") or clean_id,
        "domain": _capsule_domain(manifest),
        "state": manifest.get("state"),
        "gates": projection,
    }


@router.post("/api/capsules/{capsule_id}/gates/{gate_id}/open")
async def open_capsule_gate(
    capsule_id: str, gate_id: str, request: Request
) -> Dict[str, Any]:
    """Open an active gate and SHA-256 bind all declared targets for review."""
    clean_id = Path(capsule_id).name.strip()
    gate_key = Path(gate_id).name.strip()
    reviewer, audit_note = _decision_identity(request)
    capsule_path = _confined_capsule_path(clean_id)
    try:
        from capsule_engine import gates as gate_engine

        with CapsuleLock(capsule_path):
            manifest = load_manifest(capsule_path)
            domain = _capsule_domain(manifest)
            _require_scope(request, "factory:read", domain)
            records = gate_engine.load_records(manifest)
            active = gate_engine.active_record(records)
            if active is None or active.gate_id != gate_key:
                raise HTTPException(
                    status_code=409,
                    detail=f"Gate '{gate_key}' is not active or plan is complete",
                )
            opened = gate_engine.open_gate(
                manifest, gate_key, capsule_path, by=reviewer
            )
            save_manifest(capsule_path, manifest)
            return {
                "capsule_id": clean_id,
                "gate_id": gate_key,
                "opened_at": opened.opened_at,
                "artifacts": opened.artifacts,
                "status": opened.status,
            }
    except gate_engine.GateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/capsules/{capsule_id}/gates/{gate_id}/decision")
async def decide_capsule_gate(
    capsule_id: str, gate_id: str, req: GateDecisionRequest, request: Request
) -> Dict[str, Any]:
    """Approve, reject or (lawfully) skip one specific gate of a capsule's plan.

    Thin wrapper over the unified service (``approval.approve`` /
    ``approval.reject``), with this route's unique handling layered on top:

    - the decision vocabulary and the mandatory rejection reason are request
      validation, checked here before any lock;
    - ``skip`` of an optional gate and the 409 for a named gate that is not
      the active one are the service's named-gate checks;
    - ``in_review`` is this route's second home: a reject there uses the
      lawful ``in_review -> needs_revision`` edge, and a plan that completes
      while the capsule waits there is seated (``in_review ->
      pending_approval``, guarded by transition(): the plan must be
      complete) through ``seat_pending_on_complete``.

    Everything else — the strictest permissions (branch capability **and**
    the gate's declared authority), the state guard, TOCTOU verification,
    ``feedback.txt``, the feedback ledger, the immediate autonomy demotion,
    the trace entry and the event — lives in the service, the single source
    of truth.
    """
    clean_id = Path(capsule_id).name.strip()
    gate_key = Path(gate_id).name.strip()
    reviewer, audit_note = _decision_identity(request)
    capsule_path = _confined_capsule_path(clean_id)
    decision_key = (req.decision or "").strip().lower()
    if decision_key not in ("approve", "reject", "skip"):
        raise HTTPException(
            status_code=400, detail="decision must be approve, reject or skip"
        )
    reason = (req.reason or "").strip()
    if decision_key == "reject" and not reason:
        raise HTTPException(status_code=400, detail="A rejection requires a reason")

    try:
        if decision_key == "reject":
            outcome = approval.reject(
                capsule_path,
                principal=getattr(request.state, "principal", None),
                reviewer=reviewer,
                audit_note=audit_note,
                reason=reason,
                named_gate=gate_key,
                why=f"gate {gate_key} rejected: {reason} — {audit_note}",
            )
        else:
            outcome = approval.approve(
                capsule_path,
                principal=getattr(request.state, "principal", None),
                reviewer=reviewer,
                audit_note=audit_note,
                named_gate=gate_key,
                skip=(decision_key == "skip"),
                reason=reason or f"{decision_key} via cockpit — {audit_note}",
                seat_pending_on_complete=True,
            )
    except approval.ApprovalRefused as refused:
        raise HTTPException(
            status_code=refused.status_code, detail=refused.detail
        ) from refused
    except SecurityError as sec_exc:
        raise HTTPException(
            status_code=400, detail=f"TOCTOU Integrity Violation: {sec_exc}"
        ) from sec_exc
    except LockTimeoutError as lock_exc:
        raise HTTPException(
            status_code=409,
            detail=f"Capsule is locked by another process: {lock_exc}",
        ) from lock_exc
    except StateTransitionError as trans_exc:
        raise HTTPException(
            status_code=400, detail=f"Illegal state transition: {trans_exc}"
        ) from trans_exc
    except CapsuleError as cap_exc:
        raise HTTPException(status_code=400, detail=f"Capsule error: {cap_exc}") from cap_exc

    plan = outcome.plan
    if decision_key == "reject":
        message = (
            f"Gate '{gate_key}' rejected — capsule returned for revision"
            if outcome.state == State.NEEDS_REVISION.value
            else (
                f"Gate '{gate_key}' rejected — revision cap reached; capsule "
                f"moved to {outcome.state} for a human decision"
            )
        )
    else:
        message = f"Gate '{gate_key}' {plan['passed']}/{plan['total']} complete" + (
            f" — awaiting gate '{plan['active_gate']}'"
            if plan.get("active_gate")
            else " — all gates complete"
        )
    return {
        "status": "success",
        "capsule_id": clean_id,
        "gate_id": outcome.gate_id,
        "gate_type": outcome.gate_type,
        "decision": decision_key,
        "new_state": outcome.state,
        "gates": {
            **plan,
            "checklist": outcome.checklist,
        },
        "message": message,
    }


@router.get("/api/capsules/{capsule_id}/trace")
async def get_capsule_trace(capsule_id: str, limit: int = 200) -> Dict[str, Any]:
    """The capsule's immutable execution trace + rolled-up accounting."""
    from capsule_engine import trace as trace_engine

    clean_id = Path(capsule_id).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    bounded = max(1, min(int(limit), 2000))
    read = trace_engine.read_trace(capsule_path, limit=bounded)
    chain = trace_engine.verify_trace(capsule_path)
    return {
        "capsule_id": clean_id,
        "events": [dict(event) for event in read.events],
        "malformed": read.malformed,
        "truncated": read.truncated,
        "chain": {
            "ok": chain.ok,
            "events": chain.events,
            "first_bad_line": chain.first_bad_line,
            "detail": chain.detail,
        },
        "summary": trace_engine.summary(capsule_path).to_dict(),
    }


@router.get("/api/memory/feedback")
async def list_feedback_ledger(
    domain: Optional[str] = None,
    decision: Optional[str] = None,
    capsule_id: Optional[str] = None,
    window_days: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Cumulative feedback memory: the ledger's entries and aggregate stats.

    The ledger file is the truth; the read index can serve the same views at
    scale. Filters: domain/tag scope, decision, capsule, and the rolling
    window (default from ``config/engine.json``).
    """
    from capsule_engine import memory as memory_engine

    bounded = max(1, min(int(limit), 500))
    window_days = window_days if window_days and window_days > 0 else None
    window = memory_engine.read_entries(
        window_days=window_days,
        domain=domain,
        decisions=[decision] if decision else None,
        capsule_id=capsule_id,
        limit=None,
    )
    sliced = window.entries[max(0, int(offset)) :][:bounded]
    return {
        "generated_at": now_iso(),
        "stats": memory_engine.stats(window_days=window_days),
        "window_days": window.window_days,
        "total": len(window.entries),
        "malformed": window.malformed,
        "entries": [entry.to_dict() for entry in sliced],
    }


#: Text artifacts are embedded up to this many bytes per file in a preview.
PREVIEW_TEXT_BYTES = 16 * 1024
#: Images are embedded as data URIs up to this many bytes each / per preview.
PREVIEW_IMAGE_BYTES = 512 * 1024
PREVIEW_MAX_IMAGES = 12
PREVIEW_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
PREVIEW_TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".yaml", ".yml",
     ".html", ".htm", ".py", ".js", ".ts", ".css", ".sh", ".sql"}
)


def _preview_file_block(
    capsule_path: Path, file_path: Path, *, include_images: bool, image_budget: List[int]
) -> Dict[str, Any]:
    """One deliverable file as the preview/API sees it (bounded, read-only)."""
    import hashlib

    relative = file_path.relative_to(capsule_path).as_posix()
    try:
        raw = file_path.read_bytes()
    except OSError:
        return {"path": relative, "readable": False}
    suffix = file_path.suffix.lower()
    block: Dict[str, Any] = {
        "path": relative,
        "name": file_path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "kind": "image" if suffix in PREVIEW_IMAGE_SUFFIXES else "text",
        "readable": True,
    }
    if suffix in PREVIEW_TEXT_SUFFIXES or (not suffix and b"\x00" not in raw[:1024]):
        block["content"] = raw[:PREVIEW_TEXT_BYTES].decode("utf-8", errors="replace")
        block["truncated"] = len(raw) > PREVIEW_TEXT_BYTES
        return block
    if suffix in PREVIEW_IMAGE_SUFFIXES and include_images:
        if len(raw) <= PREVIEW_IMAGE_BYTES and image_budget[0] > 0:
            image_budget[0] -= 1
            mime = "image/png" if suffix == ".png" else (
                "image/webp" if suffix == ".webp" else (
                    "image/gif" if suffix == ".gif" else "image/jpeg"
                )
            )
            block["data_uri"] = (
                f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            )
            try:  # dimensions are a courtesy; PIL failure never fails a preview
                from PIL import Image

                with Image.open(file_path) as image:
                    block["width"], block["height"] = image.size
            except Exception:
                pass
        else:
            block["embedded"] = False
    return block


@router.get("/api/capsules/{capsule_id}/preview")
async def preview_capsule_delivery(
    capsule_id: str, include_images: bool = True
) -> Dict[str, Any]:
    """Realistic delivery preview: the exact output before final dispatch.

    Assembles, read-only, everything a reviewer (Web cockpit or Flutter app)
    needs to see the real-world result: the compartmentalized deliverable tree
    (per channel: copy + branded image + metadata), the company profile DNA,
    the gate checklist, the execution trace summary and cumulative token/cost
    accounting. ``ready_for_dispatch`` is true only when every gate is
    terminal-passed and the capsule reached ``approved``/``published``.
    """
    from capsule_engine import trace as trace_engine

    clean_id = Path(capsule_id).name.strip()
    capsule_path = _confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)

    try:
        from shared_kernel.brand_profile import load_company_profile

        profile = load_company_profile() or {}
    except Exception:
        profile = {}

    image_budget = [PREVIEW_MAX_IMAGES]
    channels: List[Dict[str, Any]] = []
    deliverables_root = capsule_path / "deliverables"
    if deliverables_root.is_dir():
        try:
            channel_dirs = sorted(
                entry for entry in deliverables_root.iterdir() if entry.is_dir()
            )
        except OSError:
            channel_dirs = []
        for channel_dir in channel_dirs:
            files: List[Dict[str, Any]] = []
            try:
                for file_path in sorted(channel_dir.rglob("*")):
                    if file_path.is_file():
                        files.append(
                            _preview_file_block(
                                capsule_path,
                                file_path,
                                include_images=include_images,
                                image_budget=image_budget,
                            )
                        )
            except OSError:
                pass
            copy_block = next(
                (f for f in files if f.get("name", "").startswith("copy") and "content" in f),
                None,
            )
            image_block = next(
                (f for f in files if f.get("kind") == "image"), None
            )
            channels.append(
                {
                    "channel": channel_dir.name,
                    "copy": copy_block.get("content") if copy_block else None,
                    "image": {
                        "path": image_block.get("path"),
                        "data_uri": image_block.get("data_uri"),
                        "width": image_block.get("width"),
                        "height": image_block.get("height"),
                    }
                    if image_block
                    else None,
                    "files": files,
                }
            )

    legacy_files: List[Dict[str, Any]] = []
    for name in (ARTIFACT_FILE, "copy.md", "image.jpg", "image.png", "image_prompt.txt"):
        candidate = capsule_path / name
        if candidate.is_file():
            legacy_files.append(
                _preview_file_block(
                    capsule_path,
                    candidate,
                    include_images=include_images,
                    image_budget=image_budget,
                )
            )

    gates = _gate_projection(manifest)
    state = str(manifest.get("state") or "")
    ready = gates["complete"] and state in (
        State.APPROVED.value,
        State.PUBLISHED.value,
    )
    trace_summary = trace_engine.summary(capsule_path, limit=200)
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}

    return {
        "generated_at": now_iso(),
        "capsule_id": manifest.get("capsule_id") or clean_id,
        "domain": _capsule_domain(manifest),
        "state": state,
        "updated_at": manifest.get("updated_at"),
        "ready_for_dispatch": ready,
        "brand_dna": {
            "name": profile.get("company_name")
            or profile.get("brand_name")
            or profile.get("name"),
            "brand_name": profile.get("brand_name"),
            "legal_name": profile.get("legal_name"),
            "tagline": profile.get("tagline"),
            "industry": profile.get("industry"),
            "colors": profile.get("brand_colors") or profile.get("colors"),
            "fonts": profile.get("typography") or profile.get("fonts"),
            "logo_present": bool(
                profile.get("logo_path")
                or profile.get("logo_url")
                or profile.get("logo")
            ),
            "logo_url": profile.get("logo_url") or profile.get("logo_path"),
            "website": profile.get("website"),
            "timezone": profile.get("timezone"),
            "currency": profile.get("currency"),
            "profile_source": "config/company_profile.json",
        },
        "content": {
            "channels": channels,
            "legacy_artifacts": legacy_files,
            "prompt": manifest.get("raw_prompt") or manifest.get("prompt"),
            "topic": metadata.get("topic"),
            "target_platform": metadata.get("target_platform"),
        },
        "gates": gates,
        "accounting": {
            **_usage_projection(manifest),
            "trace": trace_summary.to_dict(),
        },
        "metadata": {
            key: metadata.get(key)
            for key in ("topic", "country", "geo", "occasion", "event", "target_platform")
            if metadata.get(key)
        },
        "preview_version": 1,
    }


getattr(app, "add_event_handler", app.router.add_event_handler)(
    "startup", _warn_migration_debt
)


app.include_router(router)

# The mobile contract (/v1) is mounted last: every /api route and the auth
# middleware exist by now, and installing it here keeps the new namespace from
# reaching into the cockpit's wiring. ``install()`` adds the two /v1 routes and
# the envelope handlers (which delegate for every non-/v1 path).
v1_errors.install(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090, access_log=True)
