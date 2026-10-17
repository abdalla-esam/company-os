#!/usr/bin/env python3
"""brand_profile.py — Dynamic Company DNA, Brand Guidelines, and Profile Provider.

Manages dynamic company context for autonomous workers and capsules:
- Brand & Legal Identity (Name, Industry, Commercial Reg, Tax ID)
- Contact & Headquarters (Email, Phone, WhatsApp, Website, Address, Timezone)
- Visual Identity (Logo URLs/Path, Brand Colors, Typography)
- Brand Voice & Audience (Tone, Target Audience, Languages, Values)
- Social & Communication Channels (Social Links, Primary Channels)
- Standard Library only; zero external dependencies.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional

from shared_kernel.atomic_io import update_json

COMPANY_PROFILE_NAME = "company_profile.json"
COMPANY_PROFILE_ENV = "COMPANY_PROFILE_PATH"

DEFAULT_PROFILE: dict[str, Any] = {
    # 1. Identity & Legal
    "company_name": "",
    "legal_name": "",
    "brand_name": "",
    "tagline": "",
    "description": "",
    "industry": "",
    "commercial_registration": "",
    "tax_id": "",

    # 2. Contact & Communications
    "email": "",
    "support_email": "",
    "phone": "",
    "whatsapp": "",
    "website": "",

    # 3. Headquarters & Operations
    "address": {
        "country": "",
        "city": "",
        "state_province": "",
        "postal_code": "",
        "street": "",
    },
    "timezone": "",
    "currency": "",

    # 4. Visual Assets & Logos
    "logo_url": "",
    "logo_dark_url": "",
    "logo_light_url": "",
    "logo_icon_url": "",
    "logo_path": "",

    # 5. Brand Identity & Design System
    "brand_colors": {
        "primary": "",
        "secondary": "",
        "accent": "",
        "background": "",
        "text": "",
    },
    "typography": {
        "heading_font": "",
        "body_font": "",
        "primary_font": "",
    },

    # 6. Brand Voice & Audience
    "tone": "",
    "language": "",
    "target_audience": "",
    "mission": "",
    "vision": "",
    "values": [],

    # 7. Distribution Channels & Social Links
    "primary_channels": [],
    "social_links": {
        "linkedin": "",
        "x": "",
        "facebook": "",
        "instagram": "",
        "youtube": "",
        "tiktok": "",
        "github": "",
    },

    # 8. Operational Settings & Extensibility
    "default_ttl_hours": 48,
    "custom_metadata": {},
}


def deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge source mapping into target dictionary in-place."""
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return target


def company_profile_path(custom_path: Optional[str | os.PathLike[str]] = None) -> Path:
    """Resolve the path to the company profile configuration file."""
    if custom_path:
        return Path(custom_path).resolve()
    for var in (COMPANY_PROFILE_ENV, "AGENCY_COMPANY_PROFILE_FILE", "AGENCY_BRAND_PROFILE_FILE"):
        env_path = os.environ.get(var, "").strip()
        if env_path:
            return Path(env_path).resolve()
    # Default: config/company_profile.json (or fallback to repository root)
    try:
        from shared_kernel.paths import REPO_ROOT
        repo_root = REPO_ROOT
    except Exception:
        p = Path(__file__).resolve()
        repo_root = p.parents[2] if len(p.parents) > 2 else p.parents[1]
    config_dir_file = repo_root / "config" / COMPANY_PROFILE_NAME
    if config_dir_file.is_file():
        return config_dir_file
    return repo_root / COMPANY_PROFILE_NAME


def load_company_profile(custom_path: Optional[str | os.PathLike[str]] = None) -> dict[str, Any]:
    """Load the company profile from disk, falling back to environment or default."""
    path = company_profile_path(custom_path)
    profile = copy.deepcopy(DEFAULT_PROFILE)

    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                from shared_kernel.schema_version import (
                    KIND_COMPANY_PROFILE,
                    check_schema_version,
                )

                ver = check_schema_version(KIND_COMPANY_PROFILE, decoded, path)
                if ver != -1:
                    deep_merge(profile, decoded)
        except Exception:
            # Fall back gracefully without crashing
            pass

    # Environment variable overrides (if provided at runtime)
    if os.environ.get("COMPANY_NAME"):
        profile["company_name"] = os.environ["COMPANY_NAME"].strip()
    if os.environ.get("COMPANY_BRAND_NAME"):
        profile["brand_name"] = os.environ["COMPANY_BRAND_NAME"].strip()
    if os.environ.get("COMPANY_INDUSTRY"):
        profile["industry"] = os.environ["COMPANY_INDUSTRY"].strip()
    if os.environ.get("COMPANY_TONE"):
        profile["tone"] = os.environ["COMPANY_TONE"].strip()
    if os.environ.get("COMPANY_EMAIL"):
        profile["email"] = os.environ["COMPANY_EMAIL"].strip()
    if os.environ.get("COMPANY_PHONE"):
        profile["phone"] = os.environ["COMPANY_PHONE"].strip()
    if os.environ.get("COMPANY_WEBSITE"):
        profile["website"] = os.environ["COMPANY_WEBSITE"].strip()

    return profile


