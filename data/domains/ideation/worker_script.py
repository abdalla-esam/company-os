# worker_script.py — executes accepted ideation tickets. Speaks the dispatcher handoff contract.
#
# The Idea-to-Capsule Bridge with:
# 1. Monthly Archive Memory Reader:
#    Scans the repository's past archives, spool backups, and historical tickets to
#    extract previous topics and occasions, preventing repetition and providing context.
# 2. Smart Scheduling Engine:
#    Differentiates between Standard Weekly Posts (mode: queue / regular cadence)
#    and Occasion / Event Posts (mode: strict / exact scheduled time window).


from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
DELIVERABLES = os.path.join(HERE, "deliverables")
SPOOL_DIR = os.path.join(ROOT_DIR, "capsules", "spool") if os.path.isdir(os.path.join(ROOT_DIR, "capsules", "spool")) else os.path.join(ROOT_DIR, "capsules_spool")
ARCHIVE_ROOT = os.path.join(ROOT_DIR, "archive")
DOMAINS_ROOT = os.path.join(ROOT_DIR, "domains")
TICKETS_DIR = os.path.join(HERE, "tickets")

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from agent_script import classify  # single source of truth — no re-classification

_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

from capsule_engine import State, now_iso, parse_ts, save_manifest
from shared_kernel.atomic_io import atomic_text


# Common occasion keywords in Arabic & English
OCCASION_PATTERNS = [
    (r"(6\s*أكتوبر|نصر\s*أكتوبر|حرب\s*أكتوبر)", "ذكرى نصر 6 أكتوبر المجيد"),
    (r"(يوم\s*التأسيس)", "يوم التأسيس السعودي"),
    (r"(اليوم\s*الوطني)", "اليوم الوطني"),
    (r"(عيد\s*الفطر)", "عيد الفطر المبارك"),
    (r"(عيد\s*الأضحى)", "عيد الأضحى المبارك"),
    (r"(رمضان)", "شهر رمضان المبارك"),
    (r"(رأس\s*السنة|سنة\s*جديدة|عام\s*جديد)", "رأس السنة الميلادية"),
    (r"(يوم\s*المرأة|يوم\s*الأم)", "مناسبة خاصة"),
    (r"(يوم\s*الشهيد)", "يوم الشهيد"),
    (r"(ذكرى|مناسبة|احتفال|حدث\s*سنوي|anniversary|holiday|national\s*day)", "مناسبة سنوية"),
]


def detect_occasion(text: str) -> Optional[str]:
    """Detect if the idea text references a known occasion or event."""
    if not isinstance(text, str):
        return None
    for pattern, name in OCCASION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return None


# ==============================================================================
# 1. Monthly Archive Memory Reader
# ==============================================================================

