#!/usr/bin/env python3
"""trace.py — the granular execution trace and live in-flight telemetry.

Two artifacts per capsule, both plain files a human can open:

``execution_trace.jsonl``
    Append-only, hash-chained chronological audit of every step. Three event
    kinds share one stream:

    - ``step_start``    — ``step_id``, ``step_name``, ``started_at``, gear;
    - ``step_progress`` — ``step_id``, ``progress_percent``, ``active_gear``;
    - ``step_end``      — the required audit record: ``step_id``,
      ``step_name``, ``started_at``, ``finished_at``, ``duration_seconds``,
      ``gear_invoked`` (verb/provider/model), ``tokens_used`` (prompt /
      completion / total), ``estimated_cost_usd``, ``status`` (``success``,
      ``failed``, ``needs_revision``, ``held``), ``output_artifact_hashes``.

    Each line carries ``prev``/``hash`` (SHA-256 chain), so tampering or a torn
    append is detectable with :func:`verify_trace` — never silently repaired.

``telemetry.json``
    A tiny atomic sidecar rewritten while a step runs. It is what the radar
    reads to answer "what is happening *right now*": ``active_step``,
    ``progress_percent``, ``active_gear``, ``elapsed_seconds``. It is deleted
    when the step ends; a crashed process leaves it behind, and
    :func:`in_flight` then treats it as stale after ``stale_after`` seconds —
    the same lease philosophy the router applies to ``in_progress`` manifests.

Failure policy: tracing is infrastructure and must **never** break the work it
observes. Every write is best-effort (logged, counted in ``errors``); every
read reports malformed lines instead of guessing.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:  # POSIX advisory locking for concurrent appends to one capsule
    import fcntl
except ImportError:  # pragma: no cover - Windows: O_APPEND alone
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("capsule_engine.trace")

__all__ = [
    "TRACE_NAME",
    "TELEMETRY_NAME",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "STATUS_NEEDS_REVISION",
    "STATUS_HELD",
    "STATUS_RUNNING",
    "STEP_STATUSES",
    "MAX_LINE_BYTES",
    "STATE_PROGRESS",
    "ExecutionTracer",
    "StepHandle",
    "TraceRead",
    "TraceChainCheck",
    "InFlight",
    "TraceSummary",
    "trace_path",
    "telemetry_path",
    "read_trace",
    "verify_trace",
    "in_flight",
    "progress_percent",
    "summary",
    "estimate_cost",
    "hash_artifacts",
    "now_iso",
]

TRACE_NAME = "execution_trace.jsonl"
TELEMETRY_NAME = "telemetry.json"

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_NEEDS_REVISION = "needs_revision"
STATUS_HELD = "held"

#: The closed status vocabulary of a finished step.
STEP_STATUSES: frozenset[str] = frozenset(
    {STATUS_SUCCESS, STATUS_FAILED, STATUS_NEEDS_REVISION, STATUS_HELD}
)

MAX_LINE_BYTES = 128 * 1024
MAX_STEP_NAME_CHARS = 120
MAX_EVENT_FIELD_CHARS = 512
DEFAULT_STALE_AFTER = 1800.0  # 30 min: a crashed step stops being "in flight"
DEFAULT_MAX_ARTIFACT_HASHES = 20
DEFAULT_MAX_HASH_BYTES = 8 * 1024 * 1024

#: Milestone completion per capsule state (percent). The bands between them
#: are filled by the *observed* progress of the active step / gate plan, so
#: the radar shows movement inside a long stage instead of a frozen number.
STATE_PROGRESS: Mapping[str, float] = {
    "draft": 5.0,
    "in_progress": 20.0,
    "pending_qa": 45.0,
    "pending_design": 60.0,
    "in_review": 70.0,
    "pending_approval": 80.0,
    "needs_revision": 35.0,
    "stalled": 80.0,
    "approved": 90.0,
    "published": 100.0,
    "failed": 0.0,
    "archived": 100.0,
}
_BAND_ORDER: Tuple[str, ...] = (
    "draft",
    "in_progress",
    "needs_revision",
    "pending_qa",
    "pending_design",
    "in_review",
    "pending_approval",
    "approved",
    "published",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """Aware UTC timestamp in strict ISO-8601 (seconds resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _chain_hash(prev: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev.encode("utf-8"))
    digest.update(b"\n")
    digest.update(_canonical(payload))
    return digest.hexdigest()


def _clamp_percent(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:  # NaN
        return None
    return round(max(0.0, min(100.0, number)), 1)


def _clean_tokens(usage: object) -> Dict[str, int]:
    """Normalize a usage mapping to the three canonical counters."""
    if not isinstance(usage, Mapping):
        return {}
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        out[key] = int(value)
    if "total_tokens" not in out and (
        "prompt_tokens" in out or "completion_tokens" in out
    ):
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get(
            "completion_tokens", 0
        )
    return out


def trace_path(capsule_path: str | os.PathLike[str]) -> Path:
    """The capsule's execution trace file."""
    return Path(capsule_path) / TRACE_NAME


def telemetry_path(capsule_path: str | os.PathLike[str]) -> Path:
    """The capsule's live telemetry sidecar."""
    return Path(capsule_path) / TELEMETRY_NAME


def estimate_cost(
    gear: Optional[str],
    tokens: Mapping[str, int] | None,
    *,
    policy: Optional[Any] = None,
) -> float:
    """Estimated USD for a gear invocation from the configured price table."""
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            return 0.0
    return policy.trace.estimate_cost(gear, tokens)


def hash_artifacts(
    capsule_path: str | os.PathLike[str],
    relatives: Sequence[str],
    *,
    max_files: int = DEFAULT_MAX_ARTIFACT_HASHES,
    max_bytes: int = DEFAULT_MAX_HASH_BYTES,
) -> Dict[str, str]:
    """SHA-256 the named artifacts (bounded), skipping unreadable/oversized.

    Used for ``output_artifact_hashes`` in step_end events: a bounded,
    best-effort projection — a missing file is simply absent from the record,
    never hashed as empty.
    """
    capsule = Path(capsule_path).resolve()
    out: Dict[str, str] = {}
    budget = max_bytes
    for relative in relatives:
        if len(out) >= max_files or budget <= 0:
            break
        if not isinstance(relative, str) or not relative.strip():
            continue
        candidate = (capsule / relative).resolve()
        if candidate != capsule and capsule not in candidate.parents:
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > budget:
            continue
        try:
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        except OSError:
            continue
        budget -= size
        out[relative] = digest.hexdigest()
    return out


# --------------------------------------------------------------------------- #
# read structures
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TraceRead:
    """A decoded trace (oldest → newest)."""

    events: Tuple[Dict[str, Any], ...]
    malformed: int
    path: str
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.events)

    @property
    def last_step_end(self) -> Optional[Dict[str, Any]]:
        for event in reversed(self.events):
            if event.get("event") == "step_end":
                return dict(event)
        return None


@dataclass(frozen=True)
class TraceChainCheck:
    """Result of verifying the trace's hash chain."""

    ok: bool
    events: int
    first_bad_line: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class InFlight:
    """The live view of a capsule's running step (or ``None`` semantics)."""

    capsule_id: str
    step_id: str
    step_name: str
    started_at: str
    elapsed_seconds: float
    progress_percent: float
    active_gear: Optional[str]
    errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_step": self.step_name,
            "step_id": self.step_id,
            "step_name": self.step_name,
            "started_at": self.started_at,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "progress_percent": self.progress_percent,
            "active_gear": self.active_gear,
        }


