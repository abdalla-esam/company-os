#!/usr/bin/env python3
"""publishing.py — PublishingWorker: delivery through the tool bridge.

Pipeline stage: given an ``approved`` capsule, deliver its artifact to the
channels the capsule declares. This worker owns *what* is delivered — the
bound artifact, its digest, the channel list, the delivery rules — and nothing
about *how* any channel works. Delivery is one ``social:publish`` gear call per
channel through :mod:`capsule_engine.bridge`, so the provider's name, its
credentials and its payload format live in the gear, never in the kernel.

What that buys, concretely:

- swapping n8n for Meta Graph, Make, Buffer, or a 40-line local service is a
  change to ``AGENCY_BRIDGE_URL_SOCIAL_PUBLISH`` (see
  :func:`capsule_engine.bridge.resolve_endpoint`) — not a change to this file;
- every delivery leaves a receipt (``receipt_id`` + the tool that answered +
  the digest of its answer) in ``metadata.publishing_receipts``, so a retry
  skips a channel that already accepted the post instead of double-posting;
- a refusal stays a refusal: a gear that declines (policy, missing capability)
  fails the delivery closed and is recorded, never shipped as an empty success;
- the approved image travels as digest-verified bytes
  (``input.media`` = base64 + SHA-256), so "the image a human approved" and
  "the image that gets delivered" are provably the same bytes.

Rules kept from before the bridge existed: strict/queue scheduling
(``publishing_rules``), TOCTOU re-verification of the bound artifact, the
attempt cap, and never publishing the same capsule twice.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence

try:  # normal import (package on sys.path)
    from capsule_engine import BaseWorker, State
    from capsule_engine.bridge import (
        MAX_MEDIA_BYTES,
        BridgeConfigError,
        BridgeError,
        BridgeRefusalError,
        default_bridge,
        gear_contract,
        settle_exchange,
    )
    from capsule_engine.exceptions import (
        CapsuleError,
        CapsuleNotFoundError,
        ManifestSchemaError,
    )
    from capsule_engine.gateway import SpendCapExceeded
    from capsule_engine.governance import domain_of, level_for
    from capsule_engine.manifest import (
        MANIFEST_NAME,
        bound_digest,
        now_iso,
        parse_ts,
        verify_artifact,
    )
except ImportError:  # script/odd-cwd execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capsule_engine import BaseWorker, State
    from capsule_engine.bridge import (
        MAX_MEDIA_BYTES,
        BridgeConfigError,
        BridgeError,
        BridgeRefusalError,
        default_bridge,
        gear_contract,
        settle_exchange,
    )
    from capsule_engine.exceptions import (
        CapsuleError,
        CapsuleNotFoundError,
        ManifestSchemaError,
    )
    from capsule_engine.gateway import SpendCapExceeded
    from capsule_engine.governance import domain_of, level_for
    from capsule_engine.manifest import (
        MANIFEST_NAME,
        bound_digest,
        now_iso,
        parse_ts,
        verify_artifact,
    )

# The verb this stage speaks. One gear, whatever tool implements it.
GEAR = "social:publish"

# This worker's own input/output filenames for the deliverable. The engine's
# hash binding is name-agnostic (see capsule_engine.ARTIFACT_FILE).
COPY_FILE = "copy.md"
IMAGE_FILE = "image.jpg"
IMAGE_MIME = "image/jpeg"

# Read cap for the local artifact (a manifest lives beside it); the *contract*
# cap for in-flight media is stricter (bridge.MAX_MEDIA_BYTES) and is what the
# refusal message below names.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RECEIPTS = 25

# Deployment-wide default channel, used only when the capsule declares none.
DEFAULT_CHANNEL_ENV = "AGENCY_DEFAULT_CHANNEL"

MODE_STRICT = "strict"
MODE_QUEUE = "queue"
KNOWN_MODES = frozenset({MODE_STRICT, MODE_QUEUE})

RECEIPTS_KEY = "publishing_receipts"


class PublishingWorkerError(CapsuleError):
    """Base class for every deliberate PublishingWorker failure."""


class PublishScheduleError(PublishingWorkerError):
    """publishing_rules or scheduled_for is malformed."""


class PublishInputError(PublishingWorkerError):
    """The artifact/image is missing, empty, or too large to deliver."""


class PublishChannelError(PublishingWorkerError):
    """No delivery channel is declared — and guessing one is not an option."""


class PublishDeliveryError(PublishingWorkerError):
    """The delivery gear failed, refused, or is not configured."""


def _clean_channel(value: object) -> str | None:
    """Accept a usable channel name (``linkedin``, ``facebook:page``), else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64 or any(ch.isspace() for ch in text):
        return None
    return text


