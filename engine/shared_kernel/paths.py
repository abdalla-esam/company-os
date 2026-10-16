"""paths.py — Canonical Sovereign 4-Layer Path Resolution.

Provides centralized, robust, fail-safe path resolution for:
- engine/ (Immutable application code)
- data/   (Stateful storage: domains, capsules, assets, governance, index)
- config/ (Central system configuration)
- ops/    (Operations, boot scripts, and CLI tools)
"""

from __future__ import annotations

import os
from pathlib import Path


def get_repo_root() -> Path:
    """Resolve the repository root directory."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "config").is_dir() and (parent / "engine").is_dir():
            return parent
    return p.parents[2] if len(p.parents) > 2 else p.parents[1]


REPO_ROOT = get_repo_root()

# Auto-load repository .env safely
_env_file = REPO_ROOT / ".env"
if _env_file.is_file():
    try:
        import dotenv
        dotenv.load_dotenv(_env_file)
    except ImportError:
        for line in _env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

ENGINE_ROOT = REPO_ROOT / "engine"
DATA_ROOT = REPO_ROOT / "data"
CONFIG_ROOT = REPO_ROOT / "config"
OPS_ROOT = REPO_ROOT / "ops"

USERS_CONFIG_PATH = Path(
    os.getenv("AGENCY_USERS_FILE", CONFIG_ROOT / "users.json")
).resolve()

# Canonical Storage Paths with backward-compatible fallbacks
DOMAINS_DIR = Path(
    os.getenv("DOMAINS_DIR", DATA_ROOT / "domains" if (DATA_ROOT / "domains").exists() else REPO_ROOT / "domains")
).resolve()

CAPSULES_SPOOL = Path(
    os.getenv(
        "CAPSULES_SPOOL",
        DATA_ROOT / "capsules" / "spool"
        if (DATA_ROOT / "capsules" / "spool").exists()
        else REPO_ROOT / "capsules" / "spool",
    )
).resolve()

CAPSULES_SANDBOX = Path(
    os.getenv(
        "CAPSULES_SANDBOX",
        DATA_ROOT / "capsules" / "sandbox"
        if (DATA_ROOT / "capsules" / "sandbox").exists()
        else REPO_ROOT / "capsules" / "sandbox",
    )
).resolve()

ASSETS_VAULT = Path(
    os.getenv(
        "AGENCY_ASSETS_VAULT_DIR",
        os.getenv(
            "ASSETS_VAULT_DIR",
            DATA_ROOT / "assets" / "vault"
            if (DATA_ROOT / "assets" / "vault").exists()
            else REPO_ROOT / "assets" / "vault",
        ),
    )
).resolve()

GATE_QUEUE_DIR = Path(
    os.getenv(
        "GATE_QUEUE_DIR",
        DATA_ROOT / "governance" / "approval_gates" / "gate_queue"
        if (DATA_ROOT / "governance" / "approval_gates" / "gate_queue").exists()
        else REPO_ROOT / "governance" / "approval_gates" / "gate_queue",
    )
).resolve()

READ_INDEX_PATH = Path(
    os.getenv(
        "READ_INDEX_PATH",
        DATA_ROOT / "index" / ".read_index.sqlite3",
    )
).resolve()

# Durable work queue (orchestration/work_queue.py): pending/claimed/done/...
# JSON items under one directory — the filesystem is the queue.
WORK_QUEUE_DIR = Path(
    os.getenv("AGENCY_WORK_QUEUE_DIR", DATA_ROOT / "queue")
).resolve()

# --------------------------------------------------------------------------- #
# Cumulative memory, dedup index and engine policy
# --------------------------------------------------------------------------- #
# The filesystem is the single source of truth for operational memory too:
# - feedback_history.jsonl  append-only rejection/audit ledger (cross-domain)
# - dedup_index.jsonl       append-only proposal fingerprints (vectors/hashes)
# Both are plain JSONL: human-inspectable, greppable, rebuildable into the
# SQLite read cache. Nothing here is a database dependency.

MEMORY_DIR = Path(
    os.getenv("AGENCY_MEMORY_DIR", DATA_ROOT / "memory")
).resolve()

FEEDBACK_LEDGER_PATH = Path(
    os.getenv("AGENCY_MEMORY_FILE", MEMORY_DIR / "feedback_history.jsonl")
).resolve()

DEDUP_INDEX_PATH = Path(
    os.getenv("AGENCY_DEDUP_FILE", MEMORY_DIR / "dedup_index.jsonl")
).resolve()

# Central engine policy: dedup window, gate templates, memory, trace and
# spend knobs. Missing file is not an error for the policy loader (built-in
# defaults); the spend gate treats a missing spend section as fail-closed.
ENGINE_CONFIG_PATH = Path(
    os.getenv("AGENCY_ENGINE_CONFIG", CONFIG_ROOT / "engine.json")
).resolve()

# Financial spend ledger & audit (capsule_engine.spend_control):
# append-only JSONL with one line per authorize / settle / refusal /
# subscription event. The money gate refuses to spend when this file
# cannot be read or written.
FINANCE_DIR = Path(
    os.getenv("AGENCY_FINANCE_DIR", DATA_ROOT / "finance")
).resolve()

SPEND_LEDGER_PATH = Path(
    os.getenv(
        "AGENCY_SPEND_LEDGER",
        os.getenv(
            "AGENCY_FINANCE_LEDGER_FILE",
            FINANCE_DIR / "usage_ledger.jsonl",
        ),
    )
).resolve()

