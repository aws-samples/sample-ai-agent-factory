#!/usr/bin/env python3
"""Cognito M2M + Strands LiteLLMModel AgentCore Gateway spike."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlencode

from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError
from strands import Agent
from strands.models.litellm import LiteLLMModel

from gateway_spike import (
    Config,
    GatewaySpike,
    SpikeError,
    aws_error_code,
    request_id,
    validate_config,
)


class CognitoLiteLLMSpike(GatewaySpike):
    throttle_positive_twin = "strands_litellm_model_non_streaming_passed"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.cognito = self.session.client("cognito-idp")

    @property
    def pool_name(self) -> str:
        return f"{self.config.prefix}-gateway-users"

    @property
    def client_name(self) -> str:
        return f"{self.config.prefix}-gateway-client"

    @property
    def resource_server_id(self) -> str:
        return f"https://{self.config.prefix}.internal"

    @property
    def scope(self) -> str:
        return f"{self.resource_server_id}/invoke"

    @property
    def domain(self) -> str:
        return f"{self.config.prefix}-{self.config.account_id[-4:]}"

    def _find_pool(self) -> dict[str, Any] | None:
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 60}
            if next_token:
                kwargs["NextToken"] = next_token
            response = self.cognito.list_user_pools(**kwargs)
            for pool in response.get("UserPools", []):
                if pool.get("Name") == self.pool_name:
                    return pool
            next_token = response.get("NextToken")
            if not next_token:
                return None

    def _assert_pool_owned(self, pool_id: str) -> dict[str, Any]:
        response = self.cognito.describe_user_pool(UserPoolId=pool_id)
        pool = response["UserPool"]
        if pool.get("Name") != self.pool_name:
            raise SpikeError("Cognito User Pool name does not match the spike")
        tags = pool.get("UserPoolTags", {})
        if any(tags.get(key) != value for key, value in self.config.tags.items()):
            raise SpikeError("Refusing to operate on Cognito User Pool: ownership tags differ")
        return pool

    def ensure_cognito(self) -> tuple[str, str]:
        pool_id = self.state.get("userPoolId")
        client_id = self.state.get("userPoolClientId")
        if pool_id and client_id:
            self._assert_pool_owned(str(pool_id))
            self.cognito.describe_user_pool_client(
                UserPoolId=str(pool_id), ClientId=str(client_id)
            )
            return str(pool_id), str(client_id)

        existing = self._find_pool()
        if existing:
            raise SpikeError(
                f"User Pool {self.pool_name} exists without this run's state file"
            )

        pool_response = self.cognito.create_user_pool(
            PoolName=self.pool_name,
            UserPoolTags=self.config.tags,
            DeletionProtection="INACTIVE",
        )
        pool_id = str(pool_response["UserPool"]["Id"])
        self.save_state(userPoolId=pool_id)
        self.evidence.add(
            "cognito_user_pool_created",
            userPoolId=pool_id,
            awsRequestId=request_id(pool_response),
        )

        resource_response = self.cognito.create_resource_server(
            UserPoolId=pool_id,
            Identifier=self.resource_server_id,
            Name=f"{self.config.prefix} Gateway",
            Scopes=[
                {
                    "ScopeName": "invoke",
                    "ScopeDescription": "Invoke the non-production Gateway spike",
                }
            ],
        )
        self.evidence.add(
            "cognito_resource_server_created",
            identifier=self.resource_server_id,
            awsRequestId=request_id(resource_response),
        )

        client_response = self.cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=self.client_name,
            GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[self.scope],
            AllowedOAuthFlowsUserPoolClient=True,
            AccessTokenValidity=5,
            TokenValidityUnits={"AccessToken": "minutes"},
        )
        client_id = str(client_response["UserPoolClient"]["ClientId"])
        self.save_state(userPoolClientId=client_id)
        self.evidence.add(
            "cognito_m2m_client_created",
            clientIdHash=hashlib.sha256(client_id.encode("utf-8")).hexdigest(),
            awsRequestId=request_id(client_response),
        )

        domain_response = self.cognito.create_user_pool_domain(
            Domain=self.domain,
            UserPoolId=pool_id,
        )
        self.save_state(userPoolDomain=self.domain)
        self.evidence.add(
            "cognito_domain_created",
            domain=self.domain,
            awsRequestId=request_id(domain_response),
        )
        return pool_id, client_id

    def discovery_url(self) -> str:
        pool_id = self.state.get("userPoolId")
        if not pool_id:
            raise SpikeError("Cognito User Pool ID is absent from state")
        return (
            f"https://cognito-idp.{self.config.region}.amazonaws.com/"
            f"{pool_id}/.well-known/openid-configuration"
        )

    def token_endpoint(self) -> str:
        return (
            f"https://{self.domain}.auth.{self.config.region}.amazoncognito.com/"
            "oauth2/token"
        )

    def access_token(self) -> str:
        pool_id = str(self.state.get("userPoolId", ""))
        client_id = str(self.state.get("userPoolClientId", ""))
        if not pool_id or not client_id:
            raise SpikeError("Cognito client state is incomplete")
        response = self.cognito.describe_user_pool_client(
            UserPoolId=pool_id,
            ClientId=client_id,
        )
        client_secret = response["UserPoolClient"].get("ClientSecret")
        if not client_secret:
            raise SpikeError("Cognito M2M client has no secret")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        request = AWSRequest(
            method="POST",
            url=self.token_endpoint(),
            data=urlencode({"grant_type": "client_credentials", "scope": self.scope}).encode("utf-8"),
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        last_status = 0
        for _ in range(18):
            token_response = self.http.send(request.prepare())
            last_status = token_response.status_code
            if last_status == 200:
                payload = json.loads(token_response.content)
                token = payload.get("access_token")
                if token:
                    return str(token)
                raise SpikeError("Cognito token response has no access_token")
            # New domains can take time to become routable.
            if last_status not in {400, 404, 429, 500, 502, 503}:
                break
            time.sleep(5)
        raise SpikeError(f"Cognito token endpoint returned HTTP {last_status}")

    def ensure_gateway(self, role_arn: str) -> dict[str, Any]:
        existing = self._gateway_by_name()
        if existing:
            if not self.state.get("gatewayId"):
                raise SpikeError(
                    f"Gateway {self.config.gateway_name} exists without this run's state file"
                )
            self._assert_gateway_owned(existing)
            return self.wait_gateway(str(existing["gatewayId"]), {"READY"})

        client_id = str(self.state.get("userPoolClientId", ""))
        if not client_id:
            raise SpikeError("Cognito client must exist before Gateway creation")
        response = self.control.create_gateway(
            name=self.config.gateway_name,
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration={"mcp": {"supportedVersions": ["2025-11-25"]}},
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration={
                "customJWTAuthorizer": {
                    "discoveryUrl": self.discovery_url(),
                    "allowedClients": [client_id],
                }
            },
            description="Ephemeral Cognito and LiteLLMModel compatibility spike",
            tags=self.config.tags,
            clientToken=self.client_token("cognitoGateway"),
        )
        self.save_state(
            gatewayId=response["gatewayId"],
            gatewayArn=response["gatewayArn"],
            gatewayUrl=response["gatewayUrl"],
        )
        self.evidence.add(
            "cognito_gateway_created",
            gatewayId=response["gatewayId"],
            gatewayArn=response["gatewayArn"],
            awsRequestId=request_id(response),
        )
        return self.wait_gateway(str(response["gatewayId"]), {"READY"})

    def signed_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        accept: str = "application/json",
    ) -> tuple[int, Mapping[str, str], bytes]:
        payload = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else b""
        )
        headers = {"Accept": accept, "Authorization": f"Bearer {self.access_token()}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = AWSRequest(
            method=method,
            url=self._gateway_url(path),
            data=payload,
            headers=headers,
        )
        response = self.http.send(request.prepare())
        return response.status_code, response.headers, response.content

    def deploy(self) -> None:
        self.verify_identity()
        self.ensure_cognito()
        role_arn = self.ensure_role()
        gateway = self.ensure_gateway(role_arn)
        self.ensure_target(str(gateway["gatewayId"]))
        self.evidence.add("jwt_deployment_ready", gatewayId=gateway["gatewayId"])

    def verify_litellm_model(self, *, stream: bool) -> None:
        token = self.access_token()
        model = LiteLLMModel(
            model_id=f"openai/{self.config.model}",
            client_args={
                "api_base": self._gateway_url("/inference/v1"),
                "api_key": token,
            },
            params={"max_tokens": 256, "temperature": 0, "stream": stream},
        )
        agent = Agent(model=model, callback_handler=None)
        result = agent("Reply with exactly the word verified.")
        message = result.message
        if not message or not message.get("content"):
            raise SpikeError("LiteLLMModel returned no Strands message content")
        event_name = (
            "strands_litellm_model_streaming_passed"
            if stream
            else "strands_litellm_model_non_streaming_passed"
        )
        self.evidence.add(
            event_name,
            stream=stream,
            model=self.config.model,
            contentBlocks=len(message["content"]),
        )

    def verify(self) -> None:
        self.verify_identity()
        self.verify_models()
        self.verify_litellm_model(stream=False)
        self.verify_litellm_model(stream=True)
        gateway_id = str(self.state.get("gatewayId", ""))
        if not gateway_id:
            raise SpikeError("Gateway ID is absent from state")
        self.ensure_zero_rate_limit(gateway_id)
        self.verify_exact_throttle()

    def cleanup(self) -> None:
        pool_id = str(self.state.get("userPoolId", ""))
        domain = str(self.state.get("userPoolDomain", ""))
        base_error: Exception | None = None
        try:
            super().cleanup()
        except Exception as error:
            base_error = error

        if not pool_id:
            existing = self._find_pool()
            pool_id = str(existing["Id"]) if existing else ""
        if pool_id:
            try:
                self._assert_pool_owned(pool_id)
            except ClientError as error:
                if aws_error_code(error) != "ResourceNotFoundException":
                    raise
                pool_id = ""
        if pool_id:
            if domain:
                try:
                    response = self.cognito.delete_user_pool_domain(
                        Domain=domain,
                        UserPoolId=pool_id,
                    )
                    self.evidence.add(
                        "cognito_domain_deleted",
                        domain=domain,
                        awsRequestId=request_id(response),
                    )
                except ClientError as error:
                    if aws_error_code(error) != "ResourceNotFoundException":
                        raise
            response = self.cognito.delete_user_pool(UserPoolId=pool_id)
            self.evidence.add(
                "cognito_user_pool_deleted",
                userPoolId=pool_id,
                awsRequestId=request_id(response),
            )
        if self._find_pool() is not None:
            raise SpikeError("Run-owned Cognito User Pool remains after cleanup")
        self.state = {}
        self.state_store.write(self.state)
        self.evidence.add("jwt_zero_residue_verified")
        if base_error is not None:
            raise base_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "verify", "cleanup", "all"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", default="aiaf-live-20260918")
    parser.add_argument("--model", default="bedrock-mantle/openai.gpt-oss-120b")
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = validate_config(args)
    spike = CognitoLiteLLMSpike(config)
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
                        f"Verification failed: {verification_error}; cleanup failed: {cleanup_error}"
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
