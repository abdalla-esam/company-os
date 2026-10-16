#!/usr/bin/env python3
"""mint_capability.py — operator CLI for signed capability keys.

The capability layer (``capsule_engine/capabilities.py``) can only verify keys;
someone has to *issue* them. This is that someone: a one-shot, offline command
that mints a key, prints it once, and forgets it. Nothing is stored — the file
system stays the database, and the key is the state.

The signing secret comes from the environment (``AGENCY_CAPABILITY_SECRET``) or
from a file (``--secret-file``); it is deliberately **not** accepted as a
command-line argument, because argv leaks into shell history and process lists.

Commands::

    # what can I grant?
    python3 tools/mint_capability.py capabilities

    # a human reviewer who may sign off, and nothing else
    python3 tools/mint_capability.py mint --subject abdallah \
        --capabilities capsule:approve,capsule:reject,gate:approve,gate:reject

    # a read-only dashboard key, valid for a week
    python3 tools/mint_capability.py mint --subject dashboard \
        --capabilities factory:read --ttl-days 7

    # a mobile client that reads (only) the marketing sector on /v1
    python3 tools/mint_capability.py mint --subject phone \
        --capabilities domain:read --scope marketing

    # the migration root key (every capability — use sparingly, rotate often)
    python3 tools/mint_capability.py mint --subject root --all --ttl-days 30

    # what does this key actually grant? (verifies the signature)
    python3 tools/mint_capability.py inspect cap.eyJ... .Zm9v...

Exit codes: 0 success, 2 refusal (bad usage, missing/weak secret, invalid key).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
for _p in (str(REPO_ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capsule_engine.capabilities import (  # noqa: E402
    ALL_CAPABILITIES,
    CAPABILITY_ALL,
    CAPABILITY_ENV,
    DEFAULT_TTL_SECONDS,
    GLOBAL_CAPABILITIES,
    MAX_TTL_SECONDS,
    SCOPED_CAPABILITIES,
    SCOPE_ALL,
    CapabilityKey,
    mint_key,
    resolve_secret,
    verify_key,
)
from capsule_engine.exceptions import CapabilityError  # noqa: E402

DAY = 24 * 60 * 60


def _fail(message: str) -> int:
    """Print a refusal as one JSON line on stderr; return the refusal code."""
    print(
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        file=sys.stderr,
    )
    return 2


def _secret_bytes(args: argparse.Namespace) -> bytes | None:
    """Resolve the signing secret: --secret-file, else the environment."""
    if args.secret_file:
        path = Path(args.secret_file).expanduser()
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CapabilityError(f"cannot read secret file {path}: {exc}") from exc
        if not raw:
            raise CapabilityError(f"secret file {path} is empty")
        return raw.encode("utf-8")
    return resolve_secret()


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _describe(key: CapabilityKey) -> dict:
    return {
        "subject": key.subject,
        "capabilities": sorted(key.capabilities),
        "is_root": key.is_root,
        "key_id": key.key_id,
        "issued_at": _iso(key.issued_at),
        "expires_at": _iso(key.expires_at),
    }


def cmd_capabilities(_args: argparse.Namespace) -> int:
    """List the closed vocabulary, so an operator quotes real names."""
    print(
        json.dumps(
            {
                "ok": True,
                "capabilities": sorted(ALL_CAPABILITIES),
                "branch_scoped": sorted(SCOPED_CAPABILITIES),
                "engine_wide": sorted(GLOBAL_CAPABILITIES),
                "scope_all": SCOPE_ALL,
                "scope_rule": (
                    "A branch-scoped capability MUST carry --scope <domain> "
                    "(or --scope '*' for every branch); without one it is "
                    "refused, because a scoped capability with no scope grants "
                    "nothing."
                ),
                "root_grant": CAPABILITY_ALL,
                "secret_env": CAPABILITY_ENV,
                "default_ttl_days": DEFAULT_TTL_SECONDS // DAY,
                "max_ttl_days": MAX_TTL_SECONDS // DAY,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_mint(args: argparse.Namespace) -> int:
    """Mint one key and print it exactly once."""
    if args.all and args.capabilities:
        return _fail("use either --all or --capabilities, not both")
    if not args.all and not args.capabilities:
        return _fail("nothing to grant: pass --capabilities a,b or --all")
    if args.ttl_days <= 0:
        return _fail("--ttl-days must be > 0 (a key that never expires is refused)")
    if args.ttl_days * DAY > MAX_TTL_SECONDS:
        return _fail(f"--ttl-days must be <= {MAX_TTL_SECONDS // DAY}")

    granted: list[str]
    if args.all:
        granted = [CAPABILITY_ALL]
        if args.scope:
            print(
                "⚠️  --scope is ignored for a root key: it already authorises "
                "every branch.",
                file=sys.stderr,
            )
    else:
        names = [part.strip() for part in args.capabilities.split(",") if part.strip()]
        if not names:
            return _fail("--capabilities did not name a single capability")
        scope = (args.scope or "").strip()
        scoped_seen = False
        granted = []
        for name in names:
            if name in SCOPED_CAPABILITIES:
                scoped_seen = True
                if not scope:
                    return _fail(
                        f"'{name}' acts on a branch and needs --scope <domain> "
                        f"(or --scope '{SCOPE_ALL}' for every branch). A scoped "
                        "capability with no scope grants nothing, so it is "
                        "refused rather than accepted as universal."
                    )
                granted.append(f"{name}:{scope}")
            else:
                granted.append(name)
        if scope and not scoped_seen:
            print(
                f"⚠️  --scope {scope!r} had no effect: every capability named is "
                "engine-wide, not branch-scoped.",
                file=sys.stderr,
            )

    try:
        secret = _secret_bytes(args)
        token = mint_key(
            args.subject,
            granted,
            secret=secret,
            ttl_seconds=args.ttl_days * DAY,
        )
        key = verify_key(token, secret=secret)  # prove what we just issued
    except CapabilityError as exc:
        return _fail(str(exc))

    print(
        json.dumps(
            {"ok": True, "key": token, **_describe(key)},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        "⚠️  this key is a credential: copy it once and keep it out of shell "
        "history, logs, and chat.",
        file=sys.stderr,
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Verify a key's signature and print what it grants (no secret echoed)."""
    try:
        key = verify_key(args.key, secret=_secret_bytes(args))
    except CapabilityError as exc:
        return _fail(str(exc))
    print(json.dumps({"ok": True, **_describe(key)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mint_capability.py",
        description="Mint and inspect signed capability keys for the Sovereign Agency OS.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--secret-file",
            default=None,
            help=(
                "file holding the signing secret (default: the "
                f"{CAPABILITY_ENV} environment variable)"
            ),
        )

    caps = sub.add_parser("capabilities", help="list the closed capability vocabulary")
    add_common(caps)
    caps.set_defaults(func=cmd_capabilities)

    mint = sub.add_parser("mint", help="mint a signed capability key")
    add_common(mint)
    mint.add_argument("--subject", required=True, help="who holds the key")
    mint.add_argument(
        "--capabilities",
        default="",
        help="comma-separated capability names (see: capabilities)",
    )
    mint.add_argument(
        "--all",
        action="store_true",
        help=f"grant every capability ({CAPABILITY_ALL}) — root key, use sparingly",
    )
    mint.add_argument(
        "--scope",
        default=None,
        help=(
            "branch a branch-scoped capability is confined to (e.g. marketing), "
            f"or '{SCOPE_ALL}' for every branch. Required for: "
            f"{', '.join(sorted(SCOPED_CAPABILITIES))}"
        ),
    )
    mint.add_argument(
        "--ttl-days",
        type=int,
        default=DEFAULT_TTL_SECONDS // DAY,
        help="validity window in days (default: %(default)s)",
    )
    mint.set_defaults(func=cmd_mint)

    inspect = sub.add_parser("inspect", help="verify a key and print its claims")
    add_common(inspect)
    inspect.add_argument("key", help="the capability key string to verify")
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CapabilityError as exc:  # belt and braces: never traceback a refusal
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
