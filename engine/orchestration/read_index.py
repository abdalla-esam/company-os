#!/usr/bin/env python3
"""read_index.py — a read-only SQLite cache over the file-as-state vault.

**The files are the truth; this index is a phone book.**

Scaling Brick 3: counting, searching and aggregating thousands of tickets and
capsules must not mean opening thousands of files. This module maintains a
single local SQLite database (stdlib only, no SaaS, no daemon) that mirrors
just the queryable fields of every ticket and capsule. It is:

- **rebuildable**: ``rebuild()`` re-derives the whole index from the files —
  deleting the database file is always safe;
- **incremental**: ``apply_pulse_report()`` updates exactly the capsules a
  pulse touched (same selection the incremental scanner produced), so keeping
  it fresh costs near zero;
- **fail-closed by freshness**: a stale or inconsistent index is detected via
  the spool's scan-state signature and rebuilt from files automatically —
  callers never read *facts* from the index, only *views* of them;
- **switchable**: ``AGENCY_INDEX=off`` disables it entirely and every
  ``ensure()`` returns False — callers fall back to reading files.

Nothing here writes to the vault. This module never mutates a ticket or a
capsule; it only reads and caches.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("orchestration.read_index")

# Resolve import paths before ``shared_kernel.paths`` (the same trap
# ``capsule_pulse.py`` documents): a script-mode execution puts the script's own
# directory on sys.path[0], so the canonical resolver would be skipped and the
# fallback below would pin the repository's real spool/index — ignoring the
# sovereign ``CAPSULES_SPOOL``/``READ_INDEX_PATH`` overrides.
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ENGINE_ROOT.parent
for _p in (str(_ENGINE_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from shared_kernel.paths import CAPSULES_SPOOL, DOMAINS_DIR, ENGINE_ROOT, READ_INDEX_PATH, REPO_ROOT
except Exception:
    ENGINE_ROOT = _ENGINE_ROOT
    REPO_ROOT = _REPO_ROOT
    DOMAINS_DIR = Path(os.environ.get("DOMAINS_DIR", _REPO_ROOT / "data" / "domains"))
    CAPSULES_SPOOL = Path(
        os.environ.get("CAPSULES_SPOOL", _REPO_ROOT / "data" / "capsules" / "spool")
    )
    READ_INDEX_PATH = Path(
        os.environ.get(
            "READ_INDEX_PATH", _REPO_ROOT / "data" / "index" / ".read_index.sqlite3"
        )
    )

ROOT = REPO_ROOT
SPOOL_DIR = CAPSULES_SPOOL
DOMAINS_DIR = DOMAINS_DIR
SCAN_STATE_NAME = ".pulse_scan.json"  # shared signature with capsule_pulse

INDEX_NAME = "orchestration/.read_index.sqlite3"


def enabled() -> bool:
    """Kill switch: ``AGENCY_INDEX=off`` turns the whole layer off."""
    return os.environ.get("AGENCY_INDEX", "").strip().lower() != "off"


def _index_path() -> Path:
    override = os.environ.get("AGENCY_INDEX_FILE", "").strip()
    return Path(override) if override else READ_INDEX_PATH


def _connect() -> sqlite3.Connection:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


#: Bump whenever a table/column changes: ``ensure()`` rebuilds on mismatch,
#: because the files are the truth and the index is disposable.
#: v3 added the columns the ``/v1`` aggregate reads need to answer with a
#: constant number of queries and no manifest reads at all (the revision
#: counter and cap position, the birth time, the last recorded reason, and the
#: active gate's authority) — see ``orchestration/v1/reads.py``.
#: v4 adds the **last capsule decision** projection (actor, action, time, and
#: the gate/reason it names) so ``/v1/overview``'s ``recent_decisions`` can
#: report approvals too — the feedback ledger only ever holds rejections,
#: revision requests and notes, never an approval. Filled at rebuild time, so
#: a request still reads the index alone and opens zero manifests.
SCHEMA_VERSION = 4

#: How much of a history entry's ``why`` the index retains. The value is
#: display-only (the manifest remains the truth); the bound keeps one
#: pathological history line from bloating the index.
MAX_LAST_REASON_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    capsule_id TEXT PRIMARY KEY,
    domain     TEXT,
    state      TEXT,
    updated_at TEXT,
    sha256     TEXT,
    has_image  INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    calls      INTEGER DEFAULT 0,
    cost_usd   REAL,
    path       TEXT UNIQUE,
    gates_total INTEGER DEFAULT 0,
    gates_passed INTEGER DEFAULT 0,
    active_gate TEXT,
    active_gate_type TEXT,
    progress_percent REAL DEFAULT 0,
    revision_count INTEGER DEFAULT 0,
    created_at TEXT,
    last_reason TEXT,
    last_reason_at TEXT,
    active_gate_authority TEXT,
    last_decision_actor TEXT,
    last_decision_action TEXT,
    last_decision_at TEXT,
    last_decision_gate_id TEXT,
    last_decision_gate_type TEXT,
    last_decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_capsules_domain_state ON capsules(domain, state);
CREATE INDEX IF NOT EXISTS ix_capsules_progress ON capsules(progress_percent);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id  TEXT PRIMARY KEY,
    sector     TEXT,
    state      TEXT,
    created_at TEXT,
    gate_id    TEXT,
    settled    INTEGER DEFAULT 0,
    path       TEXT UNIQUE,
    stage      TEXT DEFAULT 'approved',
    title      TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_sector_state ON tickets(sector, state);
CREATE INDEX IF NOT EXISTS ix_tickets_stage ON tickets(stage);
CREATE TABLE IF NOT EXISTS feedback (
    entry_id   TEXT PRIMARY KEY,
    capsule_id TEXT,
    domain     TEXT,
    gate_id    TEXT,
    gate_type  TEXT,
    decision   TEXT,
    reviewer   TEXT,
    reason     TEXT,
    created_at TEXT,
    tags       TEXT,
    constraints TEXT,
    path       TEXT
);
CREATE INDEX IF NOT EXISTS ix_feedback_domain ON feedback(domain, created_at);
CREATE INDEX IF NOT EXISTS ix_feedback_capsule ON feedback(capsule_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _reset(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS capsules")
    conn.execute("DROP TABLE IF EXISTS tickets")
    conn.execute("DROP TABLE IF EXISTS feedback")
    conn.execute("DROP TABLE IF EXISTS meta")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    """Read the index's schema version; a missing/corrupt value forces rebuild."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return -1
    if not row or not isinstance(row[0], str):
        return -1
    try:
        return int(row[0])
    except ValueError:
        return -1


# --------------------------------------------------------------------------- #
# row extraction — read-only projection of the vault records
# --------------------------------------------------------------------------- #

def _gate_summary(
    manifest: dict[str, Any],
) -> Tuple[int, int, Any, Any, float, Any]:
    """Gate-plan projection for the radar: totals, active gate, progress.

    Read-only and defensive: a corrupt plan degrades to zeros (the capsule
    itself is still listed and its router decision reports the corruption).

    The last element is the active gate's declared ``authority`` — the
    capability a human must hold to decide it. It is read straight off the
    gate record (the same value the decision path enforces) and is ``None``
    when there is no active gate or the plan is unreadable; nothing is guessed.
    """
    try:
        from capsule_engine import gates as gate_engine
        from capsule_engine import trace as trace_engine

        records = gate_engine.load_records(manifest)
        if not records:
            return 0, 0, None, None, float(
                trace_engine.progress_percent(manifest)
            ), None
        plan = gate_engine.progress(records)
        active = gate_engine.active_record(records)
        return (
            int(plan["total"]),
            int(plan["passed"]) + int(plan["skipped"]),
            plan["active_gate"],
            plan["active_gate_type"],
            float(trace_engine.progress_percent(manifest, gate_records=records)),
            active.authority if active is not None else None,
        )
    except Exception:
        return 0, 0, None, None, 0.0, None