def delivery_channels(
    manifest: Mapping[str, Any], *, default: str | None = None
) -> list[str]:
    """The channels a capsule declares, in order, without duplicates.

    Resolution order (first hit wins, nothing is invented):

    1. ``manifest["channels"]`` — the capsule's own delivery list;
    2. ``metadata.target_platform`` / ``metadata.platform`` / ``metadata.channel``;
    3. ``default`` (or ``$AGENCY_DEFAULT_CHANNEL``) — an *operator* declaration
       for deployments whose capsules never name a channel;
    4. nothing → :class:`PublishChannelError`. Publishing with no declared
       destination is a governance failure, not a default.

    Raises:
        PublishChannelError: no channel could be resolved.
    """
    declared: list[str] = []
    raw = manifest.get("channels")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            cleaned = _clean_channel(item)
            if cleaned:
                declared.append(cleaned)

    if not declared:
        metadata = manifest.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        for key in ("target_platform", "platform", "channel"):
            cleaned = _clean_channel(meta.get(key))
            if cleaned:
                declared = [cleaned]
                break

    if not declared:
        fallback = default if default is not None else os.environ.get(DEFAULT_CHANNEL_ENV)
        cleaned = _clean_channel(fallback)
        if cleaned:
            declared = [cleaned]

    if not declared:
        raise PublishChannelError(
            "no delivery channel declared: set manifest.channels (or "
            f"metadata.target_platform), or set {DEFAULT_CHANNEL_ENV} for a "
            "deployment-wide default — refusing to guess where to publish"
        )
    return list(dict.fromkeys(declared))


