#!/usr/bin/env python3
"""editorial.py — the agency's closed AI-slop vocabulary (a governance standard).

Two things must agree on what the agency refuses to publish:

- the deterministic ``qa_review`` gate (:mod:`capsule_engine.gates`), which
  rejects a deliverable before a human ever sees it;
- the marketing reviewer's prompt (``data.domains.marketing.gateway_ops``),
  which asks the model to enforce exactly the same vocabulary.

They bind the constants below, so there is one list and zero drift: a text this
gate rejects also fails the reviewer's contract, and vice versa.

Why the vocabulary lives in the kernel rather than with the branch that applies
it: ``qa_review`` is a *core* gate type — declared by the built-in gate template
(:data:`capsule_engine.policy.DEFAULT_GATE_TEMPLATE`) and applied to the
deliverables of every domain — so the standard it enforces is governance, not
branch content. The branch imports the kernel, never the other way round.

Nothing here knows about a domain: adding or removing a word is a policy change
to what the agency is willing to ship. When a registry exists for
domain-provided standards, this module is the natural injection point.

Standard library only; Python 3.10+.
"""

from __future__ import annotations

__all__ = [
    "BANNED_BUZZWORDS",
    "BANNED_PHRASES",
]

# AI slop the agency refuses to ship — matched as whole tokens.
BANNED_BUZZWORDS = frozenset(
    {
        "bespoke", "curated", "delve", "elevate", "embark", "empower",
        "foster", "holistic", "landscape", "leverage", "paradigm", "realm",
        "revolutionize", "seamless", "synergy", "tapestry", "transformative",
        "unleash", "unlock",
    }
)

# Multi-word slop — matched against the normalized text (hyphens kept).
BANNED_PHRASES = (
    "game-changer",
    "game changer",
    "in today's fast-paced",
    "it's not just",
)
