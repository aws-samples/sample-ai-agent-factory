"""Simulate the Runtime execution role's AgentCore Identity grant.

Reads a rendered RegistryRoles template, collects every policy statement
attached to the ``AgenticAI-D03-<env>-<tenant>-<agent>-runtime`` role, resolves
partition/account/region intrinsics, and runs ``iam:SimulateCustomPolicy`` for
the exact data-plane pairs the generated agent exercises at invoke time --
including the pair AgentCore denied on the first live invoke (2026-09-23):
``GetWorkloadAccessToken`` on the bare ``workload-identity-directory/default``.

Positive pairs must be ``allowed``; a foreign directory and a foreign provider
must stay implicitly denied; the DenyDirectBedrockInvoke guard must still
deny ``bedrock:InvokeModel``. Read-only. Exit 2 on any failure.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3


def resolve(node: Any, partition: str, account: str, region: str) -> Any:
    if isinstance(node, dict):
        if "Fn::Join" in node:
            sep, parts = node["Fn::Join"]
            return sep.join(str(resolve(p, partition, account, region)) for p in parts)
        if "Ref" in node:
            return {"AWS::Partition": partition, "AWS::AccountId": account, "AWS::Region": region}.get(node["Ref"], node["Ref"])
        if "Fn::GetAtt" in node:
            return f"arn:{partition}:iam::{account}:role/RESOLVED-{node['Fn::GetAtt'][0]}"
        return {k: resolve(v, partition, account, region) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(v, partition, account, region) for v in node]
    return node


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--role-name", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--partition", default="aws")
    ap.add_argument("--workload-prefix", default="AgenticAI_D03_nonprod_demo_primary")
    ap.add_argument("--provider-name", default="AgenticAI_D03_nonprod_demo_primary_inference")
    args = ap.parse_args()

    template = json.loads(args.template.read_text())
    resources = template["Resources"]
    role_id = next(
        (lid for lid, r in resources.items() if r["Type"] == "AWS::IAM::Role" and r["Properties"].get("RoleName") == args.role_name),
        None,
    )
    if role_id is None:
        print(json.dumps({"passed": False, "error": "runtime role not found"}))
        return 2
    docs = []
    role = resources[role_id]
    for p in role["Properties"].get("Policies", []):
        docs.append(json.dumps(resolve(p["PolicyDocument"], args.partition, args.account, args.region)))
    for lid, r in resources.items():
        if r["Type"] == "AWS::IAM::Policy" and role_id in json.dumps(r["Properties"].get("Roles", [])):
            docs.append(json.dumps(resolve(r["Properties"]["PolicyDocument"], args.partition, args.account, args.region)))

    base = f"arn:{args.partition}:bedrock-agentcore:{args.region}:{args.account}"
    directory = f"{base}:workload-identity-directory/default"
    vault = f"{base}:token-vault/default"
    identity = f"{directory}/workload-identity/{args.workload_prefix}_5dfed653a473"
    provider = f"{vault}/oauth2credentialprovider/{args.provider_name}"
    sm = f"arn:{args.partition}:secretsmanager:{args.region}:{args.account}:secret"
    managed_secret = f"{sm}:bedrock-agentcore-identity!default/oauth2/{args.provider_name}-e69ee5c7-W4Q3tK"
    # Exact live Memory id shape: deterministic name + service-minted 10-char suffix.
    memory = f"{base}:memory/AgenticAI_D03_nonprod_demo_primary_memory-Tlr3wS2juR"
    # Memory CMK (id is service-minted; the identity policy pins the alias +
    # ViaService, so the simulation supplies exactly that request context).
    cmk = f"arn:{args.partition}:kms:{args.region}:{args.account}:key/5c2a3e50-5452-4a85-9aac-918836bae705"
    via_agentcore = {"ContextKeyName": "kms:ViaService", "ContextKeyValues": [f"bedrock-agentcore.{args.region}.amazonaws.com"], "ContextKeyType": "string"}
    via_secrets = {"ContextKeyName": "kms:ViaService", "ContextKeyValues": [f"secretsmanager.{args.region}.amazonaws.com"], "ContextKeyType": "string"}
    memory_alias = {"ContextKeyName": "kms:ResourceAliases", "ContextKeyValues": ["alias/agenticai/d03-runtime-memory-nonprod-demo-primary"], "ContextKeyType": "stringList"}
    other_alias = {"ContextKeyName": "kms:ResourceAliases", "ContextKeyValues": ["alias/agenticai/d03-runtime-memory-prod-demo-primary"], "ContextKeyType": "stringList"}
    cases = [
        ("bedrock-agentcore:GetWorkloadAccessToken", directory),   # exact live denial #1
        ("bedrock-agentcore:GetWorkloadAccessToken", identity),
        ("bedrock-agentcore:GetResourceOauth2Token", provider),
        ("bedrock-agentcore:GetResourceOauth2Token", vault),
        ("bedrock-agentcore:GetResourceOauth2Token", identity),
        ("bedrock-agentcore:GetResourceOauth2Token", directory),
        ("secretsmanager:GetSecretValue", managed_secret),           # exact live denial #2
        ("bedrock-agentcore:InvokeGateway", f"{base}:gateway/abc123"),
        ("bedrock-agentcore:CreateEvent", memory),                   # live gap #3 (no grant)
        ("bedrock-agentcore:GetEvent", memory),
        ("kms:GenerateDataKey", cmk, [via_agentcore, memory_alias]),  # live gap #4 (KMS as caller)
        ("kms:Decrypt", cmk, [via_agentcore, memory_alias]),
    ]
    negatives = [
        ("bedrock-agentcore:GetWorkloadAccessToken", f"{base}:workload-identity-directory/other"),
        ("bedrock-agentcore:GetResourceOauth2Token", f"{vault}/oauth2credentialprovider/SomeOtherProvider"),
        ("bedrock-agentcore:GetWorkloadAccessToken", f"{directory}/workload-identity/Other_prefix_5dfed653a473"),
        ("bedrock-agentcore:CreateWorkloadIdentity", directory),
        ("secretsmanager:GetSecretValue", f"{sm}:bedrock-agentcore-identity!default/oauth2/SomeOtherProvider-abc123-XyZ"),
        ("secretsmanager:GetSecretValue", f"arn:{args.partition}:secretsmanager:{args.region}:111111111111:secret:agenticai/inference-m2m/agenticai-inference-nonprod-abc"),
        ("bedrock:InvokeModel", f"arn:{args.partition}:bedrock:{args.region}::foundation-model/anthropic.claude-3-haiku"),
        ("bedrock-agentcore:CreateEvent", f"{base}:memory/AgenticAI_D03_prod_demo_primary_memory-Abcdefghij"),
        ("bedrock-agentcore:CreateEvent", f"{base}:memory/OtherTenant_memory-Abcdefghij"),
        ("bedrock-agentcore:DeleteEvent", memory),
        ("bedrock-agentcore:ListEvents", memory),
        ("kms:Decrypt", cmk, [via_agentcore, other_alias]),          # foreign env's Memory CMK
        ("kms:Decrypt", cmk, [via_secrets, memory_alias]),           # right key, wrong service path
        ("kms:Decrypt", cmk, []),                                    # direct call, no service context
        ("kms:CreateGrant", cmk, [via_agentcore, memory_alias]),
    ]

    def evaluate(action: str, resource: str, context: list | None) -> str:
        kwargs: dict[str, Any] = {"PolicyInputList": docs, "ActionNames": [action], "ResourceArns": [resource]}
        if context:
            kwargs["ContextEntries"] = context
        return iam.simulate_custom_policy(**kwargs)["EvaluationResults"][0]["EvalDecision"]

    iam = boto3.client("iam", region_name=args.region)
    verdict: dict[str, Any] = {"allowed": [], "denied": [], "negativeDenied": [], "negativeAllowed": []}
    for action, resource, *ctx in cases:
        d = evaluate(action, resource, ctx[0] if ctx else None)
        (verdict["allowed"] if d == "allowed" else verdict["denied"]).append({"action": action, "resourceSuffix": resource.split(":", 5)[-1], "decision": d})
    for action, resource, *ctx in negatives:
        d = evaluate(action, resource, ctx[0] if ctx else None)
        (verdict["negativeDenied"] if d != "allowed" else verdict["negativeAllowed"]).append({"action": action, "resourceSuffix": resource.split(":", 5)[-1], "decision": d})
    verdict["passed"] = not verdict["denied"] and not verdict["negativeAllowed"]
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    sys.exit(main())