def read_archive_memory(limit: int = 25) -> List[Dict[str, Any]]:
    """Scan historical archives and extract previous topics, dates, and occasions.

    Walks:
    1. The root `archive/` (spool backups, reusable master posts)
    2. Any domain monthly archives (`domains/*/archive/<YYYY-MM>/`)
    3. Current spool (`capsules_spool/`)
    4. Pending and active tickets in `domains/ideation/tickets/`

    Returns a deduplicated, sorted list of memory entries (newest first).
    """
    memory_entries: List[Dict[str, Any]] = []
    seen_topics = set()

    def _add_entry(topic: str, occasion: Optional[str], date_str: Optional[str],
                   source_id: str, platform: Optional[str]) -> None:
        clean_topic = topic.strip()
        if not clean_topic or clean_topic.lower() in seen_topics:
            return
        # Skip pure test dummy strings
        if clean_topic.startswith("mkdir -p"):
            return
        seen_topics.add(clean_topic.lower())
        dt = parse_ts(date_str) if date_str else None
        memory_entries.append({
            "topic": clean_topic,
            "occasion": occasion.strip() if occasion else None,
            "date": dt.isoformat() if dt else (date_str or now_iso()),
            "timestamp": dt.timestamp() if dt else 0.0,
            "source_id": source_id,
            "platform": platform or "facebook",
        })

    # 1. Walk root archive/ directory
    if os.path.isdir(ARCHIVE_ROOT):
        for root, _dirs, files in os.walk(ARCHIVE_ROOT):
            if "manifest.json" in files:
                path = os.path.join(root, "manifest.json")
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    meta = data.get("metadata", {})
                    topic = meta.get("topic") or data.get("raw_prompt", "").split("\n")[0]
                    occasion = meta.get("occasion")
                    date_val = data.get("published_at") or data.get("created_at")
                    _add_entry(
                        topic=topic,
                        occasion=occasion,
                        date_str=date_val,
                        source_id=data.get("capsule_id", os.path.basename(root)),
                        platform=meta.get("target_platform"),
                    )
                except Exception:
                    continue

    # 2. Walk domains/*/archive/
    if os.path.isdir(DOMAINS_ROOT):
        for root, _dirs, files in os.walk(DOMAINS_ROOT):
            if "archive" in root.split(os.sep):
                for name in files:
                    if name.endswith(".json"):
                        path = os.path.join(root, name)
                        try:
                            with open(path, encoding="utf-8") as fh:
                                data = json.load(fh)
                            req = data.get("request", "")
                            topic = req.split("\n")[0] if req else ""
                            date_val = data.get("done_at") or data.get("created_at")
                            _add_entry(
                                topic=topic,
                                occasion=None,
                                date_str=date_val,
                                source_id=data.get("ticket_id", name),
                                platform="facebook",
                            )
                        except Exception:
                            continue

    # 3. Walk active spool directory for recent topics in progress
    if os.path.isdir(SPOOL_DIR):
        for entry in sorted(os.listdir(SPOOL_DIR)):
            c_dir = os.path.join(SPOOL_DIR, entry)
            m_path = os.path.join(c_dir, "manifest.json")
            if os.path.isfile(m_path):
                try:
                    with open(m_path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    meta = data.get("metadata", {})
                    topic = meta.get("topic") or data.get("raw_prompt", "").split("\n")[0]
                    occasion = meta.get("occasion")
                    date_val = data.get("created_at")
                    _add_entry(
                        topic=topic,
                        occasion=occasion,
                        date_str=date_val,
                        source_id=data.get("capsule_id", entry),
                        platform=meta.get("target_platform"),
                    )
                except Exception:
                    continue

    # 4. Walk pending and active tickets in domains/ideation/tickets/
    if os.path.isdir(TICKETS_DIR):
        for name in sorted(os.listdir(TICKETS_DIR)):
            if name.endswith(".json"):
                path = os.path.join(TICKETS_DIR, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    req = data.get("request", "")
                    topic = data.get("context", {}).get("topic") or data.get("topic")
                    if not topic and req:
                        for line in req.split("\n"):
                            line_s = line.strip()
                            if line_s.startswith(("[Proposed Title]:", "[Title]:", "[العنوان المقترح]:")):
                                topic = re.sub(r"^\[.*?\]:\s*", "", line_s).strip()
                                break
                        if not topic:
                            topic = req.split("\n")[0].strip()

                    date_val = data.get("created_at") or data.get("done_at")
                    platform_val = (
                        data.get("target_platform")
                        or data.get("context", {}).get("platform")
                    )
                    if not platform_val and req and any(k in req for k in ("[Target Platform]:", "[Platform]:", "[المنصة المستهدفة]:")):
                        for line in req.split("\n"):
                            line_s = line.strip()
                            if line_s.startswith(("[Target Platform]:", "[Platform]:", "[المنصة المستهدفة]:")):
                                platform_val = re.sub(r"^\[.*?\]:\s*", "", line_s).strip()
                                break

                    occ_val = (
                        data.get("occasion")
                        or data.get("context", {}).get("occasion")
                        or detect_occasion(req)
                    )

                    if topic:
                        _add_entry(
                            topic=topic,
                            occasion=occ_val,
                            date_str=date_val,
                            source_id=data.get("ticket_id", name),
                            platform=platform_val or "facebook",
                        )
                except Exception:
                    continue

    # Sort newest first
    memory_entries.sort(key=lambda x: x["timestamp"], reverse=True)
    return memory_entries[:limit]


def get_recent_topic_titles(limit: int = 8) -> List[str]:
    """Return a clean list of recent topic strings for prompt steering."""
    memory = read_archive_memory(limit=limit)
    return [m["topic"] for m in memory if m.get("topic")]


# ==============================================================================
# 2. Smart Scheduling Engine (Occasion vs Weekly Scheduling)
# ==============================================================================

def calculate_next_weekly_slot(target_weekday: int = 3, target_hour: int = 9) -> str:
    """Calculate the ISO-8601 UTC timestamp for the next weekly slot.

    Default target: Thursday (weekday 3 in Python: Monday=0, Thursday=3) at 09:00 UTC.
    """
    now = datetime.now(timezone.utc)
    days_ahead = target_weekday - now.weekday()
    if days_ahead <= 0 or (days_ahead == 0 and now.hour >= target_hour):
        days_ahead += 7
    target_dt = now.replace(hour=target_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    return target_dt.isoformat(timespec="seconds")


def resolve_post_scheduling(payload: Dict[str, Any], request_text: str) -> Dict[str, Any]:
    """Smart scheduling function: differentiates between an Occasion Post and a Weekly Post.

    Contracts:
    - Occasion Post:
        - Detected via explicit field `occasion` / `post_type == "occasion"` or text regex.
        - Strict mode (`mode: strict`) with a precise scheduled_for timestamp.
        - High priority, bound to event timing.
    - Standard Weekly Post:
        - Default content post.
        - Queue mode (`mode: queue`, `scheduled_for: None` or next regular publishing slot).
        - Regular thought-leadership cadence.
    """
    explicit_type = payload.get("post_type", "").lower()
    explicit_occasion = payload.get("occasion")
    detected = detect_occasion(request_text)

    is_occasion = bool(explicit_type == "occasion" or explicit_occasion or detected)
    occasion_name = explicit_occasion or detected

    scheduled_for = payload.get("scheduled_for")

    if is_occasion:
        post_type = "occasion"
        mode = "strict"
        # If no explicit scheduled_for provided, schedule for tomorrow 08:00 UTC
        if not scheduled_for or not isinstance(scheduled_for, str):
            tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
            scheduled_for = tomorrow.replace(hour=8, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    else:
        post_type = "weekly"
        # Honor an explicit schedule; otherwise place the post on the next
        # regular weekly slot (Thursday 09:00 UTC) so routine content keeps a
        # weekly cadence instead of publishing immediately. The PublishingWorker
        # (and the router) hold it until that moment.
        mode = "strict"
        if not (scheduled_for and isinstance(scheduled_for, str) and scheduled_for.strip()):
            scheduled_for = calculate_next_weekly_slot()

    return {
        "post_type": post_type,
        "occasion": occasion_name if is_occasion else None,
        "publishing_rules": {
            "mode": mode,
            "scheduled_for": scheduled_for,
        },
    }


# ==============================================================================
# 3. Idea-to-Capsule Bridge
# ==============================================================================

def spawn_marketing_capsule(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The Idea Bridge: Transforms an ideation ticket into a Deliverable Capsule in the spool.

    Integrates:
    - Monthly Archive Memory (extracts past topics to prevent duplication)
    - Smart Scheduling (Occasion vs Weekly post differentiation)
    - Full schema compliance for capsule_engine & MarketingWorker.
    """
    ticket_id = payload.get("ticket_id", "unknown_ticket")
    client_id = payload.get("client_id", "owner")
    request = payload.get("request", "").strip()

    ts = int(time.time())
    clean_ticket_suffix = ticket_id.replace("T-", "").replace("-", "_")
    capsule_id = f"capsule_{ts}_marketing_{clean_ticket_suffix[:12]}"
    capsule_dir = os.path.join(SPOOL_DIR, capsule_id)
    os.makedirs(capsule_dir, exist_ok=True)

    now_ts = now_iso()
    state_val = State.DRAFT.value if State is not None else "draft"

    # 1. Read Archive Memory
    recent_memory = read_archive_memory(limit=15)
    recent_topic_titles = [m["topic"] for m in recent_memory[:6]]

    # 2. Resolve Scheduling (Occasion vs Weekly)
    schedule_info = resolve_post_scheduling(payload, request)
    post_type = schedule_info["post_type"]
    occasion = schedule_info["occasion"]
    publishing_rules = schedule_info["publishing_rules"]

    # 3. Extract platform and title
    platform = payload.get("target_platform", "facebook")
    if not isinstance(platform, str) or not platform.strip():
        platform = "facebook"

    topic = payload.get("topic")
    if not topic or not isinstance(topic, str):
        topic = request.split("\n")[0].strip()
        if len(topic) > 100:
            topic = topic[:97] + "..."

    # 4. Enrich prompt with memory context and type directives
    enriched_prompt_parts = [request]
    
    if post_type == "occasion" and occasion:
        enriched_prompt_parts.append(
            f"\n\n[Special Occasion Guidance]: This deliverable celebrates ({occasion}). "
            "Balance celebratory spirit with technological sovereignty and architectural depth without cliches."
        )
    else:
        enriched_prompt_parts.append(
            "\n\n[Weekly Routine Guidance]: This deliverable is part of weekly executive thought-leadership. "
            "Focus on sovereign engineering, self-governing architectures, and fail-closed reliability."
        )

    if recent_topic_titles:
        past_topics_bulleted = " • " + " • ".join(recent_topic_titles[:4])
        enriched_prompt_parts.append(
            f"\n\n[Company Archive Memory Context]: These topics were recently published:\n{past_topics_bulleted}\n"
            "Provide a fresh strategic angle and avoid repeating prior concepts or headlines."
        )

    full_raw_prompt = "".join(enriched_prompt_parts).strip()

    # 5. Build 100% compliant manifest data
    manifest_data: Dict[str, Any] = {
        "capsule_id": capsule_id,
        "domain": "marketing",
        "state": state_val,
        "attempt": 0,
        "created_at": now_ts,
        "updated_at": now_ts,
        "history": [
            {
                "at": now_ts,
                "from_state": None,
                "to_state": state_val,
                "by": "ideation_bridge",
                "why": f"capsule spawned from ideation ticket {ticket_id} (type={post_type})",
            }
        ],
        "raw_prompt": full_raw_prompt,
        "metadata": {
            "domain": "marketing",
            "topic": topic,
            "target_platform": platform.lower(),
            "post_type": post_type,
            "occasion": occasion,
            "source_sector": "ideation",
            "source_ticket_id": ticket_id,
            "client_id": client_id,
            "memory_context": {
                "recent_topics_count": len(recent_memory),
                "recent_topics_sample": recent_topic_titles[:5],
            },
        },
        "routing_context": {
            "domain": "marketing",
            "flags": {
                "requires_copy": True,  # Mandatory for MarketingWorker
                "requires_image": True,  # Enables QA -> Design pipeline
            },
        },
        "publishing_rules": publishing_rules,
    }

    # Atomic write to spool
    save_manifest(capsule_dir, manifest_data)

    # 6. Write audit deliverable in the Ideation domain
    os.makedirs(DELIVERABLES, exist_ok=True)
    deliverable_name = f"deliverable_{ticket_id}_{ts}.md"
    deliverable_path = os.path.join(DELIVERABLES, deliverable_name)
    audit_lines = [
        f"# Ideation Bridge Deliverable: {ticket_id}\n\n",
        f"- **Client ID:** `{client_id}`\n",
        f"- **Spawned Capsule ID:** `{capsule_id}`\n",
        f"- **Post Type:** `{post_type.upper()}`\n",
    ]
    if occasion:
        audit_lines.append(f"- **Occasion:** `{occasion}`\n")
    audit_lines.append(f"- **Publishing Mode:** `{publishing_rules['mode']}`\n")
    if publishing_rules.get("scheduled_for"):
        audit_lines.append(f"- **Scheduled For:** `{publishing_rules['scheduled_for']}`\n")
    audit_lines.append(f"- **Target Platform:** `{platform}`\n")
    audit_lines.append(f"- **Bridged At:** `{now_ts}`\n\n")
    audit_lines.append("## Historical Archive Memory (Past Topics Referenced)\n\n")
    for mem in recent_memory[:5]:
        occ_label = f" *({mem['occasion']})*" if mem.get("occasion") else ""
        audit_lines.append(f"- [{mem['date'][:10]}] {mem['topic']}{occ_label}\n")
    audit_lines.append(f"\n## Original Idea Request\n\n{request}\n\n---\n")
    audit_lines.append(
        "*Handoff complete: The capsule is now live in `capsules_spool/` awaiting autonomous factory pulse.*\n"
    )
    atomic_text(deliverable_path, "".join(audit_lines), encoding="utf-8")

    return {
        "capsule_id": capsule_id,
        "capsule_dir": capsule_dir,
        "deliverable_file": deliverable_name,
        "post_type": post_type,
        "occasion": occasion,
        "mode": publishing_rules["mode"],
        "scheduled_for": publishing_rules.get("scheduled_for"),
    }


def execute(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the ticket handoff: validate, bridge to capsule, and report."""
    ticket_id = payload.get("ticket_id")
    request = payload.get("request")

    if not ticket_id or not isinstance(request, str) or not request.strip():
        return {
            "result": "needs_revision",
            "reason": "unworkable handoff: missing ticket_id or request",
        }

    tier = classify(request)
    if tier == "irreversible" or tier is None:
        return {
            "result": "needs_revision",
            "reason": "outside worker territory: %s request — human execution required"
            % (tier or "unclassifiable"),
        }

    if tier == "external" and not payload.get("via"):
        return {
            "result": "needs_revision",
            "reason": "external concept without gate approval (via missing) — raise a gate first",
        }

    try:
        bridge_info = spawn_marketing_capsule(payload)
    except Exception as exc:
        return {
            "result": "needs_revision",
            "reason": f"Idea bridge failed to spawn capsule: {exc}",
        }

    return {
        "result": "done",
        "notes": f"Capsule {bridge_info['capsule_id']} spawned successfully ({bridge_info['post_type']}, mode={bridge_info['mode']}).",
        "context": {
            "capsule_id": bridge_info["capsule_id"],
            "capsule_path": bridge_info["capsule_dir"],
            "deliverable": bridge_info["deliverable_file"],
            "post_type": bridge_info["post_type"],
            "occasion": bridge_info["occasion"],
            "mode": bridge_info["mode"],
            "scheduled_for": bridge_info["scheduled_for"],
        },
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                {
                    "result": "needs_revision",
                    "reason": f"invalid handoff JSON: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return

    if not isinstance(payload, dict):
        print(
            json.dumps(
                {
                    "result": "needs_revision",
                    "reason": "invalid handoff: not a JSON object",
                },
                ensure_ascii=False,
            )
        )
        return

    print(json.dumps(execute(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
