#!/usr/bin/env python3
"""task_type.py — the Task Type loader: the pipeline as data, the constitution as code.

A **task type** is a declarative document (``config/task_types/<id>.json``) that
describes one kind of work as an ordered list of steps: which worker owns each
step, which gate (if any) the step must pass, what the step accepts as done, and
which outputs it produces. The engine reads it, validates it, and can render the
accepted B1 task card (``task_profile.json``) from it.

Why this module exists (design: ``docs/TASK_TYPES.md``):

- the engine must serve many task types — post, document, video, translation —
  **without changing the engine for each one**. Pipelines belong in data; the
  constitution (the approval boundary, gate ordering, the lease, the freeze,
  the terminal states) belongs in code and is untouched by a task type.
- **workers are named, never imported.** A step declares ``"worker": "qa"`` — a
  registry identifier resolved at route time against the live worker catalogue.
  A task type can therefore never reach around the registry into a domain
  package, which is what keeps the core core (see
  ``tests/test_dependency_direction.py``).

Contract, in one breath — **fail closed**:

- the document schema is *closed*: an unknown top-level key, step key, output
  key or artifact key is refused, so a typo can never be silently ignored;
- ordering is *structural*: every ``on_success``/``on_reject`` target must name
  an existing step or a reserved target, and at least one step must reach the
  approval boundary (a task type that can never face a human gate is
  inadmissible);
- every failure raises :class:`TaskTypeError` naming the offending element; a
  refused task type is never partially applied and never guessed at.

Deliberately absent: any token/price budget. The central ``UsageLedger`` /
``spend_cap`` does not exist yet, so a validated-but-ignored ``budget`` block
would be a lie — the field arrives with the cost ledger, and until then
``budget`` is refused as an unknown key.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from capsule_engine.exceptions import CapsuleError
from capsule_engine.task_profile import TASK_PROFILE_NAME

__all__ = [
    "TASK_TYPES_DIRNAME",
    "ENV_TASK_TYPES_DIR",
    "DEFAULT_TASK_TYPES_DIR",
    "CARD_NAME",
    "VERSION",
    "MAX_STEPS",
    "MAX_ACCEPTANCE_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_OUTPUTS_PER_STEP",
    "BOUNDARY_GATE",
    "RESERVED_TARGETS",
    "ROUTABLE_STATES",
    "TaskTypeError",
    "StepOutput",
    "StepBranch",
    "TaskTypeStep",
    "TaskType",
    "task_types_dir",
    "is_valid_id",
    "load_task_type",
    "list_task_types",
    "unregistered_workers",
    "clear_cache",
]

# --------------------------------------------------------------------------- #
# vocabulary & bounds
# --------------------------------------------------------------------------- #

TASK_TYPES_DIRNAME = "task_types"
#: Environment override for the catalogue directory (tests, ops).
ENV_TASK_TYPES_DIR = "AGENCY_TASK_TYPES_DIR"
#: ``engine/capsule_engine/task_type.py`` -> repo root -> ``config/task_types``.
DEFAULT_TASK_TYPES_DIR: Path = (
    Path(__file__).resolve().parents[2] / "config" / TASK_TYPES_DIRNAME
)

#: The accepted B1 task card this module renders (single source of truth).
CARD_NAME = TASK_PROFILE_NAME

VERSION = 1
MAX_STEPS = 12
MAX_ACCEPTANCE_CHARS = 500
MAX_TITLE_CHARS = 120
MAX_OUTPUTS_PER_STEP = 16

#: The approval boundary is a *marker*, never a step name: a step whose gate is
#: ``domain_template`` is where the human gate is seated.
BOUNDARY_GATE = "domain_template"

#: Routing targets that are not step ids.
RESERVED_TARGETS: frozenset = frozenset({"next", "needs_revision"})

#: The legacy states a step may declare so the B1 card can be rendered from it
#: during the compatibility phase (see docs/TASK_TYPES.md §5.1). Restricted to
#: the card's own routable vocabulary — a task type cannot invent a state.
ROUTABLE_STATES: frozenset = frozenset(
    {"draft", "pending_qa", "pending_design", "needs_revision"}
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_WORKER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: ``flags.requires_image`` — a namespaced routing flag, never a bare word.
_BRANCH_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")

_TOP_LEVEL_KEYS = frozenset(
    {"task_type", "version", "title", "domain", "artifacts", "inputs",
     "gate_template", "steps"}
)
_STEP_KEYS = frozenset(
    {"id", "worker", "gate", "acceptance", "outputs", "state", "on_success", "on_reject"}
)
_OUTPUT_KEYS = frozenset({"kind", "default_path"})
_ARTIFACT_KEYS = frozenset({"primary"})
_INPUT_KEYS = frozenset({"required", "optional", "defaults"})
_BRANCH_KEYS = frozenset({"branch_on", "when_true", "when_false"})


class TaskTypeError(CapsuleError):
    """A task type document is malformed or unsupported — refuse it, never guess."""


# --------------------------------------------------------------------------- #
# parsed model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StepOutput:
    """One declared deliverable of a step."""

    kind: str
    default_path: str

    def to_view(self) -> Dict[str, str]:
        return {"kind": self.kind, "default_path": self.default_path}


@dataclass(frozen=True)
class StepBranch:
    """A declared conditional edge: ``branch_on`` picks ``when_true``/``when_false``."""

    branch_on: str
    when_true: str
    when_false: str

    def to_view(self) -> Dict[str, str]:
        return {
            "branch_on": self.branch_on,
            "when_true": self.when_true,
            "when_false": self.when_false,
        }


@dataclass(frozen=True)
class TaskTypeStep:
    """One ordered step of a task type (validated)."""

    step_id: str
    worker: Optional[str]
    gate: Optional[str]
    acceptance: str
    outputs: Tuple[StepOutput, ...] = ()
    state: Optional[str] = None
    on_success: Optional[str] = None
    on_success_branch: Optional[StepBranch] = None
    on_reject: Optional[str] = None

    @property
    def is_boundary(self) -> bool:
        """True when this step is the approval boundary (no worker runs it)."""
        return self.gate == BOUNDARY_GATE

    def to_view(self) -> Dict[str, Any]:
        view: Dict[str, Any] = {
            "id": self.step_id,
            "worker": self.worker,
            "gate": self.gate,
            "state": self.state,
            "acceptance": self.acceptance,
            "boundary": self.is_boundary,
            "outputs": [output.to_view() for output in self.outputs],
            "on_success": self.on_success,
            "on_reject": self.on_reject,
        }
        if self.on_success_branch is not None:
            view["on_success"] = {"branch": self.on_success_branch.to_view()}
        return view


@dataclass(frozen=True)
class TaskType:
    """A validated task type: the pipeline as data."""

    task_type: str
    version: int
    title: str
    domain: str
    primary_artifact: Optional[str]
    required_inputs: Tuple[str, ...]
    optional_inputs: Tuple[str, ...]
    input_defaults: Mapping[str, Any]
    gate_template: Any  # a template name (str) or an inline template (tuple)
    steps: Tuple[TaskTypeStep, ...]
    path: str

    # -- lookups ------------------------------------------------------- #

    def step(self, step_id: str) -> Optional[TaskTypeStep]:
        """The step with ``step_id``, or ``None``."""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def boundary_step(self) -> Optional[TaskTypeStep]:
        """The step that seats the approval gate, or ``None``."""
        for step in self.steps:
            if step.is_boundary:
                return step
        return None

    def entry_step(self) -> Optional[TaskTypeStep]:
        """The step that owns the start (and the rework) of the pipeline.

        Declared as the step whose ``state`` is ``draft``; absent that, the
        first step that runs a worker. ``None`` for a worker-less plan.
        """
        for step in self.steps:
            if step.state == "draft" and step.worker:
                return step
        for step in self.steps:
            if step.worker:
                return step
        return None

    def declared_states(self) -> Tuple[str, ...]:
        """Every legacy state this task type's steps declare (declaration order)."""
        seen: List[str] = []
        for step in self.steps:
            if step.state and step.state not in seen:
                seen.append(step.state)
        return tuple(seen)

    def worker_for_state(self, state: str) -> Optional[str]:
        """The worker this task type puts at ``state``, or ``None``.

        ``draft`` and ``needs_revision`` fall back to the entry step: rework
        returns to the author, which is the one routing rule the legacy table
        also encodes (``DEFAULT_WORKER_BY_STATE``).
        """
        for step in self.steps:
            if step.state == state and step.worker:
                return step.worker
        if state in ("draft", "needs_revision"):
            entry = self.entry_step()
            if entry is not None:
                return entry.worker
        return None

    # -- rendering ----------------------------------------------------- #

    def to_task_profile(self) -> Dict[str, Any]:
        """Render the accepted B1 task card (``task_profile.json``) for this type.

        The card is the *compatibility seam*: the router reads it today, so a
        task type can describe a pipeline without any engine edit. Only
        routable states are rendered; the card's own validator is the last word
        (it refuses anything else).

        Raises:
            TaskTypeError: the type declares no routable stage at all, so a card
                could not be rendered (refused rather than written empty).
        """
        stages: List[Dict[str, str]] = []
        rendered: set = set()

        entry = self.entry_step()
        if entry is not None and entry.worker:
            # Rework returns to the author: draft and needs_revision share the
            # entry worker, exactly as DEFAULT_WORKER_BY_STATE maps them.
            for state in ("draft", "needs_revision"):
                stages.append(
                    {
                        "state": state,
                        "worker": entry.worker,
                        "acceptance": entry.acceptance,
                    }
                )
                rendered.add(state)

        for step in self.steps:
            if not step.state or not step.worker or step.state in rendered:
                continue
            stages.append(
                {
                    "state": step.state,
                    "worker": step.worker,
                    "acceptance": step.acceptance,
                }
            )
            rendered.add(step.state)

        if not stages:
            raise TaskTypeError(
                f"task type {self.task_type!r} declares no routable stage "
                "(no step carries a worker and a 'state'), so no task card can "
                "be rendered"
            )

        card: Dict[str, Any] = {
            "version": 1,
            "kind": self.task_type,
            "stages": stages,
        }
        if self.primary_artifact:
            card["artifact"] = self.primary_artifact
        return card

    def to_view(self) -> Dict[str, Any]:
        """A JSON-friendly view for the catalogue endpoints (no behaviour)."""
        view: Dict[str, Any] = {
            "task_type": self.task_type,
            "version": self.version,
            "title": self.title,
            "domain": self.domain,
            "primary_artifact": self.primary_artifact,
            "gate_template": (
                self.gate_template
                if isinstance(self.gate_template, str)
                else list(self.gate_template) if self.gate_template else None
            ),
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "steps": [step.to_view() for step in self.steps],
            "path": self.path,
        }
        return view