def _last_reason(manifest: dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """The newest history entry's ``(why, at)`` — the reason the last writer gave.

    This is what the ``/v1`` attention list shows for a halted capsule: the
    engine's own recorded words (for example the revision-cap escalation), not
    a sentence this index invents. Values are bounded and defensive: no
    history, an unreadable entry, or a non-string ``why`` yields ``None``.
    """
    history = manifest.get("history")
    if not isinstance(history, list) or not history:
        return None, None
    tail = history[-1]
    if not isinstance(tail, dict):
        return None, None
    why = tail.get("why")
    at = tail.get("at")
    reason = why.strip()[:MAX_LAST_REASON_CHARS] if isinstance(why, str) and why.strip() else None
    when = at.strip() if isinstance(at, str) and at.strip() else None
    return reason, when


def _last_decision(manifest: dict[str, Any]) -> Tuple[Any, ...]:
    """The newest entry of the capsule's attested decision ledger, or all-``None``.

    The ledger (``manifest['decisions']``) is the engine's own append-ordered
    record of who decided what — it holds **approvals as well as rejections**,
    which the feedback ledger does not. This projection keeps ``actor``,
    ``action``, ``at`` and the gate/reason it names so ``/v1/overview`` can
    answer ``recent_decisions`` from the index with no manifest read.

    Defensive like :func:`_last_reason`: a missing/non-list ledger, a non-dict
    tail or non-string fields yield ``None`` rather than an invented value.
    """
    decisions = manifest.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return (None, None, None, None, None, None)
    tail = decisions[-1]
    if not isinstance(tail, dict):
        return (None, None, None, None, None, None)

    def _text(key: str, *, limit: int = 200) -> Optional[str]:
        value = tail.get(key)
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    return (
        _text("actor"),
        _text("action", limit=32),
        _text("at", limit=64),
        _text("gate_id", limit=200),
        _text("gate_type", limit=200),
        _text("reason", limit=MAX_LAST_REASON_CHARS),
    )


def _capsule_row(capsule_dir: Path, manifest: dict[str, Any]) -> Tuple[Any, ...]:
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    has_image = any(
        (capsule_dir / f"image.{ext}").is_file() for ext in ("jpg", "png", "webp")
    )
    cost = metadata.get("estimated_cost_usd", usage.get("estimated_cost_usd"))
    (
        gates_total,
        gates_passed,
        active_gate,
        active_gate_type,
        progress,
        active_gate_authority,
    ) = _gate_summary(manifest)
    revisions = manifest.get("revision_count")
    if isinstance(revisions, bool) or not isinstance(revisions, int) or revisions < 0:
        revisions = 0
    last_reason, last_reason_at = _last_reason(manifest)
    created_at = manifest.get("created_at")
    return (
        str(manifest.get("capsule_id") or capsule_dir.name),
        manifest.get("domain"),
        manifest.get("state"),
        manifest.get("updated_at"),
        metadata.get("artifact_sha256"),
        1 if has_image else 0,
        int(usage.get("total_tokens") or 0),
        int(usage.get("calls") or 0),
        float(cost) if isinstance(cost, (int, float)) else None,
        str(capsule_dir),
        gates_total,
        gates_passed,
        active_gate,
        active_gate_type,
        progress,
        revisions,
        created_at if isinstance(created_at, str) else None,
        last_reason,
        last_reason_at,
        active_gate_authority,
        *_last_decision(manifest),
    )


def _ticket_row(path: Path, data: dict[str, Any], stage: str = "approved") -> Tuple[Any, ...]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    gate_id = data.get("gate_id") or data.get("via") or metadata.get("gate_id")
    settled = bool(data.get("via") or data.get("settled") or data.get("spool_capsule"))
    parent_name = path.parent.name
    if parent_name in ("scratch", "approved", "archive"):
        sec = data.get("sector") or path.parent.parent.parent.name
    elif path.parent.parent.name == "archive":
        sec = data.get("sector") or path.parent.parent.parent.name
    else:
        sec = data.get("sector") or path.parent.parent.name
    title = data.get("title") or metadata.get("title")
    return (
        str(data.get("ticket_id") or data.get("id") or path.stem),
        sec,
        data.get("state", "open"),
        data.get("created_at") or metadata.get("timestamp"),
        gate_id,
        1 if settled else 0,
        str(path),
        stage,
        title if isinstance(title, str) and title.strip() else None,
    )


def _feedback_rows() -> List[Tuple[Any, ...]]:
    """Project the central feedback ledger into index rows (rebuildable)."""
    try:
        from capsule_engine import memory as memory_engine

        ledger = memory_engine.read_entries(limit=None)
    except Exception:
        return []
    rows: List[Tuple[Any, ...]] = []
    for entry in ledger.entries:
        rows.append(
            (
                entry.entry_id,
                entry.capsule_id,
                entry.domain,
                entry.gate_id,
                entry.gate_type,
                entry.decision,
                entry.reviewer,
                entry.reason[:2000],
                entry.at,
                ",".join(entry.tags),
                " | ".join(entry.constraints)[:2000],
                ledger.path,
            )
        )
    return rows


#: Per-item fail-closed report from the most recent :func:`rebuild`: every
#: vault record this build could not understand (unknown schema_version,
#: corrupt JSON) was SKIPPED and recorded here — never dropped silently, and
#: never allowed to crash the rebuild or poison the index.
_LAST_REBUILD_SKIPPED: List[Dict[str, str]] = []


def last_rebuild_skipped() -> List[Dict[str, str]]:
    """Copy of the skip report produced by the most recent rebuild()."""
    return list(_LAST_REBUILD_SKIPPED)


#: The closed vocabulary of index skip reasons (mirrors the Watcher's
#: quarantine reasons so the two reports speak the same language).
QUARANTINE_CODES = ("schema_version", "invalid_json", "unreadable")


def _quarantine_code(detail: str) -> str:
    """Classify a skip detail into the closed ``QUARANTINE_CODES`` vocabulary.

    Derived from the detail text the scanner already recorded, so no call site
    has to change and nothing is inferred beyond what was written down. An
    unknown schema version and unparseable JSON are named precisely; everything
    else is the residual "unreadable" class.
    """
    text = detail or ""
    if "schema_version" in text:
        return "schema_version"
    if text.startswith("unreadable JSON"):
        return "invalid_json"
    return "unreadable"


def _skip(kind: str, path: Path, detail: str) -> None:
    """Record one fail-closed skip (per item) and log it."""
    _LAST_REBUILD_SKIPPED.append(
        {
            "kind": kind,
            "path": str(path),
            "detail": detail,
            "code": _quarantine_code(detail),
        }
    )
    logger.warning("read_index: skipped %s (%s): %s", kind, path, detail)


def _ticket_row_or_skip(ticket_path: Path, stage: str) -> Optional[Tuple[Any, ...]]:
    """Project one ticket, or skip + report it (fail closed per item)."""
    if "sync-conflict" in ticket_path.name or ticket_path.name.endswith(".tmp"):
        return None
    try:
        data = json.loads(ticket_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _skip("ticket", ticket_path, "unreadable JSON: %s" % exc)
        return None
    try:
        from shared_kernel.schema_version import KIND_TICKET, check_schema_version

        # Unknown/higher schema_version raises — the ticket is skipped and
        # reported instead of crashing the whole listing or rebuild.
        check_schema_version(KIND_TICKET, data, ticket_path)
    except ImportError:
        pass
    except Exception as exc:  # SchemaVersionError (fail closed, reported)
        _skip("ticket", ticket_path, str(exc))
        return None
    return _ticket_row(ticket_path, data, stage=stage)


def _scan_files() -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
    """Read the vault once and project every record into index rows.

    Per-item fail closed: one unreadable or unknown-schema ticket/capsule is
    skipped and reported (see :func:`last_rebuild_skipped`); it never crashes
    the scan and never enters the index.
    """
    from capsule_engine import load_manifest

    capsule_rows: List[Tuple[Any, ...]] = []
    ticket_rows: List[Tuple[Any, ...]] = []

    if SPOOL_DIR.is_dir():
        for manifest_path in sorted(SPOOL_DIR.rglob("manifest.json")):
            capsule_dir = manifest_path.parent
            try:
                manifest = load_manifest(capsule_dir)
            except Exception as exc:
                _skip("capsule", capsule_dir, str(exc))
                continue  # quarantined-by-nature: never enters the index
            capsule_rows.append(_capsule_row(capsule_dir, manifest))

    if DOMAINS_DIR.is_dir():
        ticket_specs = (
            ("*/tickets/approved/ticket_*.json", "approved"),
            ("*/tickets/scratch/ticket_*.json", "scratch"),
            ("*/tickets/archive/ticket_*.json", "archived"),
            ("*/tickets/archive/*/ticket_*.json", "archived"),
            ("*/archive/*/ticket_*.json", "archived"),
            ("*/archive/ticket_*.json", "archived"),
            ("*/tickets/ticket_*.json", "approved"),  # legacy root layout
        )
        seen_paths: set[Path] = set()
        for pattern, stage in ticket_specs:
            for ticket_path in sorted(DOMAINS_DIR.glob(pattern)):
                resolved = ticket_path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                row = _ticket_row_or_skip(ticket_path, stage)
                if row is not None:
                    ticket_rows.append(row)

    return capsule_rows, ticket_rows, _feedback_rows()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def rebuild() -> int:
    """Rebuild the whole index from the files. Returns capsules indexed.

    Per-item fail closed: records with an unknown schema_version (or corrupt
    content) are skipped and reported via :func:`last_rebuild_skipped` — the
    rebuild itself always completes.
    """
    global _LAST_REBUILD_SKIPPED
    _LAST_REBUILD_SKIPPED = []
    capsule_rows, ticket_rows, feedback_rows = _scan_files()
    if _LAST_REBUILD_SKIPPED:
        logger.warning(
            "read_index rebuild: skipped %d vault item(s) fail-closed: %s",
            len(_LAST_REBUILD_SKIPPED),
            "; ".join(
                "%s %s" % (item["kind"], item["path"])
                for item in _LAST_REBUILD_SKIPPED
            ),
        )
    conn = _connect()
    try:
        with conn:
            _reset(conn)
            conn.executemany(
                "INSERT OR REPLACE INTO capsules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                capsule_rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",
                ticket_rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                feedback_rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('signature', ?)",
                (_signature(),),
            )
            # The fail-closed skip report travels with the index: the ``/v1``
            # attention read reports quarantined vault items from here (one
            # query) instead of re-walking the spool on every request.
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('quarantined', ?)",
                (json.dumps(_LAST_REBUILD_SKIPPED, ensure_ascii=False),),
            )
    finally:
        conn.close()
    logger.info(
        "read_index rebuilt: %d capsules, %d tickets, %d feedback entries",
        len(capsule_rows),
        len(ticket_rows),
        len(feedback_rows),
    )
    return len(capsule_rows)


def _signature() -> str:
    """The spool scan-state's mtime+size — a cheap freshness fingerprint."""
    state_file = SPOOL_DIR / SCAN_STATE_NAME
    try:
        st = state_file.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "none"


def _is_fresh(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
    if not row:
        return False
    return row[0] == _signature()


def apply_pulse_report(report: dict[str, Any]) -> int:
    """Update exactly the capsules a pulse reported on (touched or held).

    Everything the pulse named — executed, held, failed, capped, quarantined —
    is re-projected from its manifest (the truth), so the index never drifts
    from what the files actually say. Returns rows refreshed.
    """
    if not enabled():
        return 0
    touched: set[str] = set()
    for bucket in (
        "executed", "held", "capped", "quarantined", "missing",
        "skipped", "no_worker", "locked", "failed", "demoted",
    ):
        for entry in report.get(bucket, []) or []:
            path = entry.get("capsule_path") if isinstance(entry, dict) else None
            if path:
                touched.add(str(Path(path)))

    if not touched:
        return 0

    from capsule_engine import load_manifest

    conn = _connect()
    updated = 0
    try:
        with conn:
            for capsule_str in touched:
                capsule_dir = Path(capsule_str)
                try:
                    manifest = load_manifest(capsule_dir)
                except Exception:
                    conn.execute("DELETE FROM capsules WHERE path=?", (capsule_str,))
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO capsules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _capsule_row(capsule_dir, manifest),
                )
                updated += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('signature', ?)",
                (_signature(),),
            )
    finally:
        conn.close()
    return updated


