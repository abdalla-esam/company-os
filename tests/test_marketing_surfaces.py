"""Marketing surface tests: affected endpoints + the design/typography path.

The marketing-code move out of ``capsule_engine`` changes *lazy imports* that
live inside endpoint and worker function bodies — nothing the behavioural
suite executed before. These tests call the real endpoint functions and run
the real design stage against a fixture capsule, so a stale import path fails
here instead of at the first request in production.

Covered surfaces:
- ``GET/POST /api/config/marketing`` (marketing_config);
- ``GET /api/pillars`` (pillars -> dashboard layer, company + occasions);
- ``POST /api/capsules/build_post`` (post_builder -> marketing worker);
- ``GET /api/capsules/{id}/image`` and ``.../preview`` (company profile,
  image + deliverables projection);
- ``POST /api/tools/qa_inspect`` and ``/api/tools/prompt_distill``
  (gateway review heuristics / image-prompt distillation);
- the design stage's typography banner import, observed through a spy.

All fixtures live in the shared bootstrap vault and are removed in
``tearDownClass`` so the suite can keep sharing one vault.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import contextlib
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._bootstrap import DOMAINS, SPOOL, VAULT

#: The typography module now lives in the marketing package; the design stage
#: imports it lazily, so this test fails *here* if the path ever drifts.
TYPOGRAPHY_MODULE = "data.domains.marketing.typography"

SURFACE_ID = "C-SURFACE-TEST"
NO_IMAGE_ID = "C-SURFACE-NOIMG"
DESIGN_ID = "C-SURFACE-DESIGN"

ARABIC_HEADLINE = "خبر سيادي عاجل"
SURFACE_COPY = "# Sovereign Platforms\n\nThe system pipeline and data architecture scale.\n"
VISUAL_PROMPT = (
    "sleek executive command center with holographic system architectures, "
    "glowing fiber-optic data streams, minimalist luxury glass and steel"
)


def _image_bytes(width: int = 320, height: int = 240) -> bytes:
    """A real JPEG when PIL is available; otherwise a JPEG-magic stub."""
    try:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (width, height), (12, 40, 80)).save(
            buffer, format="JPEG", quality=85
        )
        return buffer.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _manifest(capsule_id: str, state: str, *, domain: str = "marketing") -> dict:
    return {
        "capsule_id": capsule_id,
        "state": state,
        "created_at": "2026-10-13T09:00:00+00:00",
        "updated_at": "2026-10-13T09:00:00+00:00",
        "attempt": 0,
        "history": [],
        "domain": domain,
        "raw_prompt": SURFACE_COPY,
        "metadata": {"domain": domain, "topic": capsule_id, "target_platform": "linkedin"},
        "channels": ["linkedin"],
    }


def _write_surface_capsule() -> None:
    """A ready-to-review capsule: copy, prompt, image and deliverables tree."""
    capsule = SPOOL / SURFACE_ID
    capsule.mkdir(parents=True, exist_ok=True)
    (capsule / "manifest.json").write_text(
        json.dumps(_manifest(SURFACE_ID, "pending_approval")), encoding="utf-8"
    )
    (capsule / "copy.md").write_text(SURFACE_COPY, encoding="utf-8")
    (capsule / "image_prompt.txt").write_text(VISUAL_PROMPT, encoding="utf-8")
    (capsule / "image.jpg").write_bytes(_image_bytes())
    deliverable = capsule / "deliverables" / "linkedin"
    deliverable.mkdir(parents=True, exist_ok=True)
    (deliverable / "copy.md").write_text(SURFACE_COPY, encoding="utf-8")
    (deliverable / "image.jpg").write_bytes(_image_bytes(200, 200))
    no_image = SPOOL / NO_IMAGE_ID
    no_image.mkdir(parents=True, exist_ok=True)
    (no_image / "manifest.json").write_text(
        json.dumps(_manifest(NO_IMAGE_ID, "draft")), encoding="utf-8"
    )
    (no_image / "copy.md").write_text(SURFACE_COPY, encoding="utf-8")


@contextlib.contextmanager
def _profile(document: dict):
    """Point the company-profile loader at a temporary document."""
    handle, path = tempfile.mkstemp(prefix="company_profile_", suffix=".json")
    try:
        Path(path).write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        previous = os.environ.get("COMPANY_PROFILE_PATH")
        os.environ["COMPANY_PROFILE_PATH"] = path
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("COMPANY_PROFILE_PATH", None)
            else:
                os.environ["COMPANY_PROFILE_PATH"] = previous
    finally:
        os.close(handle)
        Path(path).unlink(missing_ok=True)


def _has_pillow() -> bool:
    """Whether Pillow — and only Pillow — is importable in this interpreter."""
    try:
        import PIL  # noqa: F401

        return True
    except Exception:
        return False


def _write_design_capsule() -> Path:
    """A capsule at ``pending_design`` whose copy headline is Arabic."""
    capsule = SPOOL / DESIGN_ID
    capsule.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(DESIGN_ID, "pending_design")
    manifest["routing_context"] = {
        "domain": "marketing",
        "flags": {"requires_copy": True, "requires_image": True, "requires_qa": True},
    }
    (capsule / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (capsule / "copy.md").write_text(
        f"# {ARABIC_HEADLINE}\n\n{SURFACE_COPY}", encoding="utf-8"
    )
    (capsule / "image_prompt.txt").write_text(VISUAL_PROMPT, encoding="utf-8")
    return capsule


def _remove(*capsule_ids: str) -> None:
    for capsule_id in capsule_ids:
        shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)


def _json_request(payload: dict):
    """A minimal starlette POST request carrying ``payload`` as its JSON body."""
    from starlette.requests import Request

    body = json.dumps(payload).encode("utf-8")
    state = {"received": False}

    async def receive():
        if state["received"]:
            return {"type": "http.disconnect"}
        state["received"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/config/marketing",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive,
    )


def setUpModule():
    """Create the fixtures only when this module's tests are about to run.

    unittest discovery imports every test module *before* running any test,
    so a module-level write would leak into the dashboard tests' shared-vault
    counts.
    """
    _write_surface_capsule()
    _write_design_capsule()


from orchestration import api  # noqa: E402  — env must be set first
from orchestration import read_index  # noqa: E402


class MarketingConfigEndpoint(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("MARKETING_CONFIG_PATH")
        handle, self._config_path = tempfile.mkstemp(
            prefix="marketing_config_", suffix=".json"
        )
        os.close(handle)
        os.environ["MARKETING_CONFIG_PATH"] = self._config_path

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("MARKETING_CONFIG_PATH", None)
        else:
            os.environ["MARKETING_CONFIG_PATH"] = self._previous
        Path(self._config_path).unlink(missing_ok=True)

    def test_get_returns_defaults_when_no_file_exists(self):
        config = asyncio.run(api.get_marketing_config_endpoint())
        self.assertEqual(config["cadence_hours"], 48.0)
        self.assertIn("linkedin", config["enabled_channels"])
        self.assertIn("budget_guard", config)

    def test_post_round_trips_through_disk(self):
        response = asyncio.run(
            api.update_marketing_config_endpoint(
                _json_request({"cadence_hours": 12.0, "enabled_channels": ["x"]})
            )
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["config"]["cadence_hours"], 12.0)
        persisted = json.loads(Path(self._config_path).read_text(encoding="utf-8"))
        self.assertEqual(persisted["cadence_hours"], 12.0)
        self.assertEqual(persisted["enabled_channels"], ["x"])


class PillarsEndpoint(unittest.TestCase):
    def test_four_executive_pillars_render(self):
        response = asyncio.run(api.get_pillars_endpoint())
        self.assertEqual(response["status"], "success")
        pillars = response["pillars"]
        self.assertEqual(len(pillars), 4)
        ids = {pillar["id"] for pillar in pillars}
        self.assertIn("pillar_content_marketing", ids)
        marketing = next(p for p in pillars if p["id"] == "pillar_content_marketing")
        self.assertTrue(marketing["metrics"])
        self.assertTrue(marketing["action_label_en"])


class BuildPostEndpoint(unittest.TestCase):
    def test_build_post_writes_capsule_and_tickets(self):
        request = api.PostBuildRequest(
            topic="Autonomous Logistics",
            angle="Sovereign engineering",
            headline="Autonomous Logistics",
        )
        response = asyncio.run(api.build_post_endpoint(request))
        capsule_id = response["capsule_id"]
        tickets = list(response["manifest"].get("tickets") or [])
        try:
            self.assertEqual(response["status"], "success")
            capsule_dir = SPOOL / capsule_id
            self.assertTrue((capsule_dir / "manifest.json").is_file())
            self.assertTrue((capsule_dir / "copy.md").is_file())
            manifest = json.loads(
                (capsule_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["domain"], "marketing")
            self.assertEqual(manifest["bundle_type"], "post_capsule")
            tickets_dir = DOMAINS / "marketing" / "tickets"
            for ticket_id in tickets:
                self.assertTrue(
                    (tickets_dir / f"ticket_{ticket_id}.json").is_file(),
                    f"ticket {ticket_id} not persisted",
                )
        finally:
            _remove(capsule_id)
            for ticket_id in tickets:
                (
                    DOMAINS / "marketing" / "tickets" / f"ticket_{ticket_id}.json"
                ).unlink(missing_ok=True)
            read_index.rebuild()

    def test_build_post_never_creates_dispatchable_tickets_invariant(self):
        """Invariant: no path creates both a capsule and a dispatchable paid ticket."""
        from orchestration.dispatcher import DISPATCHABLE

        request = api.PostBuildRequest(
            topic="Invariant Verification",
            angle="Zero Double Spend",
            headline="Invariant Verification Post",
        )
        response = asyncio.run(api.build_post_endpoint(request))
        capsule_id = response["capsule_id"]
        tickets = list(response["manifest"].get("tickets") or [])
        try:
            self.assertEqual(response["status"], "success")
            capsule_dir = SPOOL / capsule_id
            self.assertTrue((capsule_dir / "manifest.json").is_file())

            tickets_dir = DOMAINS / "marketing" / "tickets"
            for ticket_id in tickets:
                ticket_path = tickets_dir / f"ticket_{ticket_id}.json"
                self.assertTrue(ticket_path.is_file())
                data = json.loads(ticket_path.read_text(encoding="utf-8"))
                # Must NOT be in DISPATCHABLE (open, pending, needs_revision)
                self.assertNotIn(
                    data.get("state"),
                    DISPATCHABLE,
                    f"ticket {ticket_id} has dispatchable state {data.get('state')!r} — invariant broken",
                )
                self.assertEqual(data.get("state"), "settled")
                self.assertEqual(data.get("spool_capsule"), capsule_id)
        finally:
            _remove(capsule_id)
            for ticket_id in tickets:
                (
                    DOMAINS / "marketing" / "tickets" / f"ticket_{ticket_id}.json"
                ).unlink(missing_ok=True)
            read_index.rebuild()


class CapsuleDeliveryEndpoints(unittest.TestCase):
    def test_image_endpoint_serves_the_capsule_image(self):
        response = asyncio.run(api.get_capsule_image(SURFACE_ID))
        self.assertEqual(
            Path(getattr(response, "path")).name, "image.jpg"
        )

    def test_image_endpoint_404s_without_an_image(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(api.get_capsule_image(NO_IMAGE_ID))
        self.assertEqual(caught.exception.status_code, 404)

    def test_preview_returns_channels_copy_image_and_brand_dna(self):
        preview = asyncio.run(api.preview_capsule_delivery(SURFACE_ID))
        self.assertEqual(preview["capsule_id"], SURFACE_ID)
        self.assertFalse(preview["ready_for_dispatch"])  # not approved
        channels = {
            entry["channel"]: entry for entry in preview["content"]["channels"]
        }
        self.assertIn("linkedin", channels)
        self.assertIn("The system pipeline", channels["linkedin"]["copy"])
        self.assertTrue(channels["linkedin"]["image"]["data_uri"])
        self.assertEqual(
            preview["brand_dna"]["profile_source"], "config/company_profile.json"
        )
        self.assertIn("The system pipeline", channels["linkedin"]["copy"])


class ReviewAndPromptTools(unittest.TestCase):
    def test_qa_inspect_uses_the_shared_vocabulary(self):
        text = (
            "Our system pipeline architecture and data engine scale for every "
            "enterprise workflow with measurable throughput."
        )
        response = asyncio.run(api.qa_inspect_endpoint(api.QAInspectRequest(text=text)))
        self.assertEqual(response["status"], "success")
        self.assertTrue(response["passed"], response["feedback"])

    def test_prompt_distill_returns_a_compliant_offline_prompt(self):
        response = asyncio.run(
            api.prompt_distill_endpoint(
                api.PromptDistillRequest(text="Sovereign command center, global data.")
            )
        )
        self.assertEqual(response["status"], "success")
        self.assertTrue(response["compliant"])
        self.assertIn("photorealistic", response["distilled_prompt"])


class TypographyAndDesignPath(unittest.TestCase):
    def _typography(self):
        return importlib.import_module(TYPOGRAPHY_MODULE)

    def test_overlay_banner_renders_or_fails_safe(self):
        module = self._typography()
        image = _image_bytes()
        output, applied = module.overlay_arabic_banner(image, ARABIC_HEADLINE)
        self.assertIsInstance(output, bytes)
        if module.PIL_AVAILABLE:
            self.assertTrue(applied)
            self.assertNotEqual(output, image)
        else:  # pragma: no cover - pillow is a declared requirement
            self.assertFalse(applied)
            self.assertEqual(output, image)

    def test_brand_bar_covers_the_foot_of_the_image(self):
        """The strip the free provider stamps is the company's strip afterwards."""
        module = self._typography()
        if not _has_pillow():
            self.skipTest("pillow unavailable; the bar is a no-op")
        from PIL import Image

        image = _image_bytes()
        output, applied = module.overlay_brand_bar(image, "Fllevo", "https://fllevo.dev")
        self.assertTrue(applied)
        self.assertNotEqual(output, image)

        shot = Image.open(io.BytesIO(output)).convert("RGB")
        width, height = shot.size
        before = Image.open(io.BytesIO(image)).convert("RGB")
        # The bottom-right corner is where a provider watermark sits; it is the
        # bar's colour now, not the photograph's.
        corner = shot.getpixel((width - 4, height - 4))
        self.assertLess(sum(corner), sum(before.getpixel((width - 4, height - 4))))

    def test_brand_bar_does_not_wait_on_the_arabic_stack(self):
        """The bar draws no Arabic, so it must not inherit ``PIL_AVAILABLE``.

        That flag also requires ``arabic_reshaper``/``bidi``, which the
        production venv has neither of — so the banner it guards has never
        rendered an image there, silently, by its own fail-safe. This bar is
        what covers the provider's stamp, so Pillow alone has to be enough.
        """
        module = self._typography()
        if not _has_pillow():
            self.skipTest("pillow unavailable; the bar is a no-op")
        image = _image_bytes()
        was = module.PIL_AVAILABLE
        module.PIL_AVAILABLE = False
        try:
            output, applied = module.overlay_brand_bar(
                image, "Fllevo", "https://fllevo.dev"
            )
        finally:
            module.PIL_AVAILABLE = was
        self.assertTrue(applied)
        self.assertNotEqual(output, image)

    def test_brand_bar_takes_its_words_from_the_company_profile(self):
        """A rebrand has to reach the image without anyone touching code."""
        module = self._typography()
        if not _has_pillow():
            self.skipTest("pillow unavailable; the bar is a no-op")
        image = _image_bytes()
        document = {
            "schema_version": 1,
            "company_name": "Acme",
            "brand_name": "Acme",
            "website": "https://acme.example",
        }
        with _profile(document):
            from_profile, applied = module.overlay_brand_bar(image)
        explicit, applied_explicit = module.overlay_brand_bar(
            image, "Acme", "https://acme.example"
        )
        self.assertTrue(applied)
        self.assertTrue(applied_explicit)
        # Same bytes means the profile supplied exactly those two words.
        self.assertEqual(from_profile, explicit)

    def test_brand_bar_fails_safe(self):
        module = self._typography()
        # A payload that is not a readable image comes back untouched.
        stub = b"\xff\xd8\xff\xe0" + b"\x00" * 64
        output, applied = module.overlay_brand_bar(stub, "Fllevo", "https://fllevo.dev")
        self.assertEqual(output, stub)
        self.assertFalse(applied)
        # And with nothing to write, the image is left exactly as it came.
        image = _image_bytes()
        output, applied = module.overlay_brand_bar(image, "", "")
        self.assertEqual(output, image)
        self.assertFalse(applied)

    def test_design_stage_applies_the_brand_bar(self):
        module = self._typography()
        if not _has_pillow():
            self.skipTest("pillow unavailable; the bar is a no-op")
        from capsule_engine.gateway import OfflineModelGateway
        from workers.design import DesignWorker

        capsule = _write_design_capsule()
        manifest = json.loads(
            (capsule / "manifest.json").read_text(encoding="utf-8")
        )
        real = module.overlay_brand_bar
        seen: list[tuple] = []

        def spy(image_bytes, *args, **kwargs):
            seen.append(args)
            return real(image_bytes, *args, **kwargs)

        import shared_kernel.paths as paths

        previous_vault = paths.ASSETS_VAULT
        paths.ASSETS_VAULT = VAULT / "assets_vault"
        module.overlay_brand_bar = spy
        try:
            worker = DesignWorker(
                gateway=OfflineModelGateway(),
                fetcher=lambda prompt, timeout: (_image_bytes(), "image/jpeg"),
            )
            result = worker.execute(str(capsule), manifest)
            image_written = (capsule / "image.jpg").is_file()
            state = result["state"]
        finally:
            module.overlay_brand_bar = real
            paths.ASSETS_VAULT = previous_vault
            _remove(DESIGN_ID)

        self.assertEqual(state, "pending_qa")
        self.assertTrue(image_written)
        self.assertEqual(len(seen), 1)

    def test_design_stage_reaches_the_typography_banner(self):
        module = self._typography()
        if not module.PIL_AVAILABLE:
            self.skipTest("pillow unavailable; the banner is a no-op")
        from capsule_engine.gateway import OfflineModelGateway
        from workers.design import DesignWorker

        capsule = _write_design_capsule()
        manifest = json.loads(
            (capsule / "manifest.json").read_text(encoding="utf-8")
        )
        real = module.overlay_arabic_banner
        seen: list[str] = []

        def spy(image_bytes, headline, *args, **kwargs):
            seen.append(headline)
            return real(image_bytes, headline, *args, **kwargs)

        import shared_kernel.paths as paths

        previous_vault = paths.ASSETS_VAULT
        paths.ASSETS_VAULT = VAULT / "assets_vault"
        module.overlay_arabic_banner = spy
        try:
            worker = DesignWorker(
                gateway=OfflineModelGateway(),
                fetcher=lambda prompt, timeout: (_image_bytes(), "image/jpeg"),
            )
            result = worker.execute(str(capsule), manifest)
            image_written = (capsule / "image.jpg").is_file()
            prompt_hash = result.get("metadata", {}).get("image_prompt_hash")
            state = result["state"]
        finally:
            module.overlay_arabic_banner = real
            paths.ASSETS_VAULT = previous_vault
            _remove(DESIGN_ID)

        self.assertEqual(state, "pending_qa")
        self.assertTrue(image_written)
        self.assertEqual(seen, [ARABIC_HEADLINE])
        self.assertTrue(prompt_hash)


def tearDownModule():
    """The fixture capsules must not leak into tests that run after this module."""
    _remove(SURFACE_ID, NO_IMAGE_ID, DESIGN_ID)
    read_index.rebuild()


if __name__ == "__main__":
    unittest.main()
