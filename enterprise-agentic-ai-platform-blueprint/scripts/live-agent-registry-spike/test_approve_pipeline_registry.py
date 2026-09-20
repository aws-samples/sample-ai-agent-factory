"""Offline guards for the manual GA Registry approval utility.

Every test uses in-process fakes. Nothing here imports boto3, reads
credentials, or reaches AWS. These guards supplement — never replace — the live
approval evidence.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

import approve_pipeline_registry as apr
from approve_pipeline_registry import (
    APPROVED_STATUS,
    ApprovalConfig,
    ApprovalError,
    Evidence,
    approve,
    parse_args,
    registry_name_for,
    resource_identifier,
    run,
    stack_name_for,
    verify_deployment,
)

ACCOUNT_ID = "111111111111"
REGION = "eu-west-1"
GIT_HEAD = "0123456789abcdef0123456789abcdef01234567"
TOOL_IDS = ("tool-ping", "tool-echo")  # deliberately unsorted input order
REGISTRY_ID = "reg0123456789abcd"
RECORD_IDS = {"tool-echo": "rec00000000000echo", "tool-ping": "rec00000000000ping"}
REGISTRY_ARN = f"arn:aws:agent-registry:{REGION}:{ACCOUNT_ID}:registry/{REGISTRY_ID}"


def record_arn(record_id: str) -> str:
    return f"{REGISTRY_ARN}/record/{record_id}"


def config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: Any,
) -> ApprovalConfig:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    kwargs: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "environment": "nonprod",
        "application_id": "agenticai-platform",
        "agent_id": "registry-producer",
        "tenant_id": "platform",
        "cost_centre": "engineering",
        "expected_tool_ids": TOOL_IDS,
        "git_head": GIT_HEAD,
        "evidence_file": tmp_path / "evidence.json",
    }
    kwargs.update(overrides)
    return ApprovalConfig(**kwargs)


def governance_document(tool_id: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schemaVersion": "agenticai.tool-governance/1.0",
        "catalogueVersion": "1",
        "toolId": tool_id,
        "description": f"{tool_id} description",
        "desiredApprovalStatus": "approved",
        "target": {
            "type": "lambda",
            "arn": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{tool_id}:PROD",
        },
        "mcp": {
            "toolName": tool_id,
            "description": f"{tool_id} description",
            "inputSchema": {"type": "object"},
        },
        "authorization": {
            "defaultDecision": "DENY",
            "cedarPolicy": "permit(principal, action, resource);",
            "allowedSubjects": [],
            "allowedGroups": [],
            "combination": "AUTHENTICATED",
        },
        "ownership": {"ownerTeam": "platform-ai", "costCentre": "engineering"},
    }
    document.update(overrides)
    return document


def processed_stack_template(cfg: ApprovalConfig) -> dict[str, Any]:
    tags = [
        {"Key": key, "Value": value}
        for key, value in sorted(cfg.tags.items())
    ]
    resources: dict[str, Any] = {
        "GaRegistryRegistry": {
            "Type": apr.REGISTRY_TYPE,
            "Properties": {
                "Name": cfg.registry_name,
                "Description": f"Platform-owned GA Agent Registry ({cfg.environment}).",
                "AuthorizerType": "AWS_IAM",
                "ApprovalConfiguration": {"AutoApprovalRules": ["APPROVE_ALL"]},
                "Tags": tags,
            },
        }
    }
    for tool_id in cfg.expected_tool_ids:
        document = governance_document(tool_id)
        resources[f"GaRegistryRecord{tool_id.replace('-', '')}"] = {
            "Type": apr.RECORD_TYPE,
            "Properties": {
                "RegistryId": {"Fn::GetAtt": ["GaRegistryRegistry", "RegistryId"]},
                "Name": tool_id,
                "DisplayName": tool_id,
                "Description": document["description"],
                "RecordType": "CUSTOM",
                "RecordVersion": "1.0.0",
                "Descriptors": {"Custom": {"Data": json.dumps(document)}},
                "Tags": tags,
            },
        }
    return {"Resources": resources}


def make_record(
    tool_id: str,
    *,
    status: str = "DRAFT",
    name: str | None = None,
    document: Mapping[str, Any] | None = None,
    descriptor_data: str | None = None,
    record_type: str = "CUSTOM",
) -> dict[str, Any]:
    record_id = RECORD_IDS[tool_id]
    data = (
        descriptor_data
        if descriptor_data is not None
        else json.dumps(document if document is not None else governance_document(tool_id))
    )
    return {
        "recordId": record_id,
        "recordArn": record_arn(record_id),
        "name": name if name is not None else tool_id,
        "status": status,
        "recordType": record_type,
        "descriptors": {"custom": {"data": data}},
    }


class FakeAws:
    """In-process stand-in for ApprovalAws. Records every call it receives."""

    def __init__(
        self,
        cfg: ApprovalConfig,
        *,
        account_id: str | None = None,
        stack_status: str = "CREATE_COMPLETE",
        registry: Mapping[str, Any] | None = None,
        registry_tags: Mapping[str, str] | None = None,
        records: Mapping[str, dict[str, Any]] | None = None,
        record_tags: Mapping[str, Mapping[str, str]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        post_submit_status: str = APPROVED_STATUS,
        submit_response_status: str | None = None,
        template: Mapping[str, Any] | None = None,
        discovery: list[dict[str, Any]] | None = None,
    ) -> None:
        self.config = cfg
        self.calls: list[tuple[str, Any]] = []
        self.account_id = account_id or cfg.account_id
        self.stack_status = stack_status
        self.registry = dict(
            registry
            if registry is not None
            else {
                "name": cfg.registry_name,
                "status": "READY",
                "discoveryConfiguration": {"authorizerType": "AWS_IAM"},
                "approvalConfiguration": {"AutoApprovalRules": ["APPROVE_ALL"]},
                "registryArn": REGISTRY_ARN,
            }
        )
        # The GA control plane returns lower-camel keys; normalise the fixture.
        if "approvalConfiguration" in self.registry:
            approval = self.registry["approvalConfiguration"]
            if isinstance(approval, Mapping) and "AutoApprovalRules" in approval:
                self.registry["approvalConfiguration"] = {
                    "autoApprovalRules": approval["AutoApprovalRules"]
                }
        self.registry_tags = dict(registry_tags if registry_tags is not None else cfg.tags)
        self.records = {
            tool_id: dict(record)
            for tool_id, record in (
                records if records is not None else {t: make_record(t) for t in cfg.expected_tool_ids}
            ).items()
        }
        self.record_tags = {
            tool_id: dict(tags)
            for tool_id, tags in (
                record_tags
                if record_tags is not None
                else {tool_id: cfg.tags for tool_id in self.records}
            ).items()
        }
        self.resources = (
            resources
            if resources is not None
            else [
                {
                    "LogicalResourceId": "GaRegistryRegistry",
                    "ResourceType": apr.REGISTRY_TYPE,
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": REGISTRY_ID,
                },
                *[
                    {
                        "LogicalResourceId": f"GaRegistryRecord{tool_id}",
                        "ResourceType": apr.RECORD_TYPE,
                        "ResourceStatus": "CREATE_COMPLETE",
                        "PhysicalResourceId": record_arn(record["recordId"]),
                    }
                    for tool_id, record in sorted(self.records.items())
                ],
            ]
        )
        self.post_submit_status = post_submit_status
        self.submit_response_status = (
            submit_response_status
            if submit_response_status is not None
            else (
                post_submit_status
                if post_submit_status in apr.KNOWN_SUBMIT_STATUSES
                else "PENDING_APPROVAL"
            )
        )
        self.discovery = discovery
        self.processed_template = dict(
            template if template is not None else processed_stack_template(cfg)
        )

    # --- read paths -------------------------------------------------------- #
    def caller_account(self) -> str:
        self.calls.append(("caller_account", None))
        return self.account_id

    def describe_stack(self, stack_name: str) -> dict[str, Any]:
        self.calls.append(("describe_stack", stack_name))
        return {"StackName": stack_name, "StackStatus": self.stack_status}

    def stack_resources(self, stack_name: str) -> list[dict[str, Any]]:
        self.calls.append(("stack_resources", stack_name))
        return list(self.resources)

    def stack_template(self, stack_name: str) -> dict[str, Any]:
        self.calls.append(("stack_template", stack_name))
        return dict(self.processed_template)

    def get_registry(self, registry_id: str) -> dict[str, Any]:
        self.calls.append(("get_registry", registry_id))
        return dict(self.registry)

    def _tool_for(self, record_id: str) -> str:
        for tool_id, record in self.records.items():
            if record["recordId"] == record_id:
                return tool_id
        raise AssertionError(f"fake has no record {record_id}")

    def get_record(self, registry_id: str, record_id: str) -> dict[str, Any]:
        self.calls.append(("get_record", (registry_id, record_id)))
        return dict(self.records[self._tool_for(record_id)])

    def tags(self, arn: str) -> dict[str, str]:
        self.calls.append(("tags", arn))
        if arn == REGISTRY_ARN:
            return dict(self.registry_tags)
        for tool_id, record in self.records.items():
            if record["recordArn"] == arn:
                return dict(self.record_tags[tool_id])
        raise AssertionError(f"fake has no tags for {arn}")

    def discoverable_records(self, registry_id: str) -> list[dict[str, Any]]:
        self.calls.append(("discoverable_records", registry_id))
        if self.discovery is not None:
            return list(self.discovery)
        return [
            {"recordId": record["recordId"], "status": record["status"]}
            for record in self.records.values()
            if record["status"] == APPROVED_STATUS
        ]

    # --- the only mutation ------------------------------------------------- #
    def submit_record(self, registry_id: str, record_id: str) -> dict[str, Any]:
        self.calls.append(("submit_record", (registry_id, record_id)))
        tool_id = self._tool_for(record_id)
        self.records[tool_id]["status"] = self.post_submit_status
        return {
            "status": self.submit_response_status,
            "ResponseMetadata": {"RequestId": f"req-{tool_id}"},
        }

    # --- assertions helpers ------------------------------------------------ #
    @property
    def submissions(self) -> list[tuple[str, str]]:
        return [payload for name, payload in self.calls if name == "submit_record"]


def evidence_for(cfg: ApprovalConfig) -> Evidence:
    return Evidence(cfg)


def fast_clock() -> tuple[Any, Any, list[float]]:
    slept: list[float] = []
    now = {"value": 0.0}

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        now["value"] += seconds

    def monotonic() -> float:
        return now["value"]

    return sleeper, monotonic, slept


# --------------------------------------------------------------------------- #
# Import hygiene
# --------------------------------------------------------------------------- #


def test_module_imports_without_any_aws_sdk() -> None:
    module_dir = Path(apr.__file__).resolve().parent
    script = f"""
