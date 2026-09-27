#!/usr/bin/env python3
"""Cleanup-first GA AWS Agent Registry compatibility probe.

This probe validates the native CloudFormation resources and the GA
``agent-registry-control`` / ``agent-registry`` APIs before the blueprint's
Platform or Workload stacks depend on them. It creates only exact-prefix,
tag-owned resources, injects one failed CloudFormation update to prove rollback,
and removes every resource in ``finally``.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

REGISTRY_TYPE = "AWS::AgentRegistry::Registry"
RECORD_TYPE = "AWS::AgentRegistry::RegistryRecord"
TERMINAL_STACK_FAILURES = {
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_FAILED",
    "DELETE_FAILED",
}
EXPECTED_ROLLBACK_STATUS = "UPDATE_ROLLBACK_COMPLETE"
RECORD_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "REJECTED", "DEPRECATED"}
RESOURCE_ABSENT_CODES = {"ResourceNotFoundException", "ValidationError"}
PREFIX_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{1,30}[a-z0-9])$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:secret|password|authorization|access[_-]?token|refresh[_-]?token|session[_-]?token)",
    re.IGNORECASE,
)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE)


class SpikeError(RuntimeError):
    """The compatibility contract failed."""


@dataclass(frozen=True)
class SpikeConfig:
    account_id: str
    region: str
    prefix: str
    git_head: str
    state_file: Path
    evidence_file: Path
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{12}", self.account_id):
            raise SpikeError("--account-id must be exactly 12 digits")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", self.region):
            raise SpikeError("--region is not a valid AWS Region identifier")
        if not PREFIX_PATTERN.fullmatch(self.prefix):
            raise SpikeError(
                "--prefix must start with a lower-case letter and contain only "
                "lower-case letters, digits, and hyphens (3-32 chars)"
            )
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.git_head):
            raise SpikeError("--git-head must be a 7-40 character lower-case Git SHA")
        require_scratch_path(self.state_file)
        require_scratch_path(self.evidence_file)

    @property
    def stack_name(self) -> str:
        return self.prefix

    @property
    def registry_name(self) -> str:
        return self.prefix

    @property
    def record_name(self) -> str:
        return f"{self.prefix}-tool"

    @property
    def invalid_record_name(self) -> str:
        return f"{self.prefix}-invalid"

    @property
    def tags(self) -> dict[str, str]:
        return {
            "agenticai:test-run": self.prefix,
            "application-id": "agent-registry-spike",
            "agent-id": "registry-probe",
            "tenant-id": "compatibility",
            "cost-centre": "engineering",
            "environment": "test",
        }


def scratch_root() -> Path:
    raw = os.environ.get("KIROCREW_SCRATCH")
    if not raw:
        raise SpikeError("KIROCREW_SCRATCH is required")
    return Path(raw).expanduser().resolve()


def require_scratch_path(path: Path) -> Path:
    root = scratch_root()
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SpikeError(f"Refusing path outside KIROCREW_SCRATCH: {resolved}") from error
    return resolved


def aws_error_code(error: BaseException) -> str:
    response = getattr(error, "response", {})
    if isinstance(response, Mapping):
        details = response.get("Error", {})
        if isinstance(details, Mapping):
            return str(details.get("Code", ""))
    return error.__class__.__name__


def request_id(response: Mapping[str, Any]) -> str:
    metadata = response.get("ResponseMetadata", {})
    if isinstance(metadata, Mapping):
        return str(metadata.get("RequestId", ""))
    return ""


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def assert_no_secrets(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if SECRET_KEY_PATTERN.search(str(key)):
                raise SpikeError(f"Refusing sensitive evidence key at {path}.{key}")
            assert_no_secrets(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if JWT_PATTERN.search(value):
            raise SpikeError(f"Refusing JWT-like value at {path}")
        if AWS_ACCESS_KEY_PATTERN.search(value):
            raise SpikeError(f"Refusing AWS access-key-like value at {path}")
        if BEARER_PATTERN.search(value):
            raise SpikeError(f"Refusing bearer-token-like value at {path}")


def atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    destination = require_scratch_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe = json_safe(value)
    assert_no_secrets(safe)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def governance_document(config: SpikeConfig) -> dict[str, Any]:
    return {
        "schemaVersion": "agenticai.tool-governance/1.0",
        "toolId": "tool-echo",
        "target": {
            "type": "lambda",
            "arn": (
                f"arn:aws:lambda:{config.region}:{config.account_id}:"
                "function:agenticai-registry-spike:PROD"
            ),
        },
        "mcp": {
            "toolName": "tool-echo",
            "description": "Fixed compatibility-probe tool descriptor.",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
        "authorization": {
            "defaultDecision": "DENY",
            "allowedSubjects": ["00000000-0000-4000-8000-000000000001"],
            "allowedGroups": ["agenticai-testers"],
            "combination": "SUBJECT_OR_GROUP",
        },
        "ownership": {"ownerTeam": "platform-ai", "costCentre": "engineering"},
    }


def cfn_tags(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


def build_template(config: SpikeConfig, *, inject_invalid_record: bool = False) -> dict[str, Any]:
    resources: dict[str, Any] = {
        "Registry": {
            "Type": REGISTRY_TYPE,
            "Properties": {
                "Name": config.registry_name,
                "Description": "Ephemeral GA Agent Registry compatibility probe.",
                "AuthorizerType": "AWS_IAM",
                "ApprovalConfiguration": {"AutoApprovalRules": ["APPROVE_ALL"]},
                "Tags": cfn_tags(config.tags),
            },
        },
        "GovernanceRecord": {
            "Type": RECORD_TYPE,
            "DependsOn": "Registry",
            "Properties": {
                "RegistryId": {"Fn::GetAtt": ["Registry", "RegistryId"]},
                "Name": config.record_name,
                "DisplayName": "AgenticAI registry compatibility tool",
                "Description": "Fixed custom governance record for GA compatibility validation.",
                "RecordType": "CUSTOM",
                "RecordVersion": "1.0.0",
                "Descriptors": {
                    "Custom": {
                        "Data": json.dumps(
                            governance_document(config),
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    }
                },
                "Tags": cfn_tags(config.tags),
            },
        },
    }
    if inject_invalid_record:
        resources["InvalidRecord"] = {
            "Type": RECORD_TYPE,
            "Properties": {
                "RegistryId": "0000000000000000",
                "Name": config.invalid_record_name,
                "Description": "Intentional nonexistent parent for rollback proof.",
                "RecordType": "CUSTOM",
                "RecordVersion": "1.0.0",
                "Descriptors": {
                    "Custom": {"Data": '{"intentional":"nonexistent-parent-registry"}'}
                },
                "Tags": cfn_tags(config.tags),
            },
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Ephemeral GA Agent Registry compatibility probe.",
        "Resources": resources,
        "Outputs": {
            "RegistryArn": {"Value": {"Fn::GetAtt": ["Registry", "RegistryArn"]}},
            "RegistryId": {"Value": {"Fn::GetAtt": ["Registry", "RegistryId"]}},
            "RecordArn": {"Value": {"Fn::GetAtt": ["GovernanceRecord", "RecordArn"]}},
            "RecordId": {"Value": {"Fn::GetAtt": ["GovernanceRecord", "RecordId"]}},
            "RecordStatus": {"Value": {"Fn::GetAtt": ["GovernanceRecord", "Status"]}},
        },
    }


class Evidence:
    TERMINAL_VERDICTS = {"passed", "failed"}

    def __init__(self, config: SpikeConfig, *, preserve_terminal: bool = False) -> None:
        self.config = config
        existing = self._load_existing()
        if existing is None:
            self.document: dict[str, Any] = {
                "schemaVersion": "agenticai.agent-registry-spike/1.0",
                "status": "running",
                "accountId": config.account_id,
                "region": config.region,
                "prefix": config.prefix,
                "gitHead": config.git_head,
                "events": [],
            }
        else:
            self.document = existing
            if not (
                preserve_terminal
                and self.document.get("status") in self.TERMINAL_VERDICTS
            ):
                self.document["status"] = "running"
        self.flush()

    def _load_existing(self) -> dict[str, Any] | None:
        path = require_scratch_path(self.config.evidence_file)
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SpikeError(f"Existing evidence file is unreadable: {path}") from error
        if not isinstance(loaded, dict):
            raise SpikeError("Existing evidence must be a JSON object")
        expected = {
            "schemaVersion": "agenticai.agent-registry-spike/1.0",
            "accountId": self.config.account_id,
            "region": self.config.region,
            "prefix": self.config.prefix,
            "gitHead": self.config.git_head,
        }
        mismatches = {
            key: {"expected": value, "actual": loaded.get(key)}
            for key, value in expected.items()
            if loaded.get(key) != value
        }
        if mismatches:
            raise SpikeError(f"Existing evidence identity differs: {mismatches}")
        if not isinstance(loaded.get("events"), list):
            raise SpikeError("Existing evidence events must be a list")
        assert_no_secrets(loaded)
        return loaded

    def add(self, event: str, **details: Any) -> None:
        entry = {"event": event, "at": datetime.now().astimezone().isoformat(), **details}
        assert_no_secrets(entry)
        events = self.document["events"]
        assert isinstance(events, list)
        events.append(entry)
        self.flush()

    def status(self, status: str, **details: Any) -> None:
        current = str(self.document.get("status", ""))
        if status == "cleanup-passed" and current in self.TERMINAL_VERDICTS:
            self.document["cleanupStatus"] = "passed"
        else:
            self.document["status"] = status
        self.document.update(details)
        self.flush()

    def flush(self) -> None:
        atomic_json_write(self.config.evidence_file, self.document)


class AwsFacade:
    """The only layer that calls AWS SDK clients."""

    def __init__(self, region: str) -> None:
        import boto3  # Imported only for live execution; pure tests need no SDK.

        session = boto3.Session(region_name=region)
        self.sts = session.client("sts")
        self.cfn = session.client("cloudformation")
        self.control = session.client("agent-registry-control")
        self.discovery = session.client("agent-registry")

    def caller_account(self) -> str:
        return str(self.sts.get_caller_identity()["Account"])

    def describe_type(self, type_name: str) -> Mapping[str, Any]:
        return self.cfn.describe_type(Type="RESOURCE", TypeName=type_name)

    def get_stack(self, stack_name: str) -> Mapping[str, Any] | None:
        try:
            response = self.cfn.describe_stacks(StackName=stack_name)
        except Exception as error:  # SDK exception types are service-generated.
            if aws_error_code(error) in RESOURCE_ABSENT_CODES:
                return None
            raise
        stacks = response.get("Stacks", [])
        return stacks[0] if stacks else None

    def stack_events(self, stack_name: str) -> list[Mapping[str, Any]]:
        try:
            return list(self.cfn.describe_stack_events(StackName=stack_name).get("StackEvents", []))
        except Exception as error:
            if aws_error_code(error) in RESOURCE_ABSENT_CODES:
                return []
            raise

    def create_stack(self, config: SpikeConfig) -> Mapping[str, Any]:
        return self.cfn.create_stack(
            StackName=config.stack_name,
            TemplateBody=json.dumps(build_template(config), separators=(",", ":")),
            OnFailure="DO_NOTHING",
            Tags=cfn_tags(config.tags),
        )

    def inject_failed_update(self, config: SpikeConfig) -> Mapping[str, Any]:
        return self.cfn.update_stack(
            StackName=config.stack_name,
            TemplateBody=json.dumps(
                build_template(config, inject_invalid_record=True),
                separators=(",", ":"),
            ),
            Tags=cfn_tags(config.tags),
        )

    def delete_stack(self, stack_name: str) -> None:
        self.cfn.delete_stack(StackName=stack_name)

    def list_registries(self) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = self.control.list_registries(**kwargs)
            items.extend(response.get("registries", []))
            token = response.get("nextToken")
            if not token:
                return items

    def get_registry(self, registry_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_registry(registryId=registry_id)
        except Exception as error:
            if aws_error_code(error) in RESOURCE_ABSENT_CODES:
                return None
            raise

    def list_records(self, registry_id: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = self.control.list_registry_records(**kwargs)
            items.extend(response.get("registryRecords", []))
            token = response.get("nextToken")
            if not token:
                return items

    def get_record(self, registry_id: str, record_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_registry_record(
                registryId=registry_id,
                recordId=record_id,
            )
        except Exception as error:
            if aws_error_code(error) in RESOURCE_ABSENT_CODES:
                return None
            raise

    def submit_record(self, registry_id: str, record_id: str) -> Mapping[str, Any]:
        return self.control.submit_registry_record_for_approval(
            registryId=registry_id,
            recordId=record_id,
        )

    def tags(self, arn: str) -> dict[str, str]:
        response = self.control.list_tags_for_resource(resourceArn=arn)
        return {str(key): str(value) for key, value in response.get("tags", {}).items()}

    def discoverable_records(self, registry_id: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = self.discovery.list_discoverable_registry_records(**kwargs)
            items.extend(response.get("registryRecords", []))
            token = response.get("nextToken")
            if not token:
                return items

    def _delete_with_retry(self, label: str, operation: Any) -> None:
        deadline = time.monotonic() + 300
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                operation()
                return
            except Exception as error:
                code = aws_error_code(error)
                if code in RESOURCE_ABSENT_CODES:
                    return
                if code not in {"ConflictException", "ThrottlingException"}:
                    raise
                last_error = error
                time.sleep(5)
        raise SpikeError(
            f"Timed out deleting {label} after transitional conflicts: {last_error}"
        )

    def delete_record(self, registry_id: str, record_id: str) -> None:
        self._delete_with_retry(
            f"registry record {record_id}",
            lambda: self.control.delete_registry_record(
                registryId=registry_id,
                recordId=record_id,
            ),
        )

    def delete_registry(self, registry_id: str) -> None:
        self._delete_with_retry(
            f"registry {registry_id}",
            lambda: self.control.delete_registry(registryId=registry_id),
        )


def tags_from_cfn(stack: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("Key")): str(item.get("Value"))
        for item in stack.get("Tags", [])
        if isinstance(item, Mapping)
    }


def assert_owned_tags(actual: Mapping[str, str], config: SpikeConfig, label: str) -> None:
    expected = config.tags
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise SpikeError(f"Refusing {label}: ownership tags differ: {mismatches}")


def stack_outputs(stack: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item["OutputKey"]): str(item["OutputValue"])
        for item in stack.get("Outputs", [])
        if isinstance(item, Mapping) and "OutputKey" in item and "OutputValue" in item
    }


class RegistryProbe:
    def __init__(self, config: SpikeConfig, aws: AwsFacade, evidence: Evidence) -> None:
        self.config = config
        self.aws = aws
        self.evidence = evidence
        self.state: dict[str, Any] = {}

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        atomic_json_write(self.config.state_file, self.state)

    def verify_identity(self) -> None:
        actual = self.aws.caller_account()
        if actual != self.config.account_id:
            raise SpikeError(
                f"Expected AWS account {self.config.account_id}, got {actual}"
            )
        self.evidence.add("caller_identity_verified", accountId=actual)

    def verify_resource_types(self) -> None:
        for type_name in (REGISTRY_TYPE, RECORD_TYPE):
            response = self.aws.describe_type(type_name)
            status = str(response.get("DeprecatedStatus", ""))
            if status != "LIVE":
                raise SpikeError(f"CloudFormation type {type_name} is {status or 'UNKNOWN'}, not LIVE")
            self.evidence.add(
                "cloudformation_type_verified",
                typeName=type_name,
                deprecatedStatus=status,
                defaultVersionId=str(response.get("DefaultVersionId", "")),
            )

    def find_registry(self) -> Mapping[str, Any] | None:
        matches = [
            item for item in self.aws.list_registries()
            if str(item.get("name", "")) == self.config.registry_name
        ]
        if len(matches) > 1:
            raise SpikeError(f"Multiple registries match {self.config.registry_name}")
        return matches[0] if matches else None

    def assert_absent(self) -> None:
        stack = self.aws.get_stack(self.config.stack_name)
        registry = self.find_registry()
        if stack is not None or registry is not None:
            raise SpikeError(
                "Preflight is not clean: run cleanup for the same exact prefix first"
            )
        self.evidence.add("preflight_inventory_clean", stacks=0, registries=0)

    def wait_stack(self, wanted: set[str], timeout: int | None = None) -> Mapping[str, Any]:
        deadline = time.monotonic() + (timeout or self.config.timeout_seconds)
        last_status = "ABSENT"
        while time.monotonic() < deadline:
            stack = self.aws.get_stack(self.config.stack_name)
            if stack is None:
                if "ABSENT" in wanted:
                    return {}
                last_status = "ABSENT"
                time.sleep(3)
                continue
            last_status = str(stack.get("StackStatus", "UNKNOWN"))
            if last_status in wanted:
                return stack
            if last_status == "DELETE_COMPLETE" and "ABSENT" in wanted:
                return {}
            fatal_statuses = (
                {"DELETE_FAILED"}
                if "ABSENT" in wanted
                else TERMINAL_STACK_FAILURES
            )
            if last_status in fatal_statuses and last_status not in wanted:
                failures = [
                    {
                        "logicalId": event.get("LogicalResourceId"),
                        "status": event.get("ResourceStatus"),
                        "reason": str(event.get("ResourceStatusReason", ""))[:500],
                    }
                    for event in self.aws.stack_events(self.config.stack_name)
                    if "FAILED" in str(event.get("ResourceStatus", ""))
                ][:5]
                raise SpikeError(f"Stack reached {last_status}: {failures}")
            time.sleep(5)
        raise SpikeError(f"Stack did not reach {sorted(wanted)}; last status {last_status}")

    def wait_registry_ready(self, registry_id: str) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            registry = self.aws.get_registry(registry_id)
            if registry is None:
                raise SpikeError("Registry disappeared while waiting for READY")
            status = str(registry.get("status", "UNKNOWN"))
            if status == "READY":
                return registry
            if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}:
                raise SpikeError(f"Registry reached {status}: {registry.get('statusReason', '')}")
            time.sleep(5)
        raise SpikeError("Registry did not reach READY")

    def wait_record_status(self, registry_id: str, record_id: str, wanted: set[str]) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        last_status = "ABSENT"
        while time.monotonic() < deadline:
            record = self.aws.get_record(registry_id, record_id)
            if record is None:
                last_status = "ABSENT"
                time.sleep(3)
                continue
            last_status = str(record.get("status", "UNKNOWN"))
            if last_status in wanted:
                return record
            if last_status in RECORD_FAILURES:
                raise SpikeError(f"Record reached {last_status}: {record.get('statusReason', '')}")
            time.sleep(5)
        raise SpikeError(f"Record did not reach {sorted(wanted)}; last status {last_status}")

    def assert_record_contract(self, record: Mapping[str, Any]) -> None:
        if str(record.get("recordType")) != "CUSTOM":
            raise SpikeError("Registry record type is not CUSTOM")
        custom = record.get("descriptors", {})
        custom = custom.get("custom", {}) if isinstance(custom, Mapping) else {}
        data = custom.get("data") if isinstance(custom, Mapping) else None
        if not isinstance(data, str):
            raise SpikeError("Registry record custom descriptor data is missing")
        try:
            actual = json.loads(data)
        except json.JSONDecodeError as error:
            raise SpikeError("Registry record custom descriptor is not JSON") from error
        expected = governance_document(self.config)
        if actual != expected:
            raise SpikeError("Registry custom governance descriptor changed during round-trip")
        if str(record.get("name")) != self.config.record_name:
            raise SpikeError("Registry record name differs from the requested name")
        self.evidence.add(
            "governance_descriptor_round_trip_verified",
            schemaVersion=str(actual.get("schemaVersion")),
            recordType="CUSTOM",
            defaultDecision="DENY",
        )

    def submit_draft_for_approval(
        self,
        registry_id: str,
        record_id: str,
        initial_status: str,
    ) -> None:
        if initial_status != "DRAFT":
            raise SpikeError(
                f"Record stabilized in {initial_status}, not DRAFT; "
                "DRAFT -> submit -> APPROVED was not exercised"
            )
        submitted = self.aws.submit_record(registry_id, record_id)
        self.evidence.add(
            "record_submitted",
            resultingStatus=str(submitted.get("status", "")),
            awsRequestId=request_id(submitted),
        )

    def deploy_and_verify(self) -> None:
        response = self.aws.create_stack(self.config)
        self.evidence.add("stack_create_requested", awsRequestId=request_id(response))
        stack = self.wait_stack({"CREATE_COMPLETE"})
        assert_owned_tags(tags_from_cfn(stack), self.config, "CloudFormation stack")
        outputs = stack_outputs(stack)
        required = {"RegistryArn", "RegistryId", "RecordArn", "RecordId", "RecordStatus"}
        missing = sorted(required - outputs.keys())
        if missing:
            raise SpikeError(f"Stack outputs missing {missing}")
        self.save_state(**outputs)

        registry = self.wait_registry_ready(outputs["RegistryId"])
        assert_owned_tags(self.aws.tags(outputs["RegistryArn"]), self.config, "registry")
        self.evidence.add(
            "registry_ready",
            registryId=outputs["RegistryId"],
            registryArn=outputs["RegistryArn"],
            status=str(registry.get("status")),
        )

        record = self.wait_record_status(
            outputs["RegistryId"], outputs["RecordId"], {"DRAFT", "APPROVED"}
        )
        initial_status = str(record.get("status"))
        self.assert_record_contract(record)
        assert_owned_tags(self.aws.tags(outputs["RecordArn"]), self.config, "registry record")
        self.evidence.add("record_stable", initialStatus=initial_status)
        self.submit_draft_for_approval(
            outputs["RegistryId"],
            outputs["RecordId"],
            initial_status,
        )

        record = self.wait_record_status(
            outputs["RegistryId"], outputs["RecordId"], {"APPROVED"}
        )
        self.assert_record_contract(record)
        self.evidence.add("record_approved", status="APPROVED")

        discoverable = self.aws.discoverable_records(outputs["RegistryId"])
        match = [item for item in discoverable if item.get("recordId") == outputs["RecordId"]]
        if len(match) != 1 or str(match[0].get("status")) != "APPROVED":
            raise SpikeError("Approved custom record is not uniquely discoverable")
        self.evidence.add("data_plane_discovery_verified", matchingRecords=1)

    def prove_rollback(self) -> None:
        rollback_template = build_template(self.config, inject_invalid_record=True)
        rollback_registry_id = str(
            rollback_template["Resources"]["InvalidRecord"]["Properties"]["RegistryId"]
        )
        if self.aws.get_registry(rollback_registry_id) is not None:
            raise SpikeError(
                "Rollback sentinel Registry unexpectedly exists; refusing the update"
            )
        self.evidence.add(
            "rollback_parent_absence_verified",
            registryId=rollback_registry_id,
        )

        outputs = {key: str(value) for key, value in self.state.items()}
        response = self.aws.inject_failed_update(self.config)
        self.evidence.add("rollback_injection_requested", awsRequestId=request_id(response))
        stack = self.wait_stack({EXPECTED_ROLLBACK_STATUS})
        if str(stack.get("StackStatus")) != EXPECTED_ROLLBACK_STATUS:
            raise SpikeError("Intentional update did not roll back")
        failures = [
            event for event in self.aws.stack_events(self.config.stack_name)
            if str(event.get("LogicalResourceId")) == "InvalidRecord"
            and "FAILED" in str(event.get("ResourceStatus", ""))
        ]
        if not failures:
            raise SpikeError("Rollback completed without the intended InvalidRecord failure")
        record = self.wait_record_status(
            outputs["RegistryId"], outputs["RecordId"], {"APPROVED"}
        )
        self.assert_record_contract(record)
        invalid = [
            item for item in self.aws.list_records(outputs["RegistryId"])
            if str(item.get("name", "")) == self.config.invalid_record_name
        ]
        if invalid:
            raise SpikeError("Intentional invalid record survived CloudFormation rollback")
        self.evidence.add(
            "rollback_verified",
            stackStatus=EXPECTED_ROLLBACK_STATUS,
            originalRecordStatus="APPROVED",
            invalidRecords=0,
            failedLogicalId="InvalidRecord",
        )

    def delete_residual_registry(self) -> None:
        registry = self.find_registry()
        if registry is None:
            return
        registry_id = str(registry.get("registryId", ""))
        registry_arn = str(registry.get("registryArn", ""))
        if not registry_id or not registry_arn:
            raise SpikeError("Residual registry is missing its id or ARN")
        assert_owned_tags(self.aws.tags(registry_arn), self.config, "residual registry")
        for record in self.aws.list_records(registry_id):
            if str(record.get("name", "")) not in {
                self.config.record_name,
                self.config.invalid_record_name,
            }:
                raise SpikeError(
                    f"Refusing residual registry cleanup: unexpected record {record.get('name')}"
                )
            record_id = str(record.get("recordId", ""))
            record_arn = str(record.get("recordArn", ""))
            assert_owned_tags(self.aws.tags(record_arn), self.config, "residual record")
            self.aws.delete_record(registry_id, record_id)
            self.wait_record_absent(registry_id, record_id)
        self.aws.delete_registry(registry_id)
        self.wait_registry_absent(registry_id)
        self.evidence.add("residual_registry_deleted")

    def cleanup(self) -> None:
        stack_delete_error: BaseException | None = None
        stack = self.aws.get_stack(self.config.stack_name)
        if stack is not None:
            assert_owned_tags(tags_from_cfn(stack), self.config, "CloudFormation stack cleanup")
            try:
                self.aws.delete_stack(self.config.stack_name)
                self.wait_stack({"ABSENT"})
                self.evidence.add("cloudformation_stack_deleted")
            except Exception as error:
                stack_delete_error = error
                self.evidence.add(
                    "cloudformation_stack_delete_deferred",
                    errorType=error.__class__.__name__,
                )

        self.delete_residual_registry()

        retry_stack = self.aws.get_stack(self.config.stack_name)
        if stack_delete_error is not None and retry_stack is not None:
            assert_owned_tags(
                tags_from_cfn(retry_stack),
                self.config,
                "CloudFormation stack cleanup retry",
            )
            try:
                self.aws.delete_stack(self.config.stack_name)
                self.wait_stack({"ABSENT"})
                self.evidence.add("cloudformation_stack_deleted_after_residual_sweep")
            except Exception as error:
                stack_delete_error = error

        remaining_stack = self.aws.get_stack(self.config.stack_name)
        remaining_registry = self.find_registry()
        if remaining_stack is not None or remaining_registry is not None:
            raise SpikeError(
                "Cleanup inventory is not empty: "
                f"stack={remaining_stack is not None}, "
                f"registry={remaining_registry is not None}"
            ) from stack_delete_error
        self.evidence.add("cleanup_inventory_verified", stacks=0, registries=0)

        if stack_delete_error is not None:
            raise SpikeError(
                "CloudFormation stack deletion required residual recovery; "
                "the compatibility run cannot pass"
            ) from stack_delete_error

    def wait_record_absent(self, registry_id: str, record_id: str) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            try:
                record = self.aws.get_record(registry_id, record_id)
            except Exception as error:
                if aws_error_code(error) != "ConflictException":
                    raise
                record = {}
            if record is None:
                return
            time.sleep(5)
        raise SpikeError(f"Registry record {record_id} did not disappear")

    def wait_registry_absent(self, registry_id: str) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            try:
                registry = self.aws.get_registry(registry_id)
            except Exception as error:
                if aws_error_code(error) != "ConflictException":
                    raise
                registry = {}
            if registry is None:
                return
            time.sleep(5)
        raise SpikeError(f"Registry {registry_id} did not disappear")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "all", "cleanup"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--git-head", required=True)
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> SpikeConfig:
    root = scratch_root() / "agent-registry-spike" / args.prefix
    return SpikeConfig(
        account_id=args.account_id,
        region=args.region,
        prefix=args.prefix,
        git_head=args.git_head,
        state_file=Path(args.state_file) if args.state_file else root / "state.json",
        evidence_file=(
            Path(args.evidence_file) if args.evidence_file else root / "evidence.json"
        ),
        timeout_seconds=args.timeout_seconds,
    )


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = config_from_args(args)
    evidence = Evidence(config, preserve_terminal=args.action == "cleanup")
    aws = AwsFacade(config.region)
    probe = RegistryProbe(config, aws, evidence)
    try:
        probe.verify_identity()
        probe.verify_resource_types()
        if args.action == "preflight":
            probe.assert_absent()
            evidence.status("preflight-passed")
        elif args.action == "cleanup":
            probe.cleanup()
            evidence.status("cleanup-passed")
        else:
            probe.assert_absent()
            try:
                probe.deploy_and_verify()
                probe.prove_rollback()
                evidence.status("passed")
            finally:
                probe.cleanup()
        print(json.dumps({"status": evidence.document["status"], "evidence": str(config.evidence_file)}))
        return 0
    except Exception as error:
        evidence.status(
            "failed",
            errorType=error.__class__.__name__,
            error=str(error)[:1000],
        )
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
