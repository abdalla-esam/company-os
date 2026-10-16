"""Session B — the revision cap: the loop breaker for ``needs_revision``.

Money model (TODO.md, session B): every rejection loop round pays for LLM
calls plus an image, so a capsule that oscillates
``needs_revision -> worker -> qa/gate rejection`` forever drains the budget
silently. The cap closes that hole:

- ``manifest['revision_count']`` counts entries into ``needs_revision`` —
  incremented in ONE chokepoint (``capsule_engine.manifest.transition``),
  which every rejection source moves through: QA, the human gate
  (``orchestration.approval.reject``), design-gate re-entry and the gate
  runner all call it (proved statically: no capsule writer may assign
  ``needs_revision`` to ``manifest["state"]`` outside ``manifest.py``);
- ``spend.max_revisions_per_capsule`` (``config/engine.json``, default 3)
  bounds the counter. The counter may *reach* the cap; the rejection that
  would push it *over* escalates the capsule in the same locked write to
  ``failed`` — the constitutional human-reset position (the router holds it,
  the Watcher reports it, ``resurrect()`` is the way out). The router also
  refuses to dispatch any ``needs_revision`` manifest that is already over
  the cap (defence in depth, e.g. after a cap was lowered);
- old capsules carry no field and count as 0; a present-but-mistyped counter
  fails closed at load (schema_version rules unchanged — the field is
  optional and stamped on write like everything else).

No real network or model calls anywhere: nothing here reaches a gateway.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault env

from tests._bootstrap import REPO_ROOT

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule_engine import manifest as manifest_engine
from capsule_engine import spend_control
from capsule_engine.exceptions import ManifestSchemaError
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import (
    State,
    load_manifest,
    new_manifest,
    resurrect,
    revision_cap,
    revision_count,
    save_manifest,
    transition,
)
from capsule_engine.router import Action, CapsuleRouter
from capsule_engine.worker import BaseWorker
from orchestration import approval, watcher
from orchestration.access_control import Principal

CAP = 3  # shipped default: spend.max_revisions_per_capsule

_ENV_KEYS = (
    "AGENCY_ENGINE_CONFIG",
    "AGENCY_SPEND_LEDGER",
    "AGENCY_SPEND_KILL",
)


def _spend_config(**overrides) -> dict:
    spend = {
        "enabled": True,
        "kill": False,
        "daily_usd": 10.0,
        "monthly_usd": 100.0,
        "per_domain_daily_usd": 10.0,
        "max_calls_per_day": 1000,
        "max_revisions_per_capsule": CAP,
        "default_domain": "core",
        "known_domains": ["core", "marketing", "design"],
        "unpriced_estimate_usd": 0.02,
        "estimate_tokens": {"prompt": 1000, "completion": 500},
        "model_prices": {},
        "image_costs": {},
        "subscriptions": [],
    }
    spend.update(overrides)
    return {"version": 1, "spend": spend}


class RevisionCapTestCase(unittest.TestCase):
    """Scratch config + scratch spool per test; env restored afterwards."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="so_revision_cap_")
        self.tmp = Path(self._tmp.name).resolve()
        self.spool = self.tmp / "spool"
        self.spool.mkdir(parents=True, exist_ok=True)
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["AGENCY_SPEND_LEDGER"] = str(self.tmp / "finance" / "usage_ledger.jsonl")
        self.write_config()
        spend_control.reset_caches()

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        spend_control.reset_caches()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------- #

    def write_config(self, **overrides) -> Path:
        path = self.tmp / "engine.json"
        path.write_text(json.dumps(_spend_config(**overrides)), encoding="utf-8")
        os.environ["AGENCY_ENGINE_CONFIG"] = str(path)
        spend_control.reset_caches()
        return path

    def make_capsule(
        self,
        capsule_id: str = "cap_rc",
        state: State = State.IN_PROGRESS,
        **fields,
    ) -> tuple[Path, dict]:
        """A saved capsule in ``state`` (``revision_count`` overridable)."""
        capsule = self.spool / capsule_id
        capsule.mkdir(parents=True, exist_ok=True)
        manifest = new_manifest(capsule_id, state=state)
        manifest["domain"] = "marketing"
        manifest.update(fields)
        save_manifest(capsule, manifest)
        return capsule, manifest

    @staticmethod
    def route_reason(capsule: Path) -> tuple[Action, str]:
        decision = CapsuleRouter().route(capsule)
        return decision.action, decision.reason


