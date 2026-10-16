#!/usr/bin/env python3
"""worker_script.py — Design worker for Sovereign Factory.

Generates AI imagery based on marketing copy deliverables.
Handoff: returns pending_approval for Human-In-The-Loop review.
"""

import json
import os
import sys

import dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))

_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

# The repo root too: the shared marketing operations (image_prompt) and the
# gated image fetchers live in importable packages.
_REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared_kernel.atomic_io import atomic_bytes

# Session C: every paid call goes through the gated paths — the model gateway
# (authorize before HTTP, record after) and the spend-gated image fetchers in
# workers.design (fetch_huggingface / fetch_pollinations, both behind
# spend_control.authorize). No direct Groq or Pollinations calls remain here.
from capsule_engine.gateway import (
    GatewayConfigError,
    GatewayError,
    SpendCapExceeded,
    default_gateway,
)
from data.domains.marketing.gateway_ops import image_prompt, prepare_gateway_env
from workers.design import fetch_free_image


def execute(payload):
    # 1. Load context and deliverable_dir (Fail-Closed)
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    deliverable_dir_raw = (context.get("deliverable_dir") or "").strip()
    deliverable_file_raw = (context.get("deliverable") or "").strip()

    deliverable_dir = None
    if deliverable_dir_raw:
        if os.path.isdir(deliverable_dir_raw):
            deliverable_dir = deliverable_dir_raw
        elif os.path.isfile(deliverable_dir_raw):
            deliverable_dir = os.path.dirname(deliverable_dir_raw)
        elif "domains/marketing/deliverables" in deliverable_dir_raw:
            rel = deliverable_dir_raw.split("domains/marketing/deliverables", 1)[1].strip("/\\")
            cand = os.path.join(ROOT_DIR, "domains", "marketing", "deliverables", rel)
            if os.path.isdir(cand):
                deliverable_dir = cand
            elif os.path.isfile(cand):
                deliverable_dir = os.path.dirname(cand)

    if not deliverable_dir and deliverable_file_raw:
        if os.path.isfile(deliverable_file_raw):
            deliverable_dir = os.path.dirname(deliverable_file_raw)
        elif "domains/marketing/deliverables" in deliverable_file_raw:
            rel = deliverable_file_raw.split("domains/marketing/deliverables", 1)[1].strip("/\\")
            cand = os.path.join(ROOT_DIR, "domains", "marketing", "deliverables", rel)
            if os.path.isfile(cand):
                deliverable_dir = os.path.dirname(cand)

    if not deliverable_dir or not os.path.isdir(deliverable_dir):
        return {
            "result": "needs_revision",
            "reason": f"Missing or invalid deliverable_dir: {deliverable_dir_raw or deliverable_file_raw}"
        }

    copy_path = os.path.join(deliverable_dir, "copy.md")
    if not os.path.isfile(copy_path):
        return {
            "result": "needs_revision",
            "reason": f"copy.md not found in deliverable_dir: {deliverable_dir}"
        }

    try:
        with open(copy_path, "r", encoding="utf-8") as fh:
            copy_text = fh.read().strip()
    except Exception as exc:
        return {
            "result": "needs_revision",
            "reason": f"Failed to read copy.md: {exc}"
        }

    # 2. Step 1 (image prompt via the gated model gateway)
    #    data.domains.marketing.gateway_ops.image_prompt is the one prompt-
    #    engineering operation in the system: it calls the gateway's spend-
    #    authorised `_call_llm` seam and enforces the sub-60-word English
    #    contract itself, so a non-compliant answer can never reach the image
    #    endpoint.
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

    # Bridge the legacy sector credentials onto the gateway (shared with the
    # marketing worker: same mapping, same fail-closed rules).
    prepare_gateway_env()
    try:
        gateway = default_gateway()
        visual_prompt = image_prompt(gateway, copy_text)
    except SpendCapExceeded as exc:
        return {
            "result": "needs_revision",
            "reason": f"spend gate refused the prompt call (no request was sent): {exc}"
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
            "reason": f"image prompt generation failed: {exc}"
        }
    except Exception as exc:
        return {
            "result": "needs_revision",
            "reason": f"image prompt generation failed: {exc}"
        }

    # 3. Step 2 (image generation through the gated fetchers)
    #    workers.design.fetch_free_image -> fetch_huggingface (paid, priced)
    #    with fetch_pollinations (free) as fallback; both authorise with
    #    spend_control BEFORE the request, and a refusal never falls through
    #    to a second provider.
    try:
        image_bytes, _content_type = fetch_free_image(visual_prompt, 45.0)
    except SpendCapExceeded as exc:
        return {
            "result": "needs_revision",
            "reason": f"spend gate refused image generation (no request was sent): {exc}"
        }
    except Exception as exc:
        return {
            "result": "needs_revision",
            "reason": f"Image generation failed: {exc}"
        }

    # 4. Step 3 (Save clean image.jpg in deliverable_dir without watermark)
    image_path = os.path.join(deliverable_dir, "image.jpg")
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        # Auto-remove watermark by trimming bottom margin and scaling cleanly
        crop_bottom = int(h * 0.05) if h > 200 else 45
        cropped = img.crop((0, 0, w, h - crop_bottom))
        clean_img = cropped.resize((w, h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        clean_img.save(buf, "JPEG", quality=95)
        atomic_bytes(image_path, buf.getvalue())
    except Exception:
        # Fallback to direct bytes if PIL encounters any issue
        try:
            atomic_bytes(image_path, image_bytes)
        except Exception as exc:
            return {
                "result": "needs_revision",
                "reason": f"Failed to save image.jpg: {exc}"
            }

    # 5. Step 4 (Handoff: pending_approval for HITL review)
    return {
        "result": "pending_approval",
        "notes": "Image and copy ready for human review.",
        "context": {
            "deliverable_dir": os.path.abspath(deliverable_dir)
        }
    }


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"result": "needs_revision", "reason": f"invalid handoff JSON: {exc}"}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"result": "needs_revision", "reason": "invalid handoff: not a JSON object"}))
        return
    print(json.dumps(execute(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
