#!/usr/bin/env python3
"""messages.py — **the one place** any human-readable ``/v1`` sentence is written.

The mobile contract answers Arabic by default with an English twin. Both come
from here, keyed by a *stable machine code*, so:

- a client switches language by picking ``reason`` or ``reason_en`` — it never
  parses prose, and it never guesses from a status code;
- wording changes in one file, in one language at a time, without touching the
  read model or the route;
- a machine code stays put while its text is rewritten, so an app can ship
  before the copy is frozen.

The same rule covers the **stage labels**: the engine's states are a closed
vocabulary (``capsule_engine.manifest.State``), the app's are a short pipeline,
and :data:`STAGES` is the single table that maps one onto the other. A state
the table does not know is never silently dropped or renamed — it is reported
as ``unknown`` and counted, because a silent bucket is a lie about the total.

Nothing here reads a file, a clock or a config: it is a pure table plus two
formatters, so a wording test is a pure unit test.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

__all__ = [
    "AWAITING_STAGES",
    "KINDS",
    "KIND_QUARANTINED",
    "KIND_REQUIRES_DECISION",
    "KIND_REVISION_CAP",
    "KIND_SYSTEM_HALT",
    "STAGES",
    "STAGE_KEYS",
    "STAGE_UNKNOWN",
    "STATE_TO_STAGE",
    "codes",
    "kind_label",
    "pair",
    "stage_key",
    "stage_label",
    "stage_states",
    "stages_payload",
    "text",
    "text_en",
]


# --------------------------------------------------------------------------- #
# the pipeline table (engine state -> the app's stage)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Stage:
    """One pipeline stage the home screen shows, and the states inside it."""

    key: str
    states: Tuple[str, ...]
    ar: str
    en: str


#: **The stage-mapping table.** Ordered the way work flows, so the pipeline
#: reads left to right in the app. Every state the engine can write appears in
#: exactly one stage (asserted by the read tests), and an unlisted state falls
#: to :data:`STAGE_UNKNOWN` instead of vanishing.
STAGES: Tuple[Stage, ...] = (
    Stage("intake", ("draft",), "مسودة", "Draft"),
    Stage("working", ("in_progress", "needs_revision"), "قيد التنفيذ", "In progress"),
    Stage("review", ("in_review", "pending_qa", "pending_design"), "مراجعة داخلية", "Internal review"),
    Stage("human", ("pending_approval", "stalled"), "بانتظار اعتمادك", "Waiting for you"),
    Stage("approved", ("approved",), "معتمد", "Approved"),
    Stage("published", ("published",), "منشور", "Published"),
    Stage("halted", ("failed",), "متوقف", "Halted"),
    Stage("archived", ("archived",), "مؤرشف", "Archived"),
)

#: The stage of a state the table does not know (fail visible, never silent).
STAGE_UNKNOWN = Stage("unknown", (), "غير معروف", "Unknown")

STAGE_KEYS: Tuple[str, ...] = tuple(stage.key for stage in STAGES) + (STAGE_UNKNOWN.key,)

STATE_TO_STAGE: Mapping[str, str] = {
    state: stage.key for stage in STAGES for state in stage.states
}

_STAGE_BY_KEY: Mapping[str, Stage] = {stage.key: stage for stage in (*STAGES, STAGE_UNKNOWN)}

#: States whose stage means "a human has to act" — the engine's own attention
#: vocabulary (mirror of ``watcher``): at the gate, frozen at it, or halted.
AWAITING_STAGES: Tuple[str, ...] = ("human", "halted", "unknown")


def stage_key(state: Any) -> str:
    """The stage key for an engine state (``unknown`` when unlisted)."""
    if isinstance(state, str) and state in STATE_TO_STAGE:
        return STATE_TO_STAGE[state]
    return STAGE_UNKNOWN.key


def stage_label(state: Any) -> Tuple[str, str]:
    """``(arabic, english)`` label of the stage that holds ``state``."""
    stage = _STAGE_BY_KEY[stage_key(state)]
    return stage.ar, stage.en


def stage_states(key: str) -> Tuple[str, ...]:
    """The engine states inside a stage key (``()`` for ``unknown``)."""
    stage = _STAGE_BY_KEY.get(key)
    return stage.states if stage is not None else ()


def stages_payload() -> Tuple[Dict[str, Any], ...]:
    """The whole table in wire form — the pipeline the app draws."""
    return tuple(
        {
            "stage": stage.key,
            "label": stage.ar,
            "label_en": stage.en,
            "states": list(stage.states),
        }
        for stage in (*STAGES, STAGE_UNKNOWN)
    )


# --------------------------------------------------------------------------- #
# the attention kinds
# --------------------------------------------------------------------------- #

#: The four kinds of thing that need a human. The keys are the machine codes the
#: app switches on; ``watcher.py``'s own buckets are the same four ideas.
KIND_REQUIRES_DECISION = "requires_decision"
KIND_SYSTEM_HALT = "system_halt"
KIND_REVISION_CAP = "revision_cap"
KIND_QUARANTINED = "quarantined"

KINDS: Tuple[str, ...] = (
    KIND_REQUIRES_DECISION,
    KIND_SYSTEM_HALT,
    KIND_REVISION_CAP,
    KIND_QUARANTINED,
)


def kind_label(kind: str) -> Tuple[str, str]:
    """``(arabic, english)`` label for an attention kind."""
    ar, en = _TEXTS.get(f"kind.{kind}", (kind, kind))
    return ar, en


# --------------------------------------------------------------------------- #
# the texts
# --------------------------------------------------------------------------- #
#
# Every entry is ``code -> (arabic, english)``. ``{name}`` placeholders are
# filled by :func:`text` / :func:`text_en`; an unknown placeholder is left
# standing rather than raising, so a copy change can never 500 a read.

_TEXTS: Mapping[str, Tuple[str, str]] = {
    # --- attention kinds ---------------------------------------------------
    "kind.requires_decision": ("بانتظار قرارك", "Awaiting your decision"),
    "kind.system_halt": ("متوقف ويحتاج تدخلك", "Halted — needs you"),
    "kind.revision_cap": ("تجاوز حد التعديلات", "Revision cap exceeded"),
    "kind.quarantined": ("سجل غير قابل للقراءة", "Unreadable record"),
    # --- titles (common fields only) ---------------------------------------
    "title.requires_decision.capsule": (
        "شغلانة عند نقطة مراجعة",
        "Deliverable at a review gate",
    ),
    "title.requires_decision.ticket": ("طلب بانتظار قرار", "Request awaiting a decision"),
    "title.requires_decision.gate": ("بوابة بانتظار قرار", "Gate awaiting a decision"),
    "title.system_halt.capsule": ("شغلانة متوقفة", "Deliverable halted"),
    "title.revision_cap.capsule": (
        "شغلانة فوق حد التعديلات",
        "Deliverable over the revision cap",
    ),
    "title.quarantined.record": ("سجل غير قابل للقراءة", "Unreadable record"),
    # --- summaries (common fields only: sector, stage, revision position) ---
    "summary.capsule": (
        "قسم {domain} · {stage} · التعديلات {revision_count}/{revision_cap}",
        "{domain} · {stage} · revisions {revision_count}/{revision_cap}",
    ),
    "summary.capsule_no_revisions": ("قسم {domain} · {stage}", "{domain} · {stage}"),
    "summary.ticket": ("قسم {sector} · {stage}", "{sector} · {stage}"),
    "summary.gate": ("قسم {sector} · خطورة {risk_tier}", "{sector} · risk {risk_tier}"),
    "summary.quarantined": ("{kind}: {name}", "{kind}: {name}"),
    # --- reasons -----------------------------------------------------------
    "reason.requires_decision.capsule": (
        "الشغلانة عند نقطة مراجعة بشرية في حالة {state} وتنتظر قرارًا.",
        "The deliverable sits at a human gate in state {state} and waits for a decision.",
    ),
    "reason.requires_decision.ticket": (
        "الطلب في مرحلة {stage} وحالته {state} وينتظر قرارًا (قبول أو إعادة).",
        "The request is in the {stage} stage (state {state}) and awaits a decision "
        "(accept or send back).",
    ),
    "reason.requires_decision.gate": (
        "بوابة مفتوحة منذ {opened_at} وتنتظر قرارًا بشريًا.",
        "A gate opened at {opened_at} and waits for a human decision.",
    ),
    "reason.system_halt.capsule": (
        "الشغلانة متوقفة ({state}). آخر سبب مسجّل: {last_reason}",
        "The deliverable is halted ({state}). Last recorded reason: {last_reason}",
    ),
    "reason.system_halt.capsule_no_reason": (
        "الشغلانة متوقفة ({state}) ولا يوجد سبب مسجّل.",
        "The deliverable is halted ({state}); no reason was recorded.",
    ),
    "reason.revision_cap.capsule": (
        "عدد التعديلات {revision_count} فوق السقف {revision_cap} — القرار الآن للإنسان.",
        "Revisions {revision_count} are over the cap of {revision_cap} — the next "
        "decision is a human one.",
    ),
    "reason.quarantined.schema_version": (
        "نسخة مخطط هذا السجل أحدث من أن يقرأها المحرك — لم يُقرأ (فشل آمن).",
        "This record's schema version is newer than the engine understands — it was "
        "not read (fail closed).",
    ),
    "reason.quarantined.invalid_json": (
        "محتوى السجل غير قابل للتحليل (JSON غير صالح) — لم يُقرأ (فشل آمن).",
        "The record's content is unparseable (invalid JSON) — it was not read "
        "(fail closed).",
    ),
    "reason.quarantined.unreadable": (
        "السجل غير قابل للقراءة (محتوى أو صلاحيات) — لم يُقرأ (فشل آمن).",
        "The record is unreadable (content or permissions) — it was not read "
        "(fail closed).",
    ),
    # --- remedies ----------------------------------------------------------
    "remedy.review_deliverable": (
        "افتح المراجعة ثم اعتمد أو أعدْ التعديل بذكر السبب.",
        "Open the review, then approve or send it back with a reason.",
    ),
    "remedy.review_ticket": (
        "اقبل الطلب أو أعدْه للتعديل بذكر السبب.",
        "Accept the request, or send it back with a reason.",
    ),
    "remedy.review_gate": (
        "اعتمد البوابة أو ارفضها بذكر السبب.",
        "Approve the gate, or reject it with a reason.",
    ),
    "remedy.need_scoped_key": (
        "هذا القرار يحتاج صلاحية على هذا القسم — اطلب مفتاحًا مُقيدًا بالقسم.",
        "This decision needs a capability scoped to this sector — ask for a "
        "sector-scoped key.",
    ),
    "remedy.desktop_human_reset": (
        "هذه الحالة تُدار من سطح المكتب/الـ CLI (resurrect أو rework أو archive) "
        "ولا يوجد إجراء لها عبر الموبايل في هذه الشريحة.",
        "This state is managed from the desktop / CLI (resurrect, rework or archive); "
        "this slice's mobile contract offers no action for it.",
    ),
    "remedy.desktop_repair": (
        "أصلح الملف من سطح المكتب ثم أعد بناء الفهرس؛ لا يُعرض أي إجراء عبر الموبايل.",
        "Repair the file from the desktop and rebuild the index; no mobile action is "
        "offered for it.",
    ),
    # --- pagination / cursor refusals --------------------------------------
    "error.cursor": (
        "مؤشر الصفحة غير صالح — ابدأ من الصفحة الأولى.",
        "The page cursor is invalid — restart from the first page.",
    ),
}


def codes() -> Tuple[str, ...]:
    """Every machine code this module can resolve (for completeness tests)."""
    return tuple(sorted(_TEXTS))


def _render(template: str, params: Mapping[str, Any]) -> str:
    """Fill ``{name}`` placeholders, leaving an unknown name standing."""
    text = template
    for name, value in params.items():
        text = text.replace("{%s}" % name, "" if value is None else str(value))
    return text


def text(code: str, **params: Any) -> str:
    """The Arabic text for ``code`` (the default language)."""
    ar, _en = _TEXTS.get(code, (code, code))
    return _render(ar, params)


def text_en(code: str, **params: Any) -> str:
    """The English twin of :func:`text` — same code, same parameters."""
    _ar, en = _TEXTS.get(code, (code, code))
    return _render(en, params)


def pair(code: str, **params: Any) -> Tuple[str, str]:
    """``(arabic, english)`` for one code — the shape every response uses."""
    return text(code, **params), text_en(code, **params)
