#!/usr/bin/env python3
"""exceptions.py — the closed error hierarchy of the Sovereign Agency OS.

Every failure the engine raises on purpose derives from :class:`CapsuleError`,
so callers can catch one base class at the pulse boundary and fail closed
without ever swallowing an unrelated ``OSError`` or ``KeyboardInterrupt``.

Rules:
- errors carry context (capsule path, holder pid, JSON line/col), never guesses;
- corruption is *reported*, never repaired silently;
- the original exception is always chained (``raise ... from exc``).
"""

from __future__ import annotations


class CapsuleError(Exception):
    """Base class for every deliberate failure raised by the capsule engine."""


class CapsuleNotFoundError(CapsuleError):
    """A capsule directory (or the manifest inside it) does not exist."""


class ManifestError(CapsuleError):
    """Base class for every manifest read/validation failure."""


class ManifestCorruptError(ManifestError):
    """manifest.json exists but is not readable JSON (or not an object)."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"corrupt manifest at {path}: {detail}")


class ManifestSchemaError(ManifestError):
    """manifest.json parsed, but violates the schema (fields missing/invalid)."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"manifest schema violation at {path}: {detail}")


class ManifestTooLargeError(ManifestError):
    """A manifest would exceed the reader's size cap — refused before writing.

    Raised by :func:`capsule_engine.manifest.save_manifest` so a capsule can
    never brick itself with a file ``load_manifest`` would reject.
    """

    def __init__(self, path: str, size: int, limit: int) -> None:
        self.path = path
        self.size = size
        self.limit = limit
        super().__init__(
            f"manifest too large to save at {path}: {size} bytes > {limit} bytes "
            "(trim history or split the capsule)"
        )


class StateTransitionError(CapsuleError):
    """An illegal state transition was attempted against the transition matrix."""


class SecurityError(CapsuleError):
    """A fail-closed integrity refusal: artifact binding, verification, or path.

    Raised by the TOCTOU hash-binding layer when an artifact cannot be hashed,
    has no bound digest to verify against, or was altered after it was bound.
    Nothing is mutated when this is raised.
    """


class CapabilityError(SecurityError):
    """A fail-closed capability refusal: the key is missing, forged, expired,
    malformed, or grants a capability outside the closed vocabulary.

    Derives from :class:`SecurityError` on purpose: every existing
    ``except SecurityError`` / ``except CapsuleError`` handler already fails
    closed on it, so a capability check can never be bypassed by a caller that
    predates it. Nothing is mutated when this is raised.
    """


class UnsafePathError(CapsuleError):
    """A relative path tried to escape its capsule directory (path traversal)."""


class LockTimeoutError(CapsuleError):
    """The capsule .lock could not be acquired before the deadline."""

    def __init__(self, capsule_path: str, detail: str) -> None:
        self.capsule_path = capsule_path
        self.detail = detail
        super().__init__(f"capsule {capsule_path} is locked: {detail}")


class StateMismatchError(CapsuleError):
    """A worker was asked to execute a manifest state it does not handle."""


class WorkerExecutionError(CapsuleError):
    """A worker's ``execute`` raised; the failure has been recorded on disk."""
