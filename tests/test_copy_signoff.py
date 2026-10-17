"""The copy sign-off: a human reads the copy before an image model is paid.

The design stage is the first step that spends on a generated image, and the
copy is the artifact that decides whether the post is worth making at all. A
domain can therefore require a sign-off, which parks the capsule at
``pending_design`` — copy written, QA passed, no image yet — until a person
decides. These tests pin the four moving parts:

- the **policy** that turns the requirement on for named domains (fail-safe
  parsing: a malformed section falls back to disabled, never to required);
- the **router**, which holds ``pending_design`` while the sign-off is owed and
  gets out of the way the moment it is recorded (and never wedges a domain
  that does not ask for one);
- the **QA worker**, which requests the sign-off as it routes to design and
  binds the copy's digest so the decision names the text that was read;
- the **decision route**, which records who decided what, refuses a copy that
  changed after the ask, and sends a rejection back to the author through the
  ordinary ``needs_revision`` path.

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock
from tests._bootstrap import SPOOL, VAULT

from capsule_engine import State  # noqa: E402
from capsule_engine.manifest import (  # noqa: E402
    load_manifest,
    save_manifest,
)
from capsule_engine.policy import load_policy  # noqa: E402
from capsule_engine.router import (  # noqa: E402
    Action,
    CapsuleRouter,
)
from fastapi import HTTPException  # noqa: E402
from orchestration import access_control, api  # noqa: E402
from starlette.requests import Request  # noqa: E402
from workers.qa import QAWorker  # noqa: E402

ENGINE_CONFIG = VAULT / "engine.json"
COPY_FILE = "copy.md"

COPY_TEXT = (
    "A plain sentence of sufficient length for the automated reviewer to "
    "accept this probe capsule without any banned vocabulary."
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _restore_engine_config() -> None:
    """Put the shipped policy back and drop the parser's cached copy."""
    source = _bootstrap.REPO_ROOT / "config" / "engine.json"
    if source.is_file():
        shutil.copy2(source, ENGINE_CONFIG)
    load_policy(refresh=True)


def _write_policy(*, enabled: bool, domains: list[str] | None = None) -> None:
    """Write an engine config whose only interesting section is the sign-off."""
    section: dict = {"enabled": enabled}
    if domains is not None:
        section["domains"] = domains
    ENGINE_CONFIG.write_text(
        json.dumps({"version": 1, "copy_signoff": section}),
        encoding="utf-8",
    )
    load_policy(refresh=True)


def _manifest(
    capsule_id: str,
    state: State,
    *,
    domain: str = "marketing",
    metadata: dict | None = None,
) -> dict:
    merged: dict = {
        "capsule_id": capsule_id,
        "domain": domain,
        "state": state.value,
        "created_at": "2026-10-17T00:00:00+00:00",
        "updated_at": "2026-10-17T00:00:00+00:00",
        "attempt": 0,
        "history": [],
        "routing_context": {"flags": {"requires_image": True}},
    }
    if metadata is not None:
        merged["metadata"] = metadata
    return merged


def _place(manifest: dict, copy_text: str = COPY_TEXT) -> Path:
    """Write a capsule (manifest + copy) into the vault spool."""
    capsule_dir = SPOOL / manifest["capsule_id"]
    shutil.rmtree(capsule_dir, ignore_errors=True)
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / COPY_FILE).write_text(copy_text, encoding="utf-8")
    save_manifest(capsule_dir, manifest)
    return capsule_dir


