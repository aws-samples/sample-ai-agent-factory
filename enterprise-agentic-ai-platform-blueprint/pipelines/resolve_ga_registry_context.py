#!/usr/bin/env python3
"""Resolve approved GA Registry records for a Workload pipeline CDK synth.

The resolver runs in the Platform-side synth project. It reads only the exact
versioned SSM parameters for one environment, verifies the live Registry and
complete governance documents, and atomically writes a strict context file.
It never creates, updates, submits, approves, or deletes an AWS resource.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "agenticai.ga-registry-consumer-context/1.0"
GOVERNANCE_SCHEMA_VERSION = "agenticai.tool-governance/1.0"
SYSTEM_TAG_PREFIX = "aws:cloudformation:"
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
REGISTRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{16}$")
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{12}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResolutionError(RuntimeError):
    """The context cannot be resolved without weakening a contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ResolutionError(message)


def mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolutionError(f"{label} must be an object")
    return value


def sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ResolutionError(f"{label} must be a list")
    return list(value)


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ResolutionError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    actual = set(value)
    if actual != keys:
        raise ResolutionError(
            f"{label} keys must be exactly {sorted(keys)}; got {sorted(actual)}"
        )


@dataclass(frozen=True)
class ResolverConfig:
    account_id: str
    region: str
    environment: str
    application_id: str
    agent_id: str
    tenant_id: str
    cost_centre: str
    expected_tool_ids: tuple[str, ...]
    source_revision: str
    output: Path
    assume_reader: bool = False

    def __post_init__(self) -> None:
        require(bool(ACCOUNT_PATTERN.fullmatch(self.account_id)), "--account-id must be 12 digits")
        require(bool(REGION_PATTERN.fullmatch(self.region)), "--region is invalid")
        require(self.environment in {"nonprod", "prod"}, "--environment must be nonprod or prod")
        require(bool(SHA_PATTERN.fullmatch(self.source_revision)), "--source-revision must be a full lower-case Git SHA")
        require(bool(self.expected_tool_ids), "--expected-tool-id is required")
        require(len(set(self.expected_tool_ids)) == len(self.expected_tool_ids), "--expected-tool-id values must be unique")
        for tool_id in self.expected_tool_ids:
            require(bool(TOOL_ID_PATTERN.fullmatch(tool_id)), f"invalid tool id {tool_id!r}")
        for label, value in (
            ("application-id", self.application_id),
            ("agent-id", self.agent_id),
            ("tenant-id", self.tenant_id),
            ("cost-centre", self.cost_centre),
        ):
            text(value, label)

    @property
    def prefix(self) -> str:
        return f"/agenticai/registry/v1/{self.environment}"

    @property
    def reader_role_arn(self) -> str:
        return (
            f"arn:aws:iam::{self.account_id}:role/"
            f"AgenticAI-RegistryReader-{self.environment}"
        )

    @property
    def synth_external_id(self) -> str:
        return (
            f"agenticai-registry-synth-v1-{self.environment}-{self.account_id}"
        )

    @property
    def tags(self) -> dict[str, str]:
        return {
            "application-id": self.application_id,
            "agent-id": self.agent_id,
            "tenant-id": self.tenant_id,
            "cost-centre": self.cost_centre,
            "environment": self.environment,
        }

    @property
    def parameter_names(self) -> dict[str, str]:
        result = {
            "registryId": f"{self.prefix}/id",
            "registryArn": f"{self.prefix}/arn",
            "readerRoleArn": f"{self.prefix}/reader-role-arn",
            "readerExternalId": f"{self.prefix}/reader-external-id",
        }
        result.update(
            {
                f"record:{tool_id}": f"{self.prefix}/records/{tool_id}/id"
                for tool_id in self.expected_tool_ids
            }
        )
        return result


class RegistryAws:
    """Narrow AWS facade; tests substitute an in-process fake."""

    def __init__(self, config: ResolverConfig) -> None:
        import boto3  # Lazy: unit tests do not need or import the SDK.

        base = boto3.Session(region_name=config.region)
        identity = base.client("sts").get_caller_identity()
        caller_account = str(identity.get("Account", ""))
        if config.assume_reader:
            assumed = base.client("sts").assume_role(
                RoleArn=config.reader_role_arn,
                RoleSessionName=f"registry-synth-{config.environment}",
                ExternalId=config.synth_external_id,
                DurationSeconds=900,
            )
            credentials = mapping(assumed.get("Credentials"), "AssumeRole Credentials")
            session = boto3.Session(
                aws_access_key_id=text(credentials.get("AccessKeyId"), "AccessKeyId"),
                aws_secret_access_key=text(credentials.get("SecretAccessKey"), "SecretAccessKey"),
                aws_session_token=text(credentials.get("SessionToken"), "SessionToken"),
                region_name=config.region,
            )
        else:
            require(
                caller_account == config.account_id,
                f"direct mode expected account {config.account_id}, got {caller_account}",
            )
            session = base
        self.ssm = session.client("ssm")
        self.control = session.client("agent-registry-control")

    def parameters(self, names: Sequence[str]) -> Mapping[str, str]:
        response = mapping(
            self.ssm.get_parameters(Names=list(names), WithDecryption=False),
            "GetParameters",
        )
        invalid = sequence(response.get("InvalidParameters", []), "InvalidParameters")
        require(not invalid, f"missing SSM parameters: {sorted(str(item) for item in invalid)}")
        result: dict[str, str] = {}
        for item in sequence(response.get("Parameters"), "Parameters"):
            parameter = mapping(item, "Parameter")
            name = text(parameter.get("Name"), "Parameter.Name")
            require(name not in result, f"duplicate SSM parameter {name}")
            result[name] = text(parameter.get("Value"), f"Parameter {name} value")
        return result

    def registry(self, registry_id: str) -> Mapping[str, Any]:
        return mapping(self.control.get_registry(registryId=registry_id), "GetRegistry")

    def record(self, registry_id: str, record_id: str) -> Mapping[str, Any]:
        return mapping(
            self.control.get_registry_record(
                registryId=registry_id,
                recordId=record_id,
            ),
            "GetRegistryRecord",
        )

    def tags(self, arn: str) -> Mapping[str, str]:
        response = mapping(
            self.control.list_tags_for_resource(resourceArn=arn),
            "ListTagsForResource",
        )
        return {
            str(key): str(value)
            for key, value in mapping(response.get("tags"), "tags").items()
        }


