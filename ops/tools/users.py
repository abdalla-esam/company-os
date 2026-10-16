#!/usr/bin/env python3
"""users.py — Sovereign User Identity & Credential Management CLI.

Usage:
    python3 ops/tools/users.py list
    python3 ops/tools/users.py create --username <name> --password <pass> --role <role> --sectors <sectors>
    python3 ops/tools/users.py reset-password --username <name> --password <new_pass>
    python3 ops/tools/users.py mint-token --username <name>
    python3 ops/tools/users.py delete --username <name>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure engine is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.shared_kernel.users import (
    authenticate_user,
    create_or_update_user,
    delete_user,
    get_user_by_username,
    load_users,
    mint_user_token,
)


def cmd_list(args: argparse.Namespace) -> int:
    users = load_users()
    print("=" * 80)
    print(f"{'USERNAME':<16} {'ROLE':<14} {'SECTORS':<20} {'ACTIVE':<8} {'FULL NAME'}")
    print("=" * 80)
    for u in users:
        uname = u.get("username", "")
        role = u.get("role", "")
        secs = ",".join(u.get("authorized_sectors", []))
        active = "YES" if u.get("is_active", True) else "NO"
        full = u.get("full_name", "")
        print(f"{uname:<16} {role:<14} {secs:<20} {active:<8} {full}")
    print("=" * 80)
    print(f"Total Users: {len(users)}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    sectors = [s.strip() for s in (args.sectors or "development").split(",") if s.strip()]
    caps = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()] or None
    user = create_or_update_user(
        username=args.username,
        password=args.password,
        email=args.email,
        full_name=args.full_name,
        role=args.role,
        authorized_sectors=sectors,
        capabilities=caps,
    )
    print(f"✓ User '{user['username']}' created/updated successfully.")
    print(f"  Role: {user['role']}")
    print(f"  Authorized Sectors: {user['authorized_sectors']}")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    user = get_user_by_username(args.username)
    if not user:
        print(f"✗ User '{args.username}' not found.", file=sys.stderr)
        return 1
    create_or_update_user(
        username=args.username,
        password=args.password,
        role=user.get("role", "operator"),
        authorized_sectors=user.get("authorized_sectors", []),
    )
    print(f"✓ Password reset for user '{args.username}'.")
    return 0


def cmd_mint_token(args: argparse.Namespace) -> int:
    user = get_user_by_username(args.username)
    if not user:
        print(f"✗ User '{args.username}' not found.", file=sys.stderr)
        return 1
    token = mint_user_token(user, ttl_seconds=int(args.ttl))
    print(f"✓ Minted Sovereign Capability Token for '{args.username}':")
    print("-" * 80)
    print(token)
    print("-" * 80)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if delete_user(args.username):
        print(f"✓ User '{args.username}' deleted successfully.")
        return 0
    print(f"✗ User '{args.username}' not found.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Sovereign User Identity Management CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List all registered sovereign users")
    p_list.set_defaults(func=cmd_list)

    # create
    p_create = subparsers.add_parser("create", help="Create or update a sovereign user")
    p_create.add_argument("--username", required=True, help="Unique username")
    p_create.add_argument("--password", required=True, help="Raw initial password")
    p_create.add_argument("--role", default="sector_lead", choices=["admin", "sector_lead", "operator", "auditor"])
    p_create.add_argument("--sectors", default="development", help="Comma-separated sectors (or '*')")
    p_create.add_argument("--email", default=None, help="User email address")
    p_create.add_argument("--full-name", default=None, help="Display name")
    p_create.add_argument("--capabilities", default="", help="Optional explicit capabilities")
    p_create.set_defaults(func=cmd_create)

    # reset-password
    p_reset = subparsers.add_parser("reset-password", help="Reset password for an existing user")
    p_reset.add_argument("--username", required=True)
    p_reset.add_argument("--password", required=True)
    p_reset.set_defaults(func=cmd_reset_password)

    # mint-token
    p_mint = subparsers.add_parser("mint-token", help="Mint a signed capability token for a user")
    p_mint.add_argument("--username", required=True)
    p_mint.add_argument("--ttl", default=30 * 86400, type=int, help="TTL in seconds")
    p_mint.set_defaults(func=cmd_mint_token)

    # delete
    p_del = subparsers.add_parser("delete", help="Delete a user")
    p_del.add_argument("--username", required=True)
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
