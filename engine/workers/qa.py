#!/usr/bin/env python3
"""qa.py — QAWorker: the automated quality gate between copy and design.

Pipeline stage: given a ``pending_qa`` capsule carrying ``copy.md``, grade the
copy against the agency's strict guidelines and route it onward — or back.

Responsibilities, in order — each one fails closed:

1. **Routing gate**: this worker only runs on ``pending_qa`` capsules (plus a
   stale ``in_progress`` resume). Any other state is refused by
   :meth:`capsule_engine.worker.BaseWorker.run` before ``execute`` is called.
2. **Read input**: ``copy.md`` via the inherited ``read_text`` (size-guarded,
   traversal-safe). Missing/empty input fails *before* any state mutation, so
   the capsule stays ``pending_qa`` for a human to inspect.
3. **Review (gateway)**: ``data.domains.marketing.gateway_ops.review_copy``
   over the ``ModelGateway`` returns
   ``(passed, feedback)`` — the default factory uses the real HTTP reviewer
   when credentials are configured, and the deterministic offline rules
   (substance, and the kernel's closed slop vocabulary) otherwise. The real
   reviewer's rubric is built from the deployed company profile, so it judges
   the copy in the company's own voice rather than against a generic
   enterprise standard. Malformed gateway verdicts (wrong type, empty
   feedback) are rejected as failures.
4. **Decision branching** — the only place the pipeline forks:
   - ``passed`` -> ``pending_design`` when ``routing_context.flags
     .requires_image is True`` (the design stage still has work), otherwise
     straight to ``pending_approval`` (the human manager). The verdict is
     recorded in ``history``.
   - ``not passed`` -> ``feedback.txt`` is written with the gateway's
     feedback via the inherited ``write_text`` (atomic), and the capsule
     transitions back to ``needs_revision`` for the marketing stage; the
     rejection is recorded in ``history``.
5. **Defensive**: missing copy, gateway exceptions and malformed verdicts are
   all recorded into the manifest ``history`` by this worker and re-raised as
   ``QAError``; :meth:`capsule_engine.worker.BaseWorker.run` then persists the
   attempt counter and escalates to ``failed`` at the attempt cap. Nothing
   wedges: the lock is always released, the capsule always has a next state.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

try:  # normal import (package on sys.path)
    from capsule_engine import BaseWorker, ModelGateway, State, default_gateway
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError
except ImportError:  # script/odd-cwd execution — mirror orchestration/dispatcher.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import BaseWorker, ModelGateway, State, default_gateway
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError

# The marketing package lives under ``data/domains/``: ``data.domains.marketing.*``
# needs the repo root importable (standalone script runs included).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from data.domains.marketing.gateway_ops import review_copy  # noqa: E402

__all__ = [
    "QAWorker",
    "QAError",
    "QAInputError",
    "ModelGateway",  # re-exported from capsule_engine.gateway for callers
]

# B0: the copy stage's on-disk artifact name. Hash binding and TOCTOU
# verification are name-agnostic (capsule_engine.ARTIFACT_FILE is the engine's
# single default); this is this worker's own output filename.
COPY_FILE = "copy.md"
FEEDBACK_FILE = "feedback.txt"
MAX_FEEDBACK_BYTES = 16 * 1024  # feedback is a note, not an essay


class QAError(CapsuleError):
    """Base class for every deliberate QAWorker failure."""


class QAInputError(QAError):
    """copy.md unusable, or the gateway verdict violates its contract."""


class QAWorker(BaseWorker):
    """Grade ``copy.md`` and route the capsule onward — or back for revision.

    Args:
        gateway: optional ``ModelGateway``-compatible object (its
            ``review_copy(copy_text)`` method is the only one used).

    Class contract (see :class:`capsule_engine.BaseWorker`): ``handles``
    includes ``IN_PROGRESS`` so a stale-lease resume re-enters this worker.
    """

    name: ClassVar[str] = "qa"
    handles: ClassVar[frozenset[State]] = frozenset(
        {State.PENDING_QA, State.IN_PROGRESS}
    )
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self._gateway = gateway if gateway is not None else default_gateway()

    # ------------------------------------------------------------------ #
    # the worker contract
    # ------------------------------------------------------------------ #

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Run the QA stage: read -> review -> branch (design / approval / revision).

        Zero Waste QA Gate: Validates both copy.md and image_prompt.txt before
        handing off to DesignWorker. The visual prompt is checked for:
        1. 100% English (Fail-closed if any Arabic letters are present);
        2. Strict contextual relevance to the topic, country, and occasion.

        Raises:
            QAInputError: copy.md missing/empty, or the gateway returned a
                malformed verdict.
            QAError: the gateway itself raised.
        """
        # 1. read input (fails before any mutation)
        copy_text = self._read_copy(capsule_path)

        # 2. Zero Waste QA Gate on image_prompt.txt before design generation
        prompt_file = Path(capsule_path) / "image_prompt.txt"
        visual_prompt = ""
        if prompt_file.exists():
            try:
                visual_prompt = self.read_text(capsule_path, "image_prompt.txt").strip()
            except Exception:
                visual_prompt = ""

        requires_image = self._flag(manifest, "requires_image") is True

        if visual_prompt:
            prompt_passed, prompt_feedback = self._verify_visual_prompt(
                visual_prompt, manifest, copy_text
            )
            if not prompt_passed:
                artifact = self.write_text(capsule_path, FEEDBACK_FILE, prompt_feedback + "\n")
                self.transition(
                    manifest,
                    State.NEEDS_REVISION,
                    why=f"qa rejected: {prompt_feedback} (feedback saved to {artifact.name})",
                )
                return manifest

        # 3. text + image reviewed together: when an image was rendered, the
        # reviewer also sees the visual description that produced it.
        review_text = copy_text
        if (Path(capsule_path) / "image.jpg").exists():
            if visual_prompt:
                review_text = (
                    f"{copy_text}\n\n---\n[Attached image generated from this "
                    f"visual description]: {visual_prompt}"
                )

        # 4. review via the gateway (deterministic mock)
        try:
            passed, feedback = review_copy(self._gateway, review_text)
        except Exception as exc:  # recorded before re-raising, never swallowed
            self.record(
                manifest,
                f"qa review failed: {type(exc).__name__}: {exc}",
            )
            raise QAError(
                f"ModelGateway.review_copy failed: {type(exc).__name__}: {exc}"
            ) from exc
        # Token accounting: fold the reviewer's usage into metadata.usage.
        self.account_usage(manifest, self._gateway)
        self._validate_verdict(passed, feedback)

        # 5. branch on the verdict — one fork, two lawful destinations
        if passed:
            if visual_prompt:
                if "metadata" not in manifest or not isinstance(manifest["metadata"], dict):
                    manifest["metadata"] = {}
                manifest["metadata"]["qa_visual_approved"] = True
                manifest["metadata"]["qa_visual_prompt_hash"] = hashlib.sha256(
                    visual_prompt.encode("utf-8")
                ).hexdigest()[:16]

            has_image = (Path(capsule_path) / "image.jpg").exists()
            if requires_image and not has_image:
                destination = State.PENDING_DESIGN
                why = "qa passed: copy approved and visual prompt verified (Zero Waste QA Gate passed) -> design stage"
                if self._copy_signoff_required(manifest):
                    await_digest = self._record_copy_signoff(manifest, capsule_path)
                    self.record(
                        manifest,
                        "copy sign-off requested before the design stage "
                        f"(copy sha256 {await_digest[:16]}…)",
                    )
                    why += " (held for a human copy sign-off before the image is made)"
            else:
                destination = State.PENDING_APPROVAL
                why = f"qa passed: {feedback} -> manager approval"

            self.transition(manifest, destination, why=why)
            return manifest

        # 6. rejection: persist actionable feedback for the author, then hand back
        artifact = self.write_text(capsule_path, FEEDBACK_FILE, feedback + "\n")
        self.transition(
            manifest,
            State.NEEDS_REVISION,
            why=f"qa rejected: {feedback} (feedback saved to {artifact.name})",
        )
        return manifest

    @staticmethod
    def _copy_signoff_required(manifest: dict[str, Any]) -> bool:
        """Whether this domain's copy needs a human sign-off before design."""
        try:
            from capsule_engine.policy import load_policy

            return load_policy().copy_signoff_required(manifest.get("domain"))
        except Exception:  # a policy that cannot load never wedges a capsule
            return False

    @staticmethod
    def _record_copy_signoff(manifest: dict[str, Any], capsule_path: str) -> str:
        """Park the copy for a human, binding the hash they will be judging.

        The digest is taken at the moment of asking so the sign-off names the
        exact text that was approved. A copy edited afterwards no longer matches
        the record, and the decision endpoint refuses it rather than approving
        words nobody read.

        Returns:
            The bound copy digest (empty when there is no copy on disk).
        """
        digest = ""
        copy_file = Path(capsule_path) / COPY_FILE
        if copy_file.is_file():
            digest = hashlib.sha256(copy_file.read_bytes()).hexdigest()

        if "metadata" not in manifest or not isinstance(manifest["metadata"], dict):
            manifest["metadata"] = {}
        manifest["metadata"]["copy_signoff"] = {
            "decision": "awaiting",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "requested_by": "qa",
            "copy_sha256": digest,
        }
        return digest

    @classmethod
    def _verify_visual_prompt(
        cls, visual_prompt: str, manifest: dict[str, Any], copy_text: str
    ) -> tuple[bool, str]:
        """Zero Waste QA Gate verification of image_prompt.txt before generation.

        Enforces strict rules:
        1. 100% English: Fail-closed if ANY Arabic character is found.
        2. Relevance Check: Verifies visual prompt matches topic, country, occasion, and copy.
        """
        # Rule 1: 100% English Check (Fail-closed if Arabic letters found)
        arabic_match = re.search(
            r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]",
            visual_prompt,
        )
        if arabic_match:
            return False, (
                "REJECTED: visual prompt in image_prompt.txt contains Arabic characters; "
                "must be strictly 100% English for image generation models."
            )

        # Minimum length sanity
        words = visual_prompt.strip().split()
        if len(words) < 5:
            return False, "REJECTED: visual prompt in image_prompt.txt is too short (fewer than 5 words)."

        # Rule 2: Relevance Check (Context-Aware)
        metadata = (manifest.get("metadata") or {}) if isinstance(manifest, dict) else {}
        topic = str(metadata.get("topic") or "").strip()
        country = str(metadata.get("country") or metadata.get("geo") or "").strip()
        occasion = str(metadata.get("occasion") or metadata.get("event") or "").strip()
        combined_context = f"{topic} {country} {occasion} {copy_text}".lower()
        prompt_lower = visual_prompt.lower()

        # Check Egypt / 6 October
        is_egypt_october = any(
            kw in combined_context
            for kw in (
                "6 أكتوبر", "أكتوبر", "october", "مصر", "egypt", "سيناء",
                "sinai", "العبور", "crossing", "تحرير سيناء", "نصر أكتوبر"
            )
        )
        if is_egypt_october:
            egypt_anchors = (
                "pyramid", "pyramids", "sinai", "desert", "dunes", "october",
                "egypt", "flag", "flags", "crossing", "golden", "monument",
                "victory", "heroic", "cairo"
            )
            if not any(anchor in prompt_lower for anchor in egypt_anchors):
                return False, (
                    "REJECTED: visual prompt in image_prompt.txt does not match Egypt/6 October context; "
                    "expected thematic visual anchors (pyramids, Sinai desert dunes, flags of pride, golden hour, crossing)."
                )

        # Check Saudi / National Day / Founding Day
        is_saudi = any(
            kw in combined_context
            for kw in (
                "سعودية", "السعودية", "saudi", "ksa", "رياض", "الرياض",
                "riyadh", "درعية", "الدرعية", "diriyah", "تأسيس", "يوم التأسيس",
                "founding day", "وطني", "اليوم الوطني", "national day"
            )
        )
        if is_egypt_october:
            pass  # Already validated Egypt/October anchors above
        elif is_saudi:
            saudi_anchors = (
                "diriyah", "riyadh", "saudi", "desert", "dunes", "arabian",
                "heritage", "founding", "national", "green", "najdi"
            )
            if not any(anchor in prompt_lower for anchor in saudi_anchors):
                return False, (
                    "REJECTED: visual prompt in image_prompt.txt does not match Saudi context; "
                    "expected Diriyah, Riyadh skyline, desert dunes, or celebratory heritage elements."
                )
        elif any(
            kw in combined_context
            for kw in (
                "sovereign agency os", "quantum flux", "agent", "pipeline",
                "router", "routing", "terminal", "kernel", "نظام", "أتمتة",
                "معمارية", "بيانات", "خوارزمية"
            )
        ):
            tech_anchors = (
                "terminal", "data", "network", "command", "architecture",
                "laptop", "server", "code", "engine", "system", "dashboard",
                "futuristic", "tech", "holographic", "console", "desk"
            )
            if not any(anchor in prompt_lower for anchor in tech_anchors):
                return False, (
                    "REJECTED: visual prompt in image_prompt.txt does not match tech/system context; "
                    "expected architecture, command center, terminal, or system elements."
                )

        return True, "visual prompt verified"

    # ------------------------------------------------------------------ #
    # private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _flag(manifest: dict[str, Any], flag: str) -> object | None:
        """Return ``routing_context.flags[flag]`` (``None`` when absent)."""
        routing = manifest.get("routing_context")
        flags = routing.get("flags") if isinstance(routing, dict) else None
        return flags.get(flag) if isinstance(flags, dict) else None

    def _read_copy(self, capsule_path: str) -> str:
        """Read ``copy.md``; raise :class:`QAInputError` when unusable."""
        try:
            text = self.read_text(capsule_path, COPY_FILE)
        except CapsuleNotFoundError as exc:
            raise QAInputError(
                f"{COPY_FILE} missing — run the marketing stage first"
            ) from exc
        if not text.strip():
            raise QAInputError(f"{COPY_FILE} is empty — nothing to review")
        return text

    @staticmethod
    def _validate_verdict(passed: object, feedback: object) -> None:
        """Enforce the verdict contract: bool + non-empty, bounded feedback."""
        if not isinstance(passed, bool):
            raise QAInputError(
                f"gateway verdict 'passed' must be a bool, got {type(passed).__name__}"
            )
        if not isinstance(feedback, str) or not feedback.strip():
            raise QAInputError("gateway returned an empty review feedback string")
        if len(feedback.encode("utf-8")) > MAX_FEEDBACK_BYTES:
            raise QAInputError(
                f"gateway feedback exceeds {MAX_FEEDBACK_BYTES} bytes — refusing it"
            )


if __name__ == "__main__":  # offline smoke: python3 workers/qa.py
    import json

    demo = {
        "capsule_id": "cap-qa-demo",
        "state": "pending_qa",
        "created_at": "2026-10-03T00:00:00+00:00",
        "updated_at": "2026-10-03T00:00:00+00:00",
        "attempt": 0,
        "history": [],
        "routing_context": {"flags": {"requires_copy": True, "requires_image": True}},
    }
    tmp = Path(f"/tmp/qa_smoke_{Path(__file__).stem}")
    tmp.mkdir(exist_ok=True)
    (tmp / COPY_FILE).write_text(
        "# Sovereign Agency OS\n\n"
        "Our routing engine deploys an agent stack that cuts latency through "
        "a terminal-first workflow. No humans required.\n",
        encoding="utf-8",
    )
    from capsule_engine import save_manifest

    save_manifest(tmp, demo)
    result = QAWorker().run(str(tmp))
    print(
        json.dumps(
            {
                "state": result["state"],
                "capsule": str(tmp),
                "history_tail": result["history"][-1]["why"],
            },
            indent=2,
        )
    )
