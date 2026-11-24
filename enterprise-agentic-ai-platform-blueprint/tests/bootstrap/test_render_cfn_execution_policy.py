"""Contract tests for render-cfn-execution-policy.py."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pipelines" / "bootstrap" / "render-cfn-execution-policy.py"
REGION = "eu-west-1"
PLATFORM = "111111111111"
WORKSTREAM = "222222222222"
MANAGEMENT = "333333333333"
CONNECTION = "arn:aws:codeconnections:us-west-2:111111111111:connection/example"
OAM_SOURCE_LINK_ACTIONS = {
    "oam:CreateLink",
    "oam:DeleteLink",
    "oam:GetLink",
    "oam:ListLinks",
    "oam:ListTagsForResource",
    "oam:TagResource",
    "oam:UntagResource",
    "oam:UpdateLink",
    "xray:Link",
}


AUDIT = "444444444444"
ORGANIZATION = "o-exampleorg0a"
IAM_MANAGED_POLICY_MAX_CHARS = 6144


def render(role: str, *args: str) -> dict:
    extra = list(args)
    if role == "management" and "--organization-id" not in extra:
        extra += ["--organization-id", ORGANIZATION]
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            role,
            "--account-id",
            {
                "platform": PLATFORM,
                "workstream": WORKSTREAM,
                "management": MANAGEMENT,
                "audit": AUDIT,
                "hub-role-boundary": AUDIT,
            }[role],
            "--region",
            REGION,
            *extra,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def all_actions(policy: dict) -> set[str]:
    return {
        action
        for statement in policy["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }


def by_sid(policy: dict, sid: str) -> dict:
    return next(
        statement for statement in policy["Statement"] if statement["Sid"] == sid
    )


def actions(statement: dict) -> set[str]:
    value = statement["Action"]
    return {value} if isinstance(value, str) else set(value)


def test_platform_policy_scopes_ireland_resources_and_cross_account_roles() -> None:
    policy = render(
        "platform",
        "--target-account-id",
        WORKSTREAM,
        "--target-account-id",
        MANAGEMENT,
        "--connection-arn",
        CONNECTION,
    )

    assert by_sid(policy, "UsePlatformSourceConnection")["Resource"] == CONNECTION
    deployment_roles = by_sid(
        policy, "PassCrossAccountCdkDeploymentRolesToCodePipeline"
    )
    assert set(deployment_roles["Resource"]) == {
        f"arn:aws:iam::{WORKSTREAM}:role/cdk-hnb659fds-deploy-role-{WORKSTREAM}-{REGION}",
        f"arn:aws:iam::{MANAGEMENT}:role/cdk-hnb659fds-deploy-role-{MANAGEMENT}-{REGION}",
    }
    assert deployment_roles["Condition"] == {
        "StringEquals": {"iam:PassedToService": "codepipeline.amazonaws.com"}
    }
    registry = by_sid(policy, "ManageTaggedGaRegistries")
    assert registry["Resource"] == (
        f"arn:aws:agent-registry:{REGION}:{PLATFORM}:registry/*"
    )
    assert "iam:ListEntitiesForPolicy" in actions(
        by_sid(policy, "ManageNamedDeploymentPolicies")
    )
    provision = actions(by_sid(policy, "ProvisionBlueprintResourceFamilies"))
    assert OAM_SOURCE_LINK_ACTIONS <= provision
    assert {"cloudwatch:*", "logs:*"} <= provision
    assert "oam:*" not in provision


def test_workstream_policy_retains_bounded_pass_role_and_delete_read() -> None:
    policy = render("workstream")
    provision = actions(by_sid(policy, "ProvisionWorkstreamResourceFamilies"))
    assert OAM_SOURCE_LINK_ACTIONS <= provision
    assert {"cloudwatch:*", "logs:*"} <= provision
    assert "oam:*" not in provision
    pass_role = by_sid(policy, "PassNamedDeploymentRolesToServices")
    assert pass_role["Condition"]["StringEquals"]["iam:PassedToService"] == [
        "bedrock-agentcore.amazonaws.com",
        "cloudformation.amazonaws.com",
        "ecs-tasks.amazonaws.com",
        "lambda.amazonaws.com",
        "states.amazonaws.com",
    ]
    assert all(
        resource.startswith(f"arn:aws:iam::{WORKSTREAM}:role/")
        for resource in pass_role["Resource"]
    )
    assert "iam:ListEntitiesForPolicy" in actions(
        by_sid(policy, "ManageNamedDeploymentPolicies")
    )


def test_management_policy_scopes_every_regional_arn_to_ireland() -> None:
    policy = render("management")
    assert by_sid(policy, "ReadCdkBootstrapVersion")["Resource"] == (
        f"arn:aws:ssm:{REGION}:{MANAGEMENT}:parameter/cdk-bootstrap/hnb659fds/version"
    )
    assert by_sid(policy, "ProvisionCentralLogStream")["Resource"] == (
        f"arn:aws:kinesis:{REGION}:{MANAGEMENT}:stream/agenticai-central-logs"
    )
    assert by_sid(policy, "ManageLogArchiveAutoDeleteProviderFunction")["Resource"] == (
        f"arn:aws:lambda:{REGION}:{MANAGEMENT}:function:Nonprod-LogArchive-CustomS3AutoDeleteObjects*"
    )
    delete_read = by_sid(policy, "ReadNamedDeploymentPolicyAttachments")
    assert delete_read["Action"] == "iam:ListEntitiesForPolicy"
    assert set(delete_read["Resource"]) == {
        f"arn:aws:iam::{MANAGEMENT}:policy/*AgenticAI*",
        f"arn:aws:iam::{MANAGEMENT}:policy/*agenticai*",
    }


def test_platform_requires_target_accounts_and_own_connection() -> None:
    missing_target = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "platform",
            "--account-id",
            PLATFORM,
            "--region",
            REGION,
            "--connection-arn",
            CONNECTION,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert missing_target.returncode != 0
    assert "requires at least one --target-account-id" in missing_target.stderr

    wrong_connection = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "platform",
            "--account-id",
            PLATFORM,
            "--region",
            REGION,
            "--target-account-id",
            WORKSTREAM,
            "--connection-arn",
            "arn:aws:codeconnections:us-west-2:999999999999:connection/example",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert wrong_connection.returncode != 0
    assert "connection in the Platform account" in wrong_connection.stderr


def test_invalid_account_and_region_fail_closed() -> None:
    for args in (
        ["workstream", "--account-id", "123", "--region", REGION],
        ["workstream", "--account-id", WORKSTREAM, "--region", "ireland"],
    ):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def bootstrap_context(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "agenticai/platformNonprodAccountId": PLATFORM,
                "agenticai/platformProdAccountId": "",
                "agenticai/logArchiveAccountId": MANAGEMENT,
                "agenticai/auditAccountId": MANAGEMENT,
                "agenticai/sandboxAccountId": "",
                "agenticai/workloadNonprodAccountId": WORKSTREAM,
                "agenticai/workloadProdAccountId": WORKSTREAM,
            }
        )
    )


def run_bootstrap(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    context = tmp_path / "context.json"
    bootstrap_context(context)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "cdk-calls.log"
    work_dir = tmp_path / "cdk-work"
    cdk = bin_dir / "cdk"
    cdk.write_text(
        '#!/usr/bin/env bash\nprintf "pwd=%s tmpdir=%s cdkhome=%s args=%s\\n" '
        '"$PWD" "$TMPDIR" "$CDK_HOME" "$*" >> "$CDK_CALLS"\n'
    )
    cdk.chmod(0o755)
    env = {
        "PATH": __import__('os').environ['PATH'],
        "CDK_CLI": str(cdk),
        "CDK_BOOTSTRAP_WORK_DIR": str(work_dir),
        "CDK_CALLS": str(calls),
        "AWS_REGION": REGION,
        "CFN_EXECUTION_POLICY_NAME": "AgenticAICdkExecutionPolicyEuWest1",
    }
    env.update(overrides)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "pipelines/bootstrap/bootstrap-cross-account.sh"),
            str(context),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    result.cdk_calls = calls.read_text().splitlines() if calls.exists() else []  # type: ignore[attr-defined]
    return result


def test_bootstrap_resolves_the_local_policy_arn_in_each_account(
    tmp_path: Path,
) -> None:
    result = run_bootstrap(tmp_path)
    assert result.returncode == 0, result.stderr
    calls = result.cdk_calls  # type: ignore[attr-defined]
    assert len(calls) == 3
    work_dir = tmp_path / "cdk-work"
    for account in (PLATFORM, MANAGEMENT, WORKSTREAM):
        call = next(item for item in calls if f"aws://{account}/{REGION}" in item)
        assert f"pwd={work_dir}" in call
        assert f"tmpdir={work_dir}" in call
        assert f"cdkhome={work_dir / 'cdk-home'}" in call
        assert "args=bootstrap " in call
        assert (
            f"arn:aws:iam::{account}:policy/AgenticAICdkExecutionPolicyEuWest1" in call
        )


def test_bootstrap_refuses_a_missing_region(tmp_path: Path) -> None:
    result = run_bootstrap(tmp_path, AWS_REGION="", AWS_DEFAULT_REGION="")
    assert result.returncode != 0
    assert "set AWS_REGION or AWS_DEFAULT_REGION" in result.stderr
    assert result.cdk_calls == []  # type: ignore[attr-defined]


def test_bootstrap_refuses_one_account_arn_for_multiple_accounts(
    tmp_path: Path,
) -> None:
    result = run_bootstrap(
        tmp_path,
        CFN_EXECUTION_POLICY_NAME="",
        CFN_EXECUTION_POLICY_ARN=(
            f"arn:aws:iam::{PLATFORM}:policy/AgenticAICdkExecutionPolicyEuWest1"
        ),
    )
    assert result.returncode != 0
    assert "can bootstrap only one target" in result.stderr
    assert result.cdk_calls == []  # type: ignore[attr-defined]


def test_bootstrap_refuses_application_work_directory(tmp_path: Path) -> None:
    app_dir = tmp_path / "application"
    app_dir.mkdir()
    (app_dir / "cdk.json").write_text('{"app":"must-not-run"}')
    result = run_bootstrap(tmp_path, CDK_BOOTSTRAP_WORK_DIR=str(app_dir))
    assert result.returncode != 0
    assert "must not contain cdk.json" in result.stderr
    assert result.cdk_calls == []  # type: ignore[attr-defined]


def test_audit_policy_pins_omni_hub_roles_and_pass_role_services() -> None:
    policy = render("audit")
    by_sid = {statement["Sid"]: statement for statement in policy["Statement"]}
    manage = by_sid["ReadAndTagOmniHubRoles"]
    assert all(":role/" in resource for resource in manage["Resource"])
    assert all(f"::{AUDIT}:" in resource for resource in manage["Resource"])
    assert any(resource.endswith("role/CloudWatchOmniOperatorRole") for resource in manage["Resource"])
    # Role names are pinned to the Region: no cross-Region wildcard families.
    assert f"arn:aws:iam::{AUDIT}:role/service-role/AgentCoreEvaluationRole-omni-{REGION}" in manage["Resource"]
    assert f"arn:aws:iam::{AUDIT}:role/AgenticAI-Omni-HubStack-{REGION}-*" in manage["Resource"]
    assert not any(resource.endswith("AgentCoreEvaluationRole-omni-*") or resource.endswith("AgenticAI-Omni-HubStack-*") for resource in manage["Resource"])
    # The organization domain and its access role are the management account's.
    assert not any("CloudWatchOmniDomainAccessRole" in resource for resource in manage["Resource"])
    # The golden-gate runner is managed here but never passed to a service.
    assert any(resource.endswith(f"role/AgenticAIGoldenGateRunner-{REGION}") for resource in manage["Resource"])
    # Only reads and tags are unconditioned; every mutation IAM can condition on a
    # boundary is gated (trust policy and deletion included), and the boundary
    # can never be removed.
    assert set(manage["Action"]) == {
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:TagRole",
        "iam:UntagRole",
    }
    gating = {
        "iam:AttachRolePolicy",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:PutRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:UpdateRole",
        "iam:UpdateRoleDescription",
    }
    operator = f"arn:aws:iam::{AUDIT}:role/CloudWatchOmniOperatorRole"
    omni_policies = {
        "arn:aws:iam::aws:policy/CloudWatchOmniSpaceAccessPolicy",
        "arn:aws:iam::aws:policy/CloudWatchOmniModelInferencePolicy",
        "arn:aws:iam::aws:policy/CloudWatchOmniAWSIntegrationPolicy",
    }
    # The operator role: only with the AWS-managed Omni boundary, only the
    # AWS-managed Omni policies attached, and never an inline policy.
    operator_gate = by_sid["ChangeBoundedOperatorRole"]
    assert set(operator_gate["Action"]) == gating - {"iam:PutRolePolicy", "iam:DeleteRolePolicy"}
    assert operator_gate["Resource"] == operator
    assert set(operator_gate["Condition"]) == {"StringEquals", "StringEqualsIfExists"}
    assert operator_gate["Condition"]["StringEquals"] == {
        "iam:PermissionsBoundary": "arn:aws:iam::aws:policy/CloudWatchOmniSpaceAccessPolicy"
    }
    assert set(operator_gate["Condition"]["StringEqualsIfExists"]["iam:PolicyARN"]) == omni_policies
    # Every other hub role: only while it carries this Region's blueprint boundary.
    hub_gate = by_sid["ChangeBoundedHubRoles"]
    assert set(hub_gate["Action"]) == gating
    assert set(hub_gate["Resource"]) == set(manage["Resource"]) - {operator}
    assert hub_gate["Condition"] == {
        "StringEquals": {"iam:PermissionsBoundary": f"arn:aws:iam::{AUDIT}:policy/AgenticAI-OmniHubRoleBoundary-{REGION}"}
    }
    assert "iam:DeleteRolePermissionsBoundary" not in all_actions(policy)
    for statement in policy["Statement"]:
        if gating & actions(statement) and any(":role/" in resource and "AgenticAI-LogArchive" not in resource for resource in resources(statement)):
            assert statement["Sid"] in {"ChangeBoundedOperatorRole", "ChangeBoundedHubRoles", "ApplicationSignalsPrerequisites"}, statement["Sid"]
        if "iam:PutRolePolicy" in actions(statement):
            assert operator not in resources(statement), statement["Sid"]
    pass_role = by_sid["PassOmniHubRolesToServices"]
    assert pass_role["Action"] == "iam:PassRole"
    assert not any("GoldenGateRunner" in resource for resource in pass_role["Resource"])
    services = set(pass_role["Condition"]["StringEquals"]["iam:PassedToService"])
    assert {"cloudwatch.amazonaws.com", "logs.amazonaws.com", "bedrock-agentcore.amazonaws.com"} <= services
    provision = by_sid["ProvisionOmniHubUnscoped"]
    assert "cloudwatch:CreateSpace" in provision["Action"]
    assert "cloudwatch:GetDomainForOrganization" in provision["Action"]
    assert "cloudwatch:CreateDomainForOrganization" not in provision["Action"]
    assert "observabilityadmin:CreateCentralizationRuleForOrganization" in provision["Action"]
    assert "xray:UpdateTraceSegmentDestination" in provision["Action"]
    # Never a bare cloudwatch:* or observabilityadmin:* in the hub statement.
    assert "cloudwatch:*" not in provision["Action"]
    assert "observabilityadmin:*" not in provision["Action"]
    assert "ProvisionGovernanceTelemetryStacks" not in by_sid
    assert "organizations:CreatePolicy" not in all_actions(policy)


def test_audit_policy_enumerates_kms_logs_and_oam_for_the_hub_and_the_sink() -> None:
    policy = render("audit")
    granted = all_actions(policy)
    # No service-wide or wildcard action anywhere in the audit document.
    assert not any(action.endswith(":*") or "*" in action for action in granted)

    # Resource-less actions share the hub's unscoped statement; list-handler-only
    # calls CloudFormation never makes during stack operations are absent.
    unscoped = by_sid(policy, "ProvisionOmniHubUnscoped")
    assert unscoped["Resource"] == "*" and "Condition" not in unscoped
    assert "application-signals:StartDiscovery" in unscoped["Action"]
    assert not any(action.startswith("kms:") for action in unscoped["Action"])
    assert not {"kms:ListKeys", "oam:ListSinks", "oam:CreateSink", "kms:CreateKey"} & set(unscoped["Action"])

    # Keys: the hub stage imports the keys operators deploy (omni-hub-keys), so
    # the document carries no AWS::KMS::Key or AWS::KMS::Alias handler action; it
    # may use a key that carries the ownership tag (the Space handler encrypts on
    # the caller's behalf) and read key metadata, nothing else.
    key_any = f"arn:aws:kms:{REGION}:{AUDIT}:key/*"
    owner = "agenticai:omni-hub"
    assert not {
        "kms:CreateKey",
        "kms:TagResource",
        "kms:UntagResource",
        "kms:PutKeyPolicy",
        "kms:ScheduleKeyDeletion",
        "kms:EnableKey",
        "kms:DisableKey",
        "kms:EnableKeyRotation",
        "kms:DisableKeyRotation",
        "kms:UpdateKeyDescription",
        "kms:CreateAlias",
        "kms:UpdateAlias",
        "kms:DeleteAlias",
        "kms:ListAliases",
        "kms:ListKeys",
        "kms:GetKeyPolicy",
        "kms:Encrypt",
        "kms:CreateGrant",
        "kms:ReEncryptFrom",
        "kms:Sign",
    } & granted
    use = by_sid(policy, "UseOmniHubKeys")
    assert set(use["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert use["Resource"] == key_any
    assert use["Condition"] == {"StringEquals": {f"aws:ResourceTag/{owner}": "true"}}
    metadata = by_sid(policy, "ReadKeyMetadata")
    assert metadata["Action"] == "kms:DescribeKey"
    assert metadata["Resource"] == key_any and "Condition" not in metadata
    for statement in policy["Statement"]:
        if any(action.startswith("kms:") for action in actions(statement)):
            assert statement["Sid"] in {"UseOmniHubKeys", "ReadKeyMetadata"}, statement["Sid"]
    # The tag the policy keys on is the one the keys stack and the hub stack put on what they create.
    assert f'"{owner}"' in (ROOT / "apps" / "platform-account" / "lib" / "omni-hub-stack.ts").read_text()
    assert f'"{owner}"' in (ROOT / "apps" / "platform-account" / "lib" / "omni-hub-keys-stack.ts").read_text()

    # The sink: created (and tagged on create) only in a CloudFormation request
    # for the two AuditStack deployments, everything on an existing sink pinned
    # to those deployments through CloudFormation's reserved tag.
    audit_stacks = {"AgenticAI-Platform-AuditStack", "Nonprod-Audit"}
    create_sink = by_sid(policy, "CreateAuditStackOamSink")
    assert set(create_sink["Action"]) == {"oam:CreateSink", "oam:TagResource"}
    assert create_sink["Resource"] == "*"
    assert set(create_sink["Condition"]["StringEquals"][f"aws:RequestTag/{STACK_TAG}"]) == audit_stacks
    sink = by_sid(policy, "ManageAuditStackOamSink")
    assert sink["Resource"] == f"arn:aws:oam:{REGION}:{AUDIT}:sink/*"
    assert set(sink["Action"]) == {
        "oam:DeleteSink",
        "oam:GetSink",
        "oam:GetSinkPolicy",
        "oam:ListTagsForResource",
        "oam:PutSinkPolicy",
        "oam:TagResource",
        "oam:UntagResource",
    }
    assert set(sink["Condition"]["StringEquals"][f"aws:ResourceTag/{STACK_TAG}"]) == audit_stacks
    for statement in policy["Statement"]:
        if {a for a in actions(statement) if a.startswith("oam:")} - {"oam:ListSinks"}:
            assert statement["Sid"] in {"CreateAuditStackOamSink", "ManageAuditStackOamSink"}, statement["Sid"]
    assert "oam:CreateLink" not in granted
    # Those pins must follow the names the code gives the AuditStack.
    assert '"AgenticAI-Platform-AuditStack"' in (ROOT / "bin" / "agentic-ai-platform.ts").read_text()
    pipeline = (ROOT / "pipelines" / "platform-pipeline-stack.ts").read_text()
    assert re.search(r"""["']Nonprod["']""", pipeline) and re.search(r"""["']Audit["']""", pipeline)

    log_groups = by_sid(policy, "CreateTransactionSearchLogGroups")
    assert set(log_groups["Action"]) == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy"}
    assert set(log_groups["Resource"]) == {
        f"arn:aws:logs:{REGION}:{AUDIT}:log-group:aws/spans:*",
        f"arn:aws:logs:{REGION}:{AUDIT}:log-group:/aws/application-signals/data:*",
    }
    # Never a data-plane log read or a log-group delete.
    assert not {
        "logs:GetLogEvents",
        "logs:FilterLogEvents",
        "logs:StartQuery",
        "logs:Unmask",
        "logs:DeleteLogGroup",
        "logs:DescribeLogGroups",
    } & granted
    prerequisites = by_sid(policy, "ApplicationSignalsPrerequisites")
    assert set(prerequisites["Action"]) == {
        "cloudtrail:CreateServiceLinkedChannel",
        "iam:CreateServiceLinkedRole",
        "iam:GetRole",
    }
    assert set(prerequisites["Resource"]) == {
        f"arn:aws:iam::{AUDIT}:role/aws-service-role/application-signals.cloudwatch.amazonaws.com/AWSServiceRoleForCloudWatchApplicationSignals",
        f"arn:aws:cloudtrail:{REGION}:{AUDIT}:channel/aws-service-channel/application-signals/*",
    }
    assert "ManageOmniGoldenGateRunnerRole" not in {s["Sid"] for s in policy["Statement"]}


