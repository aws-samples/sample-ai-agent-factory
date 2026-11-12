"""Platform feature flags: one SSM parameter, a registry of known flags, fail closed.

Every surface that is not ready for every environment ships behind a flag so partial work can
merge to ``main``. A flag is a boolean in the JSON object held by the SSM parameter
``/agentcore-workflow/{env}/platform-features`` (created by the platform stack with ``{}``,
every flag off; operators flip flags with ``aws ssm put-parameter --overwrite``). The backend
returns the exact ``404 {"detail": "Not Found"}`` of a nonexistent route for a flagged-off
route (``require_feature``), and the frontend hides the surface (``useFeatureFlags``).

Fail-closed rules, each pinned by a test:

* a flag is **on** only when the parameter holds the literal JSON ``true`` under a KNOWN name;
* a missing parameter, an SSM error, a non-JSON or non-object value, an unknown key, or a
  non-boolean value all resolve to **off**; unknown and non-boolean keys are reported in
  ``ignored`` so a typo is visible to the operator instead of silently doing nothing;
* ``require_feature`` refuses an unregistered flag name at import time (a typo must not create
  a route that is permanently off and looks intentional);
* the resolved set is cached per Lambda container for ``FEATURE_FLAGS_TTL_SECONDS`` (60 s)
  so one ``tools/list``-sized request burst costs one SSM read, and an operator change lands
  within the TTL without a redeploy.

Local development (``ENVIRONMENT`` unset or ``local``) reads the JSON from the
``FEATURE_FLAGS`` environment variable instead of SSM, with the same rules.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request

from app.services.config import (
    _build_ssm_parameter_path,
    _create_ssm_client,
    _get_current_environment,
    _is_local_environment,
)

logger = logging.getLogger(__name__)

#: Every flag this build knows, with the surface it gates. A flag is registered in the PR
#: that adds the surface and removed in the PR that makes the surface unconditional.
KNOWN_FEATURE_FLAGS: dict[str, str] = {
    "platformFlagsCanary": (
        "Operational self-test for the flag plumbing: enables GET /api/config/features/canary. "
        "Gates no product feature; flip it to prove an environment's flags path end to end."
    ),
}

PARAMETER_SUFFIX = "platform-features"
LOCAL_ENV_VAR = "FEATURE_FLAGS"
TTL_ENV_VAR = "FEATURE_FLAGS_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 60
#: What a nonexistent route returns; a flagged-off route must be indistinguishable from one.
NOT_FOUND_DETAIL = "Not Found"

#: Where the resolved set came from. ``ssm``/``env`` carry real values; everything else is the
#: fail-closed default and says why.
SOURCE_SSM = "ssm"
SOURCE_ENV = "env"
SOURCE_MISSING = "missing"
SOURCE_INVALID = "invalid"
SOURCE_ERROR = "error"


class UnknownFeatureFlagError(ValueError):
    """``require_feature`` was given a name that is not in ``KNOWN_FEATURE_FLAGS``."""


@dataclass(frozen=True)
class FeatureFlagSet:
    """The resolved flags for one read, plus how they were resolved."""

    features: dict[str, bool]
    source: str
    parameter: str
    ignored: tuple[str, ...] = ()
    refreshed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def enabled(self, name: str) -> bool:
        return self.features.get(name) is True

    def as_response(self, ttl_seconds: int) -> dict:
        return {
            "features": dict(self.features),
            "source": self.source,
            "parameter": self.parameter,
            "ignored": list(self.ignored),
            "ttlSeconds": ttl_seconds,
            "refreshedAt": self.refreshed_at,
        }


# --------------------------------------------------------------------------- parsing


def all_off() -> dict[str, bool]:
    return dict.fromkeys(KNOWN_FEATURE_FLAGS, False)


def parse_feature_flags(raw: str | None) -> tuple[dict[str, bool], tuple[str, ...], bool]:
    """``(features, ignored keys, valid)`` for one raw parameter value.

    ``valid`` is False when the value is not a JSON object; the features are then all off.
    """
    features = all_off()
    if raw is None:
        return features, (), True
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return features, (), False
    if not isinstance(decoded, Mapping):
        return features, (), False
    ignored: list[str] = []
    for key, value in decoded.items():
        if key not in KNOWN_FEATURE_FLAGS or not isinstance(value, bool):
            ignored.append(str(key))
            continue
        features[key] = value
    return features, tuple(sorted(ignored)), True


# --------------------------------------------------------------------------- reading


def parameter_name(environment: str) -> str:
    return _build_ssm_parameter_path(environment, PARAMETER_SUFFIX)


def ttl_seconds() -> int:
    raw = os.environ.get(TTL_ENV_VAR, "")
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        value = DEFAULT_TTL_SECONDS
    return max(0, value)


def _read_ssm_value(ssm_client, name: str) -> tuple[str | None, str]:
    """``(raw value or None, source)``; never raises (an unreadable store means all off)."""
    try:
        response = ssm_client.get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            logger.warning("feature flags: parameter %s is absent; every flag is off", name)
            return None, SOURCE_MISSING
        logger.error("feature flags: SSM read of %s failed (%s); every flag is off", name, type(exc).__name__)
        return None, SOURCE_ERROR
    except BotoCoreError as exc:
        logger.error("feature flags: SSM read of %s failed (%s); every flag is off", name, type(exc).__name__)
        return None, SOURCE_ERROR
    return str(response.get("Parameter", {}).get("Value", "")), SOURCE_SSM


def _resolve(environment: str, ssm_client_factory: Callable[[str], object]) -> FeatureFlagSet:
    name = parameter_name(environment)
    if _is_local_environment(environment):
        raw, source = os.environ.get(LOCAL_ENV_VAR), SOURCE_ENV
        if raw is None:
            source = SOURCE_MISSING
    else:
        region = os.environ.get("AWS_REGION") or "us-east-1"
        try:
            raw, source = _read_ssm_value(ssm_client_factory(region), name)
        except Exception as exc:  # noqa: BLE001 — client construction itself failed; still fail closed
            logger.error("feature flags: SSM client unavailable (%s); every flag is off", type(exc).__name__)
            raw, source = None, SOURCE_ERROR
    features, ignored, valid = parse_feature_flags(raw)
    if not valid:
        logger.error("feature flags: %s does not hold a JSON object of booleans; every flag is off", name)
        source = SOURCE_INVALID
    if ignored:
        logger.warning("feature flags: ignoring unknown or non-boolean keys in %s: %s", name, list(ignored))
    return FeatureFlagSet(features=features, source=source, parameter=name, ignored=ignored)


# --------------------------------------------------------------------------- cache


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: FeatureFlagSet | None = None
        self._loaded_at: float = 0.0

    def get(self, *, force_refresh: bool = False) -> FeatureFlagSet:
        ttl = ttl_seconds()
        with self._lock:
            fresh = self._value is not None and (time.monotonic() - self._loaded_at) < ttl
            if fresh and not force_refresh:
                return self._value  # type: ignore[return-value]
            self._value = _resolve(_get_current_environment(), _create_ssm_client)
            self._loaded_at = time.monotonic()
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._loaded_at = 0.0


_cache = _Cache()


def get_feature_flags(*, force_refresh: bool = False) -> FeatureFlagSet:
    """The current flag set (cached per container for the TTL)."""

    return _cache.get(force_refresh=force_refresh)


def reset_feature_flag_cache() -> None:
    """Tests and the local reloader drop the cache; production relies on the TTL."""

    _cache.reset()


def feature_enabled(name: str) -> bool:
    if name not in KNOWN_FEATURE_FLAGS:
        raise UnknownFeatureFlagError(f"unknown feature flag {name!r}; known: {sorted(KNOWN_FEATURE_FLAGS)}")
    return get_feature_flags().enabled(name)


# --------------------------------------------------------------------------- FastAPI dependency


def require_feature(name: str) -> Callable[[Request], None]:
    """Dependency factory: a route behind ``name`` is a 404 while the flag is off.

    Place it BEFORE ``require_scopes`` in a route's dependencies so a flagged-off route answers
    404 to every caller; a 403 first would tell a caller the route exists.
    """

    if name not in KNOWN_FEATURE_FLAGS:
        raise UnknownFeatureFlagError(f"require_feature: unknown flag {name!r}; known: {sorted(KNOWN_FEATURE_FLAGS)}")

    def _dep(request: Request) -> None:
        if get_feature_flags().enabled(name):
            return
        logger.info("feature flag %s is off; %s %s answers 404", name, request.method, request.scope.get("path"))
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)

    _dep.feature_flag = name  # type: ignore[attr-defined]  # read by tests the way require_scopes exposes its scopes
    return _dep
