#!/usr/bin/env python3
"""gateway_ops.py — marketing model operations layered over the core gateway.

The transport, retries, usage accounting and error hierarchy live in
:mod:`capsule_engine.gateway` (generic and domain-agnostic by design). This
module owns the marketing side of the model seam:

- the mandatory image aesthetic and its word budget;
- the Context-Aware Visual Director prompt used when the model leaks Arabic
  letters into an image prompt;
- the editorial review heuristics (tech-signal tone) and the reviewer
  contract that consumes them — over the kernel's closed slop vocabulary
  (:mod:`capsule_engine.editorial`), which the deterministic ``qa_review`` gate
  enforces too, so both sides reject exactly the same text;
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

__all__ = [
    "BANNED_BUZZWORDS",
    "BANNED_PHRASES",
    "IMAGE_AESTHETIC",
    "IMAGE_SUBJECT_BUDGET",
    "IMAGE_SYSTEM_PROMPT",
    "MAX_IMAGE_PROMPT_WORDS",
    "MIN_REVIEW_WORDS",
    "MIN_TECH_SIGNALS",
    "REVIEW_SYSTEM_PROMPT",
    "TECH_SIGNALS",
    "VISUAL_DIRECTOR_SYSTEM_PROMPT",
    "build_context_aware_visual_prompt",
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

# The tech-heavy tone the agency requires: at least MIN_TECH_SIGNALS distinct
# signals must appear as whole tokens in the copy.
TECH_SIGNALS = frozenset(
    {
        "api", "agent", "architecture", "automation", "backend", "cache",
        "client", "code", "data", "database", "deploy", "encryption",
        "engine", "gateway", "kernel", "latency", "model", "node", "pipeline",
        "protocol", "router", "routing", "runtime", "stack", "system",
        "terminal", "throughput", "webhook", "workflow",
        # Arabic technical terms (whole tokens):
        "نظام", "أنظمة", "أتمتة", "معمارية", "بيانات", "خوارزمية", "خوارزميات",
        "مسار", "مسارات", "منصة", "منصات", "بنية", "بنى", "نموذج", "نماذج",
        "محرك", "محركات", "بروتوكول", "واجهة",
    }
)
MIN_TECH_SIGNALS = 2
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
    "You are the senior editorial and QA director of an elite tech agency. "
    "Enforce rigorous quality standards on the provided copy across any language. "
    "Reject copy immediately if: "
    "1) It uses any banned buzzwords ("
    + ", ".join(sorted(BANNED_BUZZWORDS))
    + ") or banned phrases ("
    + ", ".join(BANNED_PHRASES)
    + f"); 2) It lacks sufficient technical authority (fewer than {MIN_TECH_SIGNALS} technical signal terms); "
    + f"3) It is shorter than {MIN_REVIEW_WORDS} words or lacks a clear headline hook and call-to-action; "
    "4) It sounds like generic AI-slop rather than an authoritative thought-leadership post. "
    "Reply with STRICT JSON only: "
    '{"passed": true|false, "feedback": "one clear actionable sentence in English"}'
)

VISUAL_DIRECTOR_SYSTEM_PROMPT = (
    "You are an elite Context-Aware Visual Director and cinematic AI prompt engineer. "
    "Your responsibility is to craft a single, breathtaking visual scene prompt for image generation in STRICTLY ENGLISH. "
    "Rules: "
    "1. LANGUAGE: 100% English only. Absolutely NO Arabic words, letters, or scripts. "
    "2. CONTEXT AWARENESS (Country & Occasion): "
    "- If Egypt / 6th of October: Evoke majestic Pyramids of Giza, golden Sinai desert dunes, glorious golden hour sunlight, proud billowing Egyptian flags, heroic spirit of crossing and triumph. "
    "- If Saudi Arabia / Founding Day / National Day: Evoke historic Diriyah Najdi architecture, futuristic Riyadh skyline, golden desert dunes at twilight, rich celebratory green banners. "
    "- If UAE / Gulf: Evoke striking futuristic skyline, luminous architectural marvels, modern luxury oasis at dusk. "
    "- If Tech / Sovereign Agency OS: Evoke an elite executive command center, holographic system architectures, glowing fiber-optic data streams, sophisticated minimalist glass and steel. "
    "3. BRAND NATURE: High luxury, corporate prestige, photorealistic medium format photography. "
    "Strictly NO cartoon, NO 3D render illustration, NO anime, NO distorted text, NO text overlays, NO watermarks. "
    "4. GENERATION EXCELLENCE: Dramatic cinematic lighting (golden hour or volumetric chiaroscuro), ultra-detailed 8k resolution, "
    "professional camera angles (cinematic wide angle or dynamic low angle, rule of thirds). "
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
    # Check Tech / Sovereign OS
    is_tech = any(
        kw in combined
        for kw in (
            "sovereign agency os", "quantum flux", "agent", "pipeline",
            "router", "routing", "terminal", "kernel", "نظام", "أتمتة",
            "معمارية", "بيانات", "خوارزمية"
        )
    )

    if is_egypt_october:
        subject = (
            "majestic Pyramids of Giza and vast golden Sinai desert dunes during golden hour, "
            "proud Egyptian flags fluttering with dignity against the horizon, "
            "heroic spirit of crossing and triumph, luxury corporate prestige, "
            "8k Hasselblad medium format photography, dramatic cinematic wide angle"
        )
    elif is_saudi:
        subject = (
            "majestic historic Diriyah Najdi architecture meeting futuristic modern Riyadh skyline, "
            "golden desert dunes at sunset, celebratory green banners of pride, "
            "luxury brand prestige, 8k resolution, dramatic cinematic low angle"
        )
    elif is_uae:
        subject = (
            "striking futuristic Dubai skyline with luminous architectural marvels, "
            "modern luxury corporate oasis at dusk, ambient dusk lighting, "
            "8k details, dramatic cinematic wide perspective"
        )
    elif is_tech:
        subject = (
            "sleek executive command center with holographic system architectures, "
            "glowing fiber-optic data streams, minimalist luxury glass and steel, "
            "8k resolution, dramatic chiaroscuro volumetric lighting"
        )
    else:
        # Clean topic keywords in English or clean transliteration
        clean_words = [w for w in re.findall(r"[a-zA-Z0-9]+", topic) if len(w) > 2]
        topic_hint = " ".join(clean_words[:4]) if clean_words else "corporate sovereign leadership"
        subject = (
            f"luxurious executive corporate setting with minimalist modern architecture, "
            f"dramatic cinematic lighting, 8k details, professional wide angle, {topic_hint}"
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
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
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
        :data:`BANNED_PHRASES` hit fails;
    3. tech-heavy tone: at least :data:`MIN_TECH_SIGNALS` distinct
        :data:`TECH_SIGNALS` must appear.
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

    signals = sorted(signal for signal in TECH_SIGNALS if signal in token_set)
    if len(signals) < MIN_TECH_SIGNALS:
        problems.append(
            f"tech-heavy tone missing ({len(signals)} signal(s); "
            f"need {MIN_TECH_SIGNALS})"
        )

    if len(tokens) < MIN_REVIEW_WORDS:
        problems.append(
            f"copy too short ({len(tokens)} words; need {MIN_REVIEW_WORDS})"
        )

    if problems:
        return False, "REJECTED: " + "; ".join(problems) + " — revise and resubmit."
    return True, (
        "PASSED: tech-heavy tone confirmed "
        f"({len(signals)} signals: {', '.join(signals)}); "
        "no banned buzzwords."
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
