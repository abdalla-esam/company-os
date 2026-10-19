#!/usr/bin/env python3
"""worker.py — the abstract contract every capsule worker implements.

A worker is a single, focused operation against one Deliverable Capsule
(author the copy, run QA, publish to the world). Subclasses implement exactly
one abstract method::

    def execute(self, capsule_path: str, manifest: dict) -> dict: ...

``run()`` is the production entry point and does the boring, dangerous parts
exactly once, under one lock:

1. validate the capsule directory exists;
2. acquire ``.lock`` (so overlapping cron pulses serialize, never interleave);
3. load + validate ``manifest.json``;
4. check the manifest's state is one this worker ``handles`` (defence against
   a stale route);
5. call :meth:`execute` and validate the manifest it returns;
6. persist atomically; on any worker exception, record the failure on disk
   (attempt counter + history, escalating to ``failed`` at the attempt cap)
   and re-raise as :class:`WorkerExecutionError` — *the disk stays the
   witness*; a crash is never invisible.

While ``run()`` holds the lock, the worker knows which capsule it is acting
on, so :meth:`transition` can enforce the constitutional rails automatically:
entering ``pending_approval`` binds the artifact SHA-256 (any recorded file), leaving it toward
``approved``/``published`` verifies it (fail closed), the domain's autonomy
decision is recorded when the gate is entered, and a gate rejection
(``pending_approval -> needs_revision``) demotes the domain to L0.

Token accounting is a one-liner for workers: after a gateway call, hand the
gateway to :meth:`account_usage` and the provider's ``usage`` counters are
folded into ``manifest["metadata"]["usage"]`` (missing keys default to 0).

Helpers for safely reading/writing inside the capsule are provided:
every path is resolved against the capsule root and rejected if it escapes
(``../../etc/passwd`` raises :class:`UnsafePathError`), and every write goes
through temp-file + fsync + ``os.replace`` so readers never observe torn
files.

Standard library only: ``abc``, ``json``, ``os``, ``pathlib``, ``typing``.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Mapping, MutableMapping, Sequence, TYPE_CHECKING

from capsule_engine.exceptions import (
    CapsuleNotFoundError,
    ManifestCorruptError,
    ManifestSchemaError,
    StateMismatchError,
    UnsafePathError,
    WorkerExecutionError,
)
from capsule_engine.governance import (
    demote as demote_domain,
    domain_of,
    record_autonomy_decision,
)
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import (
    ARTIFACT_FILE,
    TRANSITIONS,
    USAGE_KEYS,
    State,
    accumulate_usage,
    append_history,
    last_history_entry,
    load_manifest,
    save_manifest,
    transition as lawfully_transition,
    validate_manifest,
)
from capsule_engine import trace as trace_engine

if TYPE_CHECKING:  # pragma: no cover - typing only
    from capsule_engine.tool_loop import LoopResult

logger = logging.getLogger("capsule_engine.worker")

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024  # 8 MiB per artifact write guard
MAX_TRACE_ARTIFACTS = 20  # output artifacts hashed into a step_end event
FEEDBACK_FILE = "feedback.txt"  # human/automated rejection note lived on disk


class BaseWorker(ABC):
    """Abstract base for every capsule worker.

    Subclass attributes:

        name: unique worker label recorded in history and lock metadata.
        handles: the states this worker may execute; ``run()`` refuses any
            other state. Empty means "any state" (not recommended).
        max_attempts: consecutive failures tolerated before the manifest is
            escalated to ``State.FAILED`` (human reset).
        lock_timeout: seconds ``run()`` waits for the capsule lock.
    """

    name: ClassVar[str] = "base"
    handles: ClassVar[frozenset[State]] = frozenset()
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    # Capsule this instance is currently ``run()``-ing; set under the lock so
    # transition-time guards (artifact binding/verification, governance) know
    # where the artifact lives. ``None`` outside a run — direct manifest-level
    # calls stay legacy-compatible.
    _active_capsule: ClassVar[str | None] = None
    # The capsule_id of the active run (telemetry labels every trace event).
    _active_capsule_id: ClassVar[str | None] = None

    # Optional ``governance.json`` override for the transition-time governance
    # hooks. The pulse binds it per worker before ``run()`` when a pass is
    # configured with ``governance_file=``; ``None`` means the module/env
    # default (still fail-closed to L0).
    governance_path: ClassVar[str | os.PathLike[str] | None] = None

    # Universal observability: every worker run emits an execution-trace step
    # (``execution_trace.jsonl`` + live ``telemetry.json``). Set False only for
    # workers that must not touch the capsule beyond their business write.
    trace_enabled: ClassVar[bool] = True

    # ------------------------------------------------------------------ #
    # the one method subclasses must write
    # ------------------------------------------------------------------ #

    @abstractmethod
    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Perform this worker's work and return the updated manifest.

        Contract:
        - mutate ``manifest`` in place (use :meth:`transition` /
          :meth:`record` for auditable changes) **and** return it;
        - never write ``manifest.json`` yourself — ``run()`` persists once,
          atomically, after you return;
        - raise a plain ``Exception`` on failure; ``run()`` records it and
          re-raises as :class:`WorkerExecutionError`.

        Args:
            capsule_path: absolute path of the capsule directory (lock held).
            manifest: the validated manifest dict read under the lock.
        """
        ...

    # ------------------------------------------------------------------ #
    # production entry point
    # ------------------------------------------------------------------ #

    def run(
        self, capsule_path: str, *, lock_timeout: float | None = None
    ) -> dict[str, Any]:
        """Execute the worker under the capsule lock; returns the saved manifest.

        Raises:
            CapsuleNotFoundError: the capsule directory is missing.
            LockTimeoutError: another pulse holds the capsule.
            StateMismatchError: manifest state is not in ``handles``.
            WorkerExecutionError: ``execute`` (or its validation) raised; the
                failure is already recorded on disk when this propagates.
        """
        capsule = Path(capsule_path)
        if not capsule.is_dir():
            raise CapsuleNotFoundError(f"capsule directory does not exist: {capsule}")

        timeout = self.lock_timeout if lock_timeout is None else lock_timeout
        prior = self._active_capsule
        prior_id = self._active_capsule_id
        self._active_capsule = str(capsule)
        self._active_capsule_id = None
        try:
            return self._run_locked(capsule, timeout)
        finally:
            self._active_capsule = prior
            self._active_capsule_id = prior_id

    def _run_locked(self, capsule: Path, timeout: float) -> dict[str, Any]:
        """The locked body of :meth:`run` (see there for the full contract)."""
        with CapsuleLock(capsule, timeout=timeout, purpose=self.name):
            manifest = load_manifest(capsule)
            current = State.parse(manifest["state"])
            if self.handles and current not in self.handles:
                raise StateMismatchError(
                    f"worker {self.name!r} handles "
                    f"{sorted(s.value for s in self.handles)}, "
                    f"but capsule {manifest['capsule_id']!r} is in "
                    f"{current.value!r}"
                )
            history_len = len(manifest.get("history") or [])
            # Spend attribution (spend_control): the capsule's own domain pays
            # for the gateway calls this run makes. Duck-typed hook so the
            # base worker never depends on a concrete gateway.
            gateway = getattr(self, "_gateway", None)
            bind_spend = getattr(gateway, "spend_domain", None)
            if callable(bind_spend):
                bind_spend(manifest.get("domain"))
            tracer = self._make_tracer(capsule, manifest)
            step = tracer.start(self.name, gear=self.name)
            usage_before = _usage_snapshot(manifest)
            try:
                result = self.execute(str(capsule), manifest)
                if not isinstance(result, dict):
                    raise ManifestSchemaError(
                        f"{capsule}/manifest.json",
                        f"execute() must return the manifest dict, got "
                        f"{type(result).__name__}",
                    )
                validated = validate_manifest(
                    result, source=f"{capsule}/manifest.json"
                )
                self._backfill_history(
                    validated, current, prior_history_len=history_len
                )
            except Exception as exc:  # fail closed: record, then re-raise
                try:
                    # Book against the *live* state on the manifest: a worker
                    # that marked its attempt (e.g. approved -> in_progress)
                    # before failing must be able to escalate from there.
                    failing_state = State.parse(manifest["state"])
                    self._record_failure(capsule, manifest, failing_state, exc)
                except Exception as bookkeeping_exc:
                    self._close_step(
                        step, tracer, capsule, manifest,
                        usage_before, trace_engine.STATUS_FAILED, exc,
                    )
                    raise WorkerExecutionError(
                        f"worker {self.name!r} failed on capsule "
                        f"{manifest.get('capsule_id', capsule.name)!r}: "
                        f"{type(exc).__name__}: {exc}; failure bookkeeping also "
                        f"failed: {type(bookkeeping_exc).__name__}: {bookkeeping_exc}"
                    ) from exc
                self._close_step(
                    step, tracer, capsule, manifest,
                    usage_before, trace_engine.STATUS_FAILED, exc,
                )
                raise WorkerExecutionError(
                    f"worker {self.name!r} failed on capsule "
                    f"{manifest.get('capsule_id', capsule.name)!r}: "
                    f"{type(exc).__name__}: {exc} (failure recorded on disk)"
                ) from exc
            save_manifest(capsule, validated)
            # ``needs_revision`` is a rejection, not a crash: the trace says
            # so, and the status never lies about what the run produced.
            status = (
                trace_engine.STATUS_NEEDS_REVISION
                if State.parse(validated["state"]) is State.NEEDS_REVISION
                else trace_engine.STATUS_SUCCESS
            )
            self._close_step(
                step, tracer, capsule, validated, usage_before, status, None
            )
            return validated

    # ------------------------------------------------------------------ #
    # safe capsule I/O helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve(capsule_path: str, relative: str | os.PathLike[str]) -> Path:
        """Resolve ``relative`` inside the capsule, refusing to escape it.

        Raises:
            UnsafePathError: the target resolves outside the capsule root
                (absolute paths, ``..`` traversal, symlink escapes).
        """
        root = Path(capsule_path).resolve()
        candidate = Path(relative)
        if candidate.is_absolute():
            raise UnsafePathError(
                f"absolute paths are forbidden inside a capsule: {relative}"
            )
        target = (root / candidate).resolve()
        if target != root and root not in target.parents:
            raise UnsafePathError(
                f"path escapes capsule {root}: {relative} -> {target}"
            )
        return target

    def read_text(
        self, capsule_path: str, relative: str, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> str:
        """Read a UTF-8 artifact from the capsule.

        Raises:
            CapsuleNotFoundError: the artifact does not exist.
            OSError: I/O failure, or the file exceeds ``max_bytes``.
        """
        target = self.resolve(capsule_path, relative)
        if not target.is_file():
            raise CapsuleNotFoundError(f"artifact missing: {target}")
        size = target.stat().st_size
        if size > max_bytes:
            raise OSError(f"artifact {target} is {size} bytes (> {max_bytes})")
        return target.read_text(encoding="utf-8")

    def read_bytes(
        self, capsule_path: str, relative: str, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> bytes:
        """Read a binary artifact (image, archive) from the capsule.

        Mirror of :meth:`read_text`: traversal-safe resolution and a size
        guard before the file is pulled into memory.

        Raises:
            CapsuleNotFoundError: the artifact does not exist.
            OSError: I/O failure, or the file exceeds ``max_bytes``.
        """
        target = self.resolve(capsule_path, relative)
        if not target.is_file():
            raise CapsuleNotFoundError(f"artifact missing: {target}")
        size = target.stat().st_size
        if size > max_bytes:
            raise OSError(f"artifact {target} is {size} bytes (> {max_bytes})")
        return target.read_bytes()

    def write_text(
        self, capsule_path: str, relative: str, content: str
    ) -> Path:
        """Atomically write a UTF-8 artifact inside the capsule."""
        target = self.resolve(capsule_path, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def read_json(self, capsule_path: str, relative: str) -> Any:
        """Read a JSON artifact; raise :class:`ManifestCorruptError` if invalid."""
        target = self.resolve(capsule_path, relative)
        if not target.is_file():
            raise CapsuleNotFoundError(f"artifact missing: {target}")
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestCorruptError(
                str(target),
                f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
            ) from exc

    def write_json(
        self, capsule_path: str, relative: str, payload: Any, *, indent: int = 2
    ) -> Path:
        """Atomically write a JSON artifact inside the capsule."""
        return self.write_text(
            capsule_path,
            relative,
            json.dumps(payload, ensure_ascii=False, indent=indent) + "\n",
        )

    def write_bytes(self, capsule_path: str, relative: str, content: bytes) -> Path:
        """Atomically write a binary artifact (image, archive) inside the capsule.

        Same guarantees as :meth:`write_text`: traversal-safe resolution,
        temp file in the target directory, fsync, then ``os.replace`` — a
        reader (or the Watcher) never observes a half-written binary.
        """
        target = self.resolve(capsule_path, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def read_manifest(self, capsule_path: str) -> dict[str, Any]:
        """Load and validate the capsule's manifest (read under your own lock)."""
        return load_manifest(capsule_path)

    # ------------------------------------------------------------------ #
    # execution trace (universal step telemetry)
    # ------------------------------------------------------------------ #

    def trace_step(
        self, step_name: str, *, gear: str | None = None
    ) -> "trace_engine.StepHandle":
        """Open a traced sub-step inside ``execute`` (live progress + audit).

        Usage::

            with self.trace_step("author copy", gear="text:write") as step:
                step.progress(40)
                text = self._gateway.complete(manifest, prompt)
                step.add_tokens(getattr(self._gateway, "last_usage", None))
                step.hash_outputs(["copy.md"])

        Outside a ``run()`` the handle is inert (an unenabled tracer): calling
        this from a bare unit test never writes to disk.
        """
        tracer = trace_engine.ExecutionTracer(
            Path(self._active_capsule) if self._active_capsule else Path("."),
            self._active_capsule_id or "unknown",
            enabled=bool(self.trace_enabled and self._active_capsule is not None),
        )
        return tracer.start(step_name, gear=gear)

    def _make_tracer(
        self, capsule: Path, manifest: dict[str, Any]
    ) -> "trace_engine.ExecutionTracer":
        """Tracer for this capsule; disabled outside a run or by policy."""
        active = self._active_capsule is not None
        capsule_id = str(manifest.get("capsule_id") or capsule.name)
        if active and self._active_capsule_id is None:
            self._active_capsule_id = capsule_id
        return trace_engine.ExecutionTracer(
            capsule,
            capsule_id,
            enabled=bool(self.trace_enabled and active),
        )

    def _close_step(
        self,
        step: "trace_engine.StepHandle | None",
        tracer: "trace_engine.ExecutionTracer",
        capsule: Path,
        manifest: dict[str, Any],
        usage_before: dict[str, int],
        status: str,
        error: Exception | None,
    ) -> None:
        """Finish the run's trace step: tokens delta, output hashes, status.

        Best effort by contract: a tracing failure is logged, never raised —
        observability must not be able to fail the work it observes.
        """
        if step is None:
            return
        try:
            delta = _usage_delta(usage_before, manifest)
            if delta:
                step.add_tokens(delta)
            step.hash_outputs(self._artifact_candidates(capsule, manifest))
            step.finish(status, error=str(error) if error is not None else None)
        except Exception as trace_exc:  # pragma: no cover - defensive
            logger.warning(
                "trace: closing step for %s failed: %s", capsule, trace_exc
            )
            _ = tracer  # tracer already recorded its own error counter

    def _artifact_candidates(
        self, capsule: Path, manifest: dict[str, Any]
    ) -> list[str]:
        """Bounded list of output artifacts to hash into the step record."""
        candidates: list[str] = []
        metadata = manifest.get("metadata")
        recorded = metadata.get("artifact_path") if isinstance(metadata, dict) else None
        if isinstance(recorded, str) and recorded.strip():
            candidates.append(recorded.strip())
        deliverables = capsule / "deliverables"
        if deliverables.is_dir():
            try:
                for path in sorted(deliverables.rglob("*")):
                    if path.is_file():
                        candidates.append(path.relative_to(capsule).as_posix())
                    if len(candidates) >= MAX_TRACE_ARTIFACTS:
                        break
            except OSError:
                pass
        if not candidates and (capsule / ARTIFACT_FILE).is_file():
            candidates.append(ARTIFACT_FILE)
        return candidates

    # ------------------------------------------------------------------ #
    # auditable manifest mutation helpers
    # ------------------------------------------------------------------ #

    def transition(
        self,
        manifest: MutableMapping[str, Any],
        new_state: State | str,
        *,
        why: str,
        capsule_path: Optional[str | Path] = None,
    ) -> State:
        """Lawfully move the manifest to ``new_state``, stamped with our name.

        Enforces the constitutional rails while a capsule is being run:
        entering ``pending_approval`` binds/verifies the artifact digest, and
        governance is notified of the gate entry or rejection.
        """
        previous = State.parse(manifest["state"])
        target = new_state if isinstance(new_state, State) else State.parse(new_state)
        result = lawfully_transition(
            manifest,
            new_state,
            by=self.name,
            why=why,
            capsule_path=capsule_path or self._active_capsule,
        )
        # The hooks answer to the *requested* move, not to an escalation the
        # revision cap may have appended on top of it: a rejection that was
        # bumped straight to ``failed`` was still a rejection (memory +
        # demotion must run); nothing else can differ (escalation only ever
        # follows an entry into needs_revision).
        self._governance_hooks(manifest, previous, target, why=why)
        return result

    def record(self, manifest: MutableMapping[str, Any], why: str) -> None:
        """Append a history entry without changing state (observations, notes)."""
        append_history(manifest, by=self.name, why=why)

    def account_usage(self, manifest: MutableMapping[str, Any], *sources: Any) -> dict[str, int]:
        """Fold gateway usage into ``manifest["metadata"]["usage"]``.

        Each source may be a usage mapping or a gateway exposing
        ``last_usage`` (both gateway implementations do). Missing sources,
        absent/``None`` usage records, and malformed counters are ignored —
        accounting never fails a worker, and unknown extra keys in an existing
        usage record are preserved.
        """
        merged: dict[str, int] = {}
        for source in sources:
            if source is None:
                continue
            usage = source if isinstance(source, dict) else getattr(source, "last_usage", None)
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    continue
                merged[key] = merged.get(key, 0) + value
        if not merged:
            return {}
        return accumulate_usage(manifest, merged)

    # ------------------------------------------------------------------ #
    # the tool loop (model ↔ gears)
    # ------------------------------------------------------------------ #

    def run_tools(
        self,
        capsule_path: str,
        manifest: MutableMapping[str, Any],
        messages: list[dict[str, Any]],
        *,
        gears: Sequence[str] | None = None,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
        include_irreversible: bool = False,
        offline: bool | None = None,
        temperature: float | None = None,
        step_name: str = "tool loop",
    ) -> "LoopResult":
        """Run one bounded model↔gear loop for this capsule.

        The worker supplies the transcript (system + user messages); the loop
        decides which gears to call, executes them through the governed bridge,
        and returns the final answer plus the whole transcript. Everything the
        loop touches is already accounted for: the bridge meters each gear
        attempt, the gateway meters each model turn, and this method folds the
        loop's totals into ``metadata.usage`` and the trace step.

        Args:
            capsule_path: capsule root (used for the trace + capsule context).
            manifest: the live manifest (mutated: usage, gear exchanges).
            messages: the initial transcript.
            gears: restrict the offered tools (``None`` = every safe gear).
            max_steps / max_tool_calls: hard bounds; the loop stops and says so.
            include_irreversible: also offer publish/send (off by default).
            offline: force the offline bridge (``None`` = follow the env).
            temperature: model temperature override.

        Raises:
            SpendCapExceeded: the budget refused — never swallowed.
            GatewayError: the model could not answer at all.

        A loop that hits a bound is **not** an exception: read
        ``result.stopped_reason`` and ``result.ok``.
        """
        from capsule_engine.governance import domain_of, level_for
        from capsule_engine.gateway import default_gateway
        from capsule_engine.tool_bridge import GearToolRunner, gear_tools
        from capsule_engine.tool_loop import (
            DEFAULT_MAX_STEPS,
            DEFAULT_MAX_TOOL_CALLS,
            ToolLoop,
        )

        gateway = getattr(self, "_gateway", None) or default_gateway()
        domain = str(manifest.get("domain") or domain_of(manifest))
        capsule_context = {
            "id": str(manifest.get("capsule_id") or Path(capsule_path).name),
            "state": str(manifest.get("state")),
            "attempt": int(manifest.get("attempt") or 0),
            "domain": domain,
        }
        governance_context = {"domain": domain, "autonomy_level": level_for(domain)}
        runner = GearToolRunner(
            capsule=capsule_context,
            governance=governance_context,
            offline=offline,
        )
        tools = gear_tools(
            include_irreversible=include_irreversible,
            only=gears,
        )
        allowed = [tool["function"]["name"] for tool in tools]
        loop = ToolLoop(
            gateway,
            runner,
            max_steps=DEFAULT_MAX_STEPS if max_steps is None else max_steps,
            max_tool_calls=(
                DEFAULT_MAX_TOOL_CALLS if max_tool_calls is None else max_tool_calls
            ),
            allowed_gears=allowed,
        )
        with self.trace_step(step_name, gear="tool_loop") as step:
            result = loop.run(
                manifest,
                messages,
                tools,
                temperature=temperature,
            )
            step.add_tokens(result.usage)
            self.account_usage(manifest, result.usage)
        self.record(manifest, result.summary())
        return result

    def _governance_hooks(
        self,
        manifest: MutableMapping[str, Any],
        previous: State,
        target: State,
        *,
        why: str = "",
    ) -> None:
        """React to gate entry and gate rejection (autonomy, plan, memory).

        Fail-closed philosophy: a decision recorded at gate entry makes the
        later routing deterministic (no re-roll of the 1-in-50 audit); a
        rejection is remembered in the cumulative feedback ledger and demotes
        the domain to L0. Every hook here is best-effort — the transition
        itself already happened and must never be rolled back by a bookkeeping
        failure (failures are logged, never raised).
        """
        if target is State.PENDING_APPROVAL and previous is not State.PENDING_APPROVAL:
            if previous is not State.STALLED:  # unfreezing keeps the frozen verdict
                record_autonomy_decision(
                    manifest, path=self.governance_path, by=self.name
                )
            self._ensure_gate_plan(manifest)
            return
        if previous is State.NEEDS_REVISION and target is not State.NEEDS_REVISION:
            # Rework began: reset the gate plan back to pending with empty artifacts
            # so the new revision is audited independently and without TOCTOU hash collisions.
            try:
                from capsule_engine import gates as gate_engine
                gate_engine.reset_plan(
                    manifest,
                    by=self.name,
                    reason=f"rework started: {previous.value} -> {target.value}",
                )
            except Exception as exc:
                logger.warning(
                    "governance: could not reset gate plan on rework transition: %s",
                    exc,
                )
        if target is State.NEEDS_REVISION and previous is not State.NEEDS_REVISION:
            # A rejection/revision instruction is reviewed by the next author
            # *and* by the learning memory: both need it.
            self._remember_rejection(manifest, why)
        if target is State.NEEDS_REVISION and previous is State.PENDING_APPROVAL:
            domain = domain_of(manifest)
            try:
                result = demote_domain(
                    domain,
                    path=self.governance_path,
                    reason=f"gate rejection by {self.name}: pending_approval -> needs_revision",
                )
            except OSError as exc:  # never mask the rejection itself
                logger.warning(
                    "governance: could not demote domain %r after gate rejection: %s",
                    domain,
                    exc,
                )
                return
            if result.changed:
                logger.info(
                    "governance: domain %r demoted %s -> %s after gate rejection",
                    domain,
                    result.previous_level,
                    result.level,
                )

    def _ensure_gate_plan(self, manifest: MutableMapping[str, Any]) -> None:
        """Seed the capsule's gate plan from the domain template on entry.

        Idempotent (an existing plan is never rewound) and best-effort: a
        malformed template is recorded in history and the capsule enters the
        approval phase without gates — the human still holds the pen; the
        API's gate endpoints will act only on an existing plan.
        """
        try:
            from capsule_engine import gates as gate_engine
            from capsule_engine.policy import load_policy

            policy = load_policy()
            if not policy.gates.enabled:
                return
            # Domain resolution mirrors the router: the capsule's own declared
            # branch (top-level ``domain``) wins; then the governance lookup.
            declared = manifest.get("domain")
            domain = (
                declared.strip()
                if isinstance(declared, str) and declared.strip()
                else domain_of(manifest)
            )
            specs = gate_engine.specs_from_template(
                policy.gates.template_for(domain),
                max_gates=policy.gates.max_gates,
            )
            records = gate_engine.load_records(manifest)
            if any(r.status == gate_engine.STATUS_FAILED for r in records):
                gate_engine.reset_plan(
                    manifest,
                    specs,
                    by=self.name,
                    reason="clearing previous gate failures for re-evaluation",
                )
            result = gate_engine.ensure_plan(
                manifest, specs, by=self.name, replace_empty=True
            )
            if result.created:
                logger.info(
                    "gates: plan initialized for %s: %s",
                    manifest.get("capsule_id"),
                    ", ".join(result.gate_ids),
                )
        except Exception as exc:
            logger.warning(
                "gates: plan not initialized for %s: %s",
                manifest.get("capsule_id"),
                exc,
            )
            try:
                append_history(
                    manifest,
                    by=self.name,
                    why=(
                        "gate plan not initialized: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            except Exception:  # bookkeeping only — never mask the transition
                pass

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _remember_rejection(self, manifest: MutableMapping[str, Any], why: str) -> None:
        """Append a rejection/revision instruction to the cumulative memory.

        The reason is the on-disk ``feedback.txt`` when present (that is the
        reviewer's actual instruction), else the transition note. When a gate
        record failed, its identity and targets are attached so future
        constraint queries can match the same elements. Best effort: a ledger
        failure is logged, never raised.
        """
        try:
            from capsule_engine import gates as gate_engine
            from capsule_engine import memory as memory_engine

            reason = why
            capsule = self._active_capsule
            if capsule:
                feedback = Path(capsule) / FEEDBACK_FILE
                try:
                    if feedback.is_file():
                        text = feedback.read_text(encoding="utf-8").strip()
                        if text:
                            reason = text
                except OSError:
                    pass
            gate_id: str | None = None
            gate_type: str | None = None
            targets: tuple[str, ...] = ()
            try:
                for record in gate_engine.load_records(manifest):
                    if record.status == gate_engine.STATUS_FAILED:
                        gate_id = record.gate_id
                        gate_type = record.gate_type
                        targets = record.targets
                        break
            except Exception:
                pass  # a broken plan never blocks the memory write
            memory_engine.record_rejection(
                manifest,
                reason=reason,
                decision=memory_engine.DECISION_REVISION_REQUESTED,
                reviewer=self.name,
                gate_id=gate_id,
                gate_type=gate_type,
                targets=targets,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "memory: rejection not recorded for %s: %s",
                manifest.get("capsule_id"),
                exc,
            )

    def _backfill_history(
        self,
        manifest: dict[str, Any],
        prior_state: State,
        *,
        prior_history_len: int,
    ) -> None:
        """Guarantee a state change is auditable even if execute() forgot.

        A worker that transitions without appending history gets a terse
        auto-entry; a worker that already logged the move, or that only
        mutated fields without changing state, is left untouched.
        """
        new_state = State.parse(manifest["state"])
        if new_state == prior_state:
            return
        history = manifest.get("history")
        grew = isinstance(history, list) and len(history) > prior_history_len
        tail = last_history_entry(manifest)
        if grew and tail is not None and tail.get("to_state") == new_state.value:
            return  # the worker logged the transition itself — do not duplicate
        append_history(
            manifest,
            by=self.name,
            why=f"state changed from {prior_state.value} (auto-entry)",
            from_state=prior_state,
            to_state=new_state,
        )

    def _record_failure(
        self,
        capsule: Path,
        manifest: MutableMapping[str, Any],
        current: State,
        exc: Exception,
    ) -> None:
        """Persist the failure: attempt counter, history, escalation to FAILED.

        Best effort by design: the original worker exception always propagates,
        even if the disk itself is refusing writes (that secondary failure is
        chained into the message so nothing is swallowed).

        Escalation writes ``state`` directly instead of calling
        :func:`capsule_engine.manifest.transition` on purpose: bookkeeping of a
        crash must never be blocked by the very transition matrix it records.
        """
        manifest["attempt"] = int(manifest.get("attempt", 0)) + 1
        why = f"{type(exc).__name__}: {exc}"
        attempts = int(manifest["attempt"])
        escalate = attempts >= self.max_attempts and State.FAILED in TRANSITIONS.get(
            current, frozenset()
        )
        append_history(
            manifest,
            by=self.name,
            why=f"failure #{attempts}{' — attempt cap reached' if escalate else ''}: {why}",
            from_state=current,
            to_state=State.FAILED if escalate else current,
        )
        if escalate:
            manifest["state"] = State.FAILED.value
        save_manifest(capsule, manifest)  # raw error → caught and chained by run()


# --------------------------------------------------------------------------- #
# module-level helpers: token accounting for trace steps
# --------------------------------------------------------------------------- #

def _usage_snapshot(manifest: Mapping[str, Any]) -> dict[str, int]:
    """Read ``metadata.usage`` counters defensively (missing ⇒ 0)."""
    metadata = manifest.get("metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    snapshot: dict[str, int] = {}
    if not isinstance(usage, dict):
        return snapshot
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            snapshot[key] = value
    return snapshot


def _usage_delta(
    before: Mapping[str, int], manifest: Mapping[str, Any]
) -> dict[str, int]:
    """Tokens consumed during one run: after − before, clamped at ≥ 0."""
    after = _usage_snapshot(manifest)
    delta: dict[str, int] = {}
    for key in USAGE_KEYS:
        consumed = int(after.get(key, 0)) - int(before.get(key, 0))
        if consumed > 0:
            delta[key] = consumed
    return delta
