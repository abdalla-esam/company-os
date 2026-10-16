"""Task types — the pipeline as data, validated fail-closed.

Slice 1 of ``docs/TASK_TYPES.md``. What this suite pins, and why each assertion
is load-bearing:

- **the schema is closed**: every malformed document is refused with a message
  naming the offending element, so a typo can never be silently ignored;
- **the pipeline is declarative**, but the *engine* is untouched — the router,
  the transition matrix and the workers are not edited by this slice;
- **routing parity**: a capsule with a generated task card routes exactly like
  a legacy capsule with no card, for every state the card can declare. That is
  the whole promise of slice 1 ("social_post is behaviour-identical");
- **the approval boundary is unchanged**: a task-type capsule still seats with a
  seeded gate plan, still binds `copy.md`, still refuses a tampered artifact
  and still refuses approval with no plan;
- **the generated card is not an output**: it is never hash-bound, never in
  ``manifest.outputs``, and legacy capsules (no ``task_type``) write no card at
  all.

No test here runs the beat or the pulse, and none touches real ``config/`` or
``data/`` — ``tests/_bootstrap.py`` points the catalogue at a vault copy.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repo paths + isolated vault

from tests._bootstrap import SPOOL

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from capsule_engine import task_profile
from capsule_engine import task_type as task_type_engine
from capsule_engine.exceptions import LockTimeoutError, SecurityError
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import (
    ARTIFACT_FILE,
    State,
    append_history,
    get_capsule_outputs,
    load_manifest,
    new_manifest,
    save_manifest,
    transition,
    verify_artifact,
)
from capsule_engine.router import DEFAULT_WORKER_BY_STATE, CapsuleRouter
from orchestration import access_control, api, approval

CARD = task_type_engine.CARD_NAME


def _request(path: str = "/api/capsules") -> Request:
    """A request carrying a root principal (identity comes from the credential)."""
    principal = access_control.Principal.legacy_root("task-type-tester")
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "state": {"principal": principal},
        }
    )


def _social_post_document() -> dict:
    """The shipped ``social_post`` document, as a mutable dict to perturb."""
    path = task_type_engine.task_types_dir() / "social_post.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _CatalogueCase(unittest.TestCase):
    """A temporary catalogue directory; each test perturbs one copy of social_post."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="task_types_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        task_type_engine.clear_cache()
        self.addCleanup(task_type_engine.clear_cache)

    def _write(self, document: dict, name: str = "probe") -> Path:
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def _load(self, name: str = "probe"):
        return task_type_engine.load_task_type(name, directory=self.dir)

    def assert_refused(
        self, document: dict, *, needle: str, name: str | None = None
    ) -> None:
        """The document is written, then refused with a message naming the break.

        It is written under its own declared id, so the only refusal under test
        is the one the caller perturbed (never the filename/id check).
        """
        name = name or document.get("task_type") or "probe"
        self._write(document, name)
        with self.assertRaises(task_type_engine.TaskTypeError) as ctx:
            self._load(name)
        self.assertIn(needle, str(ctx.exception))


class ShippedSocialPost(_CatalogueCase):
    """1 — the real document parses into the pipeline it claims to be."""

    def test_valid_social_post_parses(self) -> None:
        self._write(_social_post_document(), "social_post")
        parsed = self._load("social_post")

        self.assertEqual(parsed.task_type, "social_post")
        self.assertEqual(parsed.version, 1)
        self.assertEqual(parsed.domain, "marketing")
        self.assertEqual(parsed.primary_artifact, "copy.md")
        self.assertEqual(parsed.gate_template, "marketing")
        self.assertEqual(
            [step.step_id for step in parsed.steps],
            ["copy", "qa", "design", "approval"],
        )
        # The pipeline is ordered and the boundary is explicit, not inferred.
        self.assertEqual(parsed.entry_step().step_id, "copy")
        self.assertTrue(parsed.boundary_step().is_boundary)
        self.assertIsNone(parsed.boundary_step().worker)
        # The QA fork is declared, not hardcoded in a transition matrix.
        qa = parsed.step("qa")
        self.assertEqual(qa.on_success_branch.branch_on, "flags.requires_image")
        self.assertEqual(qa.on_success_branch.when_true, "design")
        self.assertEqual(qa.on_success_branch.when_false, "approval")
        self.assertEqual(parsed.step("design").on_success, "qa")


