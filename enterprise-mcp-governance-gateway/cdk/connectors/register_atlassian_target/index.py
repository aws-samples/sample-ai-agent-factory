# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom-resource handler: create/update/delete the Atlassian gateway target.

Why a custom resource (not a plain CfnGatewayTarget): the mcpServer endpoint is the
Runtime's INVOCATION URL, which requires URL-encoding the runtime ARN — CloudFormation
has no urlencode intrinsic, and the ARN is only known at deploy time. So we build the
URL here in Python and create the mcpServer target with the schema-upfront tool schema
(no admin consent at create) + OAUTH (3LO / AUTHORIZATION_CODE) credential config.

Being a custom resource, the target is CFN-tracked: ``cdk destroy`` deletes it (no
orphan), unlike the old post-deploy setup_connector.py script.
"""
import urllib.parse

import boto3

_cp = boto3.client("bedrock-agentcore-control")


def _runtime_url(region: str, runtime_arn: str) -> str:
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")


def _find(gateway_id: str, name: str):
    token = None
    while True:
        kw = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        if token:
            kw["nextToken"] = token
        resp = _cp.list_gateway_targets(**kw)
        for t in resp.get("items", []):
            if t["name"] == name:
                return t
        token = resp.get("nextToken")
        if not token:
            return None


def on_event(event, context):
    request_type = event["RequestType"]
    p = event["ResourceProperties"]
    gateway_id = p["GatewayId"]
    name = p.get("TargetName", "Atlassian")

    if request_type == "Delete":
        t = _find(gateway_id, name)
        if t:
            _cp.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or f"target-{gateway_id}-{name}"}

    target_config = {"mcp": {"mcpServer": {
        "endpoint": _runtime_url(p["Region"], p["RuntimeArn"]),
        # schema-upfront: register tools without a dynamic sync → no admin consent at
        # create. inlinePayload is a JSON string with a {"tools": [...]} object.
        "mcpToolSchema": {"inlinePayload": p["ToolSchema"]},
    }}}
    creds = [{"credentialProviderType": "OAUTH", "credentialProvider": {"oauthCredentialProvider": {
        "providerArn": p["ProviderArn"],
        "grantType": "AUTHORIZATION_CODE",
        "defaultReturnUrl": p["DefaultReturnUrl"],
        "scopes": p["Scopes"],
    }}}]

    t = _find(gateway_id, name)
    if t:
        _cp.update_gateway_target(
            gatewayIdentifier=gateway_id, targetId=t["targetId"], name=name,
            targetConfiguration=target_config, credentialProviderConfigurations=creds)
        target_id = t["targetId"]
    else:
        r = _cp.create_gateway_target(
            gatewayIdentifier=gateway_id, name=name,
            description="Atlassian (Jira+Confluence) via per-user 3LO",
            targetConfiguration=target_config, credentialProviderConfigurations=creds)
        target_id = r["targetId"]
    return {"PhysicalResourceId": target_id}
