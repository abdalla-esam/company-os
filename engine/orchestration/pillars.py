"""
orchestration.pillars — Executive Macro-Capsules Provider

Aggregates the core operational pillars of an enterprise into high-level
executive deliverable containers (Pillar Capsules):
  1. Content & Marketing Engine (إدارة المحتوى والنشر)
  2. Business Development & Client Pipeline (المبيعات وتطوير الأعمال)
  3. Operations & Delivery Pipeline (العمليات والإنتاج)
  4. Governance & Token Capital (المالية والحوكمة)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from shared_kernel.brand_profile import load_company_profile
from data.domains.marketing.occasions import get_upcoming_occasions

logger = logging.getLogger("orchestration.pillars")


@dataclass(frozen=True)
class PillarMetric:
    label_ar: str
    label_en: str
    value: str
    status: str = "normal"  # normal, ok, warn, danger, gold


@dataclass(frozen=True)
class PillarCapsule:
    id: str
    title_ar: str
    title_en: str
    icon: str
    accent_color: str
    description_ar: str
    description_en: str
    status_label_ar: str
    status_label_en: str
    status_tone: str  # ok, warn, danger, gold
    metrics: list[PillarMetric] = field(default_factory=list)
    active_deliverable_title: str | None = None
    active_deliverable_id: str | None = None
    next_milestone_ar: str | None = None
    next_milestone_en: str | None = None
    target_channels: list[str] = field(default_factory=list)
    action_type: str = "navigate"  # navigate, create_capsule, review_inbox
    action_route: str = "inbox"
    action_label_ar: str = "متابعة التفاصيل"
    action_label_en: str = "Review Details"


def get_executive_pillars(
    *,
    tickets: Sequence[Mapping[str, Any]] | None = None,
    capsules: Sequence[Mapping[str, Any]] | None = None,
    gates: Sequence[Mapping[str, Any]] | None = None,
    domains_data: Sequence[Mapping[str, Any]] | None = None,
    cadence_data: Mapping[str, Any] | None = None,
) -> list[PillarCapsule]:
    """
    Dynamically constructs the 4 core macro-capsules representing the enterprise
    from the current snapshot state.
    """
    tickets = list(tickets or [])
    capsules = list(capsules or [])
    gates = list(gates or [])
    domains_data = list(domains_data or [])

    # Load brand context
    comp_profile = load_company_profile()
    brand_name = comp_profile.get("brand_name") or comp_profile.get("company_name") or ""
    brand_tone = comp_profile.get("tone") or ""

    # Upcoming occasions
    upcoming_occasions = get_upcoming_occasions(days_ahead=30)
    next_occ = upcoming_occasions[0] if upcoming_occasions else None

    # =========================================================================
    # 1. CONTENT & MARKETING ENGINE PILLAR
    # =========================================================================
    mkt_capsules = [c for c in capsules if c.get("domain") == "marketing" or c.get("sector") == "marketing"]
    mkt_pending = [c for c in mkt_capsules if c.get("state") in ("pending_approval", "pending")]
    mkt_published = [c for c in mkt_capsules if c.get("state") == "published"]
    mkt_in_progress = [c for c in mkt_capsules if c.get("state") in ("in_progress", "draft", "in_review", "pending_design", "pending_qa")]

    mkt_active_capsule = mkt_pending[0] if mkt_pending else (mkt_in_progress[0] if mkt_in_progress else None)
    mkt_active_title = None
    mkt_active_id = None
    if mkt_active_capsule:
        mkt_active_title = (
            mkt_active_capsule.get("manifest", {}).get("metadata", {}).get("topic")
            or mkt_active_capsule.get("title")
            or mkt_active_capsule.get("capsule_id")
        )
        mkt_active_id = mkt_active_capsule.get("capsule_id") or mkt_active_capsule.get("id")

    # Content status
    if mkt_pending:
        content_status_ar = f"{len(mkt_pending)} كبسولات بانتظار توقيع الإدارة"
        content_status_en = f"{len(mkt_pending)} Capsules Awaiting Sign-Off"
        content_tone = "gold"
        content_action_route = "inbox"
        content_action_ar = "اعتماد ونشر الكبسولة ←"
        content_action_en = "Sign Off & Publish →"
    elif mkt_in_progress:
        content_status_ar = f"{len(mkt_in_progress)} كبسولات قيد التصنيع"
        content_status_en = f"{len(mkt_in_progress)} Capsules in Production"
        content_tone = "warn"
        content_action_route = "tasks"
        content_action_ar = "متابعة خط الإنتاج ←"
        content_action_en = "Track Pipeline →"
    elif mkt_published:
        content_status_ar = f"{len(mkt_published)} منشورات معتمدة على القرص"
        content_status_en = f"{len(mkt_published)} Published Posts on Disk"
        content_tone = "ok"
        content_action_route = "branch:marketing"
        content_action_ar = "استعراض المنشورات ←"
        content_action_en = "View Published Posts →"
    else:
        content_status_ar = "لا توجد كبسولات محتوى على القرص"
        content_status_en = "Zero Capsules on Disk"
        content_tone = "normal"
        content_action_route = "branch:marketing"
        content_action_ar = "بدء أول كبسولة محتوى +"
        content_action_en = "Create First Capsule +"

    # Next occasion string
    if next_occ:
        occ_str_ar = f"مناسبة قادمة: {next_occ['name_ar']} (خلال {next_occ['days_until']} يوم)"
        occ_str_en = f"Upcoming Occasion: {next_occ['name_en']} ({next_occ['days_until']}d away)"
    else:
        occ_str_ar = "لا توجد مناسبات قريبة خلال 30 يوماً"
        occ_str_en = "No upcoming occasions in next 30 days"

    content_pillar = PillarCapsule(
        id="pillar_content_marketing",
        title_ar="إدارة المحتوى والنشر والتسويق",
        title_en="Content & Marketing Engine",
        icon="📢",
        accent_color="var(--branch-2)",
        description_ar=f"محرك صناعة المحتوى المتكامل لهوية {brand_name} وتوزيع المنشورات عبر المنصات.",
        description_en=f"End-to-end content production engine for {brand_name} with multi-platform distribution.",
        status_label_ar=content_status_ar,
        status_label_en=content_status_en,
        status_tone=content_tone,
        metrics=[
            PillarMetric("المنشورات المعتمدة", "Published Posts", str(len(mkt_published)), "ok" if mkt_published else "normal"),
            PillarMetric("كبسولات قيد المراجعة", "Pending Review", str(len(mkt_pending)), "gold" if mkt_pending else "normal"),
            PillarMetric("قيد التصنيع", "In Production", str(len(mkt_in_progress)), "warn" if mkt_in_progress else "normal"),
        ],
        active_deliverable_title=mkt_active_title,
        active_deliverable_id=mkt_active_id,
        next_milestone_ar=occ_str_ar,
        next_milestone_en=occ_str_en,
        target_channels=["LinkedIn", "Facebook", "X"],
        action_type="navigate",
        action_route=content_action_route,
        action_label_ar=content_action_ar,
        action_label_en=content_action_en,
    )

    # =========================================================================
    # 2. BUSINESS DEVELOPMENT & SALES PILLAR
    # =========================================================================
    sales_tickets = [t for t in tickets if t.get("sector") in ("client_management", "sales")]
    sales_open = [t for t in sales_tickets if t.get("state") not in ("done", "settled", "archived", "closed")]
    sales_gates = [g for g in gates if g.get("domain") in ("client_management", "sales") or g.get("sector") in ("client_management", "sales")]

    sales_status_ar = f"{len(sales_open)} صفقات وعروض نشطة" if sales_open else "لا توجد صفقات مسجلة على القرص"
    sales_status_en = f"{len(sales_open)} Active Client Deals" if sales_open else "Zero Deals on Disk"
    sales_active_title = sales_open[0].get("title") if sales_open else None
    sales_active_id = sales_open[0].get("ticket_id") or sales_open[0].get("id") if sales_open else None

    sales_pillar = PillarCapsule(
        id="pillar_sales_growth",
        title_ar="المبيعات وتطوير الأعمال والعملاء",
        title_en="Sales & Client Growth",
        icon="🤝",
        accent_color="var(--branch-5)",
        description_ar="إدارة تدفق العملاء، صياغة المقترحات التجارية، وحوكمة الصفقات.",
        description_en="Client intake pipeline, commercial proposals, and deal governance.",
        status_label_ar=sales_status_ar,
        status_label_en=sales_status_en,
        status_tone="ok" if sales_open and not sales_gates else ("gold" if sales_gates else "normal"),
        metrics=[
            PillarMetric("الصفقات النشطة", "Active Deals", str(len(sales_open)), "normal"),
            PillarMetric("بوابات بانتظار الموافقة", "Pending Proposals", str(len(sales_gates)), "gold" if sales_gates else "normal"),
        ],
        active_deliverable_title=sales_active_title,
        active_deliverable_id=sales_active_id,
        next_milestone_ar="متابعة عروض الشركات" if sales_open else "لا توجد عروض مجدولة",
        next_milestone_en="Follow up partner deals" if sales_open else "Zero scheduled proposals",
        action_type="navigate",
        action_route="branch:client_management",
        action_label_ar="استعراض صفقات العملاء ←",
        action_label_en="View Client Deals →",
    )

    # =========================================================================
    # 3. OPERATIONS & DELIVERY PILLAR
    # =========================================================================
    ops_tickets = [t for t in tickets if t.get("sector") in ("operations", "development")]
    ops_inflight = [t for t in ops_tickets if t.get("state") in ("in_progress", "approved", "active")]

    ops_status_ar = f"{len(ops_inflight)} مهام قيد التشغيل" if ops_inflight else "لا توجد مهام قيد التشغيل حالياً"
    ops_status_en = f"{len(ops_inflight)} Tasks In-Flight" if ops_inflight else "Zero In-Flight Tasks"

    ops_pillar = PillarCapsule(
        id="pillar_operations_delivery",
        title_ar="العمليات والإنتاج والمنتج",
        title_en="Operations & Product Delivery",
        icon="⚙️",
        accent_color="var(--branch-3)",
        description_ar="جدولة المهام، مزامنة قواعد البيانات، ومتابعة تسليم المشاريع.",
        description_en="Task scheduling, data synchronization, and operational sprint delivery.",
        status_label_ar=ops_status_ar,
        status_label_en=ops_status_en,
        status_tone="ok" if ops_inflight else "normal",
        metrics=[
            PillarMetric("المهام قيد التشغيل", "In-Flight Tasks", str(len(ops_inflight)), "normal"),
            PillarMetric("إجمالي المخرجات", "Total Deliverables", str(len(capsules)), "ok" if capsules else "normal"),
        ],
        next_milestone_ar=f"{len(capsules)} مخرجات مسجلة على القرص",
        next_milestone_en=f"{len(capsules)} Deliverables on Disk",
        action_type="navigate",
        action_route="tasks",
        action_label_ar="فحص خط الإنتاج الموحد ←",
        action_label_en="Inspect Production Line →",
    )

    # =========================================================================
    # 4. GOVERNANCE & CAPITAL PILLAR
    # =========================================================================
    total_tokens = sum(c.get("manifest", {}).get("metadata", {}).get("usage", {}).get("total_tokens", 0) for c in capsules)
    gov_status_ar = f"{total_tokens:,} توكن مسجل في المانيفست" if total_tokens > 0 else "0 توكن مستهلك (سجل نظيف)"
    gov_status_en = f"{total_tokens:,} Tokens Logged" if total_tokens > 0 else "0 Tokens Logged (Clean State)"

    gov_pillar = PillarCapsule(
        id="pillar_governance_finance",
        title_ar="المالية وحوكمة الميزانية والوكلاء",
        title_en="Governance & Capital Oversight",
        icon="💰",
        accent_color="var(--branch-8)",
        description_ar="حوكمة الصلاحيات (L0-L3)، مراقبة استهلاك التوكنز، وميزانية التوليد.",
        description_en="Autonomy ladder governance (L0-L3), token consumption tracking, and generation budget.",
        status_label_ar=gov_status_ar,
        status_label_en=gov_status_en,
        status_tone="ok" if total_tokens > 0 else "normal",
        metrics=[
            PillarMetric("التوكنز المستهلكة", "Tokens Consumed", f"{total_tokens:,}", "normal"),
            PillarMetric("مستوى الاستقلالية", "Autonomy Level", "L1 / L2", "ok"),
        ],
        next_milestone_ar="تدقيق الصلاحيات والميزانية",
        next_milestone_en="Permission and budget audit",
        action_type="navigate",
        action_route="radar",
        action_label_ar="استعراض الرادار المالي والرقابي ←",
        action_label_en="View Financial Telemetry →",
    )

    return [content_pillar, sales_pillar, ops_pillar, gov_pillar]