class SchemaIsClosed(_CatalogueCase):
    """2,3,4,5,6,7,8,9,10,11 — every malformed document is refused."""

    def test_unknown_version_refused(self) -> None:
        document = _social_post_document()
        document["version"] = 2
        self.assert_refused(document, needle="unsupported task type version")

    def test_empty_steps_refused(self) -> None:
        document = _social_post_document()
        document["steps"] = []
        self.assert_refused(document, needle="at least one step")

    def test_too_many_steps_refused(self) -> None:
        document = _social_post_document()
        document["steps"] = document["steps"] * 4  # 16 > MAX_STEPS (12)
        self.assert_refused(document, needle="> 12")

    def test_duplicate_step_id_refused(self) -> None:
        document = _social_post_document()
        document["steps"][2]["id"] = document["steps"][1]["id"]
        self.assert_refused(document, needle="duplicated")

    def test_dangling_target_refused(self) -> None:
        document = _social_post_document()
        document["steps"][0]["on_success"] = "nowhere"
        self.assert_refused(document, needle="does not name a declared step")

    def test_dangling_branch_target_refused(self) -> None:
        document = _social_post_document()
        document["steps"][1]["on_success"]["when_false"] = "ghost"
        self.assert_refused(document, needle="does not name a declared step")

    def test_unknown_gate_type_refused(self) -> None:
        document = _social_post_document()
        document["steps"][1]["gate"] = "legal_review"
        self.assert_refused(document, needle="unknown gate type")

    def test_absolute_output_path_refused(self) -> None:
        document = _social_post_document()
        document["steps"][0]["outputs"] = [
            {"kind": "text/markdown", "default_path": "/etc/passwd"}
        ]
        self.assert_refused(document, needle="default_path")

    def test_escaping_output_path_refused(self) -> None:
        document = _social_post_document()
        document["steps"][0]["outputs"] = [
            {"kind": "text/markdown", "default_path": "../../escape.md"}
        ]
        self.assert_refused(document, needle="default_path")

    def test_empty_acceptance_refused(self) -> None:
        document = _social_post_document()
        document["steps"][0]["acceptance"] = "   "
        self.assert_refused(document, needle="acceptance")

    def test_budget_is_refused_until_the_cost_ledger_exists(self) -> None:
        """Decision §8.4: no budget field yet, so declaring one is refused.

        A negative/unbounded budget therefore cannot slip through either — the
        whole block is rejected as unsupported rather than half-validated.
        """
        document = _social_post_document()
        document["budget"] = {"max_usd": -1.0, "on_exceed": "explode"}
        self.assert_refused(document, needle="budget")

    def test_no_boundary_step_refused(self) -> None:
        """A task type that can never face a human gate is inadmissible."""
        document = _social_post_document()
        for step in document["steps"]:
            if step.get("gate") == task_type_engine.BOUNDARY_GATE:
                step["gate"] = "qa_review"  # valid gate, but no longer the boundary
                step["worker"] = "qa"
        self.assert_refused(document, needle="approval boundary")

    def test_unknown_top_level_key_refused(self) -> None:
        document = _social_post_document()
        document["shortcut"] = True
        self.assert_refused(document, needle="shortcut")

    def test_filename_must_match_the_id(self) -> None:
        self._write(_social_post_document(), "renamed")
        with self.assertRaises(task_type_engine.TaskTypeError) as ctx:
            self._load("renamed")
        self.assertIn("does not match its filename", str(ctx.exception))

    def test_worker_must_be_a_registry_name_not_a_module_path(self) -> None:
        document = _social_post_document()
        document["steps"][0]["worker"] = "data.domains.marketing.post_builder"
        self.assert_refused(document, needle="worker")

    def test_malformed_id_cannot_escape_the_catalogue(self) -> None:
        for bad in ("../escape", "Bad-Id", "", "1starts_with_digit"):
            self.assertFalse(task_type_engine.is_valid_id(bad))
            with self.assertRaises(task_type_engine.TaskTypeError):
                task_type_engine.load_task_type(bad, directory=self.dir)


