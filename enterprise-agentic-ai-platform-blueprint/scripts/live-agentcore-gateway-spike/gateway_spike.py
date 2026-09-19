#!/usr/bin/env python3
"""Real AgentCore Gateway inference/rate-limit compatibility spike.

This is intentionally a narrow Platform-account probe. It creates an IAM-
authorized Gateway, attaches the Bedrock Mantle inference connector, proves
non-streaming and streaming OpenAI-compatible calls, applies a zero-rate rule
to the known-good model, proves an exact HTTP 429, and removes every resource it
created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import boto3
import botocore
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError
from botocore.httpsession import URLLib3Session

REQUIRED_BOTO3_VERSION = "1.43.97"
DEFAULT_MODEL = "bedrock-mantle/openai.gpt-oss-120b"
MCP_VERSION = "2025-11-25"
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
TERMINAL_FAILURES = {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}


class SpikeError(RuntimeError):
    """A fail-closed spike error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_id(response: Mapping[str, Any]) -> str | None:
    metadata = response.get("ResponseMetadata", {})
    value = metadata.get("RequestId") if isinstance(metadata, Mapping) else None
    return str(value) if value else None


def aws_error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", "Unknown"))


@dataclass(frozen=True)
class Config:
    account_id: str
    region: str
    prefix: str
    model: str
    state_path: Path
    evidence_path: Path

    @property
    def gateway_name(self) -> str:
        return f"{self.prefix}-gateway"

    @property
    def role_name(self) -> str:
        return f"{self.prefix}-gateway-role"

    @property
    def role_policy_name(self) -> str:
        return f"{self.prefix}-mantle"

    @property
    def target_name(self) -> str:
        return "bedrock-mantle"

    @property
    def rate_limit_id(self) -> str:
        return f"{self.prefix}-known-model-block"

    @property
    def tags(self) -> dict[str, str]:
        return {
            "application-id": self.prefix,
            "agent-id": f"{self.prefix}-spike",
            "tenant-id": "platform",
            "cost-centre": "agentic-ai-platform",
            "environment": "nonprod",
        }


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SpikeError(f"State at {self.path} is not a JSON object")
        return value

    def write(self, value: Mapping[str, Any]) -> None:
        temporary = self.path.with_suffix(f"{self.path.suffix}.next")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)


class Evidence:
    _FORBIDDEN_KEYS = {"authorization", "token", "secret", "accesskey", "password"}

    def __init__(self, path: Path, config: Config) -> None:
        self.store = JsonStore(path)
        current = self.store.read()
        self.document: dict[str, Any] = current or {
            "schemaVersion": 1,
            "run": {
                "prefix": config.prefix,
                "region": config.region,
                "accountSuffix": config.account_id[-4:],
                "model": config.model,
                "boto3Version": boto3.__version__,
                "botocoreVersion": botocore.__version__,
                "startedAt": utc_now(),
            },
            "events": [],
        }

    def add(self, event: str, **details: Any) -> None:
        for key in details:
            normalized = key.lower().replace("_", "")
            if any(forbidden in normalized for forbidden in self._FORBIDDEN_KEYS):
                raise SpikeError(f"Evidence field {key!r} could contain credentials")
        records = self.document.setdefault("events", [])
        if not isinstance(records, list):
            raise SpikeError("Evidence events field is not an array")
        records.append({"at": utc_now(), "event": event, **details})
        self.store.write(self.document)

    def has_event(self, event: str) -> bool:
        records = self.document.get("events", [])
        return isinstance(records, list) and any(
            isinstance(record, Mapping) and record.get("event") == event
            for record in records
        )

    def finish(self, status: str) -> None:
        self.document["finishedAt"] = utc_now()
        self.document["status"] = status
        self.store.write(self.document)