def save_company_profile(
    profile: Mapping[str, Any], custom_path: Optional[str | os.PathLike[str]] = None
) -> None:
    """Atomically save the company profile to disk using update_json."""
    from shared_kernel.schema_version import (
        KIND_COMPANY_PROFILE,
        stamp_schema_version,
    )

    path = company_profile_path(custom_path)
    data = dict(profile)
    stamp_schema_version(KIND_COMPANY_PROFILE, data)

    update_json(
        path,
        lambda existing: data,
        default={},
        indent=2,
        ensure_ascii=False,
        trailing_newline=True,
    )


def get_company_field(field_path: str, default: Any = "") -> Any:
    """Retrieve a nested field from the profile using dot notation (e.g. 'address.city')."""
    prof = load_company_profile()
    curr: Any = prof
    for part in field_path.split("."):
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return default
    return curr if curr is not None else default


#: ``(label, field)`` pairs, in the order a reader should meet them in a
#: prompt: who we are, what we do, how we sound, who we serve.
PROMPT_CONTEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("Industry", "industry"),
    ("Tagline", "tagline"),
    ("What we do", "description"),
    ("Tone", "tone"),
    ("Language", "language"),
    ("Target reader", "target_audience"),
    ("Mission", "mission"),
    ("Values", "values"),
    ("Channels", "primary_channels"),
)


def _render_prompt_value(value: Any) -> str:
    """One profile value as a single prompt line: lists join, blanks vanish."""
    if isinstance(value, (list, tuple)):
        return "، ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def company_prompt_context(profile: Optional[Mapping[str, Any]] = None) -> str:
    """Render the company profile as a compact block a prompt can carry.

    The block is *descriptive*: it says who the company is. What to do with
    that — which language to write in, how many versions to produce — stays
    with the caller, because the ideation brief and the finished post want
    different instructions over the same DNA.

    Returns ``""`` when the profile names no company, so a caller may append
    the block unconditionally; an unfilled profile adds no empty headings for
    the model to interpret. Every other field is included only when non-empty,
    for the same reason.
    """
    p = profile if profile is not None else load_company_profile()
    brand = str(p.get("brand_name") or p.get("company_name") or "").strip()
    if not brand:
        return ""

    lines = [f"Company DNA — write as {brand}, not about it.", f"- Brand: {brand}"]
    for label, field in PROMPT_CONTEXT_FIELDS:
        rendered = _render_prompt_value(p.get(field))
        if rendered:
            lines.append(f"- {label}: {rendered}")

    website = _render_prompt_value(p.get("website"))
    if website:
        lines.append(f"- Website: {website}")

    # The service list is the one place a deployment names what it sells, and
    # it has no first-class field, so it lives in the documented extension
    # point. It is documented as a list: anything else is not a service list
    # and is not advertised (a scalar here once rendered as "- Services: 7").
    custom = p.get("custom_metadata")
    services = custom.get("services") if isinstance(custom, Mapping) else None
    if isinstance(services, (list, tuple)):
        rendered_services = _render_prompt_value(services)
        if rendered_services:
            lines.append(f"- Services: {rendered_services}")

    return "\n".join(lines)


def enrich_with_company_context(
    target: MutableMapping[str, Any], profile: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """Inject brand DNA context into a ticket or capsule manifest dictionary without hardcoded mocks."""
    p = profile if profile is not None else load_company_profile()
    ctx = {
        "company_name": p.get("company_name", ""),
        "brand_name": p.get("brand_name") or p.get("company_name", ""),
        "industry": p.get("industry", ""),
        "tagline": p.get("tagline", ""),
        "tone": p.get("tone", ""),
        "email": p.get("email", ""),
        "phone": p.get("phone", ""),
        "website": p.get("website", ""),
        "primary_channels": list(p.get("primary_channels", [])),
        "brand_colors": dict(p.get("brand_colors", {})),
        "typography": dict(p.get("typography", {})),
        "logo_url": p.get("logo_url", ""),
        "social_links": dict(p.get("social_links", {})),
    }
    target["company_context"] = ctx
    return ctx