def _discard_corrupt_index() -> None:
    """Delete an index file SQLite refuses to read (plus its WAL sidecars).

    The index is a **derived** artifact — every row is reproducible from the
    vault — so a file that is not a SQLite database (truncated, overwritten by a
    torn non-atomic writer, plain garbage) is discarded rather than allowed to
    fail every read that depends on it. Only the configured index path is
    touched; no vault record is ever removed.
    """
    base = _index_path()
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(base) + suffix)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # e.g. read-only parent: report, never crash
            logger.warning("read_index: could not discard corrupt index %s: %s", target, exc)
    logger.warning("read_index: discarded a corrupt index file; rebuilding from the vault")


def _rebuild_guaranteed() -> bool:
    """Rebuild the index, discarding a file SQLite refuses to read at all.

    A stale index (older schema, new spool signature) rebuilds in place — its
    tables are dropped and recreated. Only when SQLite raises on the file
    itself (``DatabaseError``: not a database) is the file discarded and the
    build retried on a fresh one.
    """
    try:
        rebuild()
    except sqlite3.DatabaseError:
        _discard_corrupt_index()
        rebuild()
    return True


def ensure() -> bool:
    """Guarantee a fresh index (build once per signature). False = disabled.

    A stale index — a schema the build has moved past, or a spool the pulse has
    since touched — is rebuilt. A **corrupt** index file is treated the same
    way: the layer is a derived read model, so the file is discarded and rebuilt
    rather than propagated as a failure into every request that reads it.
    """
    if not enabled():
        return False
    try:
        conn = _connect()
    except sqlite3.DatabaseError:
        return _rebuild_guaranteed()
    try:
        if (
            _schema_version(conn) == SCHEMA_VERSION
            and _is_fresh(conn)
            and conn.execute("SELECT 1 FROM capsules LIMIT 1").fetchone() is not None
        ):
            return True
    except sqlite3.DatabaseError:
        pass  # corrupt internals → discard and rebuild
    finally:
        conn.close()
    return _rebuild_guaranteed()


def _in_condition(column: str, values: Optional[Sequence[str]]) -> Tuple[Optional[str], List[str]]:
    """A ``<column> IN (…)`` condition for a set-of-sectors filter.

    ``None`` means "no filter"; an empty (or all-blank) set means "visible to no
    sector" and yields a condition that matches nothing — a scoped caller sees
    the empty set, never an accidental unfiltered read.
    """
    if values is None:
        return None, []
    clean = sorted({v for v in values if isinstance(v, str) and v})
    if not clean:
        return "0", []
    return "%s IN (%s)" % (column, ",".join("?" * len(clean))), clean


def _where(*conditions: Optional[str]) -> str:
    """``WHERE a AND b`` over the conditions that are present, else ``""``."""
    parts = [c for c in conditions if c]
    return (" WHERE " + " AND ".join(parts)) if parts else ""


def _keep_sector(domains: Optional[Sequence[str]]):
    """The Python twin of :func:`_in_condition`, for the file-scan fallback."""
    if domains is None:
        return lambda _value: True
    visible = {v for v in domains if isinstance(v, str) and v}
    return lambda value: value in visible


def _aggregate_rows(
    capsule_rows: List[Tuple[Any, ...]],
    ticket_rows: List[Tuple[Any, ...]],
    feedback_rows: List[Tuple[Any, ...]],
) -> Dict[str, Any]:
    """The whole radar aggregate, over rows — the file-scan twin of :func:`stats`.

    The row indices are the column order of ``_scan_files`` (and of the index
    tables), so this and the SQL path can be compared directly.
    """
    def count_by(rows: List[Tuple[Any, ...]], index: int) -> Dict[Any, int]:
        out: Dict[Any, int] = {}
        for row in rows:
            key = row[index]
            out[key] = out.get(key, 0) + 1
        return out

    total_caps = len(capsule_rows)
    total_tickets = len(ticket_rows)
    approved_tickets = sum(1 for r in ticket_rows if r[7] == "approved")

    feedback_by_domain: Dict[Any, int] = {}
    for row in feedback_rows:
        key = row[2] if row[2] is not None else "(none)"
        feedback_by_domain[key] = feedback_by_domain.get(key, 0) + 1

    return {
        "capsules": {
            "total": total_caps,
            "pending": sum(1 for r in capsule_rows if r[2] == "pending_approval"),
            "published": sum(1 for r in capsule_rows if r[2] == "published"),
            "stalled": sum(1 for r in capsule_rows if r[2] == "stalled"),
            "total_tokens": sum(int(r[6] or 0) for r in capsule_rows),
            "total_calls": sum(int(r[7] or 0) for r in capsule_rows),
            "total_cost_usd": round(sum(float(r[8] or 0.0) for r in capsule_rows), 4),
            "gated": sum(1 for r in capsule_rows if int(r[10] or 0) > 0),
            "with_active_gate": sum(1 for r in capsule_rows if r[12] is not None),
        },
        "tickets": {
            "total": total_tickets,
            "approved": approved_tickets,
            "scratch": sum(1 for r in ticket_rows if r[7] == "scratch"),
            "archived": sum(1 for r in ticket_rows if r[7] == "archived"),
            "settled": sum(1 for r in ticket_rows if r[5]),
            "acceptance_rate_percent": (
                round(approved_tickets / total_tickets * 100, 1)
                if total_tickets
                else 100.0
            ),
            "by_sector": count_by(ticket_rows, 1),
            "by_stage": count_by(ticket_rows, 7),
            "by_state": count_by(ticket_rows, 2),
        },
        "feedback": {
            "total": len(feedback_rows),
            "rejections": sum(
                1
                for r in feedback_rows
                if r[5] in ("rejected", "revision_requested")
            ),
            "by_domain": feedback_by_domain,
            "by_decision": count_by(feedback_rows, 5),
        },
    }