def user_tags(tags: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in tags.items()
        if not str(key).startswith(SYSTEM_TAG_PREFIX)
    }


def require_tags(actual: Mapping[str, str], config: ResolverConfig, label: str) -> None:
    observed = user_tags(actual)
    require(observed == config.tags, f"{label} tags differ: {observed}")


def validate_document(
    raw: Any,
    tool_id: str,
    config: ResolverConfig,
) -> dict[str, Any]:
    document = mapping(raw, f"record {tool_id} document")
    exact_keys(
        document,
        {
            "schemaVersion",
            "catalogueVersion",
            "toolId",
            "description",
            "desiredApprovalStatus",
            "target",
            "mcp",
            "authorization",
            "ownership",
        },
        f"record {tool_id} document",
    )
    require(document.get("schemaVersion") == GOVERNANCE_SCHEMA_VERSION, f"record {tool_id} schemaVersion changed")
    text(document.get("catalogueVersion"), f"record {tool_id} catalogueVersion")
    require(document.get("toolId") == tool_id, f"record {tool_id} toolId changed")
    description = text(document.get("description"), f"record {tool_id} description")
    require(document.get("desiredApprovalStatus") == "approved", f"record {tool_id} is not intended for approval")

    target = mapping(document.get("target"), f"record {tool_id} target")
    exact_keys(target, {"type", "arn"}, f"record {tool_id} target")
    require(target.get("type") == "lambda", f"record {tool_id} target type is not lambda")
    target_arn = text(target.get("arn"), f"record {tool_id} target arn")
    require(
        bool(
            re.fullmatch(
                rf"arn:(?:aws|aws-us-gov|aws-cn):lambda:{re.escape(config.region)}:{config.account_id}:function:[A-Za-z0-9-_]+:[A-Za-z0-9-_$]+",
                target_arn,
            )
        ),
        f"record {tool_id} target ARN is not a same-Region Platform Lambda alias",
    )

    mcp = mapping(document.get("mcp"), f"record {tool_id} mcp")
    exact_keys(mcp, {"toolName", "description", "inputSchema"}, f"record {tool_id} mcp")
    require(mcp.get("toolName") == tool_id, f"record {tool_id} MCP name changed")
    require(mcp.get("description") == description, f"record {tool_id} MCP description changed")
    mapping(mcp.get("inputSchema"), f"record {tool_id} inputSchema")

    authorization = mapping(document.get("authorization"), f"record {tool_id} authorization")
    exact_keys(
        authorization,
        {"defaultDecision", "cedarPolicy", "allowedSubjects", "allowedGroups", "combination"},
        f"record {tool_id} authorization",
    )
    require(authorization.get("defaultDecision") == "DENY", f"record {tool_id} default decision changed")
    require("permit" in text(authorization.get("cedarPolicy"), f"record {tool_id} cedarPolicy"), f"record {tool_id} Cedar has no permit")
    subjects = sequence(authorization.get("allowedSubjects"), f"record {tool_id} allowedSubjects")
    require(not subjects, f"record {tool_id} allowedSubjects is reserved for the later per-sub migration")
    groups = sequence(authorization.get("allowedGroups"), f"record {tool_id} allowedGroups")
    require(all(isinstance(group, str) and group for group in groups), f"record {tool_id} allowedGroups is invalid")
    require(len(set(groups)) == len(groups), f"record {tool_id} allowedGroups has duplicates")
    expected_combination = "GROUP_ONLY" if groups else "AUTHENTICATED"
    require(authorization.get("combination") == expected_combination, f"record {tool_id} combination changed")

    ownership = mapping(document.get("ownership"), f"record {tool_id} ownership")
    exact_keys(ownership, {"ownerTeam", "costCentre"}, f"record {tool_id} ownership")
    text(ownership.get("ownerTeam"), f"record {tool_id} ownerTeam")
    text(ownership.get("costCentre"), f"record {tool_id} ownership costCentre")
    return dict(document)


