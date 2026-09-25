#!/usr/bin/env python3
"""final_teardown.py — dependency-ordered, fail-closed, resumable teardown of
the pipeline topology (the stacks the Platform and Workload pipelines deploy).

`scripts/teardown.sh` destroys the directly-deployed stages through
`cdk destroy`; it has no mapping for the pipeline-deployed stage stacks
(`Nonprod-/Prod-{Registry,InferenceGateway,Guardrail}`, `Nonprod-Audit`,
`Nonprod-LogArchive`). This tool removes exactly those, plus the pipeline roots
and the Workstream stacks, one account at a time.

Dry-run by default: prints exactly what would be deleted and changes nothing.
With ``--apply`` it deletes the account's blueprint stacks one at a time in
dependency order, waits for each to reach DELETE_COMPLETE and STOPS at the
first failure (printing the failed events) so nothing is deleted out of order.

Everything that outlives ``delete-stack`` is read BEFORE the stack goes and
written to a plan file under ``--state-dir`` (default ``~/.agenticai-teardown``,
outside the checkout because it holds physical ids), so an interrupted run (a
dropped CloudShell session, an expired credential) is finished with
``--resume-residue`` instead of being lost with the stack:

* resources with ``DeletionPolicy: Retain`` / ``RetainExceptOnCreate``: KMS
  keys are scheduled for deletion with a 7-day window, SSM parameters and log
  groups are deleted, Cognito user pools have deletion protection switched off
  and are deleted, DynamoDB tables are deleted, AgentCore GA Registry records
  and then their Registry are deleted;
* service-created log groups of the stack's Lambda functions, CodeBuild
  projects and AgentCore Runtimes;
* the agent image digests the stack's Runtime versions referenced in the CDK
  container-asset repository (de-duplicated across stacks: both environments
  share digests).

Stack names are fixed per account role and the caller must pass the 12-digit
account the credentials must belong to, so the tool cannot delete in the wrong
account or touch unrelated stacks. Run the roles in this order — workstream,
then platform, then management — each with that account's credentials:

    python3 scripts/final_teardown.py --account-role workstream --expected-account <12-digit id>
    python3 scripts/final_teardown.py --account-role workstream --expected-account <12-digit id> --apply
    python3 scripts/final_teardown.py --account-role workstream --expected-account <12-digit id> --resume-residue

Retire the Platform tool-alias grants first only when the Platform Registry
stacks stay (the grants are deleted with the aliases when they go too).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, WaiterError

DEFAULT_REGION = "us-west-2"


def build_plan(tenant: str, agent: str) -> dict[str, list[str]]:
    """Dependency order per account (first deleted first).

    Consumers before producers: RuntimeMemory -> ToolGateway -> RegistryRoles
    (ToolGateway imports its execution roles from RegistryRoles by name),
    InferenceGateway -> Guardrail (imports the baseline guardrail export),
    pipeline roots first so no execution can redeploy mid-teardown.
    """
    prefix = f"AgenticAI-{tenant}-{agent}"
    return {
        "workstream": [
            f"{prefix}-prod-RuntimeMemory",
            f"{prefix}-nonprod-RuntimeMemory",
            f"{prefix}-prod-ToolGateway",
            f"{prefix}-nonprod-ToolGateway",
            f"{prefix}-prod-RegistryRoles",
            f"{prefix}-nonprod-RegistryRoles",
        ],
        "platform": [
            "AgenticAI-WorkloadPipelineStack",
            "AgenticAI-PlatformPipelineStack",
            "Prod-InferenceGateway",
            "Nonprod-InferenceGateway",
            "Prod-Guardrail",
            "Nonprod-Guardrail",
            "Prod-Registry",
            "Nonprod-Registry",
        ],
        "management": ["Nonprod-Audit", "Nonprod-LogArchive"],
    }


ROLES = ("management", "platform", "workstream")
RETAIN = {"Retain", "RetainExceptOnCreate"}
#: Delete order for retained resources inside one stack: Registry records
#: before their Registry, DynamoDB tables and everything else next, customer
#: managed keys LAST (a table or Registry encrypted with a key must go before
#: the key is scheduled for deletion).
RETAIN_ORDER = {
    "AWS::AgentRegistry::RegistryRecord": 0,
    "AWS::AgentRegistry::Registry": 1,
    "AWS::KMS::Key": 9,
}
CFG = Config(retries={"max_attempts": 5, "mode": "standard"})
#: Only these codes mean "already deleted"; anything else (throttling,
#: validation, access denied) stops the run so it can be re-run.
GONE_CODES = {"ResourceNotFoundException", "ParameterNotFound", "NotFoundException"}


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%SZ", time.gmtime()), *parts, flush=True)


#: Every (service, operation) the run may call. Checked against the installed
#: botocore BEFORE anything is deleted: an older SDK (for example a stale
#: CloudShell boto3) would otherwise delete a stack and then fail to clean what
#: it retained.
REQUIRED_OPERATIONS = {
    "cloudformation": ("DeleteStack", "GetTemplate", "ListStackResources"),
    "bedrock-agentcore-control": ("ListAgentRuntimeVersions", "GetAgentRuntime"),
    "agent-registry-control": ("ListRegistryRecords", "DeleteRegistryRecord", "DeleteRegistry"),
    "cognito-idp": ("UpdateUserPool", "DeleteUserPool"),
    "dynamodb": ("DeleteTable",),
    "ecr": ("BatchDeleteImage",),
    "kms": ("ScheduleKeyDeletion",),
    "logs": ("DeleteLogGroup",),
    "ssm": ("DeleteParameter",),
}


def missing_operations(session: boto3.Session) -> list[str]:
    """(service, operation) pairs the installed botocore does not model."""
    available = set(session.get_available_services())
    missing = []
    for service, operations in REQUIRED_OPERATIONS.items():
        if service not in available:
            missing.append(f"{service} (service unknown to botocore)")
            continue
        model = session.client(service, region_name=session.region_name or DEFAULT_REGION).meta.service_model
        missing.extend(f"{service}:{op}" for op in operations if op not in model.operation_names)
    return missing


class Account:
    def __init__(self, expected: str, region: str) -> None:
        self.session = boto3.Session(region_name=region)
        self.region = region
        gaps = missing_operations(self.session)
        if gaps:
            raise SystemExit(
                "REFUSING: the installed boto3/botocore is too old for this teardown (missing "
                + ", ".join(gaps)
                + "). Upgrade it first: python3 -m pip install --user 'boto3==1.43.98' (the version this tool was proven with)"
            )
        actual = self.session.client("sts").get_caller_identity()["Account"]
        if actual != expected:
            raise SystemExit(f"REFUSING: credentials belong to ...{actual[-4:]}, expected ...{expected[-4:]}")
        self.account = actual
        self.cfn = self.session.client("cloudformation", config=CFG)
        self._clients: dict[str, object] = {}

    def client(self, name: str):
        if name not in self._clients:
            self._clients[name] = self.session.client(name, config=CFG)
        return self._clients[name]


#: Where the resumable plan lives. Outside the repository checkout on purpose:
#: the plan holds physical resource ids and must never be committed.
DEFAULT_STATE_DIR = Path.home() / ".agenticai-teardown"


def plan_path(state_dir: Path, role: str, region: str) -> Path:
    return state_dir / f"final-teardown-{role}-{region}-plan.json"


def load_plan(state_dir: Path, role: str, region: str) -> dict:
    path = plan_path(state_dir, role, region)
    if path.exists():
        return json.loads(path.read_text())
    return {"stacks": {}, "images": []}


def save_plan(state_dir: Path, role: str, region: str, plan: dict) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = plan_path(state_dir, role, region)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=2, sort_keys=True))
    tmp.replace(path)


def stack_status(acct: Account, name: str) -> str | None:
    try:
        return acct.cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
    except ClientError as error:
        if "does not exist" in str(error):
            return None
        raise


def collect(acct: Account, name: str) -> dict:
    """Everything that outlives `delete-stack`, read before the stack goes."""
    body = acct.cfn.get_template(StackName=name, TemplateStage="Processed")["TemplateBody"]
    template = body if isinstance(body, dict) else json.loads(body)
    policies = {lid: r.get("DeletionPolicy") for lid, r in template.get("Resources", {}).items()}
    out: dict = {"retained": [], "logGroups": [], "images": [], "runtimeIds": []}
    groups: set[str] = set()
    images: set[str] = set()
    for page in acct.cfn.get_paginator("list_stack_resources").paginate(StackName=name):
        for res in page["StackResourceSummaries"]:
            lid, rtype, pid = res["LogicalResourceId"], res["ResourceType"], res.get("PhysicalResourceId")
            if not pid:
                continue
            if policies.get(lid) in RETAIN:
                out["retained"].append([rtype, pid])
            if rtype == "AWS::Lambda::Function":
                groups.add(f"/aws/lambda/{pid}")
            elif rtype == "AWS::CodeBuild::Project":
                groups.add(f"/aws/codebuild/{pid}")
            elif rtype == "AWS::BedrockAgentCore::Runtime":
                out["runtimeIds"].append(pid.rsplit("/", 1)[-1])
    control = acct.client("bedrock-agentcore-control")
    logs = acct.client("logs")
    for runtime_id in out["runtimeIds"]:
        try:
            versions = control.list_agent_runtime_versions(agentRuntimeId=runtime_id)["agentRuntimes"]
        except ClientError:
            versions = []
        for version in versions:
            detail = control.get_agent_runtime(
                agentRuntimeId=runtime_id, agentRuntimeVersion=version["agentRuntimeVersion"]
            )
            uri = detail.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri", "")
            if "@sha256:" in uri:
                images.add(uri)
        for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{runtime_id}"
        ):
            groups.update(g["logGroupName"] for g in page["logGroups"])
    out["retained"].sort(key=lambda item: RETAIN_ORDER.get(item[0], 5))
    out["logGroups"] = sorted(groups)
    out["images"] = sorted(images)
    return out


def failed_events(acct: Account, name: str) -> list[str]:
    events = acct.cfn.describe_stack_events(StackName=name)["StackEvents"][:40]
    return [
        f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}"
        for e in events
        if "FAILED" in e["ResourceStatus"]
    ]


def delete_stack(acct: Account, name: str) -> None:
    acct.cfn.delete_stack(StackName=name)
    try:
        acct.cfn.get_waiter("stack_delete_complete").wait(
            StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 240}
        )
    except WaiterError as error:
        for line in failed_events(acct, name):
            log("   FAILED", line[:300])
        raise SystemExit(
            f"STOPPED: {name} did not reach DELETE_COMPLETE ({error}); later stacks were NOT deleted"
        ) from error
    log("   DELETE_COMPLETE", name)


def registry_id_from(pid: str) -> str:
    # arn:aws:agent-registry:<r>:<acct>:registry/<registryId>[/record/<recordId>]
    return pid.split(":registry/", 1)[1].split("/", 1)[0]


def delete_registry(acct: Account, pid: str) -> None:
    client = acct.client("agent-registry-control")
    registry_id = registry_id_from(pid)
    # Any record CloudFormation did not know about still blocks the delete.
    token = None
    while True:
        kwargs = {"registryId": registry_id, "maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_registry_records(**kwargs)
        for record in resp.get("registryRecords", resp.get("records", [])):
            record_id = record.get("recordId") or record.get("registryRecordId")
            if record_id:
                log(f"      stray record ...{record_id[-4:]} -> delete")
                client.delete_registry_record(registryId=registry_id, recordId=record_id)
        token = resp.get("nextToken")
        if not token:
            break
    client.delete_registry(registryId=registry_id)


HANDLERS = {
    "AWS::KMS::Key": "schedule key deletion (7 days)",
    "AWS::SSM::Parameter": "delete parameter",
    "AWS::Logs::LogGroup": "delete log group",
    "AWS::Cognito::UserPool": "disable deletion protection, delete user pool",
    "AWS::DynamoDB::Table": "delete table",
    "AWS::AgentRegistry::RegistryRecord": "delete registry record",
    "AWS::AgentRegistry::Registry": "delete registry (after its records)",
}


def clean_one_retained(acct: Account, rtype: str, pid: str) -> None:
    if rtype == "AWS::KMS::Key":
        kms = acct.client("kms")
        if kms.describe_key(KeyId=pid)["KeyMetadata"]["KeyState"] != "PendingDeletion":
            kms.schedule_key_deletion(KeyId=pid, PendingWindowInDays=7)
    elif rtype == "AWS::SSM::Parameter":
        acct.client("ssm").delete_parameter(Name=pid)
    elif rtype == "AWS::Logs::LogGroup":
        acct.client("logs").delete_log_group(logGroupName=pid)
    elif rtype == "AWS::Cognito::UserPool":
        # UpdateUserPool resets every setting it is not given; only call it
        # when deletion protection is actually on.
        cognito = acct.client("cognito-idp")
        if cognito.describe_user_pool(UserPoolId=pid)["UserPool"].get("DeletionProtection") == "ACTIVE":
            cognito.update_user_pool(UserPoolId=pid, DeletionProtection="INACTIVE")
        cognito.delete_user_pool(UserPoolId=pid)
    elif rtype == "AWS::DynamoDB::Table":
        ddb = acct.client("dynamodb")
        ddb.delete_table(TableName=pid)
        ddb.get_waiter("table_not_exists").wait(TableName=pid, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    elif rtype == "AWS::AgentRegistry::RegistryRecord":
        registry_id = registry_id_from(pid)
        record_id = pid.rsplit("/record/", 1)[1]
        acct.client("agent-registry-control").delete_registry_record(registryId=registry_id, recordId=record_id)
    elif rtype == "AWS::AgentRegistry::Registry":
        delete_registry(acct, pid)


def clean_retained(acct: Account, retained: list, apply: bool) -> list:
    """Returns the retained resources that could NOT be cleaned (for the report)."""
    left = []
    for rtype, pid in retained:
        action = HANDLERS.get(rtype, "LEFT IN PLACE (no handler; report)")
        log(f"   retained {rtype} {pid} -> {action}")
        if rtype not in HANDLERS:
            left.append([rtype, pid])
            continue
        if not apply:
            continue
        try:
            clean_one_retained(acct, rtype, pid)
        except ClientError as error:
            if error.response["Error"]["Code"] in GONE_CODES:
                log("      already gone")
            else:
                raise
    return left


def clean_service_residue(acct: Account, groups: list, apply: bool) -> None:
    logs = acct.client("logs")
    for group in groups:
        matches = logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", [])
        if not any(g["logGroupName"] == group for g in matches):
            continue
        log(f"   service log group {group} -> delete")
        if apply:
            try:
                logs.delete_log_group(logGroupName=group)
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise


def clean_images(acct: Account, images: list, apply: bool) -> None:
    ecr = acct.client("ecr")
    for uri in images:
        repo = uri.split("/", 1)[1].split("@", 1)[0]
        digest = uri.split("@", 1)[1]
        log(f"   agent image {repo}@{digest[:19]}… -> delete")
        if not apply:
            continue
        resp = ecr.batch_delete_image(repositoryName=repo, imageIds=[{"imageDigest": digest}])
        for failure in resp.get("failures", []):
            if failure.get("failureCode") == "ImageNotFound":
                log("      already gone")
            else:
                raise SystemExit(f"STOPPED: image delete failed: {failure.get('failureCode')} {failure.get('failureReason')}")


def run_residue(acct: Account, plan: dict, apply: bool) -> list:
    left = []
    for name, collected in plan["stacks"].items():
        if collected.get("residueDone"):
            continue
        log("RESIDUE", name)
        left.extend(clean_retained(acct, collected["retained"], apply))
        clean_service_residue(acct, collected["logGroups"], apply)
        if apply:
            collected["residueDone"] = True
    clean_images(acct, plan["images"], apply)
    if apply:
        plan["imagesDone"] = True
    return left


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account-role", required=True, choices=ROLES)
    ap.add_argument("--expected-account", required=True)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--tenant-id", default="demo")
    ap.add_argument("--agent-id", default="primary")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="where the resumable plan is kept")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    mode.add_argument("--resume-residue", action="store_true", help="finish the residue cleanup from the saved plan")
    args = ap.parse_args()
    acct = Account(args.expected_account, args.region)
    role, region, state = args.account_role, args.region, args.state_dir
    stacks = build_plan(args.tenant_id, args.agent_id)[role]
    plan = load_plan(state, role, region) if (args.apply or args.resume_residue) else {"stacks": {}, "images": []}
    mode_name = "APPLY" if args.apply else "RESUME RESIDUE" if args.resume_residue else "DRY RUN (nothing is changed)"
    log("MODE", mode_name, "| role", role, "| region", region)

    if not args.resume_residue:
        for name in stacks:
            status = stack_status(acct, name)
            if status is None:
                log("ABSENT", name, "(residue from the saved plan is still cleaned)" if name in plan["stacks"] else "")
                continue
            if status.endswith("_IN_PROGRESS"):
                raise SystemExit(f"STOPPED: {name} is {status}; wait for it to settle and re-run")
            log("STACK", name, status)
            collected = collect(acct, name)
            plan["stacks"][name] = {**collected, "residueDone": False}
            plan["images"] = sorted(set(plan["images"]) | set(collected["images"]))
            if args.apply:
                save_plan(state, role, region, plan)  # BEFORE the delete, so an interruption loses nothing
                delete_stack(acct, name)
            else:
                log("   would delete-stack and wait for DELETE_COMPLETE")
        remaining = [n for n in stacks if stack_status(acct, n) is not None]
        if args.apply and remaining:
            log("REMAINING", remaining)
            return 3

    left = run_residue(acct, plan, args.apply or args.resume_residue)
    if args.apply or args.resume_residue:
        save_plan(state, role, region, plan)
    remaining = [n for n in stacks if stack_status(acct, n) is not None]
    log("REMAINING STACKS", remaining or "none")
    log("UNHANDLED RETAINED", left or "none")
    if args.apply or args.resume_residue:
        return 0 if not remaining and not left else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
