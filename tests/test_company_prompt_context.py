"""Company DNA reaches the prompts that write in the company's name.

Two stages generate text on the company's behalf — the ideator that proposes a
topic and the marketing worker that writes the post. Both used to run blind to
``config/company_profile.json``: the ideator described the *engine* ("Sovereign
Agency OS", anti-TOCTOU, autonomy ladders) and the finished post inherited that
frame, so a software business shipped its own architecture as marketing.

These tests pin the seam that closed that gap:
- :func:`shared_kernel.brand_profile.company_prompt_context` renders the profile
  (and renders *nothing* when there is no profile, so an un-configured
  deployment is unchanged);
- the copy prompt carries the identity plus the two-version rule;
- the ideator prompt carries the identity and no longer describes the engine.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared_kernel.brand_profile import company_prompt_context

from capsule_engine.gateway import OfflineModelGateway

from domains.ideation import auto_ideator
from domains.marketing import gateway_ops
from domains.marketing.gateway_ops import build_review_system_prompt, review_copy
from workers.marketing import COPY_OUTPUT_INSTRUCTIONS, brand_prompt_block


class CompanyProfilePath:
    """Point the profile loader at a temporary file for the duration of a test."""

    def __init__(self, testcase: unittest.TestCase, document):
        self._case = testcase
        self._document = document
        self._handle: int = -1
        self._path = ""

    def __enter__(self) -> str:
        handle, self._path = tempfile.mkstemp(prefix="company_profile_", suffix=".json")
        self._handle = handle
        if self._document is not None:
            Path(self._path).write_text(
                json.dumps(self._document, ensure_ascii=False), encoding="utf-8"
            )
        self._env = mock.patch.dict(
            os.environ, {"COMPANY_PROFILE_PATH": self._path}
        )
        self._env.start()
        return self._path

    def __exit__(self, *exc) -> None:
        self._env.stop()
        os.close(self._handle)
        Path(self._path).unlink(missing_ok=True)


class CompanyPromptContext(unittest.TestCase):
    def test_a_nameless_profile_renders_nothing(self):
        """An un-configured company must add no empty headings to a prompt."""
        self.assertEqual(company_prompt_context({}), "")
        self.assertEqual(company_prompt_context({"industry": "Software"}), "")

    def test_the_brand_name_leads_and_the_company_name_is_the_fallback(self):
        branded = company_prompt_context({"company_name": "Acme", "brand_name": "Acme.io"})
        self.assertIn("write as Acme.io, not about it", branded)
        self.assertIn("- Brand: Acme.io", branded)
        self.assertNotIn("Brand: Acme\n", branded)

        unbranded = company_prompt_context({"company_name": "Acme"})
        self.assertIn("write as Acme, not about it", unbranded)

    def test_only_filled_fields_render(self):
        block = company_prompt_context(
            {"company_name": "Acme", "industry": "Software", "mission": "", "vision": ""}
        )
        self.assertIn("- Industry: Software", block)
        self.assertNotIn("Mission", block)
        self.assertNotIn("Vision", block)
        # No blank value ever survives as an empty labelled line.
        for line in block.splitlines():
            self.assertNotEqual(line.strip(), "-")

    def test_lists_join_onto_one_line(self):
        block = company_prompt_context(
            {
                "company_name": "Acme",
                "values": ["Clarity", "Speed"],
                "primary_channels": ["linkedin", "x"],
            }
        )
        self.assertIn("- Values: Clarity، Speed", block)
        self.assertIn("- Channels: linkedin، x", block)

    def test_services_come_from_the_documented_extension_point(self):
        block = company_prompt_context(
            {
                "company_name": "Acme",
                "custom_metadata": {"services": ["Websites", "Automation"]},
            }
        )
        self.assertIn("- Services: Websites، Automation", block)

    def test_a_malformed_extension_point_is_simply_absent(self):
        for bad in ({"custom_metadata": "not-a-mapping"}, {"custom_metadata": {"services": 7}}):
            block = company_prompt_context({"company_name": "Acme", **bad})
            self.assertNotIn("Services", block)


class TheCopyPromptCarriesTheCompany(unittest.TestCase):
    def test_no_profile_means_no_block(self):
        with CompanyProfilePath(self, None):
            self.assertEqual(brand_prompt_block(), "")

    def test_the_profile_adds_identity_and_the_two_version_rule(self):
        with CompanyProfilePath(self, {"schema_version": 1, "company_name": "Acme", "tone": "plain"}):
            block = brand_prompt_block()
        self.assertIn("write as Acme, not about it", block)
        self.assertIn("Tone: plain", block)
        self.assertIn(COPY_OUTPUT_INSTRUCTIONS, block)
        self.assertIn("TWO clearly separated versions", block)


class TheIdeatorSpeaksForTheCompany(unittest.TestCase):
    def test_the_company_profile_reaches_the_ideator_prompt(self):
        with CompanyProfilePath(
            self,
            {
                "schema_version": 1,
                "company_name": "Acme",
                "brand_name": "Acme",
                "industry": "Software & Digital Solutions",
                "tone": "plain and practical",
            },
        ):
            prompt = auto_ideator.build_ideator_system_prompt()

        self.assertIn("You are the content strategist for Acme", prompt)
        self.assertIn("write as Acme, not about it", prompt)
        self.assertIn("Industry: Software & Digital Solutions", prompt)

    def test_without_a_profile_the_ideator_still_describes_a_software_business(self):
        with CompanyProfilePath(self, None):
            prompt = auto_ideator.build_ideator_system_prompt()
        self.assertIn(auto_ideator.IDEATOR_FALLBACK_COMPANY, prompt)

    def test_the_ideator_never_describes_the_engine(self):
        """The regression this seam exists to prevent."""
        prompt = auto_ideator.build_ideator_system_prompt()
        for engine_self_description in (
            "Sovereign Agency OS",
            "Chief Strategy & Content Officer for an elite tech agency",
        ):
            self.assertNotIn(engine_self_description, prompt)

    def test_jargon_is_named_as_forbidden(self):
        prompt = auto_ideator.build_ideator_system_prompt()
        prohibitions = prompt.split("Strict prohibitions:")[1]
        for word in ("TOCTOU", "cryptography", "sovereign", "closed-loop"):
            self.assertIn(word, prohibitions)

    def test_every_fallback_pillar_is_simple_and_platform_tagged(self):
        """The offline path must stay on-brand, not only the LLM path."""
        self.assertTrue(auto_ideator.CORE_STRATEGIC_PILLARS)
        for pillar in auto_ideator.CORE_STRATEGIC_PILLARS:
            self.assertTrue(pillar["title"].strip())
            self.assertIn(pillar["platform"], ("linkedin", "facebook"))
            self.assertGreaterEqual(len(pillar["takeaways"]), 3)
            self.assertTrue(pillar["cta"].strip())


class TheReviewerJudgesInTheCompanysVoice(unittest.TestCase):
    """The QA rubric is the stage that actually stopped the first capsule.

    It demanded "technical authority" — at least two technical signal terms —
    because it too was written for the engine's own enterprise-architecture
    feed. A plain, practical post failed it three times and the capsule died at
    the revision cap. The threshold is gone; the company's voice replaced it.
    """

    def test_the_base_rubric_no_longer_demands_technical_authority(self):
        base = gateway_ops.REVIEW_SYSTEM_PROMPT
        self.assertNotIn("technical authority", base)
        self.assertNotIn("elite tech agency", base)
        self.assertIn("unexplained jargon is a defect", base)

    def test_the_base_rubric_tells_the_reviewer_not_to_invent_hits(self):
        """The reviewer once rejected a copy for a banned word 'implied' by it.

        The word was not in the text. The vocabulary is already enforced
        deterministically, so the prompt must forbid that guess explicitly.
        """
        base = gateway_ops.REVIEW_SYSTEM_PROMPT
        self.assertIn("never report a banned word as 'implied'", base)
        self.assertIn("never reject a rewrite request over a banned word", base)

    def test_no_profile_leaves_the_rubric_unchanged(self):
        with CompanyProfilePath(self, None):
            self.assertEqual(build_review_system_prompt(), gateway_ops.REVIEW_SYSTEM_PROMPT)

    def test_the_profile_reaches_the_reviewer(self):
        with CompanyProfilePath(
            self, {"schema_version": 1, "company_name": "Acme", "tone": "plain"}
        ):
            prompt = build_review_system_prompt()
        self.assertTrue(prompt.startswith(gateway_ops.REVIEW_SYSTEM_PROMPT))
        self.assertIn("write as Acme, not about it", prompt)
        self.assertIn("Tone: plain", prompt)

    def test_the_offline_review_passes_plain_non_technical_copy(self):
        copy = (
            "Your team retypes the same order into two systems every single day. "
            "Count those minutes for one week and multiply by the hourly rate, "
            "and you have the yearly cost of that one habit. Send us the task."
        )
        passed, feedback = review_copy(OfflineModelGateway(), copy)
        self.assertTrue(passed, feedback)

    def test_the_offline_review_still_rejects_slop(self):
        slop = "We leverage a seamless holistic paradigm. " * 3
        passed, feedback = review_copy(OfflineModelGateway(), slop)
        self.assertFalse(passed)
        self.assertIn("banned", feedback)

    def test_the_offline_review_still_rejects_a_too_short_copy(self):
        passed, feedback = review_copy(OfflineModelGateway(), "Too short to ship.")
        self.assertFalse(passed)
        self.assertIn("too short", feedback)


class TheVisualDirectorLooksAtTheReadersWorld(unittest.TestCase):
    """The image prompt used to describe the engine's own architecture.

    Any copy mentioning automation or data tripped an "is_tech" branch that
    asked for "holographic system architectures" and "glowing fiber-optic data
    streams" — which is how a post about data entry got a dark corridor. The
    branch still exists, because the copy often is about technology; what it
    asks for is now an ordinary desk.
    """

    def test_software_copy_asks_for_an_ordinary_workplace(self):
        prompt = gateway_ops.build_context_aware_visual_prompt(
            {"metadata": {"topic": "Stop paying for data entry"}},
            "أتمتة إدخال البيانات توفر وقت فريقك",
        )
        self.assertIn("workspace", prompt)
        for engine_imagery in ("holographic", "fiber-optic", "command center"):
            self.assertNotIn(engine_imagery, prompt)

    def test_the_no_context_default_is_not_sovereign_leadership(self):
        prompt = gateway_ops.build_context_aware_visual_prompt({}, "plain copy")
        self.assertNotIn("sovereign", prompt)
        self.assertIn("workplace", prompt)

    def test_the_director_prompt_no_longer_evokes_the_engine(self):
        self.assertNotIn("Sovereign Agency OS", gateway_ops.VISUAL_DIRECTOR_SYSTEM_PROMPT)
        self.assertNotIn("holographic", gateway_ops.VISUAL_DIRECTOR_SYSTEM_PROMPT)
        self.assertIn("ordinary modern workplace", gateway_ops.VISUAL_DIRECTOR_SYSTEM_PROMPT)

    def test_the_aesthetic_tail_is_still_appended_verbatim(self):
        prompt = gateway_ops.build_context_aware_visual_prompt({}, "plain copy")
        self.assertTrue(prompt.endswith(gateway_ops.IMAGE_AESTHETIC))


if __name__ == "__main__":
    unittest.main()
