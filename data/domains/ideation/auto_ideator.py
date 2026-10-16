#!/usr/bin/env python3
"""auto_ideator.py — Autonomous Idea Generator & Cadence Engine for the Ideation Domain.

The Final Gear of the Ideation Factory:
1. Cadence Guard: Ensures at least 48 hours have elapsed since the newest ticket
   (bypassed with --force for manual runs or testing).
2. Archive Memory Reader: Pulls historical company topics to enforce anti-duplication.
3. ModelGateway Integration: Calls the LLM to generate exactly ONE high-impact,
   sovereign, strategic post idea with deterministic fallback if the network endpoint is offline.
4. Human-in-the-Loop Gate Binding: Persists the idea as a ticket with status `needs_gate`
   (state: `pending_approval`), raising an official approval gate for human authorization.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
TICKETS_DIR = os.path.join(HERE, "tickets")
ARCHIVE_DIR = os.path.join(HERE, "archive")
GATE_DIR = os.path.normpath(os.path.join(ROOT_DIR, "governance", "approval_gates"))

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Load environment variables (.env) safely
try:
    import dotenv
    dotenv.load_dotenv(os.path.join(ROOT_DIR, ".env"))
except ImportError:
    env_file = os.path.join(ROOT_DIR, ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

from tracker_script import raise_gate, SECTOR
from worker_script import read_archive_memory, now_iso, parse_ts, detect_occasion

# Crash-safe ticket writes come from the shared kernel's one helper; this
# script runs standalone, so bootstrap the engine directory from the known
# repository layout (auto_ideator lives three levels below the root).
_ENGINE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)
try:
    from shared_kernel.atomic_io import atomic_json
except ImportError:  # pragma: no cover - stripped environments
    # atomic_io is pure standard library: load it by path so the write-safety
    # guarantee never depends on an installed wheel.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "shared_kernel_atomic_io",
        os.path.join(_ENGINE_ROOT, "shared_kernel", "atomic_io.py"),
    )
    if _spec is None or _spec.loader is None:
        raise
    _atomic_io = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_atomic_io)
    atomic_json = _atomic_io.atomic_json

try:
    from shared_kernel.state_schema import Ticket, Decision
except ImportError:
    Ticket = None
    Decision = None

try:
    from capsule_engine.gateway import default_gateway, ModelGateway, OfflineModelGateway
except ImportError:
    default_gateway = None
    ModelGateway = None
    OfflineModelGateway = None

logger = logging.getLogger("ideation.auto_ideator")
DEFAULT_CADENCE_HOURS = 48.0

# Curated reservoir of high-authority strategic themes for deterministic fallback
CORE_STRATEGIC_PILLARS = [
    {
        "title": "Closed-Loop Architectures & Software Sovereignty: Why Rented SaaS Fails Enterprise Asset Protection",
        "angle": "Architectural comparison between SaaS vendor lock-in that leaks proprietary data versus 100% owned sovereign engines.",
        "platform": "linkedin",
        "takeaways": [
            "Companies do not own their software as long as their operational data resides in third-party databases.",
            "The concept of Self-Contained Vaults and zero-intermediary atomic operations.",
            "Building defensible competitive advantages that competitors cannot replicate with a subscription.",
        ],
        "cta": "Is your tech stack a strategic asset you own, or a rented SaaS subscription that vanishes when you stop paying?",
    },
    {
        "title": "Atomic Lock Engineering: How Autonomous Systems Prevent Collisions Without Central Orchestration Servers",
        "angle": "Explaining how flock algorithms and atomic file replacement (os.replace) enable decentralized self-coordinating agents.",
        "platform": "facebook",
        "takeaways": [
            "Race conditions are the primary vulnerability in naive corporate automation workflows.",
            "Leveraging operating system file locks to provide deterministic, zero-cost, 100% reliable concurrency.",
            "Architectural simplicity consistently outperforms complex server clusters.",
        ],
        "cta": "Transform your organization into a pulse-driven factory without the burden of centralized server maintenance.",
    },
    {
        "title": "Cryptographic Anti-TOCTOU Binding: How Executives Guarantee Output Integrity Between Review and Publishing",
        "angle": "Documenting cryptographic enforcement via immutable SHA-256 hashes bound to human review gates.",
        "platform": "linkedin",
        "takeaways": [
            "Time-of-Check to Time-of-Use: the risk of AI deliverable tampering after managerial approval.",
            "Binding human decisions directly to cryptographic content hashes to reject altered bytes.",
            "Strict technical governance eliminating silent hallucination drift and prompt injection risks.",
        ],
        "cta": "Never deploy autonomous AI agents in your enterprise without fail-closed cryptographic governance gates.",
    },
    {
        "title": "Progressive Autonomy Ladder (L0 to L3): The Engineering Methodology for Agent Self-Publishing",
        "angle": "How companies systematically scale from 100% human review to full autonomy while preserving instant safety demotions.",
        "platform": "facebook",
        "takeaways": [
            "Autonomous does not mean unaccountable: continuous 2% random audits on high-autonomy agents.",
            "Instant Demotion: any human rejection immediately resets the domain to L0 under fail-closed principles.",
            "Building trust in AI agents mirrors human talent development: gradual, verifiable, and evidence-based.",
        ],
        "cta": "Design your agency governance with architectural rigor: trust is earned incrementally and revoked instantly upon error.",
    },
    {
        "title": "Fail-Closed Architecture: Why Halting on Ambiguity is the Highest Form of Machine Intelligence",
        "angle": "The engineering philosophy of AI systems that prefer safe halting and human escalation over guessing under uncertainty.",
        "platform": "linkedin",
        "takeaways": [
            "Brittle systems assume success and crash on exceptions; sovereign systems are architected to fail closed.",
            "Confidence Thresholds: when an agent escalates for executive authorization.",
            "Long-term automation sustainability depends on zero unauthorized actions on sensitive production lines.",
        ],
        "cta": "Are your intelligent systems designed to recognize their boundaries, or do they gamble with your enterprise reputation?",
    },
]


# ==============================================================================
# 1. Cadence Guard (48h Threshold)
# ==============================================================================

def check_cadence(threshold_hours: float = DEFAULT_CADENCE_HOURS) -> Tuple[bool, float, Optional[str], Optional[datetime]]:
    """Check if at least threshold_hours have passed since the most recent ticket.

    Returns:
        (can_proceed: bool, elapsed_hours: float, latest_ticket_id: Optional[str], latest_time: Optional[datetime])
    """
    latest_time: Optional[datetime] = None
    latest_id: Optional[str] = None

    search_dirs = [TICKETS_DIR]
    if os.path.isdir(ARCHIVE_DIR):
        search_dirs.append(ARCHIVE_DIR)

    for base in search_dirs:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.startswith("ticket_") and name.endswith(".json"):
                    path = os.path.join(root, name)
                    try:
                        with open(path, encoding="utf-8") as fh:
                            data = json.load(fh)
                        created_str = data.get("created_at")
                        dt = parse_ts(created_str)
                        if dt is not None:
                            if latest_time is None or dt > latest_time:
                                latest_time = dt
                                latest_id = data.get("ticket_id") or name
                    except Exception:
                        continue

    if latest_time is None:
        # No previous tickets found -> full clearance to proceed
        return True, float("inf"), None, None

    now = datetime.now(timezone.utc)
    elapsed_seconds = (now - latest_time).total_seconds()
    elapsed_hours = max(0.0, elapsed_seconds / 3600.0)

    can_proceed = elapsed_hours >= threshold_hours
    return can_proceed, elapsed_hours, latest_id, latest_time


# ==============================================================================
# 2. Autonomous Idea Generation via ModelGateway with Deterministic Fallback
# ==============================================================================

IDEATOR_SYSTEM_PROMPT = """You are the Chief Strategy & Content Officer for an elite tech agency powered by a Sovereign Agency OS.

