#!/usr/bin/env python3
"""spend_control.py — the money gate: authorize before, record after.

One choke point for every paid call in the system (LLM tokens, image
generation, tools, subscriptions):

- :func:`authorize` runs BEFORE a request leaves the machine — the gateway
  calls it per HTTP attempt, so retries are capped too. It fails closed as a
  :class:`capsule_engine.gateway.SpendCapExceeded` (a ``GatewayError``) when
  the spend config is missing/unreadable, spend is disabled, the kill switch
  is on, the domain is unknown, the ledger is unreadable, or a cap would be
  crossed. Refusals are recorded too (cost 0), best effort.
- :func:`record` settles the reservation after the attempt
  (``succeeded`` / ``failed`` / ``refused``). Failed and timed-out calls
  still count at their estimate — a provider may bill an attempt this side
  never saw the answer of.
- :func:`record_subscription` books fixed recurring costs (VPS, Directus,
  n8n, hosting) into the same ledger. Active subscriptions from
  ``config/engine.json -> spend.subscriptions`` **and** the newest ledger
  record per service both feed the daily/monthly totals.
- :func:`blocked_reason` / :func:`status_snapshot` / :func:`set_kill_switch`
  back the beat pre-flight and the ``/api/finance/*`` endpoints.

The ledger is an append-only JSONL file (``data/finance/usage_ledger.jsonl``
by default; ``AGENCY_SPEND_LEDGER`` overrides) written through
``shared_kernel.atomic_io.append_jsonl_line`` (O_APPEND + exclusive flock +
fsync): concurrent processes never interleave or lose lines. Every event
carries ``category`` (``model`` | ``image`` | ``subscription`` | ``tool``),
a ``provider``, a ``service_name`` and the responsible ``domain``.

Accounting model:

- ``model`` / ``image`` / ``tool`` events: the full cost lands on the UTC
  day and month they settle (pending reservations count at their estimate,
  refused events count nothing);
- ``subscription`` events and config subscriptions: amortized — a monthly
  $X costs ``X / 30.4`` per day and ``X`` per month, keyed by
  ``provider/service_name`` with the newest record winning, so repeated
  monthly payments never double-count;
- prices come from ONE model-keyed table (``spend.model_prices`` /
  ``spend.image_costs`` in ``config/engine.json``); unknown models get a
  conservative ``unpriced_estimate_usd`` with ``priced:false`` and still
  count toward the call cap.

Fail-closed philosophy matches the rest of the kernel: any doubt (missing
section, corrupt JSON, unreadable ledger, unknown domain) refuses the call
instead of spending unaccounted.

Environment: ``AGENCY_SPEND_LEDGER`` (ledger path), ``AGENCY_SPEND_CONFIG``
(engine config path; defaults to the policy's ``AGENCY_ENGINE_CONFIG`` /
``shared_kernel.paths.ENGINE_CONFIG_PATH``), ``AGENCY_SPEND_KILL`` (``1``
forces the kill switch on regardless of the file).

Standard library only plus ``shared_kernel.atomic_io``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from capsule_engine.gateway import SpendCapExceeded
from capsule_engine.manifest import DEFAULT_REVISION_CAP
from shared_kernel.atomic_io import append_jsonl_line, update_json

logger = logging.getLogger("capsule_engine.spend_control")

__all__ = [
    "CATEGORY_IMAGE",
    "CATEGORY_MODEL",
    "CATEGORY_PUBLISH",
    "CATEGORY_SUBSCRIPTION",
    "CATEGORY_TOOL",
    "Reservation",
    "SpendCapExceeded",
    "authorize",
    "blocked_reason",
    "config_path",
    "kill_active",
    "ledger_path",
    "load_config",
    "record",
    "record_subscription",
    "reset_caches",
    "revision_cap",
    "set_kill_switch",
    "status_snapshot",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

CATEGORY_MODEL = "model"            # LLM / chat-completion tokens
CATEGORY_IMAGE = "image"            # image generation
CATEGORY_SUBSCRIPTION = "subscription"  # fixed recurring cost (VPS, Directus, n8n…)
CATEGORY_TOOL = "tool"              # metered tool call (bridge gear, webhook…)
CATEGORY_PUBLISH = "publish"        # publishing delivery (recorded, audited, zero cost)

CATEGORIES: frozenset[str] = frozenset(
    {
        CATEGORY_MODEL,
        CATEGORY_IMAGE,
        CATEGORY_SUBSCRIPTION,
        CATEGORY_TOOL,
        CATEGORY_PUBLISH,
    }
)

INTERVAL_DAILY = "daily"
INTERVAL_WEEKLY = "weekly"
INTERVAL_MONTHLY = "monthly"
INTERVAL_YEARLY = "yearly"
INTERVALS: frozenset[str] = frozenset(
    {INTERVAL_DAILY, INTERVAL_WEEKLY, INTERVAL_MONTHLY, INTERVAL_YEARLY}
)

#: Average days per month used to amortize recurring costs (365.25 / 12).
DAYS_PER_MONTH = 30.4

LEDGER_ENV = "AGENCY_SPEND_LEDGER"
CONFIG_ENV = "AGENCY_SPEND_CONFIG"
KILL_ENV = "AGENCY_SPEND_KILL"

#: Config + ledger caches are refreshed at most this often (seconds). This is
#: also the kill-switch staleness bound for file-based flips.
CACHE_TTL_SECONDS = 1.0

EVENT_RESERVED = "reserved"
EVENT_SETTLED = "settled"
EVENT_REFUSED = "refused"
EVENT_SUBSCRIPTION = "subscription"
EVENTS: frozenset[str] = frozenset(
    {EVENT_RESERVED, EVENT_SETTLED, EVENT_REFUSED, EVENT_SUBSCRIPTION}
)

STATUS_RESERVED = "reserved"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"
STATUS_ACTIVE = "active"
SETTLED_STATUSES: frozenset[str] = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

#: A domain is a branch name: lowercase identifier, bounded. Anything else
#: cannot be attributed to a per-domain cap and therefore fails closed.
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_DEFAULT_UNPRICED_ESTIMATE_USD = 0.02
_DEFAULT_ESTIMATE_PROMPT_TOKENS = 2500
_DEFAULT_ESTIMATE_COMPLETION_TOKENS = 1000


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Subscription:
    """One recurring obligation: amortized into daily and monthly totals."""

    name: str
    provider: str
    domain: str
    interval: str
    monthly_usd: float
    source: str  # "config" | "ledger"

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.name}"


@dataclass(frozen=True)
class SpendConfig:
    """The validated ``config/engine.json -> spend`` section."""

    enabled: bool
    kill: bool
    daily_usd: float
    monthly_usd: float
    per_domain_daily_usd: float
    max_calls_per_day: int
    max_revisions_per_capsule: int
    default_domain: str
    known_domains: tuple[str, ...]
    unpriced_estimate_usd: float
    estimate_prompt_tokens: int
    estimate_completion_tokens: int
    model_prices: Mapping[str, tuple[float, float]]  # model -> (prompt/completion per 1k)
    image_costs: Mapping[str, float]                # model/provider -> flat USD
    subscriptions: tuple[Subscription, ...]
    path: Path


def config_path() -> Path:
    """Where the spend config lives.

    ``AGENCY_SPEND_CONFIG`` wins, then ``AGENCY_ENGINE_CONFIG`` (the policy's
    own override — read at *call* time so tests and operators can redirect it
    without re-importing), then the canonical ``ENGINE_CONFIG_PATH``.
    """
    for env in (CONFIG_ENV, "AGENCY_ENGINE_CONFIG"):
        value = os.environ.get(env, "").strip()
        if value:
            return Path(value)
    try:
        from shared_kernel.paths import ENGINE_CONFIG_PATH

        return Path(ENGINE_CONFIG_PATH)
    except Exception:  # standalone execution (engine/ on sys.path only)
        return Path(__file__).resolve().parents[2] / "config" / "engine.json"


def _as_number(value: object, what: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpendCapExceeded(f"spend config invalid: {what} must be a number, got {value!r}")
    result = float(value)
    if result < 0 or (result == 0.0 and not allow_zero):
        raise SpendCapExceeded(f"spend config invalid: {what} must be > 0, got {value!r}")
    return result


def _as_int(value: object, what: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SpendCapExceeded(
            f"spend config invalid: {what} must be an integer >= {minimum}, got {value!r}"
        )
    return value


def _as_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise SpendCapExceeded(f"spend config invalid: {what} must be a boolean, got {value!r}")
    return value


def _validate_domain(name: object, what: str, known: Sequence[str]) -> str:
    if not isinstance(name, str) or not _DOMAIN_RE.match(name.strip()):
        raise SpendCapExceeded(
            f"spend config invalid: {what} must be a lowercase branch name, got {name!r}"
        )
    clean = name.strip()
    if known and clean not in known:
        raise SpendCapExceeded(
            f"spend config invalid: {what} {clean!r} is not in known_domains {list(known)!r}"
        )
    return clean


def _parse_price_row(raw: object, model: str) -> tuple[float, float]:
    if not isinstance(raw, Mapping):
        raise SpendCapExceeded(
            f"spend config invalid: model_prices[{model!r}] must be an object, got {raw!r}"
        )
    prompt = _as_number(raw.get("prompt_per_1k", 0.0), f"model_prices[{model!r}].prompt_per_1k")
    completion = _as_number(
        raw.get("completion_per_1k", 0.0), f"model_prices[{model!r}].completion_per_1k"
    )
    return prompt, completion


def _parse_subscription(raw: object, index: int, default_domain: str, known: Sequence[str]) -> Subscription:
    if not isinstance(raw, Mapping):
        raise SpendCapExceeded(
            f"spend config invalid: subscriptions[{index}] must be an object, got {raw!r}"
        )
    name = str(raw.get("name") or "").strip()
    if not name:
        raise SpendCapExceeded(f"spend config invalid: subscriptions[{index}].name is missing")
    provider = str(raw.get("provider") or "").strip()
    interval = str(raw.get("interval") or INTERVAL_MONTHLY).strip().lower()
    if interval not in INTERVALS:
        raise SpendCapExceeded(
            f"spend config invalid: subscriptions[{index}].interval must be one of "
            f"{sorted(INTERVALS)}, got {interval!r}"
        )
    domain = _validate_domain(raw.get("domain") or default_domain, f"subscriptions[{index}].domain", known)
    if "monthly_usd" in raw:
        monthly = _as_number(raw.get("monthly_usd"), f"subscriptions[{index}].monthly_usd")
        interval = INTERVAL_MONTHLY
    else:
        cost = _as_number(raw.get("cost_usd"), f"subscriptions[{index}].cost_usd")
        monthly = _monthly_equivalent(cost, interval)
    return Subscription(
        name=name, provider=provider, domain=domain, interval=interval,
        monthly_usd=round(monthly, 6), source="config",
    )


def _monthly_equivalent(cost_usd: float, interval: str) -> float:
    if interval == INTERVAL_DAILY:
        return cost_usd * DAYS_PER_MONTH
    if interval == INTERVAL_WEEKLY:
        return cost_usd * (DAYS_PER_MONTH / 7.0)
    if interval == INTERVAL_YEARLY:
        return cost_usd / 12.0
    return cost_usd


def _parse_spend(raw: object, path: Path) -> SpendConfig:
    if not isinstance(raw, Mapping):
        raise SpendCapExceeded(
            f"spend config missing or invalid in {path}: the 'spend' section is required "
            "(fail closed: no spend config means no spending)"
        )
    enabled = _as_bool(raw.get("enabled"), f"{path}: spend.enabled")
    kill = _as_bool(raw.get("kill", False), f"{path}: spend.kill")
    daily = _as_number(raw.get("daily_usd"), f"{path}: spend.daily_usd")
    monthly = _as_number(raw.get("monthly_usd"), f"{path}: spend.monthly_usd")
    per_domain = _as_number(
        raw.get("per_domain_daily_usd"), f"{path}: spend.per_domain_daily_usd"
    )
    max_calls = _as_int(raw.get("max_calls_per_day"), f"{path}: spend.max_calls_per_day", minimum=1)
    max_revisions = _as_int(
        raw.get("max_revisions_per_capsule", DEFAULT_REVISION_CAP),
        f"{path}: spend.max_revisions_per_capsule",
        minimum=0,
    )

    known_raw = raw.get("known_domains")
    if not isinstance(known_raw, list) or not known_raw:
        raise SpendCapExceeded(f"{path}: spend.known_domains must be a non-empty list")
    known: list[str] = []
    for entry in known_raw:
        known.append(_validate_domain(entry, f"{path}: spend.known_domains entry", ()))
    default_domain = _validate_domain(
        raw.get("default_domain"), f"{path}: spend.default_domain", tuple(known)
    )

    unpriced = _as_number(
        raw.get("unpriced_estimate_usd", _DEFAULT_UNPRICED_ESTIMATE_USD),
        f"{path}: spend.unpriced_estimate_usd",
    )
    estimate_tokens = raw.get("estimate_tokens") or {}
    if not isinstance(estimate_tokens, Mapping):
        raise SpendCapExceeded(f"{path}: spend.estimate_tokens must be an object")
    est_prompt = _as_int(
        estimate_tokens.get("prompt", _DEFAULT_ESTIMATE_PROMPT_TOKENS),
        f"{path}: spend.estimate_tokens.prompt", minimum=0,
    )
    est_completion = _as_int(
        estimate_tokens.get("completion", _DEFAULT_ESTIMATE_COMPLETION_TOKENS),
        f"{path}: spend.estimate_tokens.completion", minimum=0,
    )

    prices_raw = raw.get("model_prices") or {}
    if not isinstance(prices_raw, Mapping):
        raise SpendCapExceeded(f"{path}: spend.model_prices must be an object")
    model_prices = {
        str(model): _parse_price_row(row, str(model)) for model, row in prices_raw.items()
    }

    images_raw = raw.get("image_costs") or {}
    if not isinstance(images_raw, Mapping):
        raise SpendCapExceeded(f"{path}: spend.image_costs must be an object")
    image_costs = {
        str(model): _as_number(cost, f"{path}: spend.image_costs[{model!r}]")
        for model, cost in images_raw.items()
    }

    subs_raw = raw.get("subscriptions") or []
    if not isinstance(subs_raw, list):
        raise SpendCapExceeded(f"{path}: spend.subscriptions must be a list")
    subscriptions = tuple(
        _parse_subscription(entry, index, default_domain, tuple(known))
        for index, entry in enumerate(subs_raw)
        if not (isinstance(entry, Mapping) and entry.get("active") is False)
    )

    return SpendConfig(
        enabled=enabled,
        kill=kill,
        daily_usd=daily,
        monthly_usd=monthly,
        per_domain_daily_usd=per_domain,
        max_calls_per_day=max_calls,
        max_revisions_per_capsule=max_revisions,
        default_domain=default_domain,
        known_domains=tuple(known),
        unpriced_estimate_usd=unpriced,
        estimate_prompt_tokens=est_prompt,
        estimate_completion_tokens=est_completion,
        model_prices=model_prices,
        image_costs=image_costs,
        subscriptions=subscriptions,
        path=path,
    )


_CONFIG_CACHE: dict[str, Any] = {"key": None, "loaded_at": 0.0, "config": None}


def load_config(*, force: bool = False) -> SpendConfig:
    """Load and validate the spend config. Raises SpendCapExceeded when unusable.

    Cached for at most :data:`CACHE_TTL_SECONDS` and re-keyed on the file's
    (path, mtime, size), so a kill switch or cap edit takes effect within a
    second without re-reading the file on every call.
    """
    path = config_path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise SpendCapExceeded(f"spend config unreadable: {path}: {exc}") from exc
    now = time.monotonic()
    cached: SpendConfig | None = _CONFIG_CACHE.get("config")
    if (
        not force
        and cached is not None
        and _CONFIG_CACHE.get("key") == key
        and now - float(_CONFIG_CACHE.get("loaded_at") or 0.0) <= CACHE_TTL_SECONDS
    ):
        return cached
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpendCapExceeded(f"spend config unreadable: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpendCapExceeded(f"spend config unreadable: {path}: not a JSON object")
    config = _parse_spend(raw.get("spend"), path)
    _CONFIG_CACHE.update({"key": key, "loaded_at": now, "config": config})
    return config


def reset_caches() -> None:
    """Drop the config and ledger caches (tests; after out-of-band edits)."""
    _CONFIG_CACHE.clear()
    _CONFIG_CACHE.update({"key": None, "loaded_at": 0.0, "config": None})
    _TOTALS_CACHE.clear()


def _env_kill_active() -> bool:
    return os.environ.get(KILL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def kill_active(config: SpendConfig | None = None) -> bool:
    """True when paid calls must stop now (env flag, file flag, or unreadable config)."""
    if _env_kill_active():
        return True
    if config is None:
        try:
            config = load_config()
        except SpendCapExceeded:
            return True  # unusable config is itself a stop signal
    return bool(config.kill)


def blocked_reason() -> str | None:
    """Why paid work must not *start* right now, or None if it may.

    This is the cheap pre-flight (beat / dispatcher) gate: kill switch, spend
    disabled, or an unusable config. Per-call caps are still enforced by
    :func:`authorize`.
    """
    try:
        config = load_config()
    except SpendCapExceeded as exc:
        return str(exc)
    if kill_active(config):
        return "spend kill switch is ON"
    if not config.enabled:
        return "spend control is disabled (spend.enabled=false)"
    return None


def revision_cap() -> int:
    """Per-capsule revision cap (``spend.max_revisions_per_capsule``).

    The loop breaker: a capsule whose ``revision_count`` would exceed this
    value is escalated to a human position instead of being dispatched to a
    worker again (see ``capsule_engine.manifest.transition``).

    Fail-safe, deliberately unlike the money gates: an unusable config
    returns the default (:data:`DEFAULT_REVISION_CAP`) rather than raising —
    a broken file must never *lift* the cap, and a transition must never
    crash on config trouble.
    """
    try:
        return load_config().max_revisions_per_capsule
    except SpendCapExceeded:
        return DEFAULT_REVISION_CAP


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


def ledger_path() -> Path:
    """The append-only usage ledger: ``AGENCY_SPEND_LEDGER`` > ``AGENCY_FINANCE_DIR`` > data/finance/..."""
    for var in (LEDGER_ENV, "AGENCY_FINANCE_LEDGER_FILE", "AGENCY_LEDGER_FILE"):
        env = os.environ.get(var, "").strip()
        if env:
            return Path(env)
    finance_dir = os.environ.get("AGENCY_FINANCE_DIR", "").strip()
    if finance_dir:
        return Path(finance_dir) / "usage_ledger.jsonl"
    try:
        from shared_kernel.paths import SPEND_LEDGER_PATH

        return Path(SPEND_LEDGER_PATH)
    except Exception:  # standalone execution
        return (
            Path(__file__).resolve().parents[2] / "data" / "finance" / "usage_ledger.jsonl"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Reservation:
    """An authorized (reserved) spend, settled after the attempt."""

    id: str
    ts: str
    category: str
    provider: str
    service_name: str
    domain: str
    priced: bool
    estimate_usd: float
    rates: tuple[float, float] | None = None  # (prompt/completion per 1k), priced LLM only
    context: Mapping[str, Any] = field(default_factory=dict)


def _totals_invalidate() -> None:
    _TOTALS_CACHE.clear()


def _append_event(payload: Mapping[str, Any]) -> None:
    """Append one ledger event; raises SpendCapExceeded when the ledger refuses."""
    path = ledger_path()
    try:
        append_jsonl_line(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except OSError as exc:
        raise SpendCapExceeded(
            f"usage ledger unwritable: {path}: {exc} (fail closed before spend)"
        ) from exc
    _totals_invalidate()


def _try_append_refusal(payload: Mapping[str, Any]) -> None:
    """Best-effort refusal event: never mask the refusal itself with an I/O error."""
    try:
        _append_event(payload)
    except SpendCapExceeded as exc:
        logger.warning("spend: could not record refusal: %s", exc)


def _refuse(reason: str, **fields: Any) -> "SpendCapExceeded":
    """Record a refusal (best effort) and return the error to raise."""
    payload = {
        "id": uuid.uuid4().hex,
        "ts": _now_iso(),
        "event": EVENT_REFUSED,
        "status": STATUS_REFUSED,
        "reason": reason[:300],
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    _try_append_refusal(payload)
    return SpendCapExceeded(reason)


@dataclass
class _Totals:
    spent_today_usd: float = 0.0
    spent_month_usd: float = 0.0
    by_domain_today: dict[str, float] = field(default_factory=dict)
    calls_today: int = 0
    tool_calls_today: int = 0
    publish_calls_today: int = 0
    subscription_monthly_usd: float = 0.0
    subscription_daily_usd: float = 0.0
    by_domain_subscription_daily: dict[str, float] = field(default_factory=dict)
    events: int = 0
    refused_events: int = 0
    unpriced_events: int = 0


_TOTALS_CACHE: dict[str, Any] = {"path": None, "totals": None, "loaded_at": 0.0}


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SpendCapExceeded(f"usage ledger unreadable: {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpendCapExceeded(
                f"usage ledger corrupt at {path}:{lineno}: {exc} (fail closed)"
            ) from exc
        if not isinstance(obj, dict):
            raise SpendCapExceeded(
                f"usage ledger corrupt at {path}:{lineno}: event is not an object (fail closed)"
            )
        events.append(obj)
    return events


def _entry_cost(entry: Mapping[str, Any], lineno: int) -> float:
    status = entry.get("status")
    raw = entry.get("cost_usd") if status in SETTLED_STATUSES else entry.get("estimate_usd")
    if raw is None:
        raw = 0.0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise SpendCapExceeded(
            f"usage ledger corrupt: entry {lineno} carries an invalid cost {raw!r} (fail closed)"
        )
    return float(raw)


def _ledger_subscriptions(events: list[dict[str, Any]]) -> dict[str, Subscription]:
    """Newest ledger subscription record per ``provider/name`` (ts max wins)."""
    best: dict[str, tuple[str, Subscription]] = {}
    for index, event in enumerate(events):
        if event.get("event") != EVENT_SUBSCRIPTION:
            continue
        name = str(event.get("service_name") or "").strip()
        if not name:
            raise SpendCapExceeded(f"usage ledger corrupt: subscription event {index} has no service_name")
        interval = str(event.get("interval") or INTERVAL_MONTHLY).strip().lower()
        if interval not in INTERVALS:
            raise SpendCapExceeded(
                f"usage ledger corrupt: subscription event {index} has interval {interval!r}"
            )
        # A subscription event always carries the booked cost (status is
        # "active", not a settle) — read cost_usd directly.
        cost = event.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise SpendCapExceeded(
                f"usage ledger corrupt: subscription event {index} has invalid cost_usd {cost!r}"
            )
        cost = float(cost)
        sub = Subscription(
            name=name,
            provider=str(event.get("provider") or ""),
            domain=str(event.get("domain") or ""),
            interval=interval,
            monthly_usd=round(_monthly_equivalent(cost, interval), 6),
            source="ledger",
        )
        ts = str(event.get("ts") or "")
        prior = best.get(sub.key)
        if prior is None or ts >= prior[0]:
            best[sub.key] = (ts, sub)
    return {key: value[1] for key, value in best.items()}


def _fold(events: list[dict[str, Any]]) -> _Totals:
    """Fold ledger events into today/month/domain totals.

    Reserved+settled pairs merge by id; the settle wins. A reservation that
    never settled (a crash mid-call) still counts at its estimate — the
    provider may have billed it. Refused events count nothing. Subscription
    events are amortized separately (:func:`_apply_subscriptions`).
    """
    entries: dict[str, dict[str, Any]] = {}
    totals = _Totals(events=len(events))
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    for index, event in enumerate(events):
        event_name = event.get("event")
        if event_name not in EVENTS:
            raise SpendCapExceeded(
                f"usage ledger corrupt: event {index} has unknown event {event_name!r} (fail closed)"
            )
        category = event.get("category") or CATEGORY_MODEL
        if category not in CATEGORIES:
            raise SpendCapExceeded(
                f"usage ledger corrupt: event {index} has unknown category {category!r} (fail closed)"
            )
        if event_name == EVENT_SUBSCRIPTION:
            if not str(event.get("id") or ""):
                raise SpendCapExceeded(f"usage ledger corrupt: subscription event {index} has no id")
            continue

        entry_id = str(event.get("id") or "")
        if not entry_id:
            raise SpendCapExceeded(f"usage ledger corrupt: event {index} has no id (fail closed)")
        if event_name == EVENT_RESERVED:
            entries[entry_id] = dict(event)
        else:  # settled / refused
            merged = entries.get(entry_id, {})
            merged.update(event)
            entries[entry_id] = merged

    # dynamic spend (model / image / tool / publish)
    for entry in entries.values():
        status = entry.get("status") or STATUS_RESERVED
        if status == STATUS_REFUSED:
            totals.refused_events += 1
            continue
        if not (status in SETTLED_STATUSES or status == STATUS_RESERVED):
            raise SpendCapExceeded(
                f"usage ledger corrupt: entry has unknown status {status!r} (fail closed)"
            )
        if not entry.get("priced", True):
            totals.unpriced_events += 1
        cost = _entry_cost(entry, 0)
        ts = str(entry.get("ts") or "")
        domain = str(entry.get("domain") or "")
        category = entry.get("category") or CATEGORY_MODEL

        # publish events do NOT count against daily/monthly spend caps or model/image calls
        if category == CATEGORY_PUBLISH:
            if ts[:10] == today:
                totals.publish_calls_today += 1
            continue

        if ts[:7] == month:
            totals.spent_month_usd += cost
        if ts[:10] == today:
            totals.spent_today_usd += cost
            if category == CATEGORY_TOOL:
                totals.tool_calls_today += 1
            else:
                totals.calls_today += 1
            totals.by_domain_today[domain] = totals.by_domain_today.get(domain, 0.0) + cost

    totals.spent_today_usd = round(totals.spent_today_usd, 6)
    totals.spent_month_usd = round(totals.spent_month_usd, 6)
    totals.by_domain_today = {k: round(v, 6) for k, v in totals.by_domain_today.items()}
    return totals


def _apply_subscriptions(totals: _Totals, config: SpendConfig, ledger_subs: dict[str, Subscription]) -> None:
    """Fold config + ledger subscriptions into the amortized totals."""
    merged: dict[str, Subscription] = {sub.key: sub for sub in config.subscriptions}
    merged.update(ledger_subs)  # newest ledger record per service wins
    month_total = 0.0
    by_domain_daily: dict[str, float] = {}
    for sub in merged.values():
        month_total += sub.monthly_usd
        daily = sub.monthly_usd / DAYS_PER_MONTH
        by_domain_daily[sub.domain] = by_domain_daily.get(sub.domain, 0.0) + daily
    totals.subscription_monthly_usd = round(month_total, 6)
    totals.subscription_daily_usd = round(month_total / DAYS_PER_MONTH, 6)
    totals.by_domain_subscription_daily = {k: round(v, 6) for k, v in by_domain_daily.items()}
    totals.spent_month_usd = round(totals.spent_month_usd + month_total, 6)
    totals.spent_today_usd = round(totals.spent_today_usd + totals.subscription_daily_usd, 6)
    for domain, daily in by_domain_daily.items():
        totals.by_domain_today[domain] = round(totals.by_domain_today.get(domain, 0.0) + daily, 6)


def _totals() -> _Totals:
    """Current totals (cached ≤1s; invalidated by every append in this process)."""
    path = ledger_path()
    now = time.monotonic()
    cached: _Totals | None = _TOTALS_CACHE.get("totals")
    if (
        cached is not None
        and _TOTALS_CACHE.get("path") == str(path)
        and now - float(_TOTALS_CACHE.get("loaded_at") or 0.0) <= CACHE_TTL_SECONDS
    ):
        return cached
    config = load_config()
    events = _read_events(path)
    totals = _fold(events)
    _apply_subscriptions(totals, config, _ledger_subscriptions(events))
    _TOTALS_CACHE.update({"path": str(path), "totals": totals, "loaded_at": now})
    return totals


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #


def _estimate(
    category: str, model: str, config: SpendConfig, explicit: float | None
) -> tuple[float, bool, tuple[float, float] | None]:
    """``(estimate_usd, priced, rates)`` for one call. Unknown prices are conservative."""
    if category == CATEGORY_PUBLISH:
        return 0.0, True, None
    if category == CATEGORY_TOOL and model == "directus-api":
        return 0.0, True, None
    if explicit is not None:
        return (
            _as_number(explicit, "estimate_usd"),
            True,
            config.model_prices.get(model) if category == CATEGORY_MODEL else None,
        )
    if category == CATEGORY_IMAGE or category == CATEGORY_TOOL:
        flat = config.image_costs.get(model)
        if flat is None:
            if model == "directus-api":
                return 0.0, True, None
            return config.unpriced_estimate_usd, False, None
        return float(flat), True, None
    row = config.model_prices.get(model)
    if row is None:
        return config.unpriced_estimate_usd, False, None
    prompt_rate, completion_rate = row
    usd = (
        config.estimate_prompt_tokens / 1000.0 * prompt_rate
        + config.estimate_completion_tokens / 1000.0 * completion_rate
    )
    return round(usd, 6), True, (prompt_rate, completion_rate)


def _resolve_domain(domain: str | None, config: SpendConfig) -> str:
    if domain is None or not str(domain).strip():
        return config.default_domain
    clean = str(domain).strip()
    if not _DOMAIN_RE.match(clean) or clean not in config.known_domains:
        raise SpendCapExceeded(
            f"unknown domain {clean!r} — refusing paid call (per-domain caps cannot be enforced)"
        )
    return clean


# --------------------------------------------------------------------------- #
# the public gate
# --------------------------------------------------------------------------- #


def authorize(
    *,
    model: str,
    category: str = CATEGORY_MODEL,
    provider: str = "",
    domain: str | None = None,
    estimate_usd: float | None = None,
    context: Mapping[str, Any] | None = None,
) -> Reservation:
    """Reserve budget for one paid attempt, BEFORE the request. Fail closed.

    Raises:
        SpendCapExceeded: spend disabled, kill switch on, unknown category or
            domain, unusable config or ledger, or any cap would be crossed.
    """
    config = load_config()
    if not config.enabled:
        raise _refuse(
            "spend control is disabled (spend.enabled=false) — refusing paid call",
            category=category, provider=provider, service_name=model, domain=domain,
        )
    if kill_active(config):
        raise _refuse(
            "spend kill switch is ON — refusing paid call",
            category=category, provider=provider, service_name=model, domain=domain,
        )
    if category not in CATEGORIES or category == CATEGORY_SUBSCRIPTION:
        raise SpendCapExceeded(
            f"unknown spend category {category!r} — refusing paid call "
            f"(expected one of {sorted(CATEGORIES - {CATEGORY_SUBSCRIPTION})})"
        )
    if not isinstance(model, str) or not model.strip():
        raise SpendCapExceeded("spend authorize requires a model/service name — refusing paid call")
    resolved_domain = _resolve_domain(domain, config)
    estimate, priced, rates = _estimate(category, model.strip(), config, estimate_usd)
    totals = _totals()  # raises SpendCapExceeded on an unreadable/corrupt ledger

    refusal_fields = {
        "category": category, "provider": provider, "service_name": model.strip(),
        "domain": resolved_domain, "estimate_usd": estimate,
    }
    if category != CATEGORY_PUBLISH:
        if category in (CATEGORY_MODEL, CATEGORY_IMAGE):
            if totals.calls_today >= config.max_calls_per_day:
                raise _refuse(
                    f"daily call cap reached ({totals.calls_today}/{config.max_calls_per_day}) "
                    "— refusing paid call",
                    **refusal_fields,
                )
        if totals.spent_today_usd + estimate > config.daily_usd:
            raise _refuse(
                f"daily spend cap reached (${totals.spent_today_usd:.4f} + ${estimate:.4f} > "
                f"${config.daily_usd:.4f}) — refusing paid call",
                **refusal_fields,
            )
        if totals.spent_month_usd + estimate > config.monthly_usd:
            raise _refuse(
                f"monthly spend cap reached (${totals.spent_month_usd:.4f} + ${estimate:.4f} > "
                f"${config.monthly_usd:.4f}) — refusing paid call",
                **refusal_fields,
            )
        domain_today = totals.by_domain_today.get(resolved_domain, 0.0)
        if domain_today + estimate > config.per_domain_daily_usd:
            raise _refuse(
                f"per-domain daily spend cap reached for {resolved_domain!r} "
                f"(${domain_today:.4f} + ${estimate:.4f} > ${config.per_domain_daily_usd:.4f})"
                " — refusing paid call",
                **refusal_fields,
            )

    reservation = Reservation(
        id=uuid.uuid4().hex,
        ts=_now_iso(),
        category=category,
        provider=provider,
        service_name=model.strip(),
        domain=resolved_domain,
        priced=priced,
        estimate_usd=estimate,
        rates=rates,
        context=dict(context or {}),
    )
    _append_event(
        {
            "id": reservation.id,
            "ts": reservation.ts,
            "event": EVENT_RESERVED,
            "status": STATUS_RESERVED,
            "category": reservation.category,
            "provider": reservation.provider,
            "service_name": reservation.service_name,
            "domain": reservation.domain,
            "priced": reservation.priced,
            "estimate_usd": reservation.estimate_usd,
            "context": {str(k): v for k, v in reservation.context.items()},
        }
    )
    return reservation


def _settle_cost(
    reservation: Reservation, tokens: Mapping[str, int] | None, cost_usd: float | None
) -> float:
    if cost_usd is not None:
        return _as_number(cost_usd, "cost_usd")
    if reservation.category == CATEGORY_PUBLISH or (
        reservation.category == CATEGORY_TOOL and reservation.service_name == "directus-api"
    ):
        return 0.0
    if (
        tokens
        and reservation.rates is not None
        and ("prompt_tokens" in tokens or "completion_tokens" in tokens)
    ):
        prompt = tokens.get("prompt_tokens")
        completion = tokens.get("completion_tokens")
        prompt = prompt if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0 else 0
        completion = (
            completion if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0 else 0
        )
        prompt_rate, completion_rate = reservation.rates
        return round(prompt / 1000.0 * prompt_rate + completion / 1000.0 * completion_rate, 6)
    # unpriced, a token mapping without the provider's key names (never a
    # silent $0), or a failed/timed-out attempt: the reservation's estimate stands
    return reservation.estimate_usd


def record(
    reservation: Reservation,
    *,
    status: str,
    tokens: Mapping[str, int] | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Settle a reservation after the attempt (``succeeded`` / ``failed`` / ``refused``).

    Never raises for ledger trouble: the money is already spent (or already
    refused), and the pending reservation keeps counting at its estimate, so
    accounting degrades to "conservative" instead of "lost". Programmer
    errors (bad status) do raise.
    """
    if status not in SETTLED_STATUSES | {STATUS_REFUSED}:
        raise ValueError(f"status must be one of {sorted(SETTLED_STATUSES | {STATUS_REFUSED})}, got {status!r}")
    if status == STATUS_REFUSED:
        cost = 0.0
    else:
        cost = _settle_cost(reservation, tokens, cost_usd)
    payload: dict[str, Any] = {
        "id": reservation.id,
        "ts": _now_iso(),
        "event": EVENT_SETTLED,
        "status": status,
        "category": reservation.category,
        "provider": reservation.provider,
        "service_name": reservation.service_name,
        "domain": reservation.domain,
        "priced": reservation.priced,
        "estimate_usd": reservation.estimate_usd,
        "cost_usd": cost,
    }
    if tokens:
        payload["tokens"] = {
            str(key): int(value)
            for key, value in dict(tokens).items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
    try:
        _append_event(payload)
    except SpendCapExceeded as exc:
        logger.error(
            "spend: could not settle reservation %s (%s/%s): %s — pending reservation "
            "still counts at its estimate",
            reservation.id, reservation.category, reservation.service_name, exc,
        )
    return payload


def record_subscription(
    name: str,
    cost_usd: float,
    *,
    interval: str = INTERVAL_MONTHLY,
    provider: str = "",
    domain: str | None = None,
) -> dict[str, Any]:
    """Book a fixed recurring cost (VPS, Directus, n8n, hosting…) into the ledger.

    Subscriptions are facts, not requests: recording one is never refused by
    the caps (a refused subscription record would just hide an obligation).
    It *does* join the totals, so the next :func:`authorize` sees the truth
    and refuses if the caps no longer fit. Repeated records for the same
    ``provider/name`` do not stack: only the newest record counts.

    Raises:
        ValueError: bad name / cost / interval (programmer error).
        SpendCapExceeded: the spend config is unusable (fail closed).
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("record_subscription requires a non-empty name")
    if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)) or cost_usd < 0:
        raise ValueError(f"record_subscription cost_usd must be a non-negative number, got {cost_usd!r}")
    clean_interval = str(interval or INTERVAL_MONTHLY).strip().lower()
    if clean_interval not in INTERVALS:
        raise ValueError(f"interval must be one of {sorted(INTERVALS)}, got {interval!r}")
    config = load_config()
    resolved_domain = _resolve_domain(domain, config)
    payload: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": _now_iso(),
        "event": EVENT_SUBSCRIPTION,
        "status": STATUS_ACTIVE,
        "category": CATEGORY_SUBSCRIPTION,
        "provider": str(provider or "").strip(),
        "service_name": clean_name,
        "domain": resolved_domain,
        "interval": clean_interval,
        "cost_usd": round(float(cost_usd), 6),
        "monthly_usd": round(_monthly_equivalent(float(cost_usd), clean_interval), 6),
    }
    _append_event(payload)
    logger.info(
        "spend: subscription recorded — %s%s %s/%s on %s",
        payload["provider"] + "/" if payload["provider"] else "",
        clean_name, clean_interval, f"${cost_usd:g}", resolved_domain,
    )
    return payload


# --------------------------------------------------------------------------- #
# switch + status (API surface)
# --------------------------------------------------------------------------- #


def set_kill_switch(on: bool, *, by: str | None = None) -> dict[str, Any]:
    """Flip ``spend.kill`` in the config file (atomic, lock-serialized).

    ``AGENCY_SPEND_KILL=1`` in the environment is a *separate*, stronger
    switch (it cannot be flipped from here) and its state is reported back.
    """
    if not isinstance(on, bool):
        raise ValueError("set_kill_switch requires a boolean")
    path = config_path()

    def _mutate(state: Any) -> Any:
        if not isinstance(state, dict):
            raise SpendCapExceeded(f"engine config unreadable: {path} — kill switch not written")
        spend = state.get("spend")
        if not isinstance(spend, dict):
            raise SpendCapExceeded(
                f"engine config has no 'spend' section: {path} — kill switch not written"
            )
        updated = dict(state)
        updated["spend"] = {**spend, "kill": bool(on)}
        return updated

    try:
        update_json(path, _mutate, default={})
    except SpendCapExceeded:
        raise
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        raise SpendCapExceeded(f"could not write the kill switch to {path}: {exc}") from exc
    reset_caches()
    logger.warning("spend: kill switch turned %s by %s", "ON" if on else "OFF", by or "unknown")
    return {
        "kill": bool(on),
        "env_kill_active": _env_kill_active(),
        "config_path": str(path),
        "by": by or None,
    }


def status_snapshot() -> dict[str, Any]:
    """Today's/month's spend, caps, subscriptions and switch state (for /api/finance/status)."""
    ledger = ledger_path()
    base: dict[str, Any] = {
        "ledger_path": str(ledger),
        "config_path": str(config_path()),
        "timestamp": _now_iso(),
    }
    try:
        config = load_config()
    except SpendCapExceeded as exc:
        return {**base, "available": False, "error": str(exc), "blocked_reason": str(exc)}
    totals = _totals()
    merged_subs: dict[str, Subscription] = {sub.key: sub for sub in config.subscriptions}
    try:
        merged_subs.update(_ledger_subscriptions(_read_events(ledger)))
    except SpendCapExceeded as exc:
        return {**base, "available": False, "error": str(exc), "blocked_reason": str(exc)}
    return {
        **base,
        "available": True,
        "enabled": config.enabled,
        "kill_switch": kill_active(config),
        "env_kill": _env_kill_active(),
        "blocked_reason": blocked_reason(),
        "caps": {
            "daily_usd": config.daily_usd,
            "monthly_usd": config.monthly_usd,
            "per_domain_daily_usd": config.per_domain_daily_usd,
            "max_calls_per_day": config.max_calls_per_day,
        },
        "today": {
            "spent_usd": totals.spent_today_usd,
            "calls": totals.calls_today,
            "tool_calls": totals.tool_calls_today,
            "publish_calls": totals.publish_calls_today,
            "by_domain_usd": dict(totals.by_domain_today),
        },
        "month": {
            "spent_usd": totals.spent_month_usd,
            "budget_usd": config.monthly_usd,
        },
        "subscriptions": {
            "monthly_usd": totals.subscription_monthly_usd,
            "daily_amortized_usd": totals.subscription_daily_usd,
            "items": [
                {
                    "name": sub.name,
                    "provider": sub.provider,
                    "domain": sub.domain,
                    "interval": sub.interval,
                    "monthly_usd": sub.monthly_usd,
                    "source": sub.source,
                }
                for sub in sorted(merged_subs.values(), key=lambda s: s.key)
            ],
        },
        "unpriced_estimate_usd": config.unpriced_estimate_usd,
        "unpriced_events": totals.unpriced_events,
        "refused_events": totals.refused_events,
        "ledger_events": totals.events,
    }
