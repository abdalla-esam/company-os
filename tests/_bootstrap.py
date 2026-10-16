"""Test bootstrap: repository import paths + an isolated scratch vault.

Imported FIRST by every test module, before any engine import. The engine's
path layer (``shared_kernel.paths``) resolves directories from the
environment *at import time*, so the env vars below must be in place before
anything imports it — they point every read/write at a temporary vault and
never at the repository's real ``data/`` directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "engine", REPO_ROOT, REPO_ROOT / "data"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


def _is_ignorable_real_file(name: str) -> bool:
    """True only for real-tree paths that are provably NOT the engine's and so
    may change without any test touching the repository.

    - ``*.tmp`` — the atomic writers' transient staging names. A production
      process may create/rename one at any moment; a test that reached the real
      tree would still be caught by the renamed destination it writes.
    - ``.DS_Store`` — a macOS Finder metadata file. The engine never reads or
      writes it (its own backup tool lists it as a non-engine artifact), so a
      change can only come from Finder/Spotlight, never from a test.
    """
    return name.endswith(".tmp") or name == ".DS_Store"


def take_repo_snapshot() -> dict[str, tuple[int, int, str]]:
    """Capture ``(mtime_ns, size, sha256)`` of every file under config/ and data/.

    Modification *time* is compared alongside the content digest: a writer that
    rewrites a file with byte-identical content (for example the pulse scan-state
    ``{}``) leaves the hash untouched but still bumps the mtime, so a hash-only
    guard would miss a real leak.
    """
    snapshot: dict[str, tuple[int, int, str]] = {}
    for top_name in ("config", "data"):
        top_dir = REPO_ROOT / top_name
        if not top_dir.is_dir():
            continue
        for p in top_dir.rglob("*"):
            if p.is_file() and not _is_ignorable_real_file(p.name):
                rel = str(p.relative_to(REPO_ROOT))
                try:
                    st = p.stat()
                    digest = hashlib.sha256(p.read_bytes()).hexdigest()
                except OSError:
                    continue
                snapshot[rel] = (st.st_mtime_ns, st.st_size, digest)
    return snapshot


def isolation_env() -> dict[str, str]:
    """The canonical temp-vault overrides, re-derived from this module's constants.

    Every test that spawns a child process must pass these (never the ambient
    environment alone) so the child resolves every engine path inside ``VAULT``
    and can never reach the repository's real ``config/`` or ``data/``.
    """
    return {
        "CAPSULES_SPOOL": str(SPOOL),
        "CAPSULES_SANDBOX": str(SANDBOX),
        "DOMAINS_DIR": str(DOMAINS),
        "ASSETS_VAULT_DIR": str(ASSETS_VAULT),
        "READ_INDEX_PATH": str(INDEX_FILE),
        "AGENCY_INDEX_FILE": str(INDEX_FILE),
        "GATE_QUEUE_DIR": str(GATE_QUEUE),
        "AGENCY_WORK_QUEUE_DIR": str(WORK_QUEUE),
        "AGENCY_MEMORY_DIR": str(MEMORY),
        "AGENCY_MEMORY_FILE": str(MEMORY / "feedback_history.jsonl"),
        "AGENCY_DEDUP_FILE": str(MEMORY / "dedup_index.jsonl"),
        "AGENCY_GOVERNANCE_FILE": str(VAULT / "governance.json"),
        "AGENCY_USERS_FILE": str(VAULT / "users.json"),
        "AGENCY_ENGINE_CONFIG": str(VAULT / "engine.json"),
        "AGENCY_BRAND_PROFILE_FILE": str(VAULT / "company_profile.json"),
        "AGENCY_MARKETING_CONFIG_FILE": str(VAULT / "marketing_config.json"),
        "AGENCY_TASK_TYPES_DIR": str(_TASK_TYPES_DIR),
        "AGENCY_GEAR_PROBE_CACHE": str(VAULT / "gear_probes.json"),
        "AGENCY_BOOTSTRAP_ADMIN_PASSWORD": "test_bootstrap_admin_secret",
        "AGENCY_FINANCE_DIR": str(FINANCE),
        "AGENCY_SPEND_LEDGER": str(FINANCE / "usage_ledger.jsonl"),
    }


# Take snapshot BEFORE any test imports or modifies anything
INITIAL_REPO_SNAPSHOT = take_repo_snapshot()

# Isolated scratch vault (one per test run).
VAULT = Path(tempfile.mkdtemp(prefix="so_test_vault_")).resolve()
SPOOL = VAULT / "spool"
SANDBOX = VAULT / "sandbox"
DOMAINS = VAULT / "domains"
GATE_QUEUE = VAULT / "gate_queue"
WORK_QUEUE = VAULT / "queue"
MEMORY = VAULT / "memory"
INDEX_FILE = VAULT / "read_index.sqlite3"
ASSETS_VAULT = VAULT / "assets" / "vault"
FINANCE = VAULT / "finance"

for _d in (SPOOL, SANDBOX, DOMAINS, GATE_QUEUE, WORK_QUEUE, MEMORY, ASSETS_VAULT, FINANCE):
    _d.mkdir(parents=True, exist_ok=True)

import shutil

for _cfg_name in ("engine.json", "company_profile.json", "marketing_config.json"):
    _src = REPO_ROOT / "config" / _cfg_name
    if _src.is_file():
        shutil.copy2(_src, VAULT / _cfg_name)

# governance_path() falls back to the repository's real config/governance.json
# whenever AGENCY_GOVERNANCE_FILE points at a file that does not exist yet —
# so a fresh vault would leak governance WRITES into config/ (the same trap
# test_governance_atomic / test_unified_approval document). Seed the vault copy
# here, once, so no test — run alone or in the suite — can ever touch the
# repository's real governance file.
_GOV_FILE = VAULT / "governance.json"
if not _GOV_FILE.is_file():
    _GOV_FILE.write_text(
        json.dumps(
            {"schema_version": 1, "version": 1, "updated_at": None, "domains": {}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

# Task types are git-reviewed configuration (config/task_types/). Tests read a
# vault copy through AGENCY_TASK_TYPES_DIR, never the repository file, so the
# isolation guard keeps proving that no test touches real config/ or data/.
_TASK_TYPES_SRC = REPO_ROOT / "config" / "task_types"
_TASK_TYPES_DIR = VAULT / "task_types"
_TASK_TYPES_DIR.mkdir(parents=True, exist_ok=True)
if _TASK_TYPES_SRC.is_dir():
    shutil.copytree(_TASK_TYPES_SRC, _TASK_TYPES_DIR, dirs_exist_ok=True)

# The spend gate's ledger lives in the vault too (see ``isolation_env``): no
# test may ever append a spend event to the repository's real data/finance/.
os.environ.update(isolation_env())
# The index kill switch must be explicit *and already present* so a repo
# .env value can't re-introduce it (load_dotenv never overrides existing keys).
# Individual tests may flip it to "off" and must restore it.
os.environ["AGENCY_INDEX"] = "on"
