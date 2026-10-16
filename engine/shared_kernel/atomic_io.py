#!/usr/bin/env python3
"""atomic_io.py — crash-safe file primitives for a filesystem-as-database.

One shared helper for every writer that cannot afford to lose or tear data.
All of it is extracted from the proven ``save_manifest`` /
``capsule_engine.memory`` patterns — same semantics, one implementation.

Primitives
----------
- :func:`atomic_bytes` / :func:`atomic_text` / :func:`atomic_json` — write to
  a temp file *in the same directory* (named ``.<name>.<pid>.tmp`` so a crash
  never leaves a torn target and two processes never share a temp name),
  ``flush`` + ``fsync``, ``chmod``, then ``os.replace`` (atomic on POSIX and
  Windows), then a best-effort directory fsync so the rename itself survives
  a crash. A fault at any point leaves the old file or the new one — never a
  torn file — and never leaves temp litter behind.
- :func:`append_jsonl_line` — one complete JSONL line per call:
  ``O_APPEND`` + exclusive ``flock`` + ``fsync``. Concurrent appenders on any
  number of processes never interleave bytes.
- :func:`sidecar_lock` / :func:`update_json` — lock-serialized
  read-modify-write. The lock lives on a **sidecar** file
  (``.<name>.lock`` next to ``<name>``), *never on the data file itself*:
  ``os.replace`` swaps inodes, so a lock on the data file would be orphaned
  mid-critical-section. The sidecar is hidden (leading dot), so the many
  directory scanners in this repo (``gate_*.json`` globs, queue walkers)
  never mistake it for data. On POSIX the lock is ``fcntl.flock`` on an open
  file description — the kernel releases it when the holder dies, so a
  crashed writer can never wedge the file. The portable fallback is atomic
  ``O_CREAT | O_EXCL`` creation with a stale-break after
  :data:`DEFAULT_LOCK_STALE` seconds.

Invariants (all callers rely on them):
- temp file lives in the target directory — ``os.replace`` is a rename, never
  a cross-device copy;
- the sidecar lock is **non-reentrant**: never nest two acquisitions on the
  same file from one thread (the second blocks against the first);
- on POSIX the sidecar persists after release — *its presence never means
  "locked"*, only ``flock`` decides; never unlink it (unlinking a locked file
  splits the inode and breaks mutual exclusion).

Standard library only — importable from engine code *and* from subprocess
scripts (``data/governance/approval_gates/gate_script.py``). Python 3.9+.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Union

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

PathLike = Union[str, os.PathLike]

#: Mode applied to freshly written files (and their temp parents before the
#: rename), matching the long-standing ``save_manifest`` behavior.
DEFAULT_MODE = 0o644

#: Seconds a caller waits for a busy sidecar before raising LockTimeoutError.
DEFAULT_LOCK_TIMEOUT = 30.0
#: Seconds between non-blocking lock attempts.
DEFAULT_LOCK_POLL = 0.05
#: Fallback backend only: age after which an O_EXCL lock file left by a dead
#: process is broken once, loudly. flock self-heals and never needs this.
DEFAULT_LOCK_STALE = 900.0

_LOCK_PURPOSE = "atomic_io"


class LockTimeoutError(TimeoutError):
    """The sidecar lock stayed busy for the whole timeout."""


# --------------------------------------------------------------------------- #
# durability primitives
# --------------------------------------------------------------------------- #

def fsync_dir(directory: PathLike) -> None:
    """Best-effort directory fsync so a rename survives a crash.

    Not every platform lets a directory be opened; failure there is
    acceptable (the data file itself was fsynced), so this never raises.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def temp_path_for(target: PathLike) -> Path:
    """The same-directory temp path an atomic write of ``target`` uses."""
    target = Path(target)
    return target.with_name(f".{target.name}.{os.getpid()}.tmp")


def atomic_bytes(
    path: PathLike,
    data: bytes,
    *,
    mode: int = DEFAULT_MODE,
    sync_dir: bool = True,
) -> Path:
    """Atomically write ``data`` to ``path``; the old file survives any fault.

    Raises:
        OSError: the write, chmod, or replace failed (the temp file is
            removed first — no litter, and the target is untouched).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(target)
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)  # atomic on POSIX and Windows
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave litter behind
        raise
    if sync_dir:
        fsync_dir(target.parent)
    return target


def atomic_text(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = DEFAULT_MODE,
    sync_dir: bool = True,
) -> Path:
    """Atomically write a Unicode string (see :func:`atomic_bytes`)."""
    return atomic_bytes(path, text.encode(encoding), mode=mode, sync_dir=sync_dir)


def atomic_json(
    path: PathLike,
    obj: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    trailing_newline: bool = True,
    mode: int = DEFAULT_MODE,
    sync_dir: bool = True,
) -> Path:
    """Atomically write ``obj`` as pretty-printed JSON.

    ``trailing_newline`` defaults to True (the ``save_manifest`` /
    ``governance.json`` convention); pass False for byte-identical parity
    with writers that never appended one (the gate queue).
    """
    payload = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        payload += "\n"
    return atomic_text(path, payload, mode=mode, sync_dir=sync_dir)


def append_jsonl_line(path: PathLike, line: str, *, encoding: str = "utf-8") -> None:
    """Append exactly one complete line to a JSONL file, durably.

    ``O_APPEND`` guarantees the write lands at end-of-file even against
    concurrent writers; the exclusive ``flock`` keeps whole lines from
    interleaving on any platform where fcntl exists; the ``fsync`` makes the
    line survive a machine crash, not just a process crash. One ``write`` of
    one buffer means a reader observes either the whole line or none of it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode(encoding)
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, DEFAULT_MODE)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# sidecar locking + read-modify-write
# --------------------------------------------------------------------------- #

