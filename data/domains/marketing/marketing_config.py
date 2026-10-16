#!/usr/bin/env python3
"""marketing_config.py — Dynamic white-label marketing and automation configuration.

Provides dynamic, API-mutable configurations for:
- Cadence (e.g. 48 hours, 24 hours, custom interval)
- Target Channels & Distribution (LinkedIn, Facebook, X, Instagram)
- Occasions detection and scheduling policies
- Budget guards and Image generation limits (Hugging Face / Free Fallback)
- Zero-waste Asset Vault persistence

Standard library only; zero external dependencies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional

_HERE = Path(__file__).resolve().parent
_ENGINE_ROOT = Path(__file__).resolve().parents[3] / "engine"
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from shared_kernel.atomic_io import update_json

CONFIG_FILE_NAME = "marketing_config.json"
CONFIG_ENV = "MARKETING_CONFIG_PATH"

DEFAULT_MARKETING_CONFIG: dict[str, Any] = {
    "cadence_hours": 48.0,
    "enabled_channels": ["linkedin", "facebook", "x"],
    "occasions_mode": "auto",
    "schedule_mode": "strict",
    "budget_guard": {
        "max_attempts": 3,
        "max_image_budget_usd": 0.01,
        "hf_model": "black-forest-labs/FLUX.1-schnell",
        "allow_free_fallback": True,
    },
    "feedback_memory_enabled": True,
    "zero_waste_vault_enabled": True,
}


def marketing_config_path(custom_path: Optional[str | os.PathLike[str]] = None) -> Path:
    """Resolve the path to the marketing config file."""
    if custom_path:
        return Path(custom_path).resolve()
    for var in (CONFIG_ENV, "AGENCY_MARKETING_CONFIG_FILE"):
        env_path = os.environ.get(var, "").strip()
        if env_path:
            return Path(env_path).resolve()
    try:
        from shared_kernel.paths import REPO_ROOT
        repo_root = REPO_ROOT
    except Exception:
        p = Path(__file__).resolve()
        # data/domains/marketing/marketing_config.py -> the repo root is parents[3].
        repo_root = p.parents[3] if len(p.parents) > 3 else p.parents[2]
    config_dir_file = repo_root / "config" / CONFIG_FILE_NAME
    if config_dir_file.is_file():
        return config_dir_file
    return repo_root / CONFIG_FILE_NAME


def load_marketing_config(custom_path: Optional[str | os.PathLike[str]] = None) -> dict[str, Any]:
    """Load marketing config from disk with defensive fallback to defaults."""
    path = marketing_config_path(custom_path)
    config = dict(DEFAULT_MARKETING_CONFIG)

    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                from shared_kernel.schema_version import (
                    KIND_MARKETING_CONFIG,
                    check_schema_version,
                )

                ver = check_schema_version(KIND_MARKETING_CONFIG, decoded, path)
                if ver != -1:
                    config.update(decoded)
        except Exception:
            pass

    return config


def save_marketing_config(
    config: Mapping[str, Any], custom_path: Optional[str | os.PathLike[str]] = None
) -> None:
    """Atomically save the marketing config to disk using update_json."""
    from shared_kernel.schema_version import (
        KIND_MARKETING_CONFIG,
        stamp_schema_version,
    )

    path = marketing_config_path(custom_path)
    data = dict(config)
    stamp_schema_version(KIND_MARKETING_CONFIG, data)

    update_json(
        path,
        lambda existing: data,
        default={},
        indent=2,
        ensure_ascii=False,
        trailing_newline=True,
    )
