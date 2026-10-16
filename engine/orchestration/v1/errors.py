#!/usr/bin/env python3
"""errors.py — the ``/v1`` error envelope and its two exception handlers.

The mobile contract answers one error shape everywhere::

    {"error": {"code": "STALE_CONTENT_HASH",
               "message": "Stale content: …",
               "details": {...},
               "remedy": "…"},
     "detail":  "Stale content: …"}

The machine-readable ``code`` is the point of the envelope — a client can act
on ``ATTESTATION_REQUIRED`` or ``HASH_SET_MISMATCH`` without parsing prose —
while a top-level ``detail`` is kept as well, so the namespace stays honest
about the refusal to any existing reader and the Slice 0 middleware tests
(which read ``.detail``) keep passing.

Only ``/v1/`` paths are enveloped. Every other path is handed back to
FastAPI's own handlers, byte for byte, so this module cannot change an
``/api/`` response by being installed.

Standard library apart from the framework; Python 3.10+.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

V1_PREFIX = "/v1/"

#: Status → default refusal code when a raise site supplies none.
_DEFAULT_CODES: Mapping[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    423: "LOCKED",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "UNAVAILABLE",
}


def default_code(status_code: int) -> str:
    """The fallback code for a status with no explicit one."""
    return _DEFAULT_CODES.get(status_code, f"HTTP_{status_code}")


class V1Error(StarletteHTTPException):
    """A ``/v1`` refusal carrying the machine-readable code and its context.

    A ``StarletteHTTPException`` subclass so the normal ``raise`` path and any
    existing ``except HTTPException`` still apply; the extra attributes are
    read only by :func:`v1_http_exception_handler`.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        remedy: Optional[str] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code or default_code(status_code)
        self.details = dict(details) if details else None
        self.remedy = remedy


def error_payload(
    *,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
    remedy: Optional[str] = None,
) -> dict[str, Any]:
    """The ``error`` object inside the envelope (undefined fields omitted)."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = dict(details)
    if remedy:
        error["remedy"] = remedy
    return error


def v1_error_content(
    *,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
    remedy: Optional[str] = None,
) -> dict[str, Any]:
    """The full ``/v1`` refusal body: the envelope plus a top-level ``detail``."""
    return {
        "error": error_payload(
            code=code, message=message, details=details, remedy=remedy
        ),
        "detail": message,
    }


def _is_v1(path: str) -> bool:
    return isinstance(path, str) and path.startswith(V1_PREFIX)


async def v1_http_exception_handler(request, exc: StarletteHTTPException):
    """Envelope an HTTP refusal on ``/v1/``; delegate everywhere else."""
    if not _is_v1(request.url.path):
        return await http_exception_handler(request, exc)
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = getattr(exc, "code", None) or default_code(exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content=v1_error_content(
            code=code,
            message=message,
            details=getattr(exc, "details", None),
            remedy=getattr(exc, "remedy", None),
        ),
        headers=getattr(exc, "headers", None),
    )


async def v1_validation_exception_handler(request, exc: RequestValidationError):
    """Envelope a body-validation failure on ``/v1/``; delegate everywhere else."""
    if not _is_v1(request.url.path):
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(
        status_code=422,
        content=v1_error_content(
            code="VALIDATION_ERROR",
            message="Request body failed validation",
            details={"errors": jsonable_encoder(exc.errors())},
        ),
    )


def install(app) -> None:
    """Mount the ``/v1`` router and register its error envelope.

    Called from the bottom of ``orchestration/api.py`` (after every ``/api``
    route and the auth middleware), so this is the last thing the app grows.
    """
    # Imported here, not at module scope: ``router`` reads helpers from
    # ``api`` lazily, and a top-level import would invite a cycle.
    from orchestration.v1.router import router

    app.add_exception_handler(StarletteHTTPException, v1_http_exception_handler)
    app.add_exception_handler(RequestValidationError, v1_validation_exception_handler)
    app.include_router(router)
