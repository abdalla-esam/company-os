#!/usr/bin/env python3
"""users.py — Sovereign User Identity & Credential Registry.

Provides sovereign, local user authentication, PBKDF2-HMAC-SHA256 password hashing,
role-based and sector-scoped authority mapping, and capability token issuance.

Operates without external database dependencies; user identities are safely stored
and versioned in `config/users.json`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from capsule_engine.capabilities import (
    ALL_CAPABILITIES,
    CAPABILITY_ALL,
    CAPABILITY_CAPSULE_APPROVE,
    CAPABILITY_CAPSULE_CREATE,
    CAPABILITY_CAPSULE_REJECT,
    CAPABILITY_DOMAIN_READ,
    CAPABILITY_READ,
    CAPABILITY_TICKET_CREATE,
    SCOPED_CAPABILITIES,
    mint_key,
    mint_root_key,
)
from shared_kernel.paths import USERS_CONFIG_PATH
from shared_kernel.atomic_io import update_json

logger = logging.getLogger("shared_kernel.users")

ITERATIONS = 100_000
ALGORITHM = "pbkdf2_sha256"

#: Where the first-boot admin password lives when no env var supplies one.
BOOTSTRAP_PASSWORD_ENV = "AGENCY_BOOTSTRAP_ADMIN_PASSWORD"
BOOTSTRAP_PASSWORD_FILE = USERS_CONFIG_PATH.parent / ".bootstrap_admin_password"


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a cryptographically secure random salt."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ITERATIONS,
    ).hex()
    return f"{ALGORITHM}${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plain password against a stored PBKDF2 hash."""
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    try:
        iters = int(parts[1])
        salt = parts[2]
        expected_digest = parts[3]
        calculated_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iters,
        ).hex()
        return hmac.compare_digest(calculated_digest, expected_digest)
    except Exception as exc:
        logger.warning("verify_password failed with error: %s", exc)
        return False


def _bootstrap_admin_password() -> tuple[str, bool]:
    """Resolve the first admin's password. Returns ``(password, generated)``.

    Never falls back to a shipped default — a known password baked into the
    source is a company-wide skeleton key. Precedence:

    1. ``AGENCY_BOOTSTRAP_ADMIN_PASSWORD`` (operator's explicit choice);
    2. a previously generated ``config/.bootstrap_admin_password`` (0600);
    3. a fresh ``secrets.token_urlsafe(24)`` written to that file.
    """
    env_value = os.getenv(BOOTSTRAP_PASSWORD_ENV, "").strip()
    if env_value:
        return env_value, False
    try:
        existing = BOOTSTRAP_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing, False
    generated = secrets.token_urlsafe(24)
    try:
        BOOTSTRAP_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOOTSTRAP_PASSWORD_FILE.write_text(generated + "\n", encoding="utf-8")
        os.chmod(BOOTSTRAP_PASSWORD_FILE, 0o600)
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("could not persist bootstrap admin password: %s", exc)
    return generated, True


