#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point for the Enterprise MCP Governance Gateway.

Python CDK app for the governance gateway (it replaced an original boto3 deployer).

Local-only: ``app.synth()`` produces a CloudFormation cloud assembly under
``cdk.out/`` with no AWS calls. Run ``cdk synth`` (preferred) or, for a quick
offline check, ``python app.py``.
"""
import os

import aws_cdk as cdk

from enterprise_gateway.gateway_stack import EnterpriseGatewayStack
from connectors.atlassian_stack import AtlassianConnectorStack
from connectors.connector_auth_stack import ConnectorAuthStack

app = cdk.App()

# Account/region resolve from the CDK CLI environment at deploy time. Region
# default matches the existing deployer (AWS_REGION, else us-west-2). With no
# account set this is an environment-agnostic stack, which synthesizes offline.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=(
        os.environ.get("CDK_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-west-2"
    ),
)

EnterpriseGatewayStack(
    app,
    "EnterpriseMcpGatewayStack",
    env=env,
    description="Enterprise MCP Governance Gateway (Amazon Bedrock AgentCore) - CDK",
)

# Atlassian connector (separate stack; references the core gateway). The client id
# and cloud id are non-secret but deployment-specific — pass your own with
# `-c atlassian_client_id=... -c atlassian_cloud_id=...`. The REPLACE_ME defaults let
# the other stacks synth without them; the connector won't deploy until you set real values.
AtlassianConnectorStack(
    app,
    "AtlassianConnectorStack",
    env=env,
    client_id=app.node.try_get_context("atlassian_client_id") or "REPLACE_ME_ATLASSIAN_CLIENT_ID",
    cloud_id=app.node.try_get_context("atlassian_cloud_id") or "REPLACE_ME_ATLASSIAN_CLOUD_ID",
    description="Atlassian (Jira+Confluence) MCP connector for the governance gateway - CDK",
)

# Shared, provider-generic per-user 3LO consent SPA (Cognito Hosted UI + Identity
# Pool + S3/CloudFront). Serves every connector via /auth?provider=<name>. Imports
# the gateway's pool + workload id from SSM, so deploy the gateway stack first.
ConnectorAuthStack(
    app,
    "ConnectorAuthStack",
    env=env,
    description="Shared connector authorize SPA (per-user 3LO consent) for the governance gateway - CDK",
)

app.synth()
