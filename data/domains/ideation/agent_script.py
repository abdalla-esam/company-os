#!/usr/bin/env python3
"""agent_script.py — Ideation Agent MVP.

Executes architecture.md exactly: classifies raw idea requests, evaluates risk
tiers, and routes to accepted (autonomous internal drafting) or needs_gate.
Standard library only; zero external dependencies.
"""

from __future__ import annotations

import json
import sys

IRREVERSIBLE_MARKERS = (
    # English
    "delete", "purge", "destroy", "erase", "wipe",
    # Arabic
    "حذف", "إلغاء", "تدمير", "مسح", "شطب",
)

EXTERNAL_MARKERS = (
    # English
    "paid ad", "campaign launch", "ad spend", "broadcast", "press release",
    "external sponsor", "commit budget", "launch campaign", "go live now",
    # Arabic
    "إعلان ممول", "إطلاق حملة", "حملة إعلانية", "صرف ميزانية",
    "بيان صحفي", "رعاية خارجية", "نشر مباشر",
)

REVERSIBLE_MARKERS = (
    # English
    "idea", "ideat", "concept", "angle", "pitch", "brainstorm", "topic",
    "draft", "suggest", "propose", "outline", "theme", "hook", "story",
    "brief", "content plan", "trend", "write", "headline",
    # Arabic
    "فكرة", "أفكار", "زاوية", "محور", "مفهوم", "اقتراح", "مسودة",
    "موضوع", "عصف ذهني", "طرح", "خطة محتوى", "سيناريو", "صياغة",
    "مبادرة", "اتجاه", "تريند", "عنوان", "مقال", "منشور",
)


def classify(request: str) -> str | None:
    """Classify the idea request into a risk tier, or None (unclassifiable)."""
    if not isinstance(request, str):
        return None
    text = request.lower()
    if any(m in text for m in IRREVERSIBLE_MARKERS):
        return "irreversible"
    if any(m in text for m in EXTERNAL_MARKERS):
        return "external"
    if any(m in text for m in REVERSIBLE_MARKERS):
        return "reversible"
    return None


def decide(payload: dict) -> dict:
    """Evaluate inbound request according to architecture.md §3."""
    if not isinstance(payload, dict):
        return {
            "status": "rejected",
            "risk_tier": None,
            "reason": "invalid_input: not a JSON object",
        }

    missing = [f for f in ("request", "client_id", "budget_cap") if f not in payload]
    if missing:
        return {
            "status": "rejected",
            "risk_tier": None,
            "reason": "invalid_input: missing " + ", ".join(missing),
        }

    budget_cap = payload["budget_cap"]
    if not isinstance(budget_cap, (int, float)) or budget_cap < 0:
        return {
            "status": "rejected",
            "risk_tier": None,
            "reason": "invalid_budget: budget_cap must be >= 0",
        }

    # Rule 1: unclassifiable -> gate (fail closed)
    tier = classify(payload["request"])
    if tier is None:
        return {
            "status": "needs_gate",
            "risk_tier": None,
            "reason": "unclassifiable: no rule matched",
        }

    # Rule 2 & 3: irreversible / external -> gate
    if tier in ("irreversible", "external"):
        return {
            "status": "needs_gate",
            "risk_tier": tier,
            "reason": f"tier={tier} always gated",
        }

    # Rule 5: spend above budget cap -> gate
    spend = payload.get("estimated_spend", 0)
    if not isinstance(spend, (int, float)) or spend < 0:
        return {
            "status": "rejected",
            "risk_tier": tier,
            "reason": "invalid_budget: estimated_spend must be >= 0",
        }
    if spend > budget_cap:
        return {
            "status": "needs_gate",
            "risk_tier": tier,
            "reason": f"spend {spend} > cap {budget_cap}",
        }

    # Rule 4: internal ideation drafts are reversible -> accepted
    return {
        "status": "accepted",
        "risk_tier": tier,
        "reason": "reversible ideation concept, spend within cap",
    }


def main() -> None:
    try:
        payload = json.load(sys.argv[1] if len(sys.argv) > 1 else sys.stdin)
    except (json.JSONDecodeError, IndexError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "risk_tier": None,
                    "reason": f"invalid_input: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return
    print(json.dumps(decide(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
