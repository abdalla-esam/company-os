#!/usr/bin/env python3
"""policy.py — the engine's configuration spine (``config/engine.json``).

Every tunable of the universal engine lives here, in exactly one place, so no
subsystem ever hardcodes an operational constant:

- ``gates``   — gate templates per domain and the max plan length;
- ``memory``  — the cumulative feedback ledger window and constraint budget;
- ``dedup``   — the rolling deduplication window, similarity threshold and
  per-domain overrides (``window_days: 30`` today, 15/60/90/custom tomorrow);
- ``trace``   — execution-trace toggles and the gear price table used for
  ``estimated_cost_usd``.

Loading is **fail-safe, never fail-loud**: a missing file is normal (built-in
defaults apply); a malformed section is *reported* in :attr:`EnginePolicy.issues`
and replaced by its defaults, so a typo in a config file can never wedge the
factory. Validation is strict where it matters: a dedup threshold outside
``(0, 1]``, a negative window, or a non-object template entry is refused and
the default wins (the issue is always recorded).

Environment overrides (applied last, validated identically):

    AGENCY_ENGINE_CONFIG        path of the policy file
    AGENCY_GATES_ENABLED        "off"/"0"/"false" disables the gate layer
    AGENCY_MEMORY_ENABLED       same grammar
    AGENCY_DEDUP_ENABLED        same grammar
    AGENCY_DEDUP_WINDOW_DAYS    rolling window in days
    AGENCY_DEDUP_THRESHOLD      similarity threshold, e.g. "0.7"
    AGENCY_TRACE_ENABLED        same grammar

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("capsule_engine.policy")

__all__ = [
    "POLICY_NAME",
    "DEFAULT_GATE_TEMPLATE",
    "DEFAULT_GATE_AUTHORITIES",
    "DedupPolicy",
    "MemoryPolicy",
    "GatesPolicy",
    "TracePolicy",
    "DomainPolicy",
    "EnginePolicy",
    "default_policy",
    "policy_path",
    "load_policy",
    "reset_cache",
]

POLICY_NAME = "engine.json"

#: The built-in gate template — two gates: automated QA, then a domain lead.
#: A deployment overrides this in ``config/engine.json`` (1, 2, 5, 10 gates).
DEFAULT_GATE_TEMPLATE: Tuple[Dict[str, Any], ...] = (
    {
        "gate_type": "qa_review",
        "name": "Automated QA & Syntax Review",
        "targets": ("deliverables/**/*", "copy.md"),
    },
    {
        "gate_type": "domain_lead_approval",
        "name": "Domain Lead Sign-off",
        "authority": "capsule:approve",
    },
)

DEFAULT_GATE_AUTHORITIES: Dict[str, str] = {
    "domain_lead_approval": "capsule:approve",
    "manual_edit": "capsule:approve",
    "sovereign_root_approval": "capsule:approve",
}

MAX_TEMPLATE_GATES = 64  # sanity bound for one plan; override via max_gates


# --------------------------------------------------------------------------- #
# small typed coercion helpers (no guessing: invalid → default + issue)
# --------------------------------------------------------------------------- #

def _as_bool(value: object, default: bool) -> Optional[bool]:
    """Coerce a config boolean; ``None`` when the value is not a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    return None


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    return None if raw is None else _as_bool(raw, False)


