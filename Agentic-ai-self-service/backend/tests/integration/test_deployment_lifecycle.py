"""Integration tests for the deployment state machine lifecycle.

Verifies the full deploy → poll → invoke → delete lifecycle via the API,
focusing on state transitions and API contract rather than specific templates.
Uses a simple strands-agents deployment as the test vehicle.

Requirements: 3.1, 3.5, 3.6, 3.7, 9.1, 9.3
"""

import logging

import pytest

logger = logging.getLogger(__name__)

# Terminal deployment states used for polling assertions
TERMINAL_STATES = {"succeeded", "failed"}


def _build_strands_config() -> dict:
    """Build a minimal strands-agents RuntimeConfig for lifecycle testing."""
    return {
        "name": "integ-lifecycle-test",
        "entrypoint": "agent.py",
        "framework": "strands_agents",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": "You are a helpful assistant used for integration testing.",
        "deploymentType": "agentcore",
        "pythonRuntime": "PYTHON_3_13",
        "protocol": "HTTP",
        "idleTimeout": 900,
        "maxLifetime": 28800,
        "enableOtel": True,
    }


@pytest.mark.integration
class TestDeploymentLifecycle:
    """End-to-end deployment lifecycle: deploy → poll → invoke → delete.

    Validates: Requirements 3.1, 3.5, 3.6, 3.7, 9.1, 9.3
    """

    def test_deploy_returns_202_with_deployment_id(
        self,
        api_session,
        deployment_cleanup,
    ):
        """POST /api/deploy returns 202 with a deployment_id.

        Validates: Requirement 3.1
        """
        base = api_session.base_url
        payload = {
            "nodeId": "lifecycle-test-node",
            "config": _build_strands_config(),
        }

        resp = api_session.post(f"{base}/api/deploy", json=payload, timeout=60)

        assert resp.status_code == 202, f"Expected 202 from POST /api/deploy, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "deploymentId" in body, "Response must contain deploymentId"
        assert body["deploymentId"], "deploymentId must not be empty"
        assert body.get("status") == "pending", f"Initial status should be 'pending', got '{body.get('status')}'"

        # Track for cleanup even though we don't wait for completion here.
        # The deployment_cleanup fixture will attempt DELETE if a runtime_id
        # is present; for a just-started deployment there may not be one yet,
        # but we record the deployment_id for logging purposes.
        deployment_cleanup.append({"runtime_id": body["deploymentId"]})

    def test_state_transitions_pending_to_succeeded(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        """Deployment progresses through pending → in_progress → succeeded.

        Validates: Requirements 3.5, 3.6, 3.7
        """
        base = api_session.base_url
        payload = {
            "nodeId": "lifecycle-transitions-node",
            "config": _build_strands_config(),
        }

        # -- Start deployment --
        deploy_resp = api_session.post(
            f"{base}/api/deploy",
            json=payload,
            timeout=60,
        )
        assert deploy_resp.status_code == 202
        deployment_id = deploy_resp.json()["deploymentId"]
        logger.info("Started lifecycle deployment: %s", deployment_id)

        # -- Verify initial status is pending --
        status_resp = api_session.get(
            f"{base}/api/deploy/{deployment_id}",
            timeout=30,
        )
        assert status_resp.status_code == 200
        initial = status_resp.json()
        assert initial["status"] in {"pending", "in_progress"}, (
            f"Initial poll should show pending or in_progress, got '{initial['status']}'"
        )

        # -- Track observed states while polling --
        observed_states: set[str] = {initial["status"]}

        final_status = wait_for_deployment(deployment_id)
        observed_states.add(final_status["status"])

        # We expect to have seen at least pending and the terminal state.
        # in_progress may or may not be captured depending on timing.
        assert final_status["status"] == "succeeded", (
            f"Deployment did not succeed: {final_status.get('error_details', final_status)}"
        )
        assert "pending" in observed_states, "Should have observed 'pending' state at some point"

        runtime_id = final_status.get("runtime_id")
        assert runtime_id, "Succeeded deployment must have a runtime_id"
        assert final_status.get("runtime_endpoint"), "Succeeded deployment must have a runtime_endpoint"

        # Track for cleanup
        deployment_cleanup.append({"runtime_id": runtime_id})

    def test_runtime_invocation_returns_valid_response(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        """POST /api/test-runtime returns a valid response for a deployed agent.

        Validates: Requirement 9.1
        """
        base = api_session.base_url

        # -- Deploy --
        payload = {
            "nodeId": "lifecycle-invoke-node",
            "config": _build_strands_config(),
        }
        deploy_resp = api_session.post(
            f"{base}/api/deploy",
            json=payload,
            timeout=60,
        )
        assert deploy_resp.status_code == 202
        deployment_id = deploy_resp.json()["deploymentId"]
        logger.info("Started deployment for invocation test: %s", deployment_id)

        # -- Wait for success --
        final = wait_for_deployment(deployment_id)
        assert final["status"] == "succeeded", f"Deployment failed: {final.get('error_details', final)}"

        runtime_id = final["runtime_id"]
        runtime_endpoint = final["runtime_endpoint"]
        deployment_cleanup.append({"runtime_id": runtime_id})

        # -- Invoke --
        invoke_payload = {
            "endpoint": runtime_endpoint,
            "input": "What is 2 + 2?",
            "runtimeId": runtime_id,
        }
        invoke_resp = api_session.post(
            f"{base}/api/test-runtime",
            json=invoke_payload,
            timeout=120,
        )
        invoke_resp.raise_for_status()
        invoke_body = invoke_resp.json()

        assert invoke_body.get("success") is True, f"Runtime invocation failed: {invoke_body.get('error', invoke_body)}"
        assert invoke_body.get("response"), "Invocation must return a non-empty response"
        logger.info("Invocation response: %.200s", invoke_body["response"])

    def test_delete_runtime_returns_cleanup_summary(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        """DELETE /api/runtime/{id} returns success with a cleanup summary.

        Validates: Requirement 9.3
        """
        base = api_session.base_url

        # -- Deploy --
        payload = {
            "nodeId": "lifecycle-delete-node",
            "config": _build_strands_config(),
        }
        deploy_resp = api_session.post(
            f"{base}/api/deploy",
            json=payload,
            timeout=60,
        )
        assert deploy_resp.status_code == 202
        deployment_id = deploy_resp.json()["deploymentId"]
        logger.info("Started deployment for delete test: %s", deployment_id)

        # -- Wait for success --
        final = wait_for_deployment(deployment_id)
        assert final["status"] == "succeeded", f"Deployment failed: {final.get('error_details', final)}"

        runtime_id = final["runtime_id"]
        # Do NOT add to deployment_cleanup — we're testing the delete ourselves.

        # -- Delete --
        delete_resp = api_session.delete(
            f"{base}/api/runtime/{runtime_id}",
            timeout=180,
        )
        delete_resp.raise_for_status()
        delete_body = delete_resp.json()

        assert delete_body.get("success") is True, f"Runtime deletion failed: {delete_body}"
        assert delete_body.get("message"), "Delete response must include a cleanup summary message"
        logger.info("Delete summary: %s", delete_body["message"])

    def test_full_lifecycle_deploy_poll_invoke_delete(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        """Full lifecycle: deploy → poll → invoke → delete in a single flow.

        This is the primary end-to-end lifecycle test that exercises every
        stage of the deployment state machine sequentially.

        Validates: Requirements 3.1, 3.5, 3.6, 3.7, 9.1, 9.3
        """
        base = api_session.base_url

        # ---- 1. Deploy ----
        config = _build_strands_config()
        config["name"] = "integ-full-lifecycle"
        payload = {
            "nodeId": "lifecycle-full-node",
            "config": config,
        }
        deploy_resp = api_session.post(
            f"{base}/api/deploy",
            json=payload,
            timeout=60,
        )
        assert deploy_resp.status_code == 202, f"Expected 202, got {deploy_resp.status_code}: {deploy_resp.text}"
        deploy_body = deploy_resp.json()
        deployment_id = deploy_body["deploymentId"]
        assert deploy_body["status"] == "pending"
        logger.info("Full lifecycle — deploy started: %s", deployment_id)

        # ---- 2. Poll until terminal state ----
        final = wait_for_deployment(deployment_id)
        assert final["status"] == "succeeded", f"Deployment did not succeed: {final.get('error_details', final)}"

        runtime_id = final["runtime_id"]
        runtime_endpoint = final["runtime_endpoint"]
        assert runtime_id
        assert runtime_endpoint

        # Register cleanup as a safety net
        deployment_cleanup.append({"runtime_id": runtime_id})

        # Verify completed_at is populated on success
        assert final.get("completed_at") or final.get("started_at"), (
            "Succeeded deployment should have timing information"
        )

        # ---- 3. Invoke the runtime ----
        invoke_payload = {
            "endpoint": runtime_endpoint,
            "input": "Say hello in exactly three words.",
            "runtimeId": runtime_id,
        }
        invoke_resp = api_session.post(
            f"{base}/api/test-runtime",
            json=invoke_payload,
            timeout=120,
        )
        invoke_resp.raise_for_status()
        invoke_body = invoke_resp.json()
        assert invoke_body.get("success") is True, f"Invocation failed: {invoke_body.get('error', invoke_body)}"
        assert invoke_body.get("response"), "Response must not be empty"
        logger.info("Full lifecycle — invoke response: %.200s", invoke_body["response"])

        # ---- 4. Delete the runtime ----
        delete_resp = api_session.delete(
            f"{base}/api/runtime/{runtime_id}",
            timeout=180,
        )
        delete_resp.raise_for_status()
        delete_body = delete_resp.json()
        assert delete_body.get("success") is True, f"Deletion failed: {delete_body}"
        assert delete_body.get("message"), "Delete must return a summary message"
        logger.info("Full lifecycle — cleanup done: %s", delete_body["message"])