@dataclass(frozen=True)
class TraceSummary:
    """Rolled-up accounting derived from the trace (radar, previews)."""

    steps: int
    last_step: Optional[Dict[str, Any]]
    total_duration_seconds: float
    tokens: Mapping[str, int]
    estimated_cost_usd: float
    malformed: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": self.steps,
            "last_step": self.last_step,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "tokens": dict(self.tokens),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "malformed": self.malformed,
        }


# --------------------------------------------------------------------------- #
# the tracer
# --------------------------------------------------------------------------- #

class StepHandle:
    """One traced step. Prefer ``with tracer.step(...) as step:``."""

    def __init__(
        self,
        tracer: "ExecutionTracer",
        *,
        step_id: str,
        step_name: str,
        gear: Optional[str],
        started_at: str,
    ) -> None:
        self._tracer = tracer
        self.step_id = step_id
        self.step_name = step_name
        self.gear = gear
        self.started_at = started_at
        self._started_monotonic = time.monotonic()
        self._progress = 0.0
        self._tokens: Dict[str, int] = {}
        self._cost_usd = 0.0
        self._artifacts: Dict[str, str] = {}
        self._finished = False

    # -- instrumentation ---------------------------------------------------- #

    @property
    def progress_percent(self) -> float:
        """The handle's last published in-step progress (0–100)."""
        return self._progress

    def progress(self, percent: float, *, active_gear: Optional[str] = None) -> None:
        """Publish an instantaneous progress update for the radar."""
        clean = _clamp_percent(percent)
        if clean is None:
            return
        if active_gear:
            self.gear = str(active_gear)[:MAX_STEP_NAME_CHARS]
        self._progress = clean
        self._tracer._emit(
            {
                "event": "step_progress",
                "step_id": self.step_id,
                "step_name": self.step_name,
                "progress_percent": clean,
                "active_gear": self.gear,
            }
        )
        self._tracer._write_telemetry(self)

    def add_tokens(self, usage: object, *, cost_usd: Optional[float] = None) -> None:
        """Fold gear usage into the step (and estimate cost when not given)."""
        cleaned = _clean_tokens(usage)
        for key, value in cleaned.items():
            self._tokens[key] = self._tokens.get(key, 0) + value
        if cost_usd is None:
            self._cost_usd += estimate_cost(self.gear, cleaned)
        elif isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
            self._cost_usd += float(cost_usd)

    def add_artifacts(self, artifacts: Mapping[str, str]) -> None:
        """Record output artifact digests (relative path → sha256)."""
        for key, value in artifacts.items():
            if isinstance(key, str) and isinstance(value, str):
                self._artifacts[key] = value

    def hash_outputs(self, relatives: Sequence[str]) -> None:
        """Hash the named files and record them as this step's outputs."""
        self.add_artifacts(
            hash_artifacts(self._tracer.capsule_path, relatives)
        )

    # -- lifecycle ---------------------------------------------------------- #

    def finish(self, status: str = STATUS_SUCCESS, *, error: Optional[str] = None) -> None:
        """Close the step: append the ``step_end`` audit record (idempotent)."""
        if self._finished:
            return
        self._finished = True
        duration = max(0.0, time.monotonic() - self._started_monotonic)
        resolved = status if status in STEP_STATUSES else STATUS_FAILED
        event: Dict[str, Any] = {
            "event": "step_end",
            "step_id": self.step_id,
            "step_name": self.step_name,
            "started_at": self.started_at,
            "finished_at": now_iso(),
            "duration_seconds": round(duration, 3),
            "gear_invoked": self.gear,
            "tokens_used": dict(self._tokens),
            "estimated_cost_usd": round(self._cost_usd, 6),
            "status": resolved,
            "output_artifact_hashes": dict(self._artifacts),
        }
        if error:
            event["error"] = str(error)[:MAX_EVENT_FIELD_CHARS]
        self._tracer._emit(event)
        self._tracer._clear_telemetry(self)

    def __enter__(self) -> "StepHandle":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            self.finish(STATUS_SUCCESS)
        else:
            self.finish(
                STATUS_FAILED,
                error=f"{getattr(exc_type, '__name__', 'Exception')}: {exc}",
            )
        return False  # never swallow the worker's exception