# --------------------------------------------------------------------------- #
# directory resolution & caching
# --------------------------------------------------------------------------- #

#: ``path`` -> ``(mtime_ns, TaskType)``. A file edited on disk is re-read, so a
#: restart is not required to see a fix — but the catalogue is still deploy-time
#: configuration with no write API.
_CACHE: Dict[str, Tuple[int, TaskType]] = {}


def task_types_dir() -> Path:
    """The catalogue directory: ``$AGENCY_TASK_TYPES_DIR`` or ``config/task_types``."""
    for var in (ENV_TASK_TYPES_DIR, "AGENCY_TASK_TYPES_FILE"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override)
    return DEFAULT_TASK_TYPES_DIR


def is_valid_id(task_type: object) -> bool:
    """True when ``task_type`` is a well-formed task type id."""
    return isinstance(task_type, str) and bool(_ID_RE.match(task_type.strip()))


def clear_cache() -> None:
    """Forget every parsed task type (tests and hot-fix reloads)."""
    _CACHE.clear()


# --------------------------------------------------------------------------- #
# parsing helpers (every refusal names the offending element)
# --------------------------------------------------------------------------- #

def _reject(message: str) -> "TaskTypeError":
    return TaskTypeError(message)


def _mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _reject(f"{what} must be an object")
    return value


