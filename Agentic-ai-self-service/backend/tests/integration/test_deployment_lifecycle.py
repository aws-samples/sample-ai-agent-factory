"""Authenticated integration tests for the deployment state-machine lifecycle.

Every deployment is registered for verified cleanup immediately after the API
accepts it. This closes the historical leak window where a failing assertion
before ``runtime_id`` appeared left live AWS resources behind.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import pytest

from tests.integration.conftest import (
    DeploymentCleanupTracker,
    TrackedDeployment,
)

logger = logging.getLogger(__name__)


def _build_strands_config(name_token: str) -> dict[str, Any]:
    """Build a unique minimal HTTP-runtime config for lifecycle testing."""

    return {
        "name": f"it_lifecycle_{name_token}_{uuid.uuid4().hex[:8]}",
        "entrypoint": "agent.py",
        "framework": "strands_agents",
        "model": {
            "modelId": os.environ.get(
                "INTEGRATION_MODEL_ID",
                "us.anthropic.claude-sonnet-5",
            )
        },
        "systemPrompt": "Follow the integration test request exactly.",
        "deploymentType": "direct_code_deploy",
        "pythonRuntime": "PYTHON_3_13",
        "protocol": "HTTP",
        "idleTimeout": 300,
        "maxLifetime": 3600,
        "enableOtel": False,
        "multiAgentPattern": "none",
    }


def _start_deployment(
    api_session: Any,
    deployment_cleanup: DeploymentCleanupTracker,
    *,
    name_token: str,
) -> tuple[TrackedDeployment, dict[str, Any]]:
    """Start and immediately register one deployment for guaranteed cleanup."""

    payload = {
        "nodeId": f"it-lifecycle-{name_token}-{uuid.uuid4().hex[:8]}",
        "config": _build_strands_config(name_token),
    }
    response = api_session.post(
        f"{api_session.base_url}/api/deploy",
        json=payload,
        timeout=60,
    )
    assert response.status_code == 202, f"POST /api/deploy returned {response.status_code}: {response.text}"
    body = response.json()
    deployment_id = body.get("deploymentId")
    assert isinstance(deployment_id, str) and deployment_id

    record = deployment_cleanup.track(deployment_id)
    assert body.get("status") == "pending"
    logger.info("Started lifecycle deployment %s", deployment_id)
    return record, body


def _bind_succeeded_runtime(
    deployment_cleanup: DeploymentCleanupTracker,
    record: TrackedDeployment,
    status: dict[str, Any],
) -> tuple[str, str]:
    assert status.get("status") == "succeeded", f"Deployment failed: {status.get('error_details') or status}"
    runtime_id = status.get("runtime_id")
    runtime_endpoint = status.get("runtime_endpoint")
    assert isinstance(runtime_id, str) and runtime_id
    assert isinstance(runtime_endpoint, str) and runtime_endpoint
    assert status.get("runtime_protocol") == "HTTP"
    assert isinstance(status.get("created_resources"), list)
    assert status["created_resources"], "Succeeded deployment has no teardown manifest"
    deployment_cleanup.bind_runtime(record, runtime_id)
    record.last_status = status
    return runtime_id, runtime_endpoint


def _invoke_runtime(
    api_session: Any,
    *,
    runtime_id: str,
    runtime_endpoint: str,
) -> dict[str, Any]:
    response = api_session.post(
        f"{api_session.base_url}/api/test-runtime",
        json={
            "endpoint": runtime_endpoint,
            "input": "What is 2 + 2? Answer concisely.",
            "runtimeId": runtime_id,
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    assert body.get("success") is True, f"Runtime invocation failed: {body.get('error') or body}"
    assert isinstance(body.get("response"), str) and body["response"].strip()
    return body


@pytest.mark.integration
class TestDeploymentLifecycle:
    """End-to-end deployment lifecycle through the same API used by the UI."""

    def test_deploy_returns_202_with_deployment_id(
        self,
        api_session: Any,
        deployment_cleanup: DeploymentCleanupTracker,
    ) -> None:
        record, body = _start_deployment(
            api_session,
            deployment_cleanup,
            name_token="accepted",
        )
        assert body["deploymentId"] == record.deployment_id

    def test_state_transitions_pending_to_succeeded(
        self,
        api_session: Any,
        deployment_cleanup: DeploymentCleanupTracker,
        wait_for_deployment: Any,
    ) -> None:
        record, _ = _start_deployment(
            api_session,
            deployment_cleanup,
            name_token="states",
        )

        response = api_session.get(
            f"{api_session.base_url}/api/deploy/{record.deployment_id}",
            timeout=30,
        )
        response.raise_for_status()
        initial = response.json()
        assert initial["status"] in {"pending", "in_progress"}

        final = wait_for_deployment(record.deployment_id)
        _bind_succeeded_runtime(deployment_cleanup, record, final)
        assert final.get("completed_at") or final.get("started_at")

    def test_runtime_invocation_returns_valid_response(
        self,
        api_session: Any,
        deployment_cleanup: DeploymentCleanupTracker,
        wait_for_deployment: Any,
    ) -> None:
        record, _ = _start_deployment(
            api_session,
            deployment_cleanup,
            name_token="invoke",
        )
        final = wait_for_deployment(record.deployment_id)
        runtime_id, runtime_endpoint = _bind_succeeded_runtime(
            deployment_cleanup,
            record,
            final,
        )
        _invoke_runtime(
            api_session,
            runtime_id=runtime_id,
            runtime_endpoint=runtime_endpoint,
        )

    def test_delete_runtime_reaches_durable_deleted_tombstone(
        self,
        api_session: Any,
        deployment_cleanup: DeploymentCleanupTracker,
        wait_for_deployment: Any,
    ) -> None:
        record, _ = _start_deployment(
            api_session,
            deployment_cleanup,
            name_token="delete",
        )
        final = wait_for_deployment(record.deployment_id)
        _bind_succeeded_runtime(deployment_cleanup, record, final)

        tombstone = deployment_cleanup.delete_and_verify(record)
        assert tombstone.get("delete_status") == "deleted"

    def test_full_lifecycle_deploy_poll_invoke_delete(
        self,
        api_session: Any,
        deployment_cleanup: DeploymentCleanupTracker,
        wait_for_deployment: Any,
    ) -> None:
        record, body = _start_deployment(
            api_session,
            deployment_cleanup,
            name_token="full",
        )
        assert body.get("status") == "pending"

        final = wait_for_deployment(record.deployment_id)
        runtime_id, runtime_endpoint = _bind_succeeded_runtime(
            deployment_cleanup,
            record,
            final,
        )
        _invoke_runtime(
            api_session,
            runtime_id=runtime_id,
            runtime_endpoint=runtime_endpoint,
        )

        tombstone = deployment_cleanup.delete_and_verify(record)
        assert tombstone.get("delete_status") == "deleted"