import builtins
import sys

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name.split('.', 1)[0] in {{'boto3', 'botocore'}}:
        raise RuntimeError(f'unexpected AWS SDK import: {{name}}')
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
sys.path.insert(0, {str(module_dir)!r})
import approve_pipeline_registry
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    source = inspect.getsource(apr)
    assert source.count("import boto3") == 1
    assert "import boto3" in inspect.getsource(apr.ApprovalAws.__init__)


# --------------------------------------------------------------------------- #
# Derived names and CLI validation
# --------------------------------------------------------------------------- #


def test_derived_stack_and_registry_names_are_exact() -> None:
    assert stack_name_for("nonprod") == "Nonprod-Registry"
    assert stack_name_for("prod") == "Prod-Registry"
    assert registry_name_for("nonprod") == "agenticai-platform-nonprod-v1"
    assert registry_name_for("prod") == "agenticai-platform-prod-v1"
    with pytest.raises(ApprovalError):
        stack_name_for("staging")
    with pytest.raises(ApprovalError):
        registry_name_for("staging")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"account_id": "12345"}, "12 digits"),
        ({"account_id": "1111111111a1"}, "12 digits"),
        ({"region": "not-a-region"}, "Region"),
        ({"environment": "staging"}, "nonprod"),
        ({"application_id": ""}, "--application-id"),
        ({"cost_centre": " engineering"}, "--cost-centre"),
        ({"expected_tool_ids": ()}, "at least once"),
        ({"expected_tool_ids": ("tool-echo", "tool-echo")}, "unique"),
        ({"expected_tool_ids": ("Tool-Echo",)}, "kebab-case"),
        ({"git_head": "ZZZ"}, "Git SHA"),
        ({"timeout_seconds": 5}, "--timeout-seconds"),
        ({"timeout_seconds": 100000}, "--timeout-seconds"),
        ({"poll_interval_seconds": 0}, "--poll-interval-seconds"),
    ],
)
def test_config_validation_rejects_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ApprovalError, match=message):
        config(tmp_path, monkeypatch, **overrides)