def _text(
    value: object,
    what: str,
    *,
    pattern: "re.Pattern[str] | None" = None,
    max_chars: Optional[int] = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _reject(f"{what} must be a non-empty string")
    text = value.strip()
    if pattern is not None and not pattern.match(text):
        raise _reject(f"{what} is malformed: {text!r}")
    if max_chars is not None and len(text) > max_chars:
        raise _reject(f"{what} exceeds {max_chars} characters")
    return text


def _no_unknown_keys(raw: Mapping[str, Any], allowed: frozenset, what: str) -> None:
    unknown = sorted(str(key) for key in set(raw) - allowed)
    if not unknown:
        return
    detail = "the schema is closed"
    if "budget" in unknown:
        # Decision docs/TASK_TYPES.md §8.4: the field lands with the cost ledger.
        detail = (
            "a 'budget' block is not supported yet — it arrives with the cost "
            "ledger (UsageLedger / spend_cap)"
        )
    raise _reject(f"{what}: unknown key(s) {', '.join(unknown)} — {detail}")


def _safe_relative(relative: object, what: str) -> str:
    """Accept a capsule-relative path; refuse absolute/escaping/NUL paths.

    Delegates to the manifest's own rule (``manifest._safe_output_path``) so
    there is exactly one traversal check in the engine, not two that can drift.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise _reject(f"{what} must be a non-empty string")
    from capsule_engine import manifest as manifest_engine

    try:
        return manifest_engine._safe_output_path(relative)
    except Exception as exc:  # ManifestSchemaError and friends -> refuse here
        raise _reject(f"{what}: {exc}") from exc


def _known_gate_types() -> frozenset:
    """The engine's closed gate vocabulary (lazy import: gates imports manifest)."""
    from capsule_engine import gates as gate_engine

    return frozenset(gate_engine.GATE_TYPES)


def _strings(value: object, what: str) -> Tuple[str, ...]:
    """A list of non-empty, unique strings (a string is not a list here)."""
    if not isinstance(value, (list, tuple)):
        raise _reject(f"{what} must be a list")
    names: List[str] = []
    for index, entry in enumerate(value, start=1):
        names.append(_text(entry, f"{what} entry #{index}"))
    if len(set(names)) != len(names):
        raise _reject(f"{what} declares a duplicate entry")
    return tuple(names)


def _parse_artifacts(raw: object) -> Optional[str]:
    if raw is None:
        return None
    artifacts = _mapping(raw, "artifacts")
    _no_unknown_keys(artifacts, _ARTIFACT_KEYS, "artifacts")
    primary = artifacts.get("primary")
    if primary is None:
        return None
    return _safe_relative(primary, "artifacts.primary")


def _parse_inputs(raw: object) -> Tuple[Tuple[str, ...], Tuple[str, ...], Dict[str, Any]]:
    if raw is None:
        return (), (), {}
    inputs = _mapping(raw, "inputs")
    _no_unknown_keys(inputs, _INPUT_KEYS, "inputs")

    required = _strings(inputs["required"], "inputs.required") if "required" in inputs else ()
    optional = _strings(inputs["optional"], "inputs.optional") if "optional" in inputs else ()
    overlap = sorted(set(required) & set(optional))
    if overlap:
        raise _reject(
            f"inputs: {', '.join(overlap)} declared both required and optional"
        )

    defaults: Dict[str, Any] = {}
    if inputs.get("defaults") is not None:
        defaults = dict(_mapping(inputs["defaults"], "inputs.defaults"))
    return required, optional, defaults


def _parse_outputs(raw: object, step_id: str) -> Tuple[StepOutput, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise _reject(f"step {step_id!r}: outputs must be a list")
    if len(raw) > MAX_OUTPUTS_PER_STEP:
        raise _reject(
            f"step {step_id!r}: {len(raw)} outputs (> {MAX_OUTPUTS_PER_STEP})"
        )
    outputs: List[StepOutput] = []
    for index, entry in enumerate(raw, start=1):
        output = _mapping(entry, f"step {step_id!r} output #{index}")
        _no_unknown_keys(output, _OUTPUT_KEYS, f"step {step_id!r} output #{index}")
        kind = _text(output.get("kind"), f"step {step_id!r} output #{index} kind")
        default_path = _safe_relative(
            output.get("default_path"),
            f"step {step_id!r} output #{index} default_path",
        )
        outputs.append(StepOutput(kind=kind, default_path=default_path))
    return tuple(outputs)


def _parse_on_success(
    raw: object, step_id: str
) -> Tuple[Optional[str], Optional[StepBranch]]:
    if raw is None:
        return None, None
    if isinstance(raw, str):
        return _text(raw, f"step {step_id!r} on_success"), None

    branch = _mapping(raw, f"step {step_id!r} on_success")
    _no_unknown_keys(branch, _BRANCH_KEYS, f"step {step_id!r} on_success")
    parsed = StepBranch(
        branch_on=_text(
            branch.get("branch_on"),
            f"step {step_id!r} on_success.branch_on",
            pattern=_BRANCH_KEY_RE,
        ),
        when_true=_text(branch.get("when_true"), f"step {step_id!r} when_true"),
        when_false=_text(branch.get("when_false"), f"step {step_id!r} when_false"),
    )
    return None, parsed


def _parse_gate_template(raw: object) -> Any:
    if raw is None:
        return None
    if isinstance(raw, str):
        return _text(raw, "gate_template")
    if not isinstance(raw, (list, tuple)):
        raise _reject("gate_template must be a template name or an inline list")
    from capsule_engine import gates as gate_engine

    try:
        gate_engine.specs_from_template(raw)
    except Exception as exc:
        raise _reject(f"gate_template is invalid: {exc}") from exc
    return tuple(raw)


def _parse_step(entry: object, index: int) -> TaskTypeStep:
    raw = _mapping(entry, f"step #{index}")
    _no_unknown_keys(raw, _STEP_KEYS, f"step #{index}")

    step_id = _text(raw.get("id"), f"step #{index} id", pattern=_STEP_ID_RE)

    worker: Optional[str] = None
    if raw.get("worker") is not None:
        # A registry *name*, never a dotted module path: task types name
        # workers, they do not import them.
        worker = _text(raw["worker"], f"step {step_id!r} worker", pattern=_WORKER_RE)

    gate: Optional[str] = None
    if raw.get("gate") is not None:
        gate = _text(raw["gate"], f"step {step_id!r} gate")
        known = _known_gate_types()
        if gate != BOUNDARY_GATE and gate not in known:
            raise _reject(
                f"step {step_id!r}: unknown gate type {gate!r} "
                f"(known: {', '.join(sorted(known))} or {BOUNDARY_GATE!r})"
            )

    if worker is None and gate != BOUNDARY_GATE:
        raise _reject(
            f"step {step_id!r} needs a worker (only the approval boundary step, "
            f"gate {BOUNDARY_GATE!r}, may omit it)"
        )

    acceptance = _text(
        raw.get("acceptance"),
        f"step {step_id!r} acceptance",
        max_chars=MAX_ACCEPTANCE_CHARS,
    )

    state: Optional[str] = None
    if raw.get("state") is not None:
        state = _text(raw["state"], f"step {step_id!r} state")
        if state not in ROUTABLE_STATES:
            raise _reject(
                f"step {step_id!r}: state {state!r} is not routable "
                f"(known: {', '.join(sorted(ROUTABLE_STATES))})"
            )

    on_success, on_success_branch = _parse_on_success(raw.get("on_success"), step_id)
    on_reject: Optional[str] = None
    if raw.get("on_reject") is not None:
        on_reject = _text(raw["on_reject"], f"step {step_id!r} on_reject")

    return TaskTypeStep(
        step_id=step_id,
        worker=worker,
        gate=gate,
        acceptance=acceptance,
        outputs=_parse_outputs(raw.get("outputs"), step_id),
        state=state,
        on_success=on_success,
        on_success_branch=on_success_branch,
        on_reject=on_reject,
    )


def _parse_steps(raw: object) -> Tuple[TaskTypeStep, ...]:
    if not isinstance(raw, (list, tuple)):
        raise _reject("steps must be a list of step objects")
    if not raw:
        raise _reject("steps must declare at least one step")
    if len(raw) > MAX_STEPS:
        raise _reject(f"steps declares {len(raw)} steps (> {MAX_STEPS})")

    steps: List[TaskTypeStep] = []
    seen: set = set()
    for index, entry in enumerate(raw, start=1):
        step = _parse_step(entry, index)
        if step.step_id in seen:
            raise _reject(f"step id {step.step_id!r} is duplicated")
        seen.add(step.step_id)
        steps.append(step)

    # Structural ordering: every edge must land on a declared step or a
    # reserved target, so no plan can dangle.
    step_ids = {step.step_id for step in steps}
    allowed = step_ids | RESERVED_TARGETS
    for step in steps:
        targets: List[Tuple[str, str]] = []
        if step.on_success is not None:
            targets.append(("on_success", step.on_success))
        if step.on_success_branch is not None:
            targets.append(("on_success.when_true", step.on_success_branch.when_true))
            targets.append(("on_success.when_false", step.on_success_branch.when_false))
        if step.on_reject is not None:
            targets.append(("on_reject", step.on_reject))
        for label, target in targets:
            if target not in allowed:
                raise _reject(
                    f"step {step.step_id!r}: {label} target {target!r} does not "
                    f"name a declared step (known: {', '.join(sorted(step_ids))}) "
                    f"or a reserved target ({', '.join(sorted(RESERVED_TARGETS))})"
                )

    if not any(step.is_boundary for step in steps):
        raise _reject(
            f"no step reaches the approval boundary: declare one step with "
            f"gate {BOUNDARY_GATE!r}. A task type that can never face a human "
            "gate is inadmissible."
        )
    return tuple(steps)


def _parse(path: Path, data: object, *, expected_name: str) -> TaskType:
    """Validate one decoded task type document."""
    document = _mapping(data, "task type document")
    _no_unknown_keys(document, _TOP_LEVEL_KEYS, "task type document")

    name = _text(document.get("task_type"), "task_type", pattern=_ID_RE)
    if name != expected_name:
        raise _reject(
            f"task_type {name!r} does not match its filename {expected_name!r}"
        )

    version = document.get("version")
    if version != VERSION:
        raise _reject(
            f"unsupported task type version {version!r} (this build knows {VERSION})"
        )

    title_raw = document.get("title")
    title = (
        title_raw.strip()
        if isinstance(title_raw, str) and title_raw.strip()
        else name
    )
    if len(title) > MAX_TITLE_CHARS:
        raise _reject(f"title exceeds {MAX_TITLE_CHARS} characters")

    domain = _text(document.get("domain"), "domain", pattern=_ID_RE)
    required, optional, defaults = _parse_inputs(document.get("inputs"))

    return TaskType(
        task_type=name,
        version=version,
        title=title,
        domain=domain,
        primary_artifact=_parse_artifacts(document.get("artifacts")),
        required_inputs=required,
        optional_inputs=optional,
        input_defaults=defaults,
        gate_template=_parse_gate_template(document.get("gate_template")),
        steps=_parse_steps(document.get("steps")),
        path=str(path),
    )


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _read_document(path: Path) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _reject(f"task type file unreadable: {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _reject(
            f"task type file is not valid JSON: {path}: "
            f"line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc


def load_task_type(
    task_type: object, *, directory: "str | os.PathLike[str] | None" = None
) -> TaskType:
    """Load and validate one task type by id.

    Args:
        task_type: the id (``"social_post"``); validated before any file access.
        directory: catalogue override (defaults to :func:`task_types_dir`).

    Raises:
        TaskTypeError: the id is malformed, no such file exists, or the document
            breaches the schema — all refusals, never a partial load.
    """
    if not is_valid_id(task_type):
        raise _reject(f"invalid task type id: {task_type!r}")
    name = str(task_type).strip()

    base = Path(directory) if directory is not None else task_types_dir()
    path = base / f"{name}.json"
    if not path.is_file():
        raise _reject(f"unknown task type {name!r}: no such file: {path}")

    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = -1

    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]

    parsed = _parse(path, _read_document(path), expected_name=name)
    _CACHE[str(path)] = (stamp, parsed)
    return parsed


def list_task_types(
    *, directory: "str | os.PathLike[str] | None" = None
) -> Tuple[TaskType, ...]:
    """Every task type in the catalogue, sorted by id.

    A malformed document **raises** :class:`TaskTypeError`: a task-type
    catalogue that cannot load is a configuration error to fix loudly, never to
    half-render. An absent directory is simply an empty catalogue.
    """
    base = Path(directory) if directory is not None else task_types_dir()
    if not base.is_dir():
        return ()
    found: List[TaskType] = []
    for path in sorted(base.glob("*.json")):
        found.append(load_task_type(path.stem, directory=base))
    return tuple(found)


def unregistered_workers(
    known_workers: Sequence[str],
    *,
    types: "Sequence[TaskType] | None" = None,
    directory: "str | os.PathLike[str] | None" = None,
) -> List[Dict[str, str]]:
    """Every ``(task_type, step, worker)`` naming a worker that is not registered.

    The known-worker set is **passed in**, never imported: the worker catalogue
    lives above the core (``engine/workers``), and the core must not reach up
    into it (``tests/test_dependency_direction.py``). Callers hand in
    ``set(WORKERS) | set(domain_workers())``.
    """
    known = {
        worker.strip()
        for worker in (known_workers or ())
        if isinstance(worker, str) and worker.strip()
    }
    catalogue = tuple(types) if types is not None else list_task_types(directory=directory)
    gaps: List[Dict[str, str]] = []
    for task_type in catalogue:
        for step in task_type.steps:
            if step.worker and step.worker not in known:
                gaps.append(
                    {
                        "task_type": task_type.task_type,
                        "step": step.step_id,
                        "worker": step.worker,
                    }
                )
    return gaps
