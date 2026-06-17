#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entrypoint for the AgentCore Gateway stack (Module 4)."""

import os

import aws_cdk as cdk

from agentcore_gateway_stack import AgentCoreGatewayStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
)

AgentCoreGatewayStack(app, "AgentCoreGatewayStack", env=env)
app.synth()
