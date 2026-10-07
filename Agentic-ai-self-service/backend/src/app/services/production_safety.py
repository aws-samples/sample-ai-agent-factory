"""Non-bypassable deployment safety defaults for production-like environments."""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any

DEV_ENVIRONMENTS = frozenset({"dev", "development", "test", "sandbox", "preview", "local"})
PRODUCTION_GUARDRAIL_PROFILE = "production-v1"
PRODUCTION_POLICY_PROFILE = "production-v1"

_BASELINE_CONTENT_FILTERS = {
    "hate": "HIGH",
    "insults": "HIGH",
    "sexual": "HIGH",
    "violence": "HIGH",
    "misconduct": "HIGH",
    "prompt_attack": "HIGH",
}
_BASELINE_PII_ACTIONS = {
    "AWS_ACCESS_KEY": "ANONYMIZE",
    "AWS_SECRET_KEY": "ANONYMIZE",  # pragma: allowlist secret
    "CREDIT_DEBIT_CARD_NUMBER": "ANONYMIZE",
    "EMAIL": "ANONYMIZE",
    "PASSWORD": "ANONYMIZE",  # pragma: allowlist secret
    "PHONE": "ANONYMIZE",
    "SSN": "ANONYMIZE",
    "US_BANK_ACCOUNT_NUMBER": "ANONYMIZE",
    "US_SOCIAL_SECURITY_NUMBER": "ANONYMIZE",
}


class ProductionSafetyConfigurationError(ValueError):
    """A requested deployment cannot satisfy the production safety envelope."""


