#!/usr/bin/env python3
"""dedup.py — the dynamic rolling deduplication window.

Every proposal the factory accepts (copy, concept, code submission, contract
draft — anything with text) is fingerprinted and appended to::

    data/memory/dedup_index.jsonl

A fingerprint stores three things and never the raw text:

- ``sha256`` of the normalized text — the exact-duplicate detector;
- a bounded set of hashed **word** vectors (unique content tokens);
- a bounded set of hashed **phrase** vectors (word bigrams).

The near-duplicate detector is deterministic Jaccard similarity at both
levels, blended 50/50 (word overlap catches rewording; phrase overlap keeps
generic domain boilerplate from inflating the score). No external libraries,
no embeddings service, CPU-light by construction.

New proposals are validated against the window before anything downstream is
spent on them. The window is **fully configurable and never hardcoded**:

    config/engine.json → dedup.window_days        (30 today; 15/60/90/custom)
    config/engine.json → dedup.similarity_threshold (0.70 by default)
    config/engine.json → dedup.domains.<name>     per-domain overrides

A proposal whose similarity with a historical item exceeds the threshold is
reported as ``duplicate`` — callers re-roll it automatically (see
:func:`reroll_guidance`, which produces the instruction appended to the
regeneration prompt). An exact normalized-hash hit is *always* a duplicate,
regardless of threshold: the same text is never lawfully re-published inside
its own window.

The ledger is hash-chained and flock-appended exactly like the feedback
ledger, so concurrent workers on different capsules never interleave lines
and tampering is detectable.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:  # POSIX advisory locking
    import fcntl
except ImportError:  # pragma: no cover - Windows: O_APPEND alone
    fcntl = None  # type: ignore[assignment]

from shared_kernel.atomic_io import atomic_text, sidecar_lock

logger = logging.getLogger("capsule_engine.dedup")

__all__ = [
    "INDEX_NAME",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_THRESHOLD",
    "DEFAULT_SHINGLE_SIZE",
    "Fingerprint",
    "DedupEntry",
    "DedupMatch",
    "DedupVerdict",
    "index_path",
    "normalize_text",
    "fingerprint",
    "similarity",
    "check",
    "record",
    "reroll_guidance",
    "compact",
    "stats",
]

INDEX_NAME = "dedup_index.jsonl"

DEFAULT_WINDOW_DAYS = 30.0
DEFAULT_THRESHOLD = 0.70
DEFAULT_SHINGLE_SIZE = 2
DEFAULT_MAX_SHINGLES = 2000
DEFAULT_MAX_ENTRIES = 20000

MAX_TEXT_CHARS = 200_000
MAX_LINE_BYTES = 256 * 1024

_URL = re.compile(r"https?://\S+")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9\u0600-\u06FF']+")
#: Contract-standard noise: pure numeric tokens and stopword-ish glue that
#: would inflate similarity between unrelated texts.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "been", "at", "by", "as", "it",
        "this", "that", "these", "those", "from", "we", "our", "you", "your",
    }
)
_NUMERIC = re.compile(r"^\d+$")


# --------------------------------------------------------------------------- #
# normalization & fingerprints
# --------------------------------------------------------------------------- #

def normalize_text(text: str) -> str:
    """Lowercase, drop URLs, collapse whitespace — the dedup canonical form."""
    if not isinstance(text, str):
        return ""
    lowered = text[:MAX_TEXT_CHARS].lower()
    lowered = _URL.sub(" ", lowered)
    return _WS.sub(" ", lowered).strip()


def _tokens(normalized: str) -> List[str]:
    tokens = [
        token
        for token in _TOKEN.findall(normalized)
        if token not in _STOPWORDS and not _NUMERIC.match(token)
    ]
    return tokens


def _shingle_hash(shingle: str) -> int:
    digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def _sample_sorted(values: "Iterable[int]", cap: int) -> Tuple[int, ...]:
    """Deduplicate, sort, and deterministically stride-sample to ``cap``."""
    ordered = sorted(set(values))
    if cap > 0 and len(ordered) > cap:
        step = math.ceil(len(ordered) / cap)
        ordered = ordered[::step][:cap]
    return tuple(ordered)


@dataclass(frozen=True)
class Fingerprint:
    """A proposal's anti-duplication identity (raw text never persisted)."""

    sha256: str
    words: Tuple[int, ...]
    phrases: Tuple[int, ...]
    tokens: int
    shingle_size: int

    @property
    def count(self) -> int:
        return len(self.words) + len(self.phrases)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha256": self.sha256,
            "words": list(self.words),
            "phrases": list(self.phrases),
            "count": self.count,
            "tokens": self.tokens,
            "shingle_size": self.shingle_size,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Fingerprint":
        sha = raw.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise ValueError("fingerprint sha256 must be a 64-char hex digest")

        def _vectors(key: str, legacy_key: Optional[str] = None) -> Tuple[int, ...]:
            values = raw.get(key)
            if values is None and legacy_key is not None:
                values = raw.get(legacy_key)
            if values is None:
                return ()
            if not isinstance(values, list):
                raise ValueError(f"fingerprint {key} must be a list")
            return tuple(
                int(value)
                for value in values
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            )

        words = _vectors("words", legacy_key="shingles")
        phrases = _vectors("phrases")
        if not words and not phrases:
            raise ValueError("fingerprint carries no vectors")
        size = raw.get("shingle_size", DEFAULT_SHINGLE_SIZE)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("fingerprint shingle_size must be a positive integer")
        tokens = raw.get("tokens", 0)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            tokens = 0
        return cls(
            sha256=sha,
            words=words,
            phrases=phrases,
            tokens=tokens,
            shingle_size=size,
        )


