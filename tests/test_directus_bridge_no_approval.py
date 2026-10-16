"""The Directus bridge must never approve, publish, or mutate a capsule.

An external Directus card is untrusted input. A card in ``scheduled`` /
``published`` status that names a capsule is, at most, a **request** that a
human approve that capsule through the normal path — the bridge holds no
approval authority. It records the request with one log line and leaves the
capsule byte-for-byte untouched: no state change, no manifest write, no
``PublishingWorker``, no write back to Directus.

This module proves that hermetically (a fake Directus, no network, the
bootstrap vault) and pins that CASE 1 — the engine -> Directus generation
mirror — is unchanged.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import SPOOL

from capsule_engine.manifest import State, new_manifest, save_manifest
from orchestration import directus_bridge


class _FakeDirectus:
    """A hermetic stand-in for the Directus client: records calls, sends nothing."""

    def __init__(self, posts):
        self.posts = list(posts)
        self.calls: list[tuple[str, str, object]] = []

    def request(self, path, method="GET", data=None):
        self.calls.append((method, path, data))
        if path.startswith("/items/posts"):
            return self.posts
        return {"id": 1}

    def upload_file(self, *_a, **_k):  # pragma: no cover - never expected here
        raise AssertionError("upload_file must not be called")


class DirectusBridgeTestCase(unittest.TestCase):
    """Vault-only fixture plumbing shared by both cases."""

    def setUp(self) -> None:
        # The bridge must be looking at the bootstrap vault, never real data/.
        self.assertEqual(directus_bridge.SPOOL_DIR, SPOOL.resolve())
        self._created: list[Path] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self._created:
            shutil.rmtree(path, ignore_errors=True)

    def _capsule(self, capsule_id: str, state: State) -> Path:
        path = SPOOL / capsule_id
        path.mkdir(parents=True, exist_ok=True)
        save_manifest(path, new_manifest(capsule_id, state=state))
        self._created.append(path)
        return path

    def _sweep(self, posts: list[dict]) -> tuple[_FakeDirectus, dict]:
        """Run one sync with a fake client and the spend pre-flight pinned off."""
        fake = _FakeDirectus(posts)
        with mock.patch.object(
            directus_bridge, "DirectusClient", lambda: fake
        ), mock.patch.object(
            directus_bridge.spend_control, "blocked_reason", return_value=None
        ):
            stats = directus_bridge.sync_directus_cycle()
        return fake, stats


class Case2NeverMutatesCapsules(DirectusBridgeTestCase):
    """A scheduled/published card is an inert request, not an approval."""

    def test_cards_leave_the_capsules_byte_for_byte_untouched(self) -> None:
        pending = self._capsule("directus_pending_1", State.PENDING_APPROVAL)
        progress = self._capsule("directus_progress_1", State.IN_PROGRESS)
        before = {
            path.name: (path / "manifest.json").read_bytes()
            for path in (pending, progress)
        }
        history_len = {
            path.name: len(directus_bridge.load_manifest(path)["history"])
            for path in (pending, progress)
        }

        posts = [
            {"id": 41, "status": "scheduled", "capsule_id": pending.name},
            {"id": 42, "status": "published", "capsule_id": progress.name},
        ]
        with self.assertLogs("directus-bridge", level="INFO") as captured:
            fake, stats = self._sweep(posts)

        # Manifest bytes are identical — not merely the state field.
        for path in (pending, progress):
            self.assertEqual(
                (path / "manifest.json").read_bytes(),
                before[path.name],
                f"manifest changed for {path.name}",
            )
            self.assertEqual(
                len(directus_bridge.load_manifest(path)["history"]),
                history_len[path.name],
                "a history entry was added",
            )

        # States are unchanged.
        self.assertEqual(
            directus_bridge.load_manifest(pending)["state"],
            State.PENDING_APPROVAL.value,
        )
        self.assertEqual(
            directus_bridge.load_manifest(progress)["state"],
            State.IN_PROGRESS.value,
        )

        # No publisher exists in the module, nothing was published.
        self.assertFalse(
            hasattr(directus_bridge, "PublishingWorker"),
            "the bridge must not be able to construct a PublishingWorker",
        )
        self.assertEqual(stats, {"produced": 0, "published": 0, "errors": 0})

        # Exactly one Directus read, and no write back to Directus.
        self.assertEqual(
            fake.calls, [("GET", "/items/posts?limit=100", None)]
        )

        # The request is recorded: one line naming the card, capsule and state.
        joined = "\n".join(captured.output)
        self.assertIn("directus approval request ignored", joined)
        self.assertIn(pending.name, joined)
        self.assertIn(progress.name, joined)
        self.assertIn("state_seen=pending_approval", joined)
        self.assertIn("state_seen=in_progress", joined)

    def test_a_card_for_an_unrelated_state_is_silent(self) -> None:
        """A card naming a capsule that is not at the gate does nothing at all."""
        approved = self._capsule("directus_approved_1", State.APPROVED)
        before = (approved / "manifest.json").read_bytes()

        with mock.patch.object(directus_bridge, "logger") as log:
            fake, stats = self._sweep(
                [{"id": 43, "status": "scheduled", "capsule_id": approved.name}]
            )

        self.assertEqual((approved / "manifest.json").read_bytes(), before)
        self.assertEqual(stats, {"produced": 0, "published": 0, "errors": 0})
        logged = " ".join(str(call) for call in log.info.call_args_list)
        self.assertNotIn("directus approval request ignored", logged)


class Case1PipelineUnchanged(DirectusBridgeTestCase):
    """CASE 1 still drives the generation pipeline and mirrors to Directus."""

    def test_approved_for_generation_runs_the_pipeline(self) -> None:
        mkt = mock.MagicMock(name="MarketingWorker")
        qa = mock.MagicMock(name="QAWorker")
        dsn = mock.MagicMock(name="DesignWorker")

        def _assemble(cid, _spec):
            return new_manifest(cid)

        posts = [{"id": 7, "status": "approved_for_generation", "topic": "Launch"}]
        with mock.patch.object(
            directus_bridge, "MarketingWorker", mkt
        ), mock.patch.object(
            directus_bridge, "QAWorker", qa
        ), mock.patch.object(
            directus_bridge, "DesignWorker", dsn
        ), mock.patch.object(
            directus_bridge, "assemble_post_capsule_manifest", side_effect=_assemble
        ):
            fake, stats = self._sweep(posts)

        self.assertEqual(stats["produced"], 1)
        self.assertEqual(stats["errors"], 0)

        # The four pipeline stages ran (marketing, qa x2, design).
        self.assertEqual(mkt.return_value.run.call_count, 1)
        self.assertEqual(qa.return_value.run.call_count, 2)
        self.assertEqual(dsn.return_value.run.call_count, 1)

        # The card was mirrored to 'under_review' with the new capsule id.
        patches = [call for call in fake.calls if call[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        _, path, payload = patches[0]
        self.assertEqual(path, "/items/posts/7")
        self.assertEqual(payload["status"], "under_review")
        capsule_id = payload["capsule_id"]
        self.assertTrue(capsule_id.startswith("cap_post_"))

        # The generated capsule is a vault artifact — clean it up.
        created = SPOOL / capsule_id
        if created.exists():
            self._created.append(created)


if __name__ == "__main__":
    unittest.main()