def current_environment() -> str:
    """Return the normalized platform environment, defaulting local work to dev."""

    return (os.environ.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT_NAME") or "dev").strip().lower()


def is_production_like_environment(environment: str | None = None) -> bool:
    """Whether mandatory production controls apply to this environment."""

    normalized = current_environment() if environment is None else str(environment).strip().lower()
    return normalized not in DEV_ENVIRONMENTS


def _content_filters(config: Mapping[str, Any]) -> dict[str, Any]:
    supplied = config.get("contentFilters")
    if supplied is None:
        supplied = config.get("content_filters")
    if supplied is None:
        return dict(_BASELINE_CONTENT_FILTERS)
    if not isinstance(supplied, Mapping):
        raise ProductionSafetyConfigurationError("Production Guardrail contentFilters must be an object")
    merged = {str(key): value for key, value in supplied.items()}
    # The platform floor wins over a caller's weaker NONE/LOW/MEDIUM value.
    merged.update(_BASELINE_CONTENT_FILTERS)
    return merged


def _pii_filters(config: Mapping[str, Any]) -> list[dict[str, str]]:
    supplied = config.get("piiFilters")
    if supplied is None:
        supplied = config.get("pii_filters")
    if supplied is None:
        supplied = []
    if not isinstance(supplied, list):
        raise ProductionSafetyConfigurationError("Production Guardrail piiFilters must be an array")

    merged: dict[str, str] = dict(_BASELINE_PII_ACTIONS)
    for item in supplied:
        if not isinstance(item, Mapping):
            raise ProductionSafetyConfigurationError("Every production Guardrail PII filter must be an object")
        pii_type = str(item.get("type") or "").strip().upper()
        action = str(item.get("action") or "ANONYMIZE").strip().upper()
        if not pii_type:
            raise ProductionSafetyConfigurationError("Every production Guardrail PII filter requires a type")
        # BLOCK is stronger than the baseline ANONYMIZE floor. Invalid actions are
        # left for the Guardrail builder's existing validation/default behavior.
        if pii_type not in merged or action == "BLOCK":
            merged[pii_type] = action
    return [{"type": pii_type, "action": action} for pii_type, action in sorted(merged.items())]


def enforce_production_guardrail_baseline(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Inject/floor the managed Guardrail profile in production-like environments.

    Existing Guardrails are intentionally refused: READY status does not prove their
    policy still meets this platform's baseline, and Bedrock applies only one Guardrail
    per model call. A platform-created Guardrail is therefore the supported production
    envelope until policy-version attestation is implemented.
    """

    if not is_production_like_environment():
        return copy.deepcopy(dict(config)) if config is not None else None

    prepared = copy.deepcopy(dict(config or {}))
    mode = str(prepared.get("mode") or "create_new").strip().lower()
    if mode != "create_new":
        raise ProductionSafetyConfigurationError(
            "Production deployments require a platform-managed create_new Guardrail; "
            "an existing Guardrail cannot be attested against the mandatory baseline"
        )

    prepared.pop("content_filters", None)
    prepared.pop("pii_filters", None)
    prepared["enabled"] = True
    prepared["mode"] = "create_new"
    prepared["baselineProfile"] = PRODUCTION_GUARDRAIL_PROFILE
    prepared["contentFilters"] = _content_filters(config or {})
    prepared["piiFilters"] = _pii_filters(config or {})
    return prepared


def enforce_production_policy_baseline(
    config: Mapping[str, Any] | None,
    *,
    tool_gateway_required: bool,
    gateway_provider: str,
) -> dict[str, Any] | None:
    """Require non-bypassable Cedar enforcement for production tool Gateways."""

    if not is_production_like_environment() or not tool_gateway_required:
        return copy.deepcopy(dict(config)) if config is not None else None
    if str(gateway_provider or "agentcore").strip().lower() != "agentcore":
        raise ProductionSafetyConfigurationError(
            "Production tool deployments require AgentCore Gateway so Cedar Policy can enforce every tool call"
        )

    prepared = copy.deepcopy(dict(config or {}))
    prepared.pop("onEnforceFailure", None)
    prepared["enabled"] = True
    prepared["mode"] = "ENFORCE"
    prepared["on_enforce_failure"] = "fail_closed"
    prepared["baselineProfile"] = PRODUCTION_POLICY_PROFILE
    return prepared


def enforce_production_invocation_budget_envelope(
    *,
    runtime_protocol: str,
    a2a_config: Mapping[str, Any] | None,
    connected_tools: list[Any] | None,
) -> None:
    """Refuse production invocation paths that can bypass platform reservation.

    The current authorization ledger covers one platform-mediated Runtime dispatch.
    A2A allows both direct inbound Runtime calls and nested peer dispatches outside
    that boundary, so admitting it would turn a hard authorization limit into an
    advisory counter. Development remains available for A2A interoperability work.
    """

    if not is_production_like_environment():
        return
    tools = {
        str(tool).strip().lower() for tool in (connected_tools or []) if isinstance(tool, str) and str(tool).strip()
    }
    a2a_enabled = str(runtime_protocol or "HTTP").strip().upper() == "A2A" or a2a_config is not None or "a2a" in tools
    if a2a_enabled:
        raise ProductionSafetyConfigurationError(
            "Production A2A deployment is outside the invocation-authorization "
            "support envelope: inbound and nested peer Runtime calls do not yet "
            "reserve platform capacity. Use a development environment until "
            "reservation propagation is implemented."
        )


def enforce_production_execution_mode(deployment_mode: str | None) -> None:
    """Keep production inside the fully Guardrail-enforced Runtime envelope.

    The pinned ``CreateHarness`` model has lifecycle hooks but no native Bedrock
    Guardrail model field. Its documented ``after_tool_call`` deny retains the
    completed tool result before stopping, and ``after_invocation`` cannot retract
    streamed output. A managed Harness therefore cannot satisfy mandatory input,
    output, tool-argument, and tool-result screening yet.
    """

    if is_production_like_environment() and str(deployment_mode or "runtime").lower() == "harness":
        raise ProductionSafetyConfigurationError(
            "Production managed Harness deployment is outside the mandatory Guardrail "
            "support envelope: Harness hooks cannot redact model output or retract a "
            "completed unsafe tool result. Use the code-generated Runtime path in "
            "production; Harness remains available in development."
        )