def _default_bootstrap_users() -> List[Dict[str, Any]]:
    """Generate the first sovereign admin user when the registry is empty."""
    bootstrap_pass, generated = _bootstrap_admin_password()
    if generated:
        logger.warning(
            "No %s set — generated a random bootstrap admin password. Read it "
            "from %s and rotate it before exposing this server.",
            BOOTSTRAP_PASSWORD_ENV,
            BOOTSTRAP_PASSWORD_FILE,
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    return [
        {
            "username": "abdallah",
            "email": "abdallah@agency.sovereign",
            "full_name": "Abdallah Esam (Sovereign Root)",
            "password_hash": hash_password(bootstrap_pass),
            "role": "admin",
            "authorized_sectors": ["*"],
            "capabilities": ["*"],
            "is_active": True,
            "created_at": now_iso,
        },
    ]


def users_config_path(custom_path: Optional[str | os.PathLike[str]] = None) -> Path:
    """Resolve users configuration file: explicit > USERS_CONFIG_PATH (if patched) > AGENCY_USERS_FILE > default."""
    if custom_path:
        return Path(custom_path).resolve()
    from shared_kernel import paths

    if USERS_CONFIG_PATH != paths.USERS_CONFIG_PATH:
        return Path(USERS_CONFIG_PATH).resolve()
    env = os.environ.get("AGENCY_USERS_FILE", "").strip()
    if env:
        return Path(env).resolve()
    return Path(USERS_CONFIG_PATH).resolve()


def load_users(path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    """Load users from config/users.json or initialize defaults if missing."""
    target_path = users_config_path(path)
    if not target_path.exists():
        default_users = _default_bootstrap_users()
        save_users(default_users, target_path)
        return default_users
    try:
        data = json.loads(target_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to load users from %s: %s", target_path, exc)
        return []

    if isinstance(data, dict):
        from shared_kernel.schema_version import KIND_USERS, check_schema_version

        check_schema_version(KIND_USERS, data, target_path)
        if "users" in data:
            return data["users"]
    if isinstance(data, list):
        return data
    return []


def save_users(users: List[Dict[str, Any]], path: Optional[Path | str] = None) -> None:
    """Safely and atomically persist users list to config/users.json."""
    from shared_kernel.schema_version import KIND_USERS, stamp_schema_version

    target_path = users_config_path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    def _mutate(existing: Any) -> Any:
        state = {
            "version": 1,
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "users": users,
        }
        stamp_schema_version(KIND_USERS, state)
        return state

    update_json(
        target_path,
        _mutate,
        default={},
        indent=2,
        ensure_ascii=False,
        trailing_newline=False,
    )


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Find user by username (case-insensitive)."""
    uname = (username or "").strip().lower()
    for u in load_users():
        if u.get("username", "").strip().lower() == uname:
            return u
    return None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Find user by email (case-insensitive)."""
    target = (email or "").strip().lower()
    if not target:
        return None
    for u in load_users():
        if str(u.get("email", "")).strip().lower() == target:
            return u
    return None


def resolve_user(identifier: str) -> Optional[Dict[str, Any]]:
    """Resolve a login identifier to a user — username first, then email.

    The login contract has always accepted "username or email"
    (``LoginRequest.username``); this is where that promise is kept.
    """
    return get_user_by_username(identifier) or get_user_by_email(identifier)


def authenticate_user(identifier: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate credentials (by username *or* email) against the registry."""
    user = resolve_user(identifier)
    if not user:
        return None
    if not user.get("is_active", True):
        logger.warning("Authentication refused for inactive user: %s", identifier)
        return None
    stored_hash = user.get("password_hash", "")
    if verify_password(password, stored_hash):
        return user
    return None


def mint_user_token(user: Dict[str, Any], ttl_seconds: int = 30 * 86400) -> str:
    """Issue a signed sovereign capability token for an authenticated user."""
    username = user.get("username", "unknown")
    role = user.get("role", "operator")
    authorized_sectors = user.get("authorized_sectors", [])
    raw_caps = user.get("capabilities", [])

    is_admin = role == "admin" or authorized_sectors == ["*"] or "*" in raw_caps or "*" in authorized_sectors
    if is_admin:
        return mint_root_key(username, ttl_seconds=ttl_seconds)

    # Read visibility is **per sector** for a non-admin. There is deliberately
    # no engine-wide ``factory:read``: that grant reaches /api/stats,
    # /api/telemetry/costs, /api/radar/tasks, /api/memory/feedback across every
    # domain, which is exactly the leak this slice closes. A non-admin sees the
    # reads of the sectors it was granted and no others.
    grants: List[str] = []
    for sector in authorized_sectors:
        sec = sector.strip().lower()
        if not sec or sec == "*":
            continue
        grants.append(f"{CAPABILITY_DOMAIN_READ}:{sec}")
        if role in ("admin", "sector_lead"):
            grants.append(f"{CAPABILITY_TICKET_CREATE}:{sec}")
            grants.append(f"{CAPABILITY_CAPSULE_APPROVE}:{sec}")
            grants.append(f"{CAPABILITY_CAPSULE_REJECT}:{sec}")
            grants.append(f"{CAPABILITY_CAPSULE_CREATE}:{sec}")
        elif role == "operator":
            grants.append(f"{CAPABILITY_TICKET_CREATE}:{sec}")
            grants.append(f"{CAPABILITY_CAPSULE_CREATE}:{sec}")

    for cap in raw_caps:
        if cap == "*" or not cap:
            continue
        if cap == CAPABILITY_READ:
            # An explicit engine-wide read is dropped for a non-admin: it would
            # re-open every sector's reads. Sector visibility comes from the
            # ``domain:read`` grants added above; an admin already holds root.
            continue
        # If already formatted as action:verb:scope
        if cap.count(":") == 2:
            grants.append(cap)
        elif cap in SCOPED_CAPABILITIES:
            for sector in authorized_sectors:
                sec = sector.strip().lower()
                if sec and sec != "*":
                    grants.append(f"{cap}:{sec}")
        elif cap in ALL_CAPABILITIES:
            grants.append(cap)

    unique_grants = sorted(set(grants))
    return mint_key(username, capabilities=unique_grants, ttl_seconds=ttl_seconds)


def create_or_update_user(
    username: str,
    password: Optional[str] = None,
    email: Optional[str] = None,
    full_name: Optional[str] = None,
    role: str = "operator",
    authorized_sectors: Optional[List[str]] = None,
    capabilities: Optional[List[str]] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    """Create or update a user entry atomically under lock."""
    target_path = users_config_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    uname = username.strip().lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    result_user: Dict[str, Any] = {}

    def _mutate(existing_doc: Any) -> Any:
        users: List[Dict[str, Any]] = []
        if isinstance(existing_doc, dict) and "users" in existing_doc:
            users = list(existing_doc["users"])
        elif isinstance(existing_doc, list):
            users = list(existing_doc)
        elif not target_path.exists() and not existing_doc:
            users = list(_default_bootstrap_users())

        target = None
        for u in users:
            if u.get("username", "").strip().lower() == uname:
                target = u
                break

        if target is not None:
            if password:
                target["password_hash"] = hash_password(password)
            if email is not None:
                target["email"] = email
            if full_name is not None:
                target["full_name"] = full_name
            target["role"] = role
            if authorized_sectors is not None:
                target["authorized_sectors"] = authorized_sectors
            if capabilities is not None:
                target["capabilities"] = capabilities
            target["is_active"] = is_active
            target["updated_at"] = now_iso
            result_user.clear()
            result_user.update(target)
        else:
            new_user = {
                "username": uname,
                "email": email or f"{uname}@agency.sovereign",
                "full_name": full_name or uname.capitalize(),
                "password_hash": hash_password(password or secrets.token_urlsafe(16)),
                "role": role,
                "authorized_sectors": authorized_sectors or ["development"],
                # No engine-wide ``factory:read``: a non-admin's reads are
                # ``domain:read:<sector>``, minted from ``authorized_sectors``.
                "capabilities": capabilities or ["ticket:create"],
                "is_active": is_active,
                "created_at": now_iso,
            }
            users.append(new_user)
            result_user.clear()
            result_user.update(new_user)

        return {
            "version": 1,
            "updated_at": now_iso,
            "users": users,
        }

    update_json(
        target_path,
        _mutate,
        default={},
        indent=2,
        ensure_ascii=False,
        trailing_newline=False,
    )
    return result_user


def delete_user(username: str) -> bool:
    """Delete a user from the registry atomically under lock."""
    target_path = users_config_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    uname = username.strip().lower()
    deleted = False

    def _mutate(existing_doc: Any) -> Any:
        nonlocal deleted
        users: List[Dict[str, Any]] = []
        if isinstance(existing_doc, dict) and "users" in existing_doc:
            users = list(existing_doc["users"])
        elif isinstance(existing_doc, list):
            users = list(existing_doc)

        new_users = [u for u in users if u.get("username", "").strip().lower() != uname]
        if len(new_users) < len(users):
            deleted = True
            return {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "users": new_users,
            }
        return None

    update_json(
        target_path,
        _mutate,
        default={},
        indent=2,
        ensure_ascii=False,
        trailing_newline=False,
    )
    return deleted