class MaterialisedCard(_CatalogueCase):
    """13 — the rendered card is exactly what the routing table encodes."""

    def setUp(self) -> None:
        super().setUp()
        self._write(_social_post_document(), "social_post")
        self.parsed = self._load("social_post")
        self.card = self.parsed.to_task_profile()

    def test_card_shape_is_the_accepted_b1_contract(self) -> None:
        self.assertEqual(self.card["version"], 1)
        self.assertEqual(self.card["kind"], "social_post")
        self.assertEqual(self.card["artifact"], "copy.md")
        self.assertEqual(
            {stage["state"] for stage in self.card["stages"]},
            {"draft", "needs_revision", "pending_qa", "pending_design"},
        )

    def test_rendered_card_passes_the_real_card_validator(self) -> None:
        """The materialiser cannot invent a card the engine would reject."""
        holder = Path(tempfile.mkdtemp(prefix="card_"))
        self.addCleanup(shutil.rmtree, holder, ignore_errors=True)
        (holder / task_type_engine.CARD_NAME).write_text(
            json.dumps(self.card), encoding="utf-8"
        )

        profile = task_profile.load_task_profile(holder)

        self.assertIsNotNone(profile)
        self.assertEqual(profile.kind, "social_post")
        self.assertEqual(profile.artifact, "copy.md")

    def test_card_worker_matches_the_default_routing_table(self) -> None:
        by_state = {stage["state"]: stage["worker"] for stage in self.card["stages"]}
        for state in ("draft", "pending_qa", "pending_design", "needs_revision"):
            expected = DEFAULT_WORKER_BY_STATE[State(state)]
            with self.subTest(state=state):
                self.assertEqual(by_state[state], expected)
                self.assertEqual(self.parsed.worker_for_state(state), expected)

    def test_card_declares_only_routable_states(self) -> None:
        """A card may never claim a constitutional state."""
        for stage in self.card["stages"]:
            self.assertIn(stage["state"], task_profile.ROUTABLE_STATES)
            self.assertNotIn(stage["state"], task_profile.FORBIDDEN_STATES)


