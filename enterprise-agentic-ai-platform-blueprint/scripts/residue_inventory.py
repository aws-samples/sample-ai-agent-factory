#!/usr/bin/env python3
"""residue_inventory.py — READ-ONLY inventory of what this blueprint left in an account.

Lists every resource whose name marks it as the blueprint's (stack-derived
prefixes, the `agenticai` / `AgenticAI` naming convention, AgentCore resource
names) across the surfaces the pipelines and teardown touch, so a teardown can
be measured instead of assumed: run it before and after, and the "after"
column must be zero for every surface except customer-managed keys in
``PendingDeletion`` (the 7-day window is the only sanctioned residue).

It never writes. It prints counts per surface and, with ``--names``, the
resource names (no ARNs, no account ids). Surfaces a region does not offer are
reported as ``n/a`` with the error code rather than as zero.

    python3 scripts/residue_inventory.py --expected-account <12-digit id> --region us-west-2
    python3 scripts/residue_inventory.py --expected-account <12-digit id> --region us-west-2 --global --json out.json

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Callable, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

CFG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 4, "mode": "standard"})

#: Names the blueprint creates start with one of these (case-sensitive where
#: CloudFormation derives them from a stack name, lower-case where the code
#: names them explicitly).
PROJECT_NAME = re.compile(r"^(AgenticAI|agenticai|Nonprod-|Prod-|WorkloadPipeline|PlatformPipeline)")
#: S3 bucket names are lower-case versions of the same stack-derived prefixes.
PROJECT_BUCKET = re.compile(r"^(agenticai|nonprod-|prod-)")
#: Log groups: service prefixes followed by a project name.
PROJECT_LOG_GROUP = re.compile(
    r"^(/aws/(lambda|codebuild|bedrock-agentcore/runtimes|vendedlogs/[^/]+)/(AgenticAI|agenticai|Nonprod-|Prod-|WorkloadPipeline|PlatformPipeline)|/agenticai/)"
)
#: AgentCore Identity keeps OAuth2 provider secrets under this service prefix.
PROJECT_SECRET = re.compile(r"(^(AgenticAI|agenticai)|^bedrock-agentcore-identity!default/oauth2/(AgenticAI|agenticai))")
#: The exact customer-managed-key descriptions the blueprint's constructs emit
#: (generated from every `new Key(...)` in packages/, apps/ and pipelines/).
#: Matching on these rather than on a loose substring such as "AgentCore" keeps
#: another team's keys out of the count and catches keys whose description does
#: not name the project (the EU AI Act record-keeping key, for example).
PROJECT_KEY_DESCRIPTION = re.compile(
    r"^("
    r"CMK for API Gateway fronting access logs\."
    r"|CMK for AgentCore Identity Token Vault \(spec §3\.3\.5\)\."
    r"|CMK for Bedrock Model Invocation Logging log group\."
    r"|CMK for CloudTrail archive, CUR bucket, and cross-account CWL destination\."
    r"|CMK for LiteLLM gateway logs \+ config secrets\."
    r"|CMK for VPC flow-log encryption\."
    r"|CMK for per-app CloudWatch alarm SNS topic\."
    r"|CMK for short-lived AgenticAI pipeline artifacts"
    r"|Platform-level Bedrock Model Invocation Logging CMK\."
    r"|Platform-shared registry \+ experiment-tracking CMK\."
    r"|AgentCore Memory CMK for .+ \(spec §3\.4\.7\)\."
    r"|AgentCore Registry CMK \(.+\)\."
    r"|CMK for .+ Platform tool Lambda logs\."
    r"|CMK for AgentCore Gateway \(.+\) logs \+ configuration\."
    r"|CMK for AgentCore Runtime .+\."
    r"|CMK for AgentCore evaluation-gates corpus \+ run history \(.+\)\."
    r"|CMK for Gateway PolicyEngine .+ \(.+\)\."
    r"|CMK for RAG source bucket .+\."
    r"|CMK for agent-version history \(.+\)\."
    r"|CMK for catalogue-drift logs \(.+\)\."
    r"|CMK for online-evaluation samples \(.+\)\."
    r"|CMK for the cross-account inference M2M secret \(.+\)\."
    r"|Chargeback CSV CMK \(.+\)\."
    r"|EU AI Act record-keeping CMK \(.+\)\."
    r"|HITL CMK \(.+\)\."
    r"|Kill-switch CMK \(.+\)\."
    r"|Per-tenant quota state CMK \(.+\)\."
    r"|Workload-local AgentCore Memory CMK for .+\."
    r"|Workstream-local AgentCore Memory CMK for .+\."
    r")$"
)


def is_project_key_description(description: str) -> bool:
    return bool(PROJECT_KEY_DESCRIPTION.match(description or ""))


def is_project_name(name: str) -> bool:
    return bool(name) and bool(PROJECT_NAME.match(name))


def is_project_bucket(name: str) -> bool:
    return bool(PROJECT_BUCKET.match(name))


def is_project_log_group(name: str) -> bool:
    return bool(PROJECT_LOG_GROUP.match(name))


def is_project_secret(name: str) -> bool:
    return bool(PROJECT_SECRET.match(name))


def paged(client, operation: str, key: str, **kwargs) -> Iterable[dict]:
    if client.can_paginate(operation):
        for page in client.get_paginator(operation).paginate(**kwargs):
            yield from page.get(key, [])
        return
    token_in, token_out = "nextToken", "nextToken"
    while True:
        resp = getattr(client, operation)(**kwargs)
        yield from resp.get(key, [])
        token = resp.get(token_out)
        if not token:
            return
        kwargs[token_in] = token


class Inventory:
    def __init__(self, session: boto3.Session, region: str) -> None:
        self.session, self.region = session, region
        self.rows: dict[str, dict] = {}

    def client(self, name: str):
        return self.session.client(name, region_name=self.region, config=CFG)

    def surface(self, label: str, fn: Callable[[], list[str]]) -> None:
        try:
            names = sorted(fn())
            self.rows[label] = {"count": len(names), "names": names}
        except (ClientError, EndpointConnectionError) as error:
            code = error.response["Error"]["Code"] if isinstance(error, ClientError) else "NoEndpoint"
            self.rows[label] = {"count": None, "error": code}

    # ------------------------------------------------------------- surfaces

    def stacks(self) -> list[str]:
        cfn = self.client("cloudformation")
        live = [s for s in paged(cfn, "list_stacks", "StackSummaries") if s["StackStatus"] != "DELETE_COMPLETE"]
        return [s["StackName"] for s in live if is_project_name(s["StackName"])]

    def lambdas(self) -> list[str]:
        return [f["FunctionName"] for f in paged(self.client("lambda"), "list_functions", "Functions") if is_project_name(f["FunctionName"])]

    def log_groups(self) -> list[str]:
        return [g["logGroupName"] for g in paged(self.client("logs"), "describe_log_groups", "logGroups") if is_project_log_group(g["logGroupName"])]

    def agentcore(self, operation: str, key: str, name_key: str) -> Callable[[], list[str]]:
        def run() -> list[str]:
            items = paged(self.client("bedrock-agentcore-control"), operation, key)
            return [i.get(name_key, "") for i in items if is_project_name(i.get(name_key, ""))]

        return run

    def registries(self) -> list[str]:
        items = paged(self.client("agent-registry-control"), "list_registries", "registries")
        return [r["name"] for r in items if is_project_name(r.get("name", ""))]

    def secrets(self) -> list[str]:
        items = paged(self.client("secretsmanager"), "list_secrets", "SecretList", IncludePlannedDeletion=True)
        return [s["Name"] + (" (scheduled)" if s.get("DeletedDate") else "") for s in items if is_project_secret(s["Name"])]

    def kms_keys(self, state: str) -> Callable[[], list[str]]:
        def run() -> list[str]:
            kms = self.client("kms")
            aliases = {}
            for alias in paged(kms, "list_aliases", "Aliases"):
                if alias.get("TargetKeyId"):
                    aliases.setdefault(alias["TargetKeyId"], []).append(alias["AliasName"])
            out = []
            for key in paged(kms, "list_keys", "Keys"):
                meta = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]
                if meta["KeyManager"] != "CUSTOMER" or meta["KeyState"] != state:
                    continue
                names = aliases.get(key["KeyId"], [])
                project_alias = any(n.startswith("alias/agenticai") or n.startswith("alias/AgenticAI") for n in names)
                project_desc = is_project_key_description(meta.get("Description", ""))
                if project_alias or project_desc or self._project_tagged(kms, key["KeyId"]):
                    out.append((names[0] if names else f"key ...{key['KeyId'][-4:]}") + f" [{meta.get('Description', '')[:50]}]")
            return out

        return run

    def _project_tagged(self, kms, key_id: str) -> bool:
        try:
            tags = kms.list_resource_tags(KeyId=key_id).get("Tags", [])
        except ClientError:
            return False
        keys = {t["TagKey"]: t["TagValue"] for t in tags}
        stack = keys.get("aws:cloudformation:stack-name", "")
        return "application-id" in keys or is_project_name(stack)

    def ssm_parameters(self) -> list[str]:
        items = paged(
            self.client("ssm"),
            "describe_parameters",
            "Parameters",
            ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": ["/agenticai/"]}],
        )
        return [p["Name"] for p in items]

    def tables(self) -> list[str]:
        return [t for t in paged(self.client("dynamodb"), "list_tables", "TableNames") if is_project_name(t)]

    def user_pools(self) -> list[str]:
        items = paged(self.client("cognito-idp"), "list_user_pools", "UserPools", MaxResults=60)
        return [p["Name"] for p in items if is_project_name(p["Name"])]

    def pipelines(self) -> list[str]:
        return [p["name"] for p in paged(self.client("codepipeline"), "list_pipelines", "pipelines") if is_project_name(p["name"])]

    def codebuild(self) -> list[str]:
        return [p for p in paged(self.client("codebuild"), "list_projects", "projects") if is_project_name(p)]

    def guardrails(self) -> list[str]:
        return [g["name"] for g in paged(self.client("bedrock"), "list_guardrails", "guardrails") if is_project_name(g["name"])]

    def state_machines(self) -> list[str]:
        return [m["name"] for m in paged(self.client("stepfunctions"), "list_state_machines", "stateMachines") if is_project_name(m["name"])]

    def alarms(self) -> list[str]:
        cw = self.client("cloudwatch")
        names = [a["AlarmName"] for a in paged(cw, "describe_alarms", "MetricAlarms")]
        return [n for n in names if is_project_name(n)]

    def delivery_sources(self) -> list[str]:
        return [d["name"] for d in paged(self.client("logs"), "describe_delivery_sources", "deliverySources") if "agenticai" in d["name"].lower()]

    def ecr_repositories(self) -> list[str]:
        return [r["repositoryName"] for r in paged(self.client("ecr"), "describe_repositories", "repositories") if is_project_name(r["repositoryName"])]

    # --------------------------------------------------------------- global

    def roles(self) -> list[str]:
        iam = self.session.client("iam", config=CFG)
        return [r["RoleName"] for r in paged(iam, "list_roles", "Roles") if is_project_name(r["RoleName"])]

    def buckets(self) -> list[str]:
        s3 = self.session.client("s3", config=CFG)
        return [b["Name"] for b in s3.list_buckets().get("Buckets", []) if is_project_bucket(b["Name"])]

    def collect(self, include_global: bool) -> dict:
        s = self.surface
        s("cloudformation stacks", self.stacks)
        s("codepipeline pipelines", self.pipelines)
        s("codebuild projects", self.codebuild)
        s("lambda functions", self.lambdas)
        s("step functions state machines", self.state_machines)
        s("log groups", self.log_groups)
        s("logs delivery sources", self.delivery_sources)
        s("agentcore runtimes", self.agentcore("list_agent_runtimes", "agentRuntimes", "agentRuntimeName"))
        s("agentcore memories", self.agentcore("list_memories", "memories", "id"))
        s("agentcore gateways", self.agentcore("list_gateways", "items", "name"))
        s("agentcore workload identities", self.agentcore("list_workload_identities", "workloadIdentities", "name"))
        s("agentcore oauth2 providers", self.agentcore("list_oauth2_credential_providers", "credentialProviders", "name"))
        s("agentcore policy engines", self.agentcore("list_policy_engines", "policyEngines", "name"))
        s("agent registries", self.registries)
        s("secrets (incl. scheduled)", self.secrets)
        s("kms keys Enabled", self.kms_keys("Enabled"))
        s("kms keys PendingDeletion", self.kms_keys("PendingDeletion"))
        s("ssm parameters /agenticai/", self.ssm_parameters)
        s("dynamodb tables", self.tables)
        s("cognito user pools", self.user_pools)
        s("bedrock guardrails", self.guardrails)
        s("cloudwatch alarms", self.alarms)
        s("ecr repositories", self.ecr_repositories)
        if include_global:
            s("iam roles (global)", self.roles)
            s("s3 buckets (global)", self.buckets)
        return self.rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expected-account", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--global", dest="include_global", action="store_true", help="also IAM roles and S3 buckets (account-wide)")
    ap.add_argument("--names", action="store_true", help="print resource names, not just counts")
    ap.add_argument("--json", help="write the full result to this file")
    args = ap.parse_args()
    session = boto3.Session(region_name=args.region)
    actual = session.client("sts").get_caller_identity()["Account"]
    if actual != args.expected_account:
        raise SystemExit(f"REFUSING: credentials belong to ...{actual[-4:]}, expected ...{args.expected_account[-4:]}")
    rows = Inventory(session, args.region).collect(args.include_global)
    for label, row in rows.items():
        value = "n/a (" + row["error"] + ")" if row.get("count") is None else str(row["count"])
        print(f"{label:34} {value}")
        if args.names and row.get("names"):
            for name in row["names"]:
                print(f"{'':36}- {name}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"region": args.region, "surfaces": rows}, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
