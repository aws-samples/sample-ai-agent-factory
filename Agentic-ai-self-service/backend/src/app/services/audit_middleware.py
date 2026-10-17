"""Shared audit middleware for both control-plane FastAPI applications."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse

from app.services.audit_store import AuditEvent, classify_action, get_audit_store
from app.services.production_safety import is_production_like_environment

logger = logging.getLogger(__name__)


def _actor(request: Any) -> str:
    from app.services.auth import get_caller_sub

    try:
        return str(get_caller_sub(request) or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _event(
    request: Any,
    *,
    action: str,
    phase: str,
    status_code: int,
    correlation_id: str,
) -> AuditEvent:
    session_uuid = (request.headers.get("x-session-id") or "")[:128] or None
    return AuditEvent(
        org_id="default",
        actor_sub=_actor(request),
        action=action,
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        session_uuid=session_uuid,
        phase=phase,
        correlation_id=correlation_id,
    )


async def audit_http_request(
    request: Any,
    call_next: Callable[[Any], Awaitable[Any]],
):
    """Audit one request, failing closed before production write side effects.

    Production writes first append an immutable ``attempt`` row. If that create
    fails, the route is never called. The correlated ``outcome`` is appended after
    the response is known. A late outcome failure cannot roll back an arbitrary AWS
    or DynamoDB mutation, so the durable attempt remains explicitly visible as
    incomplete instead of returning a misleading empty audit trail.
    """

    action = classify_action(request.method, request.url.path)
    if action is None:
        return await call_next(request)
    # Public webhooks authenticate inside their route. That route appends its
    # intent only after HMAC/replay validation and before queue side effects;
    # pre-auditing here would let unauthenticated traffic create rows.
    if action == "trigger.invoke":
        return await call_next(request)

    required = is_production_like_environment()
    correlation_id = uuid.uuid4().hex
    if required:
        try:
            get_audit_store().record(
                _event(
                    request,
                    action=action,
                    phase="attempt",
                    status_code=0,
                    correlation_id=correlation_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Required audit intent could not be recorded (%s); request refused before route execution",
                type(exc).__name__,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Required audit evidence is unavailable, so this write was refused "
                        "before any application side effect. Retry shortly."
                    )
                },
            )

    try:
        response = await call_next(request)
    except Exception:
        try:
            get_audit_store().record(
                _event(
                    request,
                    action=action,
                    phase="outcome",
                    status_code=500,
                    correlation_id=correlation_id,
                )
            )
        except Exception as audit_exc:  # noqa: BLE001
            logger.error("Audit outcome append failed after route exception (%s)", type(audit_exc).__name__)
        raise

    try:
        get_audit_store().record(
            _event(
                request,
                action=action,
                phase="outcome",
                status_code=int(getattr(response, "status_code", 0)),
                correlation_id=correlation_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Audit outcome append failed after route completion (%s); immutable attempt remains incomplete",
            type(exc).__name__,
        )
    return response
