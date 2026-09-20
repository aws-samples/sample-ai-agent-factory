#!/usr/bin/env python3
"""Manual governance approval for the pipeline-owned R1 GA Registry producer.

This utility performs the human governance step that the Platform pipeline
deliberately does not automate: it takes the ``Nonprod-Registry`` or
``Prod-Registry`` stack that the pipeline already deployed, proves every
expected ``AWS::AgentRegistry::RegistryRecord`` is exactly the catalogued
``DRAFT`` governance record, and only then submits each one, exactly once, for
approval.

It is NOT a Workstream deployment and NOT a compatibility spike. It creates
nothing, never updates or deletes a record, and refuses to submit anything at
all unless every expected record passes preflight first.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent_registry_spike import (
    RECORD_TYPE,
    REGISTRY_TYPE,
    SpikeError,
    assert_no_secrets,
    atomic_json_write,
    aws_error_code,
    request_id,
    require_scratch_path,
    scratch_root,
)

EVIDENCE_SCHEMA_VERSION = "agenticai.registry-approval/1.0"
GOVERNANCE_SCHEMA_VERSION = "agenticai.tool-governance/1.0"
REQUIRED_DESIRED_STATUS = "approved"
REQUIRED_DEFAULT_DECISION = "DENY"
REQUIRED_AUTHORIZER_TYPE = "AWS_IAM"
REQUIRED_AUTO_APPROVAL_RULES = ["APPROVE_ALL"]
REQUIRED_REGISTRY_STATUS = "READY"
ACCEPTED_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
ACCEPTED_RESOURCE_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
DRAFT_STATUS = "DRAFT"
APPROVED_STATUS = "APPROVED"
PENDING_RECORD_STATUSES = {
    DRAFT_STATUS,
    "PENDING_APPROVAL",
    "CREATING",
    "UPDATING",
}
FAILED_RECORD_STATUSES = {
    "REJECTED",
    "DEPRECATED",
    "CREATE_FAILED",
    "UPDATE_FAILED",
}
KNOWN_RECORD_STATUSES = PENDING_RECORD_STATUSES | FAILED_RECORD_STATUSES | {APPROVED_STATUS}
KNOWN_SUBMIT_STATUSES = KNOWN_RECORD_STATUSES
SYSTEM_TAG_PREFIX = "aws:cloudformation:"
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
IDENTIFIER_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{3,127}$")
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600


class ApprovalError(SpikeError):
    """The manual governance-approval contract failed; nothing was submitted."""


def approval_scratch_path(path: Path) -> Path:
    """Apply shared scratch confinement while preserving this utility's error type."""
    try:
        return require_scratch_path(path)
    except SpikeError as error:
        raise ApprovalError(str(error)) from error


def approval_json_write(path: Path, value: Mapping[str, Any]) -> None:
    """Apply shared evidence redaction/write guards as ApprovalError failures."""
    try:
        atomic_json_write(path, value)
    except SpikeError as error:
        raise ApprovalError(str(error)) from error


def approval_no_secrets(value: Any) -> None:
    """Apply shared evidence redaction as an ApprovalError failure."""
    try:
        assert_no_secrets(value)
    except SpikeError as error:
        raise ApprovalError(str(error)) from error


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def stack_name_for(environment: str) -> str:
    """Exact pipeline-owned stack name for one Platform environment."""
    if environment == "nonprod":
        return "Nonprod-Registry"
    if environment == "prod":
        return "Prod-Registry"
    raise ApprovalError("--environment must be exactly 'nonprod' or 'prod'")


def registry_name_for(environment: str) -> str:
    """Exact registry name emitted by GaPlatformRegistryConstruct."""
    if environment not in {"nonprod", "prod"}:
        raise ApprovalError("--environment must be exactly 'nonprod' or 'prod'")
    return f"agenticai-platform-{environment}-v1"


