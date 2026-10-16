#!/usr/bin/env python3
"""worker_script.py — executes approved marketing tickets. Speaks the dispatcher handoff contract.

Marketing's bounded law (architecture.md §6): producing publish copy is
reversible craft; publishing itself stays human. External copy is
drafted ONLY with proven gate approval (`via`) — the governance chain
is verified at the execution boundary, not assumed.
Irreversible/unclassifiable intent -> needs_revision, fail-closed.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import dotenv

from agent_script import classify  # single source of truth — no re-classification

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
DELIVERABLES = os.path.join(HERE, "deliverables")

_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

# The repo root too: this worker shares the gated gateway operations with the
# rest of the system via ``data.domains.marketing.gateway_ops``.
_REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared_kernel.atomic_io import atomic_text

# The single spend-gated model seam (capsule_engine.gateway): authorize() runs
# before the request leaves the machine and record() settles it, so this worker
# can no longer spend outside the ledger (Session C: no direct Groq calls).
from capsule_engine.gateway import (
    GatewayConfigError,
    GatewayError,
    SpendCapExceeded,
    default_gateway,
)
from data.domains.marketing.gateway_ops import prepare_gateway_env

COPY_SYSTEM_NOTE = (
    "You are an expert, highly concise marketing copywriter. Your job is to "
    "output ONLY the final requested copy formatted in clean Markdown. Do NOT "
    "include conversational filler, pleasantries, or meta-commentary."
)


def execute(payload):
    # 1. Load .env and confirm a model credential exists (Fail-Closed Rule):
    #    the legacy GROQ_API_KEY, the gateway's own AGENCY_LLM_API_KEY /
    #    OPENAI_API_KEY, or an explicit offline mode — nothing else is a key.
    dotenv_path = os.path.join(ROOT_DIR, ".env")
    dotenv.load_dotenv(dotenv_path)

    groq_api_key = os.environ.get("GROQ_API_KEY")
    gateway_key = os.environ.get("AGENCY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    offline = (os.environ.get("AGENCY_OFFLINE_MODE") or "").strip().lower() in (
        "1", "true", "yes", "on", "offline"
    )
    if not offline and not groq_api_key and not gateway_key:
        return {
            "result": "needs_revision",
            "reason": "GROQ_API_KEY is missing from environment"
        }

    ticket_id = payload.get("ticket_id")
    request = payload.get("request")
    if not ticket_id or not isinstance(request, str) or not request.strip():
        return {"result": "needs_revision",
                "reason": "unworkable handoff: missing ticket_id or request"}

    tier = classify(request)
    if tier == "irreversible" or tier is None:
        return {"result": "needs_revision",
                "reason": "outside worker territory: %s request — human execution required"
                          % (tier or "unclassifiable")}
    if tier == "external" and not payload.get("via"):
        return {"result": "needs_revision",
                "reason": "external copy without a gate approval (via missing) — raise a gate first"}

    # 2. Copy generation through the gated model gateway (authorize -> HTTP ->
    #    record). A spend refusal (kill switch, caps, unusable ledger) happens
    #    BEFORE any request leaves the machine and fails the handoff closed.
    prepare_gateway_env()
    manifest = {
        "domain": "marketing",
        "metadata": {"topic": str(ticket_id), "target_platform": "social"},
    }
    prompt = f"{COPY_SYSTEM_NOTE}\n\n{request}"
    try:
        gateway = default_gateway()
        generated_text = gateway.complete(manifest, prompt).strip()
    except SpendCapExceeded as exc:
        return {
            "result": "needs_revision",
            "reason": f"spend gate refused the copy call (no request was sent): {exc}"
        }
    except GatewayConfigError as exc:
        return {
            "result": "needs_revision",
            "reason": (
                "model gateway is not configured: %s (set GROQ_API_KEY or "
                "AGENCY_LLM_API_KEY, or enable AGENCY_OFFLINE_MODE)" % exc
            )
        }
    except GatewayError as exc:
        return {
            "result": "needs_revision",
            "reason": f"model gateway copy call failed: {exc}"
        }

    # 3. Save Deliverable & Handoff (Agency OS structure)
    client_id = payload.get("client_id") or "default"
    post_dir = os.path.join(DELIVERABLES, str(client_id), f"post_{ticket_id}")
    os.makedirs(post_dir, exist_ok=True)
    copy_path = os.path.join(post_dir, "copy.md")
    try:
        atomic_text(copy_path, generated_text + "\n", encoding="utf-8")
    except Exception as exc:
        return {
            "result": "needs_revision",
            "reason": f"Failed to write deliverable file: {exc}"
        }

    return {
        "result": "done",
        "notes": "Copy written.",
        "context": {
            "deliverable_dir": os.path.abspath(post_dir)
        }
    }


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