Your mission:
Generate exactly ONE high-impact, authoritative thought-leadership post proposal.
Support multilingual content seamlessly: output in Arabic if generating for Arabic leadership audiences, or English/other language if requested, maintaining executive depth and authoritative prestige.

Strategic Themes:
- Technological sovereignty, closed-loop architectures, and autonomous self-governing systems.
- The fundamental difference between superficial scripting and robust fail-closed state machines.
- Code reliability, atomic locks, and anti-TOCTOU cryptographic integrity.
- Corporate independence from rented cloud lock-ins using sovereign local vaults.

Strict Prohibitions:
- Strictly avoid generic AI buzzwords, cliches, and fluff.
- Do NOT write the entire final post. Output only the structured strategic brief and core takeaways.
- Strictly avoid duplicating any topic listed in the historical memory context.

Required Output Format:
[Proposed Title]: ...
[Strategic Angle]: ...
[Target Platform]: linkedin (or facebook)
[Post Type]: weekly_routine
[Core Takeaways]:
- ...
- ...
- ...
[Leadership CTA]: ...
"""


def generate_fallback_strategic_idea(recent_topics: List[str]) -> str:
    """Generate a clean, high-authority idea proposal when the remote gateway is offline.

    Randomly selects (via random.choice) from curated sovereign strategic pillars,
    filtering out any pillars that already appear in recent memory or pending tickets.
    """
    recent_joined = " ".join(recent_topics).lower()

    # Filter out pillars already present in recent memory / pending tickets
    candidates = []
    for pillar in CORE_STRATEGIC_PILLARS:
        key_token = pillar["title"].split(":")[0].strip().lower()
        if key_token not in recent_joined:
            candidates.append(pillar)

    # Use candidates if available; if all have been used recently, pick from all pillars
    selection_pool = candidates if candidates else CORE_STRATEGIC_PILLARS
    selected_pillar = random.choice(selection_pool)

    takeaways_text = "\n".join(f"- {t}" for t in selected_pillar["takeaways"])
    return f"""[Proposed Title]: {selected_pillar['title']}
