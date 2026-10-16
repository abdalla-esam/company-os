#!/usr/bin/env python3
"""locking.py — crash-safe mutual exclusion for a capsule directory.

Cron pulses overlap: two dispatchers, a manual run, a retry storm. The file
system is our database, so the lock must live on the file system too — one
``.lock`` file per capsule, no daemon, no Redis, no external dependency.

Mechanism (``fcntl.flock`` on POSIX):
- the lock is held by an open **file description**, not by the file's
  existence, so it is released *by the kernel* when the holder exits, crashes,
  or is killed — a dead pulse can never wedge a capsule (no stale locks);
- acquisition is non-blocking + polled against a ``time.monotonic`` deadline,
  so a busy capsule fails fast with :class:`LockTimeoutError` instead of
  hanging a heartbeat;
- after acquiring, holder metadata (pid, host, purpose, timestamp) is written
  into the file — purely informational, for humans and for error messages.

Portable fallback (no ``fcntl``, e.g. Windows): atomic ``O_CREAT | O_EXCL``
creation, where *creating the file is the lock*. Because that scheme can go
stale on a crash, ``stale_after`` seconds are honored there: an ancient lock
file is broken once, loudly, with the old holder's metadata in the message.

Invariants:
- non-reentrant: acquiring twice from the same owner raises ``RuntimeError``;
- ``release()`` is idempotent (safe in ``finally`` blocks and ``__exit__``);
- on POSIX the ``.lock`` file persists after release — *its presence never
  means "locked"*, only ``flock`` decides; never unlink it (unlinking a locked
  file splits the inode and breaks mutual exclusion).
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capsule_engine.exceptions import CapsuleNotFoundError, LockTimeoutError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = ".lock"
DEFAULT_TIMEOUT = 30.0        # seconds a caller waits for a busy capsule
DEFAULT_POLL_INTERVAL = 0.05  # 50 ms between non-blocking attempts
DEFAULT_STALE_AFTER = 900.0   # 15 min: fallback backend only (flock self-heals)


@dataclass(frozen=True)
class LockInfo:
    """Holder metadata recovered from a lock file (best effort)."""

    pid: int | None
    host: str | None
    purpose: str | None
    acquired_at: str | None

    def describe(self) -> str:
        """Human-readable holder description for error messages."""
        parts = []
        if self.pid is not None:
            parts.append(f"pid={self.pid}")
        if self.host:
            parts.append(f"host={self.host}")
        if self.purpose:
            parts.append(f"purpose={self.purpose}")
        if self.acquired_at:
            parts.append(f"since={self.acquired_at}")
        return ", ".join(parts) if parts else "holder metadata unreadable"


def read_lock_info(capsule_path: str | os.PathLike[str]) -> LockInfo | None:
    """Parse the holder metadata out of a capsule's ``.lock`` file.

    Returns ``None`` when the file is absent; tolerates torn/partial writes
    (an empty or malformed lock file is still a valid lock — just anonymous).
    """
    lock_file = Path(capsule_path) / LOCK_NAME
    try:
        raw = lock_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return LockInfo(None, None, None, None)
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return LockInfo(None, None, None, None)
    if not isinstance(data, dict):
        return LockInfo(None, None, None, None)
    return LockInfo(
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        host=data.get("host") if isinstance(data.get("host"), str) else None,
        purpose=data.get("purpose") if isinstance(data.get("purpose"), str) else None,
        acquired_at=data.get("acquired_at")
        if isinstance(data.get("acquired_at"), str)
        else None,
    )


class CapsuleLock:
    """Advisory, non-reentrant lock over a single capsule directory.

    Usage::

        with CapsuleLock(capsule_path, timeout=10.0, purpose="marketing") as lock:
            manifest = load_manifest(capsule_path)
            ...
        # lock released here even if the body raised

    Args:
        capsule_path: directory that owns the ``.lock`` file.
        timeout: seconds to keep retrying before raising ``LockTimeoutError``.
            ``0`` means a single attempt (probe).
        poll_interval: seconds between non-blocking attempts.
        stale_after: fallback backend only — age in seconds after which a lock
            file left by a crashed process is considered dead and broken.
        purpose: short label (worker name) recorded in the lock metadata.

    Raises:
        CapsuleNotFoundError: the capsule directory does not exist.
        LockTimeoutError: the lock stayed busy for the whole timeout.
    """

    def __init__(
        self,
        capsule_path: str | os.PathLike[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stale_after: float = DEFAULT_STALE_AFTER,
        purpose: str = "worker",
    ) -> None:
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        self._capsule = Path(capsule_path)
        self._timeout = timeout
        self._poll = poll_interval
        self._stale_after = stale_after
        self._purpose = purpose
        self._fd: int | None = None
        if not self._capsule.is_dir():
            raise CapsuleNotFoundError(
                f"cannot lock a capsule that does not exist: {self._capsule}"
            )

    # -- context manager ---------------------------------------------------- #

    def __enter__(self) -> "CapsuleLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "held" if self._fd is not None else "free"
        return f"CapsuleLock({self._capsule.name!r}, {state})"

    # -- public API ---------------------------------------------------------- #

    @property
    def is_held(self) -> bool:
        """True while this instance currently owns the lock."""
        return self._fd is not None

    @property
    def lock_file(self) -> Path:
        """Path of the ``.lock`` file guarding the capsule."""
        return self._capsule / LOCK_NAME

    def acquire(self) -> "CapsuleLock":
        """Acquire the lock, polling until ``timeout`` elapses.

        Raises:
            RuntimeError: this instance already holds the lock (non-reentrant).
            LockTimeoutError: the lock is held by someone else.
        """
        if self._fd is not None:
            raise RuntimeError(
                f"CapsuleLock for {self._capsule} is non-reentrant; "
                "release() before acquiring again"
            )
        if fcntl is not None:
            self._acquire_flock()
        else:  # pragma: no cover - exercised only on platforms without fcntl
            self._acquire_excl()
        return self

    def release(self) -> None:
        """Release the lock if held. Idempotent: calling twice is a no-op."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        if fcntl is None:  # pragma: no cover - fallback backend only
            try:
                self.lock_file.unlink()
            except OSError:
                pass

    # -- backends ------------------------------------------------------------ #

    def _acquire_flock(self) -> None:
        """POSIX path: kernel-managed ``flock`` — self-healing on crash."""
        assert fcntl is not None
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            raise LockTimeoutError(
                str(self._capsule), f"cannot open lock file: {exc}"
            ) from exc
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        holder = read_lock_info(self._capsule)
                        detail = holder.describe() if holder else "unknown holder"
                        raise LockTimeoutError(
                            str(self._capsule),
                            f"held by {detail}; gave up after {self._timeout:.1f}s",
                        ) from None
                    time.sleep(self._poll)
            self._fd = fd
            self._write_metadata(fd)
        except BaseException:
            if self._fd is None:
                os.close(fd)  # never leak a descriptor on the failure path
            raise

    def _acquire_excl(self) -> None:
        """Portable fallback: creating the file atomically *is* the lock."""
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fd = os.open(
                    self.lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644
                )
            except FileExistsError:
                if self._maybe_break_stale():
                    continue  # stale lock cleared — retry creation immediately
                if time.monotonic() >= deadline:
                    holder = read_lock_info(self._capsule)
                    detail = holder.describe() if holder else "unknown holder"
                    raise LockTimeoutError(
                        str(self._capsule),
                        f"held by {detail}; gave up after {self._timeout:.1f}s",
                    ) from None
                time.sleep(self._poll)
                continue
            except OSError as exc:
                raise LockTimeoutError(
                    str(self._capsule), f"cannot create lock file: {exc}"
                ) from exc
            self._fd = fd
            self._write_metadata(fd)
            return

    def _maybe_break_stale(self) -> bool:
        """Remove a fallback-backend lock older than ``stale_after``."""
        try:
            age = time.time() - self.lock_file.stat().st_mtime
        except OSError:
            return True  # vanished between check and stat — retry immediately
        if age < self._stale_after:
            return False
        try:
            self.lock_file.unlink()
        except OSError:
            return False
        return True

    def _write_metadata(self, fd: int) -> None:
        """Record who holds the lock (informational; never trusted as truth)."""
        from capsule_engine.manifest import now_iso  # local import: no cycle

        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "purpose": self._purpose,
                "acquired_at": now_iso(),
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
