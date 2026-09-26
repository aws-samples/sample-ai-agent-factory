"""Contract tests for render-cfn-execution-policy.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pipelines" / "bootstrap" / "render-cfn-execution-policy.py"
REGION = "eu-west-1"
PLATFORM = "111111111111"
WORKSTREAM = "222222222222"
MANAGEMENT = "333333333333"
CONNECTION = "arn:aws:codeconnections:us-west-2:111111111111:connection/example"


def render(role: str, *args: str) -> dict:
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
            }[role],
            "--region",
            REGION,
            *args,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


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


def test_workstream_policy_retains_bounded_pass_role_and_delete_read() -> None:
    policy = render("workstream")
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