def test_config_rejects_evidence_outside_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("KIROCREW_SCRATCH", str(scratch))
    with pytest.raises(ApprovalError, match="outside KIROCREW_SCRATCH"):
        ApprovalConfig(
            account_id=ACCOUNT_ID,
            region=REGION,
            environment="prod",
            application_id="a",
            agent_id="b",
            tenant_id="c",
            cost_centre="d",
            expected_tool_ids=("tool-echo",),
            git_head=GIT_HEAD,
            evidence_file=tmp_path / "outside.json",
        )


def test_cli_requires_every_governance_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["approve", "--account-id", ACCOUNT_ID])


def test_cli_accepts_repeated_expected_tool_ids() -> None:
    args = parse_args(
        [
            "approve",
            "--account-id",
            ACCOUNT_ID,
            "--region",
            REGION,
            "--environment",
            "prod",
            "--application-id",
            "a",
            "--agent-id",
            "b",
            "--tenant-id",
            "c",
            "--cost-centre",
            "d",
            "--expected-tool-id",
            "tool-echo",
            "--expected-tool-id",
            "tool-ping",
            "--git-head",
            GIT_HEAD,
        ]
    )
    assert args.expected_tool_ids == ["tool-echo", "tool-ping"]
    assert args.action == "approve"


# --------------------------------------------------------------------------- #
# Physical id parsing and fail-closed readers
# --------------------------------------------------------------------------- #


