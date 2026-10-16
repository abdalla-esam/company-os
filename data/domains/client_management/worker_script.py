#!/usr/bin/env python3
"""worker_script.py — executes accepted tickets. Speaks the dispatcher handoff contract.

Roles: the agent classifies (agent_script.py §2); the worker executes.
done = a deliverable on disk (reversible artifact a human reviews).
The worker never reaches outside: irreversible/external/unclassifiable
intent -> needs_revision, fail-closed. Contract: architecture.md §6.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

from agent_script import classify  # single source of truth — no re-classification

HERE = os.path.dirname(os.path.abspath(__file__))
DELIVERABLES = os.path.join(HERE, "deliverables")

_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

from shared_kernel.atomic_io import atomic_text


def execute(payload):
    ticket_id = payload.get("ticket_id")
    request = payload.get("request")
    if not ticket_id or not isinstance(request, str) or not request.strip():
        return {"result": "needs_revision",
                "reason": "unworkable handoff: missing ticket_id or request"}

    tier = classify(request)
    if tier != "reversible":
        return {"result": "needs_revision",
                "reason": "outside worker territory: %s request — human execution required"
                          % (tier or "unclassifiable")}

    os.makedirs(DELIVERABLES, exist_ok=True)
    now = datetime.now(timezone.utc)
    name = "deliverable_%s_%s_%s.md" % (ticket_id, now.strftime("%Y%m%dT%H%M%SZ"),
                                        uuid.uuid4().hex[:8])
    path = os.path.join(DELIVERABLES, name)
    body_lines = [
        "# Deliverable for %s\n\n" % ticket_id,
        "- client: %s\n" % payload.get("client_id"),
        "- request: %s\n" % request,
    ]
    note = payload.get("revision_note")
    if note:
        body_lines.append(
            "- revision context: %s (by %s)\n"
            % (note.get("reason"), note.get("by"))
        )
    body_lines.append("- produced_at: %s\n\n" % now.isoformat())
    body_lines.append(
        "Draft produced by the %s worker (MVP).\n"
        "Human review before any external use — the worker never reaches outside.\n"
        % payload.get("sector")
    )
    atomic_text(path, "".join(body_lines), encoding="utf-8")
    return {"result": "done", "notes": "deliverable: %s" % name}


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"result": "needs_revision",
                          "reason": "invalid handoff JSON: %s" % exc}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"result": "needs_revision",
                          "reason": "invalid handoff: not a JSON object"}))
        return
    print(json.dumps(execute(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
