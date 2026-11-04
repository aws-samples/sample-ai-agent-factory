"""Pure observability configuration for customer-owned CloudFormation exports."""

from pydantic import ValidationError

from app.models.deployment_models import DeployRequest, ObservabilityConfig
from app.services.deployment_payload_validation import PayloadPhase, validate_deployment_payload
from app.services.observability import build_otel_env_vars, build_otel_resource_attributes


class CfnObservabilityError(ValueError):
    """An observability setting cannot be safely or faithfully exported."""


_DESCRIPTIVE_FIELDS = frozenset({"name", "label", "description", "componentType", "component_type"})
_KNOWN_FIELDS = (
    _DESCRIPTIVE_FIELDS
    | {"enable_otel"}
    | frozenset(ObservabilityConfig.model_fields)
    | frozenset(field.alias for field in ObservabilityConfig.model_fields.values() if field.alias)
)


def build_export_observability_env(
    request: DeployRequest,
    *,
    runtime_name: str,
) -> dict[str, str]:
    """Resolve once for runtime environment, secret grants, and instrumentation.

    Match runtime_configure_step's effective-block precedence: a nonempty
    top-level block wins, otherwise use nested runtime observability. Do not
    consult the source platform's SSM defaults: this artifact belongs to the
    receiving account. The enable flag controls external OTLP export, not the
    native usage ledger initialized for a configured or connected node.
    """
    nested = request.config.observability
    raw = request.observability_config or (nested.model_dump() if nested is not None else None)
    if raw is None:
        # The platform injects the native usage bootstrap for these advertised
        # enabling markers, even without an external collector configuration.
        if "observability" not in (request.connected_tools or []) and not request.config.enable_otel:
            return {}
        raw = {"enabled": True}

    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if unknown:
        raise CfnObservabilityError(
            "CloudFormation cannot preserve unknown observability settings: " + ", ".join(unknown)
        )

    # Apply the live env builder's snake/camel precedence before model
    # validation; in particular, a zero sample rate is not an absent value.
    normalized = {
        "enabled": raw.get("enabled", raw.get("enableOtel", raw.get("enable_otel", True))),
        "provider": raw.get("provider", "langfuse"),
        "otlp_endpoint": raw.get("otlp_endpoint") or raw.get("otlpEndpoint"),
        "otlp_protocol": raw.get("otlp_protocol") or raw.get("otlpProtocol", "http/protobuf"),
        "service_name": raw.get("service_name") or raw.get("serviceName"),
        "sample_rate": raw["sample_rate"] if "sample_rate" in raw else raw.get("sampleRate", 1.0),
        "resource_attributes": raw.get("resource_attributes") or raw.get("resourceAttributes") or {},
        "auth_header_secret_arn": raw.get("auth_header_secret_arn") or raw.get("authHeaderSecretArn"),
        "extra_headers": raw.get("extra_headers") or raw.get("extraHeaders") or {},
    }
    # An explicit false on any accepted spelling wins, as it does in
    # build_otel_env_vars; model alias precedence must not turn it back on.
    if any(raw.get(key) is False for key in ("enabled", "enableOtel", "enable_otel")):
        normalized["enabled"] = False
    try:
        observability = ObservabilityConfig.model_validate(normalized)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        # Pydantic's formatted error includes input values. Never put endpoint,
        # header, or attribute material into export-error responses.
        raise CfnObservabilityError("Invalid observability settings: " + ", ".join(fields)) from None

    # Use the same pure credential scan as the platform boundary, scoped to
    # observability. It also catches credential-bearing resource attributes.
    checked = validate_deployment_payload(
        {
            "config": {"model": request.config.model},
            "templateId": request.template_id,
            "observabilityConfig": observability.model_dump(by_alias=True),
        },
        phase=PayloadPhase.REQUEST,
    )
    if not checked.is_valid:
        paths = sorted({error.field for error in checked.errors})
        raise CfnObservabilityError(
            "Observability settings cannot be safely exported: "
            + ", ".join(paths)
            + ". Supply authentication through authHeaderSecretArn, not plaintext."
        )
    if observability.extra_headers:
        # Keep the existing export contract: arbitrary raw headers are not
        # serialized. Nested and top-level forms must enforce it equally.
        raise CfnObservabilityError(
            "Observability extraHeaders cannot be exported as plaintext; use authHeaderSecretArn."
        )

    try:
        resource_attributes = build_otel_resource_attributes(
            observability.resource_attributes,
            deployment_id=runtime_name,
        )
    except ValueError as exc:
        # The shared serializer's error is deliberately value-free.
        raise CfnObservabilityError(str(exc)) from None

    effective = observability.model_dump()
    if not observability.enabled:
        # This checkbox opts out of the external backend, not the local usage
        # ledger. Match the live codegen path, which still initializes telemetry
        # for a configured/connected node. Never carry endpoint credentials into
        # that native-only plan, including a provider's default collector URL.
        effective.update(
            enabled=True,
            otlp_endpoint=None,
            auth_header_secret_arn=None,
            extra_headers={},
        )
    env = build_otel_env_vars(
        effective,
        runtime_name=runtime_name,
        deployment_id=runtime_name,
        enable_otel_legacy=request.config.enable_otel,
    )
    if env.get("AGENT_OBSERVABILITY_ENABLED") == "true":
        # Native usage capture has no external OTLP endpoint, but configured
        # resource identity and sampling still matter. Preserve them without
        # manufacturing an exporter or disabling the local usage ledger.
        if observability.service_name:
            env["OTEL_SERVICE_NAME"] = observability.service_name
        if "sample_rate" in raw or "sampleRate" in raw:
            env["OTEL_TRACES_SAMPLER"] = "parentbased_traceidratio"
            env["OTEL_TRACES_SAMPLER_ARG"] = str(observability.sample_rate)
        if observability.resource_attributes:
            env["OTEL_RESOURCE_ATTRIBUTES"] = resource_attributes
    return env