def test_resource_identifier_extracts_ids_and_rejects_junk() -> None:
    assert resource_identifier(REGISTRY_ID, "registry") == REGISTRY_ID
    assert resource_identifier(record_arn("rec00000000000echo"), "record") == "rec00000000000echo"
    assert resource_identifier(f"{REGISTRY_ID}|rec00000000000ping", "record") == "rec00000000000ping"
    for bad in ("", "ab", "rec!!!", "/"):
        with pytest.raises(ApprovalError):
            resource_identifier(bad, "record")


def test_unknown_sdk_shapes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg, registry={"name": cfg.registry_name, "status": "READY"})
    with pytest.raises(ApprovalError, match="Unrecognised SDK response"):
        verify_deployment(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_approve_submits_each_record_exactly_once_in_sorted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    evidence = evidence_for(cfg)
    sleeper, monotonic, _slept = fast_clock()

    approve(aws, cfg, evidence, sleeper=sleeper, monotonic=monotonic)

    assert aws.submissions == [
        (REGISTRY_ID, RECORD_IDS["tool-echo"]),
        (REGISTRY_ID, RECORD_IDS["tool-ping"]),
    ]
    events = [event["event"] for event in evidence.document["events"]]
    assert events.count("processed_stack_template_verified") == 1
    assert events.count("record_submitted") == 2
    assert events.count("record_approved") == 2
    assert "data_plane_discovery_verified" in events
    assert "preflight_all_records_draft" in events


def test_verify_action_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    registry_id, preflighted = verify_deployment(aws, cfg, evidence_for(cfg))
    assert registry_id == REGISTRY_ID
    assert [tool_id for tool_id, _rid, _doc in preflighted] == ["tool-echo", "tool-ping"]
    assert aws.submissions == []


# --------------------------------------------------------------------------- #
# Atomic preflight — nothing is submitted when anything is wrong
# --------------------------------------------------------------------------- #


def test_non_draft_record_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    records = {
        "tool-echo": make_record("tool-echo"),
        "tool-ping": make_record("tool-ping", status=APPROVED_STATUS),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match="not DRAFT"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_unexpected_record_name_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    records = {
        "tool-echo": make_record("tool-echo"),
        "tool-ping": make_record("tool-ping", name="tool-rogue"),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match="unexpected registry record"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_missing_expected_record_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    # Both stack resources resolve to the same record id, so one tool is absent.
    records = {"tool-echo": make_record("tool-echo")}
    resources = [
        {
            "LogicalResourceId": "GaRegistryRegistry",
            "ResourceType": apr.REGISTRY_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": REGISTRY_ID,
        },
        {
            "LogicalResourceId": "GaRegistryRecordEcho",
            "ResourceType": apr.RECORD_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": record_arn(RECORD_IDS["tool-echo"]),
        },
    ]
    aws = FakeAws(cfg, records=records, resources=resources)
    with pytest.raises(ApprovalError, match="Expected exactly 2"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_extra_record_stack_resource_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    aws.resources.append(
        {
            "LogicalResourceId": "GaRegistryRecordRogue",
            "ResourceType": apr.RECORD_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": record_arn("rec0000000000rogue"),
        }
    )
    with pytest.raises(ApprovalError, match="Expected exactly 2"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_two_registry_stack_resources_block_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    aws.resources.append(
        {
            "LogicalResourceId": "SecondRegistry",
            "ResourceType": apr.REGISTRY_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": "reg9999999999zzzz",
        }
    )
    with pytest.raises(ApprovalError, match="exactly one AWS::AgentRegistry::Registry"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


@pytest.mark.parametrize("stack_status", ["ROLLBACK_COMPLETE", "UPDATE_IN_PROGRESS", "DELETE_FAILED"])
def test_non_complete_stack_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stack_status: str
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg, stack_status=stack_status)
    with pytest.raises(ApprovalError, match="not one of"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_incomplete_record_stack_resource_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    aws.resources[1]["ResourceStatus"] = "UPDATE_ROLLBACK_COMPLETE"
    with pytest.raises(ApprovalError, match="not one of"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_wrong_account_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg, account_id="999999999999")
    with pytest.raises(ApprovalError, match="Expected AWS account"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


# --------------------------------------------------------------------------- #
# Registry contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "registry_override, message",
    [
        ({"name": "agenticai-platform-prod-v1"}, "Expected registry"),
        ({"status": "CREATING"}, "not READY"),
        (
            {"discoveryConfiguration": {"authorizerType": "CUSTOM_JWT"}},
            "not AWS_IAM",
        ),
        (
            {"approvalConfiguration": {"autoApprovalRules": ["MANUAL"]}},
            "auto-approval rules",
        ),
    ],
)
def test_registry_contract_violations_block_every_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_override: dict[str, Any],
    message: str,
) -> None:
    cfg = config(tmp_path, monkeypatch)
    registry = {
        "name": cfg.registry_name,
        "status": "READY",
        "discoveryConfiguration": {"authorizerType": "AWS_IAM"},
        "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"]},
        "registryArn": REGISTRY_ARN,
    }
    registry.update(registry_override)
    aws = FakeAws(cfg, registry=registry)
    with pytest.raises(ApprovalError, match=message):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


# --------------------------------------------------------------------------- #
# Tag contract
# --------------------------------------------------------------------------- #


def test_registry_system_tags_are_allowed_but_user_tags_must_match_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    tolerated = {
        **cfg.tags,
        "aws:cloudformation:stack-name": cfg.stack_name,
        "aws:cloudformation:logical-id": "GaRegistryRegistry",
        "aws:cloudformation:stack-id": "arn:aws:cloudformation:stack/id",
    }
    aws = FakeAws(cfg, registry_tags=tolerated)
    verify_deployment(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"environment": "prod"},
        {"cost-centre": "other"},
        {"extra-tag": "unexpected"},
        {"aws:other-service:tag": "unexpected"},
    ],
)
def test_wrong_registry_tags_block_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: dict[str, str]
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg, registry_tags={**cfg.tags, **mutation})
    with pytest.raises(ApprovalError, match="user tags must be exactly"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_missing_record_tag_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    partial = {key: value for key, value in cfg.tags.items() if key != "tenant-id"}
    aws = FakeAws(cfg, record_tags={"tool-echo": partial, "tool-ping": cfg.tags})
    with pytest.raises(ApprovalError, match="registry record tool-echo"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


# --------------------------------------------------------------------------- #
# Governance descriptor contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "document_override, message",
    [
        ({"schemaVersion": "agenticai.tool-governance/2.0"}, "schemaVersion"),
        ({"toolId": "tool-other"}, "descriptor toolId"),
        ({"desiredApprovalStatus": "experimental"}, "desiredApprovalStatus"),
        (
            {"authorization": {"defaultDecision": "ALLOW"}},
            "defaultDecision",
        ),
    ],
)
def test_descriptor_contract_violations_block_every_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document_override: dict[str, Any],
    message: str,
) -> None:
    cfg = config(tmp_path, monkeypatch)
    records = {
        "tool-echo": make_record(
            "tool-echo", document=governance_document("tool-echo", **document_override)
        ),
        "tool-ping": make_record("tool-ping"),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match=message):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