def stats(*, domains: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
    """Aggregated views for the radar. The numbers are *derived from files*
    via the index; the source of truth never changes.

    ``domains`` restricts every aggregate to a caller's visible sectors (a
    sector *is* a domain name): capsules and feedback by ``domain``, tickets by
    ``sector``. ``None`` (the default) is the whole factory; an empty set is the
    empty view, never "everything".
    """
    if not enabled() or not ensure():
        return None
    caps_cond, caps_params = _in_condition("domain", domains)
    tix_cond, tix_params = _in_condition("sector", domains)
    fb_cond, fb_params = _in_condition("domain", domains)
    conn = _connect()
    try:
        total_caps = conn.execute(
            "SELECT COUNT(*) FROM capsules" + _where(caps_cond), caps_params
        ).fetchone()[0]
        published_caps = conn.execute(
            "SELECT COUNT(*) FROM capsules" + _where(caps_cond, "state='published'"),
            caps_params,
        ).fetchone()[0]
        pending_caps = conn.execute(
            "SELECT COUNT(*) FROM capsules"
            + _where(caps_cond, "state='pending_approval'"),
            caps_params,
        ).fetchone()[0]
        stalled_caps = conn.execute(
            "SELECT COUNT(*) FROM capsules" + _where(caps_cond, "state='stalled'"),
            caps_params,
        ).fetchone()[0]
        total_tokens = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM capsules" + _where(caps_cond),
            caps_params,
        ).fetchone()[0]
        total_calls = conn.execute(
            "SELECT COALESCE(SUM(calls),0) FROM capsules" + _where(caps_cond),
            caps_params,
        ).fetchone()[0]
        total_cost_usd = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0.0) FROM capsules" + _where(caps_cond),
            caps_params,
        ).fetchone()[0]

        total_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets" + _where(tix_cond), tix_params
        ).fetchone()[0]
        approved_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets" + _where(tix_cond, "stage='approved'"),
            tix_params,
        ).fetchone()[0]
        scratch_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets" + _where(tix_cond, "stage='scratch'"),
            tix_params,
        ).fetchone()[0]
        archived_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets" + _where(tix_cond, "stage='archived'"),
            tix_params,
        ).fetchone()[0]
        settled_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets" + _where(tix_cond, "settled=1"),
            tix_params,
        ).fetchone()[0]

        acceptance_rate = round((approved_tickets / total_tickets * 100), 1) if total_tickets > 0 else 100.0

        gated_caps = conn.execute(
            "SELECT COUNT(*) FROM capsules" + _where(caps_cond, "gates_total > 0"),
            caps_params,
        ).fetchone()[0]
        pending_gates = conn.execute(
            "SELECT COUNT(*) FROM capsules"
            + _where(caps_cond, "active_gate IS NOT NULL"),
            caps_params,
        ).fetchone()[0]
        feedback_total = conn.execute(
            "SELECT COUNT(*) FROM feedback" + _where(fb_cond), fb_params
        ).fetchone()[0]
        feedback_rejections = conn.execute(
            "SELECT COUNT(*) FROM feedback"
            + _where(fb_cond, "decision IN ('rejected','revision_requested')"),
            fb_params,
        ).fetchone()[0]

        capsules = {
            "total": total_caps,
            "pending": pending_caps,
            "published": published_caps,
            "stalled": stalled_caps,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "total_cost_usd": round(total_cost_usd, 4),
            "gated": gated_caps,
            "with_active_gate": pending_gates,
        }
        feedback = {
            "total": feedback_total,
            "rejections": feedback_rejections,
            "by_domain": dict(
                conn.execute(
                    "SELECT COALESCE(domain,'(none)'), COUNT(*) FROM feedback"
                    + _where(fb_cond)
                    + " GROUP BY COALESCE(domain,'(none)') ORDER BY 1",
                    fb_params,
                ).fetchall()
            ),
            "by_decision": dict(
                conn.execute(
                    "SELECT decision, COUNT(*) FROM feedback"
                    + _where(fb_cond)
                    + " GROUP BY decision ORDER BY 1",
                    fb_params,
                ).fetchall()
            ),
        }
        tickets = {
            "total": total_tickets,
            "approved": approved_tickets,
            "scratch": scratch_tickets,
            "archived": archived_tickets,
            "settled": settled_tickets,
            "acceptance_rate_percent": acceptance_rate,
            "by_sector": dict(
                conn.execute(
                    "SELECT sector, COUNT(*) FROM tickets"
                    + _where(tix_cond)
                    + " GROUP BY sector ORDER BY sector",
                    tix_params,
                ).fetchall()
            ),
            "by_stage": dict(
                conn.execute(
                    "SELECT stage, COUNT(*) FROM tickets"
                    + _where(tix_cond)
                    + " GROUP BY stage ORDER BY stage",
                    tix_params,
                ).fetchall()
            ),
            "by_state": dict(
                conn.execute(
                    "SELECT state, COUNT(*) FROM tickets"
                    + _where(tix_cond)
                    + " GROUP BY state ORDER BY state",
                    tix_params,
                ).fetchall()
            ),
        }
        return {"capsules": capsules, "tickets": tickets, "feedback": feedback}
    finally:
        conn.close()


