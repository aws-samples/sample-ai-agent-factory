"""Atomic authorization capacity for platform-mediated Runtime dispatches."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException

from app.services.budget_store import (
    BudgetExceeded,
    BudgetReservation,
    BudgetReservationError,
    get_budget_store,
)
from app.services.production_safety import is_production_like_environment

logger = logging.getLogger(__name__)


def reserve_invocation_budget(
    *,
    owner_sub: str,
    runtime_name: str,
    resource_tags: dict[str, Any] | None = None,
) -> BudgetReservation | None:
    """Reserve the configured authorization unit or return an exact API denial."""

    required = is_production_like_environment()
    if not os.environ.get("BUDGET_TABLE_NAME", "").strip():
        if required:
            raise HTTPException(
                status_code=503,
                detail="Production invocation budget storage is not configured; no runtime call was made.",
            )
        return None
    tags = {
        str(key): str(value)
        for key, value in (resource_tags or {}).items()
        if isinstance(key, str) and value is not None
    }
    try:
        return get_budget_store().reserve_invocation(
            "default",
            owner_sub=owner_sub,
            runtime_name=runtime_name,
            resource_tags=tags,
            required=required,
        )
    except BudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="Invocation denied: an applicable monthly authorization limit has no remaining capacity.",
            headers={"Retry-After": "3600"},
        ) from exc
    except BudgetReservationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Invocation budget authorization is temporarily unavailable; no runtime call was made.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected invocation budget authorization failure (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Invocation budget authorization is temporarily unavailable; no runtime call was made.",
        ) from exc


def settle_invocation_budget(
    reservation: BudgetReservation | None,
    *,
    dispatched: bool,
) -> None:
    """Commit a dispatched unit or release one proven not dispatched."""

    if reservation is None:
        return
    try:
        get_budget_store().settle_reservation(reservation, success=dispatched)
    except Exception as exc:  # noqa: BLE001
        # Conservative failure: the unit remains RESERVED and blocks capacity until
        # opportunistic/scheduled stale reconciliation. Never release on uncertainty.
        logger.error(
            "Invocation budget settlement failed; reservation remains conservative (%s)",
            type(exc).__name__,
        )