def _as_positive_float(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number <= 0:
        return None
    return number


def _as_nonnegative_float(value: object) -> Optional[float]:
    """Coerce a rate that may legitimately be 0.0 (a free/offline gear)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0:
        return None
    return number


def _as_int(value: object, *, minimum: int = 0) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= minimum else None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _coerce_template(raw: object) -> Optional[Tuple[Dict[str, Any], ...]]:
    """Validate a gate template list; ``None`` when it cannot be used.

    Structural validation only (gate_type/name/targets/required/authority);
    the deep contract lives in :mod:`capsule_engine.gates`, which refuses a
    template that reaches it malformed.
    """
    if not isinstance(raw, (list, tuple)):
        return None
    if len(raw) > MAX_TEMPLATE_GATES:
        return None
    template: list[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            return None
        gate_type = entry.get("gate_type")
        if not isinstance(gate_type, str) or not gate_type.strip():
            return None
        spec: Dict[str, Any] = {"gate_type": gate_type.strip()}
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            spec["name"] = name.strip()
        targets = entry.get("targets")
        if targets is not None:
            if not isinstance(targets, (list, tuple)):
                return None
            cleaned = [str(t) for t in targets if isinstance(t, str) and t.strip()]
            if len(cleaned) != len(targets):
                return None
            spec["targets"] = tuple(cleaned)
        required = entry.get("required")
        if required is not None:
            coerced = _as_bool(required, True)
            if coerced is None:
                return None
            spec["required"] = coerced
        authority = entry.get("authority")
        if authority is not None:
            if not isinstance(authority, str) or not authority.strip():
                return None
            spec["authority"] = authority.strip()
        template.append(spec)
    return tuple(template)


# --------------------------------------------------------------------------- #
# section dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DedupPolicy:
    """The rolling deduplication window and its similarity contract."""

    enabled: bool = True
    window_days: float = 30.0
    similarity_threshold: float = 0.70
    shingle_size: int = 4
    max_shingles: int = 2000
    max_entries: int = 20000
    domains: Mapping[str, "DedupPolicy"] = field(default_factory=dict)

    def for_domain(self, domain: Optional[str]) -> "DedupPolicy":
        """Return the effective policy for ``domain`` (override or self)."""
        if not self.enabled:
            return self
        if not domain:
            return self
        override = self.domains.get(str(domain).strip())
        return self if override is None else override


@dataclass(frozen=True)
class MemoryPolicy:
    """Cumulative feedback memory: ledger window and constraint budget."""

    enabled: bool = True
    window_days: float = 180.0
    max_constraints: int = 12
    max_reason_chars: int = 2000
    path: Optional[str] = None  # explicit ledger override (tests, ops)


@dataclass(frozen=True)
class GatesPolicy:
    """Arbitrary-length gate plans, templates per domain, authority map."""

    enabled: bool = True
    max_gates: int = MAX_TEMPLATE_GATES
    default_template: Tuple[Mapping[str, Any], ...] = DEFAULT_GATE_TEMPLATE
    domains: Mapping[str, Tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict
    )
    authorities: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_GATE_AUTHORITIES)
    )

    def template_for(self, domain: Optional[str]) -> Tuple[Mapping[str, Any], ...]:
        """The gate template that governs ``domain`` (fallback: default)."""
        if domain:
            specific = self.domains.get(str(domain).strip())
            if specific:
                return specific
        return self.default_template

    def authority_for(self, gate_type: str, explicit: Optional[str] = None) -> Optional[str]:
        """The capability a human gate demands (explicit wins over the map)."""
        if explicit:
            return explicit
        return self.authorities.get(gate_type)


@dataclass(frozen=True)
class CopySignoffPolicy:
    """Whether a human reads the copy before the design stage spends anything.

    The design stage is the first step that calls a paid image model, and the
    copy is the only artifact that decides whether the post is worth making at
    all. A domain can therefore require a human to sign the copy off, holding
    the capsule at ``pending_design``: the copy exists, the image does not yet.

    ``domains`` empty means every domain; a populated list means only those.
    A disabled policy means the pipeline runs straight through as before.
    """

    enabled: bool = False
    domains: frozenset[str] = field(default_factory=frozenset)

    def required_for(self, domain: Optional[str]) -> bool:
        """Whether ``domain`` must stop for a copy sign-off."""
        if not self.enabled:
            return False
        if not self.domains:
            return True
        return str(domain or "").strip() in self.domains


@dataclass(frozen=True)
class TracePolicy:
    """Execution trace toggles and the gear price table (estimated cost)."""

    enabled: bool = True
    max_events: int = 20000
    costs: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def estimate_cost(self, gear: Optional[str], tokens: Mapping[str, int] | None) -> float:
        """Estimated USD for a gear invocation from the configured price table.

        Unknown gears are priced at 0.0 — an explicit zero, never a guess.
        """
        if not gear or not tokens:
            return 0.0
        price = self.costs.get(gear)
        if not isinstance(price, Mapping):
            return 0.0
        prompt_rate = _as_nonnegative_float(price.get("prompt_per_1k")) or 0.0
        completion_rate = _as_nonnegative_float(price.get("completion_per_1k")) or 0.0
        prompt_tokens = _as_int(tokens.get("prompt_tokens")) or 0
        completion_tokens = _as_int(tokens.get("completion_tokens")) or 0
        total = prompt_tokens / 1000.0 * prompt_rate + completion_tokens / 1000.0 * completion_rate
        return round(total, 6)


@dataclass(frozen=True)
class DomainPolicy:
    """The effective view of the policy for one operational domain."""

    domain: Optional[str]
    dedup: DedupPolicy
    memory: MemoryPolicy
    gates: GatesPolicy
    trace: TracePolicy

    #: Whether this domain's copy needs a human sign-off before design spends.
    copy_signoff_enabled: bool = False


@dataclass(frozen=True)
class EnginePolicy:
    """The whole policy, plus a record of what was refused while loading."""

    version: int = 1
    gates: GatesPolicy = field(default_factory=GatesPolicy)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    dedup: DedupPolicy = field(default_factory=DedupPolicy)
    trace: TracePolicy = field(default_factory=TracePolicy)
    copy_signoff: CopySignoffPolicy = field(default_factory=CopySignoffPolicy)
    path: Optional[str] = None
    issues: Tuple[str, ...] = ()

    def for_domain(self, domain: Optional[str]) -> DomainPolicy:
        """Resolve the per-domain view (dedup overrides, gate template)."""
        return DomainPolicy(
            domain=domain,
            dedup=self.dedup.for_domain(domain),
            memory=self.memory,
            gates=self.gates,
            trace=self.trace,
            copy_signoff_enabled=self.copy_signoff.required_for(domain),
        )

    def copy_signoff_required(self, domain: Optional[str]) -> bool:
        """Whether ``domain`` must stop for a human copy sign-off."""
        return self.copy_signoff.required_for(domain)


def default_policy() -> EnginePolicy:
    """The built-in policy (no file, no environment)."""
    return EnginePolicy()


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def policy_path(explicit: Optional[str | os.PathLike[str]] = None) -> Path:
    """Resolve the policy file: explicit → env → shared_kernel → repo default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("AGENCY_ENGINE_CONFIG", "").strip()
    if env:
        return Path(env)
    try:  # prefer the canonical resolver when it is importable
        from shared_kernel.paths import ENGINE_CONFIG_PATH

        return Path(ENGINE_CONFIG_PATH)
    except Exception:  # standalone execution (engine/ on sys.path only)
        return Path(__file__).resolve().parents[2] / "config" / POLICY_NAME


def _parse_dedup(
    raw: Mapping[str, Any], issues: list[str]
) -> DedupPolicy:
    """Build a :class:`DedupPolicy` from a raw JSON object (fail-safe)."""
    base = DedupPolicy()
    values: Dict[str, Any] = {}

    for key in ("enabled",):
        if key in raw:
            coerced = _as_bool(raw.get(key), base.enabled)
            if coerced is None:
                issues.append(f"dedup.{key} is not a boolean; default kept")
            else:
                values[key] = coerced

    if "window_days" in raw:
        window = _as_positive_float(raw.get("window_days"))
        if window is None:
            issues.append(
                "dedup.window_days must be a positive number; default kept"
            )
        else:
            values["window_days"] = window

    if "similarity_threshold" in raw:
        threshold = _as_positive_float(raw.get("similarity_threshold"))
        if threshold is None or threshold > 1.0:
            issues.append(
                "dedup.similarity_threshold must be in (0, 1]; default kept"
            )
        else:
            values["similarity_threshold"] = threshold

    for key in ("shingle_size", "max_shingles", "max_entries"):
        if key in raw:
            number = _as_int(raw.get(key), minimum=1)
            if number is None:
                issues.append(f"dedup.{key} must be a positive integer; default kept")
            else:
                values[key] = number

    domains_raw = raw.get("domains")
    overrides: Dict[str, DedupPolicy] = {}
    if domains_raw is not None:
        if not isinstance(domains_raw, Mapping):
            issues.append("dedup.domains must be an object; ignored")
        else:
            parent = replace(base, **values)
            for name, section in domains_raw.items():
                if not isinstance(section, Mapping):
                    issues.append(f"dedup.domains.{name} must be an object; ignored")
                    continue
                child_issues: list[str] = []
                partial = _parse_dedup(section, child_issues)
                # Overrides inherit the parent where they declare nothing.
                override = replace(
                    parent,
                    domains={},
                    **{
                        key: getattr(partial, key)
                        for key in (
                            "enabled",
                            "window_days",
                            "similarity_threshold",
                            "shingle_size",
                            "max_shingles",
                            "max_entries",
                        )
                        if key in section
                    },
                )
                issues.extend(
                    f"dedup.domains.{name}: {issue}" for issue in child_issues
                )
                overrides[str(name)] = override

    return replace(base, domains=overrides, **values)


def _parse_memory(raw: Mapping[str, Any], issues: list[str]) -> MemoryPolicy:
    base = MemoryPolicy()
    values: Dict[str, Any] = {}
    if "enabled" in raw:
        coerced = _as_bool(raw.get("enabled"), base.enabled)
        if coerced is None:
            issues.append("memory.enabled is not a boolean; default kept")
        else:
            values["enabled"] = coerced
    if "window_days" in raw:
        window = _as_positive_float(raw.get("window_days"))
        if window is None:
            issues.append("memory.window_days must be a positive number; default kept")
        else:
            values["window_days"] = window
    for key in ("max_constraints", "max_reason_chars"):
        if key in raw:
            number = _as_int(raw.get(key), minimum=1)
            if number is None:
                issues.append(f"memory.{key} must be a positive integer; default kept")
            else:
                values[key] = number
    path = raw.get("path")
    if path is not None:
        if isinstance(path, str) and path.strip():
            values["path"] = path.strip()
        else:
            issues.append("memory.path must be a non-empty string; ignored")
    return replace(base, **values)


def _parse_gates(raw: Mapping[str, Any], issues: list[str]) -> GatesPolicy:
    base = GatesPolicy()
    values: Dict[str, Any] = {}
    if "enabled" in raw:
        coerced = _as_bool(raw.get("enabled"), base.enabled)
        if coerced is None:
            issues.append("gates.enabled is not a boolean; default kept")
        else:
            values["enabled"] = coerced
    if "max_gates" in raw:
        limit = _as_int(raw.get("max_gates"), minimum=1)
        if limit is None:
            issues.append("gates.max_gates must be a positive integer; default kept")
        else:
            values["max_gates"] = limit

    template = raw.get("default_template")
    if template is not None:
        coerced_template = _coerce_template(template)
        if coerced_template is None:
            issues.append(
                "gates.default_template is malformed; built-in template kept"
            )
        else:
            values["default_template"] = coerced_template

    domains_raw = raw.get("domains")
    templates: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
    if domains_raw is not None:
        if not isinstance(domains_raw, Mapping):
            issues.append("gates.domains must be an object; ignored")
        else:
            for name, section in domains_raw.items():
                coerced_template = _coerce_template(section)
                if coerced_template is None:
                    issues.append(
                        f"gates.domains.{name} is malformed; default template applies"
                    )
                    continue
                templates[str(name)] = coerced_template
    values["domains"] = templates

    authorities_raw = raw.get("authorities")
    if authorities_raw is not None:
        if not isinstance(authorities_raw, Mapping):
            issues.append("gates.authorities must be an object; default kept")
        else:
            merged: Dict[str, str] = dict(base.authorities)
            for gate_type, capability in authorities_raw.items():
                if isinstance(capability, str) and capability.strip():
                    merged[str(gate_type)] = capability.strip()
                else:
                    issues.append(
                        f"gates.authorities.{gate_type} must be a non-empty "
                        "string; ignored"
                    )
            values["authorities"] = merged

    return replace(base, **values)


def _parse_trace(raw: Mapping[str, Any], issues: list[str]) -> TracePolicy:
    base = TracePolicy()
    values: Dict[str, Any] = {}
    if "enabled" in raw:
        coerced = _as_bool(raw.get("enabled"), base.enabled)
        if coerced is None:
            issues.append("trace.enabled is not a boolean; default kept")
        else:
            values["enabled"] = coerced
    if "max_events" in raw:
        limit = _as_int(raw.get("max_events"), minimum=1)
        if limit is None:
            issues.append("trace.max_events must be a positive integer; default kept")
        else:
            values["max_events"] = limit

    costs_raw = raw.get("costs")
    if costs_raw is not None:
        if not isinstance(costs_raw, Mapping):
            issues.append("trace.costs must be an object; ignored")
        else:
            costs: Dict[str, Mapping[str, float]] = {}
            for gear, price in costs_raw.items():
                if not isinstance(price, Mapping):
                    issues.append(f"trace.costs.{gear} must be an object; ignored")
                    continue
                cleaned: Dict[str, float] = {}
                for rate_key in ("prompt_per_1k", "completion_per_1k"):
                    rate = _as_nonnegative_float(price.get(rate_key))
                    if rate is not None:
                        cleaned[rate_key] = rate
                    elif rate_key in price:
                        issues.append(
                            f"trace.costs.{gear}.{rate_key} must be a positive "
                            "number; ignored"
                        )
                costs[str(gear)] = cleaned
            values["costs"] = costs

    return replace(base, **values)


def _apply_env(policy: EnginePolicy, issues: list[str]) -> EnginePolicy:
    """Apply the environment overrides on top of the file policy."""
    gates = policy.gates
    memory = policy.memory
    dedup = policy.dedup
    trace = policy.trace

    env_gates = _env_bool("AGENCY_GATES_ENABLED")
    if env_gates is not None:
        gates = replace(gates, enabled=env_gates)

    env_memory = _env_bool("AGENCY_MEMORY_ENABLED")
    if env_memory is not None:
        memory = replace(memory, enabled=env_memory)

    env_dedup = _env_bool("AGENCY_DEDUP_ENABLED")
    if env_dedup is not None:
        dedup = replace(dedup, enabled=env_dedup)

    env_trace = _env_bool("AGENCY_TRACE_ENABLED")
    if env_trace is not None:
        trace = replace(trace, enabled=env_trace)

    raw_window = os.environ.get("AGENCY_DEDUP_WINDOW_DAYS", "").strip()
    if raw_window:
        try:
            window = float(raw_window)
        except ValueError:
            window = -1.0
        if window > 0:
            dedup = replace(dedup, window_days=window)
        else:
            issues.append(
                "AGENCY_DEDUP_WINDOW_DAYS must be a positive number; ignored"
            )

    raw_threshold = os.environ.get("AGENCY_DEDUP_THRESHOLD", "").strip()
    if raw_threshold:
        try:
            threshold = float(raw_threshold)
        except ValueError:
            threshold = -1.0
        if 0 < threshold <= 1.0:
            dedup = replace(dedup, similarity_threshold=threshold)
        else:
            issues.append("AGENCY_DEDUP_THRESHOLD must be in (0, 1]; ignored")

    return replace(policy, gates=gates, memory=memory, dedup=dedup, trace=trace)


#: Process-wide cache: the policy file is read at most once per process unless
#: ``load_policy(..., refresh=True)`` is requested. Re-reading a 2 KB JSON file
#: is cheap, but a pulse pass resolves the policy per worker run.
_CACHE: Optional[EnginePolicy] = None


def _parse_copy_signoff(
    raw: Mapping[str, Any], issues: list[str]
) -> CopySignoffPolicy:
    """Build a :class:`CopySignoffPolicy` from a raw JSON object (fail-safe)."""
    base = CopySignoffPolicy()
    values: Dict[str, Any] = {}

    if "enabled" in raw:
        coerced = _as_bool(raw.get("enabled"), base.enabled)
        if coerced is None:
            issues.append("copy_signoff.enabled is not a boolean; default kept")
        else:
            values["enabled"] = coerced

    domains_raw = raw.get("domains")
    if domains_raw is not None:
        if not isinstance(domains_raw, (list, tuple)):
            issues.append("copy_signoff.domains must be a list; default kept")
        else:
            names: list[str] = []
            for item in domains_raw:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                else:
                    issues.append(
                        "copy_signoff.domains entries must be non-empty strings; "
                        "entry ignored"
                    )
            values["domains"] = frozenset(names)

    return replace(base, **values)


def load_policy(
    path: Optional[str | os.PathLike[str]] = None,
    *,
    refresh: bool = False,
) -> EnginePolicy:
    """Load the engine policy (fail-safe), applying environment overrides.

    Args:
        path: explicit policy file; ``None`` resolves via env/shared_kernel.
        refresh: bypass the process cache.

    Returns:
        The effective :class:`EnginePolicy`. A missing file yields built-in
        defaults with no issue; a malformed file or section yields the
        built-in default for that section **and** records the refusal in
        :attr:`EnginePolicy.issues` — reported, never silently repaired.
    """
    global _CACHE
    if _CACHE is not None and path is None and not refresh:
        return _CACHE

    issues: list[str] = []
    resolved = policy_path(path)
    raw: Mapping[str, Any] = {}

    if resolved.is_file():
        try:
            decoded = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"policy file unreadable ({resolved}): {exc} — defaults apply")
            decoded = None
        if isinstance(decoded, Mapping):
            raw = decoded
        elif decoded is not None:
            issues.append(
                f"policy file {resolved} must contain a JSON object — "
                "defaults apply"
            )

    gates_raw = _as_mapping(raw.get("gates"))
    memory_raw = _as_mapping(raw.get("memory"))
    dedup_raw = _as_mapping(raw.get("dedup"))
    trace_raw = _as_mapping(raw.get("trace"))
    copy_signoff_raw = _as_mapping(raw.get("copy_signoff"))
    for section, value in (
        ("gates", raw.get("gates")),
        ("memory", raw.get("memory")),
        ("dedup", raw.get("dedup")),
        ("trace", raw.get("trace")),
        ("copy_signoff", raw.get("copy_signoff")),
    ):
        if value is not None and not isinstance(value, Mapping):
            issues.append(f"policy section '{section}' must be an object — ignored")

    version = _as_int(raw.get("version"), minimum=1) or 1

    policy = EnginePolicy(
        version=version,
        gates=_parse_gates(gates_raw, issues),
        memory=_parse_memory(memory_raw, issues),
        dedup=_parse_dedup(dedup_raw, issues),
        trace=_parse_trace(trace_raw, issues),
        copy_signoff=_parse_copy_signoff(copy_signoff_raw, issues),
        path=str(resolved),
        issues=tuple(issues),
    )
    policy = _apply_env(policy, issues)
    policy = replace(policy, issues=tuple(issues))

    for issue in policy.issues:
        logger.warning("policy: %s", issue)

    if path is None:
        _CACHE = policy
    return policy


def reset_cache() -> None:
    """Drop the process-wide policy cache (tests, ops reload)."""
    global _CACHE
    _CACHE = None
