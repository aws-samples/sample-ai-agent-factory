# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the AgentCore Gateway target synchronization service."""

import json
from unittest.mock import MagicMock, patch

import pytest

from services.gateway_sync import GatewaySyncService


@pytest.fixture
def sync_service():
    with patch("services.gateway_sync.boto3") as mock_boto:
        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        service = GatewaySyncService(region="us-west-2")
        service._mock_client = mock_client
        yield service


@pytest.fixture
def sample_lambda_server():
    return {
        "server_name": "search-knowledge-base",
        "name": "search-knowledge-base",
        "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:search-kb",
        "tags": "lambda,agentcore-target",
        "tool_list": [{"name": "search_knowledge_base", "inputSchema": {"type": "object"}}],
    }


@pytest.fixture
def sample_http_server():
    return {
        "server_name": "weather-api",
        "name": "weather-api",
        "proxy_pass_url": "https://api.weather.example.com/v1/current",
        "tags": "openapi,agentcore-target",
        "tool_list": [{"name": "get_weather", "inputSchema": {"type": "object"}}],
    }


@pytest.fixture
def sample_internal_server():
    return {
        "server_name": "currenttime",
        "name": "currenttime",
        # in-cluster HTTP intentional — private Docker service hostname, never leaves the VPC
        "proxy_pass_url": "http://currenttime-server:8000",
        "path": "/currenttime",
        "tags": "mcp",
    }


class TestBuildTargetConfig:

    def test_lambda_target_config(self, sync_service, sample_lambda_server):
        config = sync_service.build_target_config(sample_lambda_server)
        assert config["name"] == "search-knowledge-base"
        conn = config["targetConfiguration"]["mcp"]["lambda"]
        assert conn["lambdaArn"] == "arn:aws:lambda:us-west-2:123456789012:function:search-kb"
        assert len(conn["toolSchema"]["inlinePayload"]) == 1

    def test_http_target_config(self, sync_service, sample_http_server):
        config = sync_service.build_target_config(sample_http_server)
        assert config["name"] == "weather-api"
        assert config["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == "https://api.weather.example.com/v1/current"

    def test_invalid_lambda_arn_rejected(self, sync_service):
        server = {"name": "bad", "proxy_pass_url": "lambda://not-an-arn"}
        with pytest.raises(ValueError, match="Invalid Lambda ARN"):
            sync_service.build_target_config(server)

    def test_localhost_url_rejected(self, sync_service):
        server = {"name": "bad", "proxy_pass_url": "http://localhost:8080/api"}
        with pytest.raises(ValueError, match="Blocked URL"):
            sync_service.build_target_config(server)

    def test_metadata_ip_url_rejected(self, sync_service):
        server = {"name": "bad", "proxy_pass_url": "http://169.254.169.254/latest/meta-data"}
        with pytest.raises(ValueError, match="Blocked URL"):
            sync_service.build_target_config(server)

    def test_unknown_type_returns_none(self, sync_service):
        server = {"name": "bad", "proxy_pass_url": "grpc://somewhere"}
        assert sync_service.build_target_config(server) is None


class TestBuildTargetConfigNginx:

    def test_internal_server_routed_via_cloudfront(self, sync_service, sample_internal_server):
        config = sync_service.build_target_config_for_nginx(
            sample_internal_server, "https://d123.cloudfront.net"
        )
        assert config["name"] == "currenttime"
        assert config["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == "https://d123.cloudfront.net/mcp/currenttime"


class TestCreateTarget:

    def test_create_success(self, sync_service, sample_lambda_server):
        sync_service._mock_client.create_gateway_target.return_value = {"targetId": "target-123"}
        config = sync_service.build_target_config(sample_lambda_server)
        result = sync_service.create_target("gw-001", config)
        assert result == "target-123"

    def test_conflict_returns_existing(self, sync_service, sample_lambda_server):
        from botocore.exceptions import ClientError
        sync_service._mock_client.create_gateway_target.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "Already exists"}},
            "CreateGatewayTarget",
        )
        config = sync_service.build_target_config(sample_lambda_server)
        result = sync_service.create_target("gw-001", config)
        assert result == "existing"

    def test_api_error_returns_none(self, sync_service, sample_lambda_server):
        from botocore.exceptions import ClientError
        sync_service._mock_client.create_gateway_target.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Bad"}},
            "CreateGatewayTarget",
        )
        config = sync_service.build_target_config(sample_lambda_server)
        result = sync_service.create_target("gw-001", config)
        assert result is None


class TestDeleteTarget:

    def test_delete_success(self, sync_service):
        sync_service._mock_client.delete_gateway_target.return_value = {}
        assert sync_service.delete_target("gw-001", "target-123", "my_tool") is True

    def test_delete_already_gone(self, sync_service):
        from botocore.exceptions import ClientError
        sync_service._mock_client.delete_gateway_target.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "DeleteGatewayTarget",
        )
        assert sync_service.delete_target("gw-001", "target-gone") is True

    def test_delete_error(self, sync_service):
        from botocore.exceptions import ClientError
        sync_service._mock_client.delete_gateway_target.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Oops"}},
            "DeleteGatewayTarget",
        )
        assert sync_service.delete_target("gw-001", "target-err") is False
