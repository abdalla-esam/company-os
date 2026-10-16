#!/usr/bin/env python3
"""gear_registry.py — Sovereign Gears Registry & Dynamic Tool Discovery.

Central registry for the 16 constitutional gears of the Sovereign Agency OS.
Provides:
- Introspection of all 16 verbs, their contracts, and required inputs/outputs.
- Active endpoint and environment variable mapping (AGENCY_BRIDGE_URL_<VERB>).
- Live self-probing (probe_gear) measuring latency and tool responsiveness.
- Complete operational status reporting for the CLI and Cockpit API.

Zero external dependencies — standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from shared_kernel.paths import ENGINE_ROOT, REPO_ROOT
except Exception:
    REPO_ROOT = Path(__file__).resolve().parents[2]
    ENGINE_ROOT = REPO_ROOT / "engine"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared_kernel.atomic_io import update_json

from capsule_engine.bridge import (
    BRIDGE_ENDPOINT_ENV,
    GEAR_CONTRACTS,
    GEAR_VOCABULARY,
    BridgeConfigError,
    BridgeError,
    BridgeRefusalError,
    GearContract,
    default_bridge,
    env_var_for_gear,
    resolve_endpoint,
    resolve_token,
)

logger = logging.getLogger("capsule_engine.gear_registry")

CONSTITUTIONAL_GEARS: tuple[str, ...] = (
    "text:draft",
    "text:review",
    "image:distill",
    "image:read",
    "data:extract",
    "data:classify",
    "log:summarize",
    "social:publish",
    "message:send",
    "message:interpret",
    "lead:qualify",
    "code:propose",
    "finance:invoice",
    "people:screen",
    "support:triage",
    "docs:review",
)

# Canonical probe payloads matching each contract's input requirements
_raw_b = b"PROBE_IMAGE_BYTE_DATA_OK"
_b64 = base64.b64encode(_raw_b).decode("ascii")
_sha = hashlib.sha256(_raw_b).hexdigest()

SAMPLE_PROBES: dict[str, dict[str, Any]] = {
    "text:draft": {"brief": "Executive update on autonomous system performance."},
    "text:review": {"text": "Architectural overview: the model gateway and sovereign API."},
    "image:distill": {"text": "Enterprise autonomous cloud platform operating at scale."},
    "image:read": {
        "media": {
            "mime": "image/jpeg",
            "base64": _b64,
            "sha256": _sha,
        }
    },
    "data:extract": {"text": "Payment received: $4,500.00 on 2026-10-11", "fields": ["amount"]},
    "data:classify": {"text": "System alert: high database latency", "labels": ["infra", "billing"]},
    "log:summarize": {"entries": ["[INFO] 10:00:00 heartbeat probe ok", "[INFO] 10:01:00 router sweep clean"]},
    "social:publish": {"channel": "linkedin", "text": "Sovereign operations update."},
    "message:send": {"channel": "telegram", "to": "@admin", "text": "Healthcheck probe."},
    "message:interpret": {"channel": "whatsapp", "text": "Can you provide pricing for enterprise?"},
    "lead:qualify": {"text": "Enterprise company requesting deployment", "criteria": ["seat_count_over_50", "budget_ready"]},
    "code:propose": {"request": "Add retry backoff to gateway connection"},
    "finance:invoice": {
        "customer": "Client Enterprise Corp",
        "currency": "USD",
        "line_items": [{"description": "Core Platform License", "amount": 1000}],
    },
    "people:screen": {"text": "Senior Systems Engineer with 8 years Python/Linux", "requirements": ["python_expert", "linux_admin"]},
    "support:triage": {"text": "Webhook endpoint returning 502 Bad Gateway intermittently"},
    "docs:review": {"text": "Sovereign Engineering SOP v2.0", "checklist": ["version", "scope", "security"]},
}


@dataclass(frozen=True)
class GearInfo:
    """Introspection details for one constitutional gear."""
    verb: str
    purpose: str
    env_var: str
    endpoint: str | None
    transport: str  # "http" | "offline" | "unconfigured"
    input_required: list[str]
    output_required: list[str]
    is_configured: bool


def get_gear_info(verb: str) -> GearInfo:
    """Inspect configuration and transport status for one verb."""
    if verb not in GEAR_CONTRACTS:
        raise ValueError(f"Unknown gear verb: {verb!r}")

    contract = GEAR_CONTRACTS[verb]
    env_var = env_var_for_gear(verb)
    endpoint = resolve_endpoint(verb)

    # Determine transport mode
    offline_env = os.environ.get("AGENCY_OFFLINE_MODE", "").strip().lower()
    is_offline = offline_env in ("1", "true", "yes", "on", "offline")

    # Config first (``config/engine.json -> gears``), environment as fallback
    # — the same resolve order the bridge itself uses.
    token = resolve_token(verb)

    if endpoint and token:
        transport = "http"
        is_configured = True
    elif is_offline:
        transport = "offline"
        is_configured = True
    elif endpoint:
        transport = "http (missing token)"
        is_configured = False
    else:
        transport = "unconfigured"
        is_configured = False

    return GearInfo(
        verb=verb,
        purpose=contract.purpose,
        env_var=env_var,
        endpoint=endpoint,
        transport=transport,
        input_required=list(contract.input_required),
        output_required=list(contract.output_required),
        is_configured=is_configured,
    )


def _probe_cache_path() -> Path:
    """Where the last probe result per gear is remembered.

    A cheap JSON sidecar (not a database): the health endpoint reports when
    each gear was last probed so the cockpit can tell "temporarily down" from
    "never tested". ``AGENCY_GEAR_PROBE_CACHE`` overrides the location.
    """
    for var in ("AGENCY_GEAR_PROBE_CACHE", "AGENCY_GEAR_PROBES_FILE"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override)
    return REPO_ROOT / "data" / "index" / "gear_probes.json"


probe_cache_path = _probe_cache_path


def _load_probe_cache() -> dict[str, Any]:
    try:
        raw = json.loads(_probe_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_probe_record(verb: str, record: dict[str, Any]) -> None:
    """Best-effort persist of one probe result; never fails a probe."""
    path = _probe_cache_path()

    def _mutate(existing: Any) -> Any:
        cache = dict(existing) if isinstance(existing, dict) else {}
        cache[verb] = record
        return cache

    try:
        update_json(
            path,
            _mutate,
            default={},
            indent=2,
            ensure_ascii=False,
            trailing_newline=False,
        )
    except Exception:
        logger.debug("gear_registry: could not persist probe cache at %s", path)


def probe_gear(verb: str, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Execute a live probe call against a gear and measure response latency.

    The result is stamped with ``probed_at`` and remembered in the probe cache
    so ``GET /api/engine/gears/health`` can report recency per gear.
    """
    result = _probe_gear_impl(verb, timeout_seconds)
    result.setdefault("probed_at", datetime.now(timezone.utc).isoformat())
    if verb in GEAR_CONTRACTS:
        _save_probe_record(
            verb,
            {
                "probed_at": result.get("probed_at"),
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "latency_ms": result.get("latency_ms"),
                "transport": result.get("transport"),
            },
        )
    return result