@dataclass(frozen=True)
class ApprovalConfig:
    account_id: str
    region: str
    environment: str
    application_id: str
    agent_id: str
    tenant_id: str
    cost_centre: str
    expected_tool_ids: tuple[str, ...]
    git_head: str
    evidence_file: Path
    timeout_seconds: int = 900
    poll_interval_seconds: int = 5

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{12}", self.account_id):
            raise ApprovalError("--account-id must be exactly 12 digits")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", self.region):
            raise ApprovalError("--region is not a valid AWS Region identifier")
        if self.environment not in {"nonprod", "prod"}:
            raise ApprovalError("--environment must be exactly 'nonprod' or 'prod'")
        for label, value in (
            ("--application-id", self.application_id),
            ("--agent-id", self.agent_id),
            ("--tenant-id", self.tenant_id),
            ("--cost-centre", self.cost_centre),
        ):
            if not value or value.strip() != value:
                raise ApprovalError(
                    f"{label} must be a non-empty value with no surrounding whitespace"
                )
        if not self.expected_tool_ids:
            raise ApprovalError("--expected-tool-id must be supplied at least once")
        if len(set(self.expected_tool_ids)) != len(self.expected_tool_ids):
            raise ApprovalError("--expected-tool-id values must be unique")
        for tool_id in self.expected_tool_ids:
            if not TOOL_ID_PATTERN.fullmatch(tool_id):
                raise ApprovalError(f"--expected-tool-id '{tool_id}' is not kebab-case")
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.git_head):
            raise ApprovalError("--git-head must be a 7-40 character lower-case Git SHA")
        if not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ApprovalError(
                f"--timeout-seconds must be between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS}"
            )
        if not 1 <= self.poll_interval_seconds <= 60:
            raise ApprovalError("--poll-interval-seconds must be between 1 and 60")
        try:
            approval_scratch_path(self.evidence_file)
        except SpikeError as error:
            raise ApprovalError(str(error)) from error

    @property
    def stack_name(self) -> str:
        return stack_name_for(self.environment)

    @property
    def registry_name(self) -> str:
        return registry_name_for(self.environment)

    @property
    def sorted_tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.expected_tool_ids))

    @property
    def tags(self) -> dict[str, str]:
        return {
            "application-id": self.application_id,
            "agent-id": self.agent_id,
            "tenant-id": self.tenant_id,
            "cost-centre": self.cost_centre,
            "environment": self.environment,
        }


# --------------------------------------------------------------------------- #
# Evidence (raw, credential-free, scratch-confined)
# --------------------------------------------------------------------------- #


def _redact_if_sensitive(key: str, value: Any) -> Any:
    """Redact a status detail whose value trips the credential/JWT detector.

    Evidence must stay credential-free even on the failure path, where an SDK
    error string is outside our control. Detection reuses the shared
    ``assert_no_secrets`` guard; a trip degrades to a fixed marker instead of
    crashing the failure handler.
    """
    try:
        assert_no_secrets({key: value})
    except SpikeError:
        return "[REDACTED]"
    return value


@dataclass
class Evidence:
    config: ApprovalConfig
    document: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.document:
            self.document = {
                "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                "status": "running",
                "action": "",
                "accountId": self.config.account_id,
                "region": self.config.region,
                "environment": self.config.environment,
                "stackName": self.config.stack_name,
                "registryName": self.config.registry_name,
                "expectedToolIds": list(self.config.sorted_tool_ids),
                "gitHead": self.config.git_head,
                "events": [],
            }
        self.flush()

    def add(self, event: str, **details: Any) -> None:
        entry = {
            "event": event,
            "at": datetime.now().astimezone().isoformat(),
            **details,
        }
        approval_no_secrets(entry)
        events = self.document["events"]
        assert isinstance(events, list)
        events.append(entry)
        self.flush()

    def status(self, status: str, **details: Any) -> None:
        self.document["status"] = status
        for key, value in details.items():
            self.document[key] = _redact_if_sensitive(key, value)
        self.flush()

    def flush(self) -> None:
        approval_json_write(self.config.evidence_file, self.document)


# --------------------------------------------------------------------------- #
# Fail-closed response readers
# --------------------------------------------------------------------------- #


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApprovalError(f"Unrecognised SDK response: {label} is not an object")
    return value