def publishing_receipts(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Receipts of deliveries this capsule has already accepted (may be empty)."""
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = metadata.get(RECEIPTS_KEY)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _metadata_for_write(manifest: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    """Return a writable ``metadata`` object, creating it when absent."""
    metadata = manifest.get("metadata")  # type: ignore[assignment]
    if metadata is None:
        metadata = {}
        manifest["metadata"] = metadata  # type: ignore[index]
    if not isinstance(metadata, dict):
        raise ManifestSchemaError(
            MANIFEST_NAME,
            f"field 'metadata' must be an object to {action}, got {type(metadata).__name__}",
        )
    return metadata


class PublishingWorker(BaseWorker):
    """Deliver an approved capsule to its declared channels, through one gear.

    Args:
        bridge: an object exposing ``invoke(input_data, *, gear, capsule,
            governance, deadline_ms)`` and returning a
            :class:`capsule_engine.bridge.BridgeResult`. Defaults to
            :func:`capsule_engine.bridge.default_bridge` (or the deterministic
            offline gear when ``offline=True``); tests inject a double here.
        gear: the verb to speak — ``social:publish`` unless a branch really
            needs its own delivery contract.
        channels: explicit channel list, overriding what the capsule declares.
        deadline_ms: per-call deadline declared to the gear.
        offline: force the deterministic offline gear (dev runs, tests).
    """

    name: ClassVar[str] = "publishing"
    handles: ClassVar[frozenset[State]] = frozenset({State.APPROVED, State.IN_PROGRESS})
    max_attempts: ClassVar[int] = 3
    lock_timeout: ClassVar[float] = 30.0

    def __init__(
        self,
        *,
        bridge: Optional[Any] = None,
        gear: str = GEAR,
        channels: Optional[Sequence[str]] = None,
        deadline_ms: Optional[int] = None,
        offline: bool | None = None,
    ) -> None:
        self._gear = gear_contract(gear).name  # unknown verbs refused at construction
        self._bridge = bridge
        self._channels = [
            cleaned for cleaned in (_clean_channel(item) for item in (channels or [])) if cleaned
        ]
        if channels is not None and not self._channels:
            raise PublishChannelError("channels= was supplied but holds no usable channel")
        if deadline_ms is not None and (isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int)):
            raise PublishInputError("deadline_ms must be an integer")
        self._deadline_ms = deadline_ms
        self._offline = offline

    @property
    def gear(self) -> str:
        """The verb this worker speaks (never a tool name)."""
        return self._gear

    # ------------------------------------------------------------------ #
    # the stage
    # ------------------------------------------------------------------ #

    def execute(self, capsule_path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        self._active_capsule = Path(capsule_path)
        if not self._due_now(manifest):
            self.record(manifest, "publish deferred: scheduled_for is still in the future")
            return manifest

        metadata = manifest.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        artifact_path = meta.get("artifact_path") or COPY_FILE
        if bound_digest(manifest) is not None:
            verify_artifact(manifest, capsule_path, artifact_path)

        try:
            text = self.read_text(capsule_path, artifact_path)
        except CapsuleNotFoundError as exc:
            raise PublishInputError(f"{artifact_path} missing") from exc
        if not text.strip():
            raise PublishInputError(f"{artifact_path} is empty")

        media = self._media_payload(capsule_path)
        channels = self._channels_for(manifest)

        delivered = {str(entry.get("channel")) for entry in publishing_receipts(manifest)}
        pending = [channel for channel in channels if channel not in delivered]
        if not pending:
            self.record(
                manifest,
                "publish skipped: every declared channel already holds a receipt "
                f"({', '.join(channels)}) — not delivering twice",
            )
            self._finish(manifest, channels, why="all declared channels were already delivered", capsule_path=capsule_path)
            return manifest

        if State.parse(manifest["state"]) is not State.IN_PROGRESS:
            self.transition(manifest, State.IN_PROGRESS, why="publishing started", capsule_path=capsule_path)

        domain = domain_of(manifest)
        capsule_context = {
            "id": str(manifest.get("capsule_id") or Path(capsule_path).name),
            "state": str(manifest.get("state")),
            "attempt": int(manifest.get("attempt") or 0),
            "domain": domain,
        }
        governance_context = {"domain": domain, "autonomy_level": level_for(domain)}

        for channel in pending:
            channel_text = text
            channel_copy = f"deliverables/{channel}/copy.txt"
            if Path(capsule_path, channel_copy).is_file():
                try:
                    c_text = self.read_text(capsule_path, channel_copy)
                    if c_text.strip():
                        channel_text = c_text
                except Exception:
                    pass

            channel_media = media
            channel_img = f"deliverables/{channel}/image.jpg"
            if Path(capsule_path, channel_img).is_file():
                c_media = self._media_payload(capsule_path, image_file=channel_img)
                if c_media is not None:
                    channel_media = c_media

            input_data: dict[str, Any] = {"channel": channel, "text": channel_text}
            if channel_media is not None:
                input_data["media"] = channel_media
            answer = self._deliver(manifest, input_data, capsule_context, governance_context, channel)
            self.record(
                manifest,
                settle_exchange(manifest, answer, stage=f"publish:{channel}", by=self.name),
            )
            self._append_receipt(manifest, channel, answer)

        self._finish(
            manifest,
            channels,
            why=f"delivered {len(pending)} channel(s) through gear {self._gear}",
            capsule_path=capsule_path,
        )
        return manifest

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _channels_for(self, manifest: Mapping[str, Any]) -> list[str]:
        """Declared channels, or the worker's explicit override."""
        if self._channels:
            return list(self._channels)
        return delivery_channels(manifest)

    def _media_payload(
        self, capsule_path: str, *, image_file: str = IMAGE_FILE
    ) -> dict[str, Any] | None:
        """The approved image as contract-ready media, or ``None`` when absent.

        Raises:
            PublishInputError: the image is unreadable, or larger than the
                delivery contract allows in flight (host it and pass
                ``media_urls`` instead — the kernel will not silently upload a
                file the contract cannot carry).
        """
        try:
            raw = self.read_bytes(capsule_path, image_file)
        except CapsuleNotFoundError:
            return None
        except OSError as exc:
            raise PublishInputError(f"{image_file} unreadable: {exc}") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise PublishInputError(f"{IMAGE_FILE} is {len(raw)} bytes (> {MAX_IMAGE_BYTES})")
        if len(raw) > MAX_MEDIA_BYTES:
            raise PublishInputError(
                f"{IMAGE_FILE} is {len(raw)} bytes; the {self._gear} contract caps in-flight "
                f"media at {MAX_MEDIA_BYTES} bytes — host it and pass media_urls instead"
            )
        return {
            "mime": IMAGE_MIME,
            "base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _deliver(
        self,
        manifest: dict[str, Any],
        input_data: Mapping[str, Any],
        capsule_context: Mapping[str, Any],
        governance_context: Mapping[str, Any],
        channel: str,
    ) -> Any:
        """One gear call; a lawful refusal fails the delivery closed.

        Every failure mode (refusal, transport, HTTP, missing configuration) is
        recorded on the capsule and re-raised as :class:`PublishDeliveryError`,
        so ``run()`` counts the attempt and the operator can read why on disk.
        """
        gear = self._gear_instance()
        try:
            answer = gear.invoke(
                input_data,
                gear=self._gear,
                capsule=capsule_context,
                governance=governance_context,
                deadline_ms=self._deadline_ms,
            )
            return answer.require_ok()
        except BridgeRefusalError as exc:
            detail = f"gear refused (code={exc.code}): {exc.detail}".strip()
        except BridgeConfigError as exc:
            detail = f"delivery gear is not configured: {exc}"
        except (BridgeError, SpendCapExceeded) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        self.record(manifest, f"publish delivery failed: {detail}")
        raise PublishDeliveryError(
            f"gear {self._gear} failed for channel {channel!r}: {detail}"
        )

    def _gear_instance(self) -> Any:
        """Resolve and memoize the gear (lazily, so imports never need config)."""
        if self._bridge is None:
            try:
                self._bridge = default_bridge(self._gear, offline=self._offline)
            except BridgeConfigError as exc:
                raise PublishDeliveryError(f"no delivery gear configured: {exc}") from exc
        return self._bridge

    def _append_receipt(self, manifest: Mapping[str, Any], channel: str, answer: Any) -> dict[str, Any]:
        """Record delivery proof for ``channel`` (provider-neutral, bounded)."""
        metadata = _metadata_for_write(manifest, action="record a publishing receipt")
        receipts = [entry for entry in publishing_receipts(manifest)]
        receipt = {
            "channel": channel,
            "tool": answer.tool or None,
            "receipt_id": answer.output.get("receipt_id") if isinstance(answer.output, dict) else None,
            "output_sha256": answer.output_sha256 or None,
            "at": now_iso(),
        }
        receipts.append(receipt)
        metadata[RECEIPTS_KEY] = receipts[-MAX_RECEIPTS:]
        return receipt

    def _finish(self, manifest: dict[str, Any], channels: Sequence[str], *, why: str, capsule_path: Optional[str | Path] = None) -> None:
        """Stamp ``published_via``/``published_at`` and move to ``published``."""
        receipts = publishing_receipts(manifest)
        manifest["published_at"] = manifest.get("published_at") or now_iso()
        manifest["published_via"] = ", ".join(
            f"{entry.get('channel')} via {entry.get('tool') or 'unknown tool'}"
            for entry in receipts
            if entry.get("channel")
        ) or ", ".join(channels)
        self.transition(manifest, State.PUBLISHED, why=why, capsule_path=capsule_path)
        self._remember_published(manifest)

    def _remember_published(self, manifest: dict[str, Any]) -> None:
        """Feed the rolling dedup window with what actually went out.

        Best effort by contract: the delivery already happened, so a dedup
        bookkeeping failure is recorded in history and never raised. Domains
        excluded in ``config/engine.json`` (development/operations/finance)
        are skipped by the engine itself — code duplication is lawful.
        """
        capsule_path = self._active_capsule
        if not capsule_path:
            return
        try:
            from capsule_engine import dedup as dedup_engine
            from capsule_engine.manifest import ARTIFACT_FILE

            metadata = (
                manifest.get("metadata")
                if isinstance(manifest.get("metadata"), dict)
                else {}
            )
            relative = metadata.get("artifact_path") or ARTIFACT_FILE
            text = self.read_text(capsule_path, str(relative))
            domain = manifest.get("domain")
            entry = dedup_engine.record(
                text,
                capsule_id=str(manifest.get("capsule_id") or "unknown"),
                domain=domain if isinstance(domain, str) and domain.strip() else None,
                kind="published",
            )
            if entry is not None:
                self.record(
                    manifest,
                    "dedup index: published proposal recorded "
                    f"(sha256 {entry.sha256[:12]}…)",
                )
        except Exception as exc:
            self.record(
                manifest,
                f"dedup index not updated: {type(exc).__name__}: {exc}",
            )

    def _due_now(self, manifest: dict[str, Any]) -> bool:
        rules = manifest.get("publishing_rules")
        if rules is None:
            return True
        if not isinstance(rules, dict):
            raise PublishScheduleError("publishing_rules must be an object")

        mode = rules.get("mode", MODE_QUEUE)
        if not isinstance(mode, str) or mode not in KNOWN_MODES:
            raise PublishScheduleError(
                f"unknown publishing_rules.mode {mode!r} "
                f"(known: {', '.join(sorted(KNOWN_MODES))})"
            )
        if mode == MODE_QUEUE:
            return True

        scheduled_for = rules.get("scheduled_for")
        if scheduled_for is None or (isinstance(scheduled_for, str) and not scheduled_for.strip()):
            return True
        if not isinstance(scheduled_for, str):
            raise PublishScheduleError(
                "scheduled_for must be an ISO-8601 string, got "
                f"{type(scheduled_for).__name__}"
            )
        due = parse_ts(scheduled_for)
        if due is None:
            raise PublishScheduleError(
                f"scheduled_for is not a parseable ISO-8601 timestamp: {scheduled_for!r}"
            )
        return datetime.now(timezone.utc) >= due
