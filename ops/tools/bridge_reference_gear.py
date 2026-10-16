#!/usr/bin/env python3
"""bridge_reference_gear.py — the *other* side of the Tool Bridge, in 200 lines.

This is a gear: a tiny HTTP service that speaks the contract in
``shared_kernel/schemas/tool_bridge.request.schema.json`` /
``...response.schema.json`` and answers gears deterministically. Its job is to
prove the seam is real, cheap, and tool-agnostic:

- it runs with the standard library only (no framework, no container, no
  database) and honours the same envelopes a Dify workflow, an n8n webhook, or
  a LangGraph server would produce — which is exactly why any of them can
  replace it without touching the kernel;
- it *verifies* ``input_sha256`` before answering, refuses politely with a
  machine-readable code, and names itself in ``engine.tool``;
- it never sees a credential in the body: the bearer token arrives in the
  ``Authorization`` header and is compared in constant time.

Run it (token is read from the environment, never argv)::

    export AGENCY_BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    export AGENCY_BRIDGE_URL=http://127.0.0.1:8799/bridge
    python3 tools/bridge_reference_gear.py --port 8799

Then ask it for something::

    python3 - <<'PY'
    from capsule_engine.bridge import HttpBridge
    answer = HttpBridge(gear="text:review").invoke(
        {"text": "our engine routes every capsule through a governed pipeline today"},
        capsule={"id": "demo-1", "state": "pending_qa", "domain": "marketing"},
        governance={"domain": "marketing", "autonomy_level": "L0"},
    ).require_ok()
    print(answer.status, answer.tool, answer.output)
    PY

Deterministic behaviours worth knowing (they are the negative paths the test
suite exercises): a payload containing ``REFUSE_ME`` is answered with
``status=refused`` / ``code=policy``; a wrong ``input_sha256`` is answered with
``code=integrity``; an unknown gear is answered with ``code=unknown_gear``; an
unknown ``bridge_version`` with ``code=unsupported_version``.

A ``social:publish`` payload may carry the approved image in flight as
``media`` (standard base64 + the SHA-256 of those exact bytes). This gear
re-hashes it and answers ``code=integrity`` when the digest does not describe
the bytes — which is what makes "the approved image" and "the delivered image"
the same object rather than the same hope.

Exit codes: 0 = served until interrupted, 2 = refused to start (bad usage,
missing token, or an endpoint that cannot bind).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
for _p in (str(ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capsule_engine.bridge import (  # noqa: E402
    BRIDGE_VERSION,
    GEAR_VOCABULARY,
    MAX_MEDIA_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    canonical_bytes,
    payload_sha256,
)
# The aesthetic belongs to the marketing domain, not the core gateway.
from data.domains.marketing.gateway_ops import IMAGE_AESTHETIC  # noqa: E402

TOOL_NAME = "reference-gear"
TOOL_VERSION = "1.0"
REFUSAL_MARKER = "REFUSE_ME"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# The token's env var name is configurable so several gears can coexist.
TOKEN_ENV_DEFAULT = "AGENCY_BRIDGE_TOKEN"


def _fingerprint(*parts: Any) -> str:
    """A stable short id derived from the payload (never a random guess)."""
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]


def _answer(gear: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic, honest answers: absence is reported as absence."""
    if gear == "text:draft":
        topic = str(input_data.get("topic") or input_data.get("brief") or "campaign").strip()
        platform = str(input_data.get("platform") or "the timeline")
        language = str(input_data.get("language") or "the brief's language")
        return {
            "text": (
                f"# {topic}\n\n"
                f"{topic} lands on {platform} in {language}: a system that routes every capsule "
                "through a governed pipeline, binds the artifact hash before human review, and "
                "keeps an auditable chain of every decision.\n\n"
                "Book a demo.\n"
            )
        }
    if gear == "text:review":
        text = str(input_data.get("text") or "")
        words = [token for token in re.findall(r"[\w\u0600-\u06FF]+", text) if token]
        problems: list[str] = []
        if len(words) < 10:
            problems.append(f"too short ({len(words)} words, need 10)")
        if "synergy" in {word.lower() for word in words}:
            problems.append("banned buzzword present: synergy")
        if problems:
            return {"passed": False, "feedback": "REJECTED: " + "; ".join(problems) + " — revise and resubmit."}
        return {"passed": True, "feedback": f"PASSED: {len(words)} words, no banned buzzwords."}
    if gear == "image:distill":
        words = str(input_data.get("text") or "").split()[:12]
        subject = " ".join(words) or "executive command center"
        return {"prompt": f"{subject}, {IMAGE_AESTHETIC}"}
    if gear == "image:read":
        # Reached only for text-like payloads: the refusals above keep this gear
        # honest about not being a vision model.
        raw = base64.b64decode("".join(str(input_data["media"]["base64"]).split()), validate=True)
        text = raw.decode("utf-8", "replace").strip()
        return {
            "text": text or "(reference read: the page decoded to whitespace)",
            "confidence": 1.0 if text else 0.0,
            "blocks": [{"kind": "paragraph", "text": line.strip()} for line in text.splitlines() if line.strip()][:100],
        }
    if gear == "data:extract":
        text = str(input_data.get("text") or "")
        found: dict[str, Any] = {}
        for name in input_data.get("fields", []):
            match = re.search(rf"{re.escape(str(name))}\s*=\s*([^\s;,\n]+)", text, flags=re.IGNORECASE)
            found[str(name)] = match.group(1) if match else None
        return {"fields": found}
    if gear == "data:classify":
        text = str(input_data.get("text") or "").lower()
        labels = [str(label) for label in input_data.get("labels", [])]
        for label in labels:
            if label.lower() in text:
                return {"label": label, "confidence": 0.9}
        return {"label": labels[0], "confidence": 0.1}
    if gear in ("social:publish", "message:send"):
        key = "receipt_id" if gear == "social:publish" else "message_id"
        target = input_data.get("to") if gear == "message:send" else input_data.get("channel")
        media = input_data.get("media") if isinstance(input_data.get("media"), Mapping) else {}
        # The media digest is part of the receipt identity: a different image is
        # a different delivery, so a retry cannot be mistaken for a duplicate.
        return {
            key: f"ref-{_fingerprint(gear, target, input_data.get('text'), media.get('sha256'))}",
            "channel": str(input_data.get("channel")),
        }
    if gear == "message:interpret":
        text = str(input_data.get("text") or "")
        lowered = text.lower()
        if any(word in lowered for word in ("price", "pricing", "quote", "سعر", "تكلفة")):
            intent = "pricing_inquiry"
        elif "?" in text or any(word in lowered for word in ("how", "why", "متى", "كيف")):
            intent = "support_question"
        else:
            intent = "inbound_message"
        return {
            "intent": intent,
            "reply_text": "شكرًا لتواصلك — سيؤكد الفريق البشري التفاصيل خلال وقت قصير.",
            "escalate": any(word in lowered for word in ("refund", "urgent", "استرجاع", "عاجل")),
        }
    if gear == "lead:qualify":
        text = str(input_data.get("text") or "").lower()
        criteria = [str(item) for item in input_data.get("criteria", [])]
        met = [item for item in criteria if item.lower() in text]
        missing = [item for item in criteria if item not in met]
        score = round(len(met) / len(criteria), 2) if criteria else 0.0
        reasons = [f"criterion met: {item}" for item in met] + [f"criterion missing: {item}" for item in missing]
        return {"score": score, "qualified": score >= 0.6, "reasons": reasons[:16]}
    if gear == "code:propose":
        constraints = [str(item) for item in input_data.get("constraints", [])]
        return {
            "summary": (
                "Reference gear: a change set must be produced by a real coding tool. "
                f"Request recorded, {len(constraints)} constraint(s) noted, no files proposed."
            ),
            "files": [],
        }
    if gear == "finance:invoice":
        total = round(sum(float(item["amount"]) for item in input_data["line_items"]), 2)
        return {
            "invoice_id": f"ref-{_fingerprint(input_data['customer'], total, input_data['currency'])}",
            "total": total,
            "currency": str(input_data["currency"]),
        }
    if gear == "people:screen":
        text = str(input_data.get("text") or "").lower()
        requirements = [str(item) for item in input_data.get("requirements", [])]
        met = [item for item in requirements if item.lower() in text]
        gaps = [item for item in requirements if item not in met]
        score = round(len(met) / len(requirements), 2) if requirements else 0.0
        return {
            "score": score,
            "gaps": gaps[:16],
            "summary": f"Reference screening: {len(met)}/{len(requirements)} requirements evidenced in the application.",
        }
    if gear == "support:triage":
        lowered = str(input_data.get("text") or "").lower()
        if any(word in lowered for word in ("outage", "data loss", "down", "انقطاع")):
            severity = "critical"
        elif any(word in lowered for word in ("error", "cannot", "failed", "لا يعمل")):
            severity = "high"
        elif any(word in lowered for word in ("slow", "confusing", "بطيء")):
            severity = "medium"
        else:
            severity = "low"
        if any(word in lowered for word in ("invoice", "refund", "billing", "فاتورة")):
            category = "billing"
        elif any(word in lowered for word in ("error", "bug", "crash")):
            category = "bug"
        elif any(word in lowered for word in ("login", "password", "access")):
            category = "access"
        else:
            category = "general"
        return {"severity": severity, "category": category, "escalate": severity in ("high", "critical")}
    if gear == "docs:review":
        text = str(input_data.get("text") or "")
        lowered = text.lower()
        flags: list[dict[str, str]] = []
        for phrase, severity in (
            ("unlimited liability", "critical"),
            ("auto-renew", "warning"),
            ("indemnif", "warning"),
        ):
            if phrase in lowered:
                flags.append({"severity": severity, "note": f"document contains '{phrase}'"})
        for item in input_data.get("checklist", []):
            if str(item).lower() not in lowered:
                flags.append({"severity": "info", "note": f"checklist item not addressed: {item}"})
        return {
            "summary": f"Reference review: {len(flags)} flag(s) found against {len(input_data.get('checklist', []))} checklist item(s).",
            "flags": flags[:50],
        }
    # log:summarize
    entries = [str(line) for line in input_data.get("entries", [])]
    findings: list[dict[str, str]] = []
    for line in entries:
        lowered = line.lower()
        if "critical" in lowered:
            findings.append({"severity": "critical", "detail": line.strip()[:2000]})
        elif "warning" in lowered:
            findings.append({"severity": "warning", "detail": line.strip()[:2000]})
    return {
        "summary": f"{len(entries)} log lines scanned; {len(findings)} severity markers found.",
        "findings": findings[:50],
    }


