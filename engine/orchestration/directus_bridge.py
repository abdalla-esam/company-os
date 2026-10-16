#!/usr/bin/env python3
"""directus_bridge.py — Two-way synchronization bridge between Directus Kanban Studio and Sovereign Engine.

Functions:
1. Pushes auto-generated ideas from company-engine to Directus 'idea_backlog'.
2. Picks up 'approved_for_generation' cards from Directus, runs AI Copy & Design pipeline,
   saves assets to Zero-Waste Vault, and updates Directus card to 'under_review'.
3. Observes 'scheduled' / 'published' cards that name a capsule: such a card is
   at most a REQUEST that a human approve that capsule, so the bridge records
   the request (one log line) and mutates nothing — no state change, no
   manifest write, no publish. Approval stays a human decision (see
   orchestration/approval.py).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Domain packages live under ``data/``: ``data.domains.marketing.*`` needs the
# repo root importable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from workers.marketing import MarketingWorker
from workers.qa import QAWorker
from workers.design import DesignWorker
from data.domains.marketing.post_builder import IdeationSpec, assemble_post_capsule_manifest
from capsule_engine import spend_control
from capsule_engine.manifest import load_manifest, save_manifest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [directus-bridge] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("directus-bridge")

DIRECTUS_URL = os.getenv("DIRECTUS_INTERNAL_URL", "http://127.0.0.1:8055").rstrip("/")
ADMIN_EMAIL = os.getenv("DIRECTUS_ADMIN_EMAIL", "admin@agency.local")
ADMIN_PASSWORD = os.getenv("DIRECTUS_ADMIN_PASSWORD", "admin_secret_2026")
PUBLIC_BASE_URL = os.getenv("PUBLIC_API_URL", "http://127.0.0.1:8000").rstrip("/")
_def_spool = ROOT / "capsules" / "spool"
if not _def_spool.exists() and (ROOT / "capsules_spool").exists():
    _def_spool = ROOT / "capsules_spool"
SPOOL_DIR = Path(os.getenv("CAPSULES_SPOOL", _def_spool)).resolve()

#: Ledger identity of every Directus API call (spend_control, category tool).
#: Price: unlisted tools count at the conservative estimate from
#: ``config/engine.json -> spend.unpriced_estimate_usd`` — never $0, never
#: invisible. Add a flat USD entry for "directus-api" under ``spend.image_costs``
#: if this deployment's calls should be priced differently.
DIRECTUS_SERVICE = "directus-api"
DIRECTUS_PROVIDER = "directus"
DIRECTUS_DOMAIN = "operations"


class _MeteredCall:
    """authorize() before the HTTP request, record() after it — one call.

    The reservation is taken in :meth:`__enter__` (so a refusal propagates
    *before* any socket is opened) and settled in :meth:`__exit__`: a completed
    request succeeds, anything raised after authorization settles as failed at
    its estimate. The KILL SWITCH therefore blocks Directus traffic through the
    same choke point as every other spend path.
    """

    def __init__(self, op: str, *, service: str = DIRECTUS_SERVICE) -> None:
        self._op = op
        self._service = service
        self._reservation = None

    def __enter__(self):
        self._reservation = spend_control.authorize(
            model=self._service,
            category=spend_control.CATEGORY_TOOL,
            provider=DIRECTUS_PROVIDER,
            domain=DIRECTUS_DOMAIN,
            context={"op": self._op, "bridge": "directus"},
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        spend_control.record(
            self._reservation,
            status=(
                spend_control.STATUS_FAILED
                if exc_type is not None
                else spend_control.STATUS_SUCCEEDED
            ),
        )
        return False  # never swallow: refusals and failures keep propagating


class DirectusClient:
    def __init__(self, base_url: str = DIRECTUS_URL, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD):
        self.base_url = base_url
        self.email = email
        self.password = password
        self.token = ""

    def login(self) -> bool:
        try:
            payload = json.dumps({"email": self.email, "password": self.password}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/auth/login",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with _MeteredCall("directus.login"):
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            self.token = data.get("data", {}).get("access_token", "")
            return bool(self.token)
        except spend_control.SpendCapExceeded:
            raise  # the gate said stop — never degrade to "login failed"
        except Exception as e:
            logger.error("Directus login failed: %s", e)
            return False

    def request(self, path: str, method: str = "GET", data: dict | None = None) -> Any:
        if not self.token and not self.login():
            raise RuntimeError("Cannot authenticate with Directus")

        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        req_data = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

        try:
            with _MeteredCall(f"directus.{method.lower()}"):
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("data")
        except spend_control.SpendCapExceeded:
            raise  # the gate said stop: no retry loop, no silent skip
        except urllib.error.HTTPError as e:
            if e.code == 401:  # token expired, retry once
                if self.login():
                    headers["Authorization"] = f"Bearer {self.token}"
                    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
                    with _MeteredCall(f"directus.{method.lower()}.retry"):
                        with urllib.request.urlopen(req, timeout=15) as resp2:
                            retry_json = json.loads(resp2.read().decode("utf-8"))
                    return retry_json.get("data")
            logger.error("Directus HTTP %s on %s: %s", e.code, path, e.read().decode("utf-8"))
            raise

    def upload_file(self, file_path: Path | str, title: str = "") -> str | None:
        if not self.token and not self.login():
            raise RuntimeError("Cannot authenticate with Directus")

        p = Path(file_path)
        if not p.is_file():
            return None

        file_bytes = p.read_bytes()
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
        filename = p.name

        body_parts = [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: image/jpeg\r\n\r\n",
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(body_parts)

        url = f"{self.base_url}/files"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with _MeteredCall("directus.upload_file"):
                with urllib.request.urlopen(req, timeout=30) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
            return res_data.get("data", {}).get("id")
        except spend_control.SpendCapExceeded:
            raise  # the gate said stop — an upload is not optional to meter
        except Exception as exc:
            logger.error("Directus file upload error for %s: %s", filename, exc)
            return None


def sync_directus_cycle() -> dict[str, int]:
    """Execute one synchronization sweep between Directus Kanban and Company Engine."""
    # Cheap pre-flight (same one the beat uses): the KILL SWITCH stops the
    # sweep before a single login attempt. Per-call authorize() below still
    # gates every request individually if the flag flips mid-sweep.
    blocked = spend_control.blocked_reason()
    if blocked:
        logger.warning("Directus sync refused by the spend gate: %s", blocked)
        return {"produced": 0, "published": 0, "errors": 0}

    client = DirectusClient()
    stats = {"produced": 0, "published": 0, "errors": 0}

    try:
        posts = client.request("/items/posts?limit=100") or []
    except spend_control.SpendCapExceeded as exc:
        logger.warning("Directus sync refused by the spend gate: %s", exc)
        return stats
    except Exception as e:
        logger.error("Failed to fetch posts from Directus: %s", e)
        return stats

    for post in posts:
        post_id = post.get("id")
        status = post.get("status")
        topic = post.get("topic") or "منشور مؤسسي"
        angle = post.get("angle") or topic
        audience = post.get("audience") or "القادة التنفيذيون"
        channels = post.get("channels") or ["linkedin", "facebook", "x"]
        if isinstance(channels, str):
            channels = [ch.strip() for ch in channels.split(",") if ch.strip()]

        capsule_id = post.get("capsule_id")

        # CASE 1: Card moved to 'approved_for_generation' -> Run AI Factory
        if status == "approved_for_generation":
            logger.info("Processing approved card for generation: %s", topic)
            try:
                # 1. Assemble new capsule in spool
                spec = IdeationSpec(topic=topic, angle=angle, audience=audience, channels=channels)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                clean_topic = "".join(c for c in topic if c.isalnum() or c == "_")[:20]
                cid = f"cap_post_{ts}_{clean_topic}" if clean_topic else f"cap_post_{ts}"
                
                manifest_data = assemble_post_capsule_manifest(cid, spec)
                cap_dir = SPOOL_DIR / cid
                cap_dir.mkdir(parents=True, exist_ok=True)
                save_manifest(cap_dir, manifest_data)

                # 2. Run pipeline stages
                m_res = MarketingWorker().run(str(cap_dir))
                q1_res = QAWorker().run(str(cap_dir))
                d_res = DesignWorker().run(str(cap_dir))
                q2_res = QAWorker().run(str(cap_dir))

                final_man = load_manifest(cap_dir)
                meta = final_man.get("metadata", {})
                usage = meta.get("usage", {})
                cost = meta.get("cost", {})

                # Read copy variants
                c_master = (cap_dir / "copy.md").read_text(encoding="utf-8") if (cap_dir / "copy.md").exists() else ""
                c_li = (cap_dir / "copy_linkedin.md").read_text(encoding="utf-8") if (cap_dir / "copy_linkedin.md").exists() else c_master
                c_fb = (cap_dir / "copy_facebook.md").read_text(encoding="utf-8") if (cap_dir / "copy_facebook.md").exists() else c_master
                c_x = (cap_dir / "copy_x.md").read_text(encoding="utf-8") if (cap_dir / "copy_x.md").exists() else c_master
                prompt_txt = (cap_dir / "image_prompt.txt").read_text(encoding="utf-8") if (cap_dir / "image_prompt.txt").exists() else ""

                # Upload image to Directus Files
                img_file = cap_dir / "image.jpg"
                directus_img_id = client.upload_file(img_file, title=topic) if img_file.is_file() else None

                # Update Directus card
                update_payload = {
                    "status": "under_review",
                    "capsule_id": cid,
                    "copy_master": c_master,
                    "copy_linkedin": c_li,
                    "copy_facebook": c_fb,
                    "copy_x": c_x,
                    "visual_prompt": prompt_txt,
                    "image": directus_img_id,
                    "image_url": f"{PUBLIC_BASE_URL}/api/capsules/{cid}/image",
                    "tokens_used": usage.get("total_tokens", 0),
                    "cost_usd": cost.get("image_generation_usd", 0.0),
                }
                client.request(f"/items/posts/{post_id}", "PATCH", update_payload)
                stats["produced"] += 1
                logger.info("Card %s transitioned to 'under_review' with generated assets and image %s!", topic, directus_img_id)

            except spend_control.SpendCapExceeded as exc:
                logger.warning(
                    "Directus sync stopped by the spend gate mid-sweep: %s", exc
                )
                return stats
            except Exception as exc:
                logger.error("Failed to generate content for card %s: %s", post_id, exc)
                stats["errors"] += 1

        # CASE 2: A card in 'scheduled' / 'published' that carries a capsule_id
        # is, at most, a REQUEST that a human approve that capsule through the
        # normal path — it is never itself an approval. This bridge holds no
        # approval authority: it must not change a capsule's state, write its
        # manifest, or publish it (and it must not write back to Directus
        # either). Anything that moves capsule state belongs to the approval
        # service (human gate + reviewed-hash attestation) and the capsule
        # pulse, never to an external card status. The capsule is left exactly
        # as it is; the request is recorded by one log line.
        elif status in ("scheduled", "published") and capsule_id:
            cap_dir = SPOOL_DIR / capsule_id
            if cap_dir.exists():
                try:
                    # Read-only inspection: the manifest is never written here.
                    state = load_manifest(cap_dir).get("state")
                except Exception as exc:
                    logger.error(
                        "Directus card %s -> capsule %s: manifest unreadable: %s",
                        post_id, capsule_id, exc,
                    )
                    stats["errors"] += 1
                else:
                    if state in ("pending_approval", "in_progress"):
                        logger.info(
                            "directus approval request ignored: approval requires "
                            "a human decision (card_id=%s capsule_id=%s "
                            "state_seen=%s)",
                            post_id, capsule_id, state,
                        )

    return stats


if __name__ == "__main__":
    logger.info("Running Directus sync sweep...")
    res = sync_directus_cycle()
    logger.info("Sweep finished: %s", res)
