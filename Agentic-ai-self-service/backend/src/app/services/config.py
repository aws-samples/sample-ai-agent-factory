"""Application configuration loader.

Reads configuration from AWS SSM Parameter Store when deployed,
or from environment variables when running locally.

SSM Parameter paths:
    /agentcore-workflow/{env}/cors-origins
    /agentcore-workflow/{env}/aws-region
    /agentcore-workflow/{env}/dynamodb-table-name

When ENVIRONMENT env var is "local" or not set, SSM is skipped
and values are read directly from environment variables.

Requirements: 3.3, 5.1, 5.2
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# SSM parameter path prefix template
SSM_PREFIX = "/agentcore-workflow/{env}"

# Mapping of config keys to their SSM parameter suffixes
SSM_PARAM_SUFFIXES = {
    "aws_region": "aws-region",
    "cors_origins": "cors-origins",
    "dynamodb_table_name": "dynamodb-table-name",
    "dynamodb_flows_table_name": "dynamodb-flows-table-name",
}

# Mapping of config keys to their environment variable names
ENV_VAR_NAMES = {
    "aws_region": "AWS_REGION",
    "cors_origins": "CORS_ORIGINS",
    "dynamodb_table_name": "DYNAMODB_TABLE_NAME",
    "dynamodb_flows_table_name": "DYNAMODB_FLOWS_TABLE_NAME",
}

# Default values for optional config keys
DEFAULTS = {
    "aws_region": "us-east-1",
    "cors_origins": "http://localhost:5173",
    "dynamodb_table_name": None,
    "dynamodb_flows_table_name": None,
}


# ============================================================================
# Boto3 Wrapper Functions
# ============================================================================


def _create_ssm_client(region: str):
    """Create and return a boto3 SSM client.

    Wrapper around boto3.client('ssm') to centralize client
    creation and allow for easier testing.

    Args:
        region: AWS region name (e.g., 'us-east-1')

    Returns:
        boto3 SSM client
    """
    return boto3.client("ssm", region_name=region)


def _get_ssm_parameter(ssm_client, parameter_name: str) -> Optional[str]:
    """Read a single parameter value from SSM Parameter Store.

    Wrapper around ssm.get_parameter() that handles the
    WithDecryption flag and returns just the value string.

    Args:
        ssm_client: boto3 SSM client
        parameter_name: Full SSM parameter path

    Returns:
        The parameter value string, or None if not found
    """
    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True,
        )
        return response["Parameter"]["Value"]
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ParameterNotFound":
            logger.warning("SSM parameter not found: %s", parameter_name)
            return None
        raise


# ============================================================================
# Environment Helpers
# ============================================================================


def _read_environment_variable(var_name: str) -> Optional[str]:
    """Read a value from environment variables.

    Args:
        var_name: The environment variable name

    Returns:
        The value if set, None otherwise
    """
    return os.environ.get(var_name)


def _get_current_environment() -> str:
    """Determine the current environment from the ENVIRONMENT env var.

    Returns:
        The environment string, defaulting to "local" if not set
    """
    return os.environ.get("ENVIRONMENT", "local")


def _is_local_environment(environment: str) -> bool:
    """Check whether the environment is local (skip SSM).

    Args:
        environment: The environment string

    Returns:
        True if SSM should be skipped
    """
    return environment.lower() in ("local", "")


# ============================================================================
# SSM Parameter Path Helpers
# ============================================================================


def _build_ssm_parameter_path(env: str, suffix: str) -> str:
    """Build the full SSM parameter path for a config key.

    Args:
        env: Environment name (e.g., "dev", "prod")
        suffix: Parameter suffix (e.g., "cors-origins")

    Returns:
        Full SSM path like /agentcore-workflow/dev/cors-origins
    """
    prefix = SSM_PREFIX.format(env=env)
    return f"{prefix}/{suffix}"


# ============================================================================
# Config Value Resolution
# ============================================================================


def _resolve_config_value(
    key: str,
    environment: str,
    ssm_client,
) -> Optional[str]:
    """Resolve a single config value from SSM or environment variables.

    Strategy:
    1. If running locally, read from environment variables only
    2. If deployed, try SSM first, then fall back to environment variables

    Args:
        key: Config key name (e.g., "aws_region")
        environment: Current environment string
        ssm_client: boto3 SSM client (may be None for local)

    Returns:
        The resolved value string, or None if not found anywhere
    """
    env_var = ENV_VAR_NAMES.get(key)

    if _is_local_environment(environment):
        return _read_environment_variable(env_var) if env_var else None

    # Deployed: try SSM first
    suffix = SSM_PARAM_SUFFIXES.get(key)
    if suffix and ssm_client:
        ssm_path = _build_ssm_parameter_path(environment, suffix)
        value = _get_ssm_parameter(ssm_client, ssm_path)
        if value is not None:
            return value

    # Fall back to environment variable
    if env_var:
        return _read_environment_variable(env_var)

    return None


def _parse_cors_origins(raw: Optional[str]) -> list[str]:
    """Parse a comma-separated CORS origins string into a list.

    Args:
        raw: Comma-separated origins string, or None

    Returns:
        List of origin strings, stripped of whitespace
    """
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# ============================================================================
# AppConfig Data Class
# ============================================================================


@dataclass(frozen=True)
class AppConfig:
    """Application configuration.

    Holds all configuration values needed by the backend.
    Created once at startup via load_config().

    Attributes:
        aws_region: AWS region for service calls
        cors_origins: List of allowed CORS origin URLs
        dynamodb_table_name: DynamoDB table name (None for in-memory storage)
        environment: Environment name ("local", "dev", "prod")
    """

    aws_region: str
    cors_origins: list[str] = field(default_factory=list)
    dynamodb_table_name: Optional[str] = None
    dynamodb_flows_table_name: Optional[str] = None
    environment: str = "local"


# ============================================================================
# Config Loader
# ============================================================================


def load_config() -> AppConfig:
    """Load application configuration from SSM or environment variables.

    Determines the current environment, then resolves each config
    value from SSM Parameter Store (deployed) or environment
    variables (local). Falls back to sensible defaults.

    Returns:
        Populated AppConfig instance

    Requirements: 3.3, 5.1, 5.2
    """
    environment = _get_current_environment()
    logger.info("Loading config for environment: %s", environment)

    ssm_client = None
    if not _is_local_environment(environment):
        region_for_ssm = _read_environment_variable("AWS_REGION") or DEFAULTS["aws_region"]
        ssm_client = _create_ssm_client(region_for_ssm)

    aws_region = _resolve_config_value("aws_region", environment, ssm_client) or DEFAULTS["aws_region"]

    cors_raw = _resolve_config_value("cors_origins", environment, ssm_client) or DEFAULTS["cors_origins"]
    cors_origins = _parse_cors_origins(cors_raw)

    dynamodb_table_name = (
        _resolve_config_value("dynamodb_table_name", environment, ssm_client) or DEFAULTS["dynamodb_table_name"]
    )

    dynamodb_flows_table_name = (
        _resolve_config_value("dynamodb_flows_table_name", environment, ssm_client)
        or DEFAULTS["dynamodb_flows_table_name"]
    )

    config = AppConfig(
        aws_region=aws_region,
        cors_origins=cors_origins,
        dynamodb_table_name=dynamodb_table_name,
        dynamodb_flows_table_name=dynamodb_flows_table_name,
        environment=environment,
    )

    logger.info(
        "Config loaded: region=%s, environment=%s, dynamodb_table=%s, dynamodb_flows_table=%s, cors_origins=%s",
        config.aws_region,
        config.environment,
        config.dynamodb_table_name,
        config.dynamodb_flows_table_name,
        config.cors_origins,
    )
    return config