@pytest.mark.parametrize(
    "record_kwargs, message",
    [
        ({"descriptor_data": "not-json"}, "not JSON"),
        ({"descriptor_data": "[1,2,3]"}, "not a JSON object"),
        ({"descriptor_data": ""}, "descriptor data is missing"),
        ({"record_type": "MCP"}, "not CUSTOM"),
    ],
)
def test_malformed_descriptor_blocks_every_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kwargs: dict[str, Any],
    message: str,
) -> None:
    cfg = config(tmp_path, monkeypatch)
    records = {
        "tool-echo": make_record("tool-echo", **record_kwargs),
        "tool-ping": make_record("tool-ping"),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match=message):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_live_descriptor_must_match_processed_stack_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    tampered = governance_document("tool-echo")
    tampered["target"] = {
        "type": "lambda",
        "arn": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:rogue:PROD",
    }
    records = {
        "tool-echo": make_record("tool-echo", document=tampered),
        "tool-ping": make_record("tool-ping"),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match="differs from processed template"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_processed_template_requires_exact_expected_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    template = processed_stack_template(cfg)
    template["Resources"].pop("GaRegistryRecordtoolping")
    aws = FakeAws(cfg, template=template)
    with pytest.raises(ApprovalError, match="names differ from --expected-tool-id"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_processed_template_tag_drift_blocks_every_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    template = processed_stack_template(cfg)
    template["Resources"]["GaRegistryRegistry"]["Properties"]["Tags"].append(
        {"Key": "unexpected", "Value": "drift"}
    )
    aws = FakeAws(cfg, template=template)
    with pytest.raises(ApprovalError, match="user tags must be exactly"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_descriptor_mutation_after_submission_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch, expected_tool_ids=("tool-echo",))
    records = {"tool-echo": make_record("tool-echo")}
    resources = [
        {
            "LogicalResourceId": "GaRegistryRegistry",
            "ResourceType": apr.REGISTRY_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": REGISTRY_ID,
        },
        {
            "LogicalResourceId": "GaRegistryRecordEcho",
            "ResourceType": apr.RECORD_TYPE,
            "ResourceStatus": "CREATE_COMPLETE",
            "PhysicalResourceId": record_arn(RECORD_IDS["tool-echo"]),
        },
    ]

    class MutatingAws(FakeAws):
        def submit_record(self, registry_id: str, record_id: str) -> dict[str, Any]:
            response = super().submit_record(registry_id, record_id)
            tampered = governance_document("tool-echo", description="tampered")
            self.records["tool-echo"]["descriptors"] = {
                "custom": {"data": json.dumps(tampered)}
            }
            return response

    aws = MutatingAws(cfg, records=records, resources=resources)
    with pytest.raises(ApprovalError, match="changed during approval"):
        approve(aws, cfg, evidence_for(cfg))
    assert len(aws.submissions) == 1


# --------------------------------------------------------------------------- #
# Polling and discovery
# --------------------------------------------------------------------------- #


def test_rejected_record_fails_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg, post_submit_status="REJECTED")
    with pytest.raises(ApprovalError, match="terminal status REJECTED"):
        approve(aws, cfg, evidence_for(cfg))
    assert len(aws.submissions) == 2


def test_stuck_record_times_out_without_infinite_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch, timeout_seconds=30, poll_interval_seconds=5)
    aws = FakeAws(cfg, post_submit_status="PENDING_APPROVAL")
    sleeper, monotonic, slept = fast_clock()
    with pytest.raises(ApprovalError, match="did not reach APPROVED"):
        approve(aws, cfg, evidence_for(cfg), sleeper=sleeper, monotonic=monotonic)
    assert sum(slept) <= cfg.timeout_seconds + cfg.poll_interval_seconds


