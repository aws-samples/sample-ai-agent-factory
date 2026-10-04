"""The exact Lambda-function name prefixes this stack may create, mutate and tag (F-7d).

Every tool Lambda the backend creates for a gateway is named
``AgentCore-<token>-<kind>[-<suffix>]`` where ``token = sha256("{project}-{env}-{region}")[:10]``
(backend ``services/naming.py``: ``stack_scope_token`` / ``scoped_function_name``). The token is
computed here from the same three values, so the gateway-step role can be granted
Create/Update/Tag/AddPermission on exactly its own prefix instead of ``function:AgentCore*``,
which reached every function in the account whose name happened to start with AgentCore --
including the foreign ones this account demonstrably holds.

The region in the token is the TARGET region of the deploy, not the platform's home region:
the same stack deploys canvases into every region in ``SUPPORTED_TARGET_REGIONS`` (F-41), so
the grant enumerates one prefix per region. ``tests/test_tool_lambda_name_parity.py`` pins
this list and the token formula against the backend's.
"""

from __future__ import annotations

import hashlib

import aws_cdk as cdk

from .config import PlatformConfig

FUNCTION_NAME_PREFIX = "AgentCore"
STACK_SCOPE_TOKEN_LEN = 10

#: Mirror of ``backend/src/app/services/deployment.py::VALID_AWS_REGIONS``. Pinned by parity test.
SUPPORTED_TARGET_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-north-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "sa-east-1",
    "ca-central-1",
)


def stack_scope_token(project: str, env: str, region: str) -> str:
    """Same formula as the backend's ``stack_scope_token(stack_id(region))``."""
    return hashlib.sha256(f"{project}-{env}-{region}".encode()).hexdigest()[:STACK_SCOPE_TOKEN_LEN]


def function_name_prefix(project: str, env: str, region: str) -> str:
    return f"{FUNCTION_NAME_PREFIX}-{stack_scope_token(project, env, region)}-"


def tool_function_arn_patterns(stack: cdk.Stack, cfg: PlatformConfig) -> list[str]:
    """One ``function:AgentCore-<token>-*`` ARN pattern per supported target region.

    Region-exact on BOTH sides of the ARN: the function that carries region R's token lives
    in region R, so the pattern is ``arn:aws:lambda:R:<account>:function:AgentCore-<token_R>-*``,
    never ``arn:aws:lambda:*:...``. A token for region R granted in region S would be
    authority over a same-account name this platform never creates there -- unnecessary,
    so not granted (peer a3). The account field is exact. Every supported target region is
    enumerated, so a non-home deploy (F-41) is reachable without any wildcard;
    ``tests/test_same_account_non_home_region_iam.py`` pins exactly that set, region for
    region, against the token each ARN carries.
    """
    return [
        f"arn:aws:lambda:{region}:{stack.account}:function:{function_name_prefix(cfg.project, cfg.env, region)}*"
        for region in SUPPORTED_TARGET_REGIONS
    ]


def tool_function_log_group_arn_patterns(stack: cdk.Stack, cfg: PlatformConfig) -> list[str]:
    """The log groups of exactly the functions :func:`tool_function_arn_patterns` covers.

    Lambda writes a function's logs to ``/aws/lambda/<function name>`` (no tool function sets
    a ``LoggingConfig``), so this is the same name prefix under that path, region-exact for
    the same reason: the token carries the region the function lives in.
    """
    return [
        f"arn:aws:logs:{region}:{stack.account}:log-group:/aws/lambda/"
        f"{function_name_prefix(cfg.project, cfg.env, region)}*"
        for region in SUPPORTED_TARGET_REGIONS
    ]
