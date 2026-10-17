#!/usr/bin/env python3
"""marketing.py — MarketingWorker: the first concrete capsule worker.

Pipeline stage: given a capsule that requires copy, resolve a prompt, call the
(ModelGateway) model, persist the Markdown artifact as ``copy.md``, and hand
the capsule to the QA stage (``pending_qa``).

Responsibilities, in order — each one fails closed:

1. **Routing gate**: ``routing_context.flags.requires_copy`` must be ``True``;
   otherwise this worker must not run (``RoutingFlagError``).
2. **Prompt resolution**: manifest ``raw_prompt`` wins when present and
   non-empty; otherwise a default prompt is built from ``metadata.topic`` and
   ``metadata.target_platform`` — plain f-strings, no templating engine
   (``PromptResolutionError`` when inputs are missing/empty). When the
   capsule comes back after a QA rejection, the QA stage's ``feedback.txt``
   is appended to the prompt and consumed on success, so the rewrite
   addresses the reviewer's points instead of regenerating blindly. A
   missing feedback file is recorded as a history warning, never a hard
   stop.
3. **Gateway execution**: ``ModelGateway.complete(manifest, prompt)`` behind
   the injected gateway seam — the default factory returns the real HTTP
   client when credentials are configured and the deterministic offline
   engine otherwise. Gateway failures are recorded into the manifest's
   ``history`` by this worker *before* being re-raised as
   ``GenerationError``, and :meth:`capsule_engine.worker.BaseWorker.run`
   then persists the attempt counter / state bookkeeping on top of it.
4. **Asset persistence**: the Markdown is written via ``write_text`` (atomic
   temp + fsync + ``os.replace``, path-traversal safe) as ``copy.md``.
5. **State transition**: ``-> pending_qa`` through the lawful transition
   matrix, which appends the auditable ``history`` entry automatically.

Failure contract: every exception raised inside ``execute`` is captured on
disk by ``run()`` (attempt++, history entry, escalation to ``failed`` at the
attempt cap) — a broken generation is always visible, never silent.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, ClassVar

try:  # normal import (package on sys.path)
    from capsule_engine import ARTIFACT_FILE, BaseWorker, State
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError
    from capsule_engine.gateway import ModelGateway, default_gateway
except ImportError:  # script/odd-cwd execution — mirror orchestration/dispatcher.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import ARTIFACT_FILE, BaseWorker, State
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError
    from capsule_engine.gateway import ModelGateway, default_gateway

# The marketing package lives under ``data/domains/``: ``data.domains.marketing.*``
# needs the repo root importable (standalone script runs included).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from data.domains.marketing.gateway_ops import (  # noqa: E402
    build_context_aware_visual_prompt,
    image_prompt,
)

# The company's identity, rendered for a prompt. Absent when the deployment has
# no profile — the worker then behaves exactly as it did before this existed.
try:
    from shared_kernel.brand_profile import company_prompt_context  # noqa: E402
except ImportError:  # pragma: no cover - standalone execution
    company_prompt_context = None  # type: ignore[assignment]

# B0: the copy stage writes the engine's default artifact name — one source
# of truth for the default, this worker has no private duplicate.
COPY_FILE = ARTIFACT_FILE

__all__ = [
    "MarketingWorker",
    "MarketingWorkerError",
    "RoutingFlagError",
    "PromptResolutionError",
    "GenerationError",
    "ModelGateway",  # re-exported from capsule_engine.gateway for callers
]

DEFAULT_PROMPT_TEMPLATE = (
    "Write a compelling marketing post about {topic}.\n"
    "Target platform: {platform}.\n"
    "Output clean Markdown only: a headline, a hook, three benefit bullets, "
    "and a call to action. No preamble, no meta-commentary."
)

# Written by the QA stage on rejection (see workers/qa.py); read before a
# rewrite and consumed on success so stale feedback never leaks into a later
# revision round.
FEEDBACK_FILE = "feedback.txt"
MAX_FEEDBACK_BYTES = 16 * 1024  # mirrors workers/qa.py — a note, not an essay
#: Automatic re-roll attempts when a proposal duplicates the rolling window.
MAX_DEDUP_REROLLS = 2
REVISION_FEEDBACK_HEADER = "QA reviewer feedback to address in this rewrite:"

#: What the finished artifact must be, once the brief has chosen the topic. The
#: two-version rule lives here (not in the ideator) because this is the stage
#: that owns the artifact the reader actually sees.
COPY_OUTPUT_INSTRUCTIONS = (
    "Write the finished post, not a brief and not an outline.\n"
    "Produce TWO clearly separated versions of the same post:\n"
    "1. Arabic first — simple everyday Arabic a business owner reads without effort.\n"
    "2. English second — the same post in natural English, not a literal translation.\n"
    "Give each version its own Markdown heading. Keep each one tight enough for a "
    "social post. Mention a company service only where it fits naturally."
)


def brand_prompt_block() -> str:
    """The company's identity plus how this stage is to finish the post.

    Returns ``""`` when the deployment has no company profile, so a caller can
    append it unconditionally and an un-configured company gets exactly the
    copy it got before this block existed.
    """
    if company_prompt_context is None:
        return ""
    context = company_prompt_context().strip()
    if not context:
        return ""
    return f"{context}\n\n{COPY_OUTPUT_INSTRUCTIONS}"


class MarketingWorkerError(CapsuleError):
    """Base class for every deliberate MarketingWorker failure."""


class RoutingFlagError(MarketingWorkerError):
    """routing_context.flags.requires_copy is missing or not True."""


class PromptResolutionError(MarketingWorkerError):
    """No usable prompt: raw_prompt/metadata cannot build one, or the stored
    QA feedback is unreadable/oversized."""


class GenerationError(MarketingWorkerError):
    """The ModelGateway call failed or returned unusable output."""


class MarketingWorker(BaseWorker):
    """Author ``copy.md`` for a capsule and hand it to the QA stage.

    Args:
        gateway: optional ``ModelGateway``-compatible object (dependency
            injection — tests inject failures without patching globals).

    Class contract (see :class:`capsule_engine.BaseWorker`):
        ``handles`` includes ``IN_PROGRESS`` so a stale-lease resume (router
        routing a dead ``in_progress`` capsule back to its recorded owner)
        re-enters this worker instead of tripping the state guard.
    """

    name: ClassVar[str] = "marketing"
    handles: ClassVar[frozenset[State]] = frozenset(
        {State.DRAFT, State.NEEDS_REVISION, State.IN_PROGRESS}
    )
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self._gateway = gateway if gateway is not None else default_gateway()

    # ------------------------------------------------------------------ #
    # the worker contract
    # ------------------------------------------------------------------ #

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Produce ``copy.md`` and transition the capsule to ``pending_qa``.

        When a QA rejection left ``feedback.txt`` in the capsule, that
        feedback is appended to the prompt and consumed by this rewrite. A
        ``needs_revision`` capsule with no feedback file still proceeds —
        the gap is recorded in ``history`` as a warning, never a hard stop.

        Raises:
            RoutingFlagError: routing forbids copy for this capsule.
            PromptResolutionError: neither raw_prompt nor metadata can build
                a prompt, or the stored QA feedback is unusable (nothing is
                mutated in these cases, so the attempt fails from a clean
                state).
            GenerationError: the gateway raised or returned unusable output;
                the failure detail is already in ``history`` when run()
                persists the manifest.
        """
        self._require_copy_flag(manifest)
        prior_state = State.parse(manifest["state"])
        prompt = self._resolve_prompt(manifest)

        # Who the company is, and how this stage finishes the post. Appended
        # after the brief so the brief sets the topic and the company DNA sets
        # the voice; QA feedback (below) still lands last, closest to the task.
        company_block = brand_prompt_block()
        if company_block:
            prompt = f"{prompt}\n\n{company_block}"
            self.record(
                manifest,
                "company brand context injected into the copy prompt",
            )

        feedback = self._read_feedback(capsule_path)

        if prior_state is not State.IN_PROGRESS:
            self.transition(
                manifest, State.IN_PROGRESS, why="marketing copy generation started"
            )

        if feedback is not None:
            prompt = f"{prompt}\n\n{REVISION_FEEDBACK_HEADER}\n{feedback}"
            self.record(
                manifest,
                f"qa revision feedback loaded ({len(feedback)} chars) — "
                "addressing it in this rewrite",
            )
        elif prior_state is State.NEEDS_REVISION:
            self.record(
                manifest,
                "warning: needs_revision with no feedback.txt — "
                "regenerating without reviewer feedback",
            )

        # Cumulative learning memory: past rejection lessons from the central
        # feedback ledger (matching this domain/tags) are injected as explicit
        # negative constraints, so the same mistake is not repeated. The
        # window and budget come from config/engine.json, never hardcoded.
        constraints = self._constraint_block(manifest)
        if constraints:
            prompt = f"{prompt}\n\n{constraints}"
            self.record(
                manifest,
                f"negative constraints injected from feedback memory "
                f"({len(constraints)} chars)",
            )

        markdown = self._generate(manifest, prompt)

        # Dynamic rolling deduplication (config/engine.json → dedup): a
        # proposal too similar to a historical one inside the window is
        # re-rolled automatically; if it still duplicates after the retry
        # budget, the verdict is recorded for the gates/QA to judge — never
        # silently accepted or silently dropped.
        markdown = self._dedup_reroll(manifest, prompt, markdown)

        if not isinstance(markdown, str) or not markdown.strip():
            raise GenerationError(
                "ModelGateway returned empty/non-string output — fail closed"
            )

        artifact = self.write_text(capsule_path, COPY_FILE, markdown)

        # Clean deliverables layout: mirror into deliverables/{channel}/copy.txt
        channels: list[str] = []
        raw_channels = manifest.get("channels")
        if isinstance(raw_channels, list):
            channels = [str(c).strip() for c in raw_channels if str(c).strip()]
        if not channels:
            metadata = manifest.get("metadata")
            if isinstance(metadata, dict):
                for key in ("target_platform", "platform", "channel"):
                    val = metadata.get(key)
                    if isinstance(val, str) and val.strip():
                        channels = [val.strip()]
                        break
        if not channels:
            domain = manifest.get("domain")
            channels = [str(domain).strip() if domain else "marketing"]

        for ch in channels:
            self.write_text(capsule_path, f"deliverables/{ch}/copy.txt", markdown)

        # Context-Aware Visual Director: generate image_prompt.txt strictly in English
        visual_prompt = ""
        try:
            visual_prompt = image_prompt(self._gateway, markdown, manifest=manifest)
        except TypeError:
            try:
                visual_prompt = image_prompt(self._gateway, markdown)
            except Exception as exc:
                self.record(manifest, f"image_prompt generation failed: {exc}")
        except Exception as exc:
            self.record(manifest, f"image_prompt generation failed: {exc}")

        if not visual_prompt or not visual_prompt.strip():
            visual_prompt = build_context_aware_visual_prompt(manifest, markdown)

        # Fail-safe check: strictly 100% English, no Arabic letters permitted
        if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", visual_prompt):
            visual_prompt = build_context_aware_visual_prompt(manifest, markdown)

        self.write_text(capsule_path, "image_prompt.txt", visual_prompt.strip() + "\n")

        if feedback is None:
            why = f"{artifact.name} written ({len(markdown)} chars) and image_prompt.txt saved — queued for qa"
        else:
            note = self._clear_feedback(capsule_path)
            if note is not None:
                self.record(manifest, note)
            why = (
                f"{artifact.name} rewritten ({len(markdown)} chars) — "
                "qa feedback addressed; queued for qa"
            )

        # Zero Waste QA Gate: Always queue to PENDING_QA first.
        # DesignWorker will never be invoked until QA approves the visual prompt.
        self.transition(manifest, State.PENDING_QA, why=why)
        return manifest

    # ------------------------------------------------------------------ #
    # private helpers
    # ------------------------------------------------------------------ #

    def _generate(self, manifest: dict[str, Any], prompt: str) -> str:
        """One gateway call with token accounting; failures are recorded."""
        try:
            markdown = self._gateway.complete(manifest, prompt)
        except Exception as exc:  # one recorded failure never wedges the capsule
            self.record(
                manifest,
                f"model generation failed: {type(exc).__name__}: {exc}",
            )
            raise GenerationError(
                f"ModelGateway.complete failed: {type(exc).__name__}: {exc}"
            ) from exc
        self.account_usage(manifest, self._gateway)
        return markdown

    def _constraint_block(self, manifest: dict[str, Any]) -> str:
        """Prompt-ready negative constraints from the cumulative memory."""
        try:
            from capsule_engine import memory as memory_engine

            domain = manifest.get("domain")
            tags: list[str] = []
            routing = manifest.get("routing_context")
            if isinstance(routing, dict):
                for key in ("topic", "occasion", "channel", "kind"):
                    value = routing.get(key)
                    if isinstance(value, str) and value.strip():
                        tags.append(value.strip())
            channels = manifest.get("channels")
            if isinstance(channels, list):
                tags.extend(str(c) for c in channels if isinstance(c, str))
            return memory_engine.constraint_block(
                domain=domain if isinstance(domain, str) and domain.strip() else None,
                tags=tags,
            )
        except Exception as exc:  # memory is an enhancement, never a blocker
            self.record(
                manifest,
                f"feedback memory unavailable: {type(exc).__name__}: {exc}",
            )
            return ""

    def _dedup_reroll(
        self,
        manifest: dict[str, Any],
        prompt: str,
        markdown: str,
    ) -> str:
        """Validate against the rolling window and re-roll duplicates.

        Up to :data:`MAX_DEDUP_REROLLS` regenerations with the engine's re-roll
        guidance appended. The final verdict is always written to
        ``metadata.dedup`` (and the trace/history), so a proposal that could
        not escape its window is visible to QA and the human gate rather than
        silently accepted.
        """
        try:
            from capsule_engine import dedup as dedup_engine
        except Exception:
            return markdown

        domain = manifest.get("domain")
        clean_domain = domain.strip() if isinstance(domain, str) and domain.strip() else None
        capsule_id = str(manifest.get("capsule_id") or "unknown")
        try:
            verdict = dedup_engine.check(
                markdown, domain=clean_domain, capsule_id=capsule_id, kind="copy"
            )
        except Exception as exc:
            self.record(
                manifest, f"dedup check skipped: {type(exc).__name__}: {exc}"
            )
            return markdown

        attempts = 0
        if verdict.duplicate:
            # Auditable: the duplicate detection itself is recorded before the
            # automatic re-roll, so the trace shows the full loop.
            self.record(manifest, f"dedup: duplicate detected — {verdict.reason}")
        while verdict.duplicate and attempts < MAX_DEDUP_REROLLS:
            attempts += 1
            guidance = dedup_engine.reroll_guidance(verdict)
            try:
                markdown = self._generate(manifest, f"{prompt}\n\n{guidance}")
            except Exception as exc:
                self.record(
                    manifest,
                    f"dedup re-roll #{attempts} failed: {type(exc).__name__}: {exc}",
                )
                break
            try:
                verdict = dedup_engine.check(
                    markdown, domain=clean_domain, capsule_id=capsule_id, kind="copy"
                )
            except Exception:
                break

        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            manifest["metadata"] = metadata
        metadata["dedup"] = verdict.to_dict()
        self.record(
            manifest,
            f"dedup: {verdict.reason} (re-rolls: {attempts})",
        )
        return markdown

    @staticmethod
    def _require_copy_flag(manifest: dict[str, Any]) -> None:
        """Fail unless ``routing_context.flags.requires_copy`` is strictly True."""
        routing = manifest.get("routing_context")
        flags = routing.get("flags") if isinstance(routing, dict) else None
        requires_copy = flags.get("requires_copy") if isinstance(flags, dict) else None
        if requires_copy is not True:
            if not isinstance(routing, dict):
                found = "routing_context missing"
            elif not isinstance(flags, dict):
                found = "routing_context.flags missing"
            else:
                found = f"routing_context.flags.requires_copy = {requires_copy!r}"
            raise RoutingFlagError(
                f"worker {MarketingWorker.name!r} requires requires_copy=true; {found}"
            )

    @staticmethod
    def _resolve_prompt(manifest: dict[str, Any]) -> str:
        """Return ``raw_prompt`` when usable, else a built default prompt.

        No templating engine: plain f-string interpolation over validated
        ``metadata.topic`` and ``metadata.target_platform``.
        """
        raw = manifest.get("raw_prompt")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()

        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise PromptResolutionError(
                "no raw_prompt and 'metadata' is missing or not an object"
            )

        missing: list[str] = []
        topic = metadata.get("topic")
        platform = metadata.get("target_platform")
        if not isinstance(topic, str) or not topic.strip():
            missing.append("metadata.topic")
        if not isinstance(platform, str) or not platform.strip():
            missing.append("metadata.target_platform")
        if missing:
            raise PromptResolutionError(
                f"no raw_prompt and cannot build a default prompt; "
                f"missing or empty: {', '.join(missing)}"
            )

        return DEFAULT_PROMPT_TEMPLATE.format(topic=topic.strip(), platform=platform.strip())

    def _read_feedback(self, capsule_path: str) -> str | None:
        """Return stored QA feedback for this rewrite, or ``None`` when absent.

        ``feedback.txt`` is written by the QA stage on rejection. An
        unreadable or oversized feedback file fails closed *before* any
        mutation — the capsule stays ``needs_revision`` for the next attempt.
        """
        try:
            text = self.read_text(capsule_path, FEEDBACK_FILE)
        except CapsuleNotFoundError:
            return None
        except OSError as exc:  # unreadable → unusable prompt, nothing mutated
            raise PromptResolutionError(
                f"{FEEDBACK_FILE} could not be read: {exc}"
            ) from exc
        if not text.strip():
            return None
        if len(text.encode("utf-8")) > MAX_FEEDBACK_BYTES:
            raise PromptResolutionError(
                f"{FEEDBACK_FILE} exceeds {MAX_FEEDBACK_BYTES} bytes — "
                "refusing to build a revision prompt from it"
            )
        return text.strip()

    def _clear_feedback(self, capsule_path: str) -> str | None:
        """Consume ``feedback.txt`` after a successful rewrite.

        Returns a history note when the file could not be removed (the
        rewritten copy is already persisted, so this is not fatal), or
        ``None`` when the feedback is gone.
        """
        target = self.resolve(capsule_path, FEEDBACK_FILE)
        try:
            target.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"could not remove consumed {FEEDBACK_FILE}: {exc}"
        return None


if __name__ == "__main__":  # smoke: python3 workers/marketing.py
    import json

    demo = {
        "capsule_id": "cap-demo",
        "state": "draft",
        "created_at": "2026-10-03T00:00:00+00:00",
        "updated_at": "2026-10-03T00:00:00+00:00",
        "attempt": 0,
        "history": [],
        "routing_context": {"flags": {"requires_copy": True}},
        "metadata": {"topic": "Sovereign Agency OS", "target_platform": "linkedin"},
    }
    tmp = Path(f"/tmp/marketing_smoke_{Path(__file__).stem}")
    tmp.mkdir(exist_ok=True)
    from capsule_engine import save_manifest

    save_manifest(tmp, demo)
    result = MarketingWorker().run(str(tmp))
    print(json.dumps({"state": result["state"], "capsule": str(tmp)}, indent=2))
    print((tmp / "copy.md").read_text(encoding="utf-8"))