def _probe_gear_impl(verb: str, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """The probe itself; :func:`probe_gear` adds recency + caching."""
    if verb not in GEAR_CONTRACTS:
        return {
            "verb": verb,
            "status": "error",
            "ok": False,
            "error": f"Unknown gear verb: {verb!r}",
            "latency_ms": 0.0,
        }

    info = get_gear_info(verb)
    payload = SAMPLE_PROBES.get(verb, {})

    # A probe is a real, governed gear call: it needs the same correlation a
    # capsule would carry. A synthetic, lawful envelope (a probe never touches
    # a real capsule) keeps fake verbs and unknown states out, and routes the
    # spend through the default branch so the probe is metered like any call.
    probe_capsule = {
        "id": f"probe-{verb.replace(':', '-')}",
        "state": "pending_qa",
        "domain": "core",
    }
    probe_governance = {"domain": "core", "autonomy_level": "L0"}

    start = time.perf_counter()
    try:
        bridge = default_bridge(verb)
        ans = bridge.invoke(
            payload,
            gear=verb,
            capsule=probe_capsule,
            governance=probe_governance,
            deadline_ms=int(timeout_seconds * 1000),
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        ans.require_ok()
        return {
            "verb": verb,
            "status": "ok",
            "ok": True,
            "latency_ms": elapsed_ms,
            "tool": ans.tool,
            "transport": info.transport,
            "output_keys": list(ans.output.keys()) if isinstance(ans.output, dict) else [],
            "output_sha256": ans.output_sha256,
            "request_id": ans.request_id,
        }
    except BridgeRefusalError as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "verb": verb,
            "status": "refused",
            "ok": False,
            "latency_ms": elapsed_ms,
            "transport": info.transport,
            "code": exc.code,
            "detail": exc.detail,
        }
    except BridgeConfigError as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "verb": verb,
            "status": "unconfigured",
            "ok": False,
            "latency_ms": elapsed_ms,
            "transport": info.transport,
            "error": str(exc),
        }
    except BridgeError as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "verb": verb,
            "status": "error",
            "ok": False,
            "latency_ms": elapsed_ms,
            "transport": info.transport,
            "error": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "verb": verb,
            "status": "exception",
            "ok": False,
            "latency_ms": elapsed_ms,
            "transport": info.transport,
            "error": str(exc),
        }


def registry_summary() -> dict[str, Any]:
    """Complete summary of all 16 constitutional gears, endpoints, and transports."""
    gears_data = []
    http_count = 0
    offline_count = 0
    unconfigured_count = 0

    probes = _load_probe_cache()
    probed_count = 0

    for verb in CONSTITUTIONAL_GEARS:
        info = get_gear_info(verb)
        row = asdict(info)
        last = probes.get(verb) if isinstance(probes.get(verb), dict) else {}
        row["last_probe_at"] = last.get("probed_at")
        row["last_probe_ok"] = last.get("ok")
        row["last_probe_status"] = last.get("status")
        row["last_probe_latency_ms"] = last.get("latency_ms")
        if row["last_probe_at"]:
            probed_count += 1
        gears_data.append(row)
        if info.transport == "http":
            http_count += 1
        elif info.transport == "offline":
            offline_count += 1
        else:
            unconfigured_count += 1

    return {
        "total_gears": len(CONSTITUTIONAL_GEARS),
        "http_gears": http_count,
        "offline_gears": offline_count,
        "unconfigured_gears": unconfigured_count,
        "probed_gears": probed_count,
        "is_offline_mode": os.environ.get("AGENCY_OFFLINE_MODE", "").strip().lower() in ("1", "true", "yes", "on"),
        "gears": gears_data,
    }
