#!/usr/bin/env python3
"""gateway_ops.py — marketing model operations layered over the core gateway.

The transport, retries, usage accounting and error hierarchy live in
:mod:`capsule_engine.gateway` (generic and domain-agnostic by design). This
module owns the marketing side of the model seam:

- the mandatory image aesthetic and its word budget;
- the Context-Aware Visual Director prompt used when the model leaks Arabic
  letters into an image prompt;
- the editorial review heuristics (the closed slop vocabulary, plus substance)
  and the reviewer contract that consumes them — over the kernel's closed slop
  vocabulary (:mod:`capsule_engine.editorial`), which the deterministic
  ``qa_review`` gate enforces too, so both sides reject the same words;
- the two marketing operations as free functions:
  ``image_prompt(gateway, copy_text, manifest=None)`` and
  ``review_copy(gateway, copy_text)``.

Both operations accept the real ``ModelGateway`` (they call its documented
``_call_llm`` network seam and rely on its usage accounting) and the
deterministic ``OfflineModelGateway`` (pure functions, zero network).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from capsule_engine.editorial import BANNED_BUZZWORDS, BANNED_PHRASES
from capsule_engine.gateway import (
    DEFAULT_MODEL,
    GatewayResponseError,
    IMAGE_TEMPERATURE,
    OfflineModelGateway,
    REVIEW_TEMPERATURE,
)
from capsule_engine.manifest import USAGE_KEYS

# The deployed company's identity, rendered for a prompt. Absent when the
# deployment has no profile — every rubric then falls back to its neutral,
# company-agnostic wording rather than to nothing.
try:
    from shared_kernel.brand_profile import company_prompt_context
except ImportError:  # pragma: no cover - standalone execution
    company_prompt_context = None  # type: ignore[assignment]

__all__ = [
    "BANNED_BUZZWORDS",
    "BANNED_PHRASES",
    "IMAGE_AESTHETIC",
    "IMAGE_SUBJECT_BUDGET",
    "IMAGE_SYSTEM_PROMPT",
    "MAX_IMAGE_PROMPT_WORDS",
    "MIN_REVIEW_WORDS",
    "REVIEW_SYSTEM_PROMPT",
    "VISUAL_DIRECTOR_SYSTEM_PROMPT",
    "build_context_aware_visual_prompt",
    "build_review_system_prompt",
    "image_prompt",
    "prepare_gateway_env",
    "review_copy",
]

LEGACY_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
LEGACY_GROQ_MODEL = "qwen/qwen3.8-27b"


def prepare_gateway_env() -> None:
    """Bridge the legacy sector credentials (``GROQ_*``) onto the gateway.

    The sector worker scripts were written against ``GROQ_API_KEY`` /
    ``GROQ_MODEL``; the single spend-gated client reads ``AGENCY_LLM_API_KEY`` /
    ``AGENCY_LLM_BASE_URL`` / ``AGENCY_LLM_MODEL`` (with ``OPENAI_*``
    fallbacks). Copy the legacy values across **only where the standard names
    are unset** — an operator who already configured the gateway keeps theirs —
    and point the Groq base URL at the model named in the price table.

    Called by both sector scripts (marketing copy, design image prompt) before
    ``default_gateway()``; it mutates ``os.environ`` and returns nothing.
    """
    groq_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not groq_key:
        return
    if (os.environ.get("AGENCY_LLM_API_KEY") or "").strip() or (
        os.environ.get("OPENAI_API_KEY") or "").strip():
        return
    os.environ["AGENCY_LLM_API_KEY"] = groq_key
    os.environ.setdefault("AGENCY_LLM_BASE_URL", LEGACY_GROQ_BASE_URL)
    os.environ.setdefault(
        "AGENCY_LLM_MODEL",
        (os.environ.get("GROQ_MODEL") or "").strip() or LEGACY_GROQ_MODEL,
    )


# The mandatory aesthetic tail. DesignWorker refuses any image prompt that
# does not embed this string verbatim — one source of truth, zero drift.
IMAGE_AESTHETIC = (
    "photorealistic, cinematic lighting, corporate editorial, high resolution, no text, no watermark"
)

# "under 60 words" is a hard contract, enforced here *and* re-checked by
# DesignWorker before anything reaches the network.
MAX_IMAGE_PROMPT_WORDS = 59

# The real client keeps the same local clamp: subject words + aesthetic words
# must never exceed MAX_IMAGE_PROMPT_WORDS.
IMAGE_SUBJECT_BUDGET = MAX_IMAGE_PROMPT_WORDS - len(IMAGE_AESTHETIC.split())

# --------------------------------------------------------------------------- #
# editorial review heuristics (the marketing tone the reviewer demands)
# --------------------------------------------------------------------------- #

# ``BANNED_BUZZWORDS`` / ``BANNED_PHRASES`` are the kernel's closed slop
# vocabulary (:mod:`capsule_engine.editorial`), imported above and re-exported
# here: the reviewer prompt below is generated from the same list the
# deterministic ``qa_review`` gate enforces.
#
# A third rule used to live here — ``MIN_TECH_SIGNALS`` technical signals, the
# "technical authority" threshold — because the rubric was written for an
# enterprise-architecture feed. It was removed rather than relaxed: it demanded
# the copy be *harder* than its audience, which is the opposite of what a
# business selling plain, practical software wants to publish. Technical
# vocabulary is now neither required nor banned; only clarity is judged.
MIN_REVIEW_WORDS = 10

# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

IMAGE_SYSTEM_PROMPT = (
    "You distill marketing copy into ONE image-generation subject line: at "
    f"most {IMAGE_SUBJECT_BUDGET} lowercase words, concrete nouns, no "
    "sentences. Do NOT include style, aesthetic, colour or camera words — "
    "the caller appends the mandatory aesthetic tail verbatim."
)

REVIEW_SYSTEM_PROMPT = (
    "You are the senior editorial and QA director for a company that publishes "
    "its own marketing copy. Enforce rigorous quality standards on the provided "
    "copy across any language. Reject copy immediately if: "
    "1) It uses any banned buzzwords ("
    + ", ".join(sorted(BANNED_BUZZWORDS))
    + ") or banned phrases ("
    + ", ".join(BANNED_PHRASES)
    + "). Judge only the words that are actually present: never report a banned "
    "word as 'implied' or 'suggested', and never reject a rewrite request over a "
    "banned word you cannot quote from the copy; "
    f"2) It is shorter than {MIN_REVIEW_WORDS} words, or has no clear headline "
    "hook, or has no call to action; "
    "3) It reads as generic AI-slop rather than as something one specific "
    "company would say; "
    "4) A business owner outside the software industry could not follow it — "
    "unexplained jargon is a defect, not authority, and technical vocabulary is "
    "neither required nor penalised. "
    "Reply with STRICT JSON only: "
    '{"passed": true|false, "feedback": "one clear actionable sentence in English"}'
)


def build_review_system_prompt() -> str:
    """The reviewer's rubric, grounded in the deployed company profile.

    The base rubric is deliberately company-agnostic; the profile block is
    appended when one exists so the reviewer judges the copy against the voice
    the company actually asked for — its tone, its reader, the languages it
    publishes in — instead of a standard that belongs to nobody.

    Returns the base rubric unchanged when the deployment has no profile, so an
    un-configured company is reviewed exactly as it was before this existed.
    """
    block = ""
    if company_prompt_context is not None:
        context = company_prompt_context().strip()
        if context:
            block = f"\n{context}\n"
    return f"{REVIEW_SYSTEM_PROMPT}{block}"

VISUAL_DIRECTOR_SYSTEM_PROMPT = (
    "You are a Context-Aware Visual Director and photographic prompt engineer. "
    "Your responsibility is to craft a single, believable visual scene prompt for image generation in STRICTLY ENGLISH. "
    "Rules: "
    "1. LANGUAGE: 100% English only. Absolutely NO Arabic words, letters, or scripts. "
    "2. CONTEXT AWARENESS (Country & Occasion): "
    "- If Egypt / 6th of October: Evoke the Pyramids of Giza and golden Sinai desert dunes in warm daylight, a calm sense of national pride. "
    "- If Saudi Arabia / Founding Day / National Day: Evoke historic Diriyah Najdi architecture meeting a modern Riyadh skyline, golden desert dunes at twilight. "
    "- If UAE / Gulf: Evoke a modern Gulf city skyline at dusk, clean and uncluttered. "
    "- If Software / Business Technology: Evoke an ordinary modern workplace — a tidy desk, a laptop, a notebook, natural daylight through a window, someone working quietly. Prefer the everyday and the specific over the spectacular. "
    "3. BRAND NATURE: Clean, modern and believable — a workplace the reader recognises, photographed in natural light. Never ornate, never opulent, never science-fiction. "
    "Strictly NO cartoon, NO 3D render illustration, NO anime, NO glowing holograms, NO distorted text, NO text overlays, NO watermarks. "
    "4. GENERATION EXCELLENCE: Soft natural or golden-hour light, shallow depth of field, ultra-detailed 8k resolution, "
    "professional camera angles (rule of thirds). "
    f"5. LENGTH: Concise, under {IMAGE_SUBJECT_BUDGET} words. Output the English scene description only, no quotes, no markdown."
)

_ARABIC_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def build_context_aware_visual_prompt(
    manifest: dict[str, Any] | None = None,
    copy_text: str = "",
) -> str:
    """Context-Aware Visual Director: craft a cinematic English image prompt.

    Adheres strictly to the guidelines:
    - 100% English only (Zero Arabic letters).
    - Context-Aware (Country & Occasion: e.g. Egypt / 6th October -> pyramids,
      Sinai desert dunes, golden hour sun, proud flags of honor, spirit of crossing).
    - Brand nature: Luxury, corporate prestige, photorealistic, no cartoon, no text.
    - Generation rules: Dramatic lighting, 8k details, professional cinematic camera angles.
    """
    metadata = (manifest.get("metadata") or {}) if isinstance(manifest, dict) else {}
    topic = str(metadata.get("topic") or "").strip()
    country = str(metadata.get("country") or metadata.get("geo") or "").strip()
    occasion = str(metadata.get("occasion") or metadata.get("event") or "").strip()

    combined = f"{topic} {country} {occasion} {copy_text}".lower()

    # Check Egypt / 6th October
    is_egypt_october = any(
        kw in combined
        for kw in (
            "6 أكتوبر", "أكتوبر", "october", "مصر", "egypt", "سيناء",
            "sinai", "العبور", "crossing", "تحرير سيناء", "نصر أكتوبر"
        )
    )
    # Check Saudi Arabia / Founding Day / National Day
    is_saudi = any(
        kw in combined
        for kw in (
            "سعودية", "السعودية", "saudi", "ksa", "رياض", "الرياض",
            "riyadh", "درعية", "الدرعية", "diriyah", "تأسيس", "يوم التأسيس",
            "founding day", "وطني", "اليوم الوطني", "national day"
        )
    )
    # Check UAE / Gulf
    is_uae = any(
        kw in combined
        for kw in (
            "إمارات", "امارات", "الإمارات", "uae", "دبي", "dubai",
            "أبوظبي", "abudhabi"
        )
    )
    # Check Software / Business Technology
    is_tech = any(
        kw in combined
        for kw in (
            "software", "website", "web app", "system", "workflow",
            "automation", "data entry", "invoice", "excel", "spreadsheet",
            "نظام", "أنظمة", "برنامج", "برامج", "موقع", "مواقع", "أتمتة",
            "بيانات", "فاتورة", "فواتير", "إكسيل", "جدول"
        )
    )

    if is_egypt_october:
        subject = (
            "the Pyramids of Giza and golden Sinai desert dunes in warm daylight, "
            "a calm sense of national pride, "
            "8k Hasselblad medium format photography, wide angle"
        )
    elif is_saudi:
        subject = (
            "historic Diriyah Najdi architecture meeting a modern Riyadh skyline, "
            "golden desert dunes at twilight, "
            "8k resolution, wide angle"
        )
    elif is_uae:
        subject = (
            "a modern Gulf city skyline at dusk, clean and uncluttered, "
            "8k details, wide perspective"
        )
    elif is_tech:
        subject = (
            "a calm and tidy modern workspace: a desk with a laptop, a notebook "
            "and a cup of coffee, soft natural daylight through a window, "
            "8k details, shallow depth of field"
        )
    else:
        # Clean topic keywords in English or clean transliteration
        clean_words = [w for w in re.findall(r"[a-zA-Z0-9]+", topic) if len(w) > 2]
        topic_hint = (
            " ".join(clean_words[:4]) if clean_words else "a modern business workplace"
        )
        subject = (
            f"a bright and tidy modern workplace, soft natural daylight, "
            f"8k details, shallow depth of field, {topic_hint}"
        )

    # Append the mandatory IMAGE_AESTHETIC
    return f"{subject}, {IMAGE_AESTHETIC}"


# --------------------------------------------------------------------------- #
# the two marketing operations (real + offline share one contract)
# --------------------------------------------------------------------------- #

def image_prompt(
    gateway: Any, copy_text: str, manifest: dict[str, Any] | None = None
) -> str:
    """Distill copy into a context-aware cinematic English image prompt.

    The model writes the visual subject in pure English; this function enforces
    the aesthetic tail and sub-60-word budget. The offline gateway is a pure
    function of its inputs; the real gateway is called through its documented
    ``_call_llm`` network seam (usage lands on ``gateway.last_usage``).
    """
    if isinstance(gateway, OfflineModelGateway):
        return _offline_image_prompt(gateway, copy_text, manifest)

    if not isinstance(copy_text, str) or not copy_text.strip():
        raise GatewayResponseError("image_prompt requires non-empty copy text")

    user_content = copy_text
    if isinstance(manifest, dict):
        meta = manifest.get("metadata") or {}
        topic = meta.get("topic", "")
        country = meta.get("country", meta.get("geo", ""))
        occasion = meta.get("occasion", meta.get("event", ""))
        context_bits = []
        if topic:
            context_bits.append(f"Topic: {topic}")
        if country:
            context_bits.append(f"Country/Region: {country}")
        if occasion:
            context_bits.append(f"Occasion/Event: {occasion}")
        if context_bits:
            user_content = f"Context: {'; '.join(context_bits)}\n\nMarketing Copy:\n{copy_text}"

    subject = gateway._call_llm(
        [
            {"role": "system", "content": VISUAL_DIRECTOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        getattr(gateway, "_model", DEFAULT_MODEL),
        IMAGE_TEMPERATURE,
    )

    # Defense in depth: if the model returned Arabic characters, fallback to
    # the deterministic director
    if _ARABIC_RE.search(subject):
        import logging

        logging.getLogger("data.domains.marketing.gateway_ops").warning(
            "gateway: model leaked Arabic letters in image prompt; "
            "invoking context-aware visual director"
        )
        return build_context_aware_visual_prompt(manifest, copy_text)

    return _compose_image_prompt(subject)


def review_copy(gateway: Any, copy_text: str) -> tuple[bool, str]:
    """Strict QA verdict as ``(passed, feedback)``.

    Empty copy is rejected locally without a model call. The offline gateway
    grades deterministically against the heuristics above; the real gateway
    answers structured JSON and any non-JSON or contract-violating reply
    raises :class:`GatewayResponseError` — QA never guesses a verdict.
    """
    if isinstance(gateway, OfflineModelGateway):
        return _offline_review_copy(gateway, copy_text)

    if not isinstance(copy_text, str) or not copy_text.strip():
        return False, "REJECTED: copy is empty — nothing to review."
    raw = gateway._call_llm(
        [
            {"role": "system", "content": build_review_system_prompt()},
            {"role": "user", "content": copy_text},
        ],
        getattr(gateway, "_model", DEFAULT_MODEL),
        REVIEW_TEMPERATURE,
    )
    return _parse_verdict(raw)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _zero_usage(gateway: Any) -> None:
    """Mark an offline call as a zero-token, zero-cost observation."""
    gateway.last_usage = {key: 0 for key in USAGE_KEYS}


def _compose_image_prompt(subject: str) -> str:
    """Append the aesthetic and clamp the subject to its word budget."""
    cleaned = subject.strip().strip("\"'`").replace(IMAGE_AESTHETIC, " ")
    words = cleaned.split()
    if not words:
        raise GatewayResponseError("gateway returned an empty image subject")
    subject_part = " ".join(words[:IMAGE_SUBJECT_BUDGET])
    return f"{subject_part}, {IMAGE_AESTHETIC}"


def _parse_verdict(raw: str) -> tuple[bool, str]:
    """Parse the reviewer's strict-JSON verdict (code fences tolerated)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise GatewayResponseError("reviewer reply is not a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GatewayResponseError(
            f"reviewer JSON invalid at line {exc.lineno}: {exc.msg}"
        ) from exc
    passed = data.get("passed") if isinstance(data, dict) else None
    feedback = data.get("feedback") if isinstance(data, dict) else None
    if (
        not isinstance(passed, bool)
        or not isinstance(feedback, str)
        or not feedback.strip()
    ):
        raise GatewayResponseError(
            "reviewer verdict must be "
            '{"passed": bool, "feedback": non-empty string}'
        )
    return passed, feedback.strip()


