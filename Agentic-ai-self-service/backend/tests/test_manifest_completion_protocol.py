"""A deployment may be successful only with durable teardown inventory."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from app.models.deployment_models import DeploymentStatusEnum
from app.services.deployment_state_store import DeploymentStateStore
from app.step_handlers.status_update_step import (
    _manifest_completion_errors,
    _supplemental_failure_resources,
    handler,
)


def test_best_effort_append_marks_the_manifest_incomplete():
    store = object.__new__(DeploymentStateStore)
    store.record_resource_strict = MagicMock(side_effect=RuntimeError("ddb"))
    store.mark_resource_manifest_error = MagicMock()

    store.record_resource(
        "dep-1",
        {
            "type": "gateway",
            "id": "gw-1",
            "created_by_deployment": True,
        },
    )

    store.mark_resource_manifest_error.assert_called_once_with("dep-1")


def test_append_fails_the_step_if_even_the_error_marker_is_not_durable():
    store = object.__new__(DeploymentStateStore)
    store.record_resource_strict = MagicMock(side_effect=RuntimeError("append"))
    store.mark_resource_manifest_error = MagicMock(side_effect=RuntimeError("marker"))

    with pytest.raises(RuntimeError, match="append"):
        store.record_resource(
            "dep-1",
            {
                "type": "gateway",
                "id": "gw-1",
                "created_by_deployment": True,
            },
        )


def test_success_cannot_seal_a_manifest_with_a_recorded_error():
    store = object.__new__(DeploymentStateStore)
    state = MagicMock()
    state.resource_manifest_error = True
    store.get = MagicMock(return_value=state)

    with pytest.raises(ValueError, match="durability failure"):
        store.update_status(
            "dep-1",
            DeploymentStatusEnum.SUCCEEDED,
            completed_at=datetime.now(timezone.utc),
            resource_manifest_complete=True,
        )


def test_manifest_seal_uses_an_atomic_no_error_condition():
    store = object.__new__(DeploymentStateStore)
    store._table = MagicMock()
    state = MagicMock()
    state.resource_manifest_error = False
    store.get = MagicMock(return_value=state)

    with patch("app.services.deployment_state_store._update_item") as update_item:
        store.update_status(
            "dep-1",
            DeploymentStatusEnum.SUCCEEDED,
            resource_manifest_complete=True,
        )

    kwargs = update_item.call_args.kwargs
    assert "attribute_not_exists(resource_manifest_error)" in kwargs["condition_expr"]
    assert kwargs["expr_values"][":manifest_error_false"] is False
    assert kwargs["expr_values"][":resource_manifest_complete"] is True


def test_structural_validation_catches_a_forgotten_primary_handle():
    state = {
        "resource_manifest_version": 1,
        "resource_manifest_error": False,
        "target_region": "us-east-1",
        "created_resources": [
            {
                "type": "s3_object",
                "id": "s3://artifacts/deployments/dep-1/code.zip",
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
    }
    event = {
        "deployment_mode": "runtime",
        "runtime_id": "rt-1",
        "s3_bucket": "artifacts",
        "s3_key": "deployments/dep-1/code.zip",
    }

    errors = _manifest_completion_errors(state, event)

    assert errors == ["missing agent_runtime teardown handle for rt-1"]


def test_incomplete_failure_recovers_gateway_children_from_the_real_writer():
    record = {
        "resource_manifest_version": 1,
        "resource_manifest_complete": False,
    }
    event = {
        "deployment_mode": "runtime",
        "runtime_id": "rt-1",
        "gateway_result": {
            "gateway_id": "gw-1",
            "gateway_created_by_deployment": True,
            "gateway_name": "orders",
            "gateway_role_name": "AgentCoreGateway-orders",
            "gateway_role_created_by_deployment": True,
            "custom_tool_lambdas": ["AgentCore-CustomTool-orders-a1b2c3d4"],
            "connector_credential_providers": ["API_KEY:orders-key"],
        },
    }

    rows = _supplemental_failure_resources(record, event, "us-east-1")
    identities = {(row["type"], row.get("id") or row.get("name")) for row in rows}

    assert ("agent_runtime", "rt-1") in identities
    assert ("gateway", "gw-1") in identities
    assert ("iam_role", "AgentCoreGateway-orders") in identities
    assert (
        "lambda",
        "AgentCore-CustomTool-orders-a1b2c3d4",
    ) in identities
    assert ("api_key_credential_provider", "orders-key") in identities


def test_handler_fails_and_cleans_up_when_a_versioned_manifest_has_an_append_error(
    monkeypatch,
):
    """A durable append-error marker is a failed deployment, not a warning.

    This exercises the public status-update handler rather than only the
    structural helper/store seam: the row must never be published as
    SUCCEEDED, every available result is persisted for fallback cleanup, and
    automatic compensation is invoked.
    """
    from app.step_handlers import status_update_step

    deployment_id = "dep-manifest-error"
    store = MagicMock()
    store.acquire_finalizer_lease.return_value = "lease-token"
    store.get.return_value = {
        "deployment_id": deployment_id,
        "resource_manifest_version": 1,
        "resource_manifest_error": True,
        "target_region": "us-east-1",
        "created_resources": [
            {
                "type": "agent_runtime",
                "id": "runtime-1",
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
    }
    cleanup = MagicMock()
    monkeypatch.setattr(
        status_update_step,
        "_get_deployment_store",
        lambda: store,
    )
    monkeypatch.setattr(
        status_update_step,
        "_auto_cleanup_on_failure",
        cleanup,
    )

    result = handler(
        {
            "deployment_id": deployment_id,
            "deployment_mode": "runtime",
            "runtime_id": "runtime-1",
            "runtime_arn": ("arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/runtime-1"),
            "target_region": "us-east-1",
        },
        None,
    )

    assert result["status"] == DeploymentStatusEnum.FAILED.value
    assert "resource-manifest writes failed" in result["error_details"]
    terminal_calls = [
        call
        for call in store.update_status.call_args_list
        if call.args[1]
        in {
            DeploymentStatusEnum.SUCCEEDED,
            DeploymentStatusEnum.FAILED,
        }
    ]
    assert len(terminal_calls) == 1
    assert terminal_calls[0].args[1] is DeploymentStatusEnum.FAILED
    assert terminal_calls[0].kwargs["resource_manifest_complete"] is False
    assert terminal_calls[0].kwargs["runtime_id"] == "runtime-1"
    cleanup.assert_called_once_with(
        store,
        deployment_id,
        {
            "deployment_id": deployment_id,
            "deployment_mode": "runtime",
            "runtime_id": "runtime-1",
            "runtime_arn": ("arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/runtime-1"),
            "target_region": "us-east-1",
        },
        finalizer_token="lease-token",
    )