class CreationWritesTheCard(_CatalogueCase):
    """12 (moved to creation time) — the card is written by the creation path."""

    def _create(self, capsule_id: str, **kwargs):
        cleanups = []
        directory = SPOOL / capsule_id
        cleanups.append((shutil.rmtree, directory, {"ignore_errors": True}))
        for func, arg, kw in cleanups:
            self.addCleanup(func, arg, **kw)
        return directory

    def test_unknown_task_type_refused_at_creation(self) -> None:
        """A 400 before anything is written — no directory, no card, no manifest."""
        capsule_id = "cap_tt_unknown_type"
        directory = self._create(capsule_id)
        request = api.CreateCapsuleRequest(
            text="must never reach disk",
            capsule_id=capsule_id,
            sector="marketing",
            task_type="definitely_not_a_type",
        )

        with self.assertRaises(api.HTTPException) as ctx:
            asyncio.run(api.create_capsule_endpoint(request, _request()))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(directory.exists(), "a refused creation must not mkdir")

    def test_creation_writes_task_type_and_card(self) -> None:
        capsule_id = "cap_tt_writes_card"
        directory = self._create(capsule_id)
        request = api.CreateCapsuleRequest(
            text="declared pipeline",
            capsule_id=capsule_id,
            sector="marketing",
            task_type="social_post",
        )

        asyncio.run(api.create_capsule_endpoint(request, _request()))

        manifest = load_manifest(directory)
        self.assertEqual(manifest.get("task_type"), "social_post")
        card_path = directory / CARD
        self.assertTrue(card_path.is_file(), "the creation path must write the card")
        card = json.loads(card_path.read_text(encoding="utf-8"))
        self.assertEqual(card["kind"], "social_post")
        self.assertEqual(
            {stage["state"] for stage in card["stages"]},
            {"draft", "needs_revision", "pending_qa", "pending_design"},
        )

    def test_legacy_creation_writes_no_card_and_no_field(self) -> None:
        """§9.2 — a capsule without a task type behaves exactly as today."""
        capsule_id = "cap_tt_legacy_default"
        directory = self._create(capsule_id)
        request = api.CreateCapsuleRequest(
            text="legacy capsule", capsule_id=capsule_id, sector="marketing"
        )

        asyncio.run(api.create_capsule_endpoint(request, _request()))

        manifest = load_manifest(directory)
        self.assertNotIn("task_type", manifest)
        self.assertFalse((directory / CARD).exists())

    def test_busy_capsule_writes_no_manifest_artifact_or_card(self) -> None:
        """The card is written *inside* the lock: contention writes nothing."""
        capsule_id = "cap_tt_busy"
        directory = self._create(capsule_id)
        directory.mkdir(parents=True, exist_ok=True)

        def _zero_timeout(capsule_path, **kwargs):
            return CapsuleLock(
                capsule_path, timeout=0, purpose=kwargs.get("purpose", "waiter")
            )

        request = api.CreateCapsuleRequest(
            text="contentious",
            capsule_id=capsule_id,
            sector="marketing",
            task_type="social_post",
        )

        with CapsuleLock(directory, purpose="other-writer"):
            with mock.patch.object(api, "CapsuleLock", _zero_timeout):
                with self.assertRaises(LockTimeoutError):
                    asyncio.run(api.create_capsule_endpoint(request, _request()))

        self.assertFalse((directory / "manifest.json").exists())
        self.assertFalse((directory / ARTIFACT_FILE).exists())
        self.assertFalse((directory / CARD).exists())

    def test_catalogue_endpoints_are_read_only_views(self) -> None:
        listing = asyncio.run(api.list_task_types_endpoint())
        self.assertGreaterEqual(listing["count"], 1)
        self.assertIn("social_post", [t["task_type"] for t in listing["task_types"]])

        one = asyncio.run(api.get_task_type_endpoint("social_post"))
        self.assertEqual(one["task_type"]["task_type"], "social_post")
        self.assertEqual(one["task_profile"]["kind"], "social_post")

        # No write API exists: the catalogue directory is untouched by a read.
        before = sorted(p.name for p in task_type_engine.task_types_dir().iterdir())
        asyncio.run(api.list_task_types_endpoint())
        after = sorted(p.name for p in task_type_engine.task_types_dir().iterdir())
        self.assertEqual(before, after)

    def test_unknown_task_type_endpoint_is_404(self) -> None:
        with self.assertRaises(api.HTTPException) as ctx:
            asyncio.run(api.get_task_type_endpoint("no_such_type"))
        self.assertEqual(ctx.exception.status_code, 404)


class RouterParity(_CatalogueCase):
    """14 — a card-bearing capsule routes exactly like a legacy one."""

    CARD_STATES = (
        State.DRAFT,
        State.NEEDS_REVISION,
        State.PENDING_QA,
        State.PENDING_DESIGN,
    )

    def setUp(self) -> None:
        super().setUp()
        self._write(_social_post_document(), "social_post")
        self.card = self._load("social_post").to_task_profile()

    def _capsule(
        self,
        capsule_id: str,
        state: State,
        *,
        card: dict | None = None,
        stale: bool = False,
    ) -> Path:
        directory = SPOOL / capsule_id
        directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        manifest = new_manifest(capsule_id, state=state)
        manifest["domain"] = "marketing"
        manifest["routing_context"] = {
            "domain": "marketing",
            "flags": {"requires_copy": True, "requires_image": True},
        }
        manifest["metadata"] = {
            "topic": "topic",
            "target_platform": "linkedin",
            "artifact_path": "copy.md",
        }
        if stale:
            # A dead lease: the recorded owner is the lawful worker to resume.
            append_history(manifest, by="marketing", why="claimed for a run")
            manifest["updated_at"] = "2020-01-01T00:00:00+00:00"
        save_manifest(directory, manifest)

        if card is not None:
            (directory / CARD).write_text(json.dumps(card), encoding="utf-8")
        return directory

    def test_decisions_match_for_every_card_declarable_state(self) -> None:
        router = CapsuleRouter()
        for index, state in enumerate(self.CARD_STATES):
            with self.subTest(state=state.value):
                legacy = self._capsule(f"cap_parity_legacy_{index}", state)
                typed = self._capsule(f"cap_parity_typed_{index}", state, card=self.card)

                before = router.route(legacy)
                after = router.route(typed)

                self.assertEqual(
                    (before.action, before.worker),
                    (after.action, after.worker),
                    msg=f"{state.value}: card changed routing",
                )
                self.assertTrue(after.should_execute, after.reason)

    def test_lease_rules_are_untouched_by_a_card(self) -> None:
        """``in_progress`` is constitutional: a card never gets a say."""
        router = CapsuleRouter()
        for index, stale in enumerate((False, True)):
            with self.subTest(stale=stale):
                legacy = self._capsule(f"cap_lease_legacy_{index}", State.IN_PROGRESS, stale=stale)
                typed = self._capsule(
                    f"cap_lease_typed_{index}", State.IN_PROGRESS, card=self.card, stale=stale
                )

                before = router.route(legacy)
                after = router.route(typed)

                self.assertEqual(
                    (before.action, before.worker), (after.action, after.worker)
                )
                if stale:
                    self.assertEqual(after.worker, "marketing")
                else:
                    self.assertFalse(after.should_execute)