class _StubWorker(BaseWorker):
    """Minimal worker: the ``self.transition(...)`` shape QA/design/gate use."""

    name = "stub-rejector"
    handles = frozenset({State.IN_PROGRESS, State.NEEDS_REVISION})

    def execute(self, capsule_path: str, manifest: dict) -> dict:
        return manifest


class TestCounter(RevisionCapTestCase):
    """The counter itself: born 0, stamped on write, incremented once per entry."""

    def test_new_capsule_starts_at_zero_and_write_stamps_it(self) -> None:
        capsule, manifest = self.make_capsule()
        self.assertEqual(manifest["revision_count"], 0)
        on_disk = json.loads((capsule / "manifest.json").read_text("utf-8"))
        self.assertEqual(on_disk["revision_count"], 0)
        self.assertEqual(on_disk["schema_version"], 1)  # stamped on write, unchanged rules

    def test_old_capsule_without_counter_loads_as_zero(self) -> None:
        """A pre-cap manifest (no field at all) loads and counts as 0."""
        legacy = new_manifest("cap_legacy", state=State.IN_PROGRESS)
        del legacy["revision_count"]
        capsule = self.spool / "cap_legacy"
        capsule.mkdir(parents=True, exist_ok=True)
        (capsule / "manifest.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )
        loaded = load_manifest(capsule)
        self.assertNotIn("revision_count", loaded)
        self.assertEqual(revision_count(loaded), 0)
        # first rejection after upgrade: entry 1, still dispatchable
        result = transition(
            loaded, State.NEEDS_REVISION, by="qa", why="rejected after upgrade"
        )
        self.assertIs(result, State.NEEDS_REVISION)
        self.assertEqual(loaded["revision_count"], 1)
        save_manifest(capsule, loaded)
        self.assertEqual(load_manifest(capsule)["revision_count"], 1)

    def test_mistyped_counter_fails_closed_on_load(self) -> None:
        base = new_manifest("cap_bad_counter", state=State.IN_PROGRESS)
        for bad in ("many", -1, True, 1.5):
            broken = dict(base)
            broken["revision_count"] = bad
            with self.assertRaises(ManifestSchemaError):
                manifest_engine.validate_manifest(broken, source="manifest.json")

    def test_every_needs_revision_entry_increments(self) -> None:
        capsule, manifest = self.make_capsule()
        for round_no in range(1, 4):
            result = transition(
                manifest, State.NEEDS_REVISION, by="qa", why=f"rejection #{round_no}"
            )
            self.assertIs(result, State.NEEDS_REVISION)
            self.assertEqual(manifest["revision_count"], round_no)
            transition(manifest, State.IN_PROGRESS, by="marketing", why="rework start")
        self.assertEqual(manifest["revision_count"], 3)
        self.assertEqual(revision_count(manifest), 3)

    def test_worker_transition_counts_like_the_real_rejectors(self) -> None:
        """QA, design and the gate runner all reject via BaseWorker.transition."""
        capsule, manifest = self.make_capsule()
        worker = _StubWorker()
        worker.transition(manifest, State.NEEDS_REVISION, why="qa rejected: feedback")
        self.assertEqual(manifest["revision_count"], 1)
        # escalate path through the same call shape: seed over cap, re-reject
        manifest["revision_count"] = CAP
        transition(manifest, State.IN_PROGRESS, by="marketing", why="rework start")
        result = worker.transition(
            manifest, State.NEEDS_REVISION, why="qa rejected again"
        )
        self.assertIs(result, State.FAILED)
        self.assertEqual(manifest["revision_count"], CAP + 1)
        self.assertEqual(manifest["state"], State.FAILED.value)


class TestLoopStopsAtTheCap(RevisionCapTestCase):
    """Rejections 1..cap stay in the loop; the next one goes to a human."""

    def _reject_until_it_stops(self, manifest: dict, limit: int = 10) -> str:
        """Rejection loop exactly as qa/gate/design drive it. Returns final state."""
        for round_no in range(1, limit + 1):
            result = transition(
                manifest, State.NEEDS_REVISION, by="qa", why=f"rejection #{round_no}"
            )
            if result is State.FAILED:
                return State.FAILED.value
            transition(manifest, State.IN_PROGRESS, by="marketing", why="rework start")
        return State.parse(manifest["state"]).value

    def test_loop_stops_at_the_cap_and_escalates_to_failed(self) -> None:
        capsule, manifest = self.make_capsule()
        final = self._reject_until_it_stops(manifest)
        self.assertEqual(final, State.FAILED.value, "the loop must not run forever")
        self.assertEqual(manifest["revision_count"], CAP + 1)
        # two history entries for the crossing rejection: the entry, then the
        # human-decision escalation with a clear reason
        tail = manifest["history"][-1]
        self.assertEqual(tail["to_state"], State.FAILED.value)
        self.assertIn("revision cap exceeded", tail["why"])
        self.assertIn(f"{CAP + 1} > {CAP}", tail["why"])
        previous = manifest["history"][-2]
        self.assertEqual(previous["to_state"], State.NEEDS_REVISION.value)

        save_manifest(capsule, manifest)
        reloaded = load_manifest(capsule)
        self.assertEqual(reloaded["state"], State.FAILED.value)
        self.assertEqual(reloaded["revision_count"], CAP + 1)

        # route report: the reason names the cap (feeds the pulse's held list)
        action, reason = self.route_reason(capsule)
        self.assertIs(action, Action.HOLD)
        self.assertIn("revision cap exceeded", reason)
        self.assertIn(f"{CAP + 1} > {CAP}", reason)

    def test_loop_keeps_dispatching_until_the_cap(self) -> None:
        """Entries 1..cap are lawful rework rounds: the router still executes."""
        capsule, manifest = self.make_capsule()
        for round_no in range(1, CAP + 1):
            result = transition(
                manifest, State.NEEDS_REVISION, by="qa", why=f"rejection #{round_no}"
            )
            self.assertIs(result, State.NEEDS_REVISION)
            save_manifest(capsule, manifest)
            action, reason = self.route_reason(capsule)
            self.assertIs(action, Action.EXECUTE, f"entry {round_no} must still route")
            transition(manifest, State.IN_PROGRESS, by="marketing", why="rework")
        self.assertEqual(manifest["revision_count"], CAP)

    def test_configured_cap_is_honored(self) -> None:
        self.write_config(max_revisions_per_capsule=1)
        capsule, manifest = self.make_capsule()
        first = transition(manifest, State.NEEDS_REVISION, by="qa", why="rejection #1")
        self.assertIs(first, State.NEEDS_REVISION)
        transition(manifest, State.IN_PROGRESS, by="marketing", why="rework")
        second = transition(manifest, State.NEEDS_REVISION, by="qa", why="rejection #2")
        self.assertIs(second, State.FAILED)
        self.assertEqual(manifest["revision_count"], 2)

    def test_default_cap_when_config_key_is_absent(self) -> None:
        spend = _spend_config()
        del spend["spend"]["max_revisions_per_capsule"]
        path = self.tmp / "engine.json"
        path.write_text(json.dumps(spend), encoding="utf-8")
        os.environ["AGENCY_ENGINE_CONFIG"] = str(path)
        spend_control.reset_caches()
        self.assertEqual(spend_control.revision_cap(), CAP)
        self.assertEqual(revision_cap(), CAP)

    def test_unreadable_config_never_lifts_the_cap(self) -> None:
        path = self.tmp / "engine.json"
        path.write_text("{ not json", encoding="utf-8")
        os.environ["AGENCY_ENGINE_CONFIG"] = str(path)
        spend_control.reset_caches()
        # money gates fail closed (raise); the cap fails *safe* to the default
        with self.assertRaises(spend_control.SpendCapExceeded):
            spend_control.load_config()
        self.assertEqual(revision_cap(), CAP)

    def test_resurrect_opens_a_fresh_budget(self) -> None:
        capsule, manifest = self.make_capsule(
            state=State.FAILED, revision_count=CAP + 1, attempt=3
        )
        resurrect(manifest, State.NEEDS_REVISION, by="human", why="human: try once more")
        self.assertEqual(revision_count(manifest), 1)  # the re-entry counts, budget fresh
        self.assertEqual(manifest["attempt"], 0)
        save_manifest(capsule, manifest)
        action, _ = self.route_reason(capsule)
        self.assertIs(action, Action.EXECUTE, "a resurrected capsule must route again")


class TestHumanRejectionCounts(RevisionCapTestCase):
    """Requirement 4: the human gate path counts exactly like the workers."""

    def test_rejection_through_the_approval_service_counts(self) -> None:
        capsule, _ = self.make_capsule(state=State.PENDING_APPROVAL)
        outcome = approval.reject(
            capsule,
            principal=Principal.legacy_root("revision-cap-tester"),
            reviewer="manager",
            audit_note="unit-test",
            reason="not on brand",
        )
        self.assertEqual(outcome.state, State.NEEDS_REVISION.value)
        reloaded = load_manifest(capsule)
        self.assertEqual(revision_count(reloaded), 1)
        self.assertEqual(reloaded["state"], State.NEEDS_REVISION.value)
        self.assertTrue((capsule / "feedback.txt").is_file())

    def test_human_rejection_over_the_cap_reports_failed(self) -> None:
        capsule, _ = self.make_capsule(
            state=State.PENDING_APPROVAL, revision_count=CAP
        )
        outcome = approval.reject(
            capsule,
            principal=Principal.legacy_root("revision-cap-tester"),
            reviewer="manager",
            audit_note="unit-test",
            reason="third time is not the charm",
        )
        self.assertEqual(outcome.state, State.FAILED.value)
        reloaded = load_manifest(capsule)
        self.assertEqual(reloaded["state"], State.FAILED.value)
        self.assertEqual(revision_count(reloaded), CAP + 1)
        action, reason = self.route_reason(capsule)
        self.assertIs(action, Action.HOLD)
        self.assertIn("revision cap exceeded", reason)


class TestCounterSurvivesLockAndCrash(RevisionCapTestCase):
    """The counter persists with the transition, or not at all — never torn."""

    def test_counter_survives_lock_save_and_reload(self) -> None:
        capsule, _ = self.make_capsule()
        with CapsuleLock(capsule, purpose="test-writer", timeout=10):
            manifest = load_manifest(capsule)
            transition(manifest, State.NEEDS_REVISION, by="qa", why="rejection #1")
            save_manifest(capsule, manifest)
        reloaded = load_manifest(capsule)
        self.assertEqual(revision_count(reloaded), 1)
        self.assertEqual(reloaded["state"], State.NEEDS_REVISION.value)

    def test_crash_before_save_loses_the_transition_not_the_counter(self) -> None:
        capsule, _ = self.make_capsule()
        manifest = load_manifest(capsule)
        transition(manifest, State.NEEDS_REVISION, by="qa", why="rejection #1")
        save_manifest(capsule, manifest)  # committed

        # crash: a later mutation never reaches the disk
        doomed = load_manifest(capsule)
        transition(doomed, State.IN_PROGRESS, by="marketing", why="rework (lost)")
        self.assertEqual(revision_count(doomed), 1)

        survivor = load_manifest(capsule)  # what any other process sees
        self.assertEqual(revision_count(survivor), 1)
        self.assertEqual(survivor["state"], State.NEEDS_REVISION.value)
        # redoing the lost write continues from the persisted counter (no
        # double count, no lost count)
        transition(survivor, State.IN_PROGRESS, by="marketing", why="rework")
        transition(survivor, State.NEEDS_REVISION, by="qa", why="rejection #2")
        self.assertEqual(revision_count(survivor), 2)


class TestRouterRefusal(RevisionCapTestCase):
    """Requirement 3: over the cap means HOLD with the reason, never a worker."""

    def test_needs_revision_over_cap_is_never_dispatched(self) -> None:
        capsule, _ = self.make_capsule(state=State.NEEDS_REVISION, revision_count=CAP + 1)
        decision = CapsuleRouter().route(capsule)
        self.assertIs(decision.action, Action.HOLD)
        self.assertIsNone(decision.worker)
        self.assertIn("revision cap exceeded", decision.reason)
        self.assertIn("human decision required", decision.reason)
        self.assertIn("revision cap exceeded", decision.as_dict()["reason"])

    def test_needs_revision_at_the_cap_still_routes(self) -> None:
        capsule, _ = self.make_capsule(state=State.NEEDS_REVISION, revision_count=CAP)
        decision = CapsuleRouter().route(capsule)
        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "marketing")  # follows the declared domain

    def test_cap_lowered_after_the_fact_stops_dispatch(self) -> None:
        """Defence in depth: a capsule written under cap 3, config now 1."""
        capsule, _ = self.make_capsule(state=State.NEEDS_REVISION, revision_count=2)
        self.assertIs(self.route_reason(capsule)[0], Action.EXECUTE)
        self.write_config(max_revisions_per_capsule=1)
        action, reason = self.route_reason(capsule)
        self.assertIs(action, Action.HOLD)
        self.assertIn("revision cap exceeded", reason)

    def test_over_cap_failed_reports_the_cap_not_the_attempt(self) -> None:
        capsule, _ = self.make_capsule(
            state=State.FAILED, revision_count=CAP + 1, attempt=0
        )
        _, reason = self.route_reason(capsule)
        self.assertIn("revision cap exceeded", reason)
        # ordinary failures keep the old wording
        plain, _ = self.make_capsule("cap_plain", state=State.FAILED, attempt=3)
        _, plain_reason = self.route_reason(plain)
        self.assertIn("attempt cap reached", plain_reason)
        self.assertNotIn("revision cap", plain_reason)


