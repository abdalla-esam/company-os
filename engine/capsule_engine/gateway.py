#!/usr/bin/env python3
"""gateway.py — the single model seam: real HTTP client + offline engine.

The Model Gateway is the one place where language-model calls enter the
system. This module ships two interchangeable implementations behind the
same three methods:

- :class:`ModelGateway` — the production client: stdlib ``urllib.request``
  over any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, Groq,
  a local gateway). Credentials come from the constructor or the environment
  (``AGENCY_LLM_API_KEY`` / ``OPENAI_API_KEY``, ``AGENCY_LLM_BASE_URL`` /
  ``OPENAI_BASE_URL``). ``complete`` walks ``routing_context
  .compute_requirements`` (``tier`` -> ``fallback_tier``) so a failed
  primary model downgrades instead of killing the capsule. Transient HTTP
  errors (429, 5xx) and network timeouts automatically retry with
  exponential backoff and jitter.
- :class:`OfflineModelGateway` — the deterministic, rule-driven engine used
  by dev runs and the test suite: no network, no randomness, same verdicts
  on every call.

:func:`default_gateway` is a strict, fail-closed factory: it returns the
real :class:`ModelGateway` when credentials are configured, or raises
:class:`GatewayConfigError` unless explicitly opted into offline mode via
``offline=True`` or ``AGENCY_OFFLINE_MODE=1``.

Every network failure is wrapped in a :class:`GatewayError` subclass
(config / transport / timeout / HTTP / malformed response) so workers can
record it in ``history`` and fail closed instead of leaking raw ``urllib``
exceptions.

Every published call also exposes the provider's ``usage`` dictionary through
:attr:`ModelGateway.last_usage` (``prompt_tokens`` / ``completion_tokens`` /
``total_tokens``, missing or malformed keys defaulting to 0). ``complete``
sums the usage of every attempt it made (primary + fallback), so a worker can
fold the numbers into ``metadata.usage`` via ``BaseWorker.account_usage``.

Operations (identical signatures on both implementations):
- ``complete(manifest, prompt)`` — text generation (the copy stage).

The marketing-specific operations (image-subject distillation with the flevo
aesthetic, the visual-director fallback and the editorial review heuristics)
live in ``data.domains.marketing.gateway_ops`` and call this gateway through
its documented ``_call_llm`` seam.

Standard library only: ``json``, ``logging``, ``os``, ``random``, ``re``,
``socket``, ``time``, ``urllib.error``, ``urllib.request``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, MutableMapping

from capsule_engine.exceptions import CapsuleError
from capsule_engine.manifest import USAGE_KEYS

logger = logging.getLogger("capsule_engine.gateway")

# --------------------------------------------------------------------------- #
# real-client configuration
# --------------------------------------------------------------------------- #

# Constructor arguments win; the environment is the fallback.
GATEWAY_API_KEY_ENVS = ("AGENCY_LLM_API_KEY", "OPENAI_API_KEY")
GATEWAY_BASE_URL_ENVS = ("AGENCY_LLM_BASE_URL", "OPENAI_BASE_URL")
GATEWAY_MODEL_ENVS = ("AGENCY_LLM_MODEL", "OPENAI_MODEL")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0  # seconds — hard deadline per HTTP request
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 2.0  # seconds
OFFLINE_MODE_ENV = "AGENCY_OFFLINE_MODE"
MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # bounded read: 1 MiB of generated text
MAX_ERROR_BODY_BYTES = 4096  # error bodies are diagnostics, not payloads

# Logical tier -> concrete model id. A tier string that is not a key here is
# used verbatim, so manifests may name models directly.
DEFAULT_TIER_MODELS: dict[str, str] = {
    "tier_1_logic": "gpt-4o-mini",
    "tier_1": "gpt-4o-mini",
    "tier_2_creative": "gpt-4o",
    "tier_3_bulk": "gpt-4o-mini",
}

COPY_TEMPERATURE = 0.4
IMAGE_TEMPERATURE = 0.2
REVIEW_TEMPERATURE = 0.0  # verdicts must be as deterministic as possible
TOOL_TEMPERATURE = 0.2  # tool selection favours consistency over flourish

#: A single turn may request at most this many tools. Bounded so a model cannot
#: turn one answer into an unbounded fan-out of paid gear calls.
MAX_TOOL_CALLS_PER_TURN = 8
#: Argument payload cap per call, mirroring the bridge's own input budget.
MAX_TOOL_ARGUMENT_CHARS = 400_000


def _normalise_tool_calls(raw: object) -> list[dict[str, Any]]:
    """Validate an assistant ``tool_calls`` array; refuse anything malformed.

    Returns a list of ``{"id", "name", "arguments"}`` where ``arguments`` is the
    raw JSON **string** exactly as the model produced it (parsed later, by the
    loop, so a parse error is attributable to one call instead of the turn).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GatewayResponseError(
            f"tool_calls must be a list, got {type(raw).__name__}"
        )
    if len(raw) > MAX_TOOL_CALLS_PER_TURN:
        raise GatewayResponseError(
            f"tool_calls holds {len(raw)} calls, above the limit of {MAX_TOOL_CALLS_PER_TURN}"
        )
    clean: list[dict[str, Any]] = []
    for index, call in enumerate(raw):
        if not isinstance(call, dict):
            raise GatewayResponseError(
                f"tool_calls[{index}] must be an object, got {type(call).__name__}"
            )
        function = call.get("function")
        if not isinstance(function, dict):
            raise GatewayResponseError(f"tool_calls[{index}].function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GatewayResponseError(f"tool_calls[{index}].function.name must be a non-empty string")
        arguments = function.get("arguments")
        if arguments is None:
            arguments = "{}"
        if not isinstance(arguments, str):
            # Some providers return an object; accept it, canonically encoded.
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            else:
                raise GatewayResponseError(
                    f"tool_calls[{index}].function.arguments must be a JSON string"
                )
        if len(arguments) > MAX_TOOL_ARGUMENT_CHARS:
            raise GatewayResponseError(
                f"tool_calls[{index}].function.arguments exceed {MAX_TOOL_ARGUMENT_CHARS} characters"
            )
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{index}"
        clean.append({"id": call_id, "name": name.strip(), "arguments": arguments})
    return clean


class AssistantTurn:
    """One model answer: text, requested tools, and what it cost.

    ``content`` is ``""`` when the model only asked for tools. ``tool_calls``
    are normalised by :func:`_normalise_tool_calls`. ``usage`` is the turn's
    token counters (never ``None`` after a successful call).
    """

    __slots__ = ("content", "tool_calls", "usage")

    def __init__(
        self,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the transcript."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in self.tool_calls
            ]
        return message

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"AssistantTurn(content={self.content[:40]!r}, "
            f"tool_calls={len(self.tool_calls)}, usage={self.usage})"
        )


