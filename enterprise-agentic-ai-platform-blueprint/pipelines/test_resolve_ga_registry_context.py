"""Offline guards for the Workload pipeline GA Registry context resolver."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

MODULE_PATH = Path(__file__).with_name("resolve_ga_registry_context.py")
SPEC = importlib.util.spec_from_file_location("resolve_ga_registry_context", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)

ACCOUNT = "222222222222"
REGION = "us-west-2"
REGISTRY_ID = "ABCDEFGHIJKLMNOP"
RECORD_IDS = {"tool-echo": "ABCDEFGHIJKL", "tool-ping": "MNOPQRSTUVWX"}
SOURCE = "a" * 40


def governance(tool_id: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schemaVersion": "agenticai.tool-governance/1.0",
        "catalogueVersion": "1",
        "toolId": tool_id,
        "description": f"{tool_id} description",
        "desiredApprovalStatus": "approved",
        "target": {
            "type": "lambda",
            "arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{tool_id}:PROD",
        },
        "mcp": {
            "toolName": tool_id,
            "description": f"{tool_id} description",
            "inputSchema": {"type": "object"},
        },
        "authorization": {
            "defaultDecision": "DENY",
            "cedarPolicy": f'permit(principal, action, resource == Tool::"{tool_id}");',
            "allowedSubjects": [],
            "allowedGroups": [],
            "combination": "AUTHENTICATED",
        },
        "ownership": {"ownerTeam": "platform-ai", "costCentre": "platform"},
    }
    value.update(overrides)
    return value


def config(tmp_path: Path, **overrides: Any):
    values: dict[str, Any] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": "nonprod",
        "application_id": "demo",
        "agent_id": "primary",
        "tenant_id": "demo",
        "cost_centre": "engineering",
        "expected_tool_ids": ("tool-ping", "tool-echo"),
        "source_revision": SOURCE,
        "output": tmp_path / "context.json",
    }
    values.update(overrides)
    return resolver.ResolverConfig(**values)


class FakeAws:
    def __init__(
        self,
        cfg: Any,
        *,
        parameters: Mapping[str, str] | None = None,
        registry: Mapping[str, Any] | None = None,
        registry_tags: Mapping[str, str] | None = None,
        records: Mapping[str, Mapping[str, Any]] | None = None,
        record_tags: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.config = cfg
        registry_arn = (
            f"arn:aws:agent-registry:{cfg.region}:{cfg.account_id}:registry/{REGISTRY_ID}"
        )
        defaults = {
            cfg.parameter_names["registryId"]: REGISTRY_ID,
            cfg.parameter_names["registryArn"]: registry_arn,
            cfg.parameter_names["readerRoleArn"]: cfg.reader_role_arn,
            cfg.parameter_names["readerExternalId"]: (
                f"agenticai-registry-v1-{cfg.environment}-{cfg.account_id}"
            ),
            **{
                cfg.parameter_names[f"record:{tool_id}"]: RECORD_IDS[tool_id]
                for tool_id in cfg.expected_tool_ids
            },
        }
        self.parameter_values = dict(parameters if parameters is not None else defaults)
        self.registry_value = dict(
            registry
            if registry is not None
            else {
                "registryId": REGISTRY_ID,
                "registryArn": registry_arn,
                "name": f"agenticai-platform-{cfg.environment}-v1",
                "status": "READY",
                "discoveryConfiguration": {"authorizerType": "AWS_IAM"},
                "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"]},
            }
        )
        self.registry_tags = dict(registry_tags if registry_tags is not None else cfg.tags)
        self.records = {
            tool_id: dict(record)
            for tool_id, record in (
                records
                if records is not None
                else {
                    tool_id: {
                        "recordId": RECORD_IDS[tool_id],
                        "recordArn": f"{registry_arn}/record/{RECORD_IDS[tool_id]}",
                        "name": tool_id,
                        "status": "APPROVED",
                        "recordType": "CUSTOM",
                        "recordVersion": "1.0.0",
                        "descriptors": {
                            "custom": {"data": json.dumps(governance(tool_id))}
                        },
                    }
                    for tool_id in cfg.expected_tool_ids
                }
            ).items()
        }
        self.record_tags = {
            tool_id: dict(tags)
            for tool_id, tags in (
                record_tags
                if record_tags is not None
                else {tool_id: cfg.tags for tool_id in cfg.expected_tool_ids}
            ).items()
        }

    def parameters(self, _names: list[str]) -> Mapping[str, str]:
        return self.parameter_values

    def registry(self, _registry_id: str) -> Mapping[str, Any]:
        return self.registry_value

    def record(self, _registry_id: str, record_id: str) -> Mapping[str, Any]:
        tool_id = next(tool for tool, rid in RECORD_IDS.items() if rid == record_id)
        return self.records[tool_id]

    def tags(self, arn: str) -> Mapping[str, str]:
        if arn.endswith(f"registry/{REGISTRY_ID}"):
            return self.registry_tags
        tool_id = next(tool for tool, rid in RECORD_IDS.items() if arn.endswith(rid))
        return self.record_tags[tool_id]


def test_resolve_context_returns_sorted_exact_contract(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    result = resolver.resolve_context(FakeAws(cfg), cfg)
    assert result["schemaVersion"] == resolver.SCHEMA_VERSION
    assert result["sourceRevision"] == SOURCE
    assert [item["document"]["toolId"] for item in result["records"]] == [
        "tool-echo",
        "tool-ping",
    ]
    assert all(len(item["descriptorSha256"]) == 64 for item in result["records"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "123"},
        {"region": "wrong"},
        {"environment": "stage"},
        {"expected_tool_ids": ()},
        {"expected_tool_ids": ("tool-echo", "tool-echo")},
        {"expected_tool_ids": ("BAD",)},
        {"source_revision": "short"},
        {"application_id": ""},
    ],
)
def test_config_rejects_invalid_input(tmp_path: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(resolver.ResolutionError):
        config(tmp_path, **overrides)


def test_parameter_contract_fails_closed(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    fake = FakeAws(cfg)
    missing = dict(fake.parameter_values)
    missing.pop(cfg.parameter_names["registryId"])
    with pytest.raises(resolver.ResolutionError, match="incomplete"):
        resolver.resolve_context(FakeAws(cfg, parameters=missing), cfg)

    wrong_arn = dict(fake.parameter_values)
    wrong_arn[cfg.parameter_names["registryArn"]] += "-wrong"
    with pytest.raises(resolver.ResolutionError, match="registry ARN"):
        resolver.resolve_context(FakeAws(cfg, parameters=wrong_arn), cfg)

    wrong_role = dict(fake.parameter_values)
    wrong_role[cfg.parameter_names["readerRoleArn"]] = (
        f"arn:aws:iam::{ACCOUNT}:role/Admin"
    )
    with pytest.raises(resolver.ResolutionError, match="reader role"):
        resolver.resolve_context(FakeAws(cfg, parameters=wrong_role), cfg)

    wrong_external = dict(fake.parameter_values)
    wrong_external[cfg.parameter_names["readerExternalId"]] = "wrong"
    with pytest.raises(resolver.ResolutionError, match="ExternalId"):
        resolver.resolve_context(FakeAws(cfg, parameters=wrong_external), cfg)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"status": "CREATING"}, "not READY"),
        ({"name": "wrong"}, "name changed"),
        ({"discoveryConfiguration": {"authorizerType": "CUSTOM_JWT"}}, "authorizer"),
        ({"approvalConfiguration": {"autoApprovalRules": []}}, "approval"),
    ],
)
def test_registry_contract_fails_closed(
    tmp_path: Path, mutation: dict[str, Any], message: str
) -> None:
    cfg = config(tmp_path)
    fake = FakeAws(cfg)
    registry = dict(fake.registry_value)
    registry.update(mutation)
    with pytest.raises(resolver.ResolutionError, match=message):
        resolver.resolve_context(FakeAws(cfg, registry=registry), cfg)


def test_registry_and_record_tag_drift_fail_closed(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    with pytest.raises(resolver.ResolutionError, match="Registry tags"):
        resolver.resolve_context(FakeAws(cfg, registry_tags={**cfg.tags, "extra": "x"}), cfg)
    record_tags = {tool_id: cfg.tags for tool_id in cfg.expected_tool_ids}
    record_tags["tool-echo"] = {**cfg.tags, "environment": "prod"}
    with pytest.raises(resolver.ResolutionError, match="record tool-echo tags"):
        resolver.resolve_context(FakeAws(cfg, record_tags=record_tags), cfg)


@pytest.mark.parametrize(
    "record_mutation, message",
    [
        ({"status": "DRAFT"}, "not APPROVED"),
        ({"recordType": "MCP"}, "not CUSTOM"),
        ({"recordVersion": "2.0.0"}, "version does not match"),
        ({"name": "tool-other"}, "name mismatch"),
    ],
)
def test_record_contract_fails_closed(
    tmp_path: Path, record_mutation: dict[str, Any], message: str
) -> None:
    cfg = config(tmp_path)
    fake = FakeAws(cfg)
    records = {tool: dict(value) for tool, value in fake.records.items()}
    records["tool-echo"].update(record_mutation)
    with pytest.raises(resolver.ResolutionError, match=message):
        resolver.resolve_context(FakeAws(cfg, records=records), cfg)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update({"desiredApprovalStatus": "deprecated"}),
        lambda doc: doc["target"].update({"arn": f"arn:aws:lambda:eu-west-1:{ACCOUNT}:function:rogue:PROD"}),
        lambda doc: doc["target"].update({"arn": "arn:aws:lambda:us-east-1:333333333333:function:rogue:PROD"}),
        lambda doc: doc["mcp"].update({"toolName": "tool-other"}),
        lambda doc: doc["authorization"].update({"defaultDecision": "ALLOW"}),
        lambda doc: doc["authorization"].update({"allowedSubjects": ["developer-a"]}),
        lambda doc: doc["ownership"].update({"ownerTeam": ""}),
    ],
)
def test_governance_drift_fails_closed(tmp_path: Path, mutate: Any) -> None:
    cfg = config(tmp_path)
    fake = FakeAws(cfg)
    records = {tool: dict(value) for tool, value in fake.records.items()}
    document = governance("tool-echo")
    mutate(document)
    records["tool-echo"]["descriptors"] = {
        "custom": {"data": json.dumps(document)}
    }
    with pytest.raises(resolver.ResolutionError):
        resolver.resolve_context(FakeAws(cfg, records=records), cfg)


def test_system_cloudformation_tags_are_tolerated(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    tags = {**cfg.tags, "aws:cloudformation:stack-name": "Prod-Registry"}
    result = resolver.resolve_context(FakeAws(cfg, registry_tags=tags), cfg)
    assert result["registryId"] == REGISTRY_ID


def test_run_writes_context_atomically(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "context.json"
    argv = [
        "--account-id", ACCOUNT,
        "--region", REGION,
        "--environment", "nonprod",
        "--application-id", "demo",
        "--agent-id", "primary",
        "--tenant-id", "demo",
        "--cost-centre", "engineering",
        "--expected-tool-id", "tool-ping",
        "--expected-tool-id", "tool-echo",
        "--source-revision", SOURCE,
        "--output", str(output),
    ]

    def factory(cfg: Any) -> FakeAws:
        return FakeAws(cfg)

    assert resolver.run(argv, aws_factory=factory) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schemaVersion"] == resolver.SCHEMA_VERSION
    assert [record["document"]["toolId"] for record in written["records"]] == [
        "tool-echo",
        "tool-ping",
    ]


def test_module_imports_boto3_only_inside_live_facade() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert source.count("import boto3") == 1
    assert "import boto3  # Lazy" in source