class TestAttentionReport(RevisionCapTestCase):
    """The dashboard attention report carries the cap and its reason."""

    def _report(self, report_name: str = "attention.json") -> dict:
        output = self.tmp / report_name
        return watcher.run(self.spool, output)

    def test_over_cap_needs_revision_is_flagged_with_the_reason(self) -> None:
        self.make_capsule("cap_over", state=State.NEEDS_REVISION, revision_count=CAP + 1)
        self.make_capsule("cap_ok", state=State.NEEDS_REVISION, revision_count=CAP - 1)
        report = self._report()

        bucket = report["revision_cap"]
        self.assertEqual([entry["capsule_id"] for entry in bucket], ["cap_over"])
        flagged = bucket[0]
        self.assertIn("revision cap exceeded", flagged["attention_reason"])
        self.assertEqual(flagged["revision_count"], CAP + 1)
        self.assertEqual(report["counts"]["revision_cap"], 1)
        self.assertEqual(report["counts"]["healthy"], 1)  # cap_ok stays healthy

        # written atomically and parseable by the dashboard
        on_disk = json.loads((self.tmp / "attention.json").read_text("utf-8"))
        self.assertEqual(on_disk["revision_cap"], bucket)

    def test_escalated_failed_shows_the_cap_reason_in_system_halt(self) -> None:
        self.make_capsule(
            "cap_halt", state=State.FAILED, revision_count=CAP + 1, attempt=1
        )
        report = self._report()
        entry = report["system_halt"][0]
        self.assertEqual(entry["revision_count"], CAP + 1)
        self.assertIn("revision cap exceeded", entry["attention_reason"])
        # a plain crash failure keeps the halt entry without the cap reason
        self.make_capsule("cap_crash", state=State.FAILED, revision_count=0, attempt=2)
        report = self._report("attention2.json")
        crash = next(e for e in report["system_halt"] if e["capsule_id"] == "cap_crash")
        self.assertNotIn("attention_reason", crash)

    def test_pending_approval_is_still_authorization_not_cap(self) -> None:
        self.make_capsule("cap_gate", state=State.PENDING_APPROVAL, revision_count=CAP + 1)
        report = self._report()
        self.assertEqual(
            [entry["capsule_id"] for entry in report["requires_authorization"]],
            ["cap_gate"],
        )
        self.assertEqual(report["revision_cap"], [])