# --------------------------------------------------------------------------- #
# error hierarchy (all catchable as CapsuleError at the pulse boundary)
# --------------------------------------------------------------------------- #

class GatewayError(CapsuleError):
    """Base class for every deliberate gateway failure."""


class GatewayConfigError(GatewayError):
    """No API key is configured (constructor argument or environment)."""


class GatewayTransportError(GatewayError):
    """The endpoint could not be reached (DNS, connection, TLS, proxy)."""


class GatewayTimeoutError(GatewayError):
    """The request exceeded its hard deadline."""


class GatewayHTTPError(GatewayError):
    """The endpoint answered with a non-2xx status."""

    def __init__(self, status: int, detail: str, *, model: str = "") -> None:
        self.status = status
        self.detail = detail
        self.model = model
        suffix = f" from {model}" if model else ""
        super().__init__(f"HTTP {status}{suffix}: {detail[:200]}")


class GatewayResponseError(GatewayError):
    """The endpoint answered 2xx but the payload violates the contract."""


class SpendCapExceeded(GatewayError):
    """The spend gate refused the call BEFORE it left the machine.

    Raised by :mod:`capsule_engine.spend_control` (the single pre-call choke
    point): a daily/monthly/per-domain/call cap would be crossed, the spend
    kill switch is on, spend is disabled, or the ledger/config is unusable
    (fail closed). Not a transport failure — the request was never sent, and
    no fallback tier is attempted on it either.
    """


# --------------------------------------------------------------------------- #
# token accounting helpers (defensive: missing/malformed counters collapse to 0)
# --------------------------------------------------------------------------- #

