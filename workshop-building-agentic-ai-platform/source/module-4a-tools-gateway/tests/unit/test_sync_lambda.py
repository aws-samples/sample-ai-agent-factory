# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the Sync Lambda handler (Registry API → Gateway targets)."""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def env_setup():
    os.environ["GATEWAY_ID"] = "gw-test-001"
    os.environ["REGISTRY_URL"] = "https://registry.example.com"
    os.environ["M2M_SECRET_NAME"] = "workshop-registry-api-token"  # nosec B105
    os.environ["CLOUDFRONT_URL"] = "https://d123.cloudfront.net"
    os.environ["AWS_REGION"] = "us-west-2"
    yield
    for key in ["GATEWAY_ID", "REGISTRY_URL", "M2M_SECRET_NAME", "CLOUDFRONT_URL", "AWS_REGION"]:
        os.environ.pop(key, None)


class TestSyncLambdaHandler:

    @patch("services.gateway_sync.GatewaySyncService")
    @patch("services.registry_client.RegistryClient")
    def test_sync_creates_targets(self, mock_registry_cls, mock_sync_cls):
        import importlib
        import handlers.sync_lambda as mod
        importlib.reload(mod)

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = [
            {"server_name": "tool-a", "name": "tool-a",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:tool-a",
             "tags": "lambda"},
        ]
        mock_registry_cls.return_value = mock_registry

        mock_sync = MagicMock()
        mock_sync.list_targets.return_value = []
        mock_sync.build_target_config.return_value = {
            "name": "tool-a",
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:us-west-2:123456789012:function:tool-a"}}},
        }
        mock_sync.create_target.return_value = "target-new"
        mock_sync_cls.return_value = mock_sync

        result = mod.handler({}, None)

        assert result["created"] == 1
        assert result["errors"] == 0
        mock_sync.create_target.assert_called_once()

    @patch("services.gateway_sync.GatewaySyncService")
    @patch("services.registry_client.RegistryClient")
    def test_sync_skips_existing_targets(self, mock_registry_cls, mock_sync_cls):
        import importlib
        import handlers.sync_lambda as mod
        importlib.reload(mod)

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = [
            {"server_name": "tool-a", "name": "tool-a",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:tool-a"},
        ]
        mock_registry_cls.return_value = mock_registry

        mock_sync = MagicMock()
        mock_sync.list_targets.return_value = [{"name": "tg-tool-a"}]
        mock_sync_cls.return_value = mock_sync

        result = mod.handler({}, None)

        assert result["skipped"] == 1
        assert result["created"] == 0
        mock_sync.create_target.assert_not_called()

    @patch("services.gateway_sync.GatewaySyncService")
    @patch("services.registry_client.RegistryClient")
    def test_sync_skips_http_servers_for_path_a(self, mock_registry_cls, mock_sync_cls):
        import importlib
        import handlers.sync_lambda as mod
        importlib.reload(mod)

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = [
            {"server_name": "http-tool", "name": "http-tool",
             "proxy_pass_url": "https://api.example.com/tool"},
        ]
        mock_registry_cls.return_value = mock_registry

        mock_sync = MagicMock()
        mock_sync.list_targets.return_value = []
        mock_sync_cls.return_value = mock_sync

        result = mod.handler({}, None)

        assert result["skipped"] == 1
        assert result["created"] == 0
        mock_sync.create_target.assert_not_called()

    @patch("services.gateway_sync.GatewaySyncService")
    @patch("services.registry_client.RegistryClient")
    def test_sync_filters_by_tags(self, mock_registry_cls, mock_sync_cls):
        import importlib
        import handlers.sync_lambda as mod

        os.environ["SYNC_FILTER_TAGS"] = "agentcore-target"
        importlib.reload(mod)

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = [
            {"server_name": "tagged-tool", "name": "tagged-tool",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:tagged-tool",
             "tags": "agentcore-target,lambda"},
            {"server_name": "untagged-tool", "name": "untagged-tool",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:untagged-tool",
             "tags": "lambda"},
        ]
        mock_registry_cls.return_value = mock_registry

        mock_sync = MagicMock()
        mock_sync.list_targets.return_value = []
        mock_sync.build_target_config.return_value = {
            "name": "tagged-tool",
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:us-west-2:123456789012:function:tagged-tool"}}},
        }
        mock_sync.create_target.return_value = "target-new"
        mock_sync_cls.return_value = mock_sync

        result = mod.handler({}, None)

        assert result["created"] == 1
        assert result["filtered"] == 1  # untagged-tool filtered by tag
        assert result["skipped"] == 0
        mock_sync.create_target.assert_called_once()

        os.environ.pop("SYNC_FILTER_TAGS", None)

    @patch("services.gateway_sync.GatewaySyncService")
    @patch("services.registry_client.RegistryClient")
    def test_sync_no_filter_syncs_all(self, mock_registry_cls, mock_sync_cls):
        import importlib
        import handlers.sync_lambda as mod

        os.environ["SYNC_FILTER_TAGS"] = ""
        importlib.reload(mod)

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = [
            {"server_name": "tool-a", "name": "tool-a",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:tool-a",
             "tags": "lambda"},
            {"server_name": "tool-b", "name": "tool-b",
             "proxy_pass_url": "lambda://arn:aws:lambda:us-west-2:123456789012:function:tool-b",
             "tags": "lambda"},
        ]
        mock_registry_cls.return_value = mock_registry

        mock_sync = MagicMock()
        mock_sync.list_targets.return_value = []
        mock_sync.build_target_config.return_value = {
            "name": "tool",
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn"}}},
        }
        mock_sync.create_target.return_value = "target-new"
        mock_sync_cls.return_value = mock_sync

        result = mod.handler({}, None)

        assert result["created"] == 2
        assert mock_sync.create_target.call_count == 2

    def test_missing_gateway_id_skips(self):
        os.environ["GATEWAY_ID"] = ""
        import importlib
        import handlers.sync_lambda as mod
        importlib.reload(mod)

        result = mod.handler({}, None)
        assert result["synced"] == 0