class TestConfigWiring(RevisionCapTestCase):
    """The cap value lives in config/engine.json -> spend, one per layer."""

    def test_shipped_config_declares_the_default(self) -> None:
        os.environ["AGENCY_ENGINE_CONFIG"] = str(REPO_ROOT / "config" / "engine.json")
        spend_control.reset_caches()
        self.assertEqual(
            spend_control.load_config().max_revisions_per_capsule, CAP
        )
        raw = json.loads((REPO_ROOT / "config" / "engine.json").read_text("utf-8"))
        self.assertEqual(raw["spend"]["max_revisions_per_capsule"], CAP)

    def test_layer_defaults_agree(self) -> None:
        self.assertIs(
            spend_control.DEFAULT_REVISION_CAP, manifest_engine.DEFAULT_REVISION_CAP,
            "one constant, imported — never two drifting copies",
        )
        self.assertEqual(watcher.DEFAULT_REVISION_CAP, CAP)

    def test_watcher_reader_is_wired_to_the_engine_config(self) -> None:
        fake_root = self.tmp / "fake_root"
        (fake_root / "config").mkdir(parents=True)
        (fake_root / "config" / "engine.json").write_text(
            json.dumps({"spend": {"max_revisions_per_capsule": 1}}), encoding="utf-8"
        )
        with mock.patch.object(watcher, "ROOT", fake_root):
            self.assertEqual(watcher._revision_cap(), 1)
        # malformed config keeps the default — never lifts the cap
        (fake_root / "config" / "engine.json").write_text("{oops", encoding="utf-8")
        with mock.patch.object(watcher, "ROOT", fake_root):
            self.assertEqual(watcher._revision_cap(), watcher.DEFAULT_REVISION_CAP)