def stats_from_files(*, domains: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The file-scan fallback for :func:`stats` — the identical ``domains`` filter.

    Used when the index is off or unavailable, and the reference the SQL path is
    tested against. Applies the same predicate the SQL filter does: capsules and
    feedback by ``domain``, tickets by ``sector``.
    """
    capsule_rows, ticket_rows, feedback_rows = _scan_files()
    keep = _keep_sector(domains)
    return _aggregate_rows(
        [r for r in capsule_rows if keep(r[1])],
        [r for r in ticket_rows if keep(r[1])],
        [r for r in feedback_rows if keep(r[2])],
    )


# --------------------------------------------------------------------------- #
# feedback ledger views (Brick 3 extension: the memory engine's index)
# --------------------------------------------------------------------------- #

def query_feedback(
    *,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    decision: Optional[str] = None,
    capsule_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Paged feedback entries (newest first) — views, never the source of truth.

    ``domains`` applies a caller's authorised-sector filter in SQL (a *set*,
    unlike the single ``domain``); an empty set returns nothing.
    """
    if not enabled() or not ensure():
        return []
    sql = (
        "SELECT entry_id, capsule_id, domain, gate_id, gate_type, decision, "
        "reviewer, reason, created_at, tags, constraints, path FROM feedback"
    )
    clauses, params = [], []
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if domains is not None:
        clean = [d for d in domains if isinstance(d, str) and d]
        if not clean:
            return []  # scoped to nothing → nothing to show
        clauses.append("domain IN (%s)" % ",".join("?" * len(clean)))
        params.extend(clean)
    if decision:
        clauses.append("decision=?")
        params.append(decision)
    if capsule_id:
        clauses.append("capsule_id=?")
        params.append(capsule_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([max(0, min(int(limit), 500)), max(0, int(offset))])
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(zip(_FEEDBACK_KEYS, row)) for row in rows]


#: Column order shared by ``query_feedback`` and its file-scan fallback.
_FEEDBACK_KEYS = [
    "entry_id", "capsule_id", "domain", "gate_id", "gate_type",
    "decision", "reviewer", "reason", "created_at", "tags",
    "constraints", "path",
]

#: Column order shared by ``query_gated_capsules`` and its file-scan fallback.
_GATED_CAPSULE_KEYS = [
    "capsule_id", "domain", "state", "updated_at", "gates_total",
    "gates_passed", "active_gate", "active_gate_type",
    "progress_percent", "total_tokens", "calls", "cost_usd", "path",
]


def query_feedback_from_files(
    *,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    decision: Optional[str] = None,
    capsule_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_feedback` (identical filter)."""
    _caps, _tix, feedback_rows = _scan_files()
    visible = (
        None
        if domains is None
        else {d for d in domains if isinstance(d, str) and d}
    )
    items = [dict(zip(_FEEDBACK_KEYS, row)) for row in feedback_rows]
    if domain:
        items = [i for i in items if i["domain"] == domain]
    if domains is not None:
        items = [i for i in items if i["domain"] in visible]
    if decision:
        items = [i for i in items if i["decision"] == decision]
    if capsule_id:
        items = [i for i in items if i["capsule_id"] == capsule_id]
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    start = max(0, int(offset))
    return items[start : start + max(0, min(int(limit), 500))]


def query_gated_capsules(
    limit: int = 100, offset: int = 0, *, domains: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Capsules that carry a gate plan, with their live gate position (radar).

    ``domains`` restricts the result to a caller's authorised sectors in SQL.
    """
    if not enabled() or not ensure():
        return []
    cond, params = _in_condition("domain", domains)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT capsule_id, domain, state, updated_at, gates_total, "
            "gates_passed, active_gate, active_gate_type, progress_percent, "
            "total_tokens, calls, cost_usd, path FROM capsules"
            + _where(cond, "gates_total > 0")
            + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [max(0, min(int(limit), 500)), max(0, int(offset))],
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(_GATED_CAPSULE_KEYS, row)) for row in rows]


def query_gated_capsules_from_files(
    limit: int = 100, offset: int = 0, *, domains: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_gated_capsules` (identical filter)."""
    capsule_rows, _tix, _fb = _scan_files()
    keep = _keep_sector(domains)
    items = [
        dict(zip(_GATED_CAPSULE_KEYS, row))
        for row in capsule_rows
        if int(row[10] or 0) > 0 and keep(row[1])
    ]
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)
    start = max(0, int(offset))
    return items[start : start + max(0, min(int(limit), 500))]


#: Column order shared by every capsule projection (the SQL selectors and the
#: file-scan twins), so the two paths can be compared field for field.
_CAPSULE_KEYS = [
    "capsule_id", "domain", "state", "updated_at", "sha256", "has_image",
    "total_tokens", "calls", "cost_usd", "path", "gates_total",
    "gates_passed", "active_gate", "active_gate_type", "progress_percent",
    "revision_count", "created_at", "last_reason", "last_reason_at",
    "active_gate_authority",
    # v4: the last entry of the capsule's own decision ledger, so the home's
    # recent decisions can report approvals without opening a manifest.
    "last_decision_actor", "last_decision_action", "last_decision_at",
    "last_decision_gate_id", "last_decision_gate_type", "last_decision_reason",
]

_CAPSULE_SELECT = "SELECT " + ", ".join(_CAPSULE_KEYS) + " FROM capsules"

#: Column order shared by every ticket projection (index and file-scan twins).
_TICKET_KEYS = [
    "ticket_id", "sector", "state", "created_at", "gate_id", "settled",
    "path", "stage", "title",
]

_TICKET_SELECT = "SELECT " + ", ".join(_TICKET_KEYS) + " FROM tickets"

#: The states that put a capsule in front of a human — the engine's own
#: attention vocabulary (mirror of ``watcher.inspect_manifest``): at the
#: approval gate, frozen at it, or halted (the human-reset position).
AWAITING_HUMAN_STATES: Tuple[str, ...] = ("pending_approval", "stalled", "failed")

#: The state the loop breaker leaves behind, with its counter.
REWORK_STATE = "needs_revision"

MAX_PAGE = 500


def needs_human(state: Optional[str], revisions: Optional[int], cap: int) -> bool:
    """Does this capsule need a human? The engine's attention predicate.

    True at the approval gate (``pending_approval``), frozen there
    (``stalled``), halted (``failed``), or sitting *over* the revision cap
    while still in ``needs_revision`` — the state a lowered cap (or a hand
    edit) can leave behind, which ``transition`` would otherwise have
    escalated. This mirrors ``watcher.inspect_manifest`` exactly, and is the
    single predicate both the SQL filter and the file-scan twin apply.
    """
    if state in AWAITING_HUMAN_STATES:
        return True
    return (
        state == REWORK_STATE
        and isinstance(revisions, int)
        and not isinstance(revisions, bool)
        and revisions > cap
    )


def _needs_human_condition(cap: int) -> Tuple[str, List[Any]]:
    """The SQL twin of :func:`needs_human` (bounded, parameterised).

    NULL ``revision_count`` (a pre-cap manifest) coalesces to 0, so an old
    capsule is never wrongly treated as over the cap.
    """
    placeholders = ",".join("?" * len(AWAITING_HUMAN_STATES))
    condition = (
        "(state IN (%s) OR (state='%s' AND COALESCE(revision_count,0) > ?))"
        % (placeholders, REWORK_STATE)
    )
    return condition, list(AWAITING_HUMAN_STATES) + [int(cap)]


def keyset_condition(
    column: str,
    ident_column: str,
    after: Optional[Tuple[Optional[str], str]],
    *,
    prefix: str = "",
) -> Tuple[Optional[str], List[Any]]:
    """The SQL keyset condition for "strictly after this cursor", or none.

    The order is ``(<column> DESC, <ident> DESC)`` with NULL timestamps last
    (SQLite sorts NULLs smallest, so DESC already puts them last). A row is
    *after* the cursor when its timestamp is older, or equal with a smaller
    id, or — once the cursor is in the NULL tail — when it is also NULL with a
    smaller id. Never ``OFFSET``: a concurrent insert would shift every later
    page and duplicate or skip rows.

    ``prefix`` names the entity kind inside the id (``capsule:``, ``ticket:``)
    so that one cursor ordering spans several kinds at once — the merged
    attention list sorts by ``(at, "kind:id")`` and each per-kind query has to
    compare against exactly that same key, or the merge and the SQL would
    disagree about where a page ends.
    """
    if after is None:
        return None, []
    at, ident = after
    ident_expr = "(? || %s)" % ident_column if prefix else ident_column
    if at is None:
        return "(%s IS NULL AND %s < ?)" % (column, ident_expr), (
            [prefix, ident] if prefix else [ident]
        )
    params: List[Any] = [at, at]
    if prefix:
        params.append(prefix)
    params.append(ident)
    return (
        "((%s < ?) OR (%s = ? AND %s < ?) OR (%s IS NULL))"
        % (column, column, ident_expr, column),
        params,
    )


def keyset_keep(
    at_value: Optional[str],
    ident: Optional[str],
    after: Optional[Tuple[Optional[str], str]],
    *,
    prefix: str = "",
) -> bool:
    """The Python twin of :func:`keyset_condition` (file-scan path)."""
    if after is None:
        return True
    at, ident_cursor = after
    cursor_id = ident_cursor[len(prefix) :] if prefix and ident_cursor.startswith(prefix) else ident_cursor
    if at is None:
        return at_value is None and (ident or "") < cursor_id
    if at_value is None:
        return True  # the NULL tail sorts after every timestamp
    if at_value < at:
        return True
    return at_value == at and (ident or "") < cursor_id


def _sort_items(
    items: List[Dict[str, Any]], at_key: str, ident_key: str, *, ascending: bool = False
) -> None:
    """Sort in place: ``(<at>, <ident>)`` descending (default) or ascending.

    Descending is the paged order and puts NULL timestamps last; ascending is
    the "oldest first" order and is only ever used without a cursor (with the
    NULL timestamps excluded by the query), so the two cannot be confused. The
    tuple's first element is the ``has a timestamp`` flag, which Python compares
    before the strings — the NULLs land at the end for DESC exactly as SQLite's
    ``ORDER BY <col> IS NULL, <col> DESC`` does, and no cross-type comparison
    can occur because the flag decides first.
    """
    items.sort(
        key=lambda item: (
            item.get(at_key) is not None,
            item.get(at_key) or "",
            item.get(ident_key) or "",
        ),
        reverse=not ascending,
    )


def _capsule_conditions(
    *,
    state: Optional[str] = None,
    states: Optional[Sequence[str]] = None,
    states_not: Optional[Sequence[str]] = None,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    needs_human_only: bool = False,
    cap: int = 0,
) -> Tuple[List[str], List[Any], bool]:
    """Build the capsule filter shared by the SQL statement and the fallback.

    Returns ``(conditions, params, usable)``; ``usable=False`` means the
    caller is scoped to no sector at all, which matches *nothing* (never
    everything) — the empty view, expressed once here so the two paths cannot
    disagree about it. ``states_not`` is the complement filter the app's
    "unknown stage" view needs: every state the stage table does not name.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if state:
        clauses.append("state=?")
        params.append(state)
    if states is not None:
        clean_states = [s for s in states if isinstance(s, str) and s]
        if not clean_states:
            return [], [], False
        clauses.append("state IN (%s)" % ",".join("?" * len(clean_states)))
        params.extend(clean_states)
    if states_not is not None:
        excluded = [s for s in states_not if isinstance(s, str) and s]
        if not excluded:
            return [], [], False
        clauses.append("state NOT IN (%s)" % ",".join("?" * len(excluded)))
        params.extend(excluded)
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if domains is not None:
        clean = [d for d in domains if isinstance(d, str) and d]
        if not clean:
            return [], [], False
        clauses.append("domain IN (%s)" % ",".join("?" * len(clean)))
        params.extend(clean)
    if needs_human_only:
        condition, extra = _needs_human_condition(cap)
        clauses.append(condition)
        params.extend(extra)
    return clauses, params, True


def _capsule_keep(
    row: Dict[str, Any],
    *,
    state: Optional[str] = None,
    states: Optional[Sequence[str]] = None,
    states_not: Optional[Sequence[str]] = None,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    needs_human_only: bool = False,
    cap: int = 0,
) -> bool:
    """The Python twin of :func:`_capsule_conditions`, row by row."""
    if state and row.get("state") != state:
        return False
    if states is not None:
        visible = {s for s in states if isinstance(s, str) and s}
        if not visible or row.get("state") not in visible:
            return False
    if states_not is not None:
        excluded = {s for s in states_not if isinstance(s, str) and s}
        if not excluded or row.get("state") in excluded:
            return False
    if domain and row.get("domain") != domain:
        return False
    if domains is not None:
        visible_domains = {d for d in domains if isinstance(d, str) and d}
        if not visible_domains or row.get("domain") not in visible_domains:
            return False
    if needs_human_only and not needs_human(
        row.get("state"), row.get("revision_count"), cap
    ):
        return False
    return True


def _capsule_from_row(row: Tuple[Any, ...]) -> Dict[str, Any]:
    return dict(zip(_CAPSULE_KEYS, row))


def query_capsules(
    *, state: Optional[str] = None, states: Optional[Sequence[str]] = None,
    states_not: Optional[Sequence[str]] = None,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    needs_human_only: bool = False, cap: int = 0,
    after: Optional[Tuple[Optional[str], str]] = None,
    cursor_prefix: str = "",
    ascending: bool = False,
    limit: int = 50, offset: int = 0,
) -> List[Dict[str, Any]]:
    """Paged capsule views (Scaling Brick 4 consumer). Views only.

    ``domains`` applies a caller's authorised-sector filter in SQL so a paged
    read never has to scan rows it may not return. Order is
    ``(updated_at DESC, capsule_id DESC)``; passing ``after`` (a cursor's
    ``(updated_at, "<prefix><capsule_id>")``) pages by keyset instead of
    ``offset``. ``ascending`` is the oldest-first order the home's "5 oldest"
    list needs; it is never combined with a cursor and drops rows whose
    timestamp is missing (an unknown age cannot be the *oldest* anything).
    """
    if not enabled() or not ensure():
        return []
    clauses, params, usable = _capsule_conditions(
        state=state, states=states, states_not=states_not, domain=domain,
        domains=domains, needs_human_only=needs_human_only, cap=cap,
    )
    if not usable:
        return []
    cursor_cond, cursor_params = keyset_condition(
        "updated_at", "capsule_id", after, prefix=cursor_prefix
    )
    if cursor_cond:
        clauses.append(cursor_cond)
        params.extend(cursor_params)
    order = (
        " ORDER BY updated_at ASC, capsule_id ASC"
        if ascending
        else " ORDER BY updated_at IS NULL, updated_at DESC, capsule_id DESC"
    )
    if ascending:
        clauses.append("updated_at IS NOT NULL")
    sql = _CAPSULE_SELECT + _where(*clauses) + order + " LIMIT ? OFFSET ?"
    params.extend([max(0, min(int(limit), MAX_PAGE)), max(0, int(offset))])
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_capsule_from_row(row) for row in rows]


def query_capsules_from_files(
    *, state: Optional[str] = None, states: Optional[Sequence[str]] = None,
    states_not: Optional[Sequence[str]] = None,
    domain: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
    needs_human_only: bool = False, cap: int = 0,
    after: Optional[Tuple[Optional[str], str]] = None,
    cursor_prefix: str = "",
    ascending: bool = False,
    limit: int = 50, offset: int = 0,
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_capsules` (identical filter)."""
    capsule_rows, _tix, _fb = _scan_files()
    items = [_capsule_from_row(row) for row in capsule_rows]
    items = [
        item
        for item in items
        if _capsule_keep(
            item, state=state, states=states, states_not=states_not,
            domain=domain, domains=domains,
            needs_human_only=needs_human_only, cap=cap,
        )
        and (not ascending or item.get("updated_at") is not None)
        and keyset_keep(
            item.get("updated_at"), item.get("capsule_id"), after,
            prefix=cursor_prefix,
        )
    ]
    _sort_items(items, "updated_at", "capsule_id", ascending=ascending)
    start = max(0, int(offset))
    return items[start : start + max(0, min(int(limit), MAX_PAGE))]


def query_capsule_by_id(
    capsule_id: str, *, domains: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """One capsule's indexed view, or ``None`` — the detail route's lookup.

    Resolving the id through the index means a detail read opens exactly the
    one manifest it serves (never a glob, never a directory walk); the caller
    re-reads that manifest to answer, so the index stays a phone book.
    """
    if not capsule_id or not enabled() or not ensure():
        return None
    clauses: List[str] = ["capsule_id=?"]
    params: List[Any] = [str(capsule_id)]
    cond, extra = _in_condition("domain", domains)
    if cond:
        # "0" (scoped to nothing) is the empty match, exactly as in SQL: the
        # detail read then answers "not found", never a cross-sector row.
        clauses.append(cond)
        params.extend(extra)
    conn = _connect()
    try:
        row = conn.execute(
            _CAPSULE_SELECT + _where(*clauses) + " LIMIT 1", params
        ).fetchone()
    finally:
        conn.close()
    return _capsule_from_row(row) if row else None


def query_capsule_by_id_from_files(
    capsule_id: str, *, domains: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_capsule_by_id`."""
    keep = _keep_sector(domains)
    capsule_rows, _tix, _fb = _scan_files()
    for row in capsule_rows:
        item = _capsule_from_row(row)
        if item.get("capsule_id") != capsule_id:
            continue
        if domains is not None and not keep(item.get("domain")):
            return None
        return item
    return None


def query_tickets(
    *, sector: Optional[str] = None, sectors: Optional[Sequence[str]] = None,
    stage: Optional[str] = None, stages: Optional[Sequence[str]] = None,
    state: Optional[str] = None, states: Optional[Sequence[str]] = None,
    after: Optional[Tuple[Optional[str], str]] = None,
    cursor_prefix: str = "",
    ascending: bool = False,
    limit: int = 50, offset: int = 0,
) -> List[Dict[str, Any]]:
    """Paged ticket views queryable by sector(s), stage(s), state(s) or cursor.

    ``sectors`` applies a caller's authorised-sector filter in SQL. ``stages``
    is a set filter (so the default view can include approved + scratch while
    excluding archived, which a single ``stage`` cannot express). Order is
    ``(created_at DESC, ticket_id DESC)`` — a ticket's birth is the only
    timestamp the vault records; ``after`` pages it by keyset.
    """
    if not enabled() or not ensure():
        return []
    clauses, params = [], []
    if sector:
        clauses.append("sector=?")
        params.append(sector)
    if sectors is not None:
        clean = [s for s in sectors if isinstance(s, str) and s]
        if not clean:
            return []
        clauses.append("sector IN (%s)" % ",".join("?" * len(clean)))
        params.extend(clean)
    if stages is not None:
        clean_stages = [s for s in stages if isinstance(s, str) and s]
        if not clean_stages:
            return []
        clauses.append("stage IN (%s)" % ",".join("?" * len(clean_stages)))
        params.extend(clean_stages)
    elif stage:
        clauses.append("stage=?")
        params.append(stage)
    if states is not None:
        clean_states = [s for s in states if isinstance(s, str) and s]
        if not clean_states:
            return []
        clauses.append("state IN (%s)" % ",".join("?" * len(clean_states)))
        params.extend(clean_states)
    elif state:
        clauses.append("state=?")
        params.append(state)
    cursor_cond, cursor_params = keyset_condition(
        "created_at", "ticket_id", after, prefix=cursor_prefix
    )
    if cursor_cond:
        clauses.append(cursor_cond)
        params.extend(cursor_params)
    order = (
        " ORDER BY created_at ASC, ticket_id ASC"
        if ascending
        else " ORDER BY created_at IS NULL, created_at DESC, ticket_id DESC"
    )
    if ascending:
        clauses.append("created_at IS NOT NULL")
    sql = _TICKET_SELECT + _where(*clauses) + order + " LIMIT ? OFFSET ?"
    params.extend([max(0, min(int(limit), MAX_PAGE)), max(0, int(offset))])
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(zip(_TICKET_KEYS, row)) for row in rows]


def query_tickets_from_files(
    *, sector: Optional[str] = None, sectors: Optional[Sequence[str]] = None,
    stage: Optional[str] = None, stages: Optional[Sequence[str]] = None,
    state: Optional[str] = None, states: Optional[Sequence[str]] = None,
    after: Optional[Tuple[Optional[str], str]] = None,
    cursor_prefix: str = "",
    ascending: bool = False,
    limit: int = 50, offset: int = 0,
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_tickets` (identical filter)."""
    _caps, ticket_rows, _fb = _scan_files()
    items = [dict(zip(_TICKET_KEYS, row)) for row in ticket_rows]
    if sector:
        items = [i for i in items if i["sector"] == sector]
    if sectors is not None:
        visible = {s for s in sectors if isinstance(s, str) and s}
        items = [i for i in items if i["sector"] in visible]
    if stages is not None:
        wanted = {s for s in stages if isinstance(s, str) and s}
        items = [i for i in items if i["stage"] in wanted]
    elif stage:
        items = [i for i in items if i["stage"] == stage]
    if states is not None:
        wanted_states = {s for s in states if isinstance(s, str) and s}
        items = [i for i in items if i["state"] in wanted_states]
    elif state:
        items = [i for i in items if i["state"] == state]
    items = [
        i
        for i in items
        if (not ascending or i.get("created_at") is not None)
        and keyset_keep(
            i.get("created_at"), i.get("ticket_id"), after, prefix=cursor_prefix
        )
    ]
    _sort_items(items, "created_at", "ticket_id", ascending=ascending)
    start = max(0, int(offset))
    return items[start : start + max(0, min(int(limit), MAX_PAGE))]


def query_ticket_by_id(
    ticket_id: str, *, sectors: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """One ticket's indexed row, or ``None`` — the ticket detail's lookup.

    Lets a detail read open exactly the one ticket file it serves instead of
    walking the domains tree until the id matches.
    """
    if not ticket_id or not enabled() or not ensure():
        return None
    clauses = ["ticket_id=?"]
    params: List[Any] = [str(ticket_id)]
    cond, extra = _in_condition("sector", sectors)
    if cond:
        clauses.append(cond)
        params.extend(extra)
    conn = _connect()
    try:
        row = conn.execute(
            _TICKET_SELECT + _where(*clauses) + " LIMIT 1", params
        ).fetchone()
    finally:
        conn.close()
    return dict(zip(_TICKET_KEYS, row)) if row else None


def query_ticket_by_id_from_files(
    ticket_id: str, *, sectors: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """The file-scan fallback for :func:`query_ticket_by_id`."""
    keep = _keep_sector(sectors)
    _caps, ticket_rows, _fb = _scan_files()
    for row in ticket_rows:
        item = dict(zip(_TICKET_KEYS, row))
        if item.get("ticket_id") != ticket_id:
            continue
        if sectors is not None and not keep(item.get("sector")):
            return None
        return item
    return None


def domain_rollup(
    *,
    domains: Optional[Sequence[str]] = None,
    day: Optional[str] = None,
    cap: int = 0,
) -> Optional[Dict[str, Any]]:
    """One bounded aggregate for the ``/v1`` home: counts per state, per sector.

    Everything the home needs, in a constant number of statements whatever the
    vault's size — capsule counts and cost per ``(domain, state)``, the
    capsules a human must act on, today's capsule cost per domain, and ticket
    counts per ``(sector, state, stage)``. ``day`` is a ``YYYY-MM-DD`` UTC date
    string; omitting it omits the today-cost rows rather than zero-filling
    them. Returns ``None`` when the index is off (call the twin instead).
    """
    if not enabled() or not ensure():
        return None
    caps_cond, caps_params = _in_condition("domain", domains)
    conn = _connect()
    try:
        capsules = [
            {"domain": row[0], "state": row[1], "count": row[2], "cost_usd": round(row[3] or 0.0, 4)}
            for row in conn.execute(
                "SELECT domain, state, COUNT(*), COALESCE(SUM(cost_usd),0.0) FROM capsules"
                + _where(caps_cond)
                + " GROUP BY domain, state ORDER BY domain, state",
                caps_params,
            ).fetchall()
        ]
        # The engine's own attention buckets, counted per sector. Three bounded
        # statements so the home's headline numbers are *counts*, never the size
        # of whatever page happened to be fetched.
        attention: List[Dict[str, Any]] = []
        for kind, condition, extra in (
            (
                "at_gate",
                "state IN (%s)" % ",".join("?" * len(AWAITING_HUMAN_STATES[:-1])),
                list(AWAITING_HUMAN_STATES[:-1]),
            ),
            ("halted", "state=?", [AWAITING_HUMAN_STATES[-1]]),
            (
                "over_cap",
                "state='%s' AND COALESCE(revision_count,0) > ?" % REWORK_STATE,
                [int(cap)],
            ),
        ):
            attention.extend(
                {"domain": row[0], "kind": kind, "count": row[1]}
                for row in conn.execute(
                    "SELECT domain, COUNT(*) FROM capsules"
                    + _where(caps_cond, condition)
                    + " GROUP BY domain ORDER BY domain",
                    list(caps_params) + list(extra),
                ).fetchall()
            )
        today: List[Dict[str, Any]] = []
        if day:
            today = [
                {"domain": row[0], "cost_usd": round(row[1] or 0.0, 4)}
                for row in conn.execute(
                    "SELECT domain, COALESCE(SUM(cost_usd),0.0) FROM capsules"
                    + _where(caps_cond, "substr(updated_at,1,10)=?")
                    + " GROUP BY domain ORDER BY domain",
                    list(caps_params) + [day],
                ).fetchall()
            ]
        tickets_cond, tickets_params = _in_condition("sector", domains)
        tickets = [
            {"sector": row[0], "state": row[1], "stage": row[2], "count": row[3]}
            for row in conn.execute(
                "SELECT sector, state, stage, COUNT(*) FROM tickets"
                + _where(tickets_cond)
                + " GROUP BY sector, state, stage ORDER BY sector, state, stage",
                tickets_params,
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "capsules": capsules,
        "attention": attention,
        "today_cost": today,
        "tickets": tickets,
    }


def domain_rollup_from_files(
    *,
    domains: Optional[Sequence[str]] = None,
    day: Optional[str] = None,
    cap: int = 0,
) -> Dict[str, Any]:
    """The file-scan fallback for :func:`domain_rollup` (identical filter)."""
    capsule_rows, ticket_rows, _fb = _scan_files()
    keep = _keep_sector(domains)

    capsules: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    awaiting: Dict[Tuple[Any, str], int] = {}
    today: Dict[Any, float] = {}
    for row in capsule_rows:
        item = _capsule_from_row(row)
        if not keep(item.get("domain")):
            continue
        key = (item.get("domain"), item.get("state"))
        bucket = capsules.setdefault(
            key, {"domain": item.get("domain"), "state": item.get("state"), "count": 0, "cost_usd": 0.0}
        )
        bucket["count"] += 1
        bucket["cost_usd"] = round(bucket["cost_usd"] + float(item.get("cost_usd") or 0.0), 4)
        state = item.get("state")
        kind = None
        if state in AWAITING_HUMAN_STATES[:-1]:
            kind = "at_gate"
        elif state == AWAITING_HUMAN_STATES[-1]:
            kind = "halted"
        elif needs_human(state, item.get("revision_count"), cap):
            kind = "over_cap"  # needs_revision AND over the cap (the loop breaker)
        if kind:
            bucket_key = (item.get("domain"), kind)
            awaiting[bucket_key] = awaiting.get(bucket_key, 0) + 1
        updated = item.get("updated_at")
        if day and isinstance(updated, str) and updated[:10] == day:
            today[item.get("domain")] = round(
                today.get(item.get("domain"), 0.0) + float(item.get("cost_usd") or 0.0), 4
            )

    tickets: Dict[Tuple[Any, Any, Any], int] = {}
    for row in ticket_rows:
        item = dict(zip(_TICKET_KEYS, row))
        if not keep(item.get("sector")):
            continue
        key = (item.get("sector"), item.get("state"), item.get("stage"))
        tickets[key] = tickets.get(key, 0) + 1

    return {
        "capsules": sorted(capsules.values(), key=lambda c: (c["domain"] or "", c["state"] or "")),
        "attention": [
            {"domain": key[0], "kind": key[1], "count": count}
            for key, count in sorted(
                awaiting.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
            )
        ],
        "today_cost": [
            {"domain": domain, "cost_usd": cost}
            for domain, cost in sorted(today.items(), key=lambda kv: kv[0] or "")
        ],
        "tickets": [
            {"sector": key[0], "state": key[1], "stage": key[2], "count": count}
            for key, count in sorted(tickets.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "", kv[0][2] or ""))
        ],
    }


def recent_feedback(
    *, domains: Optional[Sequence[str]] = None, limit: int = 10
) -> List[Dict[str, Any]]:
    """The newest review decisions from the feedback ledger, sector-scoped.

    The ledger is the engine's cumulative memory of review decisions and is
    *indexed*, so the home's ``recent_decisions`` needs no manifest read. It
    records rejections and revision requests (what a reviewer said); approvals
    live in the capsule manifests and are therefore not claimed here.
    """
    if not enabled() or not ensure():
        return []
    cond, params = _in_condition("domain", domains)
    if cond == "0":
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT entry_id, capsule_id, domain, gate_id, gate_type, decision, "
            "reviewer, reason, created_at FROM feedback"
            + _where(cond)
            + " ORDER BY created_at IS NULL, created_at DESC, entry_id DESC LIMIT ?",
            params + [max(0, min(int(limit), MAX_PAGE))],
        ).fetchall()
    finally:
        conn.close()
    keys = [
        "entry_id", "capsule_id", "domain", "gate_id", "gate_type",
        "decision", "reviewer", "reason", "created_at",
    ]
    return [dict(zip(keys, row)) for row in rows]


def recent_feedback_from_files(
    *, domains: Optional[Sequence[str]] = None, limit: int = 10
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`recent_feedback` (identical filter)."""
    _caps, _tix, feedback_rows = _scan_files()
    keep = _keep_sector(domains)
    items = [dict(zip(_FEEDBACK_KEYS, row)) for row in feedback_rows]
    items = [
        {key: item.get(key) for key in (
            "entry_id", "capsule_id", "domain", "gate_id", "gate_type",
            "decision", "reviewer", "reason", "created_at",
        )}
        for item in items
        if keep(item.get("domain"))
    ]
    _sort_items(items, "created_at", "entry_id")
    return items[: max(0, min(int(limit), MAX_PAGE))]


#: Column order shared by ``recent_decisions`` and its file-scan fallback.
_DECISION_KEYS = [
    "capsule_id", "domain", "gate_id", "gate_type", "action", "actor", "at", "reason",
]


def recent_decisions(
    *, domains: Optional[Sequence[str]] = None, limit: int = 10
) -> List[Dict[str, Any]]:
    """The newest capsule decisions (approvals *and* rejections), sector-scoped.

    Read entirely from the index's ``capsules`` projection of each manifest's own
    attested decision ledger — actor, action, time, gate and reason — so the
    home's ``recent_decisions`` opens **zero manifests**. One row per capsule
    (its *last* decision); a capsule that was never decided is absent, exactly
    as the feedback ledger omits approvals. ``domains`` is the caller's
    authorised-sector set (empty ⇒ empty, never everything).
    """
    if not enabled() or not ensure():
        return []
    cond, params = _in_condition("domain", domains)
    if cond == "0":
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT capsule_id, domain, last_decision_gate_id, "
            "last_decision_gate_type, last_decision_action, last_decision_actor, "
            "last_decision_at, last_decision_reason FROM capsules"
            + _where(cond, "last_decision_at IS NOT NULL")
            + " ORDER BY last_decision_at IS NULL, last_decision_at DESC, "
            "capsule_id DESC LIMIT ?",
            params + [max(0, min(int(limit), MAX_PAGE))],
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(_DECISION_KEYS, row)) for row in rows]


def recent_decisions_from_files(
    *, domains: Optional[Sequence[str]] = None, limit: int = 10
) -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`recent_decisions` (identical filter)."""
    capsule_rows, _tix, _fb = _scan_files()
    keep = _keep_sector(domains)
    items: List[Dict[str, Any]] = []
    for row in capsule_rows:
        item = _capsule_from_row(row)
        if not keep(item.get("domain")):
            continue
        at = item.get("last_decision_at")
        if at is None:
            continue
        items.append(
            {
                "capsule_id": item.get("capsule_id"),
                "domain": item.get("domain"),
                "gate_id": item.get("last_decision_gate_id"),
                "gate_type": item.get("last_decision_gate_type"),
                "action": item.get("last_decision_action"),
                "actor": item.get("last_decision_actor"),
                "at": at,
                "reason": item.get("last_decision_reason"),
            }
        )
    _sort_items(items, "at", "capsule_id")
    return items[: max(0, min(int(limit), MAX_PAGE))]


def _quarantine_name(kind: str, path: str) -> str:
    """The public id of a quarantined item — a name, never a filesystem path."""
    base = Path(str(path)).name
    if kind == "ticket" and base.endswith(".json"):
        base = base[: -len(".json")]
        if base.startswith("ticket_"):
            base = base[len("ticket_") :]
    return base


def _quarantine_rows(report: Sequence[Any]) -> List[Dict[str, Any]]:
    """Shape the fail-closed skip report into id + reason code (no paths)."""
    rows: List[Dict[str, Any]] = []
    for item in report:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "record")
        path = str(item.get("path") or "")
        code = item.get("code") or _quarantine_code(str(item.get("detail") or ""))
        rows.append(
            {"kind": kind, "name": _quarantine_name(kind, path), "code": code}
        )
    return rows


def quarantined_items() -> Optional[List[Dict[str, Any]]]:
    """Vault records the last index build could not understand (id + reason).

    Read from the index's own ``meta`` row, so the attention read costs one
    query instead of re-walking the spool. ``None`` means the index is off.
    These records have no trustworthy sector (that is *why* they were
    rejected), so callers must treat them as visible only to a caller who may
    see every sector.
    """
    if not enabled() or not ensure():
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='quarantined'"
        ).fetchone()
    finally:
        conn.close()
    if not row or not isinstance(row[0], str):
        return []
    try:
        report = json.loads(row[0])
    except ValueError:
        return []
    return _quarantine_rows(report if isinstance(report, list) else [])


def quarantined_items_from_files() -> List[Dict[str, Any]]:
    """The file-scan fallback for :func:`quarantined_items`."""
    global _LAST_REBUILD_SKIPPED
    _LAST_REBUILD_SKIPPED = []
    _scan_files()
    return _quarantine_rows(_LAST_REBUILD_SKIPPED)

