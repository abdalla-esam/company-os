#!/usr/bin/env python3
"""worker_script.py — Operations worker for Sovereign Factory.

Handles publishing and operations actions for tickets.
Bulletproof against path mismatches (Mac vs Linux), CWD differences,
and stdin handoff variations.
"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

# Session P0: delivery no longer opens its own socket to the n8n webhook.
# Publishing is delegated to the single governed Tool Bridge pathway — the
# ``social:publish`` gear — whose endpoint/token come from config/engine.json
# ("gears") or the AGENCY_BRIDGE_URL* environment. The bridge authorizes with
# spend_control BEFORE any socket opens, so the KILL SWITCH and the caps block
# delivery with zero bytes on the wire (FOUNDATION.md, ثابت 2 & 6).



def main():
    # 1. Safe Input Loading (argv[1] file or string, or stdin)
    payload = None
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if os.path.isfile(arg):
            try:
                with open(arg, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception as exc:
                print(json.dumps({"result": "needs_revision", "reason": f"Failed to parse file payload: {exc}"}))
                return
        else:
            try:
                payload = json.loads(arg)
            except Exception as exc:
                print(json.dumps({"result": "needs_revision", "reason": f"Failed to parse argv payload: {exc}"}))
                return
    else:
        try:
            raw_input = sys.stdin.read().strip()
            if not raw_input:
                print(json.dumps({"result": "needs_revision", "reason": "Empty stdin input"}))
                return
            payload = json.loads(raw_input)
        except Exception as exc:
            print(json.dumps({"result": "needs_revision", "reason": f"Failed to parse stdin payload: {exc}"}))
            return

    if not isinstance(payload, dict):
        print(json.dumps({"result": "needs_revision", "reason": "Payload must be a JSON object"}))
        return

    ticket_id = payload.get("ticket_id", "unknown")

    # 2. Robust Deliverable Extraction (Agency OS deliverable_dir & legacy fallback)
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    deliverable_dir_raw = (context.get("deliverable_dir") or "").strip()
    deliverable_path_raw = (context.get("deliverable") or "").strip()

    if not deliverable_dir_raw and not deliverable_path_raw:
        request_text = payload.get("request") or ""
        if "Deliverable Reference:" in request_text:
            part = request_text.split("Deliverable Reference:", 1)[1]
            deliverable_path_raw = part.split("]", 1)[0].strip()

    if not deliverable_dir_raw and not deliverable_path_raw:
        print(json.dumps({
            "result": "needs_revision",
            "reason": "Missing deliverable path or deliverable_dir in context"
        }))
        return

    # 3. Path Resolution (Cross-OS, CWD-independent)
    here = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.normpath(os.path.join(here, "..", ".."))
    marketing_deliv_dir = os.path.join(root_dir, "domains", "marketing", "deliverables")

    target_file = None

    # Priority 1: Check deliverable_dir / copy.md
    if deliverable_dir_raw:
        cand_copy = os.path.join(deliverable_dir_raw, "copy.md")
        if os.path.isfile(cand_copy):
            target_file = cand_copy
        elif os.path.isfile(deliverable_dir_raw):
            target_file = deliverable_dir_raw
        elif "domains/marketing/deliverables" in deliverable_dir_raw:
            rel = deliverable_dir_raw.split("domains/marketing/deliverables", 1)[1].strip("/\\")
            cand_copy = os.path.join(marketing_deliv_dir, rel, "copy.md")
            if os.path.isfile(cand_copy):
                target_file = cand_copy
            elif os.path.isfile(os.path.join(marketing_deliv_dir, rel)):
                target_file = os.path.join(marketing_deliv_dir, rel)

    # Priority 2: Fallback to deliverable_path_raw
    if not target_file and deliverable_path_raw:
        if os.path.isfile(deliverable_path_raw):
            target_file = deliverable_path_raw
        elif "domains/marketing/deliverables" in deliverable_path_raw:
            rel = deliverable_path_raw.split("domains/marketing/deliverables", 1)[1].strip("/\\")
            cand = os.path.join(marketing_deliv_dir, rel)
            if os.path.isfile(cand):
                target_file = cand
        else:
            filename = os.path.basename(deliverable_path_raw)
            cand = os.path.join(marketing_deliv_dir, filename)
            if os.path.isfile(cand):
                target_file = cand

    if not target_file or not os.path.isfile(target_file):
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"Deliverable file not found. Checked deliverable_dir='{deliverable_dir_raw}', deliverable='{deliverable_path_raw}'"
        }))
        return

    # 4. Read File Content & Base64 Image if present
    try:
        with open(target_file, "r", encoding="utf-8") as fh:
            content = fh.read()
    except Exception as exc:
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"Failed to read file: {exc}"
        }))
        return

    # Check for image.jpg in the deliverable directory
    image_base64_string = None
    target_dir = os.path.dirname(target_file)
    cand_img = os.path.join(target_dir, "image.jpg")
    if os.path.isfile(cand_img):
        try:
            with open(cand_img, "rb") as img_fh:
                image_bytes = img_fh.read()
            image_base64_string = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as exc:
            print(json.dumps({
                "result": "needs_revision",
                "reason": f"Failed to read and encode image.jpg: {exc}"
            }))
            return

    # 5. Execution — one governed publish pathway (the Tool Bridge seam).
    #    Nothing opens a socket here: default_bridge() resolves the
    #    ``social:publish`` gear from config/engine.json ("gears") or the
    #    AGENCY_BRIDGE_URL* environment, and the bridge authorizes with
    #    spend_control before the request leaves the machine. The KILL SWITCH
    #    therefore blocks delivery with zero bytes on the wire, and the ledger
    #    records the publish reservation/settlement (category "publish", $0).
    from capsule_engine.bridge import (
        BridgeConfigError,
        BridgeError,
        BridgeRefusalError,
        default_bridge,
    )
    from capsule_engine.gateway import SpendCapExceeded

    channel = str(
        context.get("channel") or payload.get("channel") or "operations"
    ).strip() or "operations"

    publish_input = {"channel": channel, "text": content}
    if image_base64_string:
        image_bytes = base64.b64decode(image_base64_string)
        publish_input["media"] = {
            "mime": "image/jpeg",
            "base64": image_base64_string,
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        }

    # The capsule's declared seat for this delivery; the branch is the
    # operations domain, so its per-domain cap and ledger attribution apply.
    capsule_context = {"id": str(ticket_id), "state": "approved", "domain": "operations"}
    governance_context = {"domain": "operations", "autonomy_level": "L1"}

    try:
        answer = default_bridge("social:publish").invoke(
            publish_input,
            gear="social:publish",
            capsule=capsule_context,
            governance=governance_context,
        ).require_ok()
    except SpendCapExceeded as exc:
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"spend gate refused publishing (no request was sent): {exc}"
        }))
        return
    except BridgeConfigError as exc:
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"delivery gear is not configured: {exc}"
        }))
        return
    except BridgeRefusalError as exc:
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"delivery gear refused publishing (code={exc.code}): {exc.detail}"
        }))
        return
    except BridgeError as exc:
        print(json.dumps({
            "result": "needs_revision",
            "reason": f"delivery gear failed: {exc}"
        }))
        return

    # 6. Strict Success Schema — Agency OS published state
    now_iso = datetime.now(timezone.utc).isoformat()
    print(json.dumps({
        "result": "published",
        "notes": "Published successfully through the social:publish gear.",
        "published_at": now_iso,
        "receipt_id": answer.output.get("receipt_id")
    }))


if __name__ == "__main__":
    main()