def test_hub_role_boundary_caps_hub_roles_to_what_they_do_in_this_account() -> None:
    boundary = render("hub-role-boundary")
    sids = {statement["Sid"]: statement for statement in boundary["Statement"]}
    assert set(sids) == {
        "HubLogGroups",
        "HubResourcelessTelemetry",
        "HubEvaluations",
        "HubJudgeModels",
        "HubCodeEvaluators",
        "HubKeys",
    }
    granted = all_actions(boundary)
    # No service-wide action, nothing that administers the account; the only
    # action wildcard is the GenerateDataKey family the golden-gate runner uses.
    assert not any(action.endswith(":*") for action in granted)
    assert {action for action in granted if "*" in action} == {"kms:GenerateDataKey*"}
    assert not any(action.split(":")[0] in {"iam", "sts", "organizations", "ec2", "s3", "cloudformation"} for action in granted)
    assert not {"kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:CreateKey", "kms:TagResource", "logs:DeleteLogGroup", "logs:PutResourcePolicy"} & granted
    # Only actions without a resource type use "*"; everything else is pinned to
    # the Audit account (log groups in any Region for the backup-Region copy,
    # foundation models being global, the rest to the Region).
    resourceless = sids["HubResourcelessTelemetry"]
    assert resourceless["Resource"] == "*"
    # Service Authorization Reference: these have no resource type, so a grant
    # pinned to an ARN would silently deny them (StartBatchEvaluation and the
    # field-index policy calls included).
    assert set(resourceless["Action"]) == {
        "bedrock-agentcore:ListBatchEvaluations",
        "bedrock-agentcore:StartBatchEvaluation",
        "cloudwatch:PutMetricData",
        "cloudwatch:PutRecords",
        "logs:DescribeIndexPolicies",
        "logs:DescribeLogGroups",
        "logs:DescribeQueries",
        "logs:PutIndexPolicy",
        "logs:StopQuery",
    }
    assert not {"bedrock-agentcore:StartBatchEvaluation", "bedrock-agentcore:ListBatchEvaluations"} & set(sids["HubEvaluations"]["Action"])
    assert not {"logs:PutIndexPolicy", "logs:DescribeIndexPolicies"} & set(sids["HubLogGroups"]["Action"])
    # GetQueryResults and StartQuery take the log group, so they are account-scoped.
    assert {"logs:GetQueryResults", "logs:StartQuery"} <= set(sids["HubLogGroups"]["Action"])
    for sid, statement in sids.items():
        if sid == "HubResourcelessTelemetry":
            continue
        for resource in resources(statement):
            assert resource.startswith("arn:aws:"), (sid, resource)
            assert f":{AUDIT}:" in resource or resource == "arn:aws:bedrock:*::foundation-model/*", (sid, resource)
    assert sids["HubLogGroups"]["Resource"] == f"arn:aws:logs:*:{AUDIT}:log-group:*"
    assert set(resources(sids["HubEvaluations"])) == {
        f"arn:aws:bedrock-agentcore:{REGION}:{AUDIT}:dataset/*",
        f"arn:aws:bedrock-agentcore:{REGION}:{AUDIT}:batch-evaluate/*",
    }
    assert sids["HubCodeEvaluators"]["Resource"] == f"arn:aws:lambda:{REGION}:{AUDIT}:function:*"
    keys = sids["HubKeys"]
    assert keys["Resource"] == f"arn:aws:kms:{REGION}:{AUDIT}:key/*"
    assert keys["Condition"] == {"StringEquals": {"aws:ResourceTag/agenticai:omni-hub": "true"}}
    # The ceiling covers what the Dataset integration, evaluation, tag-propagation
    # and golden-gate roles' own policies use.
    assert {
        "logs:IntegrateWithDataset",
        "logs:PutIndexPolicy",
        "logs:StartQuery",
        "logs:TagResource",
        "cloudwatch:PutRecords",
        "bedrock-agentcore:StartBatchEvaluation",
        "bedrock:InvokeModel",
        "lambda:InvokeFunction",
        "kms:Decrypt",
    } <= granted