class ExecutionTracer:
    """Append-only tracer over one capsule.

    Args:
        capsule_path: the capsule directory (created if absent).
        capsule_id: recorded on every event.
        enabled: when False, every method is a lawful no-op.
        policy: optional engine policy (trace toggles + price table).
    """

    def __init__(
        self,
        capsule_path: str | os.PathLike[str],
        capsule_id: str,
        *,
        enabled: bool = True,
        policy: Optional[Any] = None,
    ) -> None:
        self.capsule_path = str(Path(capsule_path))
        self.capsule_id = str(capsule_id or "unknown")
        self.errors = 0
        if policy is None:
            try:
                from capsule_engine.policy import load_policy

                policy = load_policy()
            except Exception:
                policy = None
        self._policy = policy
        self._configured = enabled
        if policy is not None and not policy.trace.enabled:
            self._configured = False

    @property
    def enabled(self) -> bool:
        return self._configured

    # -- step lifecycle ------------------------------------------------------ #

    def start(
        self,
        step_name: str,
        *,
        gear: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> StepHandle:
        """Open a step (emits ``step_start`` and writes the telemetry sidecar)."""
        clean_name = str(step_name or "step")[:MAX_STEP_NAME_CHARS]
        clean_gear = str(gear)[:MAX_STEP_NAME_CHARS] if gear else None
        handle = StepHandle(
            self,
            step_id=(step_id or uuid.uuid4().hex[:12]),
            step_name=clean_name,
            gear=clean_gear,
            started_at=now_iso(),
        )
        if self.enabled:
            self._emit(
                {
                    "event": "step_start",
                    "step_id": handle.step_id,
                    "step_name": clean_name,
                    "started_at": handle.started_at,
                    "gear_invoked": clean_gear,
                    "status": STATUS_RUNNING,
                }
            )
            self._write_telemetry(handle)
        return handle

    def step(
        self,
        step_name: str,
        *,
        gear: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> StepHandle:
        """Open a step as a context manager (``with tracer.step(...)``)."""
        return self.start(step_name, gear=gear, step_id=step_id)

    def record_step(
        self,
        step_name: str,
        *,
        gear: Optional[str] = None,
        status: str = STATUS_SUCCESS,
        tokens: Optional[Mapping[str, int]] = None,
        cost_usd: Optional[float] = None,
        artifact_hashes: Optional[Mapping[str, str]] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Record a completed step after the fact (one JSONL line, chained).

        For steps an external process already ran (a pulse, a gear adapter):
        the audit contract is identical to :meth:`step`.
        """
        if not self.enabled:
            return
        started = started_at or now_iso()
        finished = finished_at or now_iso()
        duration = duration_seconds
        if duration is None:
            start_dt, end_dt = _parse_ts(started), _parse_ts(finished)
            duration = (
                max(0.0, (end_dt - start_dt).total_seconds())
                if start_dt is not None and end_dt is not None
                else 0.0
            )
        cleaned = _clean_tokens(tokens)
        if cost_usd is None:
            cost_usd = estimate_cost(
                gear, cleaned, policy=self._policy
            )
        self._emit(
            {
                "event": "step_end",
                "step_id": uuid.uuid4().hex[:12],
                "step_name": str(step_name or "step")[:MAX_STEP_NAME_CHARS],
                "started_at": started,
                "finished_at": finished,
                "duration_seconds": round(float(duration), 3),
                "gear_invoked": str(gear)[:MAX_STEP_NAME_CHARS] if gear else None,
                "tokens_used": cleaned,
                "estimated_cost_usd": round(float(cost_usd or 0.0), 6),
                "status": status if status in STEP_STATUSES else STATUS_FAILED,
                "output_artifact_hashes": dict(artifact_hashes or {}),
            }
        )

    # -- internals ----------------------------------------------------------- #

    def _append_line(self, payload: Mapping[str, Any]) -> bool:
        """Chain + append one line under flock. Never raises."""
        path = trace_path(self.capsule_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o644)
        except OSError as exc:
            self.errors += 1
            logger.warning("trace: cannot open %s: %s", path, exc)
            return False
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            prev = _tail_hash(fd)
            record = dict(payload)
            record["capsule_id"] = self.capsule_id
            record["at"] = now_iso()
            record["prev"] = prev
            record["hash"] = _chain_hash(prev, record)
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            if len(line) > MAX_LINE_BYTES:
                self.errors += 1
                logger.warning("trace: event exceeds %d bytes — refused", MAX_LINE_BYTES)
                return False
            os.write(fd, line)
            os.fsync(fd)
            return True
        except OSError as exc:
            self.errors += 1
            logger.warning("trace: append to %s failed: %s", path, exc)
            return False
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _emit(self, payload: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        self._append_line(payload)

    def _write_telemetry(self, handle: StepHandle) -> None:
        """Atomically replace the live sidecar (best effort)."""
        if not self.enabled:
            return
        path = telemetry_path(self.capsule_path)
        payload = {
            "capsule_id": self.capsule_id,
            "step_id": handle.step_id,
            "step_name": handle.step_name,
            "started_at": handle.started_at,
            "active_gear": handle.gear,
            "progress_percent": handle.progress_percent,
            "heartbeat": now_iso(),
            "pid": os.getpid(),
        }
        tmp = path.with_name(f".{TELEMETRY_NAME}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.errors += 1
            logger.warning("trace: telemetry write failed for %s: %s", path, exc)
            tmp.unlink(missing_ok=True)

    def _clear_telemetry(self, handle: StepHandle) -> None:
        """Remove the sidecar only when it still describes *this* step."""
        if not self.enabled:
            return
        path = telemetry_path(self.capsule_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, Mapping) and raw.get("step_id") not in (None, handle.step_id):
            return  # another step took over the sidecar — leave it alone
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _tail_hash(fd: int) -> str:
    """Chain head, read from the last 256 KiB under the append lock."""
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        return ""
    start = max(0, size - 262144)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, size - start)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    prev = ""
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, Mapping) and isinstance(raw.get("hash"), str):
            prev = str(raw["hash"])
    return prev


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def read_trace(
    capsule_path: str | os.PathLike[str],
    *,
    limit: Optional[int] = None,
) -> TraceRead:
    """Read a capsule's trace (oldest → newest), reporting malformed lines."""
    path = trace_path(capsule_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return TraceRead(events=(), malformed=0, path=str(path))
    if not raw.endswith("\n"):
        raw = raw.rsplit("\n", 1)[0] if "\n" in raw else ""
    events: list[Dict[str, Any]] = []
    malformed = 0
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(decoded, dict):
            malformed += 1
            continue
        events.append(decoded)
    truncated = False
    if limit is not None and limit >= 0 and len(events) > limit:
        events = events[-limit:]
        truncated = True
    return TraceRead(
        events=tuple(events), malformed=malformed, path=str(path), truncated=truncated
    )


def verify_trace(
    capsule_path: str | os.PathLike[str],
) -> TraceChainCheck:
    """Verify the trace's hash chain; names the first broken line."""
    path = trace_path(capsule_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return TraceChainCheck(ok=True, events=0, detail="no trace yet")
    if not raw.endswith("\n"):
        raw = raw.rsplit("\n", 1)[0] if "\n" in raw else ""
    prev = ""
    count = 0
    for index, line in enumerate(
        (line for line in raw.split("\n") if line.strip()), start=1
    ):
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            return TraceChainCheck(
                ok=False,
                events=count,
                first_bad_line=index,
                detail=f"line {index}: invalid JSON ({exc.msg})",
            )
        if not isinstance(decoded, Mapping):
            return TraceChainCheck(
                ok=False, events=count, first_bad_line=index,
                detail=f"line {index}: not a JSON object",
            )
        recorded = decoded.get("hash")
        if not isinstance(recorded, str) or not recorded:
            return TraceChainCheck(
                ok=False, events=count, first_bad_line=index,
                detail=f"line {index}: missing 'hash' (unchained event)",
            )
        if decoded.get("prev") != prev:
            return TraceChainCheck(
                ok=False, events=count, first_bad_line=index,
                detail=f"line {index}: prev pointer does not follow the chain",
            )
        payload = {k: v for k, v in decoded.items() if k != "hash"}
        if _chain_hash(prev, payload) != recorded:
            return TraceChainCheck(
                ok=False, events=count, first_bad_line=index,
                detail=f"line {index}: hash mismatch (tampered or corrupted)",
            )
        prev = recorded
        count += 1
    return TraceChainCheck(ok=True, events=count)


def _read_telemetry(capsule_path: str | os.PathLike[str]) -> Optional[Dict[str, Any]]:
    path = telemetry_path(capsule_path)
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def in_flight(
    capsule_path: str | os.PathLike[str],
    *,
    capsule_id: Optional[str] = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    now: Optional[datetime] = None,
) -> Optional[InFlight]:
    """The live step of a capsule, or ``None`` when nothing is running.

    The telemetry sidecar is trusted only while fresh: a crashed process
    leaves it behind, and after ``stale_after`` seconds it is reported as not
    in flight (the run is dead — the same lease philosophy as the router's
    ``in_progress`` handling).
    """
    raw = _read_telemetry(capsule_path)
    if raw is None:
        return None
    started = _parse_ts(raw.get("started_at"))
    heartbeat = _parse_ts(raw.get("heartbeat")) or started
    if started is None or heartbeat is None:
        return None
    reference = now or datetime.now(timezone.utc)
    age = (reference - heartbeat).total_seconds()
    if age > stale_after:
        return None
    progress = _clamp_percent(raw.get("progress_percent")) or 0.0
    return InFlight(
        capsule_id=str(raw.get("capsule_id") or capsule_id or "unknown"),
        step_id=str(raw.get("step_id") or ""),
        step_name=str(raw.get("step_name") or "step"),
        started_at=str(raw.get("started_at")),
        elapsed_seconds=max(0.0, (reference - started).total_seconds()),
        progress_percent=progress,
        active_gear=str(raw["active_gear"]) if raw.get("active_gear") else None,
    )


def progress_percent(
    manifest: Mapping[str, Any],
    *,
    gate_records: Optional[Sequence[Any]] = None,
    inflight: Optional[InFlight] = None,
) -> float:
    """Overall capsule completion (0–100), derived from observable state.

    Deterministic blend, no guesswork:

    1. the capsule's state maps to a milestone percentage
       (:data:`STATE_PROGRESS`);
    2. inside the band leading to the next milestone, the *observed* progress
       fills the gap — the gate plan's completion when the capsule carries
       gates, otherwise the active step's own ``progress_percent``;
    3. without observations, the milestone itself is reported.
    """
    state = str(manifest.get("state") or "draft")
    base = STATE_PROGRESS.get(state, 50.0)
    try:
        position = _BAND_ORDER.index(state)
    except ValueError:
        return round(base, 1)
    if position + 1 >= len(_BAND_ORDER):
        return 100.0
    next_base = STATE_PROGRESS.get(_BAND_ORDER[position + 1], base)
    band = max(0.0, next_base - base)

    observed = 0.0
    have_observation = False
    if gate_records:
        try:
            total = len(gate_records)
            done = sum(
                1
                for record in gate_records
                if getattr(record, "status", None) in ("passed", "skipped")
            )
            observed = (done / total * 100.0) if total else 0.0
            have_observation = True
        except Exception:  # never let telemetry math break a caller
            have_observation = False
    elif inflight is not None:
        observed = inflight.progress_percent
        have_observation = True

    overall = base + band * (observed / 100.0) if have_observation else base
    return round(max(0.0, min(100.0, overall)), 1)


def summary(
    capsule_path: str | os.PathLike[str],
    *,
    limit: int = 500,
) -> TraceSummary:
    """Roll up the trace: step count, last step, totals for tokens/cost/time."""
    read = read_trace(capsule_path, limit=limit)
    steps = 0
    total_duration = 0.0
    tokens: Dict[str, int] = {}
    cost = 0.0
    last: Optional[Dict[str, Any]] = None
    for event in read.events:
        if event.get("event") != "step_end":
            continue
        steps += 1
        last = dict(event)
        duration = event.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            total_duration += float(duration)
        for key, value in _clean_tokens(event.get("tokens_used")).items():
            tokens[key] = tokens.get(key, 0) + value
        event_cost = event.get("estimated_cost_usd")
        if isinstance(event_cost, (int, float)) and not isinstance(event_cost, bool):
            cost += float(event_cost)
    return TraceSummary(
        steps=steps,
        last_step=last,
        total_duration_seconds=total_duration,
        tokens=tokens,
        estimated_cost_usd=cost,
        malformed=read.malformed,
    )