[Strategic Angle]: {selected_pillar['angle']}
[Target Platform]: {selected_pillar['platform']}
[Post Type]: weekly_routine
[Core Takeaways]:
{takeaways_text}
[Leadership CTA]: {selected_pillar['cta']}"""


def generate_single_idea(recent_topics: List[str]) -> str:
    """Invoke ModelGateway to produce exactly one high-impact idea proposal.

    Falls back to deterministic strategic generation if network/transport is unavailable.
    """
    past_topics_formatted = "\n".join(f"- {t}" for t in recent_topics[:8]) if recent_topics else "No previous topics."

    user_prompt = f"""Please generate exactly one new strategic post idea proposal today.

[Historical Company Memory Context — Do Not Duplicate]:
{past_topics_formatted}

Craft an inspiring, architecturally mature, and high-authority executive idea."""

    manifest_context = {
        "domain": "ideation",
        "routing_context": {
            "domain": "ideation",
            "flags": {"requires_copy": False, "requires_image": False},
        },
    }

    full_prompt = f"{IDEATOR_SYSTEM_PROMPT}\n\n---\n\n{user_prompt}"

    if default_gateway is not None:
        try:
            gw = default_gateway()
            response_text = gw.complete(manifest_context, full_prompt)
            if response_text and len(response_text.strip()) > 30:
                return response_text.strip()
        except Exception as exc:
            logger.info("Remote gateway call failed (%s); using deterministic strategic fallback", exc)

    # Deterministic fallback ensuring business continuity
    return generate_fallback_strategic_idea(recent_topics)


# ==============================================================================
# 3. Persist Idea as Gated Ticket & Raise Governance Gate
# ==============================================================================

def save_idea_as_gated_ticket(idea_text: str) -> Dict[str, Any]:
    """Persist the generated idea as a needs_gate ticket and raise a governance gate."""
    now = datetime.now(timezone.utc)
    now_str = now_iso()
    ticket_id = f"T-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    client_id = "auto_ideator"

    # Extract clean topic title from the generated idea
    lines = [line.strip() for line in idea_text.split("\n") if line.strip()]
    topic = "Strategic Leadership Idea Proposal"
    for line in lines:
        if line.startswith(("[العنوان المقترح]:", "[Proposed Title]:", "[Title]:")):
            topic = re.sub(r"^\[.*?\]:\s*", "", line).strip()
            break
        elif not line.startswith("["):
            topic = line[:90]
            break

    # 1. Decision specifying needs_gate
    decision_payload = {
        "status": "needs_gate",
        "risk_tier": "external",
        "reason": "Autonomous proposal from auto_ideator awaiting human approval",
    }

    raw_payload = {
        "client_id": client_id,
        "request": idea_text,
        "budget_cap": 0,
        "topic": topic,
    }

    # 2. Raise gate in central governance queue
    gate_id = raise_gate(raw_payload, decision_payload)

    # 3. Assemble immutable Ticket
    ticket_filename = f"ticket_{client_id}_{ticket_id}.json"
    ticket_data = {
        "ticket_id": ticket_id,
        "client_id": client_id,
        "request": idea_text,
        "decision": {
            "status": "needs_gate",
            "risk_tier": "external",
            "reason": f"Autonomous proposal from auto_ideator awaiting human review (gate: {gate_id})",
            "ticket": ticket_filename,
            "gate": gate_id,
        },
        "created_at": now_str,
        "state": "pending_approval",  # Waiting on HITL human approval
        "via": gate_id,
        "attempts": 0,
        "history": [
            {
                "at": now_str,
                "from_state": None,
                "to_state": "pending_approval",
                "by": "auto_ideator",
                "why": f"autonomous idea generated; gate {gate_id} raised for human review",
            }
        ],
        "context": {
            "generated_by": "auto_ideator",
            "gate_id": gate_id,
            "topic": topic,
        },
    }

    if Ticket is not None:
        try:
            Ticket(**ticket_data)
        except Exception:
            pass

    os.makedirs(TICKETS_DIR, exist_ok=True)
    ticket_path = os.path.join(TICKETS_DIR, ticket_filename)

    try:
        from shared_kernel.schema_version import KIND_TICKET, stamp_schema_version

        stamp_schema_version(KIND_TICKET, ticket_data)
    except ImportError:
        pass

    atomic_json(ticket_path, ticket_data)

    return {
        "ticket_id": ticket_id,
        "ticket_file": ticket_filename,
        "ticket_path": ticket_path,
        "gate_id": gate_id,
        "topic": topic,
        "created_at": now_str,
    }


# ==============================================================================
# 4. Main Entrypoint & Runner
# ==============================================================================

def run_auto_ideation(force: bool = False, hours: float = DEFAULT_CADENCE_HOURS, dry_run: bool = False) -> Dict[str, Any]:
    """Execute one pass of the auto-ideation cycle."""
    can_proceed, elapsed_hours, latest_id, latest_dt = check_cadence(threshold_hours=hours)

    if not can_proceed and not force:
        return {
            "status": "skipped",
            "reason": f"Cadence guard: only {elapsed_hours:.2f}h elapsed since last ticket (threshold: {hours:.1f}h).",
            "elapsed_hours": round(elapsed_hours, 2),
            "threshold_hours": hours,
            "latest_ticket_id": latest_id,
            "latest_ticket_time": latest_dt.isoformat() if latest_dt else None,
        }

    # 1. Read Archive Memory
    memory_entries = read_archive_memory(limit=20)
    recent_topics = [m["topic"] for m in memory_entries if m.get("topic")]

    # 2. Generate Idea via ModelGateway (or resilient fallback)
    idea_text = generate_single_idea(recent_topics)

    if dry_run:
        return {
            "status": "dry_run",
            "elapsed_hours": round(elapsed_hours, 2),
            "recent_topics_referenced": len(recent_topics),
            "idea_text": idea_text,
        }

    # 3. Save as needs_gate Ticket and Raise Gate
    save_result = save_idea_as_gated_ticket(idea_text)

    return {
        "status": "success",
        "ticket_id": save_result["ticket_id"],
        "gate_id": save_result["gate_id"],
        "ticket_file": save_result["ticket_file"],
        "topic": save_result["topic"],
        "elapsed_hours": round(elapsed_hours, 2) if elapsed_hours != float("inf") else None,
        "decision": "needs_gate",
        "state": "pending_approval",
        "action_required": f"Human approval needed via gate {save_result['gate_id']}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto Ideator — Autonomous Cadence Idea Generator")
    parser.add_argument("--force", action="store_true", help="Bypass the 48-hour cadence guard")
    parser.add_argument("--hours", type=float, default=DEFAULT_CADENCE_HOURS, help="Cadence threshold in hours (default: 48)")
    parser.add_argument("--check", action="store_true", help="Only check cadence without generating")
    parser.add_argument("--dry-run", action="store_true", help="Generate idea and preview without saving ticket or raising gate")

    args = parser.parse_args()

    if args.check:
        can_proceed, elapsed_hours, latest_id, latest_dt = check_cadence(threshold_hours=args.hours)
        result = {
            "can_proceed": can_proceed,
            "elapsed_hours": round(elapsed_hours, 2) if elapsed_hours != float("inf") else None,
            "threshold_hours": args.hours,
            "latest_ticket_id": latest_id,
            "latest_ticket_time": latest_dt.isoformat() if latest_dt else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if can_proceed else 1)

    result = run_auto_ideation(force=args.force, hours=args.hours, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
