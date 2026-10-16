#!/usr/bin/env python3
"""observability.py — metrics, request ids, and structured logs.

The engine is file-based and daemon-free, so its runtime has to be legible from
the outside without adding a metrics SaaS: this module keeps a small,
thread-safe in-process counter set that ``GET /api/metrics`` renders in the
Prometheus text format, and it turns the log stream into JSON on request.

Design constraints:

* **bounded cardinality**: metrics are keyed by method + path + status, and the
  number of distinct series is capped (:data:`MAX_SERIES`). Past the cap new
  series are dropped and counted, never allowed to grow without bound.
* **request ids**: every request gets one (from ``X-Request-ID`` or generated),
  stored in a :class:`~contextvars.ContextVar` so any log line emitted while
  handling it carries the same id — even from a worker thread that copies the
  context.
* **no new dependencies**: standard library only (``json``, ``logging``,
  ``threading``, ``time``, ``uuid``, ``contextvars``).

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

__all__ = [
    "MAX_SERIES",
    "METRICS",
    "JsonFormatter",
    "RequestMetrics",
    "install_logging",
    "new_request_id",
    "render_metrics",
    "request_id_var",
]

#: Cap on distinct request-counter series (labels). Past it, new series are
#: counted in ``agency_http_requests_overflow_total`` instead of being stored.
MAX_SERIES = 500

#: The request id in flight — set by the API middleware, read by the log
#: formatter and by any tooling that wants to correlate a line with a request.
request_id_var: ContextVar[str] = ContextVar("agency_request_id", default="")


def new_request_id() -> str:
    """A short, unambiguous request id (UUID4 hex)."""
    return uuid.uuid4().hex


class RequestMetrics:
    """Thread-safe request counters and latency accumulators.

    Latencies are recorded as a sum + count per path (a Prometheus-style
    average), which is enough to spot a regression without the memory cost of
    a full histogram bucket set.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[Tuple[str, str, int], int] = {}
        self._durations: Dict[str, Tuple[float, int]] = {}
        self._overflow = 0
        self.started_at = time.time()

    def record(self, method: str, path: str, status: int, duration_s: float) -> None:
        method = (method or "").upper()
        status = int(status or 0)
        key = (method, path, status)
        with self._lock:
            if key not in self._counts and len(self._counts) >= MAX_SERIES:
                self._overflow += 1
                return
            self._counts[key] = self._counts.get(key, 0) + 1
            total, count = self._durations.get(path, (0.0, 0))
            self._durations[path] = (total + max(0.0, float(duration_s)), count + 1)

    def snapshot(self) -> Tuple[Dict[Tuple[str, str, int], int], Dict[str, Tuple[float, int]], int]:
        with self._lock:
            return dict(self._counts), dict(self._durations), self._overflow


#: Process-wide metrics registry (the server is a single process by design).
METRICS = RequestMetrics()


def _esc(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _metric_line(name: str, labels: Dict[str, Any], value: Any) -> str:
    if labels:
        rendered = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def render_metrics(extra_gauges: Any = None) -> str:
    """Render the Prometheus exposition text.

    ``extra_gauges`` may be a list of ``(name, help, [(labels, value), ...])``
    tuples (state-derived gauges supplied by the caller), keeping this module
    free of any dependency on the vault.
    """
    counts, durations, overflow = METRICS.snapshot()
    lines: List[str] = [
        "# HELP agency_up Process liveness (always 1 while this endpoint answers).",
        "# TYPE agency_up gauge",
        "agency_up 1",
        "# HELP agency_process_uptime_seconds Seconds since the metrics registry started.",
        "# TYPE agency_process_uptime_seconds gauge",
        f"agency_process_uptime_seconds {round(time.time() - METRICS.started_at, 3)}",
        "# HELP agency_http_requests_total HTTP requests handled, by method/path/status.",
        "# TYPE agency_http_requests_total counter",
    ]
    for (method, path, status), value in sorted(counts.items()):
        lines.append(
            _metric_line(
                "agency_http_requests_total",
                {"method": method, "path": path, "status": status},
                value,
            )
        )
    if overflow:
        lines.append("# HELP agency_http_requests_overflow_total Requests dropped from the series cap.")
        lines.append("# TYPE agency_http_requests_overflow_total counter")
        lines.append(f"agency_http_requests_overflow_total {overflow}")

    lines.append("# HELP agency_http_request_seconds_sum Total request time by path (seconds).")
    lines.append("# TYPE agency_http_request_seconds_sum counter")
    lines.append("# HELP agency_http_request_seconds_count Requests by path.")
    lines.append("# TYPE agency_http_request_seconds_count counter")
    for path, (total, count) in sorted(durations.items()):
        lines.append(_metric_line("agency_http_request_seconds_sum", {"path": path}, round(total, 6)))
        lines.append(_metric_line("agency_http_request_seconds_count", {"path": path}, count))

    for name, help_text, samples in (extra_gauges or []):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lines.append(_metric_line(name, labels, value))

    return "\n".join(lines) + "\n"


class JsonFormatter(logging.Formatter):
    """A one-line JSON log record (opt-in via ``AGENCY_LOG_FORMAT=json``)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get("")
        if rid:
            payload["request_id"] = rid
        for key in ("method", "path", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def install_logging() -> None:
    """Configure the root logger from the environment.

    ``AGENCY_LOG_FORMAT=json`` switches to :class:`JsonFormatter`;
    ``AGENCY_LOG_LEVEL`` sets the level (default INFO). Called once at import
    of the API module, replacing the previous bare ``basicConfig``.
    """
    level_name = os.environ.get("AGENCY_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)
    use_json = os.environ.get("AGENCY_LOG_FORMAT", "").strip().lower() == "json"

    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
