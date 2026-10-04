"""Inherited observability must not silently change a standalone MCP runtime.

The model-free FastMCP bundle intentionally carries no generic agent OTEL
instrumentation. Platform defaults therefore conflict with this template just
as an explicit observabilityConfig does. The important contract is ordering:
the refusal must happen before deployment/version rows, credential staging, or
Step Functions, not later in code generation after resources already exist.
"""

from __future__ import annotations

from app import deployment_handler as dh

from tests.test_deploy_gate_ordering import _client, spy  # noqa: F401


def _request() -> dict:
    return {
        "nodeId": "standalone-mcp-platform-otel",
        "templateId": "mcp-server-runtime",
        "config": {
            "name": "standalone_mcp_platform_otel",
            "protocol": "MCP",
            "enableOtel": False,
        },
    }


def test_inherited_platform_otel_refuses_mcp_before_every_side_effect(
    spy,  # noqa: F811
    monkeypatch,
) -> None:
    import app.services.observability as observability

    defaults = {
        "enabled": True,
        "provider": "custom",
        "otlp_endpoint": "https://otel.example.test/v1/traces",
        "otlp_protocol": "http/protobuf",
        "auth_header_secret_arn": ("arn:aws:secretsmanager:us-east-1:166827918465:secret:agentcore-otel/platform-test"),
        "sample_rate": 1.0,
    }
    monkeypatch.setattr(
        observability,
        "get_platform_observability_defaults",
        lambda: defaults,
    )

    # This seam sits after both durable row writes today. A correct handler
    # rejects from a preflight check and never reaches it; the current ordering
    # reaches it, proving that a later codegen refusal would be post-hoc.
    def _must_not_be_reached(**kwargs):
        spy.calls.append("runtime-credential-preparation")
        return kwargs["runtime_config"], None, defaults, []

    monkeypatch.setattr(dh, "_prepare_runtime_credentials", _must_not_be_reached)

    response = _client().post("/api/deploy", json=_request())

    assert response.status_code in {409, 422}, response.text
    detail = response.text.lower()
    assert "mcp" in detail
    assert "observability" in detail or "otel" in detail
    assert spy.side_effects == [], (
        f"an inherited-OTEL conflict must cost zero rows, secrets, and executions; observed {spy.side_effects}"
    )


def test_the_same_mcp_request_is_admitted_without_platform_otel(
    spy,  # noqa: F811
    monkeypatch,
) -> None:
    import app.services.observability as observability

    monkeypatch.setattr(
        observability,
        "get_platform_observability_defaults",
        lambda: None,
    )

    response = _client().post("/api/deploy", json=_request())

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1


def test_a_harness_is_not_misclassified_by_its_gallery_template_id(
    spy,  # noqa: F811
    monkeypatch,
) -> None:
    """Harness mode has no generated FastMCP runtime to receive platform OTEL."""
    import app.services.observability as observability

    monkeypatch.setattr(
        observability,
        "get_platform_observability_defaults",
        lambda: {"enabled": True},
    )
    monkeypatch.setattr(
        dh,
        "_prepare_runtime_credentials",
        lambda **kwargs: (kwargs["runtime_config"], None, None, []),
    )
    request = _request()
    request["deploymentMode"] = "harness"

    response = _client().post("/api/deploy", json=request)

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1


def test_an_unreadable_platform_otel_policy_refuses_mcp_before_every_side_effect(
    spy,  # noqa: F811
    monkeypatch,
) -> None:
    """An SSM outage is not evidence that the operator disabled locked OTEL.

    The platform contract says every deployed agent inherits the admin-managed
    defaults when configured. Treating a failed SSM read as ``None`` makes the
    standalone MCP preflight indistinguishable from "not configured", admits a
    runtime that may conflict with the locked policy, and caches that fail-open
    answer for the Lambda's lifetime.
    """
    import app.services.observability as observability
    import boto3

    class _UnavailableSsm:
        def get_parameters_by_path(self, **_kwargs):
            raise RuntimeError("simulated SSM outage")

    original_client = boto3.client

    def _boto_client(service_name: str, *args, **kwargs):
        if service_name == "ssm":
            return _UnavailableSsm()
        return original_client(service_name, *args, **kwargs)

    observability.get_platform_observability_defaults.cache_clear()
    monkeypatch.setattr(boto3, "client", _boto_client)
    try:
        response = _client().post("/api/deploy", json=_request())
    finally:
        observability.get_platform_observability_defaults.cache_clear()

    assert response.status_code == 503, response.text
    detail = response.text.lower()
    assert "observability" in detail or "otel" in detail
    assert spy.side_effects == [], (
        "an unreadable locked-observability policy must cost zero rows, "
        f"secrets, and executions; observed {spy.side_effects}"
    )