def _verify_media(media: Mapping[str, Any]) -> str | None:
    """Return a refusal detail when an in-flight media payload is unusable.

    A gear must not assume the caller validated anything: this one re-hashes the
    decoded bytes against the declared digest, so "the image that was approved"
    and "the image that gets delivered" are provably the same bytes.
    """
    encoded = media.get("base64")
    digest = media.get("sha256")
    if not isinstance(encoded, str) or not isinstance(digest, str):
        return "media must carry base64 and sha256"
    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        return f"media base64 cannot be decoded: {exc}"
    if len(raw) > MAX_MEDIA_BYTES:
        return f"media decodes to {len(raw)} bytes (> {MAX_MEDIA_BYTES})"
    if hashlib.sha256(raw).hexdigest() != digest:
        return "media sha256 does not match the decoded bytes"
    return None


def _refuse(request_id: str, gear: str, code: str, detail: str = "") -> dict[str, Any]:
    """A lawful refusal: machine-readable code, empty output, no partial answer."""
    refusal: dict[str, Any] = {"code": code}
    if detail:
        refusal["detail"] = detail[:2000]
    return {
        "bridge_version": BRIDGE_VERSION,
        "gear": gear if gear in GEAR_VOCABULARY else "text:review",
        "request_id": request_id if _ID_RE.match(request_id or "") else "0000000000000000",
        "status": "refused",
        "output": {},
        "engine": {"tool": TOOL_NAME, "version": TOOL_VERSION},
        "refusal": refusal,
    }