class GatewaySpike:
    throttle_positive_twin = "non_streaming_inference_passed"

    def __init__(self, config: Config) -> None:
        if boto3.__version__ != REQUIRED_BOTO3_VERSION:
            raise SpikeError(
                f"boto3 {REQUIRED_BOTO3_VERSION} is required; found {boto3.__version__}"
            )
        if not hasattr(boto3.client("bedrock-agentcore-control", region_name=config.region), "create_gateway_rate_limit"):
            raise SpikeError("Installed SDK does not expose create_gateway_rate_limit")
        self.config = config
        self.session = boto3.Session(region_name=config.region)
        self.sts = self.session.client("sts")
        self.iam = self.session.client("iam")
        self.control = self.session.client("bedrock-agentcore-control")
        self.http = URLLib3Session()
        self.state_store = JsonStore(config.state_path)
        self.state = self.state_store.read()
        self.evidence = Evidence(config.evidence_path, config)

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state_store.write(self.state)

    def client_token(self, operation: str) -> str:
        state_key = f"{operation}ClientToken"
        existing = self.state.get(state_key)
        if existing:
            return str(existing)
        # AgentCore retains idempotency tokens after resource deletion. The
        # token must be new for each clean run, but persisted before the API
        # call so a transport retry cannot create a duplicate resource.
        token = hashlib.sha256(
            f"{self.config.prefix}:{operation}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()
        self.save_state(**{state_key: token})
        return token

    def verify_identity(self) -> None:
        identity = self.sts.get_caller_identity()
        actual = str(identity["Account"])
        if actual != self.config.account_id:
            raise SpikeError(
                f"Authenticated account {actual} does not match expected {self.config.account_id}"
            )
        self.evidence.add(
            "identity_verified",
            accountSuffix=actual[-4:],
            principalType="assumed-role",
            awsRequestId=request_id(identity),
        )

    def _owned_iam_role(self) -> dict[str, Any] | None:
        try:
            response = self.iam.get_role(RoleName=self.config.role_name)
        except ClientError as error:
            if aws_error_code(error) == "NoSuchEntity":
                return None
            raise
        role = response["Role"]
        tags = {item["Key"]: item["Value"] for item in role.get("Tags", [])}
        if any(tags.get(key) != value for key, value in self.config.tags.items()):
            raise SpikeError(
                f"Refusing to adopt IAM role {self.config.role_name}: ownership tags differ"
            )
        return role

    def ensure_role(self) -> str:
        existing = self._owned_iam_role()
        if existing:
            role_arn = str(existing["Arn"])
            self.save_state(roleArn=role_arn, roleName=self.config.role_name)
            return role_arn

        partition = self.session.get_partition_for_region(self.config.region)
        source_arn = (
            f"arn:{partition}:bedrock-agentcore:{self.config.region}:"
            f"{self.config.account_id}:gateway/{self.config.gateway_name}-*"
        )
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "GatewayAssumeRolePolicy",
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": self.config.account_id},
                        "ArnLike": {"aws:SourceArn": source_arn},
                    },
                }
            ],
        }
        response = self.iam.create_role(
            RoleName=self.config.role_name,
            Description="Ephemeral AgentCore Gateway Bedrock Mantle compatibility spike",
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": key, "Value": value} for key, value in self.config.tags.items()],
        )
        role_arn = str(response["Role"]["Arn"])
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "InvokeBedrockMantle",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-mantle:ListModels",
                        "bedrock-mantle:CreateInference",
                    ],
                    "Resource": "*",
                }
            ],
        }
        self.iam.put_role_policy(
            RoleName=self.config.role_name,
            PolicyName=self.config.role_policy_name,
            PolicyDocument=json.dumps(policy),
        )
        self.save_state(roleArn=role_arn, roleName=self.config.role_name)
        self.evidence.add(
            "gateway_role_created",
            roleName=self.config.role_name,
            awsRequestId=request_id(response),
        )
        return role_arn

    def _gateway_by_name(self) -> dict[str, Any] | None:
        next_token: str | None = None
        while True:
            kwargs = {"maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.control.list_gateways(**kwargs)
            for gateway in response.get("items", []):
                if gateway.get("name") == self.config.gateway_name:
                    return gateway
            next_token = response.get("nextToken")
            if not next_token:
                return None

    def _assert_gateway_owned(self, gateway: Mapping[str, Any]) -> None:
        if gateway.get("name") != self.config.gateway_name:
            raise SpikeError("Gateway name does not match the spike")
        gateway_id = str(gateway.get("gatewayId", ""))
        if not gateway_id:
            raise SpikeError("Gateway summary has no gatewayId")
        # ListGateways returns a summary without gatewayArn. Resolve the full
        # resource before checking tags; never weaken ownership validation.
        full_gateway = (
            gateway
            if gateway.get("gatewayArn")
            else self.control.get_gateway(gatewayIdentifier=gateway_id)
        )
        if full_gateway.get("name") != self.config.gateway_name:
            raise SpikeError("Resolved Gateway name does not match the spike")
        arn = str(full_gateway.get("gatewayArn", ""))
        if not arn:
            raise SpikeError("Resolved Gateway has no gatewayArn")
        response = self.control.list_tags_for_resource(resourceArn=arn)
        tags = response.get("tags", {})
        if any(tags.get(key) != value for key, value in self.config.tags.items()):
            raise SpikeError(
                f"Refusing to operate on Gateway {arn}: ownership tags differ"
            )

    def wait_gateway(self, gateway_id: str, desired: set[str], timeout: int = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.control.get_gateway(gatewayIdentifier=gateway_id)
            status = str(response["status"])
            if status in desired:
                return response
            if status in TERMINAL_FAILURES:
                raise SpikeError(f"Gateway entered {status}: {response.get('statusReasons', [])}")
            time.sleep(5)
        raise SpikeError(f"Gateway did not reach {sorted(desired)} within {timeout}s")

    def ensure_gateway(self, role_arn: str) -> dict[str, Any]:
        existing = self._gateway_by_name()
        if existing:
            if not self.state.get("gatewayId"):
                raise SpikeError(
                    f"Gateway {self.config.gateway_name} exists without this run's state file"
                )
            self._assert_gateway_owned(existing)
            return self.wait_gateway(str(existing["gatewayId"]), {"READY"})

        response = self.control.create_gateway(
            name=self.config.gateway_name,
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration={"mcp": {"supportedVersions": [MCP_VERSION]}},
            authorizerType="AWS_IAM",
            description="Ephemeral inference and rate-limit compatibility spike",
            tags=self.config.tags,
            clientToken=self.client_token("gateway"),
        )
        self.save_state(
            gatewayId=response["gatewayId"],
            gatewayArn=response["gatewayArn"],
            gatewayUrl=response["gatewayUrl"],
        )
        self.evidence.add(
            "gateway_created",
            gatewayId=response["gatewayId"],
            gatewayArn=response["gatewayArn"],
            awsRequestId=request_id(response),
        )
        return self.wait_gateway(str(response["gatewayId"]), {"READY"})

    def wait_target(self, gateway_id: str, target_id: str, timeout: int = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.control.get_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
            )
            status = str(response["status"])
            if status == "READY":
                return response
            if status in TERMINAL_FAILURES | {"FAILED"}:
                raise SpikeError(f"Target entered {status}: {response.get('statusReasons', [])}")
            time.sleep(5)
        raise SpikeError(f"Target did not reach READY within {timeout}s")

    def ensure_target(self, gateway_id: str) -> dict[str, Any]:
        target_id = self.state.get("targetId")
        if target_id:
            return self.wait_target(gateway_id, str(target_id))

        # Official samples wait for role-policy propagation before connector
        # discovery calls bedrock-mantle:ListModels.
        time.sleep(45)
        response = self.control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=self.config.target_name,
            targetConfiguration={
                "inference": {
                    "connector": {"source": {"connectorId": "bedrock-mantle"}}
                }
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
            clientToken=self.client_token("target"),
        )
        self.save_state(targetId=response["targetId"])
        self.evidence.add(
            "inference_target_created",
            targetId=response["targetId"],
            targetName=self.config.target_name,
            awsRequestId=request_id(response),
        )
        return self.wait_target(gateway_id, str(response["targetId"]))

    def _gateway_url(self, path: str) -> str:
        gateway_id = self.state.get("gatewayId")
        if not gateway_id:
            raise SpikeError("Gateway ID is absent from state")
        return (
            f"https://{gateway_id}.gateway.bedrock-agentcore."
            f"{self.config.region}.amazonaws.com{path}"
        )

    def signed_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        accept: str = "application/json",
    ) -> tuple[int, Mapping[str, str], bytes]:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise SpikeError("No AWS credentials resolved by Boto3")
        payload = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else b""
        )
        headers = {"Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = AWSRequest(
            method=method,
            url=self._gateway_url(path),
            data=payload,
            headers=headers,
        )
        SigV4Auth(
            credentials.get_frozen_credentials(),
            "bedrock-agentcore",
            self.config.region,
        ).add_auth(request)
        response = self.http.send(request.prepare())
        return response.status_code, response.headers, response.content

    @staticmethod
    def response_request_id(headers: Mapping[str, str]) -> str | None:
        for key in ("x-amzn-requestid", "x-amz-request-id", "x-amzn-trace-id"):
            value = headers.get(key)
            if value:
                return str(value)
        return None

    def verify_models(self) -> None:
        status, headers, content = self.signed_request("GET", "/inference/v1/models")
        if status != 200:
            raise SpikeError(f"Model discovery returned HTTP {status}")
        payload = json.loads(content)
        model_ids = [
            str(item["id"])
            for item in payload.get("data", [])
            if item.get("id")
        ]
        if self.config.model not in model_ids:
            model_name = self.config.model.rsplit("/", 1)[-1]
            related_models = sorted(
                model_id
                for model_id in model_ids
                if model_name in model_id or "gpt-oss" in model_id
            )[:10]
            self.evidence.add(
                "model_discovery_mismatch",
                httpStatus=status,
                modelCount=len(model_ids),
                requestedModel=self.config.model,
                relatedModels=related_models,
                sampleModels=sorted(model_ids)[:5],
                awsRequestId=self.response_request_id(headers),
            )
            raise SpikeError(
                f"Required model {self.config.model!r} absent from Gateway discovery"
            )
        self.evidence.add(
            "model_discovery_passed",
            httpStatus=status,
            modelCount=len(model_ids),
            selectedModel=self.config.model,
            awsRequestId=self.response_request_id(headers),
        )

    def invoke(self, *, stream: bool) -> tuple[int, Mapping[str, str], bytes]:
        return self.signed_request(
            "POST",
            "/inference/v1/chat/completions",
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Reply with exactly the word verified.",
                    }
                ],
                "max_tokens": 16,
                "stream": stream,
            },
            accept="text/event-stream" if stream else "application/json",
        )

    def verify_positive_invocations(self) -> None:
        status, headers, content = self.invoke(stream=False)
        if status != 200:
            raise SpikeError(f"Non-streaming inference returned HTTP {status}")
        payload = json.loads(content)
        if not payload.get("choices"):
            raise SpikeError("Non-streaming inference returned no choices")
        self.evidence.add(
            "non_streaming_inference_passed",
            httpStatus=status,
            responseBytes=len(content),
            awsRequestId=self.response_request_id(headers),
        )

        status, headers, content = self.invoke(stream=True)
        if status != 200:
            raise SpikeError(f"Streaming inference returned HTTP {status}")
        if b"data:" not in content:
            raise SpikeError("Streaming response did not use OpenAI-compatible SSE")
        self.evidence.add(
            "streaming_inference_passed",
            httpStatus=status,
            responseBytes=len(content),
            awsRequestId=self.response_request_id(headers),
        )

    def wait_rate_limit(self, gateway_id: str, timeout: int = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.control.get_gateway_rate_limit(
                gatewayIdentifier=gateway_id,
                rateLimitId=self.config.rate_limit_id,
            )
            status = str(response["status"])
            if status == "ACTIVE":
                return response
            if status == "DELETING":
                raise SpikeError("Rate limit unexpectedly entered DELETING")
            time.sleep(3)
        raise SpikeError(f"Rate limit did not reach ACTIVE within {timeout}s")

    def ensure_zero_rate_limit(self, gateway_id: str) -> None:
        if self.state.get("rateLimitId"):
            self.wait_rate_limit(gateway_id)
            return
        response = self.control.create_gateway_rate_limit(
            gatewayIdentifier=gateway_id,
            rateLimitId=self.config.rate_limit_id,
            description="Block known-good model to prove exact Gateway throttling",
            dimensionKeys=["qualifiedModelId"],
            entries=[
                {
                    "dimensions": {
                        "qualifiedModelId": self.config.model.split("/", 1)[1]
                    },
                    "requests": [{"rate": 0, "period": "second"}],
                },
                {
                    "dimensions": {"qualifiedModelId": "*"},
                    "requests": [{"rate": 10, "period": "minute"}],
                    "tokens": [{"rate": 10_000, "period": "minute"}],
                },
            ],
            clientToken=self.client_token("rateLimit"),
        )
        self.save_state(rateLimitId=response["rateLimitId"])
        self.evidence.add(
            "zero_rate_limit_created",
            rateLimitId=response["rateLimitId"],
            awsRequestId=request_id(response),
        )
        self.wait_rate_limit(gateway_id)

    def verify_exact_throttle(self) -> None:
        # Give the data plane time to observe ACTIVE control-plane state.
        time.sleep(10)
        status, headers, content = self.invoke(stream=False)
        if status != 429:
            raise SpikeError(
                f"Expected Gateway rate-limit HTTP 429, received {status} ({len(content)} bytes)"
            )
        if not self.evidence.has_event(self.throttle_positive_twin):
            raise SpikeError(
                f"Positive-twin evidence {self.throttle_positive_twin!r} is absent"
            )
        self.evidence.add(
            "zero_rate_limit_denied_known_good_model",
            httpStatus=status,
            responseBytes=len(content),
            awsRequestId=self.response_request_id(headers),
            positiveTwin=self.throttle_positive_twin,
        )

    def deploy(self) -> None:
        self.verify_identity()
        role_arn = self.ensure_role()
        gateway = self.ensure_gateway(role_arn)
        self.ensure_target(str(gateway["gatewayId"]))
        self.evidence.add("deployment_ready", gatewayId=gateway["gatewayId"])

    def verify(self) -> None:
        self.verify_identity()
        self.verify_models()
        self.verify_positive_invocations()
        gateway_id = str(self.state.get("gatewayId", ""))
        if not gateway_id:
            raise SpikeError("Gateway ID is absent from state")
        self.ensure_zero_rate_limit(gateway_id)
        self.verify_exact_throttle()

    def _wait_deleted(
        self,
        description: str,
        getter: Callable[[], Mapping[str, Any]],
        timeout: int = 300,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                getter()
            except ClientError as error:
                if aws_error_code(error) in {"ResourceNotFoundException", "NotFoundException"}:
                    return
                raise
            time.sleep(5)
        raise SpikeError(f"{description} was not deleted within {timeout}s")

    def cleanup(self) -> None:
        self.verify_identity()
        gateway = self._gateway_by_name()
        if gateway:
            self._assert_gateway_owned(gateway)
            gateway_id = str(gateway["gatewayId"])
            rate_limit_id = self.state.get("rateLimitId")
            if rate_limit_id:
                try:
                    response = self.control.delete_gateway_rate_limit(
                        gatewayIdentifier=gateway_id,
                        rateLimitId=str(rate_limit_id),
                    )
                    self.evidence.add(
                        "rate_limit_delete_requested",
                        rateLimitId=rate_limit_id,
                        awsRequestId=request_id(response),
                    )
                except ClientError as error:
                    if aws_error_code(error) != "ResourceNotFoundException":
                        raise
                self._wait_deleted(
                    "Rate limit",
                    lambda: self.control.get_gateway_rate_limit(
                        gatewayIdentifier=gateway_id,
                        rateLimitId=str(rate_limit_id),
                    ),
                )

            target_id = self.state.get("targetId")
            if target_id:
                try:
                    response = self.control.delete_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=str(target_id),
                    )
                    self.evidence.add(
                        "target_delete_requested",
                        targetId=target_id,
                        awsRequestId=request_id(response),
                    )
                except ClientError as error:
                    if aws_error_code(error) != "ResourceNotFoundException":
                        raise
                self._wait_deleted(
                    "Gateway target",
                    lambda: self.control.get_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=str(target_id),
                    ),
                )

            response = self.control.delete_gateway(gatewayIdentifier=gateway_id)
            self.evidence.add(
                "gateway_delete_requested",
                gatewayId=gateway_id,
                awsRequestId=request_id(response),
            )
            self._wait_deleted(
                "Gateway",
                lambda: self.control.get_gateway(gatewayIdentifier=gateway_id),
            )

        role = self._owned_iam_role()
        if role:
            try:
                self.iam.delete_role_policy(
                    RoleName=self.config.role_name,
                    PolicyName=self.config.role_policy_name,
                )
            except ClientError as error:
                if aws_error_code(error) != "NoSuchEntity":
                    raise
            self.iam.delete_role(RoleName=self.config.role_name)
            self.evidence.add("gateway_role_deleted", roleName=self.config.role_name)

        if self._gateway_by_name() is not None or self._owned_iam_role() is not None:
            raise SpikeError("Run-owned resources remain after cleanup")
        self.state = {}
        self.state_store.write(self.state)
        self.evidence.add("zero_residue_verified")

    def close(self) -> None:
        self.http.close()


