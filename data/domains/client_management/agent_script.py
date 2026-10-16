#!/usr/bin/env python3
"""client_management MVP — executes architecture.md exactly. Nothing more."""

import json
import sys

# Positive identification only — anything else is unclassifiable (fail closed).
IRREVERSIBLE_MARKERS = ("delete", "drop", "purge", "destroy", "erase", "wipe")
EXTERNAL_MARKERS = ("send ", "publish", "post ", "email", "campaign", "announce")
REVERSIBLE_MARKERS = ("draft", "analy", "list", "summar", "report",
                      "research", "writ", "review", "check", "find")


def classify(request):
    """-> 'irreversible' | 'external' | 'reversible' | None (unclassifiable)."""
    text = request.lower()
    if any(m in text for m in IRREVERSIBLE_MARKERS):
        return "irreversible"
    if any(m in text for m in EXTERNAL_MARKERS):
        return "external"
    if any(m in text for m in REVERSIBLE_MARKERS):
        return "reversible"
    return None


def decide(payload):
    """Rule order mirrors architecture.md §3."""
    if not isinstance(payload, dict):
        return {"status": "rejected", "risk_tier": None,
                "reason": "invalid_input: not a JSON object"}

    missing = [f for f in ("request", "client_id", "budget_cap")
               if f not in payload]
    if missing:
        return {"status": "rejected", "risk_tier": None,
                "reason": "invalid_input: missing " + ", ".join(missing)}

    if not isinstance(payload["budget_cap"], (int, float)) or payload["budget_cap"] < 0:
        return {"status": "rejected", "risk_tier": None,
                "reason": "invalid_budget: budget_cap must be >= 0"}

    # Rule 1: unclassifiable -> gate (fail closed)
    tier = classify(payload["request"]) if isinstance(payload["request"], str) else None
    if tier is None:
        return {"status": "needs_gate", "risk_tier": None,
                "reason": "unclassifiable: no rule matched"}

    # Rule 2: irreversible / external -> gate, always
    if tier in ("irreversible", "external"):
        return {"status": "needs_gate", "risk_tier": tier,
                "reason": "tier=%s always gated" % tier}

    # Rule 3: spend above cap -> gate
    spend = payload.get("estimated_spend", 0)
    if not isinstance(spend, (int, float)) or spend < 0:
        return {"status": "rejected", "risk_tier": tier,
                "reason": "invalid_budget: estimated_spend must be >= 0"}
    if spend > payload["budget_cap"]:
        return {"status": "needs_gate", "risk_tier": tier,
                "reason": "spend %s > cap %s" % (spend, payload["budget_cap"])}

    # Rule 4: accepted carries a reason too (no bare statuses)
    return {"status": "accepted", "risk_tier": tier,
            "reason": "reversible, spend within cap"}


def main():
    try:
        payload = json.load(sys.argv[1] if len(sys.argv) > 1 else sys.stdin)
    except (json.JSONDecodeError, IndexError, OSError) as exc:
        print(json.dumps({"status": "rejected", "risk_tier": None,
                          "reason": "invalid_input: %s" % exc}))
        return
    print(json.dumps(decide(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