def _request(path: str = "/api/capsules/probe/copy_signoff") -> Request:
    """A request carrying a root principal (no scope denial can apply)."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "state": {
                "principal": access_control.Principal.legacy_root("copy-signoff-tester")
            },
        }
    )


class _PassingGateway:
    """A stand-in gateway; the reviewer itself is patched per test."""

    last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


# --------------------------------------------------------------------------- #
# the policy
# --------------------------------------------------------------------------- #


class CopySignoffPolicyTests(unittest.TestCase):
    """Parsing is fail-safe: a broken section disables, it never requires."""

    def tearDown(self) -> None:
        _restore_engine_config()

    def test_a_missing_section_asks_for_nothing(self) -> None:
        ENGINE_CONFIG.write_text(json.dumps({"version": 1}), encoding="utf-8")
        policy = load_policy(refresh=True)

        self.assertFalse(policy.copy_signoff.enabled)
        self.assertFalse(policy.copy_signoff_required("marketing"))
        self.assertFalse(policy.copy_signoff_required("design"))

    def test_enabled_domains_require_only_themselves(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])
        policy = load_policy(refresh=True)

        self.assertTrue(policy.copy_signoff_required("marketing"))
        self.assertFalse(policy.copy_signoff_required("design"))
        self.assertFalse(policy.copy_signoff_required(None))

    def test_an_empty_domain_list_means_every_domain(self) -> None:
        _write_policy(enabled=True, domains=[])
        policy = load_policy(refresh=True)

        self.assertTrue(policy.copy_signoff_required("marketing"))
        self.assertTrue(policy.copy_signoff_required("design"))

    def test_disabled_asks_for_nothing_even_when_domains_are_listed(self) -> None:
        _write_policy(enabled=False, domains=["marketing"])
        policy = load_policy(refresh=True)

        self.assertFalse(policy.copy_signoff_required("marketing"))

    def test_a_non_boolean_enabled_falls_back_to_disabled(self) -> None:
        ENGINE_CONFIG.write_text(
            json.dumps({"version": 1, "copy_signoff": {"enabled": "maybe"}}),
            encoding="utf-8",
        )
        policy = load_policy(refresh=True)

        self.assertFalse(policy.copy_signoff.enabled)
        self.assertTrue(
            any("copy_signoff.enabled" in issue for issue in policy.issues),
            policy.issues,
        )

    def test_a_malformed_domains_list_falls_back_to_none(self) -> None:
        ENGINE_CONFIG.write_text(
            json.dumps(
                {"version": 1, "copy_signoff": {"enabled": True, "domains": "marketing"}}
            ),
            encoding="utf-8",
        )
        policy = load_policy(refresh=True)

        self.assertTrue(
            any("copy_signoff.domains" in issue for issue in policy.issues),
            policy.issues,
        )
        # With the list refused, the policy has no named domains — which for an
        # enabled policy means every domain, so the operator's "enabled" intent
        # is honoured but the malformed part is not.
        self.assertTrue(policy.copy_signoff_required("marketing"))

    def test_the_domain_view_carries_the_flag(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        self.assertTrue(load_policy(refresh=True).for_domain("marketing").copy_signoff_enabled)
        self.assertFalse(load_policy(refresh=True).for_domain("design").copy_signoff_enabled)


# --------------------------------------------------------------------------- #
# the router
# --------------------------------------------------------------------------- #


class RouterHoldsPendingDesignTests(unittest.TestCase):
    """``pending_design`` is held only while the sign-off is genuinely owed."""

    def setUp(self) -> None:
        self._router = CapsuleRouter()
        self._created: list[str] = []

    def tearDown(self) -> None:
        for capsule_id in self._created:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
        _restore_engine_config()

    def _route(self, capsule_id: str, **kwargs) -> object:
        manifest = _manifest(capsule_id, State.PENDING_DESIGN, **kwargs)
        _place(manifest)
        self._created.append(capsule_id)
        return self._router.route(str(SPOOL / capsule_id))

    def test_no_record_yet_is_a_hold_that_names_the_sign_off(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route("cap_signoff_none")

        self.assertIs(decision.action, Action.HOLD)
        self.assertIn("copy sign-off", decision.reason)

    def test_awaiting_is_a_hold(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route(
            "cap_signoff_awaiting",
            metadata={"copy_signoff": {"decision": "awaiting"}},
        )

        self.assertIs(decision.action, Action.HOLD)
        self.assertIn("copy sign-off", decision.reason)

    def test_approved_lets_the_design_stage_run(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route(
            "cap_signoff_approved",
            metadata={"copy_signoff": {"decision": "approved"}},
        )

        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "design")

    def test_waived_lets_the_design_stage_run(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route(
            "cap_signoff_waived",
            metadata={"copy_signoff": {"decision": "waived"}},
        )

        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "design")

    def test_rejected_is_a_hold_rather_than_a_silent_design_run(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route(
            "cap_signoff_rejected",
            metadata={"copy_signoff": {"decision": "rejected"}},
        )

        self.assertIs(decision.action, Action.HOLD)
        self.assertIn("rejected", decision.reason)

    def test_a_policy_that_is_off_never_delays_the_pipeline(self) -> None:
        _write_policy(enabled=False, domains=["marketing"])

        decision = self._route("cap_signoff_off")

        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "design")

    def test_a_domain_outside_the_list_is_untouched(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        decision = self._route("cap_signoff_other_domain", domain="design")

        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "design")

    def test_other_states_never_consult_the_sign_off(self) -> None:
        """The hold belongs to ``pending_design``; QA is never delayed by it."""
        _write_policy(enabled=True, domains=["marketing"])

        manifest = _manifest("cap_signoff_pending_qa", State.PENDING_QA)
        _place(manifest)
        self._created.append("cap_signoff_pending_qa")

        decision = self._router.route(str(SPOOL / "cap_signoff_pending_qa"))

        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "qa")


# --------------------------------------------------------------------------- #
# the QA worker requests it
# --------------------------------------------------------------------------- #


class QARequestsTheSignoffTests(unittest.TestCase):
    """Routing to design is where the ask is created — and only there."""

    def setUp(self) -> None:
        self._created: list[str] = []

    def tearDown(self) -> None:
        for capsule_id in self._created:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
        _restore_engine_config()

    def _run_qa(self, capsule_id: str) -> dict:
        manifest = _manifest(capsule_id, State.PENDING_QA)
        capsule_dir = _place(manifest)
        self._created.append(capsule_id)
        (capsule_dir / "image_prompt.txt").write_text(
            "A tidy desk with a laptop, soft daylight, no text anywhere.",
            encoding="utf-8",
        )
        # The reviewer is patched so this test is about the sign-off the worker
        # records, not about whether the copy passes review.
        with mock.patch(
            "workers.qa.review_copy",
            return_value=(True, "the copy reads well and carries no banned vocabulary"),
        ):
            return QAWorker(gateway=_PassingGateway()).run(str(capsule_dir))

    def test_the_ask_is_recorded_with_the_copy_digest_bound(self) -> None:
        _write_policy(enabled=True, domains=["marketing"])

        result = self._run_qa("cap_signoff_qa_asks")

        self.assertEqual(result["state"], State.PENDING_DESIGN.value)
        record = load_manifest(SPOOL / "cap_signoff_qa_asks")["metadata"][
            "copy_signoff"
        ]
        self.assertEqual(record["decision"], "awaiting")
        self.assertEqual(
            record["copy_sha256"],
            hashlib.sha256(COPY_TEXT.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(record["requested_by"], "qa")

    def test_no_ask_is_recorded_when_the_domain_does_not_require_one(self) -> None:
        _write_policy(enabled=False, domains=["marketing"])

        result = self._run_qa("cap_signoff_qa_silent")

        self.assertEqual(result["state"], State.PENDING_DESIGN.value)
        metadata = load_manifest(SPOOL / "cap_signoff_qa_silent").get("metadata") or {}
        self.assertNotIn("copy_signoff", metadata)


# --------------------------------------------------------------------------- #
# the decision route
# --------------------------------------------------------------------------- #


class CopySignoffDecisionRouteTests(unittest.TestCase):
    """The route records the decision, guards it, and moves the capsule on."""

    def setUp(self) -> None:
        self._created: list[str] = []
        _write_policy(enabled=True, domains=["marketing"])
        # A pulse would try to run the very stage this test is holding back.
        self._pulse = mock.patch.object(api, "run_pulse", return_value={})
        self._pulse.start()

    def tearDown(self) -> None:
        self._pulse.stop()
        for capsule_id in self._created:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
        _restore_engine_config()

    def _awaiting(self, capsule_id: str, *, copy_text: str = COPY_TEXT) -> Path:
        """A ``pending_design`` capsule with the ask recorded, as QA leaves it."""
        digest = hashlib.sha256(copy_text.encode("utf-8")).hexdigest()
        manifest = _manifest(
            capsule_id,
            State.PENDING_DESIGN,
            metadata={
                "copy_signoff": {
                    "decision": "awaiting",
                    "requested_at": "2026-10-17T00:00:00+00:00",
                    "requested_by": "qa",
                    "copy_sha256": digest,
                }
            },
        )
        capsule_dir = _place(manifest, copy_text)
        self._created.append(capsule_id)
        return capsule_dir

    @staticmethod
    def _decide(capsule_id: str, **body) -> dict:
        """Call the decision route the way the API layer would."""
        return asyncio.run(
            api.decide_copy_signoff(
                capsule_id, api.CopySignoffRequest(**body), _request()
            )
        )

    def test_approving_records_the_decision_and_keeps_the_capsule_ready(self) -> None:
        capsule_dir = self._awaiting("cap_signoff_approve")

        result = self._decide("cap_signoff_approve", decision="approved")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["decision"], "approved")
        # Approval releases the hold without moving the capsule: the design
        # stage is the router's job on the next pass, not the route's.
        manifest = load_manifest(capsule_dir)
        self.assertEqual(manifest["state"], State.PENDING_DESIGN.value)
        record = manifest["metadata"]["copy_signoff"]
        self.assertEqual(record["decision"], "approved")
        self.assertTrue(record["decided_by"])
        self.assertTrue(record["decided_at"])
        # The decision is on the audit trail, not only in metadata.
        self.assertTrue(
            any("copy signed off" in entry.get("why", "") for entry in manifest["history"]),
            manifest["history"],
        )

    def test_the_capsule_then_reaches_the_design_stage(self) -> None:
        """The recorded approval is what the router was waiting for."""
        capsule_dir = self._awaiting("cap_signoff_approve_routes")

        self._decide("cap_signoff_approve_routes", decision="approved")

        decision = CapsuleRouter().route(str(capsule_dir))
        self.assertIs(decision.action, Action.EXECUTE)
        self.assertEqual(decision.worker, "design")

    def test_rejecting_returns_the_capsule_to_the_author_with_the_reason(self) -> None:
        capsule_dir = self._awaiting("cap_signoff_reject")

        result = self._decide(
            "cap_signoff_reject", decision="rejected", reason="the opening is weak"
        )

        self.assertEqual(result["decision"], "rejected")
        manifest = load_manifest(capsule_dir)
        self.assertEqual(manifest["state"], State.NEEDS_REVISION.value)
        record = manifest["metadata"]["copy_signoff"]
        self.assertEqual(record["decision"], "rejected")
        self.assertEqual(record["reason"], "the opening is weak")
        # The author reads feedback.txt on the rework, exactly like a QA rejection.
        self.assertIn(
            "the opening is weak",
            (capsule_dir / "feedback.txt").read_text(encoding="utf-8"),
        )

    def test_a_rejection_without_a_reason_is_refused(self) -> None:
        self._awaiting("cap_signoff_no_reason")

        with self.assertRaises(HTTPException) as caught:
            self._decide("cap_signoff_no_reason", decision="rejected")

        self.assertEqual(caught.exception.status_code, 400)

    def test_an_unknown_decision_is_refused(self) -> None:
        self._awaiting("cap_signoff_bad_decision")

        with self.assertRaises(HTTPException) as caught:
            self._decide("cap_signoff_bad_decision", decision="maybe")

        self.assertEqual(caught.exception.status_code, 400)

    def test_a_copy_edited_after_the_ask_cannot_be_signed_off(self) -> None:
        """Anti-TOCTOU: the decision names the text that was actually read."""
        capsule_dir = self._awaiting("cap_signoff_toctou")
        (capsule_dir / COPY_FILE).write_text(
            "Rewritten after the ask, so nobody has read these words.\n",
            encoding="utf-8",
        )

        with self.assertRaises(HTTPException) as caught:
            self._decide("cap_signoff_toctou", decision="approved")

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            load_manifest(capsule_dir)["metadata"]["copy_signoff"]["decision"],
            "awaiting",
        )

    def test_a_capsule_that_owes_no_sign_off_is_refused(self) -> None:
        manifest = _manifest("cap_signoff_wrong_state", State.PENDING_APPROVAL)
        _place(manifest)
        self._created.append("cap_signoff_wrong_state")

        with self.assertRaises(HTTPException) as caught:
            self._decide("cap_signoff_wrong_state", decision="approved")

        self.assertEqual(caught.exception.status_code, 400)

    def test_a_second_decision_is_refused(self) -> None:
        """A recorded decision is final — the ask is consumed, not re-openable."""
        self._awaiting("cap_signoff_twice")
        self._decide("cap_signoff_twice", decision="approved")

        with self.assertRaises(HTTPException) as caught:
            self._decide("cap_signoff_twice", decision="approved")

        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