def validate_config(
    args: argparse.Namespace,
    *,
    required_model_prefix: str | None = "bedrock-mantle/",
) -> Config:
    if not ACCOUNT_PATTERN.fullmatch(args.account_id):
        raise SpikeError("--account-id must contain exactly 12 digits")
    if not PREFIX_PATTERN.fullmatch(args.prefix):
        raise SpikeError(
            "--prefix must start with a letter and contain 3-40 lowercase letters, digits, or hyphens"
        )
    if required_model_prefix is not None and not args.model.startswith(
        required_model_prefix
    ):
        raise SpikeError(
            "Phase A requires a target-qualified "
            f"{required_model_prefix.removesuffix('/')} model"
        )
    scratch = os.environ.get("KIROCREW_SCRATCH")
    if not scratch:
        raise SpikeError("KIROCREW_SCRATCH must be set; refusing shared /tmp state")
    scratch_path = Path(scratch)
    return Config(
        account_id=args.account_id,
        region=args.region,
        prefix=args.prefix,
        model=args.model,
        state_path=Path(args.state_file) if args.state_file else scratch_path / f"{args.prefix}-gateway-state.json",
        evidence_path=(
            Path(args.evidence_file)
            if args.evidence_file
            else scratch_path / f"{args.prefix}-gateway-evidence.json"
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "verify", "cleanup", "all"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--prefix", default="aiaf-live-20260918")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = validate_config(args)
    spike = GatewaySpike(config)
    status = "failed"
    try:
        if args.command == "deploy":
            spike.deploy()
            status = "deploy-passed"
        elif args.command == "verify":
            spike.verify()
            status = "verify-passed"
        elif args.command == "cleanup":
            spike.cleanup()
            status = "cleanup-passed"
        else:
            verification_error: Exception | None = None
            try:
                spike.deploy()
                spike.verify()
            except Exception as error:
                verification_error = error
            try:
                spike.cleanup()
            except Exception as cleanup_error:
                if verification_error is not None:
                    raise SpikeError(
                        f"Verification failed: {verification_error}; "
                        f"cleanup also failed: {cleanup_error}"
                    ) from cleanup_error
                raise
            if verification_error is not None:
                raise verification_error
            status = "passed"
        spike.evidence.finish(status)
        print(f"Evidence: {config.evidence_path}")
        return 0
    except Exception as error:
        spike.evidence.add(
            "failure",
            errorType=type(error).__name__,
            errorCode=aws_error_code(error) if isinstance(error, ClientError) else None,
            message=str(error),
        )
        spike.evidence.finish("failed")
        print(f"FAIL: {error}", file=sys.stderr)
        print(f"Evidence: {config.evidence_path}", file=sys.stderr)
        return 1
    finally:
        spike.close()


if __name__ == "__main__":
    raise SystemExit(main())
