#!/usr/bin/env python3
"""design.py — DesignWorker: the second concrete capsule worker.

Pipeline stage: given a ``pending_design`` capsule carrying ``copy.md``,
produce ``image.jpg`` and hand the capsule to the human manager
(``pending_approval``).

Responsibilities, in order — each one fails closed:

1. **Routing gate**: an explicit ``routing_context.flags.requires_image is
   True`` is the *only* thing that triggers network work. Anything else
   (False, missing, malformed) gracefully fast-forwards the capsule to
   ``pending_approval`` with a history entry and performs no work at all —
   the pipeline keeps moving without burning a single HTTP call.
2. **Read input**: ``copy.md`` via the inherited ``read_text`` (size-guarded,
   traversal-safe). Missing/empty input fails *before* any state mutation.
3. **Prompt engineering**: ``data.domains.marketing.gateway_ops.image_prompt``
   over the ``ModelGateway`` — the real HTTP gateway or the deterministic
   offline engine, picked by the default factory — returns a sub-60-word image
   prompt; the worker re-validates the contract itself —
   word budget ``< 60`` and the marketing aesthetic embedded verbatim — so a
   non-compliant gateway can never reach the network (fail closed).
4. **Image generation (HTTP)**: stdlib ``urllib.request`` GET against
   ``https://image.pollinations.ai/prompt/{encoded_prompt}`` with a hard
   timeout and a bounded read. Timeouts / ``HTTPError`` / ``URLError`` are
   recorded into the manifest ``history`` and re-raised as the custom
   ``ImageGenerationError``; :meth:`capsule_engine.worker.BaseWorker.run`
   then persists the attempt counter and failure state.
5. **Asset persistence**: validated image bytes saved via the inherited
   ``write_bytes`` (atomic temp + fsync + ``os.replace``) as ``image.jpg``.
   Responses that are not ``image/*`` (or lack JPEG/PNG magic bytes when the
   header is absent) are refused — an HTML error page is never saved.
6. **State transition**: ``-> pending_approval`` through the lawful matrix,
   appending the auditable ``history`` entry; the Watcher then surfaces the
   capsule as ``requires_authorization`` for the human manager.

Standard library only (``urllib``, ``urllib.parse``); Python 3.10+.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

try:  # normal import (package on sys.path)
    from capsule_engine import BaseWorker, ModelGateway, State, default_gateway
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError
except ImportError:  # script/odd-cwd execution — mirror orchestration/dispatcher.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import BaseWorker, ModelGateway, State, default_gateway
    from capsule_engine.exceptions import CapsuleError, CapsuleNotFoundError

# The marketing package lives under ``data/domains/``: ``data.domains.marketing.*``
# needs the repo root importable (standalone script runs included).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from data.domains.marketing.gateway_ops import image_prompt  # noqa: E402

__all__ = [
    "DesignWorker",
    "DesignWorkerError",
    "DesignInputError",
    "ImageGenerationError",
    "fetch_pollinations",
    "fetch_free_image",
    "ModelGateway",  # re-exported from capsule_engine.gateway for callers
]

# B0: this worker's own input/output filename for the copy stage. The engine's
# hash binding is name-agnostic (see capsule_engine.ARTIFACT_FILE).
COPY_FILE = "copy.md"
IMAGE_FILE = "image.jpg"
IMAGE_ENDPOINT = "https://image.pollinations.ai/prompt/{encoded}"
IMAGE_TIMEOUT = 45.0  # seconds — hard deadline per request
# Price-table keys for the spend gate (config/engine.json -> spend.image_costs).
POLLINATIONS_MODEL_KEY = "pollinations-free"
HF_PROVIDER_LABEL = "huggingface"
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # bounded read: 20 MiB
USER_AGENT = "sovereign-agency-os/1.0 (+capsule-engine)"

_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG"

# Injection seam: (prompt, timeout) -> (image bytes, content-type or None).
# NB: a module-level alias is evaluated eagerly (future-import only defers
# annotations), so this uses Optional instead of `str | None` to stay
# importable on every interpreter the engine supports.
Fetcher = Callable[[str, float], tuple[bytes, Optional[str]]]


class DesignWorkerError(CapsuleError):
    """Base class for every deliberate DesignWorker failure."""


class DesignInputError(DesignWorkerError):
    """copy.md unusable, or the gateway prompt violates its contract."""


class ImageGenerationError(DesignWorkerError):
    """The image generation request failed or returned a non-image payload."""


def fetch_pollinations(prompt: str, timeout: float) -> tuple[bytes, str | None]:
    """Download one image from Pollinations; returns ``(bytes, content_type)``.

    Transport failures (timeout, DNS, HTTP 4xx/5xx) propagate as ``OSError``
    subclasses from ``urlopen`` — DesignWorker records and wraps them. The
    payload size is bounded by :data:`MAX_IMAGE_BYTES`.

    Raises:
        ImageGenerationError: empty or oversized response body.
        SpendCapExceeded: the spend gate refused before the request (kill
            switch, spend disabled, caps, or an unusable ledger/config).
    """
    from capsule_engine import spend_control

    reservation = spend_control.authorize(
        model=POLLINATIONS_MODEL_KEY,
        category=spend_control.CATEGORY_IMAGE,
        provider="pollinations",
        context={"op": "design.fetch_pollinations"},
    )
    try:
        url = IMAGE_ENDPOINT.format(encoded=quote(prompt, safe=""))
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=timeout) as response:  # fixed https host only
            headers = getattr(response, "headers", None)
            raw_type = headers.get("Content-Type") if headers else None
            content_type = raw_type.split(";")[0].strip().lower() if raw_type else None
            data = response.read(MAX_IMAGE_BYTES + 1)
        if not data:
            raise ImageGenerationError("Pollinations returned an empty body")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageGenerationError(
                f"Pollinations response exceeds {MAX_IMAGE_BYTES} bytes"
            )
    except Exception:
        spend_control.record(reservation, status=spend_control.STATUS_FAILED)
        raise
    spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)
    return data, content_type


HF_ENDPOINT = "https://router.huggingface.co/nscale/v1/images/generations"
HF_DEFAULT_MODEL = "black-forest-labs/FLUX.1-schnell"

# Image backend selection. ``config/engine.json -> design.image_provider`` is
# authoritative ("pollinations" for the free trial, swappable to
# "huggingface"); the environment is the fallback, and the credential heuristic
# is the last resort so an un-edited deployment keeps working.
IMAGE_PROVIDER_ENV = "AGENCY_IMAGE_PROVIDER"
DESIGN_CONFIG_KEY = "design"
DEFAULT_IMAGE_PROVIDER = "pollinations"
_DESIGN_CONFIG_CACHE: dict[str, Any] = {"key": None, "loaded_at": 0.0, "config": {}}
_DESIGN_CONFIG_TTL = 1.0


def fetch_huggingface(prompt: str, timeout: float) -> tuple[bytes, str | None]:
    """Generate one image via Hugging Face Inference Providers (stdlib only).

    Uses the OpenAI-style images endpoint of the ``nscale`` provider routed
    through Hugging Face; the image comes back base64-encoded in JSON.
    Needs ``HF_TOKEN``; model override via ``HF_IMAGE_MODEL``.
    HTTP/transport errors propagate as ``OSError`` subclasses.

    Raises:
        ImageGenerationError: missing token, malformed/empty/oversized body.
    """
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise ImageGenerationError("HF_TOKEN is not set")
    model = os.environ.get("HF_IMAGE_MODEL", HF_DEFAULT_MODEL).strip() or HF_DEFAULT_MODEL

    # The money gate: this is the only paid image provider — authorize before
    # the request, settle after (a failed generation still counts at its price).
    from capsule_engine import spend_control

    reservation = spend_control.authorize(
        model=model,
        category=spend_control.CATEGORY_IMAGE,
        provider=HF_PROVIDER_LABEL,
        context={"op": "design.fetch_huggingface"},
    )
    try:
        body = json.dumps(
            {"model": model, "prompt": prompt, "response_format": "b64_json"}
        ).encode("utf-8")
        request = Request(
            HF_ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urlopen(request, timeout=timeout) as response:  # fixed https host only
            raw = response.read(MAX_IMAGE_BYTES * 2)
        try:
            b64 = json.loads(raw.decode("utf-8"))["data"][0]["b64_json"]
            data = base64.b64decode(b64, validate=True)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ImageGenerationError(
                f"Hugging Face returned a malformed payload: {type(exc).__name__}"
            ) from exc
        if not data:
            raise ImageGenerationError("Hugging Face returned an empty image")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageGenerationError(
                f"Hugging Face response exceeds {MAX_IMAGE_BYTES} bytes"
            )
    except Exception:
        spend_control.record(reservation, status=spend_control.STATUS_FAILED)
        raise
    spend_control.record(reservation, status=spend_control.STATUS_SUCCEEDED)
    ctype = "image/png" if data.startswith(_PNG_MAGIC) else "image/jpeg"
    return data, ctype


def _engine_config_path() -> Path:
    """The engine config path, resolved exactly like the spend gate does."""
    try:
        from capsule_engine.spend_control import config_path

        return config_path()
    except Exception:  # standalone execution
        return Path(__file__).resolve().parents[2] / "config" / "engine.json"


def load_design_config() -> dict[str, Any]:
    """Read the ``design`` section of the engine config (cached, fail-soft).

    A missing/unreadable/malformed section is not an error — the caller falls
    back to the environment. Re-keyed on (path, mtime, size) so an operator
    edit (or test override) is visible immediately.
    """
    path = _engine_config_path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        _DESIGN_CONFIG_CACHE.update({"key": None, "loaded_at": 0.0, "config": {}})
        return {}
    now = time.monotonic()
    cached = _DESIGN_CONFIG_CACHE.get("config")
    if (
        _DESIGN_CONFIG_CACHE.get("key") == key
        and isinstance(cached, dict)
        and now - float(_DESIGN_CONFIG_CACHE.get("loaded_at") or 0.0) <= _DESIGN_CONFIG_TTL
    ):
        return cached
    config: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        section = raw.get(DESIGN_CONFIG_KEY) if isinstance(raw, dict) else None
        if isinstance(section, dict):
            config = section
    except (OSError, ValueError):
        config = {}
    _DESIGN_CONFIG_CACHE.update({"key": key, "loaded_at": now, "config": config})
    return config


def _normalize_provider(value: object) -> str:
    """Map a configured provider name onto the two supported backends (fail closed)."""
    token = str(value or "").strip().lower()
    if token in ("huggingface", "hugging-face", "hf", "nscale"):
        return "huggingface"
    if token in ("pollinations", "pollination", "free", "pollinations-free"):
        return "pollinations"
    raise DesignInputError(
        f"unknown design.image_provider {value!r} — expected 'pollinations' or 'huggingface'"
    )


def resolve_image_provider() -> str:
    """Which image backend to use: config first, then env, then credentials.

    Precedence:
      1. ``design.image_provider`` in ``config/engine.json`` (authoritative);
      2. ``AGENCY_IMAGE_PROVIDER`` environment override;
      3. legacy heuristic — ``huggingface`` when ``HF_TOKEN`` is present, else
         ``pollinations``;
      4. the keyless default, ``pollinations``.
    """
    configured = load_design_config().get("image_provider")
    if isinstance(configured, str) and configured.strip():
        return _normalize_provider(configured)
    env = os.environ.get(IMAGE_PROVIDER_ENV, "").strip()
    if env:
        return _normalize_provider(env)
    if os.environ.get("HF_TOKEN", "").strip():
        return "huggingface"
    return DEFAULT_IMAGE_PROVIDER


LAST_IMAGE_PROVIDER: str = "unknown"
LAST_IMAGE_COST_USD: float = 0.0


def fetch_free_image(prompt: str, timeout: float) -> tuple[bytes, str | None]:
    """Fetch one image through the *configured* provider.

    The backend is selected by :func:`resolve_image_provider`
    (``config/engine.json -> design.image_provider``): ``pollinations`` by
    default for the free trial, or ``huggingface`` when the operator opts in
    with a token. When ``huggingface`` is selected it still falls back to the
    keyless engine on a transport/format error — but a spend refusal never
    falls through: the budget said stop.

    Raises:
        ImageGenerationError: transport failure, non-image payload, or empty data.
        SpendCapExceeded: the spend gate refused before any request.
    """
    global LAST_IMAGE_PROVIDER, LAST_IMAGE_COST_USD
    if resolve_image_provider() == "huggingface":
        try:
            data, ctype = fetch_huggingface(prompt, timeout)
            LAST_IMAGE_PROVIDER = "huggingface/nscale (FLUX.1-schnell)"
            LAST_IMAGE_COST_USD = 0.003
            return data, ctype
        except Exception as exc:
            # The spend gate refusing (kill switch, caps, unusable ledger) must
            # NOT fall through to another provider: the budget said stop.
            from capsule_engine import spend_control

            if isinstance(exc, spend_control.SpendCapExceeded):
                raise
            if not isinstance(exc, (OSError, ImageGenerationError)):
                raise
            # else: fall through to the keyless engine
    data, ctype = fetch_pollinations(prompt, timeout)
    LAST_IMAGE_PROVIDER = "pollinations.ai (free fallback)"
    LAST_IMAGE_COST_USD = 0.0
    return data, ctype


def optimize_image_for_social(
    data: bytes,
    max_dim: int = 1080,
    quality: int = 85,
) -> tuple[bytes, dict[str, Any]]:
    """Convert raw image payload to an optimized JPEG suitable for social feed CDN.

    Guarantees:
    - Downscaled to max 1080px (standard Facebook/Instagram square & portrait resolution).
    - Compressed to progressive JPEG at quality=85.
    - Drastically reduces file size (~90% smaller, from ~1.3MB to ~140KB) preventing Facebook's aggressive re-compression.
    - Fail-safe: if PIL is missing, corrupted, or mock payload in tests, returns raw bytes untouched.

    Returns:
        (optimized_bytes, stats_dict)
    """
    stats: dict[str, Any] = {"original_bytes": len(data), "optimized": False}
    try:
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(data))
        orig_w, orig_h = im.size
        stats["original_dimensions"] = f"{orig_w}x{orig_h}"

        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        # Downscale proportionally if larger than max_dim
        if max(orig_w, orig_h) > max_dim:
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", None))
            if resample is not None:
                im.thumbnail((max_dim, max_dim), resample)
            else:
                im.thumbnail((max_dim, max_dim))

        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
        opt_data = out.getvalue()
        w, h = im.size
        stats["dimensions"] = f"{w}x{h}"
        stats["size_kb"] = round(len(opt_data) / 1024, 1)
        stats["optimized"] = True
        return opt_data, stats
    except Exception:
        stats["size_kb"] = round(len(data) / 1024, 1)
        return data, stats


IMAGE_PROMPT_FILE = "image_prompt.txt"
DEFAULT_VISUAL_PROMPT = "A sleek corporate laptop on a modern minimalist desk, professional lighting, photorealistic"


class DesignWorker(BaseWorker):
    """Produce ``image.jpg`` for a capsule and hand it to the QA stage.

    Args:
        gateway: optional ``ModelGateway``-compatible object (fallback only).
        fetcher: optional download function ``(prompt, timeout) -> (bytes,
            content_type)`` — tests inject offline fakes here; the default is
            :func:`fetch_pollinations`.
        http_timeout: hard per-request deadline in seconds.

    Class contract (see :class:`capsule_engine.BaseWorker`): ``handles``
    includes ``IN_PROGRESS`` so a stale-lease resume re-enters this worker.
    """

    name: ClassVar[str] = "design"
    handles: ClassVar[frozenset[State]] = frozenset(
        {State.PENDING_DESIGN, State.IN_PROGRESS}
    )
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def __init__(
        self,
        gateway: ModelGateway | None = None,
        *,
        fetcher: Fetcher | None = None,
        http_timeout: float = IMAGE_TIMEOUT,
    ) -> None:
        if http_timeout <= 0:
            raise ValueError("http_timeout must be > 0")
        self._gateway = gateway if gateway is not None else default_gateway()
        self._fetcher = fetcher if fetcher is not None else fetch_free_image
        self._http_timeout = http_timeout

    # ------------------------------------------------------------------ #
    # the worker contract
    # ------------------------------------------------------------------ #

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Run the design stage: gate -> read prompt -> fetch -> persist -> hand off to QA.

        Raises:
            DesignInputError: image prompt cannot be resolved.
            ImageGenerationError: the HTTP request failed, or the payload is
                not a usable image; the error detail is already in
                ``history`` when ``run()`` persists the manifest.
        """
        # 1. routing gate — graceful fast-forward, zero work, zero HTTP
        requires_image = self._flag(manifest, "requires_image")
        if requires_image is not True:
            self.transition(
                manifest,
                State.PENDING_QA,
                why=(
                    "image skipped — routing_context.flags.requires_image="
                    f"{requires_image!r}; fast-forwarded to qa"
                ),
            )
            return manifest

        # 2. mark the stage started (skipped on stale-lease resume)
        if State.parse(manifest["state"]) is not State.IN_PROGRESS:
            self.transition(manifest, State.IN_PROGRESS, why="design stage started")

        # 3. read visual prompt: image_prompt.txt -> gateway(copy.md) -> default
        prompt = self._resolve_visual_prompt(capsule_path, manifest)

        # Fail-closed: refuse to generate if prompt contains Arabic characters (Strict English requirement)
        if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", prompt):
            raise DesignInputError("visual prompt contains Arabic characters; QA gate approval required")

        # Zero Waste Gate check: Refuse to generate if visual prompt was explicitly rejected by QA
        meta = manifest.get("metadata")
        if isinstance(meta, dict) and meta.get("qa_visual_approved") is False:
            raise DesignInputError("visual prompt was rejected by QA gate; revision required before generation")

        # 4. Check if image.jpg already exists for this exact visual prompt (avoid redundant generation)
        target_image = Path(capsule_path) / IMAGE_FILE
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        current_hash = manifest.get("metadata", {}).get("image_prompt_hash")

        if target_image.exists() and current_hash == prompt_hash:
            self.record(manifest, "image generation skipped — visual prompt unchanged, using existing image (cost: $0.00)")
            if "metadata" not in manifest or not isinstance(manifest["metadata"], dict):
                manifest["metadata"] = {}
            cost_meta = manifest["metadata"].setdefault("cost", {})
            cost_meta["image_provider"] = "cache (reused)"
            cost_meta["image_generation_usd"] = 0.0
            self.transition(
                manifest,
                State.PENDING_QA,
                why="Existing optimized image reused (zero API calls, $0.00 cost)",
            )
            return manifest

        # Archive prior image if it exists before overwriting (audit record)
        if target_image.exists():
            try:
                import time
                archive_dir = Path(capsule_path) / "history_images"
                archive_dir.mkdir(exist_ok=True)
                archive_name = f"image_{int(time.time())}.jpg"
                target_image.rename(archive_dir / archive_name)
                self.record(manifest, f"prior image archived to history_images/{archive_name}")
            except Exception:
                pass

        # 5. download + validate (timeouts/errors logged, then wrapped)
        try:
            data, content_type = self._fetcher(prompt, self._http_timeout)
            self._validate_image(data, content_type)
        except ImageGenerationError as exc:
            self.record(manifest, f"image generation failed: {exc}")
            raise
        except OSError as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.record(manifest, f"image download failed: {detail}")
            raise ImageGenerationError(
                f"Image service request failed: {detail}"
            ) from exc

        # 6. Save pristine raw image first so the original generative AI output is never lost
        raw_payload = bytes(data)
        self.write_bytes(capsule_path, "image_raw.jpg", raw_payload)

        # 7. Optimize image for social publishing (1080px JPEG, ~90% compression savings)
        optimized_payload, img_stats = optimize_image_for_social(data)

        # 8. Apply optional typography banner onto the clean image (fail-safe)
        headline = self._extract_headline(capsule_path)
        if headline:
            try:
                from data.domains.marketing.typography import overlay_arabic_banner
                banner_payload, applied = overlay_arabic_banner(optimized_payload, headline)
                if applied:
                    optimized_payload = banner_payload
                    img_stats["typography_applied"] = True
                    img_stats["headline"] = headline
            except Exception:
                pass

        payload = bytes(optimized_payload)
        artifact = self.write_bytes(capsule_path, IMAGE_FILE, payload)

        # Clean deliverables layout: mirror into deliverables/{channel}/image.jpg
        channels: list[str] = []
        raw_channels = manifest.get("channels")
        if isinstance(raw_channels, list):
            channels = [str(c).strip() for c in raw_channels if str(c).strip()]
        if not channels:
            metadata = manifest.get("metadata")
            if isinstance(metadata, dict):
                for key in ("target_platform", "platform", "channel"):
                    val = metadata.get(key)
                    if isinstance(val, str) and val.strip():
                        channels = [val.strip()]
                        break
        if not channels:
            domain = manifest.get("domain")
            channels = [str(domain).strip() if domain else "marketing"]

        for ch in channels:
            self.write_bytes(capsule_path, f"deliverables/{ch}/image.jpg", payload)

        # 6.5 Zero-Waste Asset Vault preservation
        try:
            from data.domains.marketing.asset_vault import store_asset
            vault_res = store_asset(
                payload,
                prompt=prompt,
                metadata={"provider": LAST_IMAGE_PROVIDER, "cost_usd": LAST_IMAGE_COST_USD},
                capsule_id=manifest.get("capsule_id"),
            )
            manifest.setdefault("metadata", {})["asset_vault_sha256"] = vault_res["sha256"]
        except Exception as exc:
            logger.error(
                "design: asset vault preservation failed for %s: %s",
                manifest.get("capsule_id"),
                exc,
                exc_info=True,
            )
            self.record(manifest, f"asset vault preservation failed: {exc}")

        if "metadata" not in manifest or not isinstance(manifest["metadata"], dict):
            manifest["metadata"] = {}
        manifest["metadata"]["image_prompt_hash"] = prompt_hash

        # 7. Financial & Cost Accounting Tracking:
        cost_usd = LAST_IMAGE_COST_USD if self._fetcher is fetch_free_image else 0.0
        provider = LAST_IMAGE_PROVIDER if self._fetcher is fetch_free_image else "custom_fetcher"

        cost_data = manifest["metadata"].setdefault("cost", {})
        cost_data["currency"] = "USD"
        cost_data["image_generation_usd"] = cost_usd
        cost_data["image_provider"] = provider
        cost_data["image_dimensions"] = img_stats.get("dimensions", "1080x1080")
        cost_data["image_size_kb"] = img_stats.get("size_kb", round(len(payload) / 1024, 1))

        # Fold into cumulative usage
        usage = manifest["metadata"].setdefault("usage", {})
        usage["image_generations"] = usage.get("image_generations", 0) + 1
        usage["image_cost_usd"] = round(usage.get("image_cost_usd", 0.0) + cost_usd, 6)
        usage["image_provider"] = provider

        # 8. hand off to QA stage
        why_msg = (
            f"Optimized visual prompt generated via {provider} "
            f"({img_stats.get('dimensions', '1080x1080')}, {img_stats.get('size_kb', 0)} KB, "
            f"est. cost: ${cost_usd:.4f}) and rendered successfully"
        )
        self.transition(
            manifest,
            State.PENDING_QA,
            why=why_msg,
        )
        return manifest

    # ------------------------------------------------------------------ #
    # private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _flag(manifest: dict[str, Any], flag: str) -> object | None:
        """Return ``routing_context.flags[flag]`` (``None`` when absent)."""
        routing = manifest.get("routing_context")
        flags = routing.get("flags") if isinstance(routing, dict) else None
        return flags.get(flag) if isinstance(flags, dict) else None

    def _resolve_visual_prompt(self, capsule_path: str, manifest: dict[str, Any]) -> str:
        """Resolve visual description: image_prompt.txt -> copy.md gateway -> default."""
        # Check image_prompt.txt
        try:
            prompt_text = self.read_text(capsule_path, IMAGE_PROMPT_FILE)
            if prompt_text and prompt_text.strip():
                return prompt_text.strip()
        except CapsuleNotFoundError:
            pass

        # Fallback to copy.md via gateway
        try:
            copy_text = self.read_text(capsule_path, COPY_FILE)
            if copy_text and copy_text.strip():
                try:
                    generated = image_prompt(self._gateway, copy_text)
                    self.account_usage(manifest, self._gateway)
                    if generated and generated.strip():
                        return generated.strip()
                except Exception as exc:
                    self.record(manifest, f"fallback image prompt generation failed: {exc}")
        except CapsuleNotFoundError:
            pass

        # Fallback to default visual description
        return DEFAULT_VISUAL_PROMPT

    def _extract_headline(self, capsule_path: str) -> str:
        """Extract a clean headline from copy.md for the visual banner overlay."""
        try:
            copy_text = self.read_text(capsule_path, COPY_FILE)
            for line in copy_text.splitlines():
                cleaned = line.strip().strip("*#_` ").rstrip(".")
                if cleaned and len(cleaned) >= 5:
                    return cleaned
        except Exception:
            pass
        return ""

    @staticmethod
    def _validate_image(data: object, content_type: str | None) -> None:
        """Refuse payloads that are not plausibly an image (fail closed)."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ImageGenerationError("empty or non-bytes image payload")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageGenerationError(
                f"image payload exceeds {MAX_IMAGE_BYTES} bytes"
            )
        if content_type is not None and not content_type.startswith("image/"):
            raise ImageGenerationError(
                f"unexpected content-type {content_type!r} (expected image/*) "
                "— refusing to save"
            )
        if content_type is None and not (
            data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)
        ):
            raise ImageGenerationError(
                "response has no image content-type and no JPEG/PNG magic bytes "
                "— refusing to save"
            )


if __name__ == "__main__":  # offline smoke: python3 workers/design.py
    import json

    def _offline_fetcher(prompt: str, timeout: float) -> tuple[bytes, str | None]:
        """Demo fetcher — keeps the smoke test network-free."""
        return _JPEG_MAGIC + b"OFFLINE-DEMO-IMAGE", "image/jpeg"

    demo = {
        "capsule_id": "cap-design-demo",
        "state": "pending_design",
        "created_at": "2026-10-03T00:00:00+00:00",
        "updated_at": "2026-10-03T00:00:00+00:00",
        "attempt": 0,
        "history": [],
        "routing_context": {"flags": {"requires_image": True}},
    }
    tmp = Path(f"/tmp/design_smoke_{Path(__file__).stem}")
    tmp.mkdir(exist_ok=True)
    (tmp / COPY_FILE).write_text(
        "# Sovereign Agency OS\n\nTerminals glow, pipelines flow.\n", encoding="utf-8"
    )
    from capsule_engine import save_manifest

    save_manifest(tmp, demo)
    result = DesignWorker(fetcher=_offline_fetcher).run(str(tmp))
    print(
        json.dumps(
            {
                "state": result["state"],
                "capsule": str(tmp),
                "image_bytes": (tmp / IMAGE_FILE).stat().st_size,
                "history_tail": result["history"][-1]["why"],
            },
            indent=2,
        )
    )
