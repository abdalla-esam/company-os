#!/usr/bin/env python3
"""occasions.py — Smart Calendar, Hijri & Gregorian Occasions Engine.

Handles detection, calendar estimation, and dynamic event-scheduling for:
1. Islamic / Hijri Occasions (Ramadan, Eid Al-Fitr, Eid Al-Adha, Islamic New Year, Mawlid, etc.)
2. National & Regional Holidays (Saudi Founding Day, National Day, 6th of October, etc.)
3. Global & Corporate Events (New Year, Special Industry Milestones)

Standard library only; zero external dependencies.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Comprehensive Occasion Patterns (Arabic & English)
OCCASION_PATTERNS: List[Tuple[str, str, str]] = [
    # Hijri / Islamic Occasions (name, category, default lead time days)
    (r"(رمضان|شهر\s*القرآن|ramadan)", "شهر رمضان المبارك", "hijri"),
    (r"(عيد\s*الفطر|فطر\s*مبارك|eid\s*al-?fitr)", "عيد الفطر المبارك", "hijri"),
    (r"(عيد\s*الأضحى|أضحى\s*مبارك|يوم\s*النحر|eid\s*al-?adha)", "عيد الأضحى المبارك", "hijri"),
    (r"(وقفة\s*عرفات|يوم\s*عرفة|day\s*of\s*arafah)", "وقفة يوم عرفة", "hijri"),
    (r"(رأس\s*السنة\s*الهجرية|عام\s*هجري|islamic\s*new\s*year)", "رأس السنة الهجرية", "hijri"),
    (r"(المولد\s*النبوي|مولد\s*الهدى|mawlid)", "المولد النبوي الشريف", "hijri"),
    (r"(ليلة\s*القدر|العشر\s*الأواخر)", "ليلة القدر المباركة", "hijri"),

    # National & Regional Occasions
    (r"(6\s*أكتوبر|نصر\s*أكتوبر|حرب\s*أكتوبر|6th\s*of\s*october)", "ذكرى نصر 6 أكتوبر المجيد", "national"),
    (r"(يوم\s*التأسيس|تأسيس\s*المملكة|founding\s*day)", "يوم التأسيس السعودي", "national"),
    (r"(اليوم\s*الوطني|national\s*day)", "اليوم الوطني", "national"),
    (r"(يوم\s*الشهيد|martyr\s*day)", "يوم الشهيد", "national"),

    # Global / Corporate Occasions
    (r"(رأس\s*السنة\s*الميلادية|سنة\s*جديدة|عام\s*جديد|new\s*year)", "رأس السنة الميلادية", "global"),
    (r"(يوم\s*المرأة|يوم\s*الأم|women'?s\s*day|mother'?s\s*day)", "مناسبة خاصة", "global"),
]

# Calendar lookup for estimated Hijri dates (Gregorian approximate window for 2026–2028)
HIJRI_CALENDAR_ESTIMATES: Dict[int, Dict[str, Tuple[int, int]]] = {
    2026: {
        "شهر رمضان المبارك": (2, 18),      # Mid Feb 2026
        "عيد الفطر المبارك": (3, 20),      # Late Mar 2026
        "وقفة يوم عرفة": (5, 26),          # Late May 2026
        "عيد الأضحى المبارك": (5, 27),     # Late May 2026
        "رأس السنة الهجرية": (6, 16),      # Mid Jun 2026
        "المولد النبوي الشريف": (8, 26),   # Late Aug 2026
    },
    2027: {
        "شهر رمضان المبارك": (2, 8),       # Early Feb 2027
        "عيد الفطر المبارك": (3, 10),      # Early Mar 2027
        "وقفة يوم عرفة": (5, 16),          # Mid May 2027
        "عيد الأضحى المبارك": (5, 17),     # Mid May 2027
        "رأس السنة الهجرية": (6, 6),       # Early Jun 2027
        "المولد النبوي الشريف": (8, 16),   # Mid Aug 2027
    },
}

FIXED_GREGORIAN_EVENTS: Dict[str, Tuple[int, int]] = {
    "ذكرى نصر 6 أكتوبر المجيد": (10, 6),
    "يوم التأسيس السعودي": (2, 22),
    "اليوم الوطني": (9, 23),
    "رأس السنة الميلادية": (1, 1),
}


def detect_occasion(text: str) -> Optional[Dict[str, Any]]:
    """Detect if the text references a Hijri, national, or global occasion."""
    if not isinstance(text, str) or not text.strip():
        return None

    for pattern, name, category in OCCASION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return {
                "name": name,
                "category": category,
                "is_hijri": category == "hijri",
            }
    return None


def estimate_event_date(occasion_name: str, year: Optional[int] = None) -> Optional[datetime]:
    """Estimate the target event UTC datetime for a given occasion."""
    now = datetime.now(timezone.utc)
    current_year = year or now.year

    # Check fixed Gregorian first
    if occasion_name in FIXED_GREGORIAN_EVENTS:
        month, day = FIXED_GREGORIAN_EVENTS[occasion_name]
        dt = datetime(current_year, month, day, 6, 0, tzinfo=timezone.utc)
        if dt < now and (now - dt).days > 30:
            # Event for this year has passed; project to next year
            dt = datetime(current_year + 1, month, day, 6, 0, tzinfo=timezone.utc)
        return dt

    # Check Hijri lookup table
    year_map = HIJRI_CALENDAR_ESTIMATES.get(current_year, {})
    if occasion_name in year_map:
        month, day = year_map[occasion_name]
        dt = datetime(current_year, month, day, 6, 0, tzinfo=timezone.utc)
        if dt < now and (now - dt).days > 30:
            next_map = HIJRI_CALENDAR_ESTIMATES.get(current_year + 1, {})
            if occasion_name in next_map:
                n_month, n_day = next_map[occasion_name]
                dt = datetime(current_year + 1, n_month, n_day, 6, 0, tzinfo=timezone.utc)
        return dt

    return None


def calculate_occasion_schedule(
    occasion_text: str, lead_days: int = 3
) -> Optional[Dict[str, Any]]:
    """Calculate the complete schedule and dynamic TTL for an occasion post.

    Returns:
        Dictionary with occasion name, category, event_date_iso, preparation_start,
        recommended_ttl_hours, and schedule_mode ('strict').
    """
    occ = detect_occasion(occasion_text)
    if not occ:
        return None

    event_dt = estimate_event_date(occ["name"])
    if not event_dt:
        return {
            "name": occ["name"],
            "category": occ["category"],
            "is_hijri": occ["is_hijri"],
            "schedule_mode": "strict",
            "event_date": None,
            "ttl_hours": 24,
        }

    now = datetime.now(timezone.utc)
    prep_start = event_dt - timedelta(days=lead_days)
    hours_to_event = max(1, int((event_dt - now).total_seconds() // 3600))
    # TTL expires 4 hours before the event so it is approved on time
    ttl_hours = max(4, hours_to_event - 4) if hours_to_event > 4 else hours_to_event

    return {
        "name": occ["name"],
        "category": occ["category"],
        "is_hijri": occ["is_hijri"],
        "event_date": event_dt.isoformat(),
        "preparation_start": prep_start.isoformat(),
        "ttl_hours": ttl_hours,
        "schedule_mode": "strict",
    }


def get_upcoming_occasions(
    now: Optional[datetime] = None, days_ahead: int = 30
) -> List[Dict[str, Any]]:
    """Scan the Hijri and Gregorian calendar tables for upcoming occasions within days_ahead."""
    now_dt = now or datetime.now(timezone.utc)
    results = []

    all_occasions = list(FIXED_GREGORIAN_EVENTS.keys())
    for yr_map in HIJRI_CALENDAR_ESTIMATES.values():
        for name in yr_map.keys():
            if name not in all_occasions:
                all_occasions.append(name)

    en_translations = {
        "شهر رمضان المبارك": "Holy Month of Ramadan",
        "عيد الفطر المبارك": "Eid Al-Fitr",
        "عيد الأضحى المبارك": "Eid Al-Adha",
        "وقفة يوم عرفة": "Day of Arafah",
        "رأس السنة الهجرية": "Islamic New Year",
        "المولد النبوي الشريف": "Prophet's Birthday (Mawlid)",
        "ذكرى نصر 6 أكتوبر المجيد": "6th of October Victory Day",
        "يوم التأسيس السعودي": "Saudi Founding Day",
        "اليوم الوطني": "National Day",
        "رأس السنة الميلادية": "New Year's Day",
    }

    for occ_name in all_occasions:
        dt = estimate_event_date(occ_name, year=now_dt.year)
        if not dt:
            continue
        delta = dt - now_dt
        days = delta.days
        if 0 <= days <= days_ahead:
            is_hijri = occ_name in HIJRI_CALENDAR_ESTIMATES.get(now_dt.year, {})
            results.append({
                "name_ar": occ_name,
                "name_en": en_translations.get(occ_name, occ_name),
                "event_date": dt.isoformat(),
                "days_until": days,
                "is_hijri": is_hijri,
                "category": "hijri" if is_hijri else "national",
            })

    results.sort(key=lambda x: x["days_until"])
    return results