def handle_envelope(envelope: object) -> tuple[int, dict[str, Any]]:
    """Validate one request envelope and produce a response envelope.

    Returns ``(http_status, response)``: contract failures are answered with
    ``status=refused`` and HTTP 200 (the gear understood the caller and declined
    lawfully), while a malformed JSON body is answered with HTTP 400 — the same
    distinction the kernel makes between a refusal and a protocol violation.
    """
    if not isinstance(envelope, Mapping):
        return 400, _refuse("", "text:review", "bad_request", "body must be a JSON object")

    request_id = str(envelope.get("request_id") or "")
    gear = str(envelope.get("gear") or "")

    if envelope.get("bridge_version") != BRIDGE_VERSION:
        return 200, _refuse(request_id, gear, "unsupported_version", f"expected bridge_version {BRIDGE_VERSION}")
    if gear not in GEAR_VOCABULARY:
        return 200, _refuse(request_id, gear, "unknown_gear", "this gear only serves the closed vocabulary")
    if not _ID_RE.match(request_id):
        return 200, _refuse(request_id, gear, "bad_request", "request_id must match [A-Za-z0-9_-]{8,64}")

    input_data = envelope.get("input")
    if not isinstance(input_data, Mapping):
        return 200, _refuse(request_id, gear, "bad_request", "input must be an object")

    claimed = envelope.get("input_sha256")
    if claimed != payload_sha256(dict(input_data)):
        return 200, _refuse(request_id, gear, "integrity", "input_sha256 does not match the payload")

    blob = json.dumps(input_data, ensure_ascii=False)
    if REFUSAL_MARKER in blob:
        return 200, _refuse(request_id, gear, "policy", "payload carries the policy refusal marker")

    media = input_data.get("media")
    if isinstance(media, Mapping):
        problem = _verify_media(media)
        if problem:
            return 200, _refuse(request_id, gear, "integrity", problem)

    if gear == "image:read":
        if isinstance(media, Mapping):
            mime = str(media.get("mime") or "")
            if not mime.startswith("text/"):
                return 200, _refuse(
                    request_id,
                    gear,
                    "unsupported_mime",
                    f"this gear cannot read {mime or 'unknown'} pixels — attach a vision/OCR tool",
                )
        else:
            return 200, _refuse(
                request_id,
                gear,
                "unsupported_source",
                "this gear does not fetch URLs — pass media bytes or attach a fetching tool",
            )

    output = _answer(gear, input_data)
    return 200, {
        "bridge_version": BRIDGE_VERSION,
        "gear": gear,
        "request_id": request_id,
        "status": "ok",
        "output": output,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "engine": {"tool": TOOL_NAME, "version": TOOL_VERSION},
        "output_sha256": payload_sha256(output),
    }


