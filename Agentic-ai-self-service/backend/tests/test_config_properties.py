"""Property-based tests for the configuration loader.

These tests verify that the configuration loader consistently returns
the exact values stored in SSM Parameter Store (deployed mode) or
environment variables (local mode).

Requirements: 3.3, 5.2
"""

import sys

sys.path.insert(0, "src")

import os

import boto3
from hypothesis import given, settings, strategies as st
from moto import mock_aws

from app.services.config import (
    SSM_PREFIX,
    SSM_PARAM_SUFFIXES,
    ENV_VAR_NAMES,
    load_config,
)


# ============================================================================
# Constants
# ============================================================================

REGION = "us-east-1"


# ============================================================================
# Hypothesis Strategies
# ============================================================================

# Strategy for valid AWS region strings
valid_aws_region_st = st.sampled_from(
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-central-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
    ]
)

# Strategy for valid CORS origin URLs
valid_origin_st = st.from_regex(
    r"^https?://[a-z0-9][a-z0-9\-]{0,20}\.[a-z]{2,6}(:\d{2,5})?$",
    fullmatch=True,
)

# Strategy for a comma-separated list of 1-3 CORS origins
valid_cors_origins_st = st.lists(valid_origin_st, min_size=1, max_size=3).map(lambda origins: ",".join(origins))

# Strategy for valid DynamoDB table names
valid_table_name_st = st.from_regex(
    r"^[a-zA-Z][a-zA-Z0-9\-]{2,30}$",
    fullmatch=True,
)

# Strategy for non-local environment names
valid_env_name_st = st.sampled_from(["dev", "staging", "prod"])


@st.composite
def ssm_config_st(draw):
    """Generate a complete set of config values for SSM mode."""
    return {
        "environment": draw(valid_env_name_st),
        "aws_region": draw(valid_aws_region_st),
        "cors_origins": draw(valid_cors_origins_st),
        "dynamodb_table_name": draw(valid_table_name_st),
    }


@st.composite
def env_config_st(draw):
    """Generate a complete set of config values for local/env-var mode."""
    return {
        "aws_region": draw(valid_aws_region_st),
        "cors_origins": draw(valid_cors_origins_st),
        "dynamodb_table_name": draw(valid_table_name_st),
    }


# ============================================================================
# Helpers
# ============================================================================


def _put_ssm_params(env: str, config_values: dict):
    """Store config values in mocked SSM Parameter Store."""
    client = boto3.client("ssm", region_name=REGION)
    prefix = SSM_PREFIX.format(env=env)

    for key, suffix in SSM_PARAM_SUFFIXES.items():
        value = config_values.get(key)
        if value is not None:
            client.put_parameter(
                Name=f"{prefix}/{suffix}",
                Value=value,
                Type="String",
                Overwrite=True,
            )


def _set_env_for_ssm(env: str):
    """Set minimal environment variables needed for SSM mode."""
    os.environ["ENVIRONMENT"] = env
    os.environ["AWS_REGION"] = REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = REGION


def _clear_config_env_vars():
    """Remove all config-related environment variables."""
    for var in list(ENV_VAR_NAMES.values()) + [
        "ENVIRONMENT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    ]:
        os.environ.pop(var, None)


# ============================================================================
# Property 5: Configuration Loader Consistency
# Validates: Requirements 3.3, 5.2
# ============================================================================


class TestConfigLoaderConsistency:
    """Property 5: Configuration Loader Consistency.

    For any set of configuration key-value pairs stored in SSM Parameter
    Store (or environment variables in local mode), the configuration
    loader SHALL return the exact values for each key.

    **Validates: Requirements 3.3, 5.2**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_config_loader_ssm_consistency(self, data):
        """Config loader returns exact values stored in SSM Parameter Store."""
        config_values = data.draw(ssm_config_st())
        env = config_values["environment"]

        saved_env = os.environ.copy()
        try:
            _clear_config_env_vars()

            with mock_aws():
                _set_env_for_ssm(env)
                _put_ssm_params(env, config_values)

                config = load_config()

                # Verify each value matches exactly what was stored
                assert config.aws_region == config_values["aws_region"]
                assert config.dynamodb_table_name == config_values["dynamodb_table_name"]
                assert config.environment == env

                # CORS origins are parsed from comma-separated string to list
                expected_origins = [o.strip() for o in config_values["cors_origins"].split(",") if o.strip()]
                assert config.cors_origins == expected_origins
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_config_loader_env_var_consistency(self, data):
        """Config loader returns exact values from environment variables in local mode."""
        config_values = data.draw(env_config_st())

        saved_env = os.environ.copy()
        try:
            _clear_config_env_vars()

            os.environ["ENVIRONMENT"] = "local"
            os.environ["AWS_REGION"] = config_values["aws_region"]
            os.environ["CORS_ORIGINS"] = config_values["cors_origins"]
            os.environ["DYNAMODB_TABLE_NAME"] = config_values["dynamodb_table_name"]

            config = load_config()

            # Verify each value matches exactly what was set
            assert config.aws_region == config_values["aws_region"]
            assert config.dynamodb_table_name == config_values["dynamodb_table_name"]
            assert config.environment == "local"

            # CORS origins are parsed from comma-separated string to list
            expected_origins = [o.strip() for o in config_values["cors_origins"].split(",") if o.strip()]
            assert config.cors_origins == expected_origins
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
