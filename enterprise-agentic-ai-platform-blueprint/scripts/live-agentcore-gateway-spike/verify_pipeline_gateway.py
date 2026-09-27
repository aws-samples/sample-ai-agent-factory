#!/usr/bin/env python3
"""Verify a pipeline-owned AgentCore inference Gateway without mutating it."""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import replace

from botocore.exceptions import ClientError

from cognito_litellm_spike import CognitoLiteLLMSpike
from gateway_spike import (
    Config,
    SpikeError,
    aws_error_code,
    request_id,
    validate_config,
)

GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_OUTPUTS = {
    "CognitoClientId",
    "CognitoUserPoolId",
    "GatewayArn",
    "GatewayIdentifier",
    "GatewayUrl",
    "InferenceTargetId",
    "InferenceTargetName",
    "OAuthScope",
    "RateLimitId",
    "TokenEndpoint",
}
SUCCESSFUL_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
REQUIRED_TAGS = {
    "application-id",
    "agent-id",
    "tenant-id",
    "cost-centre",
    "environment",
}


class PipelineGatewayVerifier(CognitoLiteLLMSpike):
    """Read and invoke an existing pipeline stack; never create, update, or delete."""

    def __init__(
        self,
        config: Config,
        *,
        stack_name: str,
        expected_git_head: str,
        expected_environment: str,
    ) -> None:
        super().__init__(config)
        self.cloudformation = self.session.client("cloudformation")
        self.stack_name = stack_name
        self.expected_git_head = expected_git_head
        self.expected_environment = expected_environment
        self._oauth_scope = ""
        self._token_endpoint = ""
        self.outputs: dict[str, str] = {}

    @property
    def scope(self) -> str:
        if not self._oauth_scope:
            raise SpikeError("OAuth scope is unavailable before stack validation")
        return self._oauth_scope

    def token_endpoint(self) -> str:
        if not self._token_endpoint:
            raise SpikeError("Token endpoint is unavailable before stack validation")
        return self._token_endpoint

    def load_stack(self) -> None:
        response = self.cloudformation.describe_stacks(StackName=self.stack_name)
        stacks = response.get("Stacks", [])
        if len(stacks) != 1:
            raise SpikeError(f"Expected one stack named {self.stack_name!r}")
        stack = stacks[0]
        stack_status = str(stack.get("StackStatus"))
        if stack_status not in SUCCESSFUL_STACK_STATUSES:
            expected = " or ".join(sorted(SUCCESSFUL_STACK_STATUSES))
            raise SpikeError(
                f"Stack {self.stack_name} is {stack_status}, not {expected}"
            )

        outputs = {
            str(item["OutputKey"]): str(item["OutputValue"])
            for item in stack.get("Outputs", [])
            if item.get("OutputKey") and item.get("OutputValue")
        }
        missing = sorted(REQUIRED_OUTPUTS - outputs.keys())
        if missing:
            raise SpikeError(f"Stack outputs are missing: {', '.join(missing)}")

        self.outputs = outputs
        provider_model_id = self.config.model
        target_name = outputs["InferenceTargetName"]
        resolved_model = f"{target_name}/{provider_model_id}"
        self.config = replace(self.config, model=resolved_model)
        self.evidence.document["run"].update(
            {
                "model": resolved_model,
                "providerModelId": provider_model_id,
                "targetName": target_name,
            }
        )
        self._oauth_scope = outputs["OAuthScope"]
        self._token_endpoint = outputs["TokenEndpoint"]
        self.state.update(
            {
                "gatewayId": outputs["GatewayIdentifier"],
                "gatewayArn": outputs["GatewayArn"],
                "gatewayUrl": outputs["GatewayUrl"],
                "targetId": outputs["InferenceTargetId"],
                "rateLimitId": outputs["RateLimitId"],
                "userPoolId": outputs["CognitoUserPoolId"],
                "userPoolClientId": outputs["CognitoClientId"],
            }
        )
        self.evidence.add(
            "pipeline_stack_loaded",
            stackName=self.stack_name,
            stackStatus=str(stack["StackStatus"]),
            gitHead=self.expected_git_head,
            awsRequestId=request_id(response),
        )

    def verify_control_plane(self) -> None:
        gateway_id = self.outputs["GatewayIdentifier"]
        gateway = self.control.get_gateway(gatewayIdentifier=gateway_id)
        if gateway.get("status") != "READY":
            raise SpikeError(f"Gateway is {gateway.get('status')}, not READY")
        if gateway.get("authorizerType") != "CUSTOM_JWT":
            raise SpikeError("Pipeline Gateway does not use CUSTOM_JWT")
        if gateway.get("gatewayArn") != self.outputs["GatewayArn"]:
            raise SpikeError("Gateway ARN differs from the CloudFormation output")

        tag_response = self.control.list_tags_for_resource(
            resourceArn=self.outputs["GatewayArn"]
        )
        tags = tag_response.get("tags", {})
        missing_tags = sorted(REQUIRED_TAGS - tags.keys())
        if missing_tags:
            raise SpikeError(
                f"Gateway is missing allocation tags: {', '.join(missing_tags)}"
            )
        if tags.get("environment") != self.expected_environment:
            raise SpikeError(
                "Gateway environment tag differs from the explicit expected environment"
            )

        target = self.control.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=self.outputs["InferenceTargetId"],
        )
        if target.get("status") != "READY":
            raise SpikeError(f"Inference target is {target.get('status')}, not READY")
        target_name = str(target.get("name", ""))
        if target_name != self.outputs["InferenceTargetName"]:
            raise SpikeError(
                "Inference target name differs from the CloudFormation output"
            )
        providers = target.get("credentialProviderConfigurations", [])
        if not any(
            item.get("credentialProviderType") == "GATEWAY_IAM_ROLE"
            for item in providers
        ):
            raise SpikeError("Inference target does not use GATEWAY_IAM_ROLE")

        rate_limit = self.control.get_gateway_rate_limit(
            gatewayIdentifier=gateway_id,
            rateLimitId=self.outputs["RateLimitId"],
        )
        if rate_limit.get("status") != "ACTIVE":
            raise SpikeError(
                f"Gateway rate limit is {rate_limit.get('status')}, not ACTIVE"
            )

        self.evidence.add(
            "pipeline_control_plane_verified",
            gatewayId=gateway_id,
            gatewayStatus=str(gateway["status"]),
            authorizerType=str(gateway["authorizerType"]),
            targetId=self.outputs["InferenceTargetId"],
            targetName=target_name,
            targetStatus=str(target["status"]),
            resolvedModel=self.config.model,
            rateLimitId=self.outputs["RateLimitId"],
            rateLimitStatus=str(rate_limit["status"]),
            allocationTagCount=len(REQUIRED_TAGS),
            gatewayRequestId=request_id(gateway),
            targetRequestId=request_id(target),
            rateLimitRequestId=request_id(rate_limit),
        )

    def verify_client_configuration(self) -> None:
        response = self.cognito.describe_user_pool_client(
            UserPoolId=self.outputs["CognitoUserPoolId"],
            ClientId=self.outputs["CognitoClientId"],
        )
        client = response.get("UserPoolClient", {})
        if "client_credentials" not in client.get("AllowedOAuthFlows", []):
            raise SpikeError("Cognito client does not allow client_credentials")
        if self.scope not in client.get("AllowedOAuthScopes", []):
            raise SpikeError("Cognito client does not allow the Gateway OAuth scope")
        if not client.get("ClientSecret"):
            raise SpikeError("Cognito M2M client has no secret")
        self.evidence.add(
            "pipeline_cognito_client_verified",
            clientIdHash=hashlib.sha256(
                self.outputs["CognitoClientId"].encode("utf-8")
            ).hexdigest(),
            oauthFlow="client_credentials",
            scope=self.scope,
            awsRequestId=request_id(response),
        )

    def verify_deployed(self) -> None:
        self.verify_identity()
        self.load_stack()
        self.verify_control_plane()
        self.verify_client_configuration()
        self.verify_models()
        self.verify_litellm_model(stream=False)
        self.verify_litellm_model(stream=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--git-head", required=True)
    parser.add_argument("--stack-name", default="Prod-InferenceGateway")
    parser.add_argument("--environment", required=True, choices=("nonprod", "prod"))
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", default="pipeline-gateway-verify")
    parser.add_argument(
        "--model",
        default="openai.gpt-oss-120b",
        help="Provider-qualified model ID without a Gateway target-name prefix.",
    )
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not GIT_SHA_PATTERN.fullmatch(args.git_head):
        print("FAIL: --git-head must be a 40-character lowercase Git SHA", file=sys.stderr)
        return 2
    if (
        not args.model
        or "/" in args.model
        or any(char.isspace() for char in args.model)
    ):
        print(
            "FAIL: --model must be a non-empty provider-qualified ID without a Gateway target prefix",
            file=sys.stderr,
        )
        return 2

    verifier: PipelineGatewayVerifier | None = None
    try:
        config = validate_config(args, required_model_prefix=None)
        verifier = PipelineGatewayVerifier(
            config,
            stack_name=args.stack_name,
            expected_git_head=args.git_head,
            expected_environment=args.environment,
        )
        verifier.verify_deployed()
        verifier.evidence.finish("passed")
        print(f"PASS: pipeline-owned Gateway verified; evidence: {config.evidence_path}")
        return 0
    except Exception as error:
        if verifier is not None:
            verifier.evidence.add(
                "failure",
                errorType=type(error).__name__,
                errorCode=(
                    aws_error_code(error) if isinstance(error, ClientError) else None
                ),
                message=str(error),
            )
            verifier.evidence.finish("failed")
            evidence_path = verifier.config.evidence_path
        else:
            evidence_path = None
        print(f"FAIL: {error}", file=sys.stderr)
        if evidence_path is not None:
            print(f"Evidence: {evidence_path}", file=sys.stderr)
        return 1
    finally:
        if verifier is not None:
            verifier.close()


if __name__ == "__main__":
    raise SystemExit(main())
