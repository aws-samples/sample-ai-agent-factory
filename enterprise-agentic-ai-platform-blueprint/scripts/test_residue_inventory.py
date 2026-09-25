"""Offline tests for scripts/residue_inventory.py name matchers.

The inventory is only as good as its matchers: a matcher that is too narrow
reports a dirty account as clean, one that is too broad reports another
team's resources as this project's residue. These tests pin both directions,
and keep the KMS description list in sync with the constructs' source.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("residue_inventory", Path(__file__).with_name("residue_inventory.py"))
inv = importlib.util.module_from_spec(SPEC)
sys.modules["residue_inventory"] = inv
SPEC.loader.exec_module(inv)

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name",
    [
        "AgenticAI-demo-primary-prod-RuntimeMemory",
        "Nonprod-InferenceGateway",
        "Prod-Registry",
        "agenticai-d03-prod-demo-primary-reg-validator",
        "WorkloadPipelineAssetsDocke-QcOLryFpcJIg",
    ],
)
def test_project_names_match(name):
    assert inv.is_project_name(name)


@pytest.mark.parametrize("name", ["IagRegistryStack", "BedrockRouterStack", "FAST-stack", "ukiaipl-core", "cdk-hnb659fds-assets", ""])
def test_unrelated_names_do_not_match(name):
    assert not inv.is_project_name(name)


@pytest.mark.parametrize(
    "group,expected",
    [
        ("/aws/lambda/AgenticAI-D03-WorkstreamG-LogRetentionaae0aa3c5b4d-3YsT17wa2qYO", True),
        ("/aws/codebuild/WorkloadPipelineAssetsDocke-QcOLryFpcJIg", True),
        ("/aws/bedrock-agentcore/runtimes/AgenticAI_D03_prod_demo_primary_runtime-6H9L6D4QXc-DEFAULT", True),
        ("/aws/lambda/Nonprod-LogArchive-CustomS3AutoDeleteObjectsCustom-G6wbBAIOySHl", True),
        ("/agenticai/gateway/nonprod/access", True),
        ("/aws/lambda/IagRegistryStack-Handler", False),
        ("/aws/lambda/some-other-function", False),
    ],
)
def test_log_group_matcher(group, expected):
    assert inv.is_project_log_group(group) is expected


@pytest.mark.parametrize(
    "bucket,expected",
    [
        ("agenticai-aiact-nonprod-123456789012-us-east-1", True),
        ("agenticai-platformpipelin-platformpipelineartifact-nkfxv8rxwqxq", True),
        ("aws-cloudtrail-logs-123456789012-3ec67777", False),
        ("cdk-hnb659fds-assets-123456789012-us-west-2", False),
    ],
)
def test_bucket_matcher(bucket, expected):
    assert inv.is_project_bucket(bucket) is expected


@pytest.mark.parametrize(
    "secret,expected",
    [
        ("agenticai/inference-m2m/agenticai-inference-prod", True),
        ("bedrock-agentcore-identity!default/oauth2/AgenticAI_D03_prod_demo_primary_inference-3eeb676d", True),
        ("bedrock-agentcore-identity!default/oauth2/iag-provider-1234", False),
        ("prod/database/password", False),
    ],
)
def test_secret_matcher(secret, expected):
    assert inv.is_project_secret(secret) is expected


@pytest.mark.parametrize(
    "description,expected",
    [
        ("EU AI Act record-keeping CMK (nonprod/demo/primary).", True),
        ("CMK for AgentCore evaluation-gates corpus + run history (nonprod).", True),
        ("CMK for the cross-account inference M2M secret (agenticai-inference-prod).", True),
        ("Workstream-local AgentCore Memory CMK for prod-demo-primary.", True),
        ("CMK for short-lived AgenticAI pipeline artifacts", True),
        # Another team's AgentCore key must not be counted as this project's.
        ("IAG AgentCore registry key", False),
        ("CMK for AgentCore things another team built", False),
        ("", False),
    ],
)
def test_key_description_matcher(description, expected):
    assert inv.is_project_key_description(description) is expected


def _emitted_key_descriptions() -> list[str]:
    """Every `description:` of a `new Key(...)` in the constructs, with each
    template expression replaced by a representative value."""
    out = []
    for root in ("packages", "apps", "pipelines"):
        for path in (REPO / root).rglob("*.ts"):
            if "node_modules" in path.parts or path.name.endswith(".test.ts"):
                continue
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"new (?:kms\.)?Key\([^;]*?description:\s*([`'\"])(.*?)\1", text, re.S):
                description = re.sub(r"\$\{[^}]+\}", "x-value", match.group(2))
                out.append(description)
    return sorted(set(out))


def test_every_key_description_the_code_emits_is_recognised():
    emitted = _emitted_key_descriptions()
    assert len(emitted) >= 25, "the source scan found too few keys; the scan itself is broken"
    unrecognised = [d for d in emitted if not inv.is_project_key_description(d)]
    assert unrecognised == [], (
        "a construct emits a KMS key description the residue inventory does not recognise; "
        f"add it to PROJECT_KEY_DESCRIPTION: {unrecognised}"
    )