def _token_count(value: object) -> int:
    """Coerce a provider token counter: non-negative int, else 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _add_usage(sink: MutableMapping[str, int], usage: object) -> None:
    """Add a usage mapping into ``sink`` (no-op for anything unusable)."""
    if not isinstance(usage, dict):
        return
    for key in USAGE_KEYS:
        sink[key] = sink.get(key, 0) + _token_count(usage.get(key))


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

COPY_SYSTEM_PROMPT = (
    "You are the executive lead copywriter of an elite corporate tech agency. "
    "Follow the brief exactly and craft an authoritative, high-impact post tailored for social platforms (LinkedIn/Facebook). "
    "Language handling: If the brief/topic is provided in Arabic, write in fluent, eloquent executive Arabic. "
    "If provided in English or any other language, write in that language with matching prestige and authority. "
    "Structure strictly: "
    "1) A powerful headline hook (bold, concise, immediately stopping the scroll). "
    "2) 2-3 engaging story paragraphs or bullet points explaining the core value with technical depth, zero fluff. "
    "Weave in concrete technical/system terms naturally (e.g., architecture, system, pipeline, data, engine, workflow). "
    "3) A clear call-to-action (Demo / consultation). "
    "Strictly avoid banned buzzwords, fillers, and generic cliches. "
    "Keep the tone eloquent, executive, and punchy. "
    "Output clean Markdown only, no meta-commentary, no code fences."
)

def _first_env(names: tuple[str, ...]) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


# --------------------------------------------------------------------------- #
# the real client
# --------------------------------------------------------------------------- #

class ModelGateway:
    """Production gateway: OpenAI-compatible chat completions over HTTP.

    Args:
        api_key: bearer token; falls back to ``AGENCY_LLM_API_KEY`` then
            ``OPENAI_API_KEY``.
        base_url: API root, e.g. ``https://api.openai.com/v1`` or
            ``https://api.groq.com/openai/v1``; falls back to
            ``AGENCY_LLM_BASE_URL`` / ``OPENAI_BASE_URL`` /
            :data:`DEFAULT_BASE_URL`.
        model: default model id used when the manifest names no tier.
        tier_models: overrides merged over :data:`DEFAULT_TIER_MODELS`.
        timeout: per-request deadline in seconds.
        max_retries: maximum automatic retry attempts on 429, 5xx, or timeout.
        base_backoff: base multiplier in seconds for exponential backoff.

    Raises:
        GatewayConfigError: no API key is configured.

    ``_call_llm(messages, model, temperature)`` is the single network choke
    point — the seam tests and callers subclass to stay offline.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        model: str | None = None,
        tier_models: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
    ) -> None:
        key = (api_key or _first_env(GATEWAY_API_KEY_ENVS) or "").strip()
        if not key:
            raise GatewayConfigError(
                "no API key: pass api_key= or set "
                + " / ".join(GATEWAY_API_KEY_ENVS)
            )
        resolved_base = (
            base_url or _first_env(GATEWAY_BASE_URL_ENVS) or DEFAULT_BASE_URL
        ).strip()
        if not resolved_base:
            raise GatewayConfigError("base_url must be a non-empty URL")
        self._api_key = key
        self._base_url = resolved_base.rstrip("/")
        configured_model = (model or _first_env(GATEWAY_MODEL_ENVS) or DEFAULT_MODEL).strip()
        self._model = configured_model
        self._tier_models = dict(DEFAULT_TIER_MODELS)
        env_model = _first_env(GATEWAY_MODEL_ENVS)
        if env_model:
            for k in self._tier_models:
                self._tier_models[k] = env_model.strip()
        if tier_models:
            self._tier_models.update(tier_models)
        self._timeout = max(1.0, float(timeout))
        self._max_retries = max(0, int(max_retries))
        self._base_backoff = max(0.0, float(base_backoff))
        self._last_usage: dict[str, int] | None = None
        self._last_tool_calls: list[dict[str, Any]] = []
        # Spend attribution (spend_control): the branch that pays, and the
        # provider label recorded on every ledger event.
        self._spend_domain: str | None = None
        try:
            self._spend_provider = (
                urllib.parse.urlsplit(self._base_url).netloc or self._base_url
            )
        except Exception:  # defensive: never block a call on provider labelling
            self._spend_provider = self._base_url

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def base_backoff(self) -> float:
        return self._base_backoff

    def spend_domain(self, domain: object) -> None:
        """Attribute this gateway's next calls to a branch (spend_control).

        ``BaseWorker.run`` calls this duck-typed hook with the capsule's
        declared domain so per-domain caps see the truth; anything unusable
        falls back to the spend config's ``default_domain`` inside
        ``spend_control.authorize``.
        """
        clean = domain.strip() if isinstance(domain, str) else ""
        self._spend_domain = clean or None

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Usage of the most recent call, or ``None`` when none was observed.

        ``complete`` reports the summed usage of all attempts it made
        (primary + fallback); the marketing operations in
        ``data.domains.marketing.gateway_ops`` report their single call. A
        missing ``usage`` block still reports zeros — the provider answered,
        we just cannot bill it.
        """
        usage = getattr(self, "_last_usage", None)
        return dict(usage) if isinstance(usage, dict) else None

    @property
    def last_tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls requested by the most recent :meth:`complete_turn`."""
        return list(getattr(self, "_last_tool_calls", []) or [])

    # ------------------------------------------------------------------ #
    # the tool-aware turn
    # ------------------------------------------------------------------ #

    def complete_turn(
        self,
        manifest: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = TOOL_TEMPERATURE,
    ) -> AssistantTurn:
        """One model turn that may request tools, with tier fallback.

        Unlike :meth:`complete`, an answer carrying only tool calls is a
        success: ``content`` is ``""`` and ``tool_calls`` is populated. The
        caller (:class:`capsule_engine.tool_loop.ToolLoop`) executes them and
        calls back.

        Spend attribution and tier resolution are identical to :meth:`complete`
        — this is the same seam, one answer richer.
        """
        tier, fallback_tier = self._requirements(manifest)
        primary_model = self._model_for(tier)
        total: dict[str, int] = {key: 0 for key in USAGE_KEYS}
        self._last_usage = None
        declared_domain = manifest.get("domain") if isinstance(manifest, dict) else None
        self.spend_domain(declared_domain)
        try:
            text = self._call_llm(
                messages, primary_model, temperature, tools=tools
            )
            _add_usage(total, self._last_usage)
            self._last_usage = total
            return AssistantTurn(text, self.last_tool_calls, dict(total))
        except SpendCapExceeded:
            raise
        except GatewayError as primary_error:
            _add_usage(total, self._last_usage)
            fallback_model = self._model_for(fallback_tier)
            if fallback_model == primary_model:
                self._last_usage = total
                raise
            logger.warning(
                "gateway: primary model %s failed (%s) — trying fallback %s",
                primary_model,
                primary_error,
                fallback_model,
            )
            try:
                text = self._call_llm(
                    messages, fallback_model, temperature, tools=tools
                )
                _add_usage(total, self._last_usage)
                self._last_usage = total
                return AssistantTurn(text, self.last_tool_calls, dict(total))
            except SpendCapExceeded:
                raise
            except GatewayError as fallback_error:
                _add_usage(total, self._last_usage)
                self._last_usage = total
                raise GatewayError(
                    "primary and fallback model tiers both failed: "
                    f"{primary_model} ({type(primary_error).__name__}), "
                    f"{fallback_model} ({type(fallback_error).__name__})"
                ) from fallback_error

    # ------------------------------------------------------------------ #
    # the text operation
    # ------------------------------------------------------------------ #

    def complete(self, manifest: dict[str, Any], prompt: str) -> str:
        """Generate Markdown copy, downgrading to ``fallback_tier`` on failure.

        Model selection reads ``routing_context.compute_requirements``:
        ``tier`` and ``fallback_tier`` are logical names from
        :data:`DEFAULT_TIER_MODELS` or literal model ids. A primary failure
        (timeout / HTTP / transport / malformed response) is logged and the
        fallback is attempted; if both fail, a :class:`GatewayError` with the
        full chain is raised so the worker records it in ``history``.
        """
        tier, fallback_tier = self._requirements(manifest)
        messages = [
            {"role": "system", "content": COPY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        primary_model = self._model_for(tier)
        total: dict[str, int] = {key: 0 for key in USAGE_KEYS}
        self._last_usage = None
        # Spend attribution: the capsule's own domain pays for this call.
        declared_domain = manifest.get("domain") if isinstance(manifest, dict) else None
        self.spend_domain(declared_domain)
        try:
            text = self._call_llm(messages, primary_model, COPY_TEMPERATURE)
            _add_usage(total, self._last_usage)
            self._last_usage = total
            return text
        except SpendCapExceeded:
            raise  # the gate refused before the request — no fallback roll
        except GatewayError as primary_error:
            _add_usage(total, self._last_usage)
            fallback_model = self._model_for(fallback_tier)
            if fallback_model == primary_model:
                self._last_usage = total
                raise
            logger.warning(
                "gateway: primary model %s failed (%s) — trying fallback %s",
                primary_model,
                primary_error,
                fallback_model,
            )
            try:
                text = self._call_llm(messages, fallback_model, COPY_TEMPERATURE)
                _add_usage(total, self._last_usage)
                self._last_usage = total
                return text
            except SpendCapExceeded:
                raise  # the gate refused — trying the fallback would also spend
            except GatewayError as fallback_error:
                _add_usage(total, self._last_usage)
                self._last_usage = total  # billed attempts stay accountable
                raise GatewayError(
                    "primary and fallback model tiers both failed: "
                    f"{primary_model} ({type(primary_error).__name__}), "
                    f"{fallback_model} ({type(fallback_error).__name__})"
                ) from fallback_error

    # ------------------------------------------------------------------ #
    # network + parsing internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calculate_backoff(attempt: int, base_backoff: float) -> float:
        """Calculate exponential backoff with jitter: base * 2^attempt + jitter.

        Jitter is a random float between 0.01s and 0.5s (10-500ms).
        When base_backoff is 0.0, returns 0.0 (enables instantaneous testing).
        """
        if base_backoff <= 0.0:
            return 0.0
        exponential = base_backoff * (2 ** attempt)
        jitter = random.uniform(0.01, 0.5)
        return exponential + jitter

    def _sleep(self, seconds: float) -> None:
        """Hook for time.sleep, overridable in tests."""
        time.sleep(seconds)

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        base_backoff: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """POST one chat completion and return the assistant text.

        Single choke point for every network interaction: all transport
        failures are wrapped into :class:`GatewayError` subclasses.
        Transient errors (HTTP 429, HTTP 5xx, socket/network timeouts) are
        automatically retried using exponential backoff with jitter.

        ``tools`` publishes JSON-schema tool definitions to the model. When a
        turn answers with tool calls and no text, the text is empty (``""``)
        rather than an error — :attr:`last_tool_calls` carries the calls.

        Side effect: :attr:`last_usage` is reset to ``None`` and, when a 2xx
        body was decoded, set to the response's ``usage`` counters (zeros for
        missing/malformed keys). :attr:`last_tool_calls` is reset on every
        attempt and holds the assistant's calls from the final decoded body.
        Decoding happens before the content check so even a
        rejected-but-billed response stays accountable.
        """
        self._last_usage = None
        self._last_tool_calls = []
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        encoded_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "sovereign-agency-os/1.0 (+capsule-engine)",
        }
        deadline = timeout if timeout is not None else self._timeout
        retries_limit = (
            self._max_retries if max_retries is None else max(0, int(max_retries))
        )
        backoff_base = (
            self._base_backoff
            if base_backoff is None
            else max(0.0, float(base_backoff))
        )

        last_http_detail = ""

        # The money gate: one authorization per HTTP attempt (retries included),
        # settled after the attempt. Lazy import: spend_control imports this
        # module for its error base, so the dependency stays one-directional at
        # import time.
        from capsule_engine import spend_control

        for attempt in range(retries_limit + 1):
            reservation = spend_control.authorize(
                model=model,
                category=spend_control.CATEGORY_MODEL,
                provider=self._spend_provider,
                domain=self._spend_domain,
                context={"op": "gateway._call_llm", "attempt": attempt},
            )
            request = urllib.request.Request(
                url,
                data=encoded_data,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=deadline) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                break  # Successful network response! (settled after decoding)
            except (socket.timeout, TimeoutError) as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                if attempt < retries_limit:
                    delay = self._calculate_backoff(attempt, backoff_base)
                    logger.warning(
                        "gateway: %s socket timeout on attempt %d/%d — retrying in %.2fs",
                        model,
                        attempt + 1,
                        retries_limit + 1,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise GatewayTimeoutError(
                    f"{model} timed out after {deadline}s"
                ) from exc
            except urllib.error.HTTPError as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                detail = ""
                try:
                    if (
                        hasattr(exc, "fp")
                        and exc.fp is not None
                        and hasattr(exc.fp, "seek")
                    ):
                        try:
                            exc.fp.seek(0)
                        except Exception:
                            pass
                    detail = exc.read(MAX_ERROR_BODY_BYTES).decode(
                        "utf-8", "replace"
                    )
                except Exception:
                    detail = str(exc.reason)
                detail = detail.strip()
                if detail:
                    last_http_detail = detail
                else:
                    detail = last_http_detail

                is_retryable = exc.code == 429 or (500 <= exc.code < 600)
                if is_retryable and attempt < retries_limit:
                    delay = self._calculate_backoff(attempt, backoff_base)
                    logger.warning(
                        "gateway: %s HTTP %d on attempt %d/%d (%s) — retrying in %.2fs",
                        model,
                        exc.code,
                        attempt + 1,
                        retries_limit + 1,
                        detail[:100],
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise GatewayHTTPError(exc.code, detail, model=model) from exc
            except urllib.error.URLError as exc:
                spend_control.record(reservation, status=spend_control.STATUS_FAILED)
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    if attempt < retries_limit:
                        delay = self._calculate_backoff(attempt, backoff_base)
                        logger.warning(
                            "gateway: %s transport timeout (%s) on attempt %d/%d — retrying in %.2fs",
                            model,
                            exc.reason,
                            attempt + 1,
                            retries_limit + 1,
                            delay,
                        )
                        self._sleep(delay)
                        continue
                    raise GatewayTimeoutError(
                        f"{model} timed out after {deadline}s: {exc.reason}"
                    ) from exc
                raise GatewayTransportError(
                    f"{model} transport failure: {exc.reason}"
                ) from exc

        # Decode and settle the reservation. A rejected-but-billed 2xx still
        # counts (decode first, judge content after), a malformed body settles
        # as failed at its estimate, and only a usable answer settles with the
        # provider's real token counters.
        try:
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GatewayResponseError(
                    f"{model} response exceeded {MAX_RESPONSE_BYTES} bytes"
                )
            decoded = json.loads(raw.decode("utf-8"))
        except GatewayError:
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise GatewayResponseError(
                f"{model} returned a non-JSON body: {exc}"
            ) from exc
        self._last_usage = self._extract_usage(decoded)
        try:
            message = decoded["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise GatewayResponseError(
                f"{model} response has no choices[0].message"
            ) from exc
        if not isinstance(message, dict):
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise GatewayResponseError(
                f"{model} response choices[0].message is {type(message).__name__}, expected object"
            )
        content = message.get("content")
        calls = _normalise_tool_calls(message.get("tool_calls"))
        if content is None:
            content = ""
        if not isinstance(content, str):
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise GatewayResponseError(
                f"{model} response content is {type(content).__name__}, expected string"
            )
        if not content.strip() and not calls:
            spend_control.record(reservation, status=spend_control.STATUS_FAILED)
            raise GatewayResponseError(f"{model} returned empty content")
        spend_control.record(
            reservation, status=spend_control.STATUS_SUCCEEDED, tokens=self._last_usage
        )
        self._last_tool_calls = calls
        return content.strip()

    @staticmethod
    def _extract_usage(decoded: object) -> dict[str, int]:
        """Extract the response's ``usage`` counters, defensively.

        Missing block, missing keys, wrong types, negative or boolean values
        all collapse to 0. ``total_tokens`` falls back to the sum of the two
        halves when the provider omits it.
        """
        usage = decoded.get("usage") if isinstance(decoded, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        counts = {key: _token_count(usage.get(key)) for key in USAGE_KEYS}
        if counts["total_tokens"] == 0:
            counts["total_tokens"] = (
                counts["prompt_tokens"] + counts["completion_tokens"]
            )
        return counts

    @staticmethod
    def _requirements(manifest: dict[str, Any]) -> tuple[object, object]:
        """Return ``(tier, fallback_tier)`` from compute_requirements, safely."""
        routing = manifest.get("routing_context") if isinstance(manifest, dict) else None
        requirements = (
            routing.get("compute_requirements") if isinstance(routing, dict) else None
        )
        if not isinstance(requirements, dict):
            return None, None
        return requirements.get("tier"), requirements.get("fallback_tier")

    def _model_for(self, tier: object) -> str:
        """Map a logical tier to a model id (literal names pass through)."""
        if isinstance(tier, str) and tier.strip():
            name = tier.strip()
            return self._tier_models.get(name, name)
        return self._model


# --------------------------------------------------------------------------- #
# the deterministic offline engine (dev runs + the whole test suite)
# --------------------------------------------------------------------------- #


class OfflineModelGateway:
    """Deterministic offline engine: same seam, zero network, zero randomness.

    Pure functions of their inputs (same input -> same output). Used for dev
    runs, worker smoke scripts and the test suite when opted into offline mode.

    ``last_usage`` reports an all-zero record: the calls are real seams but
    cost nothing, so dev runs still populate ``metadata.usage`` with zeros
    rather than lying with ``None``.
    """

    last_usage: dict[str, int] | None = None
    last_tool_calls: list[dict[str, Any]] = []

    def _zero_usage(self) -> None:
        """Mark this virtual call as a zero-token, zero-cost observation."""
        self.last_usage = {key: 0 for key in USAGE_KEYS}

    def complete_turn(
        self,
        manifest: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = TOOL_TEMPERATURE,
    ) -> "AssistantTurn":
        """Answer without tools, deterministically.

        The offline engine makes no tool decisions: choosing a gear is a model
        judgement, and inventing one here would fake a capability the offline
        mode does not have. A tool loop driven offline therefore ends on its
        first turn — and a test that needs a tool cycle injects a scripted
        gateway instead of pretending the offline one can reason.
        """
        self._zero_usage()
        self.last_tool_calls = []
        prompt = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                prompt = str(message.get("content") or "")
                break
        text = self.complete(manifest, prompt)
        return AssistantTurn(text, [], dict(self.last_usage or {}))

    def complete(self, manifest: dict[str, Any], prompt: str) -> str:
        """Return deterministic dummy Markdown derived from the manifest."""
        self._zero_usage()
        metadata = manifest.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        topic = str(meta.get("topic") or "campaign")
        platform = str(meta.get("target_platform") or "unspecified platform")
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return (
            f"# {topic}\n\n"
            f"> {first_line}\n\n"
            f"**{topic} — made for {platform}.**\n\n"
            "Dummy copy produced by the offline ModelGateway; configure "
            "`AGENCY_LLM_API_KEY` to route through the real HTTP client.\n\n"
            "## Headline options\n\n"
            f"1. {topic}: the story your {platform} audience will share.\n"
            f"2. Meet {topic}.\n"
            f"3. Why {platform} audiences can't ignore {topic}.\n"
        )

def default_gateway(
    *,
    offline: bool | None = None,
    **overrides: Any,
) -> ModelGateway | OfflineModelGateway:
    """Strict factory: real ModelGateway when credentials are configured.

    Raises :class:`GatewayConfigError` if credentials are missing unless
    explicitly opted into offline mode (via ``offline=True`` or the
    ``AGENCY_OFFLINE_MODE=1`` environment variable).
    """
    explicit_offline = offline
    if explicit_offline is None and "offline" in overrides:
        explicit_offline = bool(overrides.pop("offline"))

    if explicit_offline is True:
        return OfflineModelGateway()

    env_offline = os.environ.get(OFFLINE_MODE_ENV, "").strip().lower()
    if explicit_offline is None and env_offline in ("1", "true", "yes", "on", "offline"):
        return OfflineModelGateway()

    has_credentials = bool(
        overrides.get("api_key") or _first_env(GATEWAY_API_KEY_ENVS)
    )
    if has_credentials:
        return ModelGateway(**overrides)

    raise GatewayConfigError(
        "no API key configured and offline mode not enabled: pass api_key=, "
        "set " + " / ".join(GATEWAY_API_KEY_ENVS) + ", pass offline=True, "
        "or set " + OFFLINE_MODE_ENV + "=1"
    )

