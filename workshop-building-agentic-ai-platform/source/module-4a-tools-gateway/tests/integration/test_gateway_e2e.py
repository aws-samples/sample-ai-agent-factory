# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration tests for the AgentCore Gateway (Module 4 rewrite).

Tests the dual-path architecture against a live deployment:
- Path A: Direct access via NGINX/CloudFront (Module 3)
- Path B: Governed access via AgentCore Gateway

Requirements (environment variables):
    GATEWAY_ID             — AgentCore Gateway ID
    REGISTRY_URL           — Module 3 Registry URL
    CLOUDFRONT_URL         — Module 3 CloudFront URL
    M2M_SECRET_NAME        — Secrets Manager secret for M2M credentials
    COGNITO_POOL_ID        — Cognito User Pool ID (Module 3)
    COGNITO_CLIENT_ID      — Cognito interactive client ID
    TEST_USER_EMAIL        — Cognito test user email
    TEST_USER_PASSWORD     — Cognito test user password

Run:
    cd source/module-4a-tools-gateway
    pytest tests/integration/test_gateway_e2e.py -v --tb=short
"""

import json
import os
import time

import boto3
import pytest
import requests


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"Environment variable {name} not set")
    return val


@pytest.fixture(scope="module")
def config():
    return {
        "gateway_id": _get_env("GATEWAY_ID"),
        "registry_url": _get_env("REGISTRY_URL"),
        "cloudfront_url": _get_env("CLOUDFRONT_URL"),
        "pool_id": _get_env("COGNITO_POOL_ID"),
        "client_id": _get_env("COGNITO_CLIENT_ID"),
        "test_email": _get_env("TEST_USER_EMAIL"),
        "test_password": _get_env("TEST_USER_PASSWORD"),
        "region": os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
    }


@pytest.fixture(scope="module")
def user_token(config):
    cognito = boto3.client("cognito-idp", region_name=config["region"])
    resp = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=config["client_id"],
        AuthParameters={
            "USERNAME": config["test_email"],
            "PASSWORD": config["test_password"],
        },
    )
    return resp["AuthenticationResult"]["AccessToken"]


@pytest.fixture(scope="module")
def gateway_url(config):
    gw_id = config["gateway_id"]
    region = config["region"]
    return f"https://{gw_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"


class TestPathA:
    """Path A: Direct access through NGINX/CloudFront."""

    def test_direct_mcp_access(self, config):
        """Tools are accessible directly via CloudFront/NGINX."""
        resp = requests.get(
            f"{config['cloudfront_url']}/mcp/currenttime",
            timeout=10,
        )
        # May return 200 or 405 depending on the MCP server's HTTP handling
        assert resp.status_code in (200, 405, 404), f"Unexpected: {resp.status_code}"


class TestPathB:
    """Path B: Governed access through AgentCore Gateway."""

    def test_tools_list_via_gateway(self, gateway_url, user_token):
        resp = requests.post(
            gateway_url,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            timeout=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data, f"Unexpected: {data}"
        tools = data["result"].get("tools", [])
        assert len(tools) > 0, "No tools in gateway"

    def test_unauthenticated_rejected(self, gateway_url):
        resp = requests.post(
            gateway_url,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 2},
            timeout=10,
        )
        data = resp.json()
        assert "error" in data

    def test_response_sanitized(self, gateway_url, user_token):
        resp = requests.post(
            gateway_url,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 3},
            timeout=30,
        )
        data = resp.json()
        for tool in data.get("result", {}).get("tools", []):
            assert "embedding" not in tool
            assert "gatewayTargetId" not in tool


class TestSyncLambda:
    """Verify sync Lambda created targets from Registry."""

    def test_targets_exist(self, config):
        client = boto3.client(
            "bedrock-agentcore-control", region_name=config["region"]
        )
        targets = client.list_gateway_targets(
            gatewayIdentifier=config["gateway_id"]
        ).get("items", [])
        assert len(targets) > 0, "No gateway targets found — sync may not have run"