def sidecar_lock_path(path: PathLike) -> Path:
    """``<dir>/.<name>.lock`` — hidden, and never the data file itself.

    A lock file name that already starts with a dot (``.audit_ledger.jsonl``)
    keeps its dot and gains the suffix, so it stays invisible to the
    directory scanners that skip hidden files.
    """
    path = Path(path)
    name = path.name
    return path.with_name(name + ".lock" if name.startswith(".") else f".{name}.lock")


def _write_lock_metadata(fd: int, purpose: str) -> None:
    """Record who holds the lock (informational; never trusted as truth)."""
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "purpose": purpose,
            "acquired_at": time.time(),
        },
        ensure_ascii=False,
    )
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    except OSError:
        pass  # metadata is a courtesy; the flock itself is already held


@contextmanager
def sidecar_lock(
    path: PathLike,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll_interval: float = DEFAULT_LOCK_POLL,
    stale_after: float = DEFAULT_LOCK_STALE,
    purpose: str = _LOCK_PURPOSE,
) -> Iterator[Path]:
    """Exclusive advisory lock on the sidecar of ``path`` (see module docs).

    Yields the sidecar lock path. ``LockTimeoutError`` if a busy lock outlives
    ``timeout`` (``0`` probes once). Non-reentrant — a second acquisition on
    the same file from the same thread deadlocks until the timeout.
    """
    if timeout < 0:
        raise ValueError("timeout must be >= 0")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be > 0")
    lock_file = sidecar_lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, DEFAULT_MODE)
        acquired = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"{lock_file}: held by another writer; "
                            f"gave up after {timeout:.1f}s"
                        ) from None
                    time.sleep(poll_interval)
            _write_lock_metadata(fd, purpose)
            yield lock_file
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
    else:  # pragma: no cover - exercised only on platforms without fcntl
        deadline = time.monotonic() + timeout
        fd = None
        while True:
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR, DEFAULT_MODE)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_file.stat().st_mtime
                except OSError:
                    continue  # vanished between check and stat — retry now
                if age >= stale_after:
                    try:
                        lock_file.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"{lock_file}: held by another writer; "
                        f"gave up after {timeout:.1f}s"
                    ) from None
                time.sleep(poll_interval)
        try:
            _write_lock_metadata(fd, purpose)
            yield lock_file
        finally:
            os.close(fd)
            try:
                lock_file.unlink()
            except OSError:
                pass


def update_json(
    path: PathLike,
    fn: Callable[[Any], Any],
    *,
    default: Any = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    trailing_newline: bool = True,
) -> Any:
    """Lock-serialized read-modify-write of a JSON document.

    Under the sidecar lock of ``path``: read the file (a missing file yields
    ``default``), call ``fn(state)``, and atomically write the result back.

    ``fn`` returns the new state to persist, or ``None`` to signal "no change"
    — nothing is written then (an unchanged document is never rewritten, and a
    missing file is never created). Returning the *same object it was handed*
    counts as a change and is written.

    ``trailing_newline`` is passed straight through to :func:`atomic_json`,
    so a writer keeps the exact byte format of the code it replaced.

    Raises:
        json.JSONDecodeError: the file exists but is not valid JSON — the
            corruption is surfaced loudly instead of being overwritten.
        LockTimeoutError: the sidecar lock stayed busy for ``timeout``.
        OSError: the read or the atomic write failed.

    Returns:
        The newly written state, or the state as read when ``fn`` returned
        None. Callers that need a computed result from inside the critical
        section should capture it in a closure.
    """
    target = Path(path)
    with sidecar_lock(target, timeout=timeout, purpose=f"update_json:{target.name}"):
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            state = default
        else:
            state = default if not raw.strip() else json.loads(raw)
        new_state = fn(state)
        if new_state is None:
            return state
        atomic_json(
            target,
            new_state,
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=sort_keys,
            trailing_newline=trailing_newline,
        )
        return new_state


__all__ = [
    "DEFAULT_MODE",
    "LockTimeoutError",
    "append_jsonl_line",
    "atomic_bytes",
    "atomic_json",
    "atomic_text",
    "fsync_dir",
    "sidecar_lock",
    "sidecar_lock_path",
    "temp_path_for",
    "update_json",
]