class TestNoBypass(RevisionCapTestCase):
    """Static guard: transition() stays THE chokepoint for needs_revision.

    The counter only means something if no writer can move a capsule into
    ``needs_revision`` behind its back. This scans every capsule-manifest
    writer (the kernel, the workers, and the orchestration routes that touch
    capsules) for a direct ``...["state"] = ...needs_revision...`` assignment.
    ``dispatcher.py`` / ticket code is deliberately out of scope: tickets are
    a different state machine (their own attempt-cap policy).
    """

    _SCAN_ROOTS = (
        REPO_ROOT / "engine" / "capsule_engine",
        REPO_ROOT / "engine" / "workers",
    )
    _SCAN_FILES = (
        REPO_ROOT / "engine" / "orchestration" / "api.py",
        REPO_ROOT / "engine" / "orchestration" / "approval.py",
        REPO_ROOT / "engine" / "orchestration" / "capsule_pulse.py",
        REPO_ROOT / "engine" / "orchestration" / "directus_bridge.py",
    )

    @staticmethod
    def _needs_revision_state_writes(path: Path) -> list[str]:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                index = target.slice
                if isinstance(index, ast.Index):  # pragma: no cover - py<3.9
                    index = index.value
                if not (isinstance(index, ast.Constant) and index.value == "state"):
                    continue
                segment = ast.get_source_segment(source, node.value) or ""
                if "NEEDS_REVISION" in segment or "needs_revision" in segment:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {segment}")
        return hits

    def test_no_needs_revision_state_write_bypasses_the_counter(self) -> None:
        files: list[Path] = []
        for root in self._SCAN_ROOTS:
            files.extend(sorted(root.glob("*.py")))
        files.extend(self._SCAN_FILES)
        offenders: list[str] = []
        for path in files:
            if path.name == "manifest.py":
                continue  # the chokepoint itself
            offenders.extend(self._needs_revision_state_writes(path))
        self.assertEqual(
            offenders,
            [],
            "needs_revision must only be entered through manifest.transition() "
            f"(which increments revision_count and enforces the cap): {offenders}",
        )

    def test_every_rejecting_module_calls_the_chokepoint(self) -> None:
        """The named rejection sources literally route through transition().

        QA and the human gate reject directly; design-gate re-entry is
        rejected by the gate runner while the capsule sits ``pending_design``
        (the design worker itself only ever moves to ``pending_qa`` — it has
        no rejection edge of its own, proved by the AST scan above).
        """
        sources = {
            REPO_ROOT / "engine" / "workers" / "qa.py": "self.transition(",
            REPO_ROOT / "engine" / "workers" / "gate_runner.py": "self.transition(",
            REPO_ROOT / "engine" / "orchestration" / "approval.py": "transition(",
        }
        for path, call in sources.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn(call, text, f"{path.name} must reject via transition()")
            self.assertIn(
                "State.NEEDS_REVISION", text, f"{path.name} must reject to needs_revision"
            )
        design = (REPO_ROOT / "engine" / "workers" / "design.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("self.transition(", design)  # moves stages through the kernel
        self.assertNotIn(
            'State.NEEDS_REVISION', design,
            "design has no rejection edge — its gate's rejection is gate_runner's",
        )


if __name__ == "__main__":
    unittest.main()
