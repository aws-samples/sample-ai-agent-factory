"""Opt-in IAM policy simulator checks for the rendered execution policies.

The contract tests next door assert the shape of the documents. This module
asks IAM itself (``SimulateCustomPolicy``, a read-only call that needs no
resources to exist) whether the ownership boundaries behave as documented:

* the audit role creates (and tags on create) a KMS key only when the request
  carries ``agenticai:omni-hub=true``, and can never add that tag to an
  existing key, change or use a key without it, or point a hub alias at one;
* the management role changes an OU or SCP only when it carries the OrgStack's
  CloudFormation stack-name tag.

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
SCP = f"arn:aws:organizations::{MANAGEMENT}:policy/{ORGANIZATION}/service_control_policy/p-examplepol"


def render(*args: str) -> str:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def context(key: str, value: str) -> list[dict]:
    return [{"ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": "string"}]


def decisions(document: str, actions: list[str], resources: list[str], ctx: list[dict] | None = None) -> dict[str, str]:
    iam = boto3.client("iam", region_name="us-east-1")
    params = {"PolicyInputList": [document], "ActionNames": actions, "ResourceArns": resources}
    if ctx:
        params["ContextEntries"] = ctx
    return {r["EvalActionName"]: r["EvalDecision"] for r in iam.simulate_custom_policy(**params)["EvaluationResults"]}


@pytest.fixture(scope="module")
def audit_document() -> str:
    return render("audit", "--account-id", AUDIT, "--region", REGION)


@pytest.fixture(scope="module")
def management_document() -> str:
    return render("management", "--account-id", MANAGEMENT, "--region", REGION, "--organization-id", ORGANIZATION)


def test_audit_role_creates_tagged_keys_only_in_a_hub_request(audit_document: str) -> None:
    # KMS authorizes CreateKey and its dependent TagResource without a key ARN.
    tagged = decisions(audit_document, ["kms:CreateKey", "kms:TagResource"], ["*"], context(f"aws:RequestTag/{OWNER_TAG}", "true"))
    assert tagged == {"kms:CreateKey": "allowed", "kms:TagResource": "allowed"}
    untagged = decisions(audit_document, ["kms:CreateKey", "kms:TagResource"], ["*"])
    assert set(untagged.values()) == {"implicitDeny"}


def test_audit_role_cannot_claim_an_existing_key(audit_document: str) -> None:
    # The ownership request tag does not help on an existing key: the create
    # grant excludes key ARNs and the change grant needs the tag already there.
    claim = decisions(audit_document, ["kms:TagResource"], [KEY], context(f"aws:RequestTag/{OWNER_TAG}", "true"))
    assert claim == {"kms:TagResource": "implicitDeny"}
    unowned = decisions(
        audit_document,
        ["kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:Decrypt", "kms:GenerateDataKey", "kms:UntagResource"],
        [KEY],
    )
    assert set(unowned.values()) == {"implicitDeny"}
    owned = decisions(
        audit_document,
        ["kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:Decrypt", "kms:GenerateDataKey", "kms:TagResource"],
        [KEY],
        context(f"aws:ResourceTag/{OWNER_TAG}", "true"),
    )
    assert set(owned.values()) == {"allowed"}
    # Reads and EnableKeyRotation are the documented exception on any key.
    assert set(decisions(audit_document, ["kms:DescribeKey", "kms:EnableKeyRotation"], [KEY]).values()) == {"allowed"}


def test_audit_role_points_hub_aliases_only_at_owned_keys(audit_document: str) -> None:
    owned = decisions(audit_document, ["kms:CreateAlias", "kms:UpdateAlias"], [SPACE_ALIAS, KEY], context(f"aws:ResourceTag/{OWNER_TAG}", "true"))
    assert set(owned.values()) == {"allowed"}
    unowned = decisions(audit_document, ["kms:CreateAlias", "kms:UpdateAlias"], [SPACE_ALIAS, KEY])
    assert set(unowned.values()) == {"implicitDeny"}
    other_alias = decisions(audit_document, ["kms:CreateAlias"], [f"arn:aws:kms:{REGION}:{AUDIT}:alias/not-ours"])
    assert other_alias == {"kms:CreateAlias": "implicitDeny"}


def test_management_role_changes_only_the_org_stacks_scps(management_document: str) -> None:
    mutations = ["organizations:UpdatePolicy", "organizations:DeletePolicy", "organizations:DetachPolicy"]
    owned = decisions(management_document, mutations, [SCP], context(f"aws:ResourceTag/{STACK_TAG}", "AgenticAI-Management-OrgStack"))
    assert set(owned.values()) == {"allowed"}
    foreign = decisions(management_document, mutations, [SCP], context(f"aws:ResourceTag/{STACK_TAG}", "Some-Other-Stack"))
    assert set(foreign.values()) == {"implicitDeny"}
    assert set(decisions(management_document, mutations, [SCP]).values()) == {"implicitDeny"}
    assert json.loads(management_document)["Version"] == "2012-10-17"