def require_field(response: Mapping[str, Any], key: str, label: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value:
        raise ApprovalError(f"Unrecognised SDK response: {label} lacks a '{key}' string")
    return value


def require_sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ApprovalError(f"Unrecognised SDK response: {label} is not a list")
    return list(value)


def require_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ApprovalError(f"Refusing malformed {label}: {value!r}")
    return value


def resource_identifier(physical_id: str, label: str) -> str:
    """Derive a control-plane id from a CloudFormation physical resource id.

    GA physical ids are either the bare service id or an ARN/composite whose
    last path segment is the id. Anything else fails closed.
    """
    if not isinstance(physical_id, str) or not physical_id:
        raise ApprovalError(f"Unrecognised SDK response: {label} has no physical id")
    candidate = physical_id
    for separator in ("|", "/", ":"):
        if separator in candidate:
            candidate = candidate.rsplit(separator, 1)[-1]
    return require_identifier(candidate, label)


# --------------------------------------------------------------------------- #
# AWS facade (constructed only for live execution)
# --------------------------------------------------------------------------- #


class ApprovalAws:
    """The only layer that talks to AWS. Tests substitute a fake."""

    def __init__(self, region: str) -> None:
        import boto3  # Imported lazily so offline tests need no SDK.

        session = boto3.Session(region_name=region)
        self.sts = session.client("sts")
        self.cfn = session.client("cloudformation")
        self.control = session.client("agent-registry-control")
        self.discovery = session.client("agent-registry")

    def caller_account(self) -> str:
        return str(require_mapping(self.sts.get_caller_identity(), "GetCallerIdentity")["Account"])

    def describe_stack(self, stack_name: str) -> Mapping[str, Any]:
        try:
            response = self.cfn.describe_stacks(StackName=stack_name)
        except Exception as error:  # SDK exception classes are service-generated.
            raise ApprovalError(
                f"Cannot describe stack {stack_name}: {aws_error_code(error)}"
            ) from error
        stacks = require_sequence(
            require_mapping(response, "DescribeStacks").get("Stacks"), "Stacks"
        )
        if len(stacks) != 1:
            raise ApprovalError(f"Expected exactly one stack named {stack_name}")
        return require_mapping(stacks[0], "Stack")

    def stack_resources(self, stack_name: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"StackName": stack_name}
            if token:
                kwargs["NextToken"] = token
            response = require_mapping(
                self.cfn.list_stack_resources(**kwargs), "ListStackResources"
            )
            for item in require_sequence(
                response.get("StackResourceSummaries"), "StackResourceSummaries"
            ):
                items.append(require_mapping(item, "StackResourceSummary"))
            token = response.get("NextToken")
            if not token:
                return items

    def stack_template(self, stack_name: str) -> Mapping[str, Any]:
        """Return the processed template CloudFormation used for the live stack."""
        try:
            response = require_mapping(
                self.cfn.get_template(StackName=stack_name, TemplateStage="Processed"),
                "GetTemplate",
            )
        except Exception as error:  # SDK exception classes are service-generated.
            raise ApprovalError(
                f"Cannot read processed template for {stack_name}: {aws_error_code(error)}"
            ) from error
        return require_mapping(response.get("TemplateBody"), "TemplateBody")

    def get_registry(self, registry_id: str) -> Mapping[str, Any]:
        return require_mapping(self.control.get_registry(registryId=registry_id), "GetRegistry")

    def get_record(self, registry_id: str, record_id: str) -> Mapping[str, Any]:
        return require_mapping(
            self.control.get_registry_record(registryId=registry_id, recordId=record_id),
            "GetRegistryRecord",
        )

    def submit_record(self, registry_id: str, record_id: str) -> Mapping[str, Any]:
        return require_mapping(
            self.control.submit_registry_record_for_approval(
                registryId=registry_id, recordId=record_id
            ),
            "SubmitRegistryRecordForApproval",
        )

    def tags(self, arn: str) -> dict[str, str]:
        response = require_mapping(
            self.control.list_tags_for_resource(resourceArn=arn), "ListTagsForResource"
        )
        tags = require_mapping(response.get("tags"), "tags")
        return {str(key): str(value) for key, value in tags.items()}

    def discoverable_records(self, registry_id: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = require_mapping(
                self.discovery.list_discoverable_registry_records(**kwargs),
                "ListDiscoverableRegistryRecords",
            )
            for item in require_sequence(
                response.get("registryRecords"), "registryRecords"
            ):
                items.append(require_mapping(item, "registryRecord"))
            token = response.get("nextToken")
            if not token:
                return items


# --------------------------------------------------------------------------- #
# Verification helpers
# --------------------------------------------------------------------------- #


def user_tags(actual: Mapping[str, str]) -> dict[str, str]:
    """Drop CloudFormation system tags; every other tag stays in scope."""
    return {
        str(key): str(value)
        for key, value in actual.items()
        if not str(key).startswith(SYSTEM_TAG_PREFIX)
    }


def assert_exact_tags(actual: Mapping[str, str], config: ApprovalConfig, label: str) -> None:
    observed = user_tags(actual)
    if observed != config.tags:
        raise ApprovalError(
            f"Refusing {label}: user tags must be exactly {config.tags}, got {observed}"
        )


def assert_identity(aws: Any, config: ApprovalConfig, evidence: Evidence) -> None:
    actual = aws.caller_account()
    if actual != config.account_id:
        raise ApprovalError(f"Expected AWS account {config.account_id}, got {actual}")
    evidence.add("caller_identity_verified", accountId=actual)


def assert_stack_ready(aws: Any, config: ApprovalConfig, evidence: Evidence) -> Mapping[str, Any]:
    stack = aws.describe_stack(config.stack_name)
    name = require_field(stack, "StackName", "Stack")
    if name != config.stack_name:
        raise ApprovalError(f"Expected stack {config.stack_name}, got {name}")
    status = require_field(stack, "StackStatus", "Stack")
    if status not in ACCEPTED_STACK_STATUSES:
        raise ApprovalError(
            f"Stack {config.stack_name} is {status}, not one of "
            f"{sorted(ACCEPTED_STACK_STATUSES)}"
        )
    evidence.add("stack_verified", stackName=name, stackStatus=status)
    return stack


def collect_stack_registry_resources(
    aws: Any, config: ApprovalConfig, evidence: Evidence
) -> tuple[str, tuple[str, ...]]:
    """Return (registryId, recordIds) from the pipeline-owned stack."""
    registries: list[str] = []
    records: list[str] = []
    for summary in aws.stack_resources(config.stack_name):
        resource_type = str(summary.get("ResourceType", ""))
        if resource_type not in {REGISTRY_TYPE, RECORD_TYPE}:
            continue
        status = require_field(summary, "ResourceStatus", "StackResourceSummary")
        logical_id = require_field(summary, "LogicalResourceId", "StackResourceSummary")
        if status not in ACCEPTED_RESOURCE_STATUSES:
            raise ApprovalError(
                f"Stack resource {logical_id} ({resource_type}) is {status}, "
                f"not one of {sorted(ACCEPTED_RESOURCE_STATUSES)}"
            )
        identifier = resource_identifier(
            str(summary.get("PhysicalResourceId", "")), f"{resource_type} physical id"
        )
        (registries if resource_type == REGISTRY_TYPE else records).append(identifier)
    if len(registries) != 1:
        raise ApprovalError(
            f"Expected exactly one {REGISTRY_TYPE} stack resource, found {len(registries)}"
        )
    if len(set(records)) != len(records):
        raise ApprovalError("Stack reported duplicate RegistryRecord physical ids")
    expected_count = len(config.expected_tool_ids)
    if len(records) != expected_count:
        raise ApprovalError(
            f"Expected exactly {expected_count} {RECORD_TYPE} stack resources, "
            f"found {len(records)}"
        )
    evidence.add(
        "stack_resources_verified",
        registryResources=1,
        recordResources=len(records),
    )
    return registries[0], tuple(records)


def assert_registry_contract(
    aws: Any, config: ApprovalConfig, registry_id: str, evidence: Evidence
) -> str:
    registry = aws.get_registry(registry_id)
    name = require_field(registry, "name", "GetRegistry")
    if name != config.registry_name:
        raise ApprovalError(f"Expected registry {config.registry_name}, got {name}")
    status = require_field(registry, "status", "GetRegistry")
    if status != REQUIRED_REGISTRY_STATUS:
        raise ApprovalError(f"Registry {name} is {status}, not {REQUIRED_REGISTRY_STATUS}")
    discovery = require_mapping(
        registry.get("discoveryConfiguration"), "discoveryConfiguration"
    )
    authorizer = require_field(discovery, "authorizerType", "discoveryConfiguration")
    if authorizer != REQUIRED_AUTHORIZER_TYPE:
        raise ApprovalError(
            f"Registry {name} authorizer is {authorizer}, not {REQUIRED_AUTHORIZER_TYPE}"
        )
    approval = require_mapping(registry.get("approvalConfiguration"), "approvalConfiguration")
    rules = require_sequence(approval.get("autoApprovalRules"), "autoApprovalRules")
    if [str(rule) for rule in rules] != REQUIRED_AUTO_APPROVAL_RULES:
        raise ApprovalError(
            f"Registry {name} auto-approval rules are {rules}, "
            f"not {REQUIRED_AUTO_APPROVAL_RULES}"
        )
    arn = require_field(registry, "registryArn", "GetRegistry")
    assert_exact_tags(aws.tags(arn), config, f"registry {name}")
    evidence.add(
        "registry_verified",
        registryId=registry_id,
        registryArn=arn,
        name=name,
        status=status,
        authorizerType=authorizer,
        autoApprovalRules=[str(rule) for rule in rules],
    )
    return arn


def parse_governance_document(record: Mapping[str, Any], label: str) -> dict[str, Any]:
    if str(record.get("recordType")) != "CUSTOM":
        raise ApprovalError(f"Refusing {label}: recordType is not CUSTOM")
    descriptors = require_mapping(record.get("descriptors"), f"{label} descriptors")
    custom = require_mapping(descriptors.get("custom"), f"{label} custom descriptor")
    data = custom.get("data")
    if not isinstance(data, str) or not data:
        raise ApprovalError(f"Refusing {label}: custom descriptor data is missing")
    try:
        document = json.loads(data)
    except json.JSONDecodeError as error:
        raise ApprovalError(f"Refusing {label}: custom descriptor is not JSON") from error
    if not isinstance(document, dict):
        raise ApprovalError(f"Refusing {label}: custom descriptor is not a JSON object")
    return document


def assert_governance_contract(document: Mapping[str, Any], tool_id: str) -> None:
    label = f"record {tool_id}"
    schema = document.get("schemaVersion")
    if schema != GOVERNANCE_SCHEMA_VERSION:
        raise ApprovalError(
            f"Refusing {label}: schemaVersion is {schema!r}, not "
            f"{GOVERNANCE_SCHEMA_VERSION!r}"
        )
    if document.get("toolId") != tool_id:
        raise ApprovalError(
            f"Refusing {label}: descriptor toolId is {document.get('toolId')!r}"
        )
    if document.get("desiredApprovalStatus") != REQUIRED_DESIRED_STATUS:
        raise ApprovalError(
            f"Refusing {label}: desiredApprovalStatus is "
            f"{document.get('desiredApprovalStatus')!r}, not {REQUIRED_DESIRED_STATUS!r}"
        )
    authorization = require_mapping(document.get("authorization"), f"{label} authorization")
    if authorization.get("defaultDecision") != REQUIRED_DEFAULT_DECISION:
        raise ApprovalError(
            f"Refusing {label}: defaultDecision is "
            f"{authorization.get('defaultDecision')!r}, not {REQUIRED_DEFAULT_DECISION!r}"
        )


def template_tags(value: Any, label: str) -> dict[str, str]:
    """Parse a CloudFormation ``Tags`` list without tolerating duplicates."""
    result: dict[str, str] = {}
    for item in require_sequence(value, f"{label} Tags"):
        tag = require_mapping(item, f"{label} tag")
        key = require_field(tag, "Key", f"{label} tag")
        tag_value = require_field(tag, "Value", f"{label} tag")
        if key in result:
            raise ApprovalError(f"Refusing {label}: duplicate template tag {key!r}")
        result[key] = tag_value
    return result


def template_governance_documents(
    aws: Any,
    config: ApprovalConfig,
    evidence: Evidence,
) -> dict[str, dict[str, Any]]:
    """Load the exact governance documents from the deployed stack template.

    The processed template is the pipeline-owned source of truth for approval.
    A live record that merely preserves the four routing fields while changing
    its target, Cedar policy, MCP schema, or ownership must never pass preflight.
    """
    template = aws.stack_template(config.stack_name)
    resources = require_mapping(template.get("Resources"), "Template Resources")
    registry_properties: list[Mapping[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    descriptor_hashes: dict[str, str] = {}

    for logical_id, raw_resource in resources.items():
        resource = require_mapping(raw_resource, f"template resource {logical_id}")
        resource_type = require_field(resource, "Type", f"template resource {logical_id}")
        if resource_type not in {REGISTRY_TYPE, RECORD_TYPE}:
            continue
        properties = require_mapping(
            resource.get("Properties"), f"template resource {logical_id} Properties"
        )
        if resource_type == REGISTRY_TYPE:
            registry_properties.append(properties)
            continue

        name = require_field(properties, "Name", f"template record {logical_id}")
        if name in documents:
            raise ApprovalError(f"Refusing processed template: duplicate record name {name!r}")
        if properties.get("DisplayName") != name:
            raise ApprovalError(f"Refusing template record {name}: DisplayName must equal Name")
        if properties.get("RecordType") != "CUSTOM":
            raise ApprovalError(f"Refusing template record {name}: RecordType is not CUSTOM")
        if properties.get("RecordVersion") != "1.0.0":
            raise ApprovalError(
                f"Refusing template record {name}: RecordVersion is not '1.0.0'"
            )
        descriptors = require_mapping(
            properties.get("Descriptors"), f"template record {name} Descriptors"
        )
        custom = require_mapping(
            descriptors.get("Custom"), f"template record {name} Custom descriptor"
        )
        data = custom.get("Data")
        if not isinstance(data, str) or not data:
            raise ApprovalError(f"Refusing template record {name}: descriptor Data is missing")
        try:
            document = json.loads(data)
        except json.JSONDecodeError as error:
            raise ApprovalError(
                f"Refusing template record {name}: descriptor Data is not JSON"
            ) from error
        if not isinstance(document, dict):
            raise ApprovalError(
                f"Refusing template record {name}: descriptor Data is not a JSON object"
            )
        assert_governance_contract(document, name)
        if properties.get("Description") != document.get("description"):
            raise ApprovalError(
                f"Refusing template record {name}: Description differs from governance document"
            )
        assert_exact_tags(
            template_tags(properties.get("Tags"), f"template record {name}"),
            config,
            f"template registry record {name}",
        )
        documents[name] = document
        descriptor_hashes[name] = hashlib.sha256(data.encode("utf-8")).hexdigest()

    if len(registry_properties) != 1:
        raise ApprovalError(
            f"Expected exactly one {REGISTRY_TYPE} in processed template, "
            f"found {len(registry_properties)}"
        )
    registry = registry_properties[0]
    if registry.get("Name") != config.registry_name:
        raise ApprovalError("Processed template Registry name does not match the environment")
    if registry.get("AuthorizerType") != REQUIRED_AUTHORIZER_TYPE:
        raise ApprovalError("Processed template Registry authorizer is not AWS_IAM")
    if registry.get("ApprovalConfiguration") != {
        "AutoApprovalRules": REQUIRED_AUTO_APPROVAL_RULES
    }:
        raise ApprovalError("Processed template Registry approval configuration changed")
    assert_exact_tags(
        template_tags(registry.get("Tags"), "template Registry"),
        config,
        "template Registry",
    )

    expected = set(config.expected_tool_ids)
    if set(documents) != expected:
        raise ApprovalError(
            "Processed template RegistryRecord names differ from --expected-tool-id: "
            f"expected {sorted(expected)}, got {sorted(documents)}"
        )
    evidence.add(
        "processed_stack_template_verified",
        recordCount=len(documents),
        descriptorSha256=descriptor_hashes,
    )
    return documents


def record_status(record: Mapping[str, Any], label: str) -> str:
    status = require_field(record, "status", label)
    if status not in KNOWN_RECORD_STATUSES:
        raise ApprovalError(f"Refusing {label}: unknown record status {status!r}")
    return status


def preflight_records(
    aws: Any,
    config: ApprovalConfig,
    registry_id: str,
    record_ids: Sequence[str],
    expected_documents: Mapping[str, Mapping[str, Any]],
    evidence: Evidence,
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    """Read EVERY record before any mutation. Submits nothing.

    Returns a tool-id-sorted tuple of (toolId, recordId, governanceDocument).
    """
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    expected = set(config.expected_tool_ids)
    for record_id in record_ids:
        record = aws.get_record(registry_id, record_id)
        name = require_field(record, "name", f"record {record_id}")
        if name not in expected:
            raise ApprovalError(
                f"Refusing unexpected registry record {name!r} (id {record_id}); "
                f"expected only {sorted(expected)}"
            )
        if name in seen:
            raise ApprovalError(f"Refusing duplicate registry record name {name!r}")
        status = record_status(record, f"record {name}")
        if status != DRAFT_STATUS:
            raise ApprovalError(
                f"Refusing record {name}: status is {status}, not {DRAFT_STATUS}; "
                "no record was submitted"
            )
        document = parse_governance_document(record, f"record {name}")
        assert_governance_contract(document, name)
        expected_document = expected_documents.get(name)
        if expected_document is None or document != dict(expected_document):
            raise ApprovalError(
                f"Refusing record {name}: governance descriptor differs from processed template"
            )
        arn = require_field(record, "recordArn", f"record {name}")
        assert_exact_tags(aws.tags(arn), config, f"registry record {name}")
        seen[name] = (record_id, document)
        evidence.add(
            "record_preflight_verified",
            toolId=name,
            recordId=record_id,
            recordArn=arn,
            status=status,
        )
    missing = sorted(expected - set(seen))
    if missing:
        raise ApprovalError(f"Missing expected registry records {missing}; nothing submitted")
    evidence.add("preflight_all_records_draft", recordCount=len(seen))
    return tuple(
        (tool_id, seen[tool_id][0], seen[tool_id][1]) for tool_id in sorted(seen)
    )


def submit_records(
    aws: Any,
    config: ApprovalConfig,
    registry_id: str,
    preflighted: Sequence[tuple[str, str, dict[str, Any]]],
    evidence: Evidence,
) -> None:
    """Submit each preflighted record exactly once, in tool-id order."""
    for tool_id, record_id, _document in preflighted:
        response = aws.submit_record(registry_id, record_id)
        status = response.get("status")
        if status is not None:
            if not isinstance(status, str) or status not in KNOWN_SUBMIT_STATUSES:
                raise ApprovalError(
                    f"Unrecognised SubmitRegistryRecordForApproval status {status!r} "
                    f"for {tool_id}"
                )
        evidence.add(
            "record_submitted",
            toolId=tool_id,
            recordId=record_id,
            resultingStatus=str(status) if status is not None else "",
            awsRequestId=request_id(response),
        )


def poll_record_approved(
    aws: Any,
    config: ApprovalConfig,
    registry_id: str,
    tool_id: str,
    record_id: str,
    evidence: Evidence,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Mapping[str, Any]:
    deadline = monotonic() + config.timeout_seconds
    last_status = "UNKNOWN"
    while monotonic() < deadline:
        record = aws.get_record(registry_id, record_id)
        last_status = record_status(record, f"record {tool_id}")
        if last_status == APPROVED_STATUS:
            evidence.add("record_approved", toolId=tool_id, recordId=record_id)
            return record
        if last_status in FAILED_RECORD_STATUSES:
            raise ApprovalError(f"Record {tool_id} reached terminal status {last_status}")
        sleeper(config.poll_interval_seconds)
    raise ApprovalError(
        f"Record {tool_id} did not reach {APPROVED_STATUS} within "
        f"{config.timeout_seconds}s; last status {last_status}"
    )


def assert_descriptor_unchanged(
    record: Mapping[str, Any], tool_id: str, expected: Mapping[str, Any]
) -> None:
    actual = parse_governance_document(record, f"record {tool_id}")
    if actual != dict(expected):
        raise ApprovalError(f"Governance descriptor for {tool_id} changed during approval")
    assert_governance_contract(actual, tool_id)


def poll_discovery(
    aws: Any,
    config: ApprovalConfig,
    registry_id: str,
    expected_record_ids: Mapping[str, str],
    evidence: Evidence,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for all approved records to converge on the discovery data plane."""
    expected_ids = set(expected_record_ids.values())
    deadline = monotonic() + config.timeout_seconds
    while True:
        discovered: dict[str, str] = {}
        for item in aws.discoverable_records(registry_id):
            record_id = require_field(item, "recordId", "discoverable record")
            if record_id in discovered:
                raise ApprovalError(f"Data plane returned duplicate record id {record_id}")
            discovered[record_id] = record_status(item, f"discoverable record {record_id}")

        unexpected = sorted(set(discovered) - expected_ids)
        if unexpected:
            raise ApprovalError(
                f"Data-plane discovery mismatch: unexpected record ids {unexpected}"
            )
        not_approved = sorted(
            record_id for record_id, status in discovered.items() if status != APPROVED_STATUS
        )
        if not_approved:
            raise ApprovalError(f"Discoverable records are not APPROVED: {not_approved}")
        missing = sorted(expected_ids - set(discovered))
        if not missing:
            evidence.add(
                "data_plane_discovery_verified",
                recordCount=len(discovered),
                toolIds=sorted(expected_record_ids),
            )
            return
        now = monotonic()
        if now >= deadline:
            raise ApprovalError(
                "Data-plane discovery mismatch: did not converge within "
                f"{config.timeout_seconds}s; missing {missing}"
            )
        sleeper(min(float(config.poll_interval_seconds), deadline - now))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def verify_deployment(
    aws: Any, config: ApprovalConfig, evidence: Evidence
) -> tuple[str, tuple[tuple[str, str, dict[str, Any]], ...]]:
    """Read-only verification of the pipeline-owned producer. Mutates nothing."""
    assert_identity(aws, config, evidence)
    assert_stack_ready(aws, config, evidence)
    expected_documents = template_governance_documents(aws, config, evidence)
    registry_id, record_ids = collect_stack_registry_resources(aws, config, evidence)
    assert_registry_contract(aws, config, registry_id, evidence)
    preflighted = preflight_records(
        aws, config, registry_id, record_ids, expected_documents, evidence
    )
    return registry_id, preflighted


def approve(
    aws: Any,
    config: ApprovalConfig,
    evidence: Evidence,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    registry_id, preflighted = verify_deployment(aws, config, evidence)
    submit_records(aws, config, registry_id, preflighted, evidence)
    approved_ids: dict[str, str] = {}
    for tool_id, record_id, document in preflighted:
        record = poll_record_approved(
            aws,
            config,
            registry_id,
            tool_id,
            record_id,
            evidence,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        assert_descriptor_unchanged(record, tool_id, document)
        approved_ids[tool_id] = record_id
    poll_discovery(
        aws,
        config,
        registry_id,
        approved_ids,
        evidence,
        sleeper=sleeper,
        monotonic=monotonic,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("verify", "approve"),
        help="'verify' is read-only; 'approve' submits every DRAFT record once.",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True, choices=("nonprod", "prod"))
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--cost-centre", required=True)
    parser.add_argument(
        "--expected-tool-id",
        required=True,
        action="append",
        dest="expected_tool_ids",
        help="Repeat once per catalogued tool id expected in the registry.",
    )
    parser.add_argument("--git-head", required=True)
    parser.add_argument("--evidence-file")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-interval-seconds", type=int, default=5)
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> ApprovalConfig:
    default = (
        scratch_root()
        / "registry-approval"
        / f"{args.environment}-{args.account_id}"
        / "evidence.json"
    )
    return ApprovalConfig(
        account_id=args.account_id,
        region=args.region,
        environment=args.environment,
        application_id=args.application_id,
        agent_id=args.agent_id,
        tenant_id=args.tenant_id,
        cost_centre=args.cost_centre,
        expected_tool_ids=tuple(args.expected_tool_ids),
        git_head=args.git_head,
        evidence_file=Path(args.evidence_file) if args.evidence_file else default,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    aws_factory: Callable[[str], Any] = ApprovalAws,
) -> int:
    args = parse_args(argv)
    try:
        config = config_from_args(args)
    except SpikeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        evidence = Evidence(config)
        evidence.status("running", action=args.action)
    except Exception as error:
        # An unusable evidence path is an input problem, not a contract failure:
        # exit 2 and never contact AWS, rather than raising a raw traceback.
        safe_error = _redact_if_sensitive("error", str(error)[:1000])
        print(f"ERROR: cannot initialise evidence: {safe_error}", file=sys.stderr)
        return 2
    try:
        aws = aws_factory(config.region)
        if args.action == "verify":
            verify_deployment(aws, config, evidence)
            evidence.status("verify-passed", action=args.action)
        else:
            approve(aws, config, evidence)
            evidence.status("approve-passed", action=args.action)
        print(
            json.dumps(
                {
                    "status": evidence.document["status"],
                    "evidence": str(config.evidence_file),
                }
            )
        )
        return 0
    except Exception as error:
        safe_error = _redact_if_sensitive("error", str(error)[:1000])
        evidence.status(
            "failed",
            action=args.action,
            errorType=error.__class__.__name__,
            error=safe_error,
        )
        print(f"ERROR: {safe_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
