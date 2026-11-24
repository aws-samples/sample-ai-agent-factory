"""Opt-in IAM policy simulator checks for the rendered execution policies.

The contract tests next door assert the shape of the documents. This module
asks IAM itself (``SimulateCustomPolicy``, a read-only call that needs no
resources to exist) whether the boundaries behave as documented:

* the audit role never creates, tags or changes a KMS key, uses only keys that
  carry ``agenticai:omni-hub=true`` and reads key metadata;
* the audit role creates the operator role only with the AWS-managed Omni
  boundary and only the AWS-managed Omni policies, and every other hub role
  only with the blueprint boundary, which it can never remove;
* the management role changes an OU or SCP only when it carries the OrgStack's
  CloudFormation stack-name tag.

Every context key a statement conditions on is supplied explicitly, so a result
never depends on the simulator's treatment of a missing key.

Set ``AGENTICAI_SIMULATE_IAM=1`` with credentials that allow
``iam:SimulateCustomPolicy`` (any account; nothing is created) to run it:

    AGENTICAI_SIMULATE_IAM=1 python -m pytest tests/bootstrap -q

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3") if os.environ.get("AGENTICAI_SIMULATE_IAM") == "1" else None

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTICAI_SIMULATE_IAM") != "1",
    reason="set AGENTICAI_SIMULATE_IAM=1 (needs iam:SimulateCustomPolicy) to run the simulator checks",
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pipelines" / "bootstrap" / "render-cfn-execution-policy.py"
REGION = "eu-west-1"
AUDIT = "444444444444"
MANAGEMENT = "333333333333"
ORGANIZATION = "o-exampleorg0a"
OWNER_TAG = "agenticai:omni-hub"
STACK_TAG = "aws:cloudformation:stack-name"
KEY = f"arn:aws:kms:{REGION}:{AUDIT}:key/11111111-2222-3333-4444-555555555555"
SPACE_ALIAS = f"arn:aws:kms:{REGION}:{AUDIT}:alias/agenticai/omni-space-{REGION}"
OPERATOR_ROLE = f"arn:aws:iam::{AUDIT}:role/CloudWatchOmniOperatorRole"
EVALUATION_ROLE = f"arn:aws:iam::{AUDIT}:role/service-role/AgentCoreEvaluationRole-omni-{REGION}"
OMNI_BOUNDARY = "arn:aws:iam::aws:policy/CloudWatchOmniSpaceAccessPolicy"
HUB_BOUNDARY = f"arn:aws:iam::{AUDIT}:policy/AgenticAI-OmniHubRoleBoundary"
ADMINISTRATOR = "arn:aws:iam::aws:policy/AdministratorAccess"
SCP = f"arn:aws:organizations::{MANAGEMENT}:policy/{ORGANIZATION}/service_control_policy/p-examplepol"


def render(*args: str) -> str:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def context(*pairs: tuple[str, str]) -> list[dict]:
    return [{"ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": "string"} for key, value in pairs]


def decisions(
    document: str,
    actions: list[str],
    resources: list[str],
    ctx: list[dict] | None = None,
    boundary: str | None = None,
) -> dict[str, str]:
    iam = boto3.client("iam", region_name="us-east-1")
    params = {"PolicyInputList": [document], "ActionNames": actions, "ResourceArns": resources}
    if ctx:
        params["ContextEntries"] = ctx
    if boundary:
        params["PermissionsBoundaryPolicyInputList"] = [boundary]
    return {r["EvalActionName"]: r["EvalDecision"] for r in iam.simulate_custom_policy(**params)["EvaluationResults"]}


ADMINISTRATOR_DOCUMENT = json.dumps(
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
)


@pytest.fixture(scope="module")
def audit_document() -> str:
    return render("audit", "--account-id", AUDIT, "--region", REGION)


@pytest.fixture(scope="module")
def boundary_document() -> str:
    return render("hub-role-boundary", "--account-id", AUDIT, "--region", REGION)


@pytest.fixture(scope="module")
def management_document() -> str:
    return render("management", "--account-id", MANAGEMENT, "--region", REGION, "--organization-id", ORGANIZATION)


def test_audit_role_never_creates_tags_or_changes_a_key(audit_document: str) -> None:
    # Not even a request that carries the ownership tag creates or tags a key.
    create = decisions(
        audit_document,
        ["kms:CreateKey", "kms:TagResource"],
        ["*"],
        context((f"aws:RequestTag/{OWNER_TAG}", "true"), ("aws:RequestedRegion", REGION)),
    )
    assert set(create.values()) == {"implicitDeny"}
    # Nor does a key that already carries the tag become changeable.
    owned = decisions(
        audit_document,
        ["kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:TagResource", "kms:UntagResource", "kms:EnableKeyRotation"],
        [KEY],
        context((f"aws:ResourceTag/{OWNER_TAG}", "true")),
    )
    assert set(owned.values()) == {"implicitDeny"}
    aliases = decisions(
        audit_document,
        ["kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias"],
        [SPACE_ALIAS, KEY],
        context((f"aws:ResourceTag/{OWNER_TAG}", "true")),
    )
    assert set(aliases.values()) == {"implicitDeny"}


def test_audit_role_uses_only_keys_that_carry_the_hub_tag(audit_document: str) -> None:
    owned = decisions(audit_document, ["kms:Decrypt", "kms:GenerateDataKey"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "true")))
    assert set(owned.values()) == {"allowed"}
    unowned = decisions(audit_document, ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "false")))
    assert set(unowned.values()) == {"implicitDeny"}
    assert decisions(audit_document, ["kms:Encrypt"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "true"))) == {"kms:Encrypt": "implicitDeny"}
    # Key metadata is readable for any key in the account and Region, as documented.
    assert decisions(audit_document, ["kms:DescribeKey"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "false"))) == {"kms:DescribeKey": "allowed"}


def test_audit_role_creates_the_operator_role_only_within_the_omni_boundary(audit_document: str) -> None:
    omni = context(("iam:PermissionsBoundary", OMNI_BOUNDARY))
    assert decisions(audit_document, ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], [OPERATOR_ROLE], omni) == {
        "iam:CreateRole": "allowed",
        "iam:PutRolePermissionsBoundary": "allowed",
    }
    # The blueprint boundary is not the operator role's; neither is an inline policy.
    assert decisions(audit_document, ["iam:CreateRole"], [OPERATOR_ROLE], context(("iam:PermissionsBoundary", HUB_BOUNDARY))) == {"iam:CreateRole": "implicitDeny"}
    assert decisions(audit_document, ["iam:PutRolePolicy"], [OPERATOR_ROLE], omni) == {"iam:PutRolePolicy": "implicitDeny"}
    # Only the AWS-managed Omni policies can be attached to it.
    attach_omni = context(("iam:PermissionsBoundary", OMNI_BOUNDARY), ("iam:PolicyARN", OMNI_BOUNDARY))
    assert decisions(audit_document, ["iam:AttachRolePolicy"], [OPERATOR_ROLE], attach_omni) == {"iam:AttachRolePolicy": "allowed"}
    attach_admin = context(("iam:PermissionsBoundary", OMNI_BOUNDARY), ("iam:PolicyARN", ADMINISTRATOR))
    assert decisions(audit_document, ["iam:AttachRolePolicy"], [OPERATOR_ROLE], attach_admin) == {"iam:AttachRolePolicy": "implicitDeny"}


def test_audit_role_creates_hub_roles_only_within_the_blueprint_boundary(audit_document: str) -> None:
    bounded = context(("iam:PermissionsBoundary", HUB_BOUNDARY))
    gated = ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:PutRolePermissionsBoundary"]
    assert set(decisions(audit_document, gated, [EVALUATION_ROLE], bounded).values()) == {"allowed"}
    # Without a boundary, with the operator's AWS-managed one, or with an administrator boundary: no.
    assert set(decisions(audit_document, gated, [EVALUATION_ROLE]).values()) == {"implicitDeny"}
    assert set(decisions(audit_document, gated, [EVALUATION_ROLE], context(("iam:PermissionsBoundary", OMNI_BOUNDARY))).values()) == {"implicitDeny"}
    assert set(decisions(audit_document, gated, [EVALUATION_ROLE], context(("iam:PermissionsBoundary", ADMINISTRATOR))).values()) == {"implicitDeny"}
    # The boundary can never be removed, and no other role name is in reach.
    assert decisions(audit_document, ["iam:DeleteRolePermissionsBoundary"], [EVALUATION_ROLE]) == {"iam:DeleteRolePermissionsBoundary": "implicitDeny"}
    assert decisions(audit_document, ["iam:CreateRole"], [f"arn:aws:iam::{AUDIT}:role/SomethingElse"], bounded) == {"iam:CreateRole": "implicitDeny"}


def test_hub_role_boundary_is_a_ceiling_that_still_lets_the_hub_roles_work(boundary_document: str) -> None:
    """An administrator identity policy under the boundary keeps only what the hub roles do."""
    log_group = f"arn:aws:logs:{REGION}:{AUDIT}:log-group:/centralized/555555555555/{REGION}/aws/spans"
    allowed = decisions(
        ADMINISTRATOR_DOCUMENT,
        ["logs:GetLogEvents", "logs:StartQuery", "logs:PutLogEvents", "logs:IntegrateWithDataset"],
        [log_group],
        boundary=boundary_document,
    )
    assert set(allowed.values()) == {"allowed"}
    # Actions without a resource type are authorized against "*"; the Service
    # Authorization Reference lists these four that way, so the ceiling must too.
    resourceless = decisions(
        ADMINISTRATOR_DOCUMENT,
        ["bedrock-agentcore:StartBatchEvaluation", "bedrock-agentcore:ListBatchEvaluations", "logs:PutIndexPolicy", "logs:DescribeIndexPolicies"],
        ["*"],
        boundary=boundary_document,
    )
    assert set(resourceless.values()) == {"allowed"}
    denied = decisions(
        ADMINISTRATOR_DOCUMENT,
        ["logs:DeleteLogGroup", "logs:PutResourcePolicy", "iam:CreateRole", "sts:AssumeRole", "kms:PutKeyPolicy"],
        ["*"],
        boundary=boundary_document,
    )
    assert set(denied.values()) == {"implicitDeny"}
    assert decisions(ADMINISTRATOR_DOCUMENT, ["logs:GetLogEvents"], [f"arn:aws:logs:{REGION}:999999999999:log-group:other"], boundary=boundary_document) == {"logs:GetLogEvents": "implicitDeny"}
    assert decisions(ADMINISTRATOR_DOCUMENT, ["kms:Decrypt"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "true")), boundary=boundary_document) == {"kms:Decrypt": "allowed"}
    assert decisions(ADMINISTRATOR_DOCUMENT, ["kms:Decrypt"], [KEY], context((f"aws:ResourceTag/{OWNER_TAG}", "false")), boundary=boundary_document) == {"kms:Decrypt": "implicitDeny"}


def test_management_role_changes_only_the_org_stacks_scps(management_document: str) -> None:
    mutations = ["organizations:UpdatePolicy", "organizations:DeletePolicy", "organizations:DetachPolicy"]
    owned = decisions(management_document, mutations, [SCP], context((f"aws:ResourceTag/{STACK_TAG}", "AgenticAI-Management-OrgStack")))
    assert set(owned.values()) == {"allowed"}
    foreign = decisions(management_document, mutations, [SCP], context((f"aws:ResourceTag/{STACK_TAG}", "Some-Other-Stack")))
    assert set(foreign.values()) == {"implicitDeny"}
    assert set(decisions(management_document, mutations, [SCP]).values()) == {"implicitDeny"}
    assert json.loads(management_document)["Version"] == "2012-10-17"
