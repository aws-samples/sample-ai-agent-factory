# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create the AgentCore Gateway with Cognito auth and interceptors.

Run: python create_gateway.py

Uses environment variables (set these first):
  REGION, POOL_ID, CLIENT_ID, GATEWAY_ROLE_ARN,
  REQUEST_INTERCEPTOR_ARN, RESPONSE_INTERCEPTOR_ARN
"""

import os
import boto3

REGION = os.environ["REGION"]
POOL_ID = os.environ["POOL_ID"]
CLIENT_ID = os.environ["CLIENT_ID"]
M2M_CLIENT_ID = os.environ.get("M2M_CLIENT_ID", "")
GATEWAY_ROLE_ARN = os.environ["GATEWAY_ROLE_ARN"]
REQUEST_INTERCEPTOR_ARN = os.environ["REQUEST_INTERCEPTOR_ARN"]
RESPONSE_INTERCEPTOR_ARN = os.environ["RESPONSE_INTERCEPTOR_ARN"]

# Validate ARNs before calling the API
for name, val in [
    ("GATEWAY_ROLE_ARN", GATEWAY_ROLE_ARN),
    ("REQUEST_INTERCEPTOR_ARN", REQUEST_INTERCEPTOR_ARN),
    ("RESPONSE_INTERCEPTOR_ARN", RESPONSE_INTERCEPTOR_ARN),
]:
    if not val.startswith("arn:aws:"):
        print(f"ERROR: {name} looks wrong: {val!r}")
        print("       It should start with 'arn:aws:'")
        raise SystemExit(1)

GATEWAY_NAME = "tools-gateway"

client = boto3.client("bedrock-agentcore-control", region_name=REGION)

print(f"Creating AgentCore Gateway in {REGION}...")
print(f"  Pool ID:      {POOL_ID}")
print(f"  Client ID:    {CLIENT_ID}")
print(f"  M2M Client:   {M2M_CLIENT_ID or '(not set)'}")
print(f"  Role ARN:     {GATEWAY_ROLE_ARN}")
print(f"  Req Interceptor:  {REQUEST_INTERCEPTOR_ARN}")
print(f"  Resp Interceptor: {RESPONSE_INTERCEPTOR_ARN}")
print()

# Check for an existing gateway with the same name
existing = client.list_gateways().get("items", [])
match = [g for g in existing if g["name"] == GATEWAY_NAME]

DISCOVERY_URL = (
    f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
    "/.well-known/openid-configuration"
)
ALLOWED_CLIENTS = [c for c in [CLIENT_ID, M2M_CLIENT_ID] if c]

INTERCEPTOR_CONFIGS = [
    {
        "interceptor": {
            "lambda": {"arn": REQUEST_INTERCEPTOR_ARN},
        },
        "interceptionPoints": ["REQUEST"],
        "inputConfiguration": {"passRequestHeaders": True},
    },
    {
        "interceptor": {
            "lambda": {"arn": RESPONSE_INTERCEPTOR_ARN},
        },
        "interceptionPoints": ["RESPONSE"],
        "inputConfiguration": {"passRequestHeaders": True},
    },
]

if match:
    gateway_id = match[0]["gatewayId"]
    print(f"Gateway '{GATEWAY_NAME}' already exists — updating configuration.")
    print(f"  Gateway ID:  {gateway_id}")
    print(f"  Status:      {match[0]['status']}")

    # Update gateway to ensure the authorizer config is current. Interceptors are
    # set at create time and persist for the life of the gateway; update_gateway
    # does not accept interceptorConfigurations, so it is intentionally omitted
    # here (passing it raises a parameter-validation error on re-runs).
    try:
        client.update_gateway(
            gatewayIdentifier=gateway_id,
            name=GATEWAY_NAME,
            roleArn=GATEWAY_ROLE_ARN,
            protocolType="MCP",
            protocolConfiguration={
                "mcp": {
                    "supportedVersions": ["2025-03-26"],
                    "instructions": (
                        "Tools Gateway for the Agentic AI Landing Zone. "
                        "Search for tools by describing what you need."
                    ),
                }
            },
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration={
                "customJWTAuthorizer": {
                    "discoveryUrl": DISCOVERY_URL,
                    "allowedClients": ALLOWED_CLIENTS,
                }
            },
        )
        print("  Configuration updated successfully.")
    except Exception as exc:
        print(f"  Warning: Could not update gateway config: {exc}")
        print("  The gateway may need to be deleted and recreated.")
else:
    response = client.create_gateway(
        name=GATEWAY_NAME,
        description="Tools Gateway for the Agentic AI Landing Zone",
        roleArn=GATEWAY_ROLE_ARN,
        protocolType="MCP",
        protocolConfiguration={
            "mcp": {
                "supportedVersions": ["2025-03-26"],
                "searchType": "SEMANTIC",
                "instructions": (
                    "Tools Gateway for the Agentic AI Landing Zone. "
                    "Search for tools by describing what you need."
                ),
            }
        },
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": DISCOVERY_URL,
                "allowedClients": ALLOWED_CLIENTS,
            }
        },
        interceptorConfigurations=INTERCEPTOR_CONFIGS,
    )
    gateway_id = response["gatewayId"]
    gateway_url = response.get("gatewayUrl", "")
    print(f"Gateway created successfully!")
    print(f"  Gateway ID:  {gateway_id}")
    if gateway_url:
        print(f"  Gateway URL: {gateway_url}")

# Store gateway ID in SSM Parameter Store for cross-stack discovery
ssm = boto3.client("ssm", region_name=REGION)
env_name = os.environ.get("ENVIRONMENT_NAME", "workshop")
ssm_param = f"/agentcore-gateway/{env_name}/gateway-id"
ssm.put_parameter(
    Name=ssm_param,
    Value=gateway_id,
    Type="String",
    Overwrite=True,
    Description="AgentCore Gateway ID (written by create_gateway.py)",
)
print(f"Stored gateway ID in SSM: {ssm_param} = {gateway_id}")

# Auto-update Sync Lambda with gateway ID (read-merge-update to preserve env vars)
lambda_client = boto3.client("lambda", region_name=REGION)
SYNC_FN_NAME = "agentcore-gateway-sync"
try:
    current = lambda_client.get_function_configuration(FunctionName=SYNC_FN_NAME)
    env_vars = current.get("Environment", {}).get("Variables", {})
    env_vars["GATEWAY_ID"] = gateway_id
    lambda_client.update_function_configuration(
        FunctionName=SYNC_FN_NAME,
        Environment={"Variables": env_vars},
    )
    print(f"Updated {SYNC_FN_NAME} GATEWAY_ID = {gateway_id}")
except Exception as exc:
    print(f"Warning: Could not auto-update Lambda: {exc}")
    print(f"Manually run:")
    print(f"  aws lambda update-function-configuration \\")
    print(f"    --function-name {SYNC_FN_NAME} \\")
    print(f'    --environment \'{{\"Variables\":{{\"GATEWAY_ID\":\"{gateway_id}\"}}}}\' \\')
    print(f"    --region {REGION}")
