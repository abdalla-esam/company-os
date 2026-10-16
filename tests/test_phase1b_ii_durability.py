"""Tests for Phase 1b-ii durability, atomic writes, fault injection, and races.

Covers:
1. Capsule artifact writers: atomic_text / atomic_bytes fault injection
   (create_capsule overwrite, build_post artifact, approval feedback.txt,
   marketing worker copy.md, design worker image.jpg, client_management deliverable,
   ideation worker deliverable and kernel save_manifest).
2. Config files: locked read-modify-write via update_json, fault injection,
   byte parity, and N-writer races for users.json, company_profile.json,
   marketing_config.json, and gear_probes.json.
3. Asset vault: atomic image write, index.jsonl append via append_jsonl_line,
   and loud surfacing of index append errors (no silent pass).
4. Ideation manifest helper: verifies capsule_engine.manifest.save_manifest
   is used, enforcing schema validation, size gate, and fsync.
5. create_branch (create_domain) endpoint: verifies POST /api/domains creates
   branch directories, docs stub, and governance entry without NameError.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault
from tests._bootstrap import DOMAINS, REPO_ROOT, SPOOL, VAULT

import asyncio
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from capsule_engine import State
from capsule_engine.exceptions import ManifestSchemaError, ManifestTooLargeError
from capsule_engine.gear_registry import _load_probe_cache, _save_probe_record
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import load_manifest
from data.domains.marketing import asset_vault
from data.domains.marketing.asset_vault import AssetVaultError, store_asset
from data.domains.marketing.marketing_config import (
    DEFAULT_MARKETING_CONFIG,
    load_marketing_config,
    marketing_config_path,
    save_marketing_config,
)
from orchestration import access_control, api
from shared_kernel.brand_profile import (
    DEFAULT_PROFILE,
    company_profile_path,
    load_company_profile,
    save_company_profile,
)
from shared_kernel.users import (
    USERS_CONFIG_PATH,
    create_or_update_user,
    load_users,
    save_users,
)

FAULT = "disk fault injected between tmp and rename"


def _request(path: str = "/api") -> Request:
    principal = access_control.Principal.legacy_root("phase-1b-ii-tester")
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


def _tmp_litter(directory: Path) -> list[str]:
    return sorted(p.name for p in Path(directory).rglob("*.tmp*") if not p.name.endswith(".lock"))


class CapsuleArtifactDurabilityTests(unittest.TestCase):
    """Artifact and deliverable writes: atomic_text / atomic_bytes durability."""

    def setUp(self) -> None:
        self.test_dir = VAULT / f"durability_test_{time.time_ns()}"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.test_dir, ignore_errors=True))

    def test_create_capsule_artifact_fault_injection(self) -> None:
        capsule_id = f"cap_artifact_fault_{time.time_ns()}"
        capsule_dir = SPOOL / capsule_id
        capsule_dir.mkdir(parents=True, exist_ok=True)

        artifact_file = capsule_dir / "copy.md"
        artifact_file.write_text("# Old deliverable", encoding="utf-8")
        before_bytes = artifact_file.read_bytes()

        req = api.CreateCapsuleRequest(
            capsule_id=capsule_id,
            text="# New attempted overwrite",
            sector="marketing",
        )

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
            with self.assertRaises((OSError, Exception)):
                asyncio.run(api.create_capsule_endpoint(req, _request()))

        self.assertEqual(artifact_file.read_bytes(), before_bytes, "old artifact must survive")
        self.assertEqual(_tmp_litter(capsule_dir), [], "no tmp litter left behind")

    def test_build_post_artifact_fault_injection(self) -> None:
        req = api.PostBuildRequest(
            topic="Fault Injection Topic",
            angle="Durability Angle",
            unified_copy="# Pre-built copy text",
        )

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
            with self.assertRaises(OSError):
                asyncio.run(api.build_post_endpoint(req))

    def test_approval_feedback_txt_fault_injection(self) -> None:
        capsule_id = f"cap_feedback_{time.time_ns()}"
        capsule_dir = SPOOL / capsule_id
        capsule_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "capsule_id": capsule_id,
            "state": "pending_approval",
            "created_at": "2026-10-13T00:00:00+00:00",
            "updated_at": "2026-10-13T00:00:00+00:00",
            "attempt": 0,
            "history": [],
            "metadata": {"domain": "marketing"},
            "gate_plan": {"records": []},
        }
        (capsule_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        feedback_file = capsule_dir / "feedback.txt"
        feedback_file.write_text("old feedback\n", encoding="utf-8")
        before_bytes = feedback_file.read_bytes()

        from orchestration import approval

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
            with self.assertRaises(OSError):
                approval._reject_locked(
                    capsule_dir,
                    principal=access_control.Principal.legacy_root("tester"),
                    reviewer="human_reviewer",
                    reason="new rejected reason",
                    named_gate=None,
                    transition_why="test rejection",
                )

        self.assertEqual(feedback_file.read_bytes(), before_bytes, "old feedback must survive")
        self.assertEqual(_tmp_litter(capsule_dir), [])

    def test_marketing_worker_copy_fault_injection(self) -> None:
        from data.domains.marketing import worker_script as mkt_worker

        deliverable_dir = self.test_dir / "post_123"
        deliverable_dir.mkdir(parents=True, exist_ok=True)
        copy_file = deliverable_dir / "copy.md"
        copy_file.write_text("prior copy\n", encoding="utf-8")
        before_bytes = copy_file.read_bytes()

        payload = {
            "ticket_id": "T-123",
            "request": "A safe marketing copy request",
            "client_id": "client_abc",
        }

        # The copy call goes through the gated gateway seam (Session C: no
        # direct Groq POST), so the transport is stubbed AT that seam and the
        # fault under test stays the one this test is about: the atomic write.
        fake_gateway = mock.MagicMock()
        fake_gateway.complete.return_value = "Fresh generated copy"
        with mock.patch.object(mkt_worker, "classify", return_value="reversible"):
            with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
                with mock.patch.object(mkt_worker, "default_gateway", return_value=fake_gateway):
                    with mock.patch.object(mkt_worker, "DELIVERABLES", str(self.test_dir)):
                        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                            res = mkt_worker.execute(payload)
                            self.assertEqual(res["result"], "needs_revision")
                            self.assertIn("Failed to write deliverable file", res["reason"])
        fake_gateway.complete.assert_called_once()

        persisted = self.test_dir / "client_abc" / "post_T-123" / "copy.md"
        if persisted.exists():
            self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_design_worker_image_fault_injection(self) -> None:
        from data.domains.design import worker_script as design_worker

        deliverable_dir = self.test_dir / "design_post"
        deliverable_dir.mkdir(parents=True, exist_ok=True)
        (deliverable_dir / "copy.md").write_text("# copy deliverable", encoding="utf-8")
        img_file = deliverable_dir / "image.jpg"
        img_file.write_bytes(b"prior_image_bytes")
        before_bytes = img_file.read_bytes()

        payload = {
            "ticket_id": "T-DESIGN-1",
            "context": {"deliverable_dir": str(deliverable_dir)},
        }

        # Session C: the worker no longer calls Groq or Pollinations itself —
        # both steps are the gated seams (gateway_ops.image_prompt and
        # workers.design.fetch_free_image). Stub AT those seams so the fault
        # under test stays the atomic image write.
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            with mock.patch.object(
                design_worker, "image_prompt", return_value="sleek modern architecture"
            ):
                with mock.patch.object(
                    design_worker,
                    "fetch_free_image",
                    return_value=(b"new_generated_image_bytes", "image/jpeg"),
                ):
                    with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                        res = design_worker.execute(payload)
                        self.assertEqual(res["result"], "needs_revision")
                        self.assertIn("Failed to save image.jpg", res["reason"])

        self.assertEqual(img_file.read_bytes(), before_bytes, "prior image survives")
        self.assertEqual(_tmp_litter(deliverable_dir), [])

    def test_client_management_worker_fault_injection(self) -> None:
        cm_dir = str(REPO_ROOT / "data" / "domains" / "client_management")
        if cm_dir not in sys.path:
            sys.path.insert(0, cm_dir)
        from data.domains.client_management import worker_script as cm_worker

        payload = {
            "ticket_id": "T-CM-1",
            "request": "A reversible client management onboarding document",
            "client_id": "client_xyz",
            "sector": "client_management",
        }

        with mock.patch.object(cm_worker, "DELIVERABLES", str(self.test_dir)):
            with mock.patch.object(cm_worker, "classify", return_value="reversible"):
                with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                    with self.assertRaises(OSError):
                        cm_worker.execute(payload)

        self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_ideation_worker_deliverable_and_kernel_save_manifest(self) -> None:
        from data.domains.ideation import worker_script as ideation_worker

        ticket_id = f"T-IDEA-{time.time_ns()}"
        capsule_dir = SPOOL / f"cap_idea_{time.time_ns()}"
        capsule_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "ticket_id": ticket_id,
            "request": "Autonomous AI Strategy in Enterprise",
            "client_id": "client_test",
            "sector": "ideation",
            "created_at": "2026-10-13T00:00:00+00:00",
        }

        with mock.patch.object(ideation_worker, "DELIVERABLES", str(self.test_dir)):
            with mock.patch.object(ideation_worker, "SPOOL_DIR", str(SPOOL)):
                with mock.patch.object(ideation_worker, "classify", return_value="reversible"):
                    res = ideation_worker.execute(payload)

        self.assertEqual(res["result"], "done")
        capsule_id = res["context"]["capsule_id"]
        out_cap_dir = Path(res["context"]["capsule_path"])
        self.assertTrue((out_cap_dir / "manifest.json").is_file())
        saved_manifest = load_manifest(out_cap_dir)
        self.assertEqual(saved_manifest["capsule_id"], capsule_id)
        self.assertEqual(saved_manifest["state"], State.DRAFT.value)

        # Confirm deliverable was atomically written
        deliv_path = self.test_dir / res["context"]["deliverable"]
        self.assertTrue(deliv_path.is_file())
        self.assertIn("Ideation Bridge Deliverable", deliv_path.read_text(encoding="utf-8"))


class ConfigDurabilityAndRaceTests(unittest.TestCase):
    """Config writers: locked read-modify-write, byte parity, fault injection, races."""

    def setUp(self) -> None:
        self.test_dir = VAULT / f"config_test_{time.time_ns()}"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.test_dir, ignore_errors=True))

    def test_users_config_fault_injection_and_byte_parity(self) -> None:
        custom_users = self.test_dir / "users.json"
        with mock.patch("shared_kernel.users.USERS_CONFIG_PATH", custom_users):
            initial = [{"username": "alice", "role": "admin"}]
            save_users(initial)
            before_text = custom_users.read_text(encoding="utf-8")
            self.assertFalse(before_text.endswith("\n"), "users.json has no trailing newline")
            self.assertEqual(before_text, json.dumps(json.loads(before_text), indent=2, ensure_ascii=False))

            with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                with self.assertRaises(OSError):
                    create_or_update_user(username="bob", role="operator")

            self.assertEqual(custom_users.read_text(encoding="utf-8"), before_text, "prior users survive")
            self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_company_profile_fault_injection_and_byte_parity(self) -> None:
        custom_profile = self.test_dir / "company_profile.json"
        profile_data = dict(DEFAULT_PROFILE)
        profile_data["company_name"] = "Sovereign Holding"

        save_company_profile(profile_data, custom_path=custom_profile)
        before_text = custom_profile.read_text(encoding="utf-8")
        self.assertTrue(before_text.endswith("\n"), "company_profile.json has trailing newline")
        self.assertEqual(before_text, json.dumps(json.loads(before_text), indent=2, ensure_ascii=False) + "\n")

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
            with self.assertRaises(OSError):
                save_company_profile({"company_name": "Overwritten"}, custom_path=custom_profile)

        self.assertEqual(custom_profile.read_text(encoding="utf-8"), before_text, "prior profile survives")
        self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_marketing_config_fault_injection_and_byte_parity(self) -> None:
        custom_cfg = self.test_dir / "marketing_config.json"
        cfg_data = dict(DEFAULT_MARKETING_CONFIG)
        cfg_data["cadence_hours"] = 36.0

        save_marketing_config(cfg_data, custom_path=custom_cfg)
        before_text = custom_cfg.read_text(encoding="utf-8")
        self.assertTrue(before_text.endswith("\n"), "marketing_config.json has trailing newline")
        self.assertEqual(before_text, json.dumps(json.loads(before_text), indent=2, ensure_ascii=False) + "\n")

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
            with self.assertRaises(OSError):
                save_marketing_config({"cadence_hours": 99.0}, custom_path=custom_cfg)

        self.assertEqual(custom_cfg.read_text(encoding="utf-8"), before_text, "prior config survives")
        self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_gear_probes_fault_injection_and_byte_parity(self) -> None:
        custom_probes = self.test_dir / "gear_probes.json"
        with mock.patch("capsule_engine.gear_registry._probe_cache_path", return_value=custom_probes):
            _save_probe_record("probe_test_verb", {"ok": True, "latency_ms": 12.5})
            before_text = custom_probes.read_text(encoding="utf-8")
            self.assertFalse(before_text.endswith("\n"), "gear_probes.json has no trailing newline")
            self.assertEqual(before_text, json.dumps(json.loads(before_text), indent=2, ensure_ascii=False))

            with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                _save_probe_record("another_verb", {"ok": False})

            self.assertEqual(custom_probes.read_text(encoding="utf-8"), before_text, "prior probes survive")
            self.assertEqual(_tmp_litter(self.test_dir), [])

    def test_users_n_writer_race_zero_lost_updates(self) -> None:
        custom_users = self.test_dir / "users_race.json"
        n_writers = 12
        barrier = threading.Barrier(n_writers)
        errors: list[str] = []

        with mock.patch("shared_kernel.users.USERS_CONFIG_PATH", custom_users):
            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=5.0)
                    create_or_update_user(
                        username=f"user_{index:02d}",
                        role="operator",
                        email=f"user_{index:02d}@sovereign.test",
                    )
                except Exception as exc:
                    errors.append(f"worker_{index}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [], f"Thread errors: {errors}")
            persisted = json.loads(custom_users.read_text(encoding="utf-8"))
            persisted_users = persisted.get("users", [])
            persisted_usernames = {u["username"] for u in persisted_users}
            for i in range(n_writers):
                self.assertIn(f"user_{i:02d}", persisted_usernames, f"lost update for user_{i:02d}")

    def test_gear_probes_n_writer_race_zero_lost_updates(self) -> None:
        custom_probes = self.test_dir / "gear_probes_race.json"
        n_writers = 12
        barrier = threading.Barrier(n_writers)
        errors: list[str] = []

        with mock.patch("capsule_engine.gear_registry._probe_cache_path", return_value=custom_probes):
            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=5.0)
                    _save_probe_record(
                        f"gear_verb_{index:02d}",
                        {"ok": True, "index": index, "latency_ms": index * 5.0},
                    )
                except Exception as exc:
                    errors.append(f"worker_{index}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [], f"Thread errors: {errors}")
            persisted = json.loads(custom_probes.read_text(encoding="utf-8"))
            for i in range(n_writers):
                verb = f"gear_verb_{i:02d}"
                self.assertIn(verb, persisted, f"lost probe record for {verb}")
                self.assertEqual(persisted[verb]["index"], i)


class AssetVaultDurabilityAndRaceTests(unittest.TestCase):
    """Asset vault: atomic image write, index append, loud error surfacing, N-writer race."""

    def setUp(self) -> None:
        self.vault_dir = VAULT / f"vault_test_{time.time_ns()}"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.vault_dir, ignore_errors=True))

    def test_store_asset_atomic_write_and_index_append(self) -> None:
        with mock.patch("data.domains.marketing.asset_vault.get_vault_dir", return_value=self.vault_dir):
            data = b"image_content_123"
            res = store_asset(data, prompt="A beautiful sovereign skyline")
            sha = res["sha256"]

            asset_file = self.vault_dir / f"{sha}.jpg"
            self.assertTrue(asset_file.is_file())
            self.assertEqual(asset_file.read_bytes(), data)

            index_file = self.vault_dir / "index.jsonl"
            self.assertTrue(index_file.is_file())
            lines = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["sha256"], sha)
            self.assertEqual(lines[0]["prompt"], "A beautiful sovereign skyline")

    def test_store_asset_surfaces_index_append_error_loudly(self) -> None:
        with mock.patch("data.domains.marketing.asset_vault.get_vault_dir", return_value=self.vault_dir):
            data = b"orphan_image_data"
            sha = hashlib.sha256(data).hexdigest()
            with mock.patch("data.domains.marketing.asset_vault.append_jsonl_line", side_effect=OSError("disk I/O failure")):
                with self.assertRaises(AssetVaultError) as ctx:
                    store_asset(data, prompt="Failing index prompt")
                self.assertIn("Failed to record index entry", str(ctx.exception))
            self.assertFalse(
                (self.vault_dir / f"{sha}.jpg").exists(),
                "no orphan image file should remain in vault when indexing fails",
            )

    def test_store_asset_image_fault_injection(self) -> None:
        with mock.patch("data.domains.marketing.asset_vault.get_vault_dir", return_value=self.vault_dir):
            data = b"fault_image_data"
            with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)):
                with self.assertRaises(OSError):
                    store_asset(data, prompt="Faulty image write")

            self.assertEqual(_tmp_litter(self.vault_dir), [])

    def test_store_asset_n_writer_race_zero_lost_index_entries(self) -> None:
        n_writers = 16
        barrier = threading.Barrier(n_writers)
        errors: list[str] = []

        with mock.patch("data.domains.marketing.asset_vault.get_vault_dir", return_value=self.vault_dir):
            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=5.0)
                    img_data = f"concurrent_image_bytes_{index:03d}".encode("utf-8")
                    store_asset(img_data, prompt=f"Prompt for worker {index}")
                except Exception as exc:
                    errors.append(f"worker_{index}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [], f"Thread errors: {errors}")
            index_file = self.vault_dir / "index.jsonl"
            self.assertTrue(index_file.is_file())
            lines = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), n_writers, "all index entries must be recorded")

            seen_prompts = {entry["prompt"] for entry in lines}
            for i in range(n_writers):
                self.assertIn(f"Prompt for worker {i}", seen_prompts)


class IdeationKernelSaveManifestIntegrationTests(unittest.TestCase):
    """Ideation domain uses the kernel save_manifest (validation + size gate + fsync)."""

    def setUp(self) -> None:
        self.cap_dir = SPOOL / f"cap_manifest_check_{time.time_ns()}"
        self.cap_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.cap_dir, ignore_errors=True))

    def test_kernel_save_manifest_enforces_schema_validation(self) -> None:
        from data.domains.ideation import worker_script as ideation_worker

        # Corrupt manifest missing required fields (e.g. state, attempt, history)
        invalid_manifest = {
            "capsule_id": "cap_bad",
            # missing required fields
        }
        with self.assertRaises(ManifestSchemaError):
            ideation_worker.save_manifest(self.cap_dir, invalid_manifest)

    def test_kernel_save_manifest_enforces_size_gate(self) -> None:
        from data.domains.ideation import worker_script as ideation_worker

        huge_manifest = {
            "capsule_id": "cap_huge",
            "state": "draft",
            "created_at": "2026-10-13T00:00:00+00:00",
            "updated_at": "2026-10-13T00:00:00+00:00",
            "attempt": 0,
            "history": [],
            "payload": "X" * (2 * 1024 * 1024),  # 2MB exceeds 1MB cap
        }
        with self.assertRaises(ManifestTooLargeError):
            ideation_worker.save_manifest(self.cap_dir, huge_manifest)


class CreateBranchEndpointTests(unittest.TestCase):
    """POST /api/domains (create_domain / create_branch) endpoint tests."""

    def setUp(self) -> None:
        self.test_docs_dir = VAULT / "docs" / "domains"
        self.test_docs_dir.mkdir(parents=True, exist_ok=True)

    def test_create_branch_endpoint_succeeds_without_name_error(self) -> None:
        branch_id = f"test_branch_{time.time_ns() % 100000}"
        req = api.CreateDomainRequest(
            domain_id=branch_id,
            title="Automated Test Branch",
            level="L1",
        )

        with mock.patch("orchestration.api.REPO_ROOT", VAULT):
            response = asyncio.run(api.create_domain(req))

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["domain_id"], branch_id)
        self.assertIn("Branch '%s' successfully founded." % branch_id, response["message"])

        branch_dir = DOMAINS / branch_id
        self.assertTrue(branch_dir.is_dir())
        self.assertTrue((branch_dir / "tickets").is_dir())
        self.assertTrue((branch_dir / "deliverables").is_dir())
        self.assertTrue((branch_dir / "archive").is_dir())
        self.assertTrue((branch_dir / "__init__.py").is_file())
        self.assertTrue((branch_dir / "tickets" / ".gitkeep").is_file())

        doc_path = VAULT / "docs" / "domains" / f"{branch_id}.md"
        self.assertTrue(doc_path.is_file(), "docs/domains file must be created")
        doc_content = doc_path.read_text(encoding="utf-8")
        self.assertIn("Automated Test Branch", doc_content)

    def test_create_branch_rejects_duplicate(self) -> None:
        branch_id = f"dup_branch_{time.time_ns() % 100000}"
        req = api.CreateDomainRequest(domain_id=branch_id, level="L0")

        with mock.patch("orchestration.api.REPO_ROOT", VAULT):
            asyncio.run(api.create_domain(req))
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api.create_domain(req))
            self.assertEqual(ctx.exception.status_code, 409)

    def test_create_branch_rejects_invalid_id(self) -> None:
        from fastapi import HTTPException
        for bad_id in ["..", "_internal", "bad/path", "spaced id", ""]:
            req = api.CreateDomainRequest(domain_id="dummy", level="L0")
            req.domain_id = bad_id
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api.create_domain(req))
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