def make_handler(token: str) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``token`` (compared in constant time)."""

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = f"{TOOL_NAME}/{TOOL_VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib hook
            sys.stderr.write(f"[{TOOL_NAME}] {self.address_string()} {fmt % args}\n")

        def _send(self, status: int, payload: Mapping[str, Any]) -> None:
            body = canonical_bytes(dict(payload))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            started = time.monotonic()
            if self.path.rstrip("/") not in ("", "/bridge"):
                self._send(404, {"error": "not_found"})
                return
            presented = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(presented, expected):
                self._send(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send(400, {"error": "bad_length"})
                return
            raw = self.rfile.read(length)
            if len(raw) > MAX_RESPONSE_BYTES:  # pragma: no cover - guarded by bad_length above
                self._send(400, {"error": "too_large"})
                return
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send(400, {"error": "invalid_json", "detail": str(exc)})
                return
            status, response = handle_envelope(envelope)
            self._send(status, response)
            sys.stderr.write(
                f"[{TOOL_NAME}] gear={response.get('gear')} status={response.get('status')} "
                f"request_id={response.get('request_id')} latency_ms={int((time.monotonic() - started) * 1000)}\n"
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            self._send(
                200,
                {"gear": TOOL_NAME, "version": TOOL_VERSION, "vocabulary": sorted(GEAR_VOCABULARY)},
            )

    return BridgeHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reference implementation of the Tool Bridge gear side.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8799, help="bind port (default: 8799)")
    parser.add_argument("--token-env", default=TOKEN_ENV_DEFAULT, help=f"env var holding the bearer token (default: {TOKEN_ENV_DEFAULT})")
    args = parser.parse_args(argv)

    token = (os.environ.get(args.token_env) or "").strip()
    if not token:
        print(
            f"error: no bearer token in ${args.token_env} — refusing to serve an unauthenticated gear",
            file=sys.stderr,
        )
        return 2

    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(token))
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    print(
        f"{TOOL_NAME}/{TOOL_VERSION} listening on http://{args.host}:{server.server_port}/bridge "
        f"(gears: {', '.join(sorted(GEAR_VOCABULARY))})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{TOOL_NAME}] shutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
