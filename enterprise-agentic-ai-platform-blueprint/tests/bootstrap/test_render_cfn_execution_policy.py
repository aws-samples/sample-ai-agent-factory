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
    manage = by_sid["ManageOmniHubRoles"]
    assert all(":role/" in resource for resource in manage["Resource"])
    assert all(f"::{AUDIT}:" in resource for resource in manage["Resource"])
    assert any(resource.endswith("role/CloudWatchOmniOperatorRole") for resource in manage["Resource"])
    assert any("AgentCoreEvaluationRole-omni-" in resource for resource in manage["Resource"])
    # The organization domain and its access role are the management account's.
    assert not any("CloudWatchOmniDomainAccessRole" in resource for resource in manage["Resource"])
    pass_role = by_sid["PassOmniHubRolesToServices"]
    assert pass_role["Action"] == "iam:PassRole"
    services = set(pass_role["Condition"]["StringEquals"]["iam:PassedToService"])
    assert {"cloudwatch.amazonaws.com", "logs.amazonaws.com", "bedrock-agentcore.amazonaws.com"} <= services
    provision = by_sid["ProvisionOmniHubSpaceAndCentralization"]
    assert "cloudwatch:CreateSpace" in provision["Action"]
    assert "cloudwatch:GetDomainForOrganization" in provision["Action"]
    assert "cloudwatch:CreateDomainForOrganization" not in provision["Action"]
    assert "observabilityadmin:CreateCentralizationRuleForOrganization" in provision["Action"]
    assert "xray:UpdateTraceSegmentDestination" in provision["Action"]
    # Never a bare cloudwatch:* or observabilityadmin:* in the hub statement.
    assert "cloudwatch:*" not in provision["Action"]
    assert "observabilityadmin:*" not in provision["Action"]
    # The sink and the hub keys/log groups are the only service-wide grants.
    assert set(by_sid["ProvisionGovernanceTelemetryStacks"]["Action"]) == {"kms:*", "logs:*", "oam:*"}
    assert "organizations:CreatePolicy" not in all_actions(policy)


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
    # Create request.
    create = by_sid(policy, "CreateOuAndScpForStack")
    assert set(create["Action"]) == {
        "organizations:CreateOrganizationalUnit",
        "organizations:CreatePolicy",
    }
    assert set(create["Resource"]) == {root, scp}
    assert create["Condition"] == {"StringEquals": {f"aws:RequestTag/{STACK_TAG}": ORG_STACK}}

    # Every change to an existing OU or SCP requires the resource to carry the
    # same tag, which only CloudFormation can apply (aws: prefix).
    mutate = by_sid(policy, "ChangeStackOwnedOuAndScp")
    assert set(mutate["Action"]) == ORGANIZATIONS_MUTATIONS
    assert set(mutate["Resource"]) == {ou, scp}
    assert mutate["Condition"] == {"StringEquals": {f"aws:ResourceTag/{STACK_TAG}": ORG_STACK}}
    for statement in policy["Statement"]:
        if ORGANIZATIONS_MUTATIONS & actions(statement):
            assert statement["Sid"] == "ChangeStackOwnedOuAndScp"

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
    unscoped = by_sid(policy, "ManageOrganization")
    assert unscoped["Resource"] == "*"
    assert set(unscoped["Action"]) == {
        "organizations:CreateOrganization",
        "organizations:DescribeOrganization",
        "organizations:ListOrganizationalUnitsForParent",
        "organizations:ListPolicies",
        "organizations:ListRoots",
    }
    slr = by_sid(policy, "CreateOrganizationsSlr")
    assert slr["Action"] == "iam:CreateServiceLinkedRole"
    assert slr["Condition"] == {"StringEquals": {"iam:AWSServiceName": "organizations.amazonaws.com"}}

    granted = all_actions(policy)
    assert "organizations:*" not in granted
    # Deleting the Organization, account membership (invite/move/remove/close)
    # and account ARNs are never part of the stage.
    assert not {
        "organizations:DeleteOrganization",
        "organizations:MoveAccount",
        "organizations:InviteAccountToOrganization",
        "organizations:RemoveAccountFromOrganization",
        "organizations:CloseAccount",
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


def test_management_requires_organization_id_and_other_roles_reject_it() -> None:
    missing = subprocess.run(
        [sys.executable, str(SCRIPT), "management", "--account-id", MANAGEMENT, "--region", REGION],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "requires --organization-id" in missing.stderr
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
        ("audit", render("audit")),
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