@pytest.mark.parametrize(
    "unknown_status",
    ["MYSTERY_STATE", "SUBMITTED", "IN_REVIEW", "DELETE_FAILED"],
)
def test_unknown_record_status_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_status: str,
) -> None:
    cfg = config(tmp_path, monkeypatch)
    records = {
        "tool-echo": make_record("tool-echo", status=unknown_status),
        "tool-ping": make_record("tool-ping"),
    }
    aws = FakeAws(cfg, records=records)
    with pytest.raises(ApprovalError, match="unknown record status"):
        approve(aws, cfg, evidence_for(cfg))
    assert aws.submissions == []


def test_unknown_submit_status_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)

    class OddSubmitAws(FakeAws):
        def submit_record(self, registry_id: str, record_id: str) -> dict[str, Any]:
            self.calls.append(("submit_record", (registry_id, record_id)))
            return {"status": "QUEUED_SOMEWHERE"}

    aws = OddSubmitAws(cfg)
    with pytest.raises(ApprovalError, match="Unrecognised SubmitRegistryRecordForApproval"):
        approve(aws, cfg, evidence_for(cfg))
    assert len(aws.submissions) == 1


def test_discovery_retries_until_all_approved_records_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(
        tmp_path,
        monkeypatch,
        timeout_seconds=30,
        poll_interval_seconds=5,
    )

    class EventuallyVisibleAws(FakeAws):
        discovery_reads = 0

        def discoverable_records(self, registry_id: str) -> list[dict[str, Any]]:
            self.discovery_reads += 1
            if self.discovery_reads == 1:
                self.calls.append(("discoverable_records", registry_id))
                return []
            return super().discoverable_records(registry_id)

    aws = EventuallyVisibleAws(cfg)
    sleeper, monotonic, slept = fast_clock()
    approve(aws, cfg, evidence_for(cfg), sleeper=sleeper, monotonic=monotonic)
    assert aws.discovery_reads == 2
    assert slept == [5.0]