def _offline_image_prompt(
    gateway: Any, copy_text: str, manifest: dict[str, Any] | None = None
) -> str:
    """Distill ``copy_text`` into a sub-60-word image-generation prompt.

    Deterministic: if context-aware signals (country, occasion, or manifest)
    are present, crafts an intelligent cinematic English prompt. Otherwise
    falls back to distilled salient tokens + aesthetic.
    """
    _zero_usage(gateway)
    copy_text = copy_text if isinstance(copy_text, str) else ""

    meta = (manifest.get("metadata") or {}) if isinstance(manifest, dict) else {}
    topic = str(meta.get("topic") or "").lower()
    country = str(meta.get("country") or "").lower()
    occasion = str(meta.get("occasion") or "").lower()
    combined = f"{topic} {country} {occasion} {copy_text.lower()}"

    has_context_signal = any(
        kw in combined
        for kw in (
            "6 أكتوبر", "أكتوبر", "october", "مصر", "egypt", "سيناء", "sinai",
            "العبور", "سعودية", "saudi", "ksa", "رياض", "riyadh", "diriyah",
            "تأسيس", "إمارات", "uae", "دبي", "dubai"
        )
    )
    if has_context_signal or (manifest is not None and (topic or country or occasion)):
        return build_context_aware_visual_prompt(manifest, copy_text)

    budget = MAX_IMAGE_PROMPT_WORDS - len(IMAGE_AESTHETIC.split())
    subject = _distill(copy_text, budget)
    return f"{subject}, {IMAGE_AESTHETIC}"


