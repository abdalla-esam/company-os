#!/usr/bin/env python3
"""asset_vault.py — Zero-Waste Asset Vault & Content-Addressed Storage.

Ensures that every generated visual asset or graphic deliverable is permanently
preserved in the asset library, indexed by SHA-256, so nothing is wasted or lost
even if a capsule is rejected or revised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_ENGINE_ROOT = Path(__file__).resolve().parents[3] / "engine"
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from shared_kernel.atomic_io import append_jsonl_line, atomic_bytes

logger = logging.getLogger("marketing.asset_vault")


class AssetVaultError(RuntimeError):
    """Raised when an asset write or index entry append fails."""
    pass


VAULT_DIR_NAME = "assets_vault"


def get_vault_dir() -> Path:
    """Return the absolute path to the central asset vault directory."""
    for var in ("AGENCY_ASSETS_VAULT_DIR", "ASSETS_VAULT_DIR"):
        env = os.environ.get(var, "").strip()
        if env:
            return Path(env).resolve()
    try:
        from shared_kernel.paths import ASSETS_VAULT
        vault_dir = ASSETS_VAULT
    except Exception:
        # data/domains/marketing/asset_vault.py -> the repo root is parents[3].
        repo_root = Path(__file__).resolve().parents[3]
        vault_dir = repo_root / "data" / "assets" / "vault"
    return vault_dir


def store_asset(
    image_bytes: bytes,
    prompt: str,
    metadata: Optional[Dict[str, Any]] = None,
    capsule_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Store an asset by SHA-256 and update the index ledger."""
    vault_dir = get_vault_dir()
    vault_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(image_bytes).hexdigest()
    asset_file = vault_dir / f"{sha}.jpg"

    newly_created = False
    if not asset_file.exists():
        atomic_bytes(asset_file, image_bytes)
        newly_created = True

    meta_file = vault_dir / "index.jsonl"
    record = {
        "sha256": sha,
        "bytes_len": len(image_bytes),
        "prompt": prompt,
        "capsule_id": capsule_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "extra": metadata or {},
    }

    try:
        append_jsonl_line(meta_file, json.dumps(record, ensure_ascii=False))
    except Exception as exc:
        if newly_created:
            try:
                asset_file.unlink(missing_ok=True)
            except OSError:
                pass
        logger.error(
            "Failed to append asset index record for sha256=%s to %s: %s",
            sha,
            meta_file,
            exc,
            exc_info=True,
        )
        raise AssetVaultError(
            f"Failed to record index entry for asset {sha}: {exc}"
        ) from exc

    return {
        "sha256": sha,
        "path": str(asset_file),
        "filename": f"{sha}.jpg",
        "size_kb": round(len(image_bytes) / 1024, 1),
    }


def find_asset_by_sha(sha256: str) -> Optional[Path]:
    """Retrieve an asset file path given its SHA-256 hash."""
    vault_dir = get_vault_dir()
    candidate = vault_dir / f"{sha256}.jpg"
    return candidate if candidate.is_file() else None


def list_vault_assets(limit: int = 50) -> List[Dict[str, Any]]:
    """List recent assets stored in the Zero-Waste Vault."""
    vault_dir = get_vault_dir()
    meta_file = vault_dir / "index.jsonl"
    if not meta_file.is_file():
        return []

    results: List[Dict[str, Any]] = []
    try:
        with open(meta_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
    except Exception:
        pass

    return results[-limit:][::-1]
