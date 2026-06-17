"""Integration tests for deploying and invoking the 3 built-in templates.

Each test performs real AWS API calls through the deployed API Gateway:
  1. POST /api/deploy with template-specific configuration
  2. Poll GET /api/deploy/{deployment_id} until terminal state
  3. POST /api/test-runtime to invoke the deployed agent
  4. DELETE /api/runtime/{runtime_id} to clean up

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

import logging

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_runtime_config(name: str, framework: str) -> dict:
    """Build a minimal RuntimeConfig payload with camelCase aliases."""
    return {
        "name": name,
        "entrypoint": "agent.py",
        "framework": framework,
        "model": {"modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
        "systemPrompt": "You are a helpful assistant.",
        "deploymentType": "agentcore",
        "pythonRuntime": "python3.13",
        "protocol": "HTTP",
        "idleTimeout": 900,
        "maxLifetime": 28800,
        "enableOtel": True,
    }


def _deploy_template(
    api_session,
    template_id: str,
    runtime_config: dict,
    *,
    connected_tools: list | None = None,
    gateway_config: dict | None = None,
    gateway_tools: list | None = None,
) -> dict:
    """POST /api/deploy and return the parsed JSON response.

    Raises ``AssertionError`` if the response status is not 202.
    """
    base = api_session.base_url
    payload = {
        "nodeId": "integration-test-node",
        "config": runtime_config,
        "templateId": template_id,
        "connectedTools": connected_tools,
        "gatewayConfig": gateway_config,
        "gatewayTools": gateway_tools,
    }
    resp = api_session.post(f"{base}/api/deploy", json=payload, timeout=60)
    assert resp.status_code == 202, f"Expected 202 from POST /api/deploy, got {resp.status_code}: {resp.text}"
    return resp.json()


def _invoke_runtime(api_session, runtime_id: str, endpoint: str, query: str) -> dict:
    """POST /api/test-runtime and return the parsed JSON response."""
    base = api_session.base_url
    payload = {
        "endpoint": endpoint,
        "input": query,
        "runtimeId": runtime_id,
    }
    resp = api_session.post(f"{base}/api/test-runtime", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _delete_runtime(api_session, runtime_id: str) -> dict:
    """DELETE /api/runtime/{runtime_id} and return the parsed JSON response."""
    base = api_session.base_url
    resp = api_session.delete(f"{base}/api/runtime/{runtime_id}", timeout=180)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Test: LangGraph Web Search template
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLangGraphWebSearch:
    """Deploy, invoke, and clean up the LangGraph Web Search template.

    Validates: Requirements 10.1, 10.4, 10.5, 10.6
    """

    def test_deploy_invoke_cleanup(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        # -- Deploy --
        config = _build_runtime_config(
            name="integ-langgraph-web-search",
            framework="langgraph",
        )
        deploy_resp = _deploy_template(
            api_session,
            template_id="langchain-web-search",
            runtime_config=config,
        )
        deployment_id = deploy_resp["deploymentId"]
        assert deployment_id, "deploymentId must be present in deploy response"
        logger.info("Started LangGraph deployment: %s", deployment_id)

        # -- Poll until terminal state --
        status = wait_for_deployment(deployment_id)
        assert status["status"] == "succeeded", f"LangGraph deployment failed: {status.get('error_details', status)}"

        runtime_id = status.get("runtime_id")
        runtime_endpoint = status.get("runtime_endpoint")
        assert runtime_id, "runtime_id must be present after successful deployment"
        assert runtime_endpoint, "runtime_endpoint must be present after successful deployment"

        # Track for cleanup (runs even if assertions below fail)
        deployment_cleanup.append({"runtime_id": runtime_id})

        # -- Invoke --
        invoke_resp = _invoke_runtime(
            api_session,
            runtime_id=runtime_id,
            endpoint=runtime_endpoint,
            query="What is the capital of France?",
        )
        assert invoke_resp.get("success") is True, f"Runtime invocation failed: {invoke_resp.get('error', invoke_resp)}"
        assert invoke_resp.get("response"), "Invocation response must not be empty"
        logger.info("LangGraph invocation succeeded: %.200s", invoke_resp["response"])

        # -- Delete --
        delete_resp = _delete_runtime(api_session, runtime_id)
        assert delete_resp.get("success") is True, f"Runtime deletion failed: {delete_resp}"
        logger.info("LangGraph cleanup completed")


# ---------------------------------------------------------------------------
# Test: Strands + Gateway template
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStrandsGateway:
    """Deploy, invoke, and clean up the Strands + Gateway template.

    Validates: Requirements 10.2, 10.4, 10.5, 10.6
    """

    def test_deploy_invoke_cleanup(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        # -- Deploy --
        config = _build_runtime_config(
            name="integ-strands-gateway",
            framework="strands-agents",
        )
        gateway_config = {
            "enabled": True,
            "name": "integ-strands-gw",
        }
        deploy_resp = _deploy_template(
            api_session,
            template_id="strands-gateway-agent",
            runtime_config=config,
            gateway_config=gateway_config,
        )
        deployment_id = deploy_resp["deploymentId"]
        assert deployment_id, "deploymentId must be present in deploy response"
        logger.info("Started Strands+Gateway deployment: %s", deployment_id)

        # -- Poll until terminal state --
        status = wait_for_deployment(deployment_id)
        assert status["status"] == "succeeded", (
            f"Strands+Gateway deployment failed: {status.get('error_details', status)}"
        )

        runtime_id = status.get("runtime_id")
        runtime_endpoint = status.get("runtime_endpoint")
        assert runtime_id, "runtime_id must be present after successful deployment"
        assert runtime_endpoint, "runtime_endpoint must be present after successful deployment"

        # Track for cleanup (runs even if assertions below fail)
        deployment_cleanup.append(
            {
                "runtime_id": runtime_id,
                "gateway_config": gateway_config,
            }
        )

        # -- Invoke --
        invoke_resp = _invoke_runtime(
            api_session,
            runtime_id=runtime_id,
            endpoint=runtime_endpoint,
            query="Summarize the latest AI news.",
        )
        assert invoke_resp.get("success") is True, f"Runtime invocation failed: {invoke_resp.get('error', invoke_resp)}"
        assert invoke_resp.get("response"), "Invocation response must not be empty"
        logger.info("Strands+Gateway invocation succeeded: %.200s", invoke_resp["response"])

        # -- Delete --
        delete_resp = _delete_runtime(api_session, runtime_id)
        assert delete_resp.get("success") is True, f"Runtime deletion failed: {delete_resp}"
        logger.info("Strands+Gateway cleanup completed")


# ---------------------------------------------------------------------------
# Test: Customer Support Assistant template
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCustomerSupportAssistant:
    """Deploy, invoke, and clean up the Customer Support Assistant template.

    Validates: Requirements 10.3, 10.4, 10.5, 10.6
    """

    def test_deploy_invoke_cleanup(
        self,
        api_session,
        deployment_cleanup,
        wait_for_deployment,
    ):
        # -- Deploy --
        config = _build_runtime_config(
            name="integ-customer-support",
            framework="strands-agents",
        )
        config["systemPrompt"] = (
            "You are a customer support assistant. Help users with their questions "
            "about products, orders, and account issues."
        )
        gateway_config = {
            "enabled": True,
            "name": "integ-support-gw",
        }
        deploy_resp = _deploy_template(
            api_session,
            template_id="customer-support-assistant",
            runtime_config=config,
            gateway_config=gateway_config,
        )
        deployment_id = deploy_resp["deploymentId"]
        assert deployment_id, "deploymentId must be present in deploy response"
        logger.info("Started Customer Support deployment: %s", deployment_id)

        # -- Poll until terminal state --
        status = wait_for_deployment(deployment_id)
        assert status["status"] == "succeeded", (
            f"Customer Support deployment failed: {status.get('error_details', status)}"
        )

        runtime_id = status.get("runtime_id")
        runtime_endpoint = status.get("runtime_endpoint")
        assert runtime_id, "runtime_id must be present after successful deployment"
        assert runtime_endpoint, "runtime_endpoint must be present after successful deployment"

        # Track for cleanup (runs even if assertions below fail)
        deployment_cleanup.append(
            {
                "runtime_id": runtime_id,
                "gateway_config": gateway_config,
            }
        )

        # -- Invoke --
        invoke_resp = _invoke_runtime(
            api_session,
            runtime_id=runtime_id,
            endpoint=runtime_endpoint,
            query="I need help with my recent order. It hasn't arrived yet.",
        )
        assert invoke_resp.get("success") is True, f"Runtime invocation failed: {invoke_resp.get('error', invoke_resp)}"
        assert invoke_resp.get("response"), "Invocation response must not be empty"
        logger.info("Customer Support invocation succeeded: %.200s", invoke_resp["response"])

        # -- Delete --
        delete_resp = _delete_runtime(api_session, runtime_id)
        assert delete_resp.get("success") is True, f"Runtime deletion failed: {delete_resp}"
        logger.info("Customer Support cleanup completed")
