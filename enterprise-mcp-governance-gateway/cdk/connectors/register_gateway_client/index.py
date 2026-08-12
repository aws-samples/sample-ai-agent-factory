# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom-resource handler: register (or de-register) a Cognito app client in the
AgentCore gateway's ``allowedClients``, WITHOUT clobbering any other gateway config.

Why this exists: the connector-authorize SPA uses its own Hosted-UI Cognito client,
which the gateway must trust (``authorizerConfiguration.customJWTAuthorizer.allowedClients``).
That list lives on the gateway (a different stack), and the SPA client is created in
ConnectorAuthStack — a cross-stack cycle — so we register it at deploy time here.

CRITICAL: ``update_gateway`` is a FULL REPLACE. We therefore GET the current gateway,
carry ALL existing fields forward (esp. ``policyEngineConfiguration`` — dropping it
disables Cedar enforcement), and only change ``allowedClients``. (A partial
update_gateway is exactly what must be avoided.)
"""
import boto3

_ctl = boto3.client("bedrock-agentcore-control")

# EVERY field update_gateway accepts (derived from the API model), except the
# identifier and idempotency token. update_gateway is a FULL REPLACE, so we carry
# ALL of these forward from the live gateway and change only allowedClients —
# this guarantees we never silently drop a field (interceptorConfigurations,
# policyEngineConfiguration, protocolConfiguration, kmsKeyArn, …).
_UPDATE_FIELDS = [
    m for m in _ctl.meta.service_model.operation_model("UpdateGateway").input_shape.members
    if m not in ("gatewayIdentifier", "clientToken")
]


def _apply(gateway_id: str, allowed_clients: list) -> None:
    """Re-issue update_gateway preserving every field, changing only allowedClients."""
    g = _ctl.get_gateway(gatewayIdentifier=gateway_id)
    params = {"gatewayIdentifier": gateway_id}
    for f in _UPDATE_FIELDS:
        v = g.get(f)
        if v is not None:
            params[f] = v
    cj = params.get("authorizerConfiguration", {}).get("customJWTAuthorizer")
    if cj is None:
        raise RuntimeError("Gateway is not CUSTOM_JWT; cannot manage allowedClients.")
    cj["allowedClients"] = allowed_clients
    _ctl.update_gateway(**params)


def on_event(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    gateway_id = props["GatewayId"]
    client_id = props["ClientId"]
    pid = f"allowedclient-{gateway_id}-{client_id}"

    g = _ctl.get_gateway(gatewayIdentifier=gateway_id)
    cj = (g.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    allowed = list(cj.get("allowedClients") or [])

    if request_type in ("Create", "Update"):
        if client_id not in allowed:
            allowed.append(client_id)
            _apply(gateway_id, allowed)
    elif request_type == "Delete":
        remaining = [c for c in allowed if c != client_id]
        # Never leave the list empty (that would break inbound auth for everyone);
        # only write if something else remains.
        if remaining and remaining != allowed:
            _apply(gateway_id, remaining)

    return {"PhysicalResourceId": pid}
