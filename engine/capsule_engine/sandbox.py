#!/usr/bin/env python3
"""sandbox.py — Capsule Sandbox & Ticket-by-Ticket Trial Engine.

Provides an isolated environment (capsules_sandbox/) where engineers and operators
can manufacture, trial, test, and inspect capsules ticket-by-ticket across all 9 domains
without polluting the production spool (capsules_spool/).

Guarantees:
- Zero side-effects by default: offline ModelGateway, zero external network.
- Full constitutional pipeline: exercises worker -> qa -> design -> qa -> gate.
- Cryptographic verification: verifies TOCTOU hash bindings and audit chains.
- Safe promotion: transfers a verified capsule into the production spool — it
  is seated at ``pending_approval`` with a fresh gate plan, whatever state it
  held in the sandbox (sandbox approvals never carry over).
- Standard library Python 3.10+ only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from shared_kernel.paths import CAPSULES_SANDBOX, CAPSULES_SPOOL, DOMAINS_DIR, REPO_ROOT
except Exception:
    REPO_ROOT = Path(__file__).resolve().parents[2]
    DOMAINS_DIR = REPO_ROOT / "data" / "domains"
    CAPSULES_SPOOL = REPO_ROOT / "data" / "capsules" / "spool"
    CAPSULES_SANDBOX = REPO_ROOT / "data" / "capsules" / "sandbox"

try:
    from capsule_engine import (
        ARTIFACT_FILE,
        CapsuleError,
        CapsuleNotFoundError,
        SecurityError,
        State,
        load_manifest,
        now_iso,
        save_manifest,
        transition,
    )
    from capsule_engine.audit import verify_chain
    from capsule_engine.locking import CapsuleLock
    from capsule_engine.manifest import (
        artifact_sha256,
        new_manifest,
        seat_at_approval_gate,
        validate_manifest,
        verify_artifact,
    )
except ImportError:
    sys.path.insert(0, str(REPO_ROOT))
    from capsule_engine import (
        ARTIFACT_FILE,
        CapsuleError,
        CapsuleNotFoundError,
        SecurityError,
        State,
        load_manifest,
        now_iso,
        save_manifest,
        transition,
    )
    from capsule_engine.audit import verify_chain
    from capsule_engine.locking import CapsuleLock
    from capsule_engine.manifest import (
        artifact_sha256,
        new_manifest,
        seat_at_approval_gate,
        validate_manifest,
        verify_artifact,
    )

logger = logging.getLogger("capsule_engine.sandbox")

DEFAULT_SANDBOX = CAPSULES_SANDBOX
DEFAULT_SPOOL = CAPSULES_SPOOL
DOMAINS_ROOT = DOMAINS_DIR

#: Publishing credentials a trial must never see: the sandbox can exercise
#: the publishing worker's *state machine*, but the request must never be
#: able to leave for a real bridge, social API, or webhook.
PUBLISH_ENV_VARS: tuple[str, ...] = (
    "FB_PAGE_ID",
    "META_SYSTEM_TOKEN",
    "N8N_PUBLISH_WEBHOOK_URL",
    "N8N_WEBHOOK_URL",
    "N8N_API_KEY",
    "EVOLUTION_API_KEY",
    "IG_ACCOUNT_ID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)
PUBLISH_ENV_PREFIXES: tuple[str, ...] = ("AGENCY_BRIDGE_URL",)


def trial_env_overrides(live: bool) -> dict[str, str]:
    """Environment overlay for one sandbox trial.

    Publishing is locked down in **every** mode — ``live=True`` keeps real
    model/image credentials (that is the point of a live trial) but still
    blanks the publish bridge (``AGENCY_BRIDGE_URL*``) and the social/webhook
    tokens, so ``auto_approve`` can sign the capsule and drive the publishing
    worker to a *refusal* (``resolve_endpoint`` -> None -> BridgeConfigError),
    never to a real post. ``live=False`` additionally forces the offline
    gateway and blanks the image token.
    """
    overrides: dict[str, str] = {}
    for name in PUBLISH_ENV_VARS:
        overrides[name] = ""
    for name in os.environ:
        if name.startswith(PUBLISH_ENV_PREFIXES):
            overrides[name] = ""
    if not live:
        overrides["AGENCY_OFFLINE_MODE"] = "1"
        overrides["HF_TOKEN"] = ""
    return overrides


class SandboxError(CapsuleError):
    """Base exception for sandbox operations."""


class TicketNotFoundError(SandboxError):
    """Ticket was not found in any domain or directory."""


class PromotionError(SandboxError):
    """Failed to promote capsule from sandbox to production."""


class CapsuleSandbox:
    """Manages isolated trial runs and lifecycle in capsules_sandbox/."""

    def __init__(
        self,
        sandbox_root: Path | str | None = None,
        spool_root: Path | str | None = None,
        domains_root: Path | str | None = None,
    ) -> None:
        self.repo_root = REPO_ROOT
        self.sandbox_root = Path(
            sandbox_root or os.environ.get("CAPSULES_SANDBOX", DEFAULT_SANDBOX)
        ).resolve()
        self.spool_root = Path(
            spool_root or os.environ.get("CAPSULES_SPOOL", DEFAULT_SPOOL)
        ).resolve()
        self.domains_root = Path(
            domains_root or os.environ.get("DOMAINS_DIR", DOMAINS_ROOT)
        ).resolve()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.spool_root.mkdir(parents=True, exist_ok=True)

    def find_ticket(
        self, ticket_id: str, sector: str | None = None
    ) -> tuple[str, Path, dict[str, Any]]:
        """Search for a ticket by ID across domain folders (scratch, approved, root).

        Returns: (sector, ticket_path, ticket_dict)
        Raises: TicketNotFoundError if not found.
        """
        clean_tid = ticket_id.strip()
        sectors_to_check = [sector] if sector else [
            d.name for d in sorted(self.domains_root.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]

        for sec in sectors_to_check:
            tdir = self.domains_root / sec / "tickets"
            if not tdir.is_dir():
                continue

            subdirs = [tdir / "approved", tdir / "scratch", tdir]
            for folder in subdirs:
                if not folder.is_dir():
                    continue
                for f in sorted(folder.iterdir()):
                    if (
                        f.is_file()
                        and f.name.endswith(".json")
                        and not f.name.endswith(".tmp")
                        and "sync-conflict" not in f.name
                        and not f.name.startswith(".syncthing")
                    ):
                        # Match by filename or by ticket_id inside JSON
                        match_by_name = (
                            f.name == f"ticket_{clean_tid}.json"
                            or clean_tid in f.name
                        )
                        if match_by_name:
                            try:
                                data = json.loads(f.read_text(encoding="utf-8"))
                                return sec, f, data
                            except Exception:
                                pass
                        else:
                            # Peek ticket_id field inside JSON
                            try:
                                data = json.loads(f.read_text(encoding="utf-8"))
                                if isinstance(data, dict) and data.get("ticket_id") == clean_tid:
                                    return sec, f, data
                            except Exception:
                                pass

        raise TicketNotFoundError(
            f"Ticket '{ticket_id}' not found in {'sector ' + sector if sector else 'any sector'}"
        )

    def create_trial_capsule(
        self,
        ticket_id: str,
        sector: str | None = None,
        capsule_id: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[str, Path, dict[str, Any]]:
        """Materialize an isolated trial capsule inside capsules_sandbox/ from a ticket.

        Returns: (capsule_id, capsule_path, manifest_data)
        """
        found_sec, ticket_path, ticket_data = self.find_ticket(ticket_id, sector)
        effective_sec = sector or found_sec or "marketing"

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        sanitized_tid = "".join(c if c.isalnum() or c in "-_" else "_" for c in ticket_id)
        cid = capsule_id or f"sbx_{effective_sec}_{sanitized_tid}_{ts}"

        # Resolve prompt and topic flexibly
        raw_request = (
            ticket_data.get("request")
            or ticket_data.get("title")
            or ticket_data.get("prompt")
        )
        context = ticket_data.get("context") or {}
        topic = (
            context.get("topic")
            or ticket_data.get("title")
            or (raw_request[:80] if raw_request else f"Trial topic for {ticket_id}")
        )
        if not raw_request:
            raw_request = (
                f"Draft an executive thought-leadership post on {topic} for enterprise leaders."
            )

        channels = ticket_data.get("channels") or ["linkedin"]
        platform = (
            context.get("target_platform")
            or ticket_data.get("target_platform")
            or channels[0]
        )

        manifest = new_manifest(
            capsule_id=cid,
            state=State.DRAFT,
            tickets=[ticket_id],
            channels=channels,
        )

        try:
            from workers import WORKERS, domain_workers
            available_workers = set(WORKERS.keys()) | set(domain_workers().keys())
        except Exception:
            available_workers = {"marketing"}

        factory_domain = effective_sec if effective_sec in available_workers else "marketing"

        manifest["domain"] = factory_domain
        manifest["raw_prompt"] = raw_request
        manifest["routing_context"] = {
            "domain": factory_domain,
            "flags": {
                "requires_copy": True,
                "requires_image": True,
                "requires_qa": True,
            },
        }

        manifest["metadata"] = {
            "is_sandbox": True,
            "source_ticket_id": ticket_id,
            "source_sector": effective_sec,
            "source_ticket_file": ticket_path.name,
            "trial_at": now_iso(),
            "topic": topic,
            "target_platform": str(platform).lower(),
            "bundle_breakdown": {
                "ticket_id": ticket_id,
                "sector": effective_sec,
            },
        }

        manifest["publishing_rules"] = {"mode": "queue", "scheduled_for": None}

        if overrides:
            for k, v in overrides.items():
                if isinstance(v, dict) and isinstance(manifest.get(k), dict):
                    manifest[k].update(v)
                else:
                    manifest[k] = v

        cap_dir = self.sandbox_root / cid
        cap_dir.mkdir(parents=True, exist_ok=True)
        # Creation takes the capsule's own lock before anything is written
        # inside it: a busy capsule raises LockTimeoutError instead of
        # writing unlocked, and no concurrent writer can race the manifest.
        with CapsuleLock(cap_dir, purpose="sandbox:create_trial_capsule"):
            save_manifest(cap_dir, manifest)

        return cid, cap_dir, manifest

    def run_trial(
        self,
        ticket_id: str,
        sector: str | None = None,
        live: bool = False,
        max_transitions: int = 10,
        reviewer: str = "sandbox_reviewer",
        auto_approve: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a full trial pipeline for a ticket in isolation.

        1. Creates sandbox capsule.
        2. Configures isolated offline environment (unless live).
        3. Drives pulse transitions step-by-step through workers.
        4. Optionally signs approval and publishes if auto_approve=True.
        5. Gathers full diagnostics and returns report.
        """
        cid, cap_dir, init_manifest = self.create_trial_capsule(
            ticket_id, sector=sector, overrides=overrides
        )

        # Prepare child execution environment: publishing is ALWAYS locked
        # down (see trial_env_overrides) — a sandbox never reaches a real
        # publish bridge, whatever the live flag says.
        prev_env = dict(os.environ)
        try:
            os.environ.update(trial_env_overrides(live))
            os.environ["AGENCY_INCREMENTAL_PULSE"] = "off"
            os.environ["CAPSULES_SPOOL"] = str(self.sandbox_root)

            # Lazy import capsule_pulse to avoid top-level circular dependencies
            from orchestration.capsule_pulse import run_pulse

            # Phase 1: Run pulses until held at gate or terminal
            pulse_reports = []
            for attempt in range(1, 6):
                rep = run_pulse(
                    spool=self.sandbox_root,
                    max_transitions=max_transitions,
                    scan="full",
                )
                pulse_reports.append(rep)
                current_state = self._read_state(cap_dir)
                if current_state in (
                    "pending_approval",
                    "failed",
                    "stalled",
                    "published",
                    "done",
                ):
                    break

            interim_state = self._read_state(cap_dir)

            # Phase 2: If auto-approve is requested and reached gate, sign and publish
            if auto_approve and interim_state == "pending_approval":
                with CapsuleLock(cap_dir):
                    mf = load_manifest(cap_dir)
                    rules = mf.get("publishing_rules") or {}
                    if rules.get("scheduled_for"):
                        mf["publishing_rules"] = {"mode": "queue", "scheduled_for": None}
                    transition(
                        mf,
                        State.APPROVED,
                        by=reviewer,
                        why="sandbox trial auto-approval verification",
                        capsule_path=cap_dir,
                    )
                    save_manifest(cap_dir, mf)

                # Final pulse to exercise publishing worker
                pub_rep = run_pulse(
                    spool=self.sandbox_root,
                    max_transitions=max_transitions,
                    scan="full",
                )
                pulse_reports.append(pub_rep)

        finally:
            os.environ.clear()
            os.environ.update(prev_env)

        # Gather final diagnostics
        final_manifest = load_manifest(cap_dir)
        final_state = final_manifest.get("state")
        meta = final_manifest.get("metadata") or {}

        # Collect produced artifacts
        artifacts_info: dict[str, Any] = {}
        for entry in sorted(cap_dir.iterdir()):
            if entry.is_file() and entry.name != "manifest.json" and not entry.name.endswith(".lock"):
                content_bytes = entry.read_bytes()
                digest = hashlib.sha256(content_bytes).hexdigest()
                info: dict[str, Any] = {
                    "size_bytes": len(content_bytes),
                    "sha256": digest,
                }
                if entry.name.endswith(".md") or entry.name.endswith(".txt"):
                    try:
                        text = content_bytes.decode("utf-8")
                        info["preview"] = text[:300] + ("..." if len(text) > 300 else "")
                        info["lines"] = len(text.splitlines())
                    except Exception:
                        pass
                artifacts_info[entry.name] = info

        history = final_manifest.get("history") or []
        transitions = [
            {
                "seq": h.get("seq"),
                "from": h.get("from_state"),
                "to": h.get("to_state"),
                "by": h.get("by"),
                "why": h.get("why"),
                "at": h.get("at"),
            }
            for h in history
            if h.get("to_state") or h.get("from_state")
        ]

        success_states = ("pending_approval", "approved", "published", "done")
        is_ok = final_state in success_states

        return {
            "ok": is_ok,
            "capsule_id": cid,
            "ticket_id": ticket_id,
            "sector": final_manifest.get("domain", sector),
            "state": final_state,
            "initial_state": init_manifest.get("state"),
            "transitions_count": len(transitions),
            "transitions": transitions,
            "artifact_sha256": meta.get("artifact_sha256"),
            "bound_artifact_path": meta.get("artifact_path"),
            "artifacts": artifacts_info,
            "usage": meta.get("usage") or {},
            "publishing_receipts": meta.get("publishing_receipts"),
            "capsule_path": str(cap_dir),
            "is_sandbox": True,
            "live_mode": live,
        }

    def promote(
        self,
        sandbox_capsule_id: str,
        by: str = "operator",
        note: str = "",
    ) -> dict[str, Any]:
        """Atomically promote a verified capsule from sandbox to production spool.

        Checks:
        1. Source exists and manifest schema is valid.
        2. TOCTOU artifact hash verification passes (cryptographic match).
        3. Target does not already exist in production spool.

        The record **always lands at ``pending_approval`` with a fresh gate
        plan**, whatever state it held in the sandbox — sandbox approvals
        (including ``auto_approve`` trial runs) never carry over, so no capsule
        can enter the production spool already approved or published. A
        seating failure rolls the copy back (the source stays for a retry).
        """
        src_dir = self.sandbox_root / sandbox_capsule_id
        if not src_dir.is_dir():
            raise CapsuleNotFoundError(f"Sandbox capsule '{sandbox_capsule_id}' not found")

        src_manifest = load_manifest(src_dir)
        validate_manifest(src_manifest)

        # Enforce TOCTOU artifact integrity before moving to production
        meta = src_manifest.get("metadata") or {}
        bound_sha = meta.get("artifact_sha256")
        bound_path = meta.get("artifact_path", ARTIFACT_FILE)

        if bound_sha:
            actual_sha = artifact_sha256(src_dir, bound_path)
            if actual_sha != bound_sha:
                raise SecurityError(
                    f"TOCTOU violation during promotion: {bound_path} sha256 mismatch "
                    f"({actual_sha} != {bound_sha})"
                )

        dst_dir = self.spool_root / sandbox_capsule_id
        if dst_dir.exists():
            raise PromotionError(
                f"Cannot promote: capsule '{sandbox_capsule_id}' already exists in production spool"
            )

        # Copy whole capsule tree to production spool
        shutil.copytree(src_dir, dst_dir)

        # Seat at the approval gate: pending_approval + fresh gate plan + bound
        # digest + chained audit record. Nothing else may enter the spool.
        try:
            with CapsuleLock(dst_dir):
                dst_manifest = load_manifest(dst_dir)
                now = now_iso()

                seat_at_approval_gate(
                    dst_manifest,
                    dst_dir,
                    by=by,
                    why=f"Promoted from sandbox to production spool: {note}".strip(),
                )

                dst_meta = dst_manifest.setdefault("metadata", {})
                dst_meta["is_sandbox"] = False
                dst_meta["promoted_at"] = now
                dst_meta["promoted_by"] = by
                dst_meta["promotion_note"] = note or "Verified via capsule sandbox trial"

                dst_manifest["updated_at"] = now
                save_manifest(dst_dir, dst_manifest)
        except Exception as exc:
            # Never leave a production copy that could not be seated at the
            # gate: roll the copy back, keep the source for a retry.
            shutil.rmtree(dst_dir, ignore_errors=True)
            raise PromotionError(f"Promotion refused: {exc}") from exc

        # Remove source sandbox directory
        shutil.rmtree(src_dir, ignore_errors=True)

        return {
            "ok": True,
            "capsule_id": sandbox_capsule_id,
            "spool_path": str(dst_dir),
            "state": dst_manifest.get("state"),
            "promoted_at": now,
            "promoted_by": by,
        }

    def list_sandbox_capsules(self) -> list[dict[str, Any]]:
        """List all trial capsules currently inside capsules_sandbox/."""
        if not self.sandbox_root.is_dir():
            return []

        results = []
        for item in sorted(self.sandbox_root.iterdir()):
            if item.is_dir() and (item / "manifest.json").is_file():
                try:
                    mf = json.loads((item / "manifest.json").read_text(encoding="utf-8"))
                    meta = mf.get("metadata") or {}
                    results.append(
                        {
                            "capsule_id": mf.get("capsule_id", item.name),
                            "state": mf.get("state"),
                            "domain": mf.get("domain"),
                            "created_at": mf.get("created_at"),
                            "updated_at": mf.get("updated_at"),
                            "source_ticket_id": meta.get("source_ticket_id"),
                            "has_copy": (item / "copy.md").is_file(),
                            "has_image": (item / "image.jpg").is_file(),
                            "artifact_sha256": meta.get("artifact_sha256"),
                            "path": str(item),
                        }
                    )
                except Exception as exc:
                    logger.warning("Unreadable sandbox capsule %s: %s", item.name, exc)

        return sorted(results, key=lambda x: str(x.get("created_at") or ""), reverse=True)

    def inspect_sandbox_capsule(self, capsule_id: str) -> dict[str, Any]:
        """Deep inspection of a sandbox capsule manifest, audit chain, and artifacts."""
        cap_dir = self.sandbox_root / capsule_id
        if not cap_dir.is_dir():
            raise CapsuleNotFoundError(f"Sandbox capsule '{capsule_id}' not found")

        manifest = load_manifest(cap_dir)
        meta = manifest.get("metadata") or {}
        history = manifest.get("history") or []

        # Audit chain validation
        chain_valid = False
        chain_detail = ""
        try:
            status = verify_chain(history)
            chain_valid = status.ok if hasattr(status, "ok") else bool(status)
            chain_detail = getattr(status, "detail", "")
        except Exception as exc:
            chain_valid = False
            chain_detail = str(exc)

        artifacts = []
        for f in sorted(cap_dir.iterdir()):
            if f.is_file() and not f.name.endswith(".lock"):
                artifacts.append({
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                })

        return {
            "capsule_id": capsule_id,
            "manifest": manifest,
            "audit_chain_intact": chain_valid,
            "artifacts": artifacts,
            "state": manifest.get("state"),
            "domain": manifest.get("domain"),
            "source_ticket_id": meta.get("source_ticket_id"),
            "artifact_sha256": meta.get("artifact_sha256"),
        }

    def clean_sandbox(self, keep_recent: int = 0) -> int:
        """Remove trial directories from capsules_sandbox/, preserving .gitkeep."""
        if not self.sandbox_root.is_dir():
            return 0

        dirs = [
            d for d in sorted(self.sandbox_root.iterdir(), key=lambda p: p.stat().st_mtime)
            if d.is_dir()
        ]

        to_delete = dirs[: max(0, len(dirs) - keep_recent)] if keep_recent > 0 else dirs
        count = 0
        for d in to_delete:
            shutil.rmtree(d, ignore_errors=True)
            count += 1
        return count

    def _read_state(self, cap_dir: Path) -> str | None:
        mf_path = cap_dir / "manifest.json"
        if not mf_path.is_file():
            return None
        try:
            return json.loads(mf_path.read_text(encoding="utf-8")).get("state")
        except Exception:
            return None