def test_hub_role_boundary_is_rendered_and_gated_per_hub_region() -> None:
    """A second hub Region gets its own boundary policy, matched by its own audit document.

    IAM policy names are account-wide and the boundary pins Regional resources,
    so one shared `AgenticAI-OmniHubRoleBoundary` would deny the second
    Region's evaluations, evaluator functions and keys. Each Region's name
    carries the Region, and the audit document for that Region gates on it.
    """
    regions = ("eu-west-1", "us-east-1")
    names = {}
    for region in regions:
        boundary = subprocess.run(
            [sys.executable, str(SCRIPT), "hub-role-boundary", "--account-id", AUDIT, "--region", region],
            cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout
        audit = json.loads(
            subprocess.run(
                [sys.executable, str(SCRIPT), "audit", "--account-id", AUDIT, "--region", region],
                cwd=ROOT, check=True, capture_output=True, text=True,
            ).stdout
        )
        gate = by_sid(audit, "ChangeBoundedHubRoles")
        names[region] = gate["Condition"]["StringEquals"]["iam:PermissionsBoundary"]
        assert names[region] == f"arn:aws:iam::{AUDIT}:policy/AgenticAI-OmniHubRoleBoundary-{region}"
        # The Regional statements of the boundary name only that Region.
        sids = {statement["Sid"]: statement for statement in json.loads(boundary)["Statement"]}
        for sid in ("HubEvaluations", "HubCodeEvaluators", "HubKeys"):
            for resource in resources(sids[sid]):
                assert f":{region}:" in resource, (region, sid, resource)
        assert f"arn:aws:bedrock:{region}:{AUDIT}:inference-profile/*" in resources(sids["HubJudgeModels"])
    assert names["eu-west-1"] != names["us-east-1"]
    readme = (ROOT / "README.md").read_text()
    assert "AgenticAI-OmniHubRoleBoundary-<REGION>" in readme and "once per hub Region" in readme


def test_simulator_helper_keeps_one_decision_per_action_and_resource() -> None:
    """IAM nests per-resource decisions under each action; the helper must not collapse them."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_render_cfn_execution_policy_simulation import flatten_decisions

    alias = "arn:aws:kms:eu-west-1:444444444444:alias/agenticai/omni-space-eu-west-1"
    key = "arn:aws:kms:eu-west-1:444444444444:key/11111111-2222-3333-4444-555555555555"
    response = [
        {
            # Several resources: the top level is an ARN template with the aggregate.
            "EvalActionName": "kms:CreateAlias",
            "EvalResourceName": "arn:aws:kms:${Region}:${Account}:${ResourceType}/${Id}",
            "EvalDecision": "implicitDeny",
            "ResourceSpecificResults": [
                {"EvalResourceName": alias, "EvalResourceDecision": "allowed"},
                {"EvalResourceName": key, "EvalResourceDecision": "implicitDeny"},
            ],
        },
        {
            # One resource: the top level is that resource's decision.
            "EvalActionName": "kms:Decrypt",
            "EvalResourceName": key,
            "EvalDecision": "allowed",
            "ResourceSpecificResults": [],
        },
    ]
    assert flatten_decisions(response) == {
        ("kms:CreateAlias", alias): "allowed",
        ("kms:CreateAlias", key): "implicitDeny",
        ("kms:Decrypt", key): "allowed",
    }


def test_audit_policy_grants_no_key_creation_or_change_so_operators_own_the_hub_keys() -> None:
    """The hub's keys come from the operator-deployed `omni-hub-keys` stage.

    Tagging a key at creation would need kms:TagResource on every key of the
    account and Region (KMS, "Controlling access to tags"), which would let the
    role bring any existing key under hub management; so the document carries
    no key creation or change at all, and the README routes operators through
    the keys stage and the one-time tagging of a hub deployed before the tag.
    """
    policy = render("audit")
    kms_actions = {action for action in all_actions(policy) if action.startswith("kms:")}
    assert kms_actions == {"kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"}
    for statement in policy["Statement"]:
        if {"kms:Decrypt", "kms:GenerateDataKey"} & actions(statement):
            assert statement["Condition"] == {"StringEquals": {"aws:ResourceTag/agenticai:omni-hub": "true"}}
    readme = (ROOT / "README.md").read_text()
    assert "stage=omni-hub-keys" in readme
    assert "agenticai/omniHubKeyArns" in readme
    assert "TagKey=agenticai:omni-hub,TagValue=true" in readme
    bin_source = (ROOT / "bin" / "agentic-ai-platform.ts").read_text()
    assert '"omni-hub-keys"' in bin_source and "agenticai/omniHubKeyArns" in bin_source


ORG_STACK = "AgenticAI-Management-OrgStack"
STACK_TAG = "aws:cloudformation:stack-name"
ORGANIZATIONS_MUTATIONS = {
    "organizations:UpdateOrganizationalUnit",
    "organizations:DeleteOrganizationalUnit",
    "organizations:UpdatePolicy",
    "organizations:DeletePolicy",
    "organizations:AttachPolicy",
    "organizations:DetachPolicy",
    "organizations:TagResource",
    "organizations:UntagResource",
}


def resources(statement: dict) -> list[str]:
    value = statement["Resource"]
    return value if isinstance(value, list) else [value]


def test_management_policy_pins_organizations_changes_to_the_org_stack() -> None:
    policy = render("management")
    root = f"arn:aws:organizations::{MANAGEMENT}:root/{ORGANIZATION}/*"
    ou = f"arn:aws:organizations::{MANAGEMENT}:ou/{ORGANIZATION}/*"
    scp = f"arn:aws:organizations::{MANAGEMENT}:policy/{ORGANIZATION}/service_control_policy/*"

    # Creating an OU (under the root) or an SCP is allowed only inside the
    # OrgStack: CloudFormation sends its reserved stack-name tag in every
    # Create request, and a tagged create also authorizes TagResource.
    create = by_sid(policy, "CreateOuAndScpForStack")
    assert set(create["Action"]) == {
        "organizations:CreateOrganizationalUnit",
        "organizations:CreatePolicy",
        "organizations:TagResource",
    }
    assert set(create["Resource"]) == {root, ou, scp}
    assert create["Condition"] == {"StringEquals": {f"aws:RequestTag/{STACK_TAG}": ORG_STACK}}

    # Every change to an existing OU or SCP requires the resource to carry the
    # same tag, which only CloudFormation can apply (aws: prefix).
    mutate = by_sid(policy, "ChangeStackOwnedOuAndScp")
    assert set(mutate["Action"]) == ORGANIZATIONS_MUTATIONS
    assert set(mutate["Resource"]) == {ou, scp}
    assert mutate["Condition"] == {"StringEquals": {f"aws:ResourceTag/{STACK_TAG}": ORG_STACK}}
    destructive = ORGANIZATIONS_MUTATIONS - {"organizations:TagResource"}
    stack_conditions = (
        {"StringEquals": {f"aws:RequestTag/{STACK_TAG}": ORG_STACK}},
        {"StringEquals": {f"aws:ResourceTag/{STACK_TAG}": ORG_STACK}},
    )
    for statement in policy["Statement"]:
        if destructive & actions(statement):
            assert statement["Sid"] == "ChangeStackOwnedOuAndScp"
        if "organizations:TagResource" in actions(statement):
            assert statement.get("Condition") in stack_conditions, statement["Sid"]

    read = by_sid(policy, "ReadOuAndScp")
    assert "Condition" not in read
    assert set(read["Resource"]) == {ou, scp}
    assert set(read["Action"]) == {
        "organizations:DescribeOrganizationalUnit",
        "organizations:DescribePolicy",
        "organizations:ListParents",
        "organizations:ListTagsForResource",
        "organizations:ListTargetsForPolicy",
    }
    unscoped = by_sid(policy, "ReadOrganization")
    assert unscoped["Resource"] == "*"
    assert set(unscoped["Action"]) == {
        "organizations:DescribeOrganization",
        "organizations:ListOrganizationalUnitsForParent",
        "organizations:ListPolicies",
        "organizations:ListRoots",
    }

    granted = all_actions(policy)
    assert "organizations:*" not in granted
    # Creating or deleting the Organization, account membership
    # (invite/move/remove/close) and account ARNs are never part of the stage.
    assert not {
        "organizations:CreateOrganization",
        "organizations:DeleteOrganization",
        "organizations:MoveAccount",
        "organizations:InviteAccountToOrganization",
        "organizations:RemoveAccountFromOrganization",
        "organizations:CloseAccount",
        "iam:CreateServiceLinkedRole",
    } & granted
    assert not any(":account/" in r for s in policy["Statement"] for r in resources(s))


def test_management_log_archive_roles_share_one_lifecycle_statement() -> None:
    policy = render("management")
    roles = by_sid(policy, "ManageLogArchiveRoles")
    assert set(roles["Resource"]) == {
        f"arn:aws:iam::{MANAGEMENT}:role/AgenticAI-LogArchive-CWLDestinationRole",
        f"arn:aws:iam::{MANAGEMENT}:role/Nonprod-LogArchive-CustomS3AutoDeleteObjects*",
    }
    assert "iam:PutRolePermissionsBoundary" not in roles["Action"]
    assert "iam:PassRole" not in roles["Action"]
    assert {"iam:CreateRole", "iam:DeleteRole", "iam:AttachRolePolicy", "iam:PutRolePolicy"} <= set(roles["Action"])
    # Each role is still passable to exactly one service.
    assert by_sid(policy, "PassLogArchiveDestinationRoleToCloudWatchLogs")["Condition"] == {
        "StringEquals": {"iam:PassedToService": "logs.amazonaws.com"}
    }
    assert by_sid(policy, "PassLogArchiveAutoDeleteProviderRoleToLambda")["Condition"] == {
        "StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}
    }


def test_management_stack_name_defaults_to_the_org_stack_and_is_validated() -> None:
    # The default must be the stack id bin/agentic-ai-platform.ts gives the stage.
    bin_source = (ROOT / "bin" / "agentic-ai-platform.ts").read_text()
    assert f'"{ORG_STACK}"' in bin_source
    custom = render("management", "--stack-name", "Custom-OrgStack")
    create = by_sid(custom, "CreateOuAndScpForStack")
    mutate = by_sid(custom, "ChangeStackOwnedOuAndScp")
    assert create["Condition"]["StringEquals"][f"aws:RequestTag/{STACK_TAG}"] == "Custom-OrgStack"
    assert mutate["Condition"]["StringEquals"][f"aws:ResourceTag/{STACK_TAG}"] == "Custom-OrgStack"
    assert ORG_STACK not in json.dumps(custom)
    malformed = subprocess.run(
        [
            sys.executable, str(SCRIPT), "management", "--account-id", MANAGEMENT, "--region", REGION,
            "--organization-id", ORGANIZATION, "--stack-name", "not a stack name",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert malformed.returncode != 0
    assert "--stack-name must be a CloudFormation stack name" in malformed.stderr
    without_org = subprocess.run(
        [sys.executable, str(SCRIPT), "management", "--account-id", MANAGEMENT, "--region", REGION, "--stack-name", ORG_STACK],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert without_org.returncode != 0
    assert "--stack-name needs --organization-id" in without_org.stderr
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "audit", "--account-id", AUDIT, "--region", REGION, "--stack-name", ORG_STACK],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "apply only to the management policy" in rejected.stderr


def test_management_policy_keeps_the_omni_domain_and_leaves_the_hub_to_audit() -> None:
    policy = render("management")
    domain = by_sid(policy, "ProvisionOmniOrganizationDomain")
    assert "cloudwatch:CreateDomainForOrganization" in domain["Action"]
    assert "cloudwatch:CreateDomainAccessGrantForOrganization" in domain["Action"]
    assert "cloudwatch:CreateSpace" not in domain["Action"]
    role = by_sid(policy, "ManageOmniDomainAccessRole")
    assert role["Resource"] == f"arn:aws:iam::{MANAGEMENT}:role/CloudWatchOmniDomainAccessRole"
    pass_role = by_sid(policy, "PassOmniDomainAccessRoleToCloudWatch")
    assert pass_role["Condition"]["StringEquals"]["iam:PassedToService"] == "cloudwatch.amazonaws.com"
    granted = all_actions(policy)
    assert "cloudwatch:CreateSpace" not in granted
    assert "observabilityadmin:CreateCentralizationRuleForOrganization" not in granted
    assert "xray:UpdateTraceSegmentDestination" not in granted


def test_management_without_organization_id_renders_domain_and_log_archive_only() -> None:
    # The pre-existing invocation still renders and says on stderr that it
    # carries no Organizations grant (stdout stays pure JSON).
    previous = subprocess.run(
        [sys.executable, str(SCRIPT), "management", "--account-id", MANAGEMENT, "--region", REGION],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert previous.returncode == 0
    policy = json.loads(previous.stdout)
    assert not any(action.startswith("organizations:") for action in all_actions(policy))
    assert {s["Sid"] for s in policy["Statement"]} >= {"ProvisionOmniOrganizationDomain", "ManageLogArchiveRoles"}
    assert "cannot deploy the OrgStack" in previous.stderr
    pinned = render("management")
    assert {s["Sid"] for s in pinned["Statement"]} - {s["Sid"] for s in policy["Statement"]} == {
        "CreateOuAndScpForStack",
        "ChangeStackOwnedOuAndScp",
        "ReadOuAndScp",
        "ReadOrganization",
    }
    # What the management document no longer carries, in either form: the Omni
    # hub (space, hub roles, centralization rule, Transaction Search) moved to
    # the audit document, so the Audit account bootstraps from `audit`.
    hub_actions = {
        "cloudwatch:CreateSpace",
        "observabilityadmin:CreateCentralizationRuleForOrganization",
        "xray:UpdateTraceSegmentDestination",
    }
    for document in (policy, pinned):
        assert not hub_actions & all_actions(document)
        assert "ManageOmniHubRoles" not in {s["Sid"] for s in document["Statement"]}
    assert hub_actions <= all_actions(render("audit"))
    malformed = subprocess.run(
        [sys.executable, str(SCRIPT), "management", "--account-id", MANAGEMENT, "--region", REGION, "--organization-id", "org-123"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert malformed.returncode != 0
    assert "must look like o-" in malformed.stderr
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "workstream", "--account-id", WORKSTREAM, "--region", REGION, "--organization-id", ORGANIZATION],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "apply only to the management policy" in rejected.stderr


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_cfn_execution_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oversized_policy_is_refused_before_it_reaches_iam(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    renderer = load_renderer()
    argv = [
        "render-cfn-execution-policy.py", "platform", "--account-id", PLATFORM, "--region", REGION,
        "--target-account-id", WORKSTREAM, "--connection-arn", CONNECTION,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    # Shrink the limit below the platform document: the renderer must exit
    # non-zero, name the measured size and the limit, and print no document.
    monkeypatch.setattr(renderer, "IAM_MANAGED_POLICY_MAX_CHARS", 1024)
    with pytest.raises(SystemExit) as refused:
        renderer.main()
    assert refused.value.code not in (0, None)
    message = str(refused.value.code)
    assert re.search(r"rendered policy is [0-9]{4,} characters", message)
    assert "allow 1024" in message
    assert capsys.readouterr().out == ""
    # The real limit lets the same invocation through.
    monkeypatch.setattr(renderer, "IAM_MANAGED_POLICY_MAX_CHARS", IAM_MANAGED_POLICY_MAX_CHARS)
    renderer.main()
    assert json.loads(capsys.readouterr().out)["Version"] == "2012-10-17"


def test_every_rendered_policy_fits_one_iam_managed_policy() -> None:
    for role, policy in (
        ("workstream", render("workstream")),
        ("management", render("management")),
        ("management --stack-name", render("management", "--stack-name", "A" * 128)),
        ("audit", render("audit")),
        ("hub-role-boundary", render("hub-role-boundary")),
        ("platform", render("platform", "--target-account-id", WORKSTREAM, "--connection-arn", CONNECTION)),
    ):
        # IAM counts the document without whitespace; the console-created CDK
        # execution policy is a single managed policy.
        size = len(json.dumps(policy, separators=(",", ":"), sort_keys=True))
        assert size <= IAM_MANAGED_POLICY_MAX_CHARS, f"{role} policy is {size} characters"


def test_workstream_and_platform_policies_grant_transaction_search_opt_in() -> None:
    for role, policy in (
        ("workstream", render("workstream")),
        ("platform", render("platform", "--target-account-id", WORKSTREAM, "--connection-arn", CONNECTION)),
    ):
        actions = {
            action
            for statement in policy["Statement"]
            for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
        }
        assert "xray:UpdateTraceSegmentDestination" in actions, role
        assert "xray:GetTraceSegmentDestination" in actions, role