def _offline_review_copy(gateway: Any, copy_text: str) -> tuple[bool, str]:
    """Deterministically grade copy against the agency's strict guidelines.

    Rules (pure functions of ``copy_text`` — same input, same verdict):
    1. length sanity: fewer than :data:`MIN_REVIEW_WORDS` tokens fails;
    2. banned AI buzzwords: any :data:`BANNED_BUZZWORDS` /
        :data:`BANNED_PHRASES` hit fails.

    Only what a pure function can actually decide is decided here. Whether the
    copy sounds like the company, reads clearly, and carries a hook and a call
    to action is the reviewer's judgement; the offline path must not pretend to
    make it — least of all by demanding technical vocabulary the company never
    asked for.
    """
    _zero_usage(gateway)
    if not isinstance(copy_text, str) or not copy_text.strip():
        return False, "REJECTED: copy is empty — nothing to review."

    normalized = " ".join(_normalize(copy_text).split())
    tokens = _tokenize(normalized)
    token_set = set(tokens)

    problems: list[str] = []
    hits = sorted(
        {word for word in BANNED_BUZZWORDS if word in token_set}
        | {phrase for phrase in BANNED_PHRASES if phrase in normalized}
    )
    if hits:
        problems.append(f"banned buzzwords present: {', '.join(hits)}")

    if len(tokens) < MIN_REVIEW_WORDS:
        problems.append(
            f"copy too short ({len(tokens)} words; need {MIN_REVIEW_WORDS})"
        )

    if problems:
        return False, "REJECTED: " + "; ".join(problems) + " — revise and resubmit."
    return True, (
        f"PASSED: no banned buzzwords; {len(tokens)} words of reviewable copy."
    )


