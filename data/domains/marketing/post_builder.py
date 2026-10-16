#!/usr/bin/env python3
"""post_builder.py — Deconstructs and assembles Post Capsules from atomic tickets (T1, T2, T3).

Architecture:
- T1 (Ideation): Topic, Angle, Occasion, Target Channels, Key Pillars, CTA
- T2 (Copywriting): Platform-tailored text variants (LinkedIn, Facebook, X)
- T3 (Visual Design): Master image prompt, aspect ratios (1:1, 4:5, 16:9), Brand Overlay
- Bundling: Assembles T1 + T2 + T3 into a single cohesive Deliverable Capsule

Standard library only; zero external dependencies.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from shared_kernel.brand_profile import enrich_with_company_context, load_company_profile
from capsule_engine.manifest import (
    new_manifest,
    set_capsule_bundle,
)
from data.domains.marketing.occasions import calculate_occasion_schedule, detect_occasion
from shared_kernel.state_schema import Ticket


@dataclass
class IdeationSpec:
    """T1: The Core Idea & Strategic Angle."""
    topic: str
    angle: str
    audience: str = "Enterprise Decision Makers, Founders, Industry Leaders"
    occasion: Optional[str] = None
    channels: List[str] = field(default_factory=lambda: ["linkedin", "facebook", "x"])
    pillars: List[str] = field(default_factory=list)
    cta: str = "Share your thoughts in the comments below."
    ticket_id: Optional[str] = None


@dataclass
class CopywritingSpec:
    """T2: Platform-tailored copy variants."""
    headline: str
    linkedin_copy: Optional[str] = None
    facebook_copy: Optional[str] = None
    x_copy: Optional[str] = None
    unified_copy: Optional[str] = None
    hashtags: List[str] = field(default_factory=list)
    ticket_id: Optional[str] = None


@dataclass
class VisualDesignSpec:
    """T3: Visual asset specifications and dimensions."""
    visual_concept: str
    image_prompt: str
    aspect_ratios: List[str] = field(default_factory=lambda: ["1:1", "4:5", "16:9"])
    include_logo: bool = True
    ticket_id: Optional[str] = None


def create_atomic_tickets(
    idea: IdeationSpec,
    copy: Optional[CopywritingSpec] = None,
    design: Optional[VisualDesignSpec] = None,
    company_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Ticket]:
    """Instantiate atomic Ticket models for T1, T2, and T3 with company context."""
    profile = company_profile or load_company_profile()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tickets: Dict[str, Ticket] = {}

    # Schedule / Occasion metadata
    occ_info = calculate_occasion_schedule(idea.topic) if not idea.occasion else calculate_occasion_schedule(idea.occasion)
    event_date = occ_info.get("event_date") if occ_info else None
    ttl_hours = occ_info.get("ttl_hours", 48) if occ_info else 48

    # T1: Ideation Ticket
    t1_id = idea.ticket_id or f"T1-IDEA-{ts}"
    idea.ticket_id = t1_id
    t1 = Ticket(
        ticket_id=t1_id,
        client_id="company_os",
        request=f"Ideate: {idea.topic} | Angle: {idea.angle}",
        state="done" if copy else "open",
        channels=idea.channels,
        event_date=event_date,
        ttl_hours=ttl_hours,
        context={
            "topic": idea.topic,
            "angle": idea.angle,
            "audience": idea.audience,
            "occasion": occ_info.get("name") if occ_info else None,
            "pillars": idea.pillars,
            "cta": idea.cta,
        },
    )
    enrich_with_company_context(t1.__dict__, profile)
    tickets["T1"] = t1

    # T2: Copywriting Ticket (if provided)
    if copy:
        t2_id = copy.ticket_id or f"T2-COPY-{ts}"
        copy.ticket_id = t2_id
        t2 = Ticket(
            ticket_id=t2_id,
            client_id="company_os",
            request=f"Draft copy for {idea.topic}",
            state="done" if design else "open",
            channels=idea.channels,
            event_date=event_date,
            ttl_hours=ttl_hours,
            context={
                "headline": copy.headline,
                "linkedin": copy.linkedin_copy,
                "facebook": copy.facebook_copy,
                "x": copy.x_copy,
                "unified": copy.unified_copy,
                "hashtags": copy.hashtags,
                "parent_idea": t1_id,
            },
        )
        enrich_with_company_context(t2.__dict__, profile)
        tickets["T2"] = t2

    # T3: Visual Design Ticket (if provided)
    if design:
        t3_id = design.ticket_id or f"T3-DSGN-{ts}"
        design.ticket_id = t3_id
        t3 = Ticket(
            ticket_id=t3_id,
            client_id="company_os",
            request=f"Design visual for {idea.topic}",
            state="open",
            channels=idea.channels,
            event_date=event_date,
            ttl_hours=ttl_hours,
            context={
                "concept": design.visual_concept,
                "image_prompt": design.image_prompt,
                "aspect_ratios": design.aspect_ratios,
                "include_logo": design.include_logo,
            },
        )
        enrich_with_company_context(t3.__dict__, profile)
        tickets["T3"] = t3

    return tickets


def assemble_post_capsule_manifest(
    capsule_id: str,
    idea: IdeationSpec,
    copy: Optional[CopywritingSpec] = None,
    design: Optional[VisualDesignSpec] = None,
    company_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a unified Post Capsule manifest bundling T1, T2, and T3."""
    profile = company_profile or load_company_profile()
    tickets_dict = create_atomic_tickets(idea, copy, design, profile)
    ticket_ids = [t.ticket_id for t in tickets_dict.values()]

    occ_info = calculate_occasion_schedule(idea.topic) if not idea.occasion else calculate_occasion_schedule(idea.occasion)
    event_date = occ_info.get("event_date") if occ_info else None
    ttl_hours = occ_info.get("ttl_hours", 48) if occ_info else 48

    manifest = new_manifest(
        capsule_id=capsule_id,
        tickets=ticket_ids,
        bundle_type="post_capsule",
        channels=idea.channels,
        company_context=profile,
        event_date=event_date,
        ttl_hours=ttl_hours,
    )
    manifest["domain"] = "marketing"
    manifest["routing_context"] = {
        "domain": "marketing",
        "flags": {
            "requires_copy": True,
            "requires_image": True,
            "requires_qa": True,
        },
    }

    manifest["raw_prompt"] = f"Write an authoritative executive thought-leadership post for {idea.topic}. Angle: {idea.angle}. Target channels: {', '.join(idea.channels)}."
    
    # Attach bundle breakdown into metadata for fast rich rendering in Cockpit
    manifest.setdefault("metadata", {})
    manifest["metadata"]["topic"] = idea.topic
    manifest["metadata"]["angle"] = idea.angle
    manifest["metadata"]["target_platform"] = idea.channels[0] if idea.channels else "linkedin"
    manifest["metadata"]["bundle_breakdown"] = {
        "idea": {
            "ticket_id": tickets_dict["T1"].ticket_id,
            "topic": idea.topic,
            "angle": idea.angle,
            "occasion": occ_info.get("name") if occ_info else None,
            "audience": idea.audience,
        },
        "copy": {
            "ticket_id": tickets_dict.get("T2", tickets_dict["T1"]).ticket_id if copy else None,
            "headline": copy.headline if copy else None,
            "channels": idea.channels,
        },
        "design": {
            "ticket_id": tickets_dict.get("T3", tickets_dict["T1"]).ticket_id if design else None,
            "aspect_ratios": design.aspect_ratios if design else ["1:1", "4:5", "16:9"],
        },
    }

    # Set parent capsule_id on all constituent tickets
    for t in tickets_dict.values():
        t.capsule_id = capsule_id
        t.bundle_status = "bundled"

    return manifest