@pytest.mark.parametrize(
    "discovery",
    [
        [],
        [{"recordId": RECORD_IDS["tool-echo"], "status": APPROVED_STATUS}],
        [
            {"recordId": RECORD_IDS["tool-echo"], "status": APPROVED_STATUS},
            {"recordId": RECORD_IDS["tool-ping"], "status": APPROVED_STATUS},
            {"recordId": "rec0000000000rogue", "status": APPROVED_STATUS},
        ],
    ],
)
def test_discovery_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery: list[dict[str, Any]]
) -> None:
    cfg = config(tmp_path, monkeypatch, timeout_seconds=30, poll_interval_seconds=5)
    aws = FakeAws(cfg, discovery=discovery)
    sleeper, monotonic, _slept = fast_clock()
    with pytest.raises(ApprovalError, match="discovery mismatch"):
        approve(aws, cfg, evidence_for(cfg), sleeper=sleeper, monotonic=monotonic)


def test_discovery_with_non_approved_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(
        cfg,
        discovery=[
            {"recordId": RECORD_IDS["tool-echo"], "status": APPROVED_STATUS},
            {"recordId": RECORD_IDS["tool-ping"], "status": "DRAFT"},
        ],
    )
    with pytest.raises(ApprovalError, match="not APPROVED"):
        approve(aws, cfg, evidence_for(cfg))


# --------------------------------------------------------------------------- #
# Evidence safety
# --------------------------------------------------------------------------- #


