#!/usr/bin/env python3
"""Cleanup-first AgentCore Gateway PolicyEngine compatibility spike.

This is a narrow, ephemeral Platform-account probe of Policy in Amazon Bedrock
AgentCore. It stands up a self-contained topology, proves Cedar enforcement
behaviour against a deterministic truth table, proves the documented
``ENFORCE -> LOG_ONLY -> ENFORCE`` rollback with behaviour twins, and then
removes everything it created.

What it creates (all prefix-owned, all ephemeral):

* a Cognito User Pool with two app clients, three groups (one allowed plus two
  collision decoys), and four users spanning the subject/group truth table;
* a deterministic echo Lambda exposing five MCP tools, two least-privilege
  roles, and a pre-created/tagged one-day log group;
* an AgentCore PolicyEngine with eight ``ACTIVE`` Cedar policies created under
  ``FAIL_ON_ANY_FINDINGS``;
* a ``CUSTOM_JWT`` MCP Gateway and Lambda target, associated with the engine in
  ``LOG_ONLY`` before policy creation and then moved to ``ENFORCE``.

AWS documents scalar JWT claims as Cedar tags but not the representation of the
array-valued ``cognito:groups`` claim. The spike therefore exercises one narrow
quoted-element candidate and succeeds only if exact group members are allowed
while prefix/suffix-collision groups are denied. Product code must not adopt the
candidate until that live proof passes.

Commands: ``deploy``, ``verify``, ``rollback``, ``cleanup``, ``all``. ``verify``
cannot reach a mutating API at all; ``rollback`` may reach exactly one, the
named gateway policy-engine mode transition. ``all`` runs cleanup in ``finally``.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import boto3
import botocore
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError
from botocore.httpsession import URLLib3Session

import policy_engine_model as model
from gateway_spike import (
    REQUIRED_BOTO3_VERSION,
    Evidence,
    JsonStore,
    SpikeError,
    aws_error_code,
    request_id,
    utc_now,
)

# --------------------------------------------------------------------------
# Mutation registry — the contract the reachability test enforces
# --------------------------------------------------------------------------

#: Every AWS call this module makes that changes state. A new mutating SDK call
#: must be added here or the conformance test fails.
MUTATING_API_CALLS = frozenset(
    {
        "create_role",
        "put_role_policy",
        "delete_role_policy",
        "delete_role",
        "create_function",
        "delete_function",
        "add_permission",
        "create_log_group",
        "put_retention_policy",
        "delete_log_group",
        "create_user_pool",
        "delete_user_pool",
        "create_user_pool_client",
        "delete_user_pool_client",
        "create_group",
        "delete_group",
        "admin_create_user",
        "admin_set_user_password",
        "admin_add_user_to_group",
        "create_policy_engine",
        "delete_policy_engine",
        "create_policy",
        "delete_policy",
        "create_gateway",
        "delete_gateway",
        "update_gateway",
        "create_gateway_target",
        "delete_gateway_target",
    }
)

#: Calls whose names look mutating but only read, so ``verify`` may use them.
READ_ONLY_EXCEPTIONS = frozenset({"admin_get_user", "admin_initiate_auth"})

#: The single mutating operation the rollback command is allowed to perform. The
#: pinned service model has NO SetPolicyEngineMode / AssociatePolicyEngine /
#: DisassociatePolicyEngine operation: association and mode transitions are
#: expressed through ``UpdateGateway.policyEngineConfiguration`` (B2). So the
#: real, and only, mutation the mode transition reaches is ``update_gateway``.
ROLLBACK_MUTATION = "update_gateway"

LAMBDA_RUNTIME = "python3.13"
LAMBDA_HANDLER = "index.handler"
LAMBDA_TIMEOUT_SECONDS = 10
LAMBDA_MEMORY_MB = 128
LOG_RETENTION_DAYS = 1
ACCESS_TOKEN_MINUTES = 5

GATEWAY_READY_STATES = frozenset({"READY"})
GATEWAY_FAILURES = frozenset(
    {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}
)
NOT_FOUND_CODES = frozenset(
    {"ResourceNotFoundException", "NotFoundException", "NoSuchEntity", "404"}
)

#: Error codes that signal "a dependency has not propagated yet" and are safe to
#: retry with the SAME idempotency token (B5). Anything not in this set is a real
#: failure and is re-raised immediately rather than being masked by a retry.
PROPAGATION_RETRY_CODES = frozenset(
    {
        "AccessDeniedException",
        "InternalServerException",
        "ThrottlingException",
        "ConflictException",
    }
)

#: Bounded propagation budget for a create that depends on a fresh IAM role
#: reaching the AgentCore/Lambda control plane (B5). At least five minutes, using
#: the same client token on every attempt so a retry can never create a second
#: resource. Replaces the old fixed 45-second ``time.sleep``.
PROPAGATION_TIMEOUT_SECONDS = 360
PROPAGATION_INTERVAL_SECONDS = 15

MODE_PROPAGATION_TIMEOUT = 300
MODE_PROPAGATION_INTERVAL = 10

#: Bounded budget for an asynchronous policy/engine delete to become absent (B4).
DELETE_ABSENT_TIMEOUT = 300
DELETE_ABSENT_INTERVAL = 5

#: Management-caller actions surfaced in the SimulatePrincipalPolicy check and in
#: the README (B9). Includes InvokeGateway (the data-plane entry the gateway role
#: is invoked through) and ManageResourceScopedPolicy (resource-scoped policy
#: writes; ManageAdminPolicy is deliberately excluded -- no wildcard policy).
MANAGEMENT_ACTIONS = (
    "bedrock-agentcore:CreatePolicyEngine",
    "bedrock-agentcore:CreatePolicy",
    "bedrock-agentcore:UpdatePolicy",
    "bedrock-agentcore:DeletePolicy",
    "bedrock-agentcore:DeletePolicyEngine",
    "bedrock-agentcore:ManageResourceScopedPolicy",
    "bedrock-agentcore:UpdateGateway",
    "bedrock-agentcore:InvokeGateway",
)

#: Policy-evaluation actions the gateway role needs, split by resource (B9):
#: GetPolicyEngine is scoped to the ENGINE only; the two authorize actions are
#: scoped to the ENGINE and the GATEWAY.
GATEWAY_AUTHZ_ENGINE_ACTIONS = ("bedrock-agentcore:GetPolicyEngine",)
GATEWAY_AUTHZ_EVAL_ACTIONS = (
    "bedrock-agentcore:AuthorizeAction",
    "bedrock-agentcore:PartiallyAuthorizeActions",
)

#: Inline Lambda source. Deterministic, dependency-free, and it never logs or
#: echoes an argument *value* — only the sorted argument field names — so no
#: tool input or output text can reach CloudWatch or the evidence file.
LAMBDA_SOURCE = f'''"""Ephemeral deterministic MCP echo tool for the PolicyEngine spike."""

ECHO_MARKER = "{model.ECHO_MARKER}"


def _tool_name(context):
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {{}}
    return str(custom.get("bedrockAgentCoreToolName", "unknown"))


def handler(event, context):
    fields = sorted(event) if isinstance(event, dict) else []
    return {{"marker": ECHO_MARKER, "tool": _tool_name(context), "argumentFields": fields}}
'''


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyEngineConfig:
    """Validated, immutable run configuration."""

    account_id: str
    region: str
    names: model.SpikeNames
    state_path: Path
    evidence_path: Path
    include_expiry_wait: bool = False

    @property
    def prefix(self) -> str:
        return self.names.prefix

    @property
    def tags(self) -> dict[str, str]:
        return self.names.allocation_tags()

    @property
    def partition(self) -> str:
        return "aws"

    def arn(self, service: str, resource: str) -> str:
        return f"arn:{self.partition}:{service}:{self.region}:{self.account_id}:{resource}"

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com"


def build_config(args: argparse.Namespace) -> PolicyEngineConfig:
    """Validate CLI input and resolve scratch-backed state/evidence paths."""
    if not model.ACCOUNT_PATTERN.fullmatch(str(args.account_id)):
        raise SpikeError("--account-id must contain exactly 12 digits")
    names = model.SpikeNames(prefix=str(args.prefix))
    if not model.region_is_supported(str(args.region)):
        raise SpikeError(
            f"Region {args.region!r} is not in the documented Policy in AgentCore "
            f"region list {sorted(model.POLICY_SUPPORTED_REGIONS)}"
        )
    scratch = os.environ.get("KIROCREW_SCRATCH")
    if not scratch:
        raise SpikeError("KIROCREW_SCRATCH must be set; refusing shared /tmp state")
    scratch_path = Path(scratch)
    state_path = (
        Path(args.state_file)
        if args.state_file
        else scratch_path / f"{names.prefix}-policy-engine-state.json"
    )
    evidence_path = (
        Path(args.evidence_file)
        if args.evidence_file
        else scratch_path / f"{names.prefix}-policy-engine-evidence.json"
    )
    return PolicyEngineConfig(
        account_id=str(args.account_id),
        region=str(args.region),
        names=names,
        state_path=state_path,
        evidence_path=evidence_path,
        # A complete run always proves token expiry; standalone commands may
        # opt in explicitly when their process has the in-memory user secrets.
        include_expiry_wait=bool(args.include_expiry_wait) or args.command == "all",
    )


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


class SpikeEvidence(Evidence):
    """Evidence document with value-level credential scanning.

    Reuses the base class's forbidden-key check, ``has_event`` and ``finish``,
    and adds a recursive value scan so a token or password cannot reach the file
    even inside a nested structure or an innocuously named field.
    """

    def __init__(self, path: Path, config: PolicyEngineConfig) -> None:  # noqa: D107
        self.store = JsonStore(path)
        current = self.store.read()
        self.document: dict[str, Any] = current or {
            "schemaVersion": 1,
            "spike": "agentcore-gateway-policy-engine",
            "run": {
                "prefix": config.prefix,
                "region": config.region,
                "accountSuffix": config.account_id[-4:],
                "boto3Version": boto3.__version__,
                "botocoreVersion": botocore.__version__,
                "mcpProtocolVersion": model.MCP_PROTOCOL_VERSION,
                "policyValidationMode": model.POLICY_VALIDATION_MODE,
                "startedAt": utc_now(),
            },
            "unknowns": [],
            "events": [],
        }

    def add(self, event: str, **details: Any) -> None:
        model.assert_no_secret_values(details, path=event)
        super().add(event, **details)

    def add_unknown(self, code: str, detail: str) -> None:
        record = model.unknown(code, detail)
        record["at"] = utc_now()
        unknowns = self.document.setdefault("unknowns", [])
        if not isinstance(unknowns, list):
            raise SpikeError("Evidence unknowns field is not an array")
        unknowns.append(record)
        self.store.write(self.document)


# --------------------------------------------------------------------------
# MCP client
# --------------------------------------------------------------------------


class McpSession:
    """Minimal MCP-over-HTTP client. Never logs headers or bodies."""

    def __init__(self, http: URLLib3Session, url: str) -> None:
        self.http = http
        self.url = url
        self.session_id: str | None = None
        self._next_id = 0

    def _identifier(self) -> int:
        self._next_id += 1
        return self._next_id

    def _headers(self, token: str | None, *, handshake: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if not handshake:
            headers[model.MCP_PROTOCOL_HEADER] = model.MCP_PROTOCOL_VERSION
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _decode(headers: Mapping[str, str], content: bytes) -> Mapping[str, Any] | None:
        if not content:
            return None
        content_type = str(headers.get("content-type", ""))
        if "text/event-stream" in content_type:
            for line in content.decode("utf-8", "replace").splitlines():
                if line.startswith("data:"):
                    candidate = json.loads(line[5:].strip())
                    if isinstance(candidate, Mapping) and (
                        "result" in candidate or "error" in candidate
                    ):
                        return candidate
            return None
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, Mapping) else None

    def post(
        self,
        token: str | None,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        handshake: bool = False,
        include_id: bool = True,
    ) -> tuple[int, Mapping[str, Any] | None]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if include_id:
            body["id"] = self._identifier()
        if params is not None:
            body["params"] = params
        request = AWSRequest(
            method="POST",
            url=self.url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(token, handshake=handshake),
        )
        response = self.http.send(request.prepare())
        if response.status_code == 200:
            returned = response.headers.get("Mcp-Session-Id") or response.headers.get(
                "mcp-session-id"
            )
            if returned:
                self.session_id = str(returned)
        return response.status_code, self._decode(response.headers, response.content)

    def initialize(self, token: str | None) -> tuple[int, Mapping[str, Any] | None]:
        status, payload = self.post(
            token,
            "initialize",
            {
                "protocolVersion": model.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "policy-engine-spike", "version": "1.0.0"},
            },
            handshake=True,
        )
        if status == 200:
            self.post(token, "notifications/initialized", include_id=False)
        return status, payload

    def list_tools(self, token: str) -> tuple[int, Mapping[str, Any] | None]:
        return self.post(token, "tools/list", {})

    def call_tool(
        self, token: str, name: str, arguments: Mapping[str, Any]
    ) -> tuple[int, Mapping[str, Any] | None]:
        return self.post(token, "tools/call", {"name": name, "arguments": arguments})


# --------------------------------------------------------------------------
# API wrappers
# --------------------------------------------------------------------------


class PolicyEngineApi:
    """One named wrapper per AWS operation, with a mutation scope guard.

    Every mutating wrapper asks :meth:`_require_mutation` first, so a command
    that declares an empty mutation scope cannot change AWS state even if a
    future refactor accidentally wires a mutator into it.
    """

    def __init__(self, session: boto3.Session, config: PolicyEngineConfig) -> None:
        self.config = config
        self.sts = session.client("sts")
        self.iam = session.client("iam")
        self.lam = session.client("lambda")
        self.logs = session.client("logs")
        self.cognito = session.client("cognito-idp")
        self.control = session.client("bedrock-agentcore-control")
        self.mutation_scope: frozenset[str] = frozenset()

    # -- guard ----------------------------------------------------------
    def _require_mutation(self, operation: str) -> None:
        if operation not in MUTATING_API_CALLS:
            raise SpikeError(f"Operation {operation!r} is not in the mutation registry")
        if operation not in self.mutation_scope:
            raise SpikeError(
                f"Refusing {operation!r}: the current command's mutation scope is "
                f"{sorted(self.mutation_scope) or 'read-only'}"
            )

    def capability(self, operation_name: str, member: str) -> bool:
        """True when the pinned SDK models ``member`` on ``operation_name``."""
        try:
            shape = self.control.meta.service_model.operation_model(
                operation_name
            ).input_shape
        except Exception:  # pragma: no cover - service model always present
            return False
        return bool(shape is not None and member in shape.members)

    # -- identity -------------------------------------------------------
    def get_caller_identity(self) -> Mapping[str, Any]:
        return self.sts.get_caller_identity()

    def simulate_principal_policy(
        self, source_arn: str, actions: Sequence[str]
    ) -> Mapping[str, Any]:
        return self.iam.simulate_principal_policy(
            PolicySourceArn=source_arn, ActionNames=list(actions)
        )

    # -- IAM ------------------------------------------------------------
    def get_role(self, role_name: str) -> Mapping[str, Any] | None:
        try:
            return self.iam.get_role(RoleName=role_name)["Role"]
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def get_role_policy(self, role_name: str, policy_name: str) -> Mapping[str, Any] | None:
        try:
            return self.iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_role(
        self, role_name: str, trust: Mapping[str, Any], description: str
    ) -> Mapping[str, Any]:
        self._require_mutation("create_role")
        return self.iam.create_role(
            RoleName=role_name,
            Description=description,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": k, "Value": v} for k, v in self.config.tags.items()],
        )

    def put_role_policy(
        self, role_name: str, policy_name: str, document: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self._require_mutation("put_role_policy")
        return self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(document),
        )

    def delete_role_policy(self, role_name: str, policy_name: str) -> None:
        self._require_mutation("delete_role_policy")
        try:
            self.iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    def delete_role(self, role_name: str) -> None:
        self._require_mutation("delete_role")
        try:
            self.iam.delete_role(RoleName=role_name)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    # -- Lambda ---------------------------------------------------------
    def get_function(self, name: str) -> Mapping[str, Any] | None:
        try:
            return self.lam.get_function(FunctionName=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_function(self, name: str, role_arn: str, zip_bytes: bytes) -> Mapping[str, Any]:
        self._require_mutation("create_function")
        return self.lam.create_function(
            FunctionName=name,
            Runtime=LAMBDA_RUNTIME,
            Role=role_arn,
            Handler=LAMBDA_HANDLER,
            Code={"ZipFile": zip_bytes},
            Description="Ephemeral PolicyEngine spike echo tool",
            Timeout=LAMBDA_TIMEOUT_SECONDS,
            MemorySize=LAMBDA_MEMORY_MB,
            Publish=False,
            Tags=dict(self.config.tags),
        )

    def get_lambda_policy(self, name: str) -> Mapping[str, Any] | None:
        try:
            response = self.lam.get_policy(FunctionName=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise
        document = json.loads(str(response["Policy"]))
        if not isinstance(document, Mapping):
            raise SpikeError("Lambda resource policy is not a JSON object")
        return document

    def add_permission(self, name: str, statement_id: str, principal_arn: str) -> Mapping[str, Any]:
        self._require_mutation("add_permission")
        return self.lam.add_permission(
            FunctionName=name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal=principal_arn,
        )

    def delete_function(self, name: str) -> None:
        self._require_mutation("delete_function")
        try:
            self.lam.delete_function(FunctionName=name)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    # -- CloudWatch Logs ------------------------------------------------
    def get_log_group(self, name: str) -> Mapping[str, Any] | None:
        response = self.logs.describe_log_groups(logGroupNamePrefix=name, limit=50)
        return next(
            (group for group in response.get("logGroups", []) if group.get("logGroupName") == name),
            None,
        )

    def get_log_group_tags(self, name: str) -> Mapping[str, str]:
        return self.logs.list_tags_log_group(logGroupName=name).get("tags", {})

    def create_log_group(self, name: str) -> Mapping[str, Any]:
        self._require_mutation("create_log_group")
        return self.logs.create_log_group(logGroupName=name, tags=dict(self.config.tags))

    def put_retention_policy(self, name: str, days: int) -> Mapping[str, Any]:
        self._require_mutation("put_retention_policy")
        return self.logs.put_retention_policy(logGroupName=name, retentionInDays=days)

    def delete_log_group(self, name: str) -> None:
        self._require_mutation("delete_log_group")
        try:
            self.logs.delete_log_group(logGroupName=name)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    # -- Cognito --------------------------------------------------------
    def find_user_pool(self, pool_name: str) -> Mapping[str, Any] | None:
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 60}
            if next_token:
                kwargs["NextToken"] = next_token
            response = self.cognito.list_user_pools(**kwargs)
            for pool in response.get("UserPools", []):
                if pool.get("Name") == pool_name:
                    return pool
            next_token = response.get("NextToken")
            if not next_token:
                return None

    def describe_user_pool(self, pool_id: str) -> Mapping[str, Any] | None:
        try:
            return self.cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_user_pool(self, pool_name: str) -> Mapping[str, Any]:
        self._require_mutation("create_user_pool")
        return self.cognito.create_user_pool(
            PoolName=pool_name,
            UserPoolTags=dict(self.config.tags),
            DeletionProtection="INACTIVE",
            MfaConfiguration="OFF",
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 12,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": True,
                }
            },
        )

    def create_user_pool_client(self, pool_id: str, client_name: str) -> Mapping[str, Any]:
        self._require_mutation("create_user_pool_client")
        return self.cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=client_name,
            # No client secret: the spike authenticates users server-side with
            # ADMIN_USER_PASSWORD_AUTH, so there is no secret to hold at all.
            GenerateSecret=False,
            ExplicitAuthFlows=[
                "ALLOW_ADMIN_USER_PASSWORD_AUTH",
                "ALLOW_REFRESH_TOKEN_AUTH",
            ],
            AccessTokenValidity=ACCESS_TOKEN_MINUTES,
            TokenValidityUnits={"AccessToken": "minutes"},
        )

    def delete_user_pool_client(self, pool_id: str, client_id: str) -> None:
        self._require_mutation("delete_user_pool_client")
        try:
            self.cognito.delete_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    def create_group(self, pool_id: str, group_name: str) -> Mapping[str, Any]:
        self._require_mutation("create_group")
        return self.cognito.create_group(
            UserPoolId=pool_id,
            GroupName=group_name,
            Description="Shared group proving group membership is not the entitlement",
        )

    def delete_group(self, pool_id: str, group_name: str) -> None:
        self._require_mutation("delete_group")
        try:
            self.cognito.delete_group(UserPoolId=pool_id, GroupName=group_name)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    def admin_create_user(self, pool_id: str, user_name: str) -> Mapping[str, Any]:
        self._require_mutation("admin_create_user")
        return self.cognito.admin_create_user(
            UserPoolId=pool_id, Username=user_name, MessageAction="SUPPRESS"
        )

    def admin_set_user_password(self, pool_id: str, user_name: str, password: str) -> None:
        self._require_mutation("admin_set_user_password")
        self.cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=user_name, Password=password, Permanent=True
        )

    def admin_add_user_to_group(self, pool_id: str, user_name: str, group_name: str) -> None:
        self._require_mutation("admin_add_user_to_group")
        self.cognito.admin_add_user_to_group(
            UserPoolId=pool_id, Username=user_name, GroupName=group_name
        )

    def admin_get_user(self, pool_id: str, user_name: str) -> Mapping[str, Any] | None:
        """Read-only despite the ``admin_`` prefix; safe inside ``verify``."""
        try:
            return self.cognito.admin_get_user(UserPoolId=pool_id, Username=user_name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES | {"UserNotFoundException"}:
                return None
            raise

    def admin_initiate_auth(
        self, pool_id: str, client_id: str, user_name: str, password: str
    ) -> Mapping[str, Any]:
        """Mints a short-lived token in memory; changes no infrastructure."""
        return self.cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user_name, "PASSWORD": password},
        )

    def delete_user_pool(self, pool_id: str) -> None:
        self._require_mutation("delete_user_pool")
        try:
            self.cognito.delete_user_pool(UserPoolId=pool_id)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    # -- PolicyEngine ---------------------------------------------------
    def find_policy_engine(self, engine_name: str) -> Mapping[str, Any] | None:
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.control.list_policy_engines(**kwargs)
            for engine in response.get("policyEngines", response.get("items", [])):
                if engine.get("name") == engine_name:
                    return engine
            next_token = response.get("nextToken")
            if not next_token:
                return None

    def get_policy_engine(self, engine_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_policy_engine(policyEngineId=engine_id)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_policy_engine(
        self, engine_name: str, client_token: str
    ) -> Mapping[str, Any]:
        self._require_mutation("create_policy_engine")
        return self.control.create_policy_engine(
            name=engine_name,
            description="Ephemeral PolicyEngine compatibility spike",
            clientToken=client_token,
            tags=dict(self.config.tags),
        )

    def delete_policy_engine(self, engine_id: str) -> None:
        self._require_mutation("delete_policy_engine")
        try:
            self.control.delete_policy_engine(policyEngineId=engine_id)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    def list_policy_summaries(self, engine_id: str) -> list[Mapping[str, Any]]:
        """List policy *summaries* for the engine (B1).

        Uses ``ListPolicySummaries`` -- the lightweight listing whose response is
        ``{"policies": [...], "nextToken": ...}`` with each summary carrying
        ``policyId``/``name``/``status``/``enforcementMode`` but no Cedar
        ``definition``. The spike standardises on this for listing and
        absence-polling; full statements are fetched per-id via ``get_policy``.
        """
        policies: list[Mapping[str, Any]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"policyEngineId": engine_id, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.control.list_policy_summaries(**kwargs)
            policies.extend(response.get("policies", []))
            next_token = response.get("nextToken")
            if not next_token:
                return policies

    def get_policy_summary(self, engine_id: str, policy_id: str) -> Mapping[str, Any] | None:
        """Read one policy summary; ``None`` when it is absent (async delete)."""
        try:
            return self.control.get_policy_summary(
                policyEngineId=engine_id, policyId=policy_id
            )
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def get_policy(self, engine_id: str, policy_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_policy(policyEngineId=engine_id, policyId=policy_id)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_policy(
        self, engine_id: str, name: str, statement: str, client_token: str
    ) -> Mapping[str, Any]:
        self._require_mutation("create_policy")
        model.assert_no_pattern_matching(statement)
        return self.control.create_policy(
            policyEngineId=engine_id,
            name=name,
            definition={"cedar": {"statement": statement}},
            description="Ephemeral PolicyEngine spike policy",
            validationMode=model.POLICY_VALIDATION_MODE,
            enforcementMode=model.POLICY_ENFORCEMENT_MODE,
            clientToken=client_token,
        )

    def delete_policy(self, engine_id: str, policy_id: str) -> None:
        self._require_mutation("delete_policy")
        try:
            self.control.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    # -- Gateway --------------------------------------------------------
    def find_gateway(self, gateway_name: str) -> Mapping[str, Any] | None:
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.control.list_gateways(**kwargs)
            for gateway in response.get("items", []):
                if gateway.get("name") == gateway_name:
                    return gateway
            next_token = response.get("nextToken")
            if not next_token:
                return None

    def get_gateway(self, gateway_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_gateway(gatewayIdentifier=gateway_id)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def list_tags_for_resource(self, arn: str) -> Mapping[str, str]:
        return self.control.list_tags_for_resource(resourceArn=arn).get("tags", {})

    def create_gateway(
        self, gateway_name: str, role_arn: str, client_id: str, pool_id: str, client_token: str
    ) -> Mapping[str, Any]:
        self._require_mutation("create_gateway")
        return self.control.create_gateway(
            name=gateway_name,
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration={
                "mcp": {"supportedVersions": [model.MCP_PROTOCOL_VERSION]}
            },
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=self.jwt_authorizer(pool_id, client_id),
            description="Ephemeral PolicyEngine compatibility spike gateway",
            tags=dict(self.config.tags),
            clientToken=client_token,
        )

    def jwt_authorizer(self, pool_id: str, client_id: str) -> dict[str, Any]:
        discovery = (
            f"{self.config.issuer}/{pool_id}/.well-known/openid-configuration"
        )
        return {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery,
                "allowedClients": [client_id],
            }
        }

    def _gateway_update_kwargs(
        self, gateway: Mapping[str, Any], pool_id: str, client_id: str
    ) -> dict[str, Any]:
        return {
            "gatewayIdentifier": str(gateway["gatewayId"]),
            "name": str(gateway["name"]),
            "roleArn": str(gateway["roleArn"]),
            "protocolType": "MCP",
            "protocolConfiguration": {
                "mcp": {"supportedVersions": [model.MCP_PROTOCOL_VERSION]}
            },
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": self.jwt_authorizer(pool_id, client_id),
        }

    def associate_policy_engine(
        self,
        gateway: Mapping[str, Any],
        pool_id: str,
        client_id: str,
        engine_arn: str,
        mode: str,
    ) -> Mapping[str, Any]:
        """Associate the engine at ``mode`` via ``UpdateGateway``.

        The pinned service model exposes no dedicated association or
        mode-transition operation; both are expressed by re-sending the gateway
        definition with ``policyEngineConfiguration`` (B2). This is the single
        mutation the rollback command may reach.
        """
        self._require_mutation("update_gateway")
        if mode not in model.GATEWAY_MODES:
            raise SpikeError(f"Unsupported policy engine mode {mode!r}")
        kwargs = self._gateway_update_kwargs(gateway, pool_id, client_id)
        kwargs["policyEngineConfiguration"] = {"arn": engine_arn, "mode": mode}
        return self.control.update_gateway(**kwargs)

    def detach_policy_engine(
        self, gateway: Mapping[str, Any], pool_id: str, client_id: str
    ) -> Mapping[str, Any]:
        """Detach the engine (cleanup only) by re-sending the gateway without a
        ``policyEngineConfiguration``, so it becomes deletable."""
        self._require_mutation("update_gateway")
        return self.control.update_gateway(
            **self._gateway_update_kwargs(gateway, pool_id, client_id)
        )

    def delete_gateway(self, gateway_id: str) -> None:
        self._require_mutation("delete_gateway")
        try:
            self.control.delete_gateway(gatewayIdentifier=gateway_id)
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise

    def list_gateway_targets(self, gateway_id: str) -> list[Mapping[str, Any]]:
        targets: list[Mapping[str, Any]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"gatewayIdentifier": gateway_id, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.control.list_gateway_targets(**kwargs)
            targets.extend(response.get("items", []))
            next_token = response.get("nextToken")
            if not next_token:
                return targets

    def get_gateway_target(self, gateway_id: str, target_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def create_gateway_target(
        self, gateway_id: str, target_name: str, lambda_arn: str, client_token: str
    ) -> Mapping[str, Any]:
        self._require_mutation("create_gateway_target")
        return self.control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=target_name,
            description="Ephemeral deterministic echo tools",
            targetConfiguration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arn,
                        "toolSchema": {
                            "inlinePayload": [
                                model.echo_tool_schema(tool) for tool in model.SPIKE_TOOLS
                            ]
                        },
                    }
                }
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
            clientToken=client_token,
        )

    def delete_gateway_target(self, gateway_id: str, target_id: str) -> None:
        self._require_mutation("delete_gateway_target")
        try:
            self.control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
        except ClientError as error:
            if aws_error_code(error) not in NOT_FOUND_CODES:
                raise


# --------------------------------------------------------------------------
# IAM document builders (pure)
# --------------------------------------------------------------------------


def gateway_trust_policy(config: PolicyEngineConfig) -> dict[str, Any]:
    source_arn = config.arn(
        "bedrock-agentcore", f"gateway/{config.names.gateway_name}-*"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GatewayAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": config.account_id},
                    "ArnLike": {"aws:SourceArn": source_arn},
                },
            }
        ],
    }


def lambda_trust_policy() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def lambda_logs_policy(config: PolicyEngineConfig) -> dict[str, Any]:
    group = config.arn("logs", f"log-group:/aws/lambda/{config.names.lambda_name}")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WriteOwnLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [group, f"{group}:*"],
            }
        ],
    }


def gateway_invoke_policy(lambda_arn: str) -> dict[str, Any]:
    if "*" in lambda_arn:
        raise SpikeError("Refusing a wildcard Lambda invoke resource")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeExactEchoTool",
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": lambda_arn,
            }
        ],
    }


def gateway_authorize_engine_policy(engine_arn: str) -> dict[str, Any]:
    """GetPolicyEngine, scoped to the ENGINE ARN only (B9)."""
    if "*" in engine_arn:
        raise SpikeError("Refusing a wildcard policy-evaluation resource")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadPolicyEngine",
                "Effect": "Allow",
                "Action": list(GATEWAY_AUTHZ_ENGINE_ACTIONS),
                "Resource": engine_arn,
            }
        ],
    }


def gateway_authorize_eval_policy(engine_arn: str, gateway_arn: str) -> dict[str, Any]:
    """AuthorizeAction/PartiallyAuthorizeActions, scoped to ENGINE + GATEWAY (B9)."""
    for arn in (engine_arn, gateway_arn):
        if "*" in arn:
            raise SpikeError("Refusing a wildcard policy-evaluation resource")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EvaluatePolicyEngine",
                "Effect": "Allow",
                "Action": list(GATEWAY_AUTHZ_EVAL_ACTIONS),
                "Resource": [engine_arn, gateway_arn],
            }
        ],
    }


def build_lambda_zip() -> bytes:
    """Deterministic in-memory zip so its fingerprint is reproducible."""
    buffer = io.BytesIO()
    info = zipfile.ZipInfo(filename="index.py", date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(info, LAMBDA_SOURCE)
    return buffer.getvalue()


def role_arn_for_simulation(caller_arn: str, account_id: str) -> str | None:
    """Convert an assumed-role session ARN into its role ARN, if possible."""
    marker = ":assumed-role/"
    if marker not in caller_arn:
        return caller_arn if ":role/" in caller_arn or ":user/" in caller_arn else None
    remainder = caller_arn.split(marker, 1)[1]
    role_name = remainder.split("/", 1)[0]
    if not role_name:
        return None
    return f"arn:aws:iam::{account_id}:role/{role_name}"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


class PolicyEngineSpike:
    """Bounded deploy/verify/rollback/cleanup orchestration."""

    def __init__(self, config: PolicyEngineConfig) -> None:
        if boto3.__version__ != REQUIRED_BOTO3_VERSION:
            raise SpikeError(
                f"boto3 {REQUIRED_BOTO3_VERSION} is required; found {boto3.__version__}"
            )
        self.config = config
        self.names = config.names
        self.session = boto3.Session(region_name=config.region)
        self.api = PolicyEngineApi(self.session, config)
        if not hasattr(self.api.control, "create_policy_engine"):
            raise SpikeError("Installed SDK does not expose create_policy_engine")
        self.http = URLLib3Session()
        self.state_store = JsonStore(config.state_path)
        self.state: dict[str, Any] = self.state_store.read()
        self.evidence = SpikeEvidence(config.evidence_path, config)
        self._passwords: dict[str, str] = {}

    # -- small helpers --------------------------------------------------
    def record(self, event: str, **details: Any) -> None:
        self.evidence.add(event, **details)

    def record_unknown(self, code: str, detail: str) -> None:
        self.evidence.add_unknown(code, detail)

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state_store.write(self.state)

    def allow_mutations(self, *operations: str) -> None:
        self.api.mutation_scope = frozenset(operations)

    def client_token(self, operation: str) -> str:
        key = f"{operation}ClientToken"
        existing = self.state.get(key)
        if existing:
            return str(existing)
        token = hashlib.sha256(
            f"{self.names.prefix}:{operation}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()
        self.save_state(**{key: token})
        return token

    def require_state(self, key: str) -> str:
        value = self.state.get(key)
        if not value:
            raise SpikeError(f"State key {key!r} is absent; run deploy first")
        return str(value)

    def assert_owned_tags(self, tags: Mapping[str, str], label: str) -> None:
        expected = self.config.tags
        if any(tags.get(key) != value for key, value in expected.items()):
            raise SpikeError(f"Refusing to operate on {label}: ownership tags differ")

    # -- bounded propagation retry (B5) ---------------------------------
    def call_with_propagation(
        self, label: str, attempt: Callable[[], Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        """Retry one idempotent AgentCore operation while IAM propagates.

        Create callers MUST close over one stable client token; UpdateGateway is
        itself idempotent. Only explicit transient/propagation codes are retried
        for six minutes. Validation errors fail immediately.
        """
        started = time.monotonic()
        deadline = started + PROPAGATION_TIMEOUT_SECONDS
        attempts = 0
        while True:
            attempts += 1
            try:
                response = attempt()
                self.record(
                    "propagation_call_succeeded",
                    resource=label,
                    attempts=attempts,
                    waitedSeconds=round(time.monotonic() - started, 1),
                )
                return response
            except ClientError as error:
                code = aws_error_code(error)
                if code not in PROPAGATION_RETRY_CODES:
                    raise
                if time.monotonic() >= deadline:
                    raise SpikeError(
                        f"{label} did not succeed within "
                        f"{PROPAGATION_TIMEOUT_SECONDS}s (last retryable code {code})"
                    ) from error
                time.sleep(PROPAGATION_INTERVAL_SECONDS)
            except BotoCoreError as error:
                # Transport-level hiccup (e.g. endpoint resolution) -> bounded retry.
                if time.monotonic() >= deadline:
                    raise SpikeError(
                        f"{label} kept failing at transport level "
                        f"({type(error).__name__})"
                    ) from error
                time.sleep(PROPAGATION_INTERVAL_SECONDS)

    # -- identity and preconditions -------------------------------------
    def verify_identity(self) -> None:
        identity = self.api.get_caller_identity()
        actual = str(identity["Account"])
        if actual != self.config.account_id:
            raise SpikeError(
                f"Authenticated account {actual} does not match expected "
                f"{self.config.account_id}"
            )
        if not model.region_is_supported(self.config.region):
            raise SpikeError(f"Region {self.config.region} is not a documented Policy region")
        self.record(
            "identity_verified",
            accountSuffix=actual[-4:],
            region=self.config.region,
            regionIsEmea=self.config.region in model.POLICY_EMEA_REGIONS,
            awsRequestId=request_id(identity),
        )

    def check_management_permissions(self) -> None:
        """Best-effort, read-only check of the management caller's permissions."""
        identity = self.api.get_caller_identity()
        source = role_arn_for_simulation(str(identity["Arn"]), self.config.account_id)
        if source is None:
            self.record_unknown(
                "management_permissions_unverified",
                "Caller ARN is not a role or user ARN, so SimulatePrincipalPolicy "
                "could not be used. Required management actions are documented in "
                "the README instead.",
            )
            return
        try:
            response = self.api.simulate_principal_policy(source, MANAGEMENT_ACTIONS)
        except (ClientError, BotoCoreError) as error:
            self.record_unknown(
                "management_permissions_unverified",
                "SimulatePrincipalPolicy was unavailable "
                f"({type(error).__name__}); required management actions are "
                "documented in the README instead.",
            )
            return
        decisions = {
            str(item["EvalActionName"]): str(item["EvalDecision"])
            for item in response.get("EvaluationResults", [])
        }
        denied = {
            action: decisions.get(action, "missing")
            for action in MANAGEMENT_ACTIONS
            if decisions.get(action) != "allowed"
        }
        if denied:
            raise SpikeError(f"Management caller lacks required actions: {denied}")
        self.record("management_permissions_simulated", decisions=decisions)

    # -- policy engine ---------------------------------------------------
    def assert_policy_engine_owned(self, engine: Mapping[str, Any]) -> tuple[str, str]:
        name = str(engine.get("name", ""))
        engine_id = str(engine.get("policyEngineId", ""))
        engine_arn = str(engine.get("policyEngineArn", ""))
        if name != self.names.engine_name or not engine_id or not engine_arn:
            raise SpikeError("Refusing to operate on a PolicyEngine with mismatched identity")
        self.assert_owned_tags(
            self.api.list_tags_for_resource(engine_arn), f"PolicyEngine {engine_arn}"
        )
        return engine_id, engine_arn

    def ensure_policy_engine(self) -> tuple[str, str]:
        engine_id = self.state.get("policyEngineId")
        if engine_id:
            engine = self.api.get_policy_engine(str(engine_id))
            if engine is None:
                raise SpikeError("State names a policy engine that no longer exists")
            return self.assert_policy_engine_owned(engine)

        if self.api.find_policy_engine(self.names.engine_name) is not None:
            raise SpikeError(
                f"Policy engine {self.names.engine_name} exists without this run's state file"
            )
        if not self.api.capability("CreatePolicyEngine", "tags"):
            raise SpikeError("Pinned SDK does not support required PolicyEngine ownership tags")
        response = self.api.create_policy_engine(
            self.names.engine_name, self.client_token("policyEngine")
        )
        engine_id = str(response["policyEngineId"])
        engine_arn = str(response["policyEngineArn"])
        self.save_state(policyEngineId=engine_id, policyEngineArn=engine_arn)
        self.record(
            "policy_engine_created",
            policyEngineId=engine_id,
            tagged=True,
            awsRequestId=request_id(response),
        )
        self.wait_policy_engine_active(engine_id)
        engine = self.api.get_policy_engine(engine_id)
        if engine is None:
            raise SpikeError("Policy engine disappeared after becoming ACTIVE")
        return self.assert_policy_engine_owned(engine)

    def wait_policy_engine_active(self, engine_id: str, timeout: int = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            engine = self.api.get_policy_engine(engine_id)
            if engine is None:
                raise SpikeError("Policy engine disappeared while waiting for ACTIVE")
            status = str(engine["status"])
            if status == model.POLICY_ACTIVE_STATUS:
                self.record("policy_engine_active", policyEngineId=engine_id)
                return
            if status in model.POLICY_TERMINAL_FAILURES:
                raise SpikeError(
                    f"Policy engine entered {status}: {engine.get('statusReasons', [])}"
                )
            time.sleep(5)
        raise SpikeError(f"Policy engine did not reach ACTIVE within {timeout}s")

    # -- IAM + Lambda ----------------------------------------------------
    def ensure_role(
        self, role_name: str, trust: Mapping[str, Any], description: str
    ) -> str:
        existing = self.api.get_role(role_name)
        if existing is not None:
            tags = {item["Key"]: item["Value"] for item in existing.get("Tags", [])}
            self.assert_owned_tags(tags, f"IAM role {role_name}")
            actual_trust = existing.get("AssumeRolePolicyDocument")
            if json.dumps(actual_trust, sort_keys=True) != json.dumps(trust, sort_keys=True):
                raise SpikeError(f"IAM role {role_name} trust policy differs")
            return str(existing["Arn"])
        response = self.api.create_role(role_name, trust, description)
        role_arn = str(response["Role"]["Arn"])
        self.record("iam_role_created", roleName=role_name, awsRequestId=request_id(response))
        return role_arn

    def assert_lambda_owned(self, function: Mapping[str, Any]) -> str:
        configuration = function.get("Configuration", {})
        name = str(configuration.get("FunctionName", ""))
        arn = str(configuration.get("FunctionArn", ""))
        if name != self.names.lambda_name or not arn:
            raise SpikeError("Refusing to operate on a Lambda with mismatched identity")
        self.assert_owned_tags(function.get("Tags", {}), f"Lambda {name}")
        return arn

    def ensure_log_group(self) -> None:
        group = self.api.get_log_group(self.names.log_group_name)
        if group is None:
            response = self.api.create_log_group(self.names.log_group_name)
            self.record(
                "lambda_log_group_created",
                logGroupName=self.names.log_group_name,
                awsRequestId=request_id(response),
            )
        self.assert_owned_tags(
            self.api.get_log_group_tags(self.names.log_group_name),
            f"log group {self.names.log_group_name}",
        )
        self.api.put_retention_policy(self.names.log_group_name, LOG_RETENTION_DAYS)

    def create_lambda_with_propagation(self, role_arn: str, payload: bytes) -> Mapping[str, Any]:
        """Create by unique name, safely recovering a lost successful response."""
        started = time.monotonic()
        deadline = started + PROPAGATION_TIMEOUT_SECONDS
        attempts = 0
        while True:
            attempts += 1
            existing = self.api.get_function(self.names.lambda_name)
            if existing is not None:
                self.assert_lambda_owned(existing)
                self.record(
                    "lambda_create_recovered",
                    attempts=attempts,
                    waitedSeconds=round(time.monotonic() - started, 1),
                )
                return existing
            try:
                response = self.api.create_function(self.names.lambda_name, role_arn, payload)
                self.record(
                    "lambda_create_succeeded",
                    attempts=attempts,
                    waitedSeconds=round(time.monotonic() - started, 1),
                )
                return {"Configuration": response, "Tags": dict(self.config.tags)}
            except ClientError as error:
                if aws_error_code(error) not in {
                    "InvalidParameterValueException",
                    "ResourceConflictException",
                }:
                    raise
                if time.monotonic() >= deadline:
                    raise SpikeError("Lambda execution role did not propagate in time") from error
                time.sleep(PROPAGATION_INTERVAL_SECONDS)
            except BotoCoreError as error:
                if time.monotonic() >= deadline:
                    raise SpikeError("Lambda creation did not complete in time") from error
                time.sleep(PROPAGATION_INTERVAL_SECONDS)

    def ensure_lambda(self) -> str:
        existing = self.api.get_function(self.names.lambda_name)
        if existing is not None:
            arn = self.assert_lambda_owned(existing)
            self.ensure_log_group()
            self.save_state(lambdaArn=arn)
            return arn
        role_arn = self.ensure_role(
            self.names.lambda_role_name,
            lambda_trust_policy(),
            "Ephemeral PolicyEngine spike echo-tool execution role",
        )
        self.api.put_role_policy(
            self.names.lambda_role_name,
            self.names.lambda_logs_policy_name,
            lambda_logs_policy(self.config),
        )
        self.ensure_log_group()
        self.save_state(lambdaRoleArn=role_arn)
        payload = build_lambda_zip()
        function = self.create_lambda_with_propagation(role_arn, payload)
        arn = self.assert_lambda_owned(function)
        self.save_state(lambdaArn=arn)
        self.record(
            "echo_lambda_created",
            functionName=self.names.lambda_name,
            zipFingerprint=model.fingerprint(payload.hex()),
            runtime=LAMBDA_RUNTIME,
        )
        return arn

    def ensure_lambda_permission(self, role_arn: str) -> None:
        expected_sid = self.names.lambda_permission_id
        document = self.api.get_lambda_policy(self.names.lambda_name)
        statements = [] if document is None else document.get("Statement", [])
        existing = next(
            (
                statement
                for statement in statements
                if isinstance(statement, Mapping) and statement.get("Sid") == expected_sid
            ),
            None,
        )
        if existing is not None:
            principal = existing.get("Principal", {})
            actions = existing.get("Action")
            if (
                not isinstance(principal, Mapping)
                or principal.get("AWS") != role_arn
                or actions not in ("lambda:InvokeFunction", ["lambda:InvokeFunction"])
                or existing.get("Effect") != "Allow"
                or existing.get("Condition") not in (None, {})
            ):
                raise SpikeError("Existing Lambda permission differs from the exact Gateway role")
            self.record("lambda_permission_reused", statementId=expected_sid)
            return
        response = self.api.add_permission(
            self.names.lambda_name, expected_sid, role_arn
        )
        self.record(
            "lambda_permission_created",
            statementId=expected_sid,
            principalArn=role_arn,
            awsRequestId=request_id(response),
        )

    def ensure_gateway_role(self, lambda_arn: str) -> str:
        role_arn = self.ensure_role(
            self.names.gateway_role_name,
            gateway_trust_policy(self.config),
            "Ephemeral PolicyEngine spike gateway service role",
        )
        self.api.put_role_policy(
            self.names.gateway_role_name,
            self.names.gateway_invoke_policy_name,
            gateway_invoke_policy(lambda_arn),
        )
        self.save_state(gatewayRoleArn=role_arn)
        self.record("gateway_role_invoke_policy_attached", roleName=self.names.gateway_role_name)
        return role_arn

    def attach_authorize_policies(self, engine_arn: str, gateway_arn: str) -> None:
        """Grant policy evaluation actions with service-specific resource scopes."""
        self.api.put_role_policy(
            self.names.gateway_role_name,
            self.names.gateway_authz_engine_policy_name,
            gateway_authorize_engine_policy(engine_arn),
        )
        self.api.put_role_policy(
            self.names.gateway_role_name,
            self.names.gateway_authz_gateway_policy_name,
            gateway_authorize_eval_policy(engine_arn, gateway_arn),
        )
        self.record(
            "gateway_role_authorize_policies_attached",
            engineActions=list(GATEWAY_AUTHZ_ENGINE_ACTIONS),
            evaluationActions=list(GATEWAY_AUTHZ_EVAL_ACTIONS),
            engineResource=engine_arn,
            evaluationResources=[engine_arn, gateway_arn],
        )

    # -- Cognito ---------------------------------------------------------
    def ensure_cognito(self) -> tuple[str, str, str]:
        pool_id = self.state.get("userPoolId")
        if pool_id:
            pool = self.api.describe_user_pool(str(pool_id))
            if pool is None:
                raise SpikeError("State names a user pool that no longer exists")
            if str(pool.get("Name", "")) != self.names.user_pool_name:
                raise SpikeError("State references a Cognito pool with a different name")
            self.assert_owned_tags(pool.get("UserPoolTags", {}), "Cognito User Pool")
            return (
                str(pool_id),
                self.require_state("primaryClientId"),
                self.require_state("foreignClientId"),
            )
        if self.api.find_user_pool(self.names.user_pool_name) is not None:
            raise SpikeError(
                f"User pool {self.names.user_pool_name} exists without this run's state file"
            )
        response = self.api.create_user_pool(self.names.user_pool_name)
        pool_id = str(response["UserPool"]["Id"])
        self.save_state(userPoolId=pool_id)
        self.record("cognito_pool_created", userPoolId=pool_id, awsRequestId=request_id(response))

        primary = self.api.create_user_pool_client(pool_id, self.names.primary_client_name)
        primary_id = str(primary["UserPoolClient"]["ClientId"])
        foreign = self.api.create_user_pool_client(pool_id, self.names.foreign_client_name)
        foreign_id = str(foreign["UserPoolClient"]["ClientId"])
        self.save_state(primaryClientId=primary_id, foreignClientId=foreign_id)
        self.record(
            "cognito_clients_created",
            primaryClientIdFingerprint=model.fingerprint(primary_id),
            foreignClientIdFingerprint=model.fingerprint(foreign_id),
            # No client secret is generated at all, so there is nothing to hold.
            authMode="ADMIN_USER_PASSWORD_AUTH",
        )
        groups = model.group_layout(self.names)
        for group_name in groups.values():
            self.api.create_group(pool_id, group_name)
        self.record("cognito_groups_created", groups=groups)
        return pool_id, primary_id, foreign_id

    def ensure_users(self, pool_id: str) -> dict[str, str]:
        subjects: dict[str, str] = dict(self.state.get("subjects", {}))
        for label in model.SUBJECT_LABELS:
            user_name = self.names.user_name(label)
            if self.api.admin_get_user(pool_id, user_name) is None:
                self.api.admin_create_user(pool_id, user_name)
            for group_name in model.groups_for_label(self.names, label):
                self.api.admin_add_user_to_group(pool_id, user_name, group_name)
            password = self._passwords.get(label) or model.generate_transient_password()
            self._passwords[label] = password
            self.api.admin_set_user_password(pool_id, user_name, password)
            subjects[label] = self.read_subject(pool_id, user_name)
        self.save_state(subjects=subjects)
        self.record(
            "cognito_users_ready",
            groupMatrix={
                label: list(model.groups_for_label(self.names, label))
                for label in model.SUBJECT_LABELS
            },
            subjectFingerprints={
                label: model.fingerprint(value) for label, value in subjects.items()
            },
            subjectGroupMatrixComplete=True,
        )
        return subjects

    def read_subject(self, pool_id: str, user_name: str) -> str:
        user = self.api.admin_get_user(pool_id, user_name)
        if user is None:
            raise SpikeError(f"User {user_name} is absent")
        for attribute in user.get("UserAttributes", []):
            if attribute.get("Name") == "sub":
                return str(attribute["Value"])
        raise SpikeError(f"User {user_name} has no sub attribute")

    def access_token(self, label: str) -> str:
        """Mint a short-lived access token. Held in memory only."""
        pool_id = self.require_state("userPoolId")
        return self._token_for_client(label, self.require_state("primaryClientId"), pool_id)

    def _token_for_client(self, label: str, client_id: str, pool_id: str) -> str:
        password = self._passwords.get(label)
        if password is None:
            raise SpikeError(
                f"No in-memory password for {label!r}. User passwords are never "
                "persisted, so verify and rollback can only mint tokens in the "
                "same process that created the users — use the 'all' command."
            )
        response = self.api.admin_initiate_auth(
            pool_id, client_id, self.names.user_name(label), password
        )
        result = response.get("AuthenticationResult", {})
        token = result.get("AccessToken")
        if not token:
            raise SpikeError(f"Cognito returned no access token for {label}")
        return str(token)

    def foreign_token(self, label: str) -> str:
        return self._token_for_client(
            label, self.require_state("foreignClientId"), self.require_state("userPoolId")
        )

    @staticmethod
    def token_groups(token: str) -> tuple[str, ...]:
        """Decode only the in-memory payload to inspect Cognito group claims."""
        parts = token.split(".")
        if len(parts) != 3:
            raise SpikeError("Cognito returned a malformed access token")
        claims = json.loads(model._b64url_decode(parts[1]))
        if not isinstance(claims, Mapping):
            raise SpikeError("Cognito access-token payload is not an object")
        groups = claims.get(model.GROUP_CLAIM_NAME, [])
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise SpikeError("Cognito groups claim is not a string array")
        return tuple(sorted(groups))

    def verify_token_group_matrix(self) -> None:
        for label in model.SUBJECT_LABELS:
            actual = self.token_groups(self.access_token(label))
            expected = tuple(sorted(model.groups_for_label(self.names, label)))
            if actual != expected:
                raise SpikeError(
                    f"Cognito groups for {label} are {actual}, expected {expected}"
                )
        self.record(
            "cognito_group_claim_matrix_verified",
            representation="jwt-array-of-strings",
            labels=list(model.SUBJECT_LABELS),
        )

    # -- Gateway ---------------------------------------------------------
    def ensure_gateway(self, role_arn: str, pool_id: str, client_id: str) -> Mapping[str, Any]:
        gateway_id = self.state.get("gatewayId")
        if gateway_id:
            gateway = self.wait_gateway_ready(str(gateway_id))
            self.assert_gateway_contract(gateway, role_arn, pool_id, client_id)
            return gateway
        if self.api.find_gateway(self.names.gateway_name) is not None:
            raise SpikeError(
                f"Gateway {self.names.gateway_name} exists without this run's state file"
            )
        token = self.client_token("gateway")
        response = self.call_with_propagation(
            "Gateway",
            lambda: self.api.create_gateway(
                self.names.gateway_name,
                role_arn,
                client_id,
                pool_id,
                token,
            ),
        )
        self.save_state(
            gatewayId=str(response["gatewayId"]),
            gatewayArn=str(response["gatewayArn"]),
            gatewayUrl=str(response["gatewayUrl"]),
        )
        self.record(
            "gateway_created",
            gatewayId=str(response["gatewayId"]),
            gatewayArn=str(response["gatewayArn"]),
            authorizerType="CUSTOM_JWT",
            awsRequestId=request_id(response),
        )
        gateway = self.wait_gateway_ready(str(response["gatewayId"]))
        self.assert_gateway_contract(gateway, role_arn, pool_id, client_id)
        return gateway

    def wait_gateway_ready(self, gateway_id: str, timeout: int = 300) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            gateway = self.api.get_gateway(gateway_id)
            if gateway is None:
                raise SpikeError("Gateway disappeared while waiting for READY")
            status = str(gateway["status"])
            if status in GATEWAY_READY_STATES:
                self.assert_gateway_owned(gateway)
                return gateway
            if status in GATEWAY_FAILURES:
                raise SpikeError(
                    f"Gateway entered {status}: {gateway.get('statusReasons', [])}"
                )
            time.sleep(5)
        raise SpikeError(f"Gateway did not reach READY within {timeout}s")

    def assert_gateway_owned(self, gateway: Mapping[str, Any]) -> None:
        if gateway.get("name") != self.names.gateway_name:
            raise SpikeError("Gateway name does not match the spike")
        arn = str(gateway.get("gatewayArn", ""))
        if not arn:
            raise SpikeError("Gateway has no ARN")
        self.assert_owned_tags(self.api.list_tags_for_resource(arn), f"Gateway {arn}")

    def assert_target_owned(
        self, target: Mapping[str, Any], lambda_arn: str
    ) -> None:
        if str(target.get("name", "")) != self.names.target_name:
            raise SpikeError("Gateway target name differs from the spike")
        configuration = target.get("targetConfiguration", {})
        mcp = configuration.get("mcp", {}) if isinstance(configuration, Mapping) else {}
        target_lambda = mcp.get("lambda", {}) if isinstance(mcp, Mapping) else {}
        if not isinstance(target_lambda, Mapping) or target_lambda.get("lambdaArn") != lambda_arn:
            raise SpikeError("Gateway target Lambda ARN differs from the spike")

    def assert_gateway_contract(
        self,
        gateway: Mapping[str, Any],
        role_arn: str,
        pool_id: str,
        client_id: str,
    ) -> None:
        if str(gateway.get("roleArn", "")) != role_arn:
            raise SpikeError("Gateway execution role differs from the spike")
        if str(gateway.get("protocolType", "")) != "MCP":
            raise SpikeError("Gateway protocol is not MCP")
        expected_authorizer = self.api.jwt_authorizer(pool_id, client_id)
        if json.dumps(gateway.get("authorizerConfiguration"), sort_keys=True) != json.dumps(
            expected_authorizer, sort_keys=True
        ):
            raise SpikeError("Gateway CUSTOM_JWT configuration differs from the spike")

    def ensure_target(self, gateway_id: str, lambda_arn: str) -> str:
        target_id = self.state.get("targetId")
        if target_id:
            target = self.wait_target_ready(gateway_id, str(target_id))
            self.assert_target_owned(target, lambda_arn)
            return str(target["targetId"])
        token = self.client_token("target")
        response = self.call_with_propagation(
            "Gateway target",
            lambda: self.api.create_gateway_target(
                gateway_id, self.names.target_name, lambda_arn, token
            ),
        )
        target_id = str(response["targetId"])
        self.save_state(targetId=target_id)
        self.record(
            "gateway_target_created",
            targetId=target_id,
            targetName=self.names.target_name,
            tools=list(model.SPIKE_TOOLS),
            awsRequestId=request_id(response),
        )
        target = self.wait_target_ready(gateway_id, target_id)
        self.assert_target_owned(target, lambda_arn)
        return target_id

    def wait_target_ready(
        self, gateway_id: str, target_id: str, timeout: int = 300
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self.api.get_gateway_target(gateway_id, target_id)
            if target is None:
                raise SpikeError("Gateway target disappeared while waiting for READY")
            status = str(target["status"])
            if status == "READY":
                return target
            if status in GATEWAY_FAILURES:
                raise SpikeError(
                    f"Target entered {status}: {target.get('statusReasons', [])}"
                )
            time.sleep(5)
        raise SpikeError(f"Gateway target did not reach READY within {timeout}s")

    # -- Cedar policies ---------------------------------------------------
    def policy_set(self) -> tuple[model.ToolPolicy, ...]:
        subjects = dict(self.state.get("subjects", {}))
        return model.build_policy_set(names=self.names, subjects=subjects)

    def rendered_statements(self) -> dict[str, str]:
        return model.render_policies(
            self.policy_set(),
            target_name=self.names.target_name,
            gateway_arn=self.require_state("gatewayArn"),
        )

    def ensure_policies(self, engine_id: str) -> dict[str, str]:
        created: dict[str, str] = dict(self.state.get("policyIds", {}))
        for name, statement in self.rendered_statements().items():
            if name in created:
                policy_id = str(created[name])
                policy = self.api.get_policy(engine_id, policy_id)
                if policy is None or str(policy.get("name", "")) != name:
                    raise SpikeError(f"State policy {name} is absent or mismatched")
                actual = str(
                    policy.get("definition", {}).get("cedar", {}).get("statement", "")
                )
                if actual.strip() != statement.strip():
                    raise SpikeError(f"State policy {name} has a different definition")
                self.wait_policy_active(engine_id, policy_id, name)
                continue
            response = self.api.create_policy(
                engine_id, name, statement, self.client_token(f"policy-{name}")
            )
            created[name] = str(response["policyId"])
            self.save_state(policyIds=created)
            self.record(
                "cedar_policy_created",
                policyName=name,
                policyId=created[name],
                validationMode=model.POLICY_VALIDATION_MODE,
                enforcementMode=model.POLICY_ENFORCEMENT_MODE,
                statementFingerprint=model.fingerprint(statement),
                awsRequestId=request_id(response),
            )
            self.wait_policy_active(engine_id, created[name], name)
        return created

    def wait_policy_active(
        self, engine_id: str, policy_id: str, policy_name: str, timeout: int = 180
    ) -> None:
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            policy = self.api.get_policy(engine_id, policy_id)
            if policy is None:
                raise SpikeError(f"Policy {policy_name} disappeared while activating")
            status = str(policy["status"])
            if status == model.POLICY_ACTIVE_STATUS:
                self.record(
                    "cedar_policy_active",
                    policyName=policy_name,
                    activationSeconds=round(time.monotonic() - started, 1),
                )
                return
            if status in model.POLICY_TERMINAL_FAILURES:
                raise SpikeError(
                    f"Policy {policy_name} entered {status}: "
                    f"{policy.get('statusReasons', [])}"
                )
            time.sleep(5)
        raise SpikeError(f"Policy {policy_name} did not reach ACTIVE within {timeout}s")

    # -- MCP probes -------------------------------------------------------
    def mcp(self) -> McpSession:
        return McpSession(self.http, self.require_state("gatewayUrl"))

    def open_session(self, token: str) -> McpSession:
        session = self.mcp()
        status, payload = session.initialize(token)
        if status != 200:
            raise SpikeError(f"MCP initialize returned HTTP {status}")
        negotiated = None
        if isinstance(payload, Mapping) and isinstance(payload.get("result"), Mapping):
            negotiated = payload["result"].get("protocolVersion")
        self.record(
            "mcp_session_initialized",
            httpStatus=status,
            negotiatedProtocolVersion=str(negotiated) if negotiated else None,
        )
        return session

    def observe_decision(
        self, session: McpSession, token: str, case: model.DecisionCase
    ) -> model.ToolOutcome:
        qualified = model.qualified_action(self.names.target_name, case.tool)
        status, payload = session.call_tool(token, qualified, case.arguments)
        if status != 200 or payload is None:
            raise SpikeError(
                f"tools/call for {case.case_id} returned HTTP {status} without a payload"
            )
        return model.classify_tool_result(payload)

    def wait_for_decision(
        self,
        label: str,
        case: model.DecisionCase,
        expected: model.Decision,
        *,
        timeout: int = MODE_PROPAGATION_TIMEOUT,
    ) -> float:
        """Poll a single probe until the expected decision is observed."""
        token = self.access_token(label)
        session = self.open_session(token)
        started = time.monotonic()
        deadline = started + timeout
        last: model.Decision | None = None
        while time.monotonic() < deadline:
            try:
                outcome = self.observe_decision(session, token, case)
            except model.ToolResultError:
                time.sleep(MODE_PROPAGATION_INTERVAL)
                continue
            last = outcome.decision
            if outcome.decision is expected:
                return round(time.monotonic() - started, 1)
            time.sleep(MODE_PROPAGATION_INTERVAL)
        raise SpikeError(
            f"{case.case_id} never reached {expected.value} within {timeout}s "
            f"(last observed {last.value if last else 'none'})"
        )

    # -- verification steps ----------------------------------------------
    def verify_gateway_configuration(self) -> Mapping[str, Any]:
        gateway = self.api.get_gateway(self.require_state("gatewayId"))
        if gateway is None:
            raise SpikeError("Gateway is absent")
        self.assert_gateway_owned(gateway)
        self.assert_gateway_contract(
            gateway,
            self.require_state("gatewayRoleArn"),
            self.require_state("userPoolId"),
            self.require_state("primaryClientId"),
        )
        if str(gateway.get("authorizerType")) != "CUSTOM_JWT":
            raise SpikeError("Gateway is not using CUSTOM_JWT inbound authorization")
        association = gateway.get("policyEngineConfiguration") or {}
        if str(association.get("arn", "")) != self.require_state("policyEngineArn"):
            raise SpikeError("Gateway is associated with a different policy engine")
        if str(association.get("mode")) != model.GATEWAY_MODE_ENFORCE:
            raise SpikeError(
                f"Gateway policy engine mode is {association.get('mode')!r}, expected ENFORCE"
            )
        self.record(
            "gateway_enforce_association_verified",
            gatewayId=str(gateway["gatewayId"]),
            mode=model.GATEWAY_MODE_ENFORCE,
            roleArn=str(gateway["roleArn"]),
        )
        return gateway

    def verify_execution_role(self) -> None:
        actuals = {
            "invoke": self.api.get_role_policy(
                self.names.gateway_role_name, self.names.gateway_invoke_policy_name
            ),
            "engine": self.api.get_role_policy(
                self.names.gateway_role_name,
                self.names.gateway_authz_engine_policy_name,
            ),
            "evaluation": self.api.get_role_policy(
                self.names.gateway_role_name,
                self.names.gateway_authz_gateway_policy_name,
            ),
        }
        if any(document is None for document in actuals.values()):
            raise SpikeError("Gateway role is missing an expected inline policy")
        expected = {
            "invoke": gateway_invoke_policy(self.require_state("lambdaArn")),
            "engine": gateway_authorize_engine_policy(
                self.require_state("policyEngineArn")
            ),
            "evaluation": gateway_authorize_eval_policy(
                self.require_state("policyEngineArn"), self.require_state("gatewayArn")
            ),
        }
        for label, response in actuals.items():
            assert response is not None
            actual = response["PolicyDocument"]
            document = actual if isinstance(actual, Mapping) else json.loads(str(actual))
            if json.dumps(document, sort_keys=True) != json.dumps(expected[label], sort_keys=True):
                raise SpikeError(f"Gateway role {label} policy does not match expectation")
        self.record(
            "gateway_execution_role_verified",
            engineActions=list(GATEWAY_AUTHZ_ENGINE_ACTIONS),
            evaluationActions=list(GATEWAY_AUTHZ_EVAL_ACTIONS),
            invokeResource=self.require_state("lambdaArn"),
            wildcardResources=0,
        )

    def verify_policies(self, engine_id: str) -> None:
        expected = self.rendered_statements()
        summaries = self.api.list_policy_summaries(engine_id)
        foreign = sorted(
            str(policy.get("name", ""))
            for policy in summaries
            if not self.names.owns(str(policy.get("name", "")))
        )
        if foreign:
            raise SpikeError(f"Run-owned PolicyEngine contains foreign policies: {foreign}")
        live = {str(policy["name"]): policy for policy in summaries}
        if set(live) != set(expected):
            raise SpikeError(
                f"Policy set differs: missing={sorted(set(expected) - set(live))}, "
                f"unexpected={sorted(set(live) - set(expected))}"
            )
        policy_by_name = {policy.name: policy for policy in self.policy_set()}
        for name, statement in expected.items():
            summary = live[name]
            if str(summary["status"]) != model.POLICY_ACTIVE_STATUS:
                raise SpikeError(f"Policy {name} is {summary['status']}, expected ACTIVE")
            policy = self.api.get_policy(engine_id, str(summary["policyId"]))
            if policy is None:
                raise SpikeError(f"Policy {name} disappeared after summary listing")
            actual = str(policy.get("definition", {}).get("cedar", {}).get("statement", ""))
            if actual.strip() != statement.strip():
                raise SpikeError(f"Policy {name} statement differs from the generated one")
            model.assert_no_pattern_matching(actual)
            expected_action = model.action_literal(
                self.names.target_name, policy_by_name[name].tool
            )
            if expected_action not in actual:
                raise SpikeError(f"Policy {name} does not name the exact AgentCore action")
            if model.gateway_literal(self.require_state("gatewayArn")) not in actual:
                raise SpikeError(f"Policy {name} does not scope to the exact Gateway ARN")
        self.record(
            "cedar_policies_verified",
            policyCount=len(expected),
            actionFormat="<TargetName>___<ToolName>",
            resourceScope="exact-gateway-arn",
            groupClaimCandidate=model.GROUP_CLAIM_CANDIDATE_REPRESENTATION,
            statementFingerprints={
                name: model.fingerprint(statement) for name, statement in expected.items()
            },
        )

    def verify_tools_list(self) -> None:
        expected = model.expected_listing(
            self.policy_set(),
            names=self.names,
            subjects=dict(self.state.get("subjects", {})),
            target_name=self.names.target_name,
        )
        visible_total = 0
        filtered_total = 0
        for label in model.SUBJECT_LABELS:
            token = self.access_token(label)
            session = self.open_session(token)
            status, payload = session.list_tools(token)
            if status != 200 or payload is None:
                raise SpikeError(f"tools/list for {label} returned HTTP {status}")
            listed = model.extract_tool_names(payload)
            owned = tuple(
                name
                for name in listed
                if name.startswith(f"{self.names.target_name}{model.ACTION_SEPARATOR}")
            )
            unexpected = sorted(set(listed) - set(owned))
            if unexpected:
                raise SpikeError(f"tools/list exposed unexpected tools: {unexpected}")
            wanted = tuple(sorted(expected[label]))
            if owned != wanted:
                raise SpikeError(f"tools/list for {label} returned {owned}, expected {wanted}")
            filtered = [
                model.qualified_action(self.names.target_name, tool)
                for tool in model.SPIKE_TOOLS
                if model.qualified_action(self.names.target_name, tool) not in owned
            ]
            visible_total += len(owned)
            filtered_total += len(filtered)
            self.record(
                "tools_list_filtered",
                subjectLabel=label,
                visibleTools=list(owned),
                filteredTools=filtered,
            )
        if visible_total == 0 or filtered_total == 0:
            raise SpikeError("tools/list proof requires both visible and filtered tools")

    def verify_truth_table(self) -> None:
        subjects = dict(self.state.get("subjects", {}))
        cases = model.build_truth_table(
            self.policy_set(), names=self.names, subjects=subjects
        )
        allowed = 0
        denied = 0
        for label in model.SUBJECT_LABELS:
            token = self.access_token(label)
            session = self.open_session(token)
            for case in (case for case in cases if case.subject_label == label):
                outcome = self.observe_decision(session, token, case)
                if outcome.decision is not case.expected:
                    raise SpikeError(
                        f"{case.case_id}: expected {case.expected.value}, observed "
                        f"{outcome.decision.value}"
                    )
                allowed += 1 if outcome.decision is model.Decision.ALLOW else 0
                denied += 1 if outcome.decision is model.Decision.DENY else 0
                self.record(
                    "cedar_decision_matched",
                    caseId=case.case_id,
                    semantics=case.semantics,
                    tool=case.tool,
                    argumentFields=sorted(case.arguments),
                    expected=case.expected.value,
                    observed=outcome.decision.value,
                    reason=outcome.reason,
                    bodyFingerprint=outcome.body_fingerprint,
                )
        if allowed == 0 or denied == 0:
            raise SpikeError(
                "The truth table must contain both allows and denials to be meaningful"
            )
        self.record(
            "cedar_truth_table_verified",
            caseCount=len(cases),
            allowCount=allowed,
            denyCount=denied,
            anySemanticsCases=[
                case.case_id for case in cases if case.semantics.startswith("ANY")
            ],
            allSemanticsCases=[
                case.case_id for case in cases if case.semantics.startswith("ALL")
            ],
        )
        self.record(
            "group_claim_candidate_live_verified",
            representation=model.GROUP_CLAIM_CANDIDATE_REPRESENTATION,
            exactMemberLabels=[model.ALPHA, model.BETA],
            collisionDeniedLabels=[model.GAMMA, model.DELTA],
        )

    def verify_direct_call_denied(self) -> None:
        """A denied tool must be denied on ``tools/call``, not merely hidden."""
        subjects = dict(self.state.get("subjects", {}))
        case = model.DecisionCase(
            case_id="beta-denied-direct",
            subject_label=model.BETA,
            tool=model.TOOL_UNPERMITTED,
            arguments=model.probe_arguments(mode=model.READONLY_MODE, audit=True),
            expected=model.Decision.DENY,
            semantics="default-deny-direct-call",
        )
        if (
            model.evaluate(
                self.policy_set(),
                subject=subjects[model.BETA],
                groups=model.groups_for_label(self.names, model.BETA),
                tool=case.tool,
                arguments=case.arguments,
            )
            is not model.Decision.DENY
        ):
            raise SpikeError("The direct-call case is not modelled as a denial")
        token = self.access_token(model.BETA)
        session = self.open_session(token)
        outcome = self.observe_decision(session, token, case)
        if outcome.decision is not model.Decision.DENY:
            raise SpikeError("A filtered tool was invocable by direct tools/call")
        self.record(
            "direct_tool_call_denied",
            caseId=case.case_id,
            reason=outcome.reason,
            bodyFingerprint=outcome.body_fingerprint,
        )

    # -- authentication negatives ----------------------------------------
    def probe_unauthenticated(self, token: str | None) -> int:
        session = self.mcp()
        status, _ = session.initialize(token)
        if status == 200:
            status, _ = session.list_tools(token or "")
        return status

    def verify_auth_failures(self) -> None:
        subjects = dict(self.state.get("subjects", {}))
        # Keys deliberately avoid credential-sounding words so that the
        # evidence secret-safety scanner does not reject the record itself.
        failures: dict[str, int] = {}

        failures["missing_header"] = self.probe_unauthenticated(None)
        failures["malformed_compact_jws"] = self.probe_unauthenticated("not-a-valid-jwt")

        valid = self.access_token(model.ALPHA)
        failures["forged_subject_signature_mismatch"] = self.probe_unauthenticated(
            model.tamper_token_subject(valid, subjects[model.BETA])
        )
        delta = self.access_token(model.DELTA)
        failures["forged_group_signature_mismatch"] = self.probe_unauthenticated(
            model.tamper_token_groups(
                delta, [model.group_layout(self.names)["allowed"]]
            )
        )
        failures["unsigned_alg_none"] = self.probe_unauthenticated(
            model.unsigned_token(
                subject=subjects[model.ALPHA],
                issuer=f"{self.config.issuer}/{self.require_state('userPoolId')}",
                client_id=self.require_state("primaryClientId"),
                expires_at=int(time.time()) + 300,
            )
        )
        failures["client_not_allow_listed"] = self.probe_unauthenticated(
            self.foreign_token(model.ALPHA)
        )

        unexpected = {
            name: status for name, status in failures.items() if status != 401
        }
        if unexpected:
            raise SpikeError(
                f"Gateway auth negatives did not return HTTP 401: {unexpected}"
            )
        self.record(
            "auth_failures_rejected",
            statuses=failures,
            positiveTwin="tools_list_filtered",
        )
        self.record(
            "cognito_client_binding_verified",
            claim="client_id",
            rejectedCase="client_not_allow_listed",
            httpStatus=failures["client_not_allow_listed"],
        )
        self.verify_expired_token()

    def verify_expired_token(self) -> None:
        wait_seconds = ACCESS_TOKEN_MINUTES * 60 + 60
        if not self.config.include_expiry_wait:
            self.record_unknown(
                "expired_token_denial_not_attempted",
                "Cognito's minimum access-token lifetime is "
                f"{ACCESS_TOKEN_MINUTES} minutes, so proving expiry costs a "
                f"{wait_seconds}s wall-clock wait. Re-run with "
                "--include-expiry-wait to convert this unknown into evidence.",
            )
            return
        token = self.access_token(model.ALPHA)
        time.sleep(wait_seconds)
        status = self.probe_unauthenticated(token)
        if status != 401:
            raise SpikeError(f"Expired token returned HTTP {status}, expected 401")
        self.record("expired_token_rejected", httpStatus=status, waitedSeconds=wait_seconds)

    # -- commands ---------------------------------------------------------
    def deploy(self) -> None:
        self.allow_mutations(
            "create_role",
            "put_role_policy",
            "create_function",
            "add_permission",
            "create_log_group",
            "put_retention_policy",
            "create_user_pool",
            "create_user_pool_client",
            "create_group",
            "admin_create_user",
            "admin_set_user_password",
            "admin_add_user_to_group",
            "create_policy_engine",
            "create_policy",
            "create_gateway",
            "create_gateway_target",
            "update_gateway",
        )
        self.verify_identity()
        engine_id, engine_arn = self.ensure_policy_engine()
        lambda_arn = self.ensure_lambda()
        role_arn = self.ensure_gateway_role(lambda_arn)
        self.ensure_lambda_permission(role_arn)
        pool_id, primary_id, _ = self.ensure_cognito()
        self.ensure_users(pool_id)
        self.verify_token_group_matrix()
        gateway = self.ensure_gateway(role_arn, pool_id, primary_id)
        self.ensure_target(str(gateway["gatewayId"]), lambda_arn)
        self.attach_authorize_policies(engine_arn, str(gateway["gatewayArn"]))
        # CreatePolicy validates against the schema of an associated Gateway.
        # Association therefore MUST precede policy creation.
        self.associate_log_only(gateway, pool_id, primary_id, engine_arn)
        self.ensure_policies(engine_id)
        self.associate_enforce(gateway, pool_id, primary_id, engine_arn)
        self.record("deployment_ready", gatewayId=str(gateway["gatewayId"]))

    def enforcement_probe(self) -> model.DecisionCase:
        """Fixed default-deny request used as the LOG_ONLY/ENFORCE twin."""
        return model.DecisionCase(
            case_id="delta-denied-tool-mode-twin",
            subject_label=model.DELTA,
            tool=model.TOOL_UNPERMITTED,
            arguments=model.probe_arguments(mode=model.WRITE_MODE, audit=True),
            expected=model.Decision.DENY,
            semantics="mode-twin",
        )

    def associate_log_only(
        self,
        gateway: Mapping[str, Any],
        pool_id: str,
        client_id: str,
        engine_arn: str,
    ) -> None:
        response = self.call_with_propagation(
            "PolicyEngine LOG_ONLY association",
            lambda: self.api.associate_policy_engine(
                gateway, pool_id, client_id, engine_arn, model.GATEWAY_MODE_LOG_ONLY
            ),
        )
        self.record(
            "policy_engine_associated_log_only",
            mode=model.GATEWAY_MODE_LOG_ONLY,
            awsRequestId=request_id(response),
        )
        self.wait_gateway_ready(str(gateway["gatewayId"]))
        seconds = self.wait_for_decision(
            self.enforcement_probe().subject_label,
            self.enforcement_probe(), model.Decision.ALLOW
        )
        self.record(
            "log_only_does_not_enforce",
            caseId=self.enforcement_probe().case_id,
            observed=model.Decision.ALLOW.value,
            propagationSeconds=seconds,
        )

    def associate_enforce(
        self,
        gateway: Mapping[str, Any],
        pool_id: str,
        client_id: str,
        engine_arn: str,
    ) -> None:
        response = self.call_with_propagation(
            "PolicyEngine ENFORCE association",
            lambda: self.api.associate_policy_engine(
                gateway, pool_id, client_id, engine_arn, model.GATEWAY_MODE_ENFORCE
            ),
        )
        self.record(
            "policy_engine_associated_enforce",
            mode=model.GATEWAY_MODE_ENFORCE,
            awsRequestId=request_id(response),
        )
        self.wait_gateway_ready(str(gateway["gatewayId"]))
        seconds = self.wait_for_decision(
            self.enforcement_probe().subject_label,
            self.enforcement_probe(), model.Decision.DENY
        )
        self.record(
            "enforce_denies_unpermitted_request",
            caseId=self.enforcement_probe().case_id,
            observed=model.Decision.DENY.value,
            propagationSeconds=seconds,
            positiveTwin="log_only_does_not_enforce",
        )

    def verify(self) -> None:
        """Read-only. No mutating API is reachable from here."""
        self.allow_mutations()
        self.verify_identity()
        self.check_management_permissions()
        self.verify_gateway_configuration()
        self.verify_execution_role()
        self.verify_policies(self.require_state("policyEngineId"))
        self.verify_token_group_matrix()
        self.verify_tools_list()
        self.verify_truth_table()
        self.verify_direct_call_denied()
        self.verify_auth_failures()
        self.record("verification_passed")

    def rollback(self) -> None:
        """ENFORCE -> LOG_ONLY -> ENFORCE with a behaviour twin at each step."""
        self.allow_mutations(ROLLBACK_MUTATION)
        self.verify_identity()
        gateway = self.verify_gateway_configuration()
        pool_id = self.require_state("userPoolId")
        client_id = self.require_state("primaryClientId")
        engine_arn = self.require_state("policyEngineArn")
        probe = self.enforcement_probe()
        before = self.wait_for_decision(probe.subject_label, probe, model.Decision.DENY)
        self.associate_log_only(gateway, pool_id, client_id, engine_arn)
        self.associate_enforce(gateway, pool_id, client_id, engine_arn)
        after = self.wait_for_decision(probe.subject_label, probe, model.Decision.DENY)
        self.record(
            "rollback_twins_verified",
            caseId=probe.case_id,
            sequence=["ENFORCE:DENY", "LOG_ONLY:ALLOW", "ENFORCE:DENY"],
            enforceBeforeSeconds=before,
            enforceAfterSeconds=after,
        )

    # -- cleanup ----------------------------------------------------------
    def cleanup(self) -> None:
        self.allow_mutations(
            "update_gateway",
            "delete_policy",
            "delete_policy_engine",
            "delete_gateway",
            "delete_gateway_target",
            "delete_function",
            "delete_log_group",
            "delete_role_policy",
            "delete_role",
            "delete_user_pool",
        )
        self.verify_identity()
        self.cleanup_gateway()
        self.cleanup_policy_engine()
        self.cleanup_compute()
        self.cleanup_identity()
        residue = self.residual_inventory()
        if any(residue.values()):
            raise SpikeError(f"Run-owned resources remain after cleanup: {residue}")
        self.record("zero_residual_verified", inventory=residue)
        self.state = {}
        self.state_store.write(self.state)

    def cleanup_gateway(self) -> None:
        """Detach the engine, then remove target and gateway in dependency order."""
        gateway_id = self.state.get("gatewayId")
        gateway = (
            self.api.get_gateway(str(gateway_id))
            if gateway_id
            else self.api.find_gateway(self.names.gateway_name)
        )
        if gateway is None:
            return
        if not gateway.get("gatewayArn"):
            gateway = self.api.get_gateway(str(gateway["gatewayId"]))
            if gateway is None:
                return
        self.assert_gateway_owned(gateway)
        gateway_id = str(gateway["gatewayId"])
        pool_id = self.state.get("userPoolId")
        client_id = self.state.get("primaryClientId")
        if gateway.get("policyEngineConfiguration") and pool_id and client_id:
            response = self.call_with_propagation(
                "PolicyEngine detachment",
                lambda: self.api.detach_policy_engine(
                    gateway, str(pool_id), str(client_id)
                ),
            )
            self.record(
                "policy_engine_disassociated", awsRequestId=request_id(response)
            )
            self.wait_gateway_ready(gateway_id)
        for target in self.api.list_gateway_targets(gateway_id):
            name = str(target.get("name", ""))
            if name != self.names.target_name:
                raise SpikeError(f"Refusing to delete unexpected gateway target {name!r}")
            self.api.delete_gateway_target(gateway_id, str(target["targetId"]))
            self.wait_absent(
                f"target {target['targetId']}",
                lambda tid=str(target["targetId"]): self.api.get_gateway_target(
                    gateway_id, tid
                ),
            )
            self.record("gateway_target_deleted", targetId=str(target["targetId"]))
        self.api.delete_gateway(gateway_id)
        self.wait_absent("gateway", lambda: self.api.get_gateway(gateway_id))
        self.record("gateway_deleted", gatewayId=gateway_id)

    def cleanup_policy_engine(self) -> None:
        """Delete owned policies asynchronously before the owned engine."""
        engine_id = self.state.get("policyEngineId")
        engine = self.api.get_policy_engine(str(engine_id)) if engine_id else None
        if engine is None:
            summary = self.api.find_policy_engine(self.names.engine_name)
            if summary is None:
                return
            engine = self.api.get_policy_engine(str(summary["policyEngineId"]))
            if engine is None:
                return
        engine_id, _ = self.assert_policy_engine_owned(engine)
        allowed_names = set(self.names.policy_names)
        for policy in self.api.list_policy_summaries(engine_id):
            name = str(policy.get("name", ""))
            policy_id = str(policy.get("policyId", ""))
            if name not in allowed_names or not policy_id:
                raise SpikeError(f"Refusing to delete unexpected policy {name!r}")
            self.api.delete_policy(engine_id, policy_id)
            self.wait_absent(
                f"policy {policy_id}",
                lambda pid=policy_id: self.api.get_policy_summary(engine_id, pid),
                timeout=DELETE_ABSENT_TIMEOUT,
            )
            self.record("cedar_policy_deleted", policyName=name)
        remaining = self.api.list_policy_summaries(engine_id)
        if remaining:
            raise SpikeError(f"{len(remaining)} policies remain in the engine")
        self.api.delete_policy_engine(engine_id)
        self.wait_absent(
            "policy engine",
            lambda: self.api.get_policy_engine(engine_id),
            timeout=DELETE_ABSENT_TIMEOUT,
        )
        self.record("policy_engine_deleted", policyEngineId=engine_id)

    def cleanup_compute(self) -> None:
        function = self.api.get_function(self.names.lambda_name)
        if function is not None:
            self.assert_lambda_owned(function)
            self.api.delete_function(self.names.lambda_name)
            self.wait_absent(
                "Lambda function",
                lambda: self.api.get_function(self.names.lambda_name),
            )
            self.record("echo_lambda_deleted", functionName=self.names.lambda_name)
        for role_name, policy_names in (
            (
                self.names.gateway_role_name,
                (
                    self.names.gateway_invoke_policy_name,
                    self.names.gateway_authz_engine_policy_name,
                    self.names.gateway_authz_gateway_policy_name,
                ),
            ),
            (self.names.lambda_role_name, (self.names.lambda_logs_policy_name,)),
        ):
            role = self.api.get_role(role_name)
            if role is None:
                continue
            tags = {item["Key"]: item["Value"] for item in role.get("Tags", [])}
            self.assert_owned_tags(tags, f"IAM role {role_name}")
            for policy_name in policy_names:
                self.api.delete_role_policy(role_name, policy_name)
            self.api.delete_role(role_name)
            self.record("iam_role_deleted", roleName=role_name)
        log_group = self.api.get_log_group(self.names.log_group_name)
        if log_group is not None:
            self.assert_owned_tags(
                self.api.get_log_group_tags(self.names.log_group_name),
                f"log group {self.names.log_group_name}",
            )
            self.api.delete_log_group(self.names.log_group_name)
            self.wait_absent(
                "Lambda log group",
                lambda: self.api.get_log_group(self.names.log_group_name),
            )
            self.record("lambda_log_group_deleted", logGroupName=self.names.log_group_name)

    def cleanup_identity(self) -> None:
        """Deleting the owned pool removes its clients, groups, and users."""
        pool_id = self.state.get("userPoolId")
        if not pool_id:
            summary = self.api.find_user_pool(self.names.user_pool_name)
            if summary is None:
                return
            pool_id = summary.get("Id")
        if not pool_id:
            raise SpikeError("Discovered Cognito pool has no identifier")
        pool = self.api.describe_user_pool(str(pool_id))
        if pool is None:
            return
        if str(pool.get("Name", "")) != self.names.user_pool_name:
            raise SpikeError("Refusing to delete a user pool with a different name")
        self.assert_owned_tags(pool.get("UserPoolTags", {}), "Cognito User Pool")
        self.api.delete_user_pool(str(pool_id))
        self.record("cognito_pool_deleted", userPoolId=str(pool_id))

    def wait_absent(
        self,
        label: str,
        getter: Callable[[], Any],
        timeout: int = DELETE_ABSENT_TIMEOUT,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getter() is None:
                return
            time.sleep(DELETE_ABSENT_INTERVAL)
        raise SpikeError(f"{label} was not deleted within {timeout}s")

    def residual_inventory(self) -> dict[str, list[str]]:
        """Read-only sweep of every resource class this spike can create."""
        gateway = self.api.find_gateway(self.names.gateway_name)
        engine = self.api.find_policy_engine(self.names.engine_name)
        pool = self.api.find_user_pool(self.names.user_pool_name)
        function = self.api.get_function(self.names.lambda_name)
        log_group = self.api.get_log_group(self.names.log_group_name)
        roles = [
            name
            for name in (self.names.gateway_role_name, self.names.lambda_role_name)
            if self.api.get_role(name) is not None
        ]
        return {
            "gateways": [self.names.gateway_name] if gateway else [],
            "policyEngines": [self.names.engine_name] if engine else [],
            "userPools": [self.names.user_pool_name] if pool else [],
            "functions": [self.names.lambda_name] if function else [],
            "logGroups": [self.names.log_group_name] if log_group else [],
            "roles": roles,
        }

    def close(self) -> None:
        self._passwords.clear()
        self.http.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

COMMANDS = ("deploy", "verify", "rollback", "cleanup", "all")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--prefix", default="aiaf-pe-spike")
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    parser.add_argument(
        "--include-expiry-wait",
        action="store_true",
        help=(
            "Also prove expired-token denial. Costs a "
            f"{ACCESS_TOKEN_MINUTES}-minute wall-clock wait."
        ),
    )
    return parser.parse_args(argv)


def run_command(spike: PolicyEngineSpike, command: str) -> str:
    """Dispatch one bounded command. ``all`` always cleans up in ``finally``."""
    if command == "deploy":
        spike.deploy()
        return "deploy-passed"
    if command == "verify":
        spike.verify()
        return "verify-passed"
    if command == "rollback":
        spike.rollback()
        return "rollback-passed"
    if command == "cleanup":
        spike.cleanup()
        return "cleanup-passed"
    primary: Exception | None = None
    try:
        spike.deploy()
        spike.verify()
        spike.rollback()
    except Exception as error:  # noqa: BLE001 - cleanup must still run
        primary = error
    finally:
        try:
            spike.cleanup()
        except Exception as cleanup_error:
            if primary is not None:
                raise SpikeError(
                    f"Run failed: {primary}; cleanup also failed: {cleanup_error}"
                ) from cleanup_error
            raise
    if primary is not None:
        raise primary
    return "passed"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except Exception as error:  # noqa: BLE001 - no evidence file exists yet
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    spike = PolicyEngineSpike(config)
    try:
        status = run_command(spike, args.command)
        spike.evidence.finish(status)
        print(f"Evidence: {config.evidence_path}")
        return 0
    except Exception as error:  # noqa: BLE001 - single fail-closed exit path
        spike.evidence.add(
            "failure",
            errorType=type(error).__name__,
            errorCode=aws_error_code(error) if isinstance(error, ClientError) else None,
            message=model.sanitize_error(str(error), prefix=args.command),
        )
        spike.evidence.finish("failed")
        print(f"FAIL: {error}", file=sys.stderr)
        print(f"Evidence: {config.evidence_path}", file=sys.stderr)
        return 1
    finally:
        spike.close()


if __name__ == "__main__":
    raise SystemExit(main())