class ApprovalBoundaryUnchanged(_CatalogueCase):
    """15 — the guarantees the task-type model must not weaken."""

    def _task_type_capsule(self, capsule_id: str) -> Path:
        directory = SPOOL / capsule_id
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        request = api.CreateCapsuleRequest(
            text="bound content",
            capsule_id=capsule_id,
            sector="marketing",
            task_type="social_post",
        )
        asyncio.run(api.create_capsule_endpoint(request, _request()))
        return directory

    def test_card_is_not_an_output_and_is_not_hash_bound(self) -> None:
        directory = self._task_type_capsule("cap_bound_outputs")
        manifest = load_manifest(directory)

        outputs = get_capsule_outputs(manifest)
        self.assertEqual([entry["path"] for entry in outputs], ["copy.md"])
        self.assertNotIn(CARD, json.dumps(outputs))
        self.assertEqual(manifest["metadata"]["artifact_path"], "copy.md")
        # The gate plan never targets the card either.
        self.assertNotIn(CARD, json.dumps(manifest.get("gates") or []))

    def test_seat_still_binds_the_artifact_and_seeds_the_plan(self) -> None:
        directory = self._task_type_capsule("cap_bound_seat")
        manifest = load_manifest(directory)

        self.assertEqual(manifest["state"], "pending_approval")
        self.assertTrue(manifest["metadata"]["artifact_sha256"])
        self.assertTrue(manifest.get("gates"), "the gate plan must be seeded")
        verify_artifact(manifest, directory)  # untampered: no raise

    def test_tampered_artifact_refuses_the_approval_edge(self) -> None:
        directory = self._task_type_capsule("cap_bound_tamper")
        manifest = load_manifest(directory)
        on_disk = (directory / "manifest.json").read_bytes()

        (directory / "copy.md").write_text("tampered", encoding="utf-8")

        with self.assertRaises(SecurityError):
            transition(
                manifest,
                State.APPROVED,
                by="tester",
                why="must be refused",
                capsule_path=directory,
            )
        self.assertEqual(
            (directory / "manifest.json").read_bytes(),
            on_disk,
            "a refused approval edge must mutate nothing",
        )

    def test_planless_task_type_capsule_cannot_be_approved(self) -> None:
        directory = self._task_type_capsule("cap_bound_planless")
        with CapsuleLock(directory):
            manifest = load_manifest(directory)
            manifest["gates"] = []  # strip the plan, exactly like a repaired capsule
            save_manifest(directory, manifest)

        with self.assertRaises(approval.ApprovalRefused):
            approval.approve(
                directory,
                principal=access_control.Principal.legacy_root("tester"),
                reviewer="tester",
                audit_note="planless",
            )

        self.assertEqual(load_manifest(directory)["state"], "pending_approval")


if __name__ == "__main__":
    unittest.main()