# --------------------------------------------------------------------------- #
# deterministic text helpers (offline image prompt + review)
# --------------------------------------------------------------------------- #

def _normalize(text: str) -> str:
    """Split camelCase boundaries and lowercase, for deterministic matching."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).lower()


def _tokenize(normalized: str) -> list[str]:
    """Whole-token view of normalized text (hyphens/apostrophes separate)."""
    return re.findall(r"[a-z0-9+#]+", normalized)


_MARKUP_RE = re.compile(r"[#*_>`\[\]()|]+")
_STRIP_CHARS = ".,:;!?'\"—–-"

# Already carried by IMAGE_AESTHETIC — never repeat them in the subject.
_AESTHETIC_WORDS = frozenset(
    word.strip(",").lower() for word in IMAGE_AESTHETIC.split()
)

# Deliberately small: enough to strip boilerplate, never "smart".
_STOPWORDS = frozenset(
    """
    the and for with that this your you are were has have had not but can
    all any who what when why how from into over under about post write
    writes compelling dummy produced meta commentary output headline hook
    call action benefit benefits bullet bullets markdown only preamble
    swap made meets meeting its their they them will just very more most
    """.split()
)


def _distill(copy_text: str, budget: int) -> str:
    """Return up to ``budget`` salient, deduplicated lowercase tokens."""
    if budget <= 0:
        return ""
    seen: set[str] = set()
    picked: list[str] = []
    for token in _MARKUP_RE.sub(" ", copy_text).lower().split():
        token = token.strip(_STRIP_CHARS)
        if (
            len(token) < 3
            or token.isdigit()
            or token in _STOPWORDS
            or token in _AESTHETIC_WORDS
            or token in seen
        ):
            continue
        seen.add(token)
        picked.append(token)
        if len(picked) >= budget:
            break
    return " ".join(picked) if picked else "terminal dashboard"