def fingerprint(
    text: str,
    *,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
    max_shingles: int = DEFAULT_MAX_SHINGLES,
) -> Fingerprint:
    """Build a deterministic two-level fingerprint for ``text``.

    Word vectors: every unique content token. Phrase vectors: word shingles of
    ``shingle_size`` (default 2) tokens. Each level is capped at
    ``max_shingles`` with a deterministic stride sample, so a 10 000-word
    document costs the same as a 2 000-word one (CPU-light, hardware
    agnostic). Empty content yields an all-hash fingerprint with no vectors.
    """
    normalized = normalize_text(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    tokens = _tokens(normalized)
    size = max(1, int(shingle_size))
    if not tokens:
        return Fingerprint(
            sha256=digest, words=(), phrases=(), tokens=0, shingle_size=size
        )

    words = _sample_sorted((_shingle_hash(token) for token in tokens), max_shingles)
    phrases: Tuple[int, ...] = ()
    if len(tokens) >= 2:
        span = min(size, len(tokens))
        phrases = _sample_sorted(
            (
                _shingle_hash(" ".join(tokens[i : i + span]))
                for i in range(len(tokens) - span + 1)
            ),
            max_shingles,
        )
    return Fingerprint(
        sha256=digest,
        words=words,
        phrases=phrases,
        tokens=len(tokens),
        shingle_size=size,
    )


def _jaccard(
    left: Tuple[int, ...], right: Tuple[int, ...]
) -> float:
    """Jaccard similarity of two sorted vector lists (merge walk, O(n+m))."""
    if not left or not right:
        return 0.0
    a, b = left, right
    i = j = 0
    intersection = 0
    union = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            intersection += 1
            union += 1
            i += 1
            j += 1
        elif a[i] < b[j]:
            union += 1
            i += 1
        else:
            union += 1
            j += 1
    union += (len(a) - i) + (len(b) - j)
    if union == 0:
        return 0.0
    return intersection / union


def similarity(left: Fingerprint, right: Fingerprint) -> float:
    """Blended Jaccard similarity of two fingerprints (0.0–1.0).

    50/50 word-level and phrase-level Jaccard; a fingerprint without phrase
    vectors (one-token text, legacy line) is compared at word level only.
    """
    word_score = _jaccard(left.words, right.words)
    if not left.phrases or not right.phrases:
        return word_score
    phrase_score = _jaccard(left.phrases, right.phrases)
    return 0.5 * word_score + 0.5 * phrase_score


# --------------------------------------------------------------------------- #
# ledger records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DedupEntry:
    """One historical proposal fingerprint in the rolling index."""

    at: str
    capsule_id: str
    domain: Optional[str]
    kind: str
    sha256: str
    words: Tuple[int, ...]
    phrases: Tuple[int, ...]
    count: int
    shingle_size: int
    tokens: int = 0
    prev: str = ""
    hash: str = ""

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(
            sha256=self.sha256,
            words=self.words,
            phrases=self.phrases,
            tokens=self.tokens,
            shingle_size=self.shingle_size,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "at": self.at,
            "capsule_id": self.capsule_id,
            "domain": self.domain,
            "kind": self.kind,
            "sha256": self.sha256,
            "words": list(self.words),
            "phrases": list(self.phrases),
            "count": self.count,
            "shingle_size": self.shingle_size,
            "tokens": self.tokens,
        }
        if self.prev:
            payload["prev"] = self.prev
        if self.hash:
            payload["hash"] = self.hash
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DedupEntry":
        at = raw.get("at")
        if not isinstance(at, str) or not at.strip():
            raise ValueError("entry timestamp is required")
        fp = Fingerprint.from_dict(raw)
        return cls(
            at=at,
            capsule_id=str(raw.get("capsule_id") or "unknown")[:120],
            domain=str(raw["domain"]) if isinstance(raw.get("domain"), str) else None,
            kind=str(raw.get("kind") or "proposal")[:64],
            sha256=fp.sha256,
            words=fp.words,
            phrases=fp.phrases,
            count=fp.count,
            shingle_size=fp.shingle_size,
            tokens=fp.tokens,
            prev=str(raw.get("prev") or ""),
            hash=str(raw.get("hash") or ""),
        )


@dataclass(frozen=True)
class DedupMatch:
    """The strongest historical match behind a verdict."""

    entry: DedupEntry
    similarity: float
    exact: bool


@dataclass(frozen=True)
class DedupVerdict:
    """The decision the caller acts on (record it, re-roll, or proceed)."""

    duplicate: bool
    similarity: float
    threshold: float
    window_days: float
    exact: bool
    checked: int
    matched: Optional[DedupMatch]
    fingerprint: Fingerprint
    reason: str

    @property
    def reroll(self) -> bool:
        """True when the proposal must be regenerated (the engine re-rolls)."""
        return self.duplicate

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly view for manifests, traces and the API."""
        return {
            "duplicate": self.duplicate,
            "similarity": round(self.similarity, 4),
            "threshold": self.threshold,
            "window_days": self.window_days,
            "exact": self.exact,
            "checked": self.checked,
            "reason": self.reason,
            "sha256": self.fingerprint.sha256,
            "matched": (
                {
                    "capsule_id": self.matched.entry.capsule_id,
                    "domain": self.matched.entry.domain,
                    "at": self.matched.entry.at,
                    "similarity": round(self.matched.similarity, 4),
                    "exact": self.matched.exact,
                }
                if self.matched is not None
                else None
            ),
        }


# --------------------------------------------------------------------------- #
# path & ledger plumbing
# --------------------------------------------------------------------------- #

def index_path(
    explicit: Optional[str | os.PathLike[str]] = None,
    *,
    policy: Optional[Any] = None,
) -> Path:
    """Resolve the dedup index: explicit → env → shared_kernel → default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("AGENCY_DEDUP_FILE", "").strip()
    if env:
        return Path(env)
    try:
        from shared_kernel.paths import DEDUP_INDEX_PATH

        return Path(DEDUP_INDEX_PATH)
    except Exception:
        return Path(__file__).resolve().parents[2] / "data" / "memory" / INDEX_NAME


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _chain_hash(prev: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev.encode("utf-8"))
    digest.update(b"\n")
    digest.update(_canonical(payload))
    return digest.hexdigest()


def _parse_ts(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_entries(
    path: Path, *, max_entries: int
) -> Tuple[List[DedupEntry], int]:
    """Read the newest ``max_entries`` complete lines; malformed are counted."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    # 256 KiB covers thousands of fingerprints; read the tail (newest matter).
    window_bytes = min(size, max(256 * 1024, max_entries * 4096))
    try:
        with path.open("rb") as handle:
            if window_bytes < size:
                handle.seek(size - window_bytes)
                handle.readline()
            data = handle.read()
    except OSError:
        return [], 0
    text = data.decode("utf-8", errors="replace")
    if not text.endswith("\n"):
        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
    entries: List[DedupEntry] = []
    malformed = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError("not an object")
            entries.append(DedupEntry.from_dict(raw))
        except (json.JSONDecodeError, ValueError, TypeError):
            malformed += 1
    if len(entries) > max_entries > 0:
        entries = entries[-max_entries:]
    return entries, malformed


def _tail_hash(fd: int) -> str:
    """The chain head, read from the last 256 KiB under the append lock."""
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        return ""
    start = max(0, size - 262144)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, size - start)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    prev = ""
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, Mapping) and isinstance(raw.get("hash"), str):
            prev = str(raw["hash"])
    return prev


def record(
    text: str,
    *,
    capsule_id: str,
    domain: Optional[str] = None,
    kind: str = "proposal",
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
    at: Optional[str] = None,
    max_shingles: Optional[int] = None,
    shingle_size: Optional[int] = None,
    enabled: bool = True,
) -> Optional[DedupEntry]:
    """Append a proposal's fingerprint to the rolling index.

    Returns the appended entry, or ``None`` when dedup is disabled, the text
    carries no usable tokens, or the disk refused the write (logged, never
    raised — recording memory must not abort the workflow that produced it).
    """
    if not enabled:
        return None
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            policy = None
    if policy is not None and not policy.dedup.enabled:
        return None
    effective = policy.dedup.for_domain(domain) if policy is not None else None
    if effective is not None and not effective.enabled:
        # Per-domain exclusion (e.g. development/operations/finance): code
        # duplication and daily server checks must never be deduped.
        return None
    size = int(
        shingle_size
        if shingle_size is not None
        else (effective.shingle_size if effective else DEFAULT_SHINGLE_SIZE)
    )
    cap = int(
        max_shingles
        if max_shingles is not None
        else (effective.max_shingles if effective else DEFAULT_MAX_SHINGLES)
    )
    fp = fingerprint(text, shingle_size=size, max_shingles=cap)
    if not fp.words and not fp.phrases:
        return None

    entry_at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = index_path(path)
    payload_core: Dict[str, Any] = {
        "at": entry_at,
        "capsule_id": str(capsule_id)[:120] or "unknown",
        "domain": domain,
        "kind": str(kind or "proposal")[:64],
        **fp.to_dict(),
    }

    payload: Dict[str, Any] = dict(payload_core)
    # One sidecar lock around tail-read + append, shared with ``compact``:
    # the chain head must be read and written atomically with respect to both
    # other appenders and the pruning rewrite, or fingerprints vanish.
    with sidecar_lock(ledger, purpose="dedup:record"):
        try:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(ledger, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o644)
        except OSError as exc:
            logger.warning("dedup: cannot open index %s: %s", ledger, exc)
            return None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            prev = _tail_hash(fd)
            payload["prev"] = prev
            payload["hash"] = _chain_hash(prev, payload)
            line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            if len(line) > MAX_LINE_BYTES:
                logger.warning("dedup: fingerprint line exceeds %d bytes — refused", MAX_LINE_BYTES)
                return None
            os.write(fd, line)
            os.fsync(fd)
        except OSError as exc:
            logger.warning("dedup: append to %s failed: %s", ledger, exc)
            return None
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    return DedupEntry.from_dict(payload)


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #

def check(
    text: str,
    *,
    domain: Optional[str] = None,
    capsule_id: Optional[str] = None,
    kind: str = "proposal",
    window_days: Optional[float] = None,
    threshold: Optional[float] = None,
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
    exclude_same_capsule: bool = True,
) -> DedupVerdict:
    """Validate a proposal against the rolling window.

    Rules (fail-closed, deterministic):

    1. an exact normalized-hash hit inside the window is always a duplicate;
    2. otherwise the best Jaccard similarity against windowed entries of the
       same domain is compared to the threshold (``> threshold`` wins — the
       requirement's ">70%" is strict);
    3. a proposal with too little content to fingerprint is *not* silently
       passed: it is reported as ``duplicate=False`` with a reason, and
       callers may still proceed (there is nothing to compare).

    ``exclude_same_capsule`` (default) keeps a capsule's own retries from
    tripping its own history: re-checking the same copy while reworking a
    capsule is a revision, not plagiarism.
    """
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            policy = None
    if policy is not None and not policy.dedup.enabled:
        fp_disabled = fingerprint(text)
        return DedupVerdict(
            duplicate=False,
            similarity=0.0,
            threshold=DEFAULT_THRESHOLD,
            window_days=DEFAULT_WINDOW_DAYS,
            exact=False,
            checked=0,
            matched=None,
            fingerprint=fp_disabled,
            reason="dedup disabled globally by policy",
        )
    effective = policy.dedup.for_domain(domain) if policy is not None else None
    if effective is not None and not effective.enabled:
        fp_disabled = fingerprint(text)
        return DedupVerdict(
            duplicate=False,
            similarity=0.0,
            threshold=effective.similarity_threshold,
            window_days=effective.window_days,
            exact=False,
            checked=0,
            matched=None,
            fingerprint=fp_disabled,
            reason="dedup disabled by policy",
        )

    window = float(
        window_days
        if window_days is not None
        else (effective.window_days if effective else DEFAULT_WINDOW_DAYS)
    )
    limit = float(
        threshold
        if threshold is not None
        else (effective.similarity_threshold if effective else DEFAULT_THRESHOLD)
    )
    if not (0.0 < limit <= 1.0):
        limit = DEFAULT_THRESHOLD
        logger.warning("dedup: threshold %r out of range — using %s", threshold, limit)

    fp = fingerprint(
        text,
        shingle_size=effective.shingle_size if effective else DEFAULT_SHINGLE_SIZE,
        max_shingles=effective.max_shingles if effective else DEFAULT_MAX_SHINGLES,
    )
    if not fp.words and not fp.phrases:
        return DedupVerdict(
            duplicate=False,
            similarity=0.0,
            threshold=limit,
            window_days=window,
            exact=False,
            checked=0,
            matched=None,
            fingerprint=fp,
            reason="proposal has too little content to fingerprint — nothing to compare",
        )

    ledger = index_path(path)
    entries, malformed = _read_entries(
        ledger, max_entries=effective.max_entries if effective else DEFAULT_MAX_ENTRIES
    )
    if malformed:
        logger.warning("dedup: %d malformed line(s) ignored in %s", malformed, ledger)

    since = datetime.fromtimestamp(
        time.time() - window * 86400.0, tz=timezone.utc
    )
    best: Optional[DedupMatch] = None
    checked = 0
    for entry in entries:
        if domain and (entry.domain or "") != domain:
            continue
        if exclude_same_capsule and capsule_id and entry.capsule_id == capsule_id:
            continue
        at = _parse_ts(entry.at)
        if at is None or at < since:
            continue
        checked += 1
        if entry.sha256 == fp.sha256:
            exact_match = DedupMatch(entry=entry, similarity=1.0, exact=True)
            return DedupVerdict(
                duplicate=True,
                similarity=1.0,
                threshold=limit,
                window_days=window,
                exact=True,
                checked=checked,
                matched=exact_match,
                fingerprint=fp,
                reason=(
                    f"exact duplicate of capsule {entry.capsule_id!r} "
                    f"({entry.kind}) inside the {window:g}-day window"
                ),
            )
        score = similarity(fp, entry.fingerprint)
        if best is None or score > best.similarity:
            best = DedupMatch(entry=entry, similarity=score, exact=False)

    if best is not None and best.similarity > limit:
        return DedupVerdict(
            duplicate=True,
            similarity=best.similarity,
            threshold=limit,
            window_days=window,
            exact=False,
            checked=checked,
            matched=best,
            fingerprint=fp,
            reason=(
                f"similarity {best.similarity:.1%} with capsule "
                f"{best.entry.capsule_id!r} ({best.entry.kind}) exceeds the "
                f"{limit:.0%} threshold inside the {window:g}-day window"
            ),
        )

    top = best.similarity if best is not None else 0.0
    return DedupVerdict(
        duplicate=False,
        similarity=top,
        threshold=limit,
        window_days=window,
        exact=False,
        checked=checked,
        matched=best,
        fingerprint=fp,
        reason=(
            f"unique ({checked} windowed proposal(s) checked; best similarity "
            f"{top:.1%} ≤ {limit:.0%} threshold)"
        ),
    )


def reroll_guidance(verdict: DedupVerdict) -> str:
    """The instruction appended to a regeneration prompt after a duplicate.

    Deterministic, evidence-based (names what it duplicated and how closely)
    and actionable (states the required novelty).
    """
    if not verdict.duplicate:
        return ""
    matched = verdict.matched
    lines = [
        "RE-ROLL REQUIRED — this proposal duplicated existing output and was rejected:",
        f"- {verdict.reason}",
        (
            f"- Produce a materially different angle, structure and wording: "
            f"similarity must fall below {verdict.threshold:.0%}."
        ),
    ]
    if matched is not None and matched.entry.domain:
        lines.append(
            f"- Do not reuse the framing of the {matched.entry.domain} item "
            f"from {matched.entry.at}."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# maintenance & stats
# --------------------------------------------------------------------------- #

def compact(
    *,
    window_days: Optional[float] = None,
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
) -> int:
    """Rewrite the index keeping only entries inside the window.

    The read and the replace run under the same sidecar lock ``record``
    takes, so a fingerprint appended between the read and the rewrite can no
    longer vanish; the write itself is atomic (temp + fsync + ``os.replace``)
    — a crash leaves the old ledger intact. Returns the number of entries
    kept. The chain is preserved verbatim for the kept lines (their
    ``prev``/``hash`` fields stay valid).
    """
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            policy = None
    window = float(
        window_days
        if window_days is not None
        else (policy.dedup.window_days if policy is not None else DEFAULT_WINDOW_DAYS)
    )
    ledger = index_path(path)
    if not ledger.is_file():
        return 0
    with sidecar_lock(ledger, purpose="dedup:compact"):
        try:
            raw_lines = ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        since = datetime.fromtimestamp(time.time() - window * 86400.0, tz=timezone.utc)
        kept: List[str] = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
                if not isinstance(decoded, Mapping):
                    continue
                at = _parse_ts(decoded.get("at"))
            except json.JSONDecodeError:
                continue
            if at is None or at < since:
                continue
            kept.append(line)
        atomic_text(ledger, "\n".join(kept) + ("\n" if kept else ""))
    logger.info("dedup: compacted %d -> %d entries", len(raw_lines), len(kept))
    return len(kept)


def stats(
    *,
    path: Optional[str | os.PathLike[str]] = None,
    window_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Aggregate views for the radar: counts by domain/kind in the window."""
    ledger = index_path(path)
    entries, malformed = _read_entries(ledger, max_entries=DEFAULT_MAX_ENTRIES)
    since = (
        datetime.fromtimestamp(time.time() - window_days * 86400.0, tz=timezone.utc)
        if window_days is not None and window_days > 0
        else None
    )
    by_domain: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    in_window = 0
    for entry in entries:
        if since is not None:
            at = _parse_ts(entry.at)
            if at is None or at < since:
                continue
        in_window += 1
        key = entry.domain or "(none)"
        by_domain[key] = by_domain.get(key, 0) + 1
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
    return {
        "path": str(ledger),
        "total": len(entries),
        "in_window": in_window,
        "window_days": window_days,
        "malformed": malformed,
        "by_domain": by_domain,
        "by_kind": by_kind,
    }
