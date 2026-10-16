"""schema_version.py — Canonical schema versioning & fail-closed loaders.

Part 4 Phase 2 of Sovereign Agency OS architecture.

Guarantees:
- Per-kind CURRENT and SUPPORTED schema versions.
- Missing version in legacy files is treated as v1 (logged once per kind).
- Unknown or higher versions FAIL CLOSED for critical entities:
  manifests, tickets, gate entries, governance.json, users.json.
- Non-critical config files (company_profile, marketing_config) warn and degrade
  to defaults.
- Versions are stamped ON WRITE only (never mass-rewriting old files).
- Empty migration registry ready for future version bumps.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, MutableMapping, Sequence, Set, Tuple

logger = logging.getLogger("shared_kernel.schema_version")

# Known file kinds
KIND_MANIFEST = "manifest"
KIND_TICKET = "ticket"
KIND_GATE_ENTRY = "gate_entry"
KIND_GOVERNANCE = "governance"
KIND_USERS = "users"
KIND_COMPANY_PROFILE = "company_profile"
KIND_MARKETING_CONFIG = "marketing_config"

ALL_KINDS = (
    KIND_MANIFEST,
    KIND_TICKET,
    KIND_GATE_ENTRY,
    KIND_GOVERNANCE,
    KIND_USERS,
    KIND_COMPANY_PROFILE,
    KIND_MARKETING_CONFIG,
)

# Current canonical versions written on new writes
CURRENT_VERSIONS: Dict[str, int] = {
    KIND_MANIFEST: 1,
    KIND_TICKET: 1,
    KIND_GATE_ENTRY: 1,
    KIND_GOVERNANCE: 1,
    KIND_USERS: 1,
    KIND_COMPANY_PROFILE: 1,
    KIND_MARKETING_CONFIG: 1,
}

# Supported versions accepted by loaders
SUPPORTED_VERSIONS: Dict[str, Tuple[int, ...]] = {
    KIND_MANIFEST: (1,),
    KIND_TICKET: (1,),
    KIND_GATE_ENTRY: (1,),
    KIND_GOVERNANCE: (1,),
    KIND_USERS: (1,),
    KIND_COMPANY_PROFILE: (1,),
    KIND_MARKETING_CONFIG: (1,),
}

# Critical files that must fail closed on unknown/higher version
CRITICAL_KINDS: frozenset[str] = frozenset(
    {
        KIND_MANIFEST,
        KIND_TICKET,
        KIND_GATE_ENTRY,
        KIND_GOVERNANCE,
        KIND_USERS,
    }
)

# Config files that warn and degrade to defaults on unknown/higher version
CONFIG_KINDS: frozenset[str] = frozenset(
    {
        KIND_COMPANY_PROFILE,
        KIND_MARKETING_CONFIG,
    }
)

# Empty registry for future migrations: {kind: {(from_ver, to_ver): migration_func}}
MIGRATIONS: Dict[str, Dict[Tuple[int, int], Callable[[Dict[str, Any]], Dict[str, Any]]]] = {}

# Deduplicated logging tracker: log once per file kind when missing version
_WARNED_MISSING_KINDS: Set[str] = set()


class SchemaVersionError(ValueError):
    """Raised when an unsupported or higher schema version is encountered in a critical entity."""

    def __init__(
        self,
        kind: str,
        found_version: Any,
        supported: Sequence[int],
        source: str | os.PathLike[str],
    ) -> None:
        self.kind = kind
        self.found_version = found_version
        self.supported = tuple(supported)
        self.source = str(source)
        super().__init__(
            f"Unsupported schema_version {found_version!r} for {kind} in '{self.source}'. "
            f"Supported versions: {list(self.supported)}"
        )


def check_schema_version(
    kind: str,
    data: Mapping[str, Any],
    source: str | os.PathLike[str] = "<unknown>",
) -> int:
    """Validate the schema version of a loaded record.

    Rules:
    - Missing version: treat as v1 and log a warning once per file kind.
    - Supported version: return the version integer.
    - Unknown or higher version:
        - For critical entities (manifest, ticket, gate_entry, governance, users):
          FAIL CLOSED by raising SchemaVersionError.
        - For configs (company_profile, marketing_config):
          log warning and return -1 to signal fallback to defaults.
    """
    if kind not in ALL_KINDS:
        raise ValueError(f"Unknown kind {kind!r} (known: {ALL_KINDS})")

    # Extract version field (prefer schema_version, fallback to version)
    raw_ver: Any = None
    if "schema_version" in data:
        raw_ver = data["schema_version"]
    elif "version" in data:
        raw_ver = data["version"]

    if raw_ver is None:
        if kind not in _WARNED_MISSING_KINDS:
            logger.warning(
                "schema_version missing in %s (%s) — treating as v1",
                kind,
                source,
            )
            _WARNED_MISSING_KINDS.add(kind)
        return 1

    supported = SUPPORTED_VERSIONS.get(kind, (1,))

    # Coerce integer
    try:
        ver_int = int(raw_ver)
    except (ValueError, TypeError):
        ver_int = -999

    if ver_int in supported:
        return ver_int

    # Higher or unknown version
    if kind in CRITICAL_KINDS:
        raise SchemaVersionError(kind, raw_ver, supported, source)

    # Config files: warn and degrade to defaults
    logger.warning(
        "Unsupported schema_version %r for %s in %s (supported: %s) — falling back to defaults",
        raw_ver,
        kind,
        source,
        list(supported),
    )
    return -1


def stamp_schema_version(kind: str, data: MutableMapping[str, Any]) -> None:
    """Stamp the current schema_version on write into data."""
    if kind not in ALL_KINDS:
        raise ValueError(f"Unknown kind {kind!r} (known: {ALL_KINDS})")

    target_ver = CURRENT_VERSIONS[kind]
    data["schema_version"] = target_ver

    # For governance and users, keep "version": 1 reconciled and aligned
    if kind in (KIND_GOVERNANCE, KIND_USERS):
        data["version"] = target_ver


def reset_warned_kinds() -> None:
    """Reset the deduplication tracker (useful for testing warning behavior)."""
    _WARNED_MISSING_KINDS.clear()


__all__ = [
    "ALL_KINDS",
    "CONFIG_KINDS",
    "CRITICAL_KINDS",
    "CURRENT_VERSIONS",
    "KIND_COMPANY_PROFILE",
    "KIND_GATE_ENTRY",
    "KIND_GOVERNANCE",
    "KIND_MANIFEST",
    "KIND_MARKETING_CONFIG",
    "KIND_TICKET",
    "KIND_USERS",
    "MIGRATIONS",
    "SUPPORTED_VERSIONS",
    "SchemaVersionError",
    "check_schema_version",
    "reset_warned_kinds",
    "stamp_schema_version",
]
