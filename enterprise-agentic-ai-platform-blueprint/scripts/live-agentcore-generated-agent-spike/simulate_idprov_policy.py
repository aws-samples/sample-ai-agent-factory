"""Simulate the credential-provider role policy against the exact live denials.

Reads a rendered CloudFormation template (from ``cdk synth`` / the phase-23
test harness) for the nonprod RuntimeMemory stack, extracts the inline policy
of the ``AgenticAI-D03-<env>-<tenant>-<agent>-idprov`` role, substitutes the
partition/account/region intrinsics, and runs ``iam:SimulateCustomPolicy``
for EVERY bedrock-agentcore call the custom-resource handler makes
(create/get/update/delete of the WorkloadIdentity and Oauth2CredentialProvider,
``TagResource``, ``ListTagsForResource``, ``CreateTokenVault``), each evaluated
against both the modeled parent container (``workload-identity-directory/
default``, ``token-vault/default``) and the named resource. This is exactly
the pattern AgentCore denied live, three times, on 2026-09-23.

Every pair must evaluate ``allowed``; foreign containers must stay implicitly
denied; a bare-"*" resource must NOT be present outside the ViaService-bound
KMS statement. Prints a compact JSON verdict and exits 2 on any failure.
Read-only: SimulateCustomPolicy mutates nothing.

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
            return {
                "AWS::Partition": partition,
                "AWS::AccountId": account,
                "AWS::Region": region,
            }.get(node["Ref"], node["Ref"])
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
    args = ap.parse_args()

    template = json.loads(args.template.read_text())
    role = next(
        (r for r in template["Resources"].values()
         if r["Type"] == "AWS::IAM::Role" and r["Properties"].get("RoleName") == args.role_name),
        None,
    )
    if role is None:
        print(json.dumps({"passed": False, "error": "role not found in template"}))
        return 2
    policies = role["Properties"]["Policies"]
    docs = [json.dumps(resolve(p["PolicyDocument"], args.partition, args.account, args.region)) for p in policies]

    base = f"arn:{args.partition}:bedrock-agentcore:{args.region}:{args.account}"
    directory = f"{base}:workload-identity-directory/default"
    vault = f"{base}:token-vault/default"
    identity = f"{directory}/workload-identity/AgenticAI_D03_probe"
    provider = f"{vault}/oauth2credentialprovider/AgenticAI_D03_probe"
    # Every bedrock-agentcore call the custom-resource handler makes, evaluated
    # against BOTH the parent container and the named resource -- the service
    # authorizer has been observed live to use either.
    identity_actions = (
        "CreateWorkloadIdentity",
        "GetWorkloadIdentity",
        "DeleteWorkloadIdentity",
        "TagResource",
        "ListTagsForResource",
    )
    provider_actions = (
        "CreateOauth2CredentialProvider",
        "GetOauth2CredentialProvider",
        "UpdateOauth2CredentialProvider",
        "DeleteOauth2CredentialProvider",
        "TagResource",
        "ListTagsForResource",
    )
    cases = [("bedrock-agentcore:CreateTokenVault", vault)]
    cases += [(f"bedrock-agentcore:{a}", r) for a in identity_actions for r in (directory, identity)]
    cases += [(f"bedrock-agentcore:{a}", r) for a in provider_actions for r in (vault, provider)]
    # Negative twin: a foreign directory/vault id must stay implicitly denied.
    negatives = [
        ("bedrock-agentcore:CreateWorkloadIdentity", f"{base}:workload-identity-directory/other"),
        ("bedrock-agentcore:TagResource", f"{base}:runtime/abc"),
    ]

    iam = boto3.client("iam", region_name=args.region)
    verdict: dict[str, Any] = {"allowed": [], "denied": [], "negativeDenied": [], "negativeAllowed": []}
    for action, resource in cases:
        res = iam.simulate_custom_policy(PolicyInputList=docs, ActionNames=[action], ResourceArns=[resource])
        decision = res["EvaluationResults"][0]["EvalDecision"]
        (verdict["allowed"] if decision == "allowed" else verdict["denied"]).append(
            {"action": action, "resourceSuffix": resource.split(":", 5)[-1], "decision": decision}
        )
    for action, resource in negatives:
        res = iam.simulate_custom_policy(PolicyInputList=docs, ActionNames=[action], ResourceArns=[resource])
        decision = res["EvaluationResults"][0]["EvalDecision"]
        (verdict["negativeDenied"] if decision != "allowed" else verdict["negativeAllowed"]).append(
            {"action": action, "resourceSuffix": resource.split(":", 5)[-1], "decision": decision}
        )

    bare_star = [
        s.get("Sid")
        for p in policies
        for s in p["PolicyDocument"]["Statement"]
        if (s.get("Resource") == "*" or s.get("Resource") == ["*"]) and s.get("Sid") != "DecryptPlatformM2mSecret"
    ]
    verdict["bareWildcardStatements"] = bare_star
    verdict["passed"] = not verdict["denied"] and not verdict["negativeAllowed"] and not bare_star
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    sys.exit(main())