def resolve_context(aws: Any, config: ResolverConfig) -> dict[str, Any]:
    parameter_names = config.parameter_names
    values_by_name = aws.parameters(list(parameter_names.values()))
    require(
        set(values_by_name) == set(parameter_names.values()),
        "GetParameters returned an incomplete or unexpected parameter set",
    )
    values = {key: values_by_name[name] for key, name in parameter_names.items()}
    registry_id = values["registryId"]
    require(bool(REGISTRY_ID_PATTERN.fullmatch(registry_id)), "registry ID shape is invalid")
    expected_registry_arn = (
        f"arn:aws:agent-registry:{config.region}:{config.account_id}:registry/{registry_id}"
    )
    require(values["registryArn"] == expected_registry_arn, "registry ARN does not match the ID")
    require(values["readerRoleArn"] == config.reader_role_arn, "reader role ARN differs from R1")
    require(
        values["readerExternalId"]
        == f"agenticai-registry-v1-{config.environment}-{config.account_id}",
        "Workstream reader ExternalId differs from R1",
    )

    registry = aws.registry(registry_id)
    require(registry.get("registryId") == registry_id, "GetRegistry returned a different ID")
    require(registry.get("registryArn") == expected_registry_arn, "GetRegistry returned a different ARN")
    require(registry.get("name") == f"agenticai-platform-{config.environment}-v1", "Registry name changed")
    require(registry.get("status") == "READY", "Registry is not READY")
    discovery = mapping(registry.get("discoveryConfiguration"), "discoveryConfiguration")
    require(discovery.get("authorizerType") == "AWS_IAM", "Registry authorizer is not AWS_IAM")
    approval = mapping(registry.get("approvalConfiguration"), "approvalConfiguration")
    require(approval.get("autoApprovalRules") == ["APPROVE_ALL"], "Registry approval configuration changed")
    require_tags(aws.tags(expected_registry_arn), config, "Registry")

    records: list[dict[str, Any]] = []
    for tool_id in sorted(config.expected_tool_ids):
        record_id = values[f"record:{tool_id}"]
        require(bool(RECORD_ID_PATTERN.fullmatch(record_id)), f"record ID for {tool_id} is invalid")
        record = aws.record(registry_id, record_id)
        record_arn = f"{expected_registry_arn}/record/{record_id}"
        require(record.get("recordId") == record_id, f"record ID mismatch for {tool_id}")
        require(record.get("recordArn") == record_arn, f"record ARN mismatch for {tool_id}")
        require(record.get("name") == tool_id, f"record name mismatch for {tool_id}")
        require(record.get("status") == "APPROVED", f"record {tool_id} is not APPROVED")
        require(record.get("recordType") == "CUSTOM", f"record {tool_id} is not CUSTOM")
        descriptors = mapping(record.get("descriptors"), f"record {tool_id} descriptors")
        custom = mapping(descriptors.get("custom"), f"record {tool_id} custom descriptor")
        data = text(custom.get("data"), f"record {tool_id} descriptor data")
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as error:
            raise ResolutionError(f"record {tool_id} descriptor is not JSON") from error
        document = validate_document(parsed, tool_id, config)
        expected_record_version = f"{document['catalogueVersion']}.0.0"
        require(
            record.get("recordVersion") == expected_record_version,
            f"record {tool_id} version does not match catalogueVersion",
        )
        require_tags(aws.tags(record_arn), config, f"record {tool_id}")
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        require(bool(DIGEST_PATTERN.fullmatch(digest)), "descriptor digest failed")
        records.append(
            {
                "recordId": record_id,
                "recordArn": record_arn,
                "descriptorSha256": digest,
                "document": document,
            }
        )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "environment": config.environment,
        "region": config.region,
        "platformAccountId": config.account_id,
        "sourceRevision": config.source_revision,
        "registryId": registry_id,
        "registryArn": expected_registry_arn,
        "readerRoleArn": config.reader_role_arn,
        "readerExternalId": values["readerExternalId"],
        "records": records,
    }


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", choices=("nonprod", "prod"), required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--cost-centre", required=True)
    parser.add_argument("--expected-tool-id", action="append", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--assume-reader",
        action="store_true",
        help="Assume the environment's R1 reader role (required in pipeline synth).",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> ResolverConfig:
    return ResolverConfig(
        account_id=args.account_id,
        region=args.region,
        environment=args.environment,
        application_id=args.application_id,
        agent_id=args.agent_id,
        tenant_id=args.tenant_id,
        cost_centre=args.cost_centre,
        expected_tool_ids=tuple(args.expected_tool_id),
        source_revision=args.source_revision,
        output=Path(args.output),
        assume_reader=bool(args.assume_reader),
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    aws_factory: Any = RegistryAws,
) -> int:
    try:
        config = config_from_args(parse_args(argv))
        context = resolve_context(aws_factory(config), config)
        atomic_write(config.output, context)
    except ResolutionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "resolved",
                "environment": config.environment,
                "recordCount": len(context["records"]),
                "output": str(config.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