def test_evidence_is_written_under_scratch_and_is_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    aws = FakeAws(cfg)
    evidence = evidence_for(cfg)
    approve(aws, cfg, evidence)
    written = json.loads(cfg.evidence_file.read_text(encoding="utf-8"))
    assert written["schemaVersion"] == apr.EVIDENCE_SCHEMA_VERSION
    assert written["stackName"] == "Nonprod-Registry"
    assert written["registryName"] == "agenticai-platform-nonprod-v1"
    assert written["expectedToolIds"] == ["tool-echo", "tool-ping"]
    assert cfg.evidence_file.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "details",
    [
        {"token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX3ZhbHVl"},
        {"sessionToken": "harmless"},
        {"value": "AKIA" + ("A" * 16)},
        {"header": "Bearer abcdefghijklmnopqrstuvwxyz"},
    ],
)
def test_evidence_rejects_credential_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, details: dict[str, str]
) -> None:
    cfg = config(tmp_path, monkeypatch)
    evidence = evidence_for(cfg)
    with pytest.raises(ApprovalError):
        evidence.add("suspicious", **details)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def cli_argv(tmp_path: Path, action: str) -> list[str]:
    return [
        action,
        "--account-id",
        ACCOUNT_ID,
        "--region",
        REGION,
        "--environment",
        "nonprod",
        "--application-id",
        "agenticai-platform",
        "--agent-id",
        "registry-producer",
        "--tenant-id",
        "platform",
        "--cost-centre",
        "engineering",
        "--expected-tool-id",
        "tool-ping",
        "--expected-tool-id",
        "tool-echo",
        "--git-head",
        GIT_HEAD,
        "--evidence-file",
        str(tmp_path / "cli-evidence.json"),
        "--poll-interval-seconds",
        "1",
    ]


def test_run_approve_writes_passing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    created: list[FakeAws] = []

    def factory(region: str) -> FakeAws:
        assert region == REGION
        cfg = config(tmp_path, monkeypatch)
        fake = FakeAws(cfg)
        created.append(fake)
        return fake

    assert run(cli_argv(tmp_path, "approve"), aws_factory=factory) == 0
    written = json.loads((tmp_path / "cli-evidence.json").read_text(encoding="utf-8"))
    assert written["status"] == "approve-passed"
    assert written["action"] == "approve"
    assert len(created[0].submissions) == 2


def test_run_verify_submits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    created: list[FakeAws] = []

    def factory(_region: str) -> FakeAws:
        cfg = config(tmp_path, monkeypatch)
        fake = FakeAws(cfg)
        created.append(fake)
        return fake

    assert run(cli_argv(tmp_path, "verify"), aws_factory=factory) == 0
    written = json.loads((tmp_path / "cli-evidence.json").read_text(encoding="utf-8"))
    assert written["status"] == "verify-passed"
    assert created[0].submissions == []


def test_run_records_failure_and_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))

    def factory(_region: str) -> FakeAws:
        cfg = config(tmp_path, monkeypatch)
        return FakeAws(cfg, stack_status="ROLLBACK_COMPLETE")

    assert run(cli_argv(tmp_path, "approve"), aws_factory=factory) == 1
    written = json.loads((tmp_path / "cli-evidence.json").read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert written["errorType"] == "ApprovalError"


def test_run_redacts_credential_shaped_error_on_failure_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX3ZhbHVl"

    class ExplodingAws(FakeAws):
        def caller_account(self) -> str:  # raises before any submission
            raise apr.ApprovalError(f"boom token={jwt}")

    def factory(_region: str) -> FakeAws:
        cfg = config(tmp_path, monkeypatch)
        return ExplodingAws(cfg)

    assert run(cli_argv(tmp_path, "approve"), aws_factory=factory) == 1
    written = json.loads((tmp_path / "cli-evidence.json").read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert written["error"] == "[REDACTED]"
    assert jwt not in json.dumps(written)
    captured = capsys.readouterr()
    assert jwt not in captured.err
    assert "ERROR: [REDACTED]" in captured.err


def test_run_rejects_invalid_config_before_touching_aws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))

    def factory(_region: str) -> FakeAws:
        raise AssertionError("aws_factory must not be called for invalid input")

    argv = cli_argv(tmp_path, "approve")
    argv[argv.index("--account-id") + 1] = "123"
    assert run(argv, aws_factory=factory) == 2


def test_run_rejects_unwritable_evidence_path_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable evidence path must exit 2, not raise an uncaught OSError."""
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)

    def factory(_region: str) -> FakeAws:
        raise AssertionError("aws_factory must not be called when evidence is unusable")

    argv = cli_argv(tmp_path, "approve")
    argv[argv.index("--evidence-file") + 1] = str(read_only / "nested" / "evidence.json")
    try:
        assert run(argv, aws_factory=factory) == 2
    finally:
        read_only.chmod(0o700)
