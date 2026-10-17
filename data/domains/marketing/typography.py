"""typography.py — Optional Arabic typography and banner overlay module.

Design:
- Completely decoupled and standalone module.
- Fail-safe: If anything fails (missing font, text wrapping error, etc.),
  the original raw image bytes are returned untouched without breaking the pipeline.
- Uses PIL, arabic_reshaper, and bidi.algorithm to render crisp Arabic headlines.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


FONTS_DIR = Path(__file__).resolve().parent / "fonts"

ARABIC_FONTS = [
    str(FONTS_DIR / "NotoSansArabic-Bold.ttf"),
    str(FONTS_DIR / "Amiri-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
LATIN_FONTS = [
    str(FONTS_DIR / "Roboto-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def find_font(paths: list[str], size: int = 48) -> Optional[Any]:
    if not PIL_AVAILABLE:
        return None
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def reshape_arabic_text(text: str) -> str:
    """Correctly shapes Arabic glyphs and applies BiDi algorithm for RTL rendering."""
    if not text:
        return ""
    try:
        configuration = {
            'delete_harakat': False,
            'support_ligatures': True,
        }
        reshaper = arabic_reshaper.ArabicReshaper(configuration=configuration)
        reshaped = reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
LOGO_DIR = ASSETS_DIR / "logo"


def overlay_arabic_banner(
    image_bytes: bytes,
    headline: str,
    brand_name: Optional[str] = None,
    social_tag: Optional[str] = None,
    website: Optional[str] = None,
    phone: Optional[str] = None,
    show_logo_badge: bool = True,
    max_width: int = 1080,
) -> tuple[bytes, bool]:
    """Applies a professional modern glassmorphism banner with an Arabic headline,
    optional top brand badge, and social/contact icons footer.

    Returns:
        (result_bytes, applied_boolean)
        If PIL is unavailable or processing fails, returns (image_bytes, False) safely.
    """
    if not PIL_AVAILABLE or not headline or not headline.strip():
        return image_bytes, False

    try:
        from shared_kernel.brand_profile import load_company_profile
        company_prof = load_company_profile()
    except Exception:
        company_prof = {}

    if brand_name is None:
        brand_name = (company_prof.get("brand_name") or company_prof.get("company_name") or "").strip()
    if social_tag is None:
        social_tag = (
            company_prof.get("social_links", {}).get("x")
            or company_prof.get("social_links", {}).get("linkedin")
            or company_prof.get("social_links", {}).get("instagram")
            or company_prof.get("social_tag")
            or company_prof.get("custom_metadata", {}).get("social_tag")
        )
    if website is None:
        website = company_prof.get("website") or company_prof.get("custom_metadata", {}).get("website")
    if phone is None:
        phone = company_prof.get("phone") or company_prof.get("whatsapp") or company_prof.get("custom_metadata", {}).get("phone")

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = img.size

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # 1. Top Brand Watermark Badge (Top-Left)
        emblem_candidate: Optional[Path] = None
        custom_logo = company_prof.get("logo_path")
        if custom_logo and Path(custom_logo).exists():
            emblem_candidate = Path(custom_logo)
        else:
            for cand in [LOGO_DIR / "emblem.png", LOGO_DIR / "logo.png"]:
                if cand.exists():
                    emblem_candidate = cand
                    break

        if show_logo_badge and emblem_candidate and brand_name:
            try:
                badge_w, badge_h = int(width * 0.25), int(height * 0.062)
                badge_x, badge_y = 35, 35
                draw.rounded_rectangle(
                    [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
                    radius=int(badge_h // 2),
                    fill=(10, 16, 26, 220),
                    outline=(0, 215, 255, 180),
                    width=2,
                )
                logo_img = Image.open(emblem_candidate).convert("RGBA")
                logo_size = int(badge_h * 0.65)
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", None))
                logo_resized = logo_img.resize((logo_size, logo_size), resample) if resample else logo_img.resize((logo_size, logo_size))
                logo_x = badge_x + 16
                logo_y = badge_y + (badge_h - logo_size) // 2
                overlay.paste(logo_resized, (logo_x, logo_y), logo_resized)

                font_logo = find_font(LATIN_FONTS, size=int(badge_h * 0.44))
                draw.text(
                    (logo_x + logo_size + 12, badge_y + int(badge_h * 0.22)),
                    brand_name,
                    font=font_logo,
                    fill=(255, 255, 255, 255),
                )
            except Exception:
                pass

        # 2. Lower-Third Glassmorphic Banner
        has_footer_icons = bool(social_tag or website or phone) and ICONS_DIR.exists()
        banner_height = int(height * 0.22) if has_footer_icons else int(height * 0.19)
        banner_top = height - banner_height

        gradient_steps = 45
        for i in range(gradient_steps):
            alpha = int(240 * (i / gradient_steps))
            y = banner_top - gradient_steps + i
            draw.line([(0, y), (width, y)], fill=(8, 14, 24, alpha))

        draw.rectangle([(0, banner_top), (width, height)], fill=(8, 14, 24, 245))

        cyan_accent = (0, 215, 255, 240)
        accent_y = banner_top - gradient_steps
        draw.line([(0, accent_y), (width, accent_y)], fill=cyan_accent, width=3)

        # 3. Arabic Headline
        font_size = max(24, int(width * 0.038))
        font = find_font(ARABIC_FONTS, size=font_size)

        clean_headline = headline.strip().strip("*#_` ").rstrip(".")
        reshaped_headline = reshape_arabic_text(clean_headline)

        bbox = draw.textbbox((0, 0), reshaped_headline, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        text_x = max(20, (width - text_w) // 2)
        text_y = banner_top + int(banner_height * (0.16 if has_footer_icons else 0.28))

        draw.text((text_x + 2, text_y + 2), reshaped_headline, font=font, fill=(0, 0, 0, 230))
        draw.text((text_x, text_y), reshaped_headline, font=font, fill=(255, 255, 255, 255))

        # 4. Social & Contact Bar in Footer
        if has_footer_icons:
            try:
                footer_y = text_y + text_h + 24
                icon_size = max(18, int(width * 0.028))
                font_footer = find_font(LATIN_FONTS, size=max(12, int(width * 0.020)))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", None))

                fb_p = ICONS_DIR / "facebook.png"
                in_p = ICONS_DIR / "linkedin.png"
                x_p = ICONS_DIR / "x.png"
                link_p = ICONS_DIR / "link.png"
                phone_p = ICONS_DIR / "phone.png"

                spacing = 8
                sep_spacing = 26
                seg_list = []

                # Group 1: Social icons + tag
                if social_tag and fb_p.exists() and in_p.exists() and x_p.exists():
                    tag_w = draw.textbbox((0, 0), social_tag, font=font_footer)[2] - draw.textbbox((0, 0), social_tag, font=font_footer)[0]
                    seg_w = (icon_size * 3) + (spacing * 2) + 10 + tag_w
                    seg_list.append(("social", seg_w))

                # Group 2: Web
                if website and link_p.exists():
                    web_w = draw.textbbox((0, 0), website, font=font_footer)[2] - draw.textbbox((0, 0), website, font=font_footer)[0]
                    seg_list.append(("web", icon_size + 8 + web_w))

                # Group 3: Phone
                if phone and phone_p.exists():
                    ph_w = draw.textbbox((0, 0), phone, font=font_footer)[2] - draw.textbbox((0, 0), phone, font=font_footer)[0]
                    seg_list.append(("phone", icon_size + 8 + ph_w))

                if seg_list:
                    total_w = sum(w for _, w in seg_list) + sep_spacing * 2 * (len(seg_list) - 1)
                    curr_x = (width - total_w) // 2

                    for idx, (kind, _) in enumerate(seg_list):
                        if idx > 0:
                            draw.line(
                                [(curr_x, footer_y + 2), (curr_x, footer_y + icon_size - 2)],
                                fill=(0, 215, 255, 120),
                                width=2,
                            )
                            curr_x += sep_spacing

                        if kind == "social":
                            for ip in (fb_p, in_p, x_p):
                                ic = Image.open(ip).resize((icon_size, icon_size), resample) if resample else Image.open(ip).resize((icon_size, icon_size))
                                overlay.paste(ic, (curr_x, footer_y), ic)
                                curr_x += icon_size + spacing
                            curr_x += 2
                            draw.text((curr_x, footer_y + (icon_size - 16) // 2), social_tag, font=font_footer, fill=(220, 235, 255, 230))
                            curr_x += tag_w + sep_spacing
                        elif kind == "web":
                            ic = Image.open(link_p).resize((icon_size, icon_size), resample) if resample else Image.open(link_p).resize((icon_size, icon_size))
                            overlay.paste(ic, (curr_x, footer_y), ic)
                            curr_x += icon_size + 8
                            draw.text((curr_x, footer_y + (icon_size - 16) // 2), website, font=font_footer, fill=cyan_accent)
                            curr_x += web_w + sep_spacing
                        elif kind == "phone":
                            ic = Image.open(phone_p).resize((icon_size, icon_size), resample) if resample else Image.open(phone_p).resize((icon_size, icon_size))
                            overlay.paste(ic, (curr_x, footer_y), ic)
                            curr_x += icon_size + 8
                            draw.text((curr_x, footer_y + (icon_size - 16) // 2), phone, font=font_footer, fill=(220, 235, 255, 230))
                            curr_x += ph_w + sep_spacing
            except Exception:
                pass

        # Composite together
        final_img = Image.alpha_composite(img, overlay).convert("RGB")
        out_buf = io.BytesIO()
        final_img.save(out_buf, format="JPEG", quality=88, optimize=True)
        return out_buf.getvalue(), True

    except Exception:
        # Absolute Fail-Safe: Never crash, return original image
        return image_bytes, False


#: Share of the image height the brand bar takes. At the 768px the keyless
#: provider returns, this is ~58px: enough to cover the name it stamps into the
#: bottom-right corner, and still a footer rather than a crop.
BRAND_BAR_RATIO = 0.075

#: The banner's own palette, so the bar reads as part of the same design.
BRAND_BAR_BG = (8, 14, 24)
BRAND_BAR_ACCENT = (0, 215, 255)
BRAND_BAR_TEXT = (255, 255, 255)


def overlay_brand_bar(
    image_bytes: bytes,
    brand_name: Optional[str] = None,
    website: Optional[str] = None,
    height_ratio: float = BRAND_BAR_RATIO,
) -> tuple[bytes, bool]:
    """Draw the company's own bar across the foot of the image.

    The keyless image provider stamps its own name into the bottom-right corner
    of everything it hands back. This covers that strip with the company's mark
    — the same place, the company's name — and leaves the picture above it
    alone.

    It draws no Arabic, so it must *not* inherit :data:`PIL_AVAILABLE`: that
    flag also requires ``arabic_reshaper``/``bidi``, which the production venv
    does not have, which is why the Arabic banner has never rendered an image
    there. Pillow alone is enough here, and everything else fails safe.

    Args:
        image_bytes: the (already optimized) image payload.
        brand_name: the wordmark; read from ``config/company_profile.json`` when
            omitted, so a rebrand reaches the image without touching code.
        website: the address shown beside the wordmark; same profile source.
        height_ratio: bar height as a share of the image height.

    Returns:
        ``(result_bytes, applied)``. Without Pillow, with nothing to write, or
        on any rendering error: the input bytes untouched and ``False``.
    """
    if brand_name is None or website is None:
        try:
            from shared_kernel.brand_profile import load_company_profile

            profile = load_company_profile()
        except Exception:
            profile = {}
        if brand_name is None:
            brand_name = str(
                profile.get("brand_name") or profile.get("company_name") or ""
            ).strip()
        if website is None:
            website = str(profile.get("website") or "").strip()

    if not brand_name and not website:
        return image_bytes, False

    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        width, height = img.size

        bar_h = max(28, int(height * height_ratio))
        top = height - bar_h
        draw.rectangle([(0, top), (width, height)], fill=BRAND_BAR_BG)
        draw.rectangle([(0, top), (width, top + 3)], fill=BRAND_BAR_ACCENT)

        pad = max(14, int(width * 0.025))
        font_brand = find_font(LATIN_FONTS, size=max(13, int(bar_h * 0.52)))
        font_site = find_font(LATIN_FONTS, size=max(11, int(bar_h * 0.36)))

        brand_box = (
            draw.textbbox((0, 0), brand_name, font=font_brand) if brand_name else None
        )
        site_box = (
            draw.textbbox((0, 0), website, font=font_site) if website else None
        )
        brand_w = (brand_box[2] - brand_box[0]) if brand_box else 0
        site_w = (site_box[2] - site_box[0]) if site_box else 0

        # The wordmark centres in the space the address leaves. If the two would
        # touch, the address is dropped rather than overlapped: the name is the
        # part that has to survive.
        site_x = width - site_w - pad
        brand_x = (width - brand_w) // 2
        if website and (brand_x + brand_w + pad > site_x):
            website, site_box, site_w = "", None, 0
            site_x = width - pad
            brand_x = (width - brand_w) // 2

        if brand_name and brand_box:
            ink_h = brand_box[3] - brand_box[1]
            draw.text(
                (brand_x, top + (bar_h - ink_h) // 2 - brand_box[1]),
                brand_name,
                font=font_brand,
                fill=BRAND_BAR_TEXT,
            )
        if website and site_box:
            ink_h = site_box[3] - site_box[1]
            draw.text(
                (site_x, top + (bar_h - ink_h) // 2 - site_box[1]),
                website,
                font=font_site,
                fill=BRAND_BAR_ACCENT,
            )

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=90, optimize=True, progressive=True)
        return out_buf.getvalue(), True

    except Exception:
        # The banner's contract, for the same reason: a decoration never breaks
        # a pipeline.
        return image_bytes, False
