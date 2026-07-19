"""Unit tests for the configuration loader.

Tests cover both local (env var) and deployed (SSM) modes,
including fallback behavior and CORS parsing.

Requirements: 3.3, 5.1, 5.2
"""

import os
from unittest.mock import patch

import boto3
import pytest
from app.services.config import (
    AppConfig,
    _build_ssm_parameter_path,
    _create_ssm_client,
    _get_ssm_parameter,
    _is_local_environment,
    _parse_cors_origins,
    load_config,
)
from moto import mock_aws

# ============================================================================
# Unit tests for helper functions
# ============================================================================


class TestIsLocalEnvironment:
    def test_local_string(self):
        assert _is_local_environment("local") is True

    def test_empty_string(self):
        assert _is_local_environment("") is True

    def test_dev_string(self):
        assert _is_local_environment("dev") is False

    def test_prod_string(self):
        assert _is_local_environment("prod") is False

    def test_case_insensitive(self):
        assert _is_local_environment("LOCAL") is True
        assert _is_local_environment("Local") is True


class TestBuildSsmParameterPath:
    def test_dev_cors(self):
        path = _build_ssm_parameter_path("dev", "cors-origins")
        assert path == "/agentcore-workflow/dev/cors-origins"

    def test_prod_region(self):
        path = _build_ssm_parameter_path("prod", "aws-region")
        assert path == "/agentcore-workflow/prod/aws-region"

    def test_dev_dynamodb(self):
        path = _build_ssm_parameter_path("dev", "dynamodb-table-name")
        assert path == "/agentcore-workflow/dev/dynamodb-table-name"


class TestParseCorsOrigins:
    def test_single_origin(self):
        assert _parse_cors_origins("http://localhost:5173") == ["http://localhost:5173"]

    def test_multiple_origins(self):
        result = _parse_cors_origins("http://a.com, http://b.com")
        assert result == ["http://a.com", "http://b.com"]

    def test_none_returns_empty(self):
        assert _parse_cors_origins(None) == []

    def test_empty_string_returns_empty(self):
        assert _parse_cors_origins("") == []

    def test_strips_whitespace(self):
        result = _parse_cors_origins("  http://a.com  ,  http://b.com  ")
        assert result == ["http://a.com", "http://b.com"]


# ============================================================================
# SSM wrapper tests (using moto)
# ============================================================================


class TestGetSsmParameter:
    @mock_aws
    def test_reads_existing_parameter(self):
        client = boto3.client("ssm", region_name="us-east-1")
        client.put_parameter(
            Name="/test/param",
            Value="test-value",
            Type="String",
        )
        ssm = _create_ssm_client("us-east-1")
        assert _get_ssm_parameter(ssm, "/test/param") == "test-value"

    @mock_aws
    def test_returns_none_for_missing_parameter(self):
        ssm = _create_ssm_client("us-east-1")
        assert _get_ssm_parameter(ssm, "/nonexistent/param") is None

    @mock_aws
    def test_reads_secure_string(self):
        client = boto3.client("ssm", region_name="us-east-1")
        client.put_parameter(
            Name="/test/secret",
            Value="secret-value",
            Type="SecureString",
        )
        ssm = _create_ssm_client("us-east-1")
        assert _get_ssm_parameter(ssm, "/test/secret") == "secret-value"


# ============================================================================
# Integration tests for load_config
# ============================================================================


class TestLoadConfigLocal:
    """Tests for load_config in local mode (env vars only)."""

    @patch.dict(
        os.environ,
        {
            "ENVIRONMENT": "local",
            "AWS_REGION": "us-west-2",
            "CORS_ORIGINS": "http://localhost:3000,http://localhost:5173",
            "DYNAMODB_TABLE_NAME": "my-table",
        },
        clear=False,
    )
    def test_reads_from_env_vars(self):
        config = load_config()
        assert config.environment == "local"
        assert config.aws_region == "us-west-2"
        assert config.cors_origins == ["http://localhost:3000", "http://localhost:5173"]
        assert config.dynamodb_table_name == "my-table"

    @patch.dict(os.environ, {"ENVIRONMENT": "local"}, clear=True)
    def test_uses_defaults_when_env_vars_missing(self):
        config = load_config()
        assert config.environment == "local"
        assert config.aws_region == "us-east-1"
        assert config.cors_origins == ["http://localhost:5173"]
        assert config.dynamodb_table_name is None

    @patch.dict(os.environ, {}, clear=True)
    def test_defaults_to_local_when_environment_not_set(self):
        config = load_config()
        assert config.environment == "local"


class TestLoadConfigDeployed:
    """Tests for load_config in deployed mode (SSM + env var fallback)."""

    @mock_aws
    @patch.dict(
        os.environ,
        {
            "ENVIRONMENT": "dev",
            "AWS_REGION": "us-east-1",
            # clear=True wipes the ambient AWS creds that let moto's SSM mock
            # intercept the call; without dummy creds the boto3 SSM client
            # escapes to REAL AWS and fails NoCredentialsError on a runner with
            # no credentials (CI). Supply fakes so moto stays in control.
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SESSION_TOKEN": "testing",
        },
        clear=True,
    )
    def test_reads_from_ssm(self):
        client = boto3.client("ssm", region_name="us-east-1")
        client.put_parameter(
            Name="/agentcore-workflow/dev/aws-region",
            Value="eu-west-1",
            Type="String",
        )
        client.put_parameter(
            Name="/agentcore-workflow/dev/cors-origins",
            Value="https://d123.cloudfront.net",
            Type="String",
        )
        client.put_parameter(
            Name="/agentcore-workflow/dev/dynamodb-table-name",
            Value="workflows-dev",
            Type="String",
        )

        config = load_config()
        assert config.environment == "dev"
        assert config.aws_region == "eu-west-1"
        assert config.cors_origins == ["https://d123.cloudfront.net"]
        assert config.dynamodb_table_name == "workflows-dev"

    @mock_aws
    @patch.dict(
        os.environ,
        {
            "ENVIRONMENT": "dev",
            "AWS_REGION": "us-east-1",
            "CORS_ORIGINS": "http://fallback.com",
            # See test_reads_from_ssm: fake creds keep moto in control after
            # clear=True (otherwise the SSM probe hits real AWS on CI).
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SESSION_TOKEN": "testing",
        },
        clear=True,
    )
    def test_falls_back_to_env_when_ssm_missing(self):
        # No SSM params set — should fall back to env vars
        config = load_config()
        assert config.cors_origins == ["http://fallback.com"]


class TestAppConfigImmutability:
    def test_frozen_dataclass(self):
        config = AppConfig(
            aws_region="us-east-1",
            cors_origins=["http://localhost"],
            environment="local",
        )
        with pytest.raises(AttributeError):
            config.aws_region = "eu-west-1"
