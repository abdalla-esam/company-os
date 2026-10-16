#!/usr/bin/env python3
"""task_profile.py — B1 Task Profile: per-task stage declarations over a fixed
constitutional engine.

A capsule may ship ``task_profile.json`` declaring **its own** pipeline
stages: which worker owns each routable state, an optional acceptance note,
and an optional primary-artifact path (consumed by the B0 hash binding).

Hard rules (fail closed — a rejected card is *ignored*, never guessed):

- the card can only declare routable states: ``draft``, ``pending_qa``,
  ``pending_design``, ``needs_revision`` — the constitutional states
  (``pending_approval`` gate, ``approved``, ``published``, ``failed``,
  ``stalled``, ``archived``) are **forbidden** and rejecting the card;
- at most :data:`MAX_STAGES` stages, no duplicated state within the card;
- ``worker`` must be a non-empty string; whether it exists in the pulse
  registry is decided at routing time (unknown ⇒ the lawful ``no_worker``
  bucket, exactly as today);
- ``artifact`` (optional) must be a safe relative path — never absolute,
  never escaping the capsule (mirrors the engine's artifact rules);
- a malformed/unreadable card raises :class:`TaskProfileError`, and the
  router treats that as "no card" for the pass: the capsule proceeds on the
  default tables and the pulse reports it (nothing is silently simulated).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

try:  # normal import (package on sys.path)
    from capsule_engine.exceptions import SecurityError
except ImportError:  # script/odd-cwd execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine.exceptions import SecurityError

__all__ = [
    "TASK_PROFILE_NAME",
    "MAX_STAGES",
    "ROUTABLE_STATES",
    "FORBIDDEN_STATES",
    "TaskProfileError",
    "TaskProfile",
    "load_task_profile",
    "worker_for_state",
]

TASK_PROFILE_NAME = "task_profile.json"
MAX_STAGES = 8

# States a card may declare — mirrors the spec: router-owned workflow stages.
ROUTABLE_STATES = frozenset(
    {"draft", "pending_qa", "pending_design", "needs_revision"}
)
# Constitutional states a card may NEVER claim.
FORBIDDEN_STATES = frozenset(
    {"pending_approval", "approved", "published", "stalled", "failed", "archived",
     "in_progress", "in_review"}
)

VERSION = 1


class TaskProfileError(Exception):
    """The task card is malformed or violates the contract — ignore the card."""


class TaskProfile:
    """A validated task card: immutable mapping state → worker (+ acceptance)."""

    __slots__ = ("kind", "artifact", "_stages", "_acceptance")

    def __init__(
        self,
        stages: Mapping[str, str],
        *,
        kind: str = "",
        artifact: Optional[str] = None,
        acceptance: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.kind = str(kind or "")
        self.artifact = artifact
        self._stages: dict[str, str] = dict(stages)
        self._acceptance: dict[str, str] = dict(acceptance or {})

    def worker_for(self, state: str) -> Optional[str]:
        """The declared worker for ``state``, or None when undeclared."""
        return self._stages.get(state)

    def acceptance_for(self, state: str) -> Optional[str]:
        return self._acceptance.get(state)

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self._stages)


def _reject(reason: str) -> None:
    raise TaskProfileError(reason)


def _validate_artifact(path: str) -> str:
    """Accept a safe relative artifact path; refuse anything else."""
    if not isinstance(path, str) or not path.strip():
        _reject("artifact must be a non-empty relative path")
    candidate = Path(path)
    if candidate.is_absolute():
        _reject(f"artifact must be relative to the capsule: {path!r}")
    resolved = Path(path.strip())
    if ".." in resolved.parts or resolved.parts and resolved.parts[0] in ("~", "/"):
        _reject(f"artifact escapes the capsule: {path!r}")
    return path.strip()


def _parse_card(data: Any) -> TaskProfile:
    """Validate a decoded card; raise :class:`TaskProfileError` on any breach."""
    if not isinstance(data, dict):
        _reject("task profile must be a JSON object")

    if data.get("version") != VERSION:
        _reject(f"unsupported task profile version: {data.get('version')!r}")

    stages_raw = data.get("stages")
    if not isinstance(stages_raw, list) or not (1 <= len(stages_raw) <= MAX_STAGES):
        _reject(f"stages must be a list of 1..{MAX_STAGES} entries")

    stages: dict[str, str] = {}
    acceptance: dict[str, str] = {}
    for entry in stages_raw:
        if not isinstance(entry, dict):
            _reject("each stage must be an object")
        state = entry.get("state")
        worker = entry.get("worker")
        if not isinstance(state, str) or not state.strip():
            _reject("stage.state must be a non-empty string")
        state = state.strip()
        if state in FORBIDDEN_STATES:
            _reject(f"constitutional state {state!r} cannot be declared by a card")
        if state not in ROUTABLE_STATES:
            _reject(f"state {state!r} is not a card-declarable (routable) state")
        if state in stages:
            _reject(f"duplicate stage for state {state!r}")
        if not isinstance(worker, str) or not worker.strip():
            _reject(f"stage.worker for {state!r} must be a non-empty string")
        stages[state] = worker.strip()
        note = entry.get("acceptance")
        if isinstance(note, str) and note.strip():
            acceptance[state] = note.strip()

    kind = data.get("kind")
    if kind is not None and not isinstance(kind, str):
        _reject("kind must be a string")

    artifact = data.get("artifact")
    artifact_path = _validate_artifact(artifact) if artifact is not None else None

    return TaskProfile(
        stages,
        kind=kind or "",
        artifact=artifact_path,
        acceptance=acceptance,
    )


def load_task_profile(capsule_path: str | os.PathLike[str]) -> Optional[TaskProfile]:
    """Load and validate ``task_profile.json`` from a capsule directory.

    Returns:
        TaskProfile: a valid card.
        None: no card is present.

    Raises:
        TaskProfileError: a card exists but breaches the contract — callers
            treat this as "no card" for the pass and report it.
    """
    card = Path(capsule_path) / TASK_PROFILE_NAME
    if not card.is_file():
        return None
    try:
        data = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskProfileError(f"unreadable task profile: {exc}") from exc
    return _parse_card(data)


def worker_for_state(
    profile: Optional[TaskProfile], state: str
) -> Optional[str]:
    """Convenience: the card's worker for ``state`` (None when undeclared)."""
    return profile.worker_for(state) if profile is not None else None


# Re-exported so the router (and readers) see the security boundary next to
# the contract: the artifact path is validated against the same traversal
# rules the engine enforces when hashing.
_ = SecurityError
