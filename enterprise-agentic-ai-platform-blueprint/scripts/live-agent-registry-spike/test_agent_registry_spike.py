"""Offline guards for the GA Agent Registry compatibility probe.

These tests protect template shape, fixed governance metadata, evidence
redaction, scratch containment, and exact ownership checks. They supplement —
never replace — the live AWS probe.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_registry_spike import (
    RECORD_TYPE,
    REGISTRY_TYPE,
    SpikeConfig,
    SpikeError,
    assert_no_secrets,
    assert_owned_tags,
    build_template,
    governance_document,
    require_scratch_path,
)

ACCOUNT_ID = "111111111111"
REGION = "eu-west-1"
GIT_HEAD = "0123456789abcdef0123456789abcdef01234567"


def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SpikeConfig:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    return SpikeConfig(
        account_id=ACCOUNT_ID,
        region=REGION,
        prefix="aiaf-ar-spike",
        git_head=GIT_HEAD,
        state_file=tmp_path / "state.json",
        evidence_file=tmp_path / "evidence.json",
    )


def tags_as_map(values: list[dict[str, str]]) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in values}


def test_template_uses_native_ga_resource_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    template = build_template(cfg)
    assert template["Resources"]["Registry"]["Type"] == REGISTRY_TYPE
    assert template["Resources"]["GovernanceRecord"]["Type"] == RECORD_TYPE
    rendered = json.dumps(template)
    assert "Custom::BedrockAgentCoreRegistry" not in rendered
    assert "AWS::DynamoDB::Table" not in rendered


def test_registry_is_iam_authorized_and_auto_approves_submissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    props = build_template(cfg)["Resources"]["Registry"]["Properties"]
    assert props["AuthorizerType"] == "AWS_IAM"
    assert props["ApprovalConfiguration"] == {"AutoApprovalRules": ["APPROVE_ALL"]}


def test_record_uses_custom_descriptor_with_deny_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    props = build_template(cfg)["Resources"]["GovernanceRecord"]["Properties"]
    assert props["RecordType"] == "CUSTOM"
    data = json.loads(props["Descriptors"]["Custom"]["Data"])
    assert data == governance_document(cfg)
    assert data["authorization"]["defaultDecision"] == "DENY"
    assert data["target"]["arn"].endswith(":PROD")


def test_template_carries_all_required_allocation_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    template = build_template(cfg)
    for logical_id in ("Registry", "GovernanceRecord"):
        actual = tags_as_map(template["Resources"][logical_id]["Properties"]["Tags"])
        assert actual == cfg.tags
        for required in (
            "application-id",
            "agent-id",
            "tenant-id",
            "cost-centre",
            "environment",
        ):
            assert required in actual


def test_rollback_template_adds_only_an_intentionally_invalid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    normal = build_template(cfg)
    injected = build_template(cfg, inject_invalid_record=True)
    assert set(injected["Resources"]) - set(normal["Resources"]) == {"InvalidRecord"}
    invalid = injected["Resources"]["InvalidRecord"]["Properties"]
    assert invalid["RecordType"] == "MCP"
    assert set(invalid["Descriptors"]) == {"Custom"}
    assert tags_as_map(invalid["Tags"]) == cfg.tags


def test_outputs_pin_all_cross_account_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    outputs = build_template(cfg)["Outputs"]
    assert set(outputs) == {
        "RegistryArn",
        "RegistryId",
        "RecordArn",
        "RecordId",
        "RecordStatus",
    }


@pytest.mark.parametrize(
    "bad_prefix",
    ["UPPER", "ab", "starts_underscore", "a" * 33, "starts-with-dash-"],
)
def test_config_rejects_unsafe_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_prefix: str,
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    with pytest.raises(SpikeError, match="prefix"):
        SpikeConfig(
            account_id=ACCOUNT_ID,
            region=REGION,
            prefix=bad_prefix,
            git_head=GIT_HEAD,
            state_file=tmp_path / "state.json",
            evidence_file=tmp_path / "evidence.json",
        )


def test_paths_must_remain_inside_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("KIROCREW_SCRATCH", str(scratch))
    assert require_scratch_path(scratch / "safe.json") == scratch / "safe.json"
    with pytest.raises(SpikeError, match="outside"):
        require_scratch_path(tmp_path / "outside.json")


def test_symlink_escape_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    scratch.mkdir()
    outside.mkdir()
    (scratch / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("KIROCREW_SCRATCH", str(scratch))
    with pytest.raises(SpikeError, match="outside"):
        require_scratch_path(scratch / "escape" / "evidence.json")


@pytest.mark.parametrize(
    "value",
    [
        {"accessToken": "not-even-a-real-token"},
        {"nested": {"password": "fixed"}},
        {"value": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.signature"},
    ],
)
def test_evidence_rejects_sensitive_keys_and_jwt_shapes(value: object) -> None:
    with pytest.raises(SpikeError, match="Refusing"):
        assert_no_secrets(value)


def test_ownership_requires_every_exact_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    assert_owned_tags(cfg.tags, cfg, "resource")
    wrong = {**cfg.tags, "agenticai:test-run": "someone-else"}
    with pytest.raises(SpikeError, match="Refusing"):
        assert_owned_tags(wrong, cfg, "resource")


def test_governance_document_contains_no_account_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path, monkeypatch)
    rendered = json.dumps(governance_document(cfg), sort_keys=True)
    assert "${PLATFORM_ACCOUNT_ID}" not in rendered
    assert ACCOUNT_ID in rendered


def test_independent_cleanup_preserves_terminal_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_registry_spike import Evidence

    cfg = config(tmp_path, monkeypatch)
    first = Evidence(cfg)
    first.status("passed")
    cleanup = Evidence(cfg, preserve_terminal=True)
    cleanup.status("cleanup-passed")
    persisted = json.loads(cfg.evidence_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "passed"
    assert persisted["cleanupStatus"] == "passed"


def test_existing_evidence_identity_mismatch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_registry_spike import Evidence

    cfg = config(tmp_path, monkeypatch)
    first = Evidence(cfg)
    first.status("passed")
    changed = SpikeConfig(
        account_id=ACCOUNT_ID,
        region=REGION,
        prefix=cfg.prefix,
        git_head="f" * 40,
        state_file=cfg.state_file,
        evidence_file=cfg.evidence_file,
    )
    with pytest.raises(SpikeError, match="identity differs"):
        Evidence(changed, preserve_terminal=True)


def fake_access_key(prefix: str) -> str:
    return prefix + ("A" * 16)


@pytest.mark.parametrize(
    "value",
    [
        fake_access_key("AKIA"),
        fake_access_key("ASIA"),
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
    ],
)
def test_evidence_rejects_additional_credential_value_shapes(value: str) -> None:
    with pytest.raises(SpikeError, match="Refusing"):
        assert_no_secrets({"value": value})


def test_pinned_botocore_exposes_exact_control_plane_operations() -> None:
    from botocore.loaders import create_loader

    model = create_loader().load_service_model("agent-registry-control", "service-2")
    operations = model["operations"]
    for operation in (
        "GetRegistry",
        "GetRegistryRecord",
        "ListRegistries",
        "ListRegistryRecords",
        "ListTagsForResource",
        "SubmitRegistryRecordForApproval",
        "DeleteRegistryRecord",
        "DeleteRegistry",
    ):
        assert operation in operations

    submit_input = model["shapes"][operations["SubmitRegistryRecordForApproval"]["input"]["shape"]]
    assert set(submit_input["required"]) == {"registryId", "recordId"}
    assert set(submit_input["members"]) == {"registryId", "recordId"}


def test_pinned_botocore_exposes_exact_discovery_operation() -> None:
    from botocore.loaders import create_loader

    model = create_loader().load_service_model("agent-registry", "service-2")
    operation = model["operations"]["ListDiscoverableRegistryRecords"]
    input_shape = model["shapes"][operation["input"]["shape"]]
    assert input_shape["required"] == ["registryId"]
    assert {"registryId", "maxResults", "nextToken", "filters"}.issubset(
        input_shape["members"]
    )


class RecordingEvidence:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def add(self, event: str, **details: object) -> None:
        self.events.append((event, details))


class SequencedStackAws:
    def __init__(self, statuses: list[str | None]) -> None:
        self.statuses = statuses

    def get_stack(self, _stack_name: str) -> dict[str, str] | None:
        status = self.statuses.pop(0) if self.statuses else None
        return {"StackStatus": status} if status is not None else None

    def stack_events(self, _stack_name: str) -> list[dict[str, str]]:
        return []


def test_cleanup_wait_treats_predelete_failure_status_as_transitional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_registry_spike
    from agent_registry_spike import RegistryProbe

    cfg = config(tmp_path, monkeypatch)
    aws = SequencedStackAws(["ROLLBACK_COMPLETE", None])
    evidence = RecordingEvidence()
    monkeypatch.setattr(agent_registry_spike.time, "sleep", lambda _seconds: None)

    probe = RegistryProbe(cfg, aws, evidence)  # type: ignore[arg-type]

    assert probe.wait_stack({"ABSENT"}) == {}


def test_cleanup_wait_still_fails_immediately_on_delete_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_registry_spike import RegistryProbe

    cfg = config(tmp_path, monkeypatch)
    aws = SequencedStackAws(["DELETE_FAILED"])
    evidence = RecordingEvidence()
    probe = RegistryProbe(cfg, aws, evidence)  # type: ignore[arg-type]

    with pytest.raises(SpikeError, match="DELETE_FAILED"):
        probe.wait_stack({"ABSENT"})


def test_submission_gate_requires_draft_and_calls_submit_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_registry_spike import RegistryProbe

    class SubmitAws:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def submit_record(self, registry_id: str, record_id: str) -> dict[str, object]:
            self.calls.append((registry_id, record_id))
            return {
                "status": "APPROVED",
                "ResponseMetadata": {"RequestId": "request-id"},
            }

    cfg = config(tmp_path, monkeypatch)
    aws = SubmitAws()
    evidence = RecordingEvidence()
    probe = RegistryProbe(cfg, aws, evidence)  # type: ignore[arg-type]

    with pytest.raises(SpikeError, match="not DRAFT"):
        probe.submit_draft_for_approval("registry-id", "record-id", "APPROVED")
    assert aws.calls == []

    probe.submit_draft_for_approval("registry-id", "record-id", "DRAFT")
    assert aws.calls == [("registry-id", "record-id")]
    assert [event for event, _details in evidence.events] == ["record_submitted"]


def test_cleanup_sweeps_residuals_after_stack_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_registry_spike import RegistryProbe, cfn_tags

    cfg = config(tmp_path, monkeypatch)

    class RecoveryAws:
        def __init__(self) -> None:
            self.stack: dict[str, object] | None = {
                "StackStatus": "DELETE_FAILED",
                "Tags": cfn_tags(cfg.tags),
            }
            self.registry: dict[str, str] | None = {
                "name": cfg.registry_name,
                "registryId": "registry-id",
                "registryArn": "registry-arn",
            }
            self.record: dict[str, str] | None = {
                "name": cfg.record_name,
                "recordId": "record-id",
                "recordArn": "record-arn",
            }
            self.delete_stack_calls = 0
            self.deleted_records: list[str] = []
            self.deleted_registries: list[str] = []

        def get_stack(self, _stack_name: str) -> dict[str, object] | None:
            return self.stack

        def delete_stack(self, _stack_name: str) -> None:
            self.delete_stack_calls += 1
            if self.delete_stack_calls == 2:
                self.stack = None

        def list_registries(self) -> list[dict[str, str]]:
            return [self.registry] if self.registry is not None else []

        def tags(self, _arn: str) -> dict[str, str]:
            return cfg.tags

        def list_records(self, _registry_id: str) -> list[dict[str, str]]:
            return [self.record] if self.record is not None else []

        def delete_record(self, _registry_id: str, record_id: str) -> None:
            self.deleted_records.append(record_id)
            self.record = None

        def get_record(
            self,
            _registry_id: str,
            _record_id: str,
        ) -> dict[str, str] | None:
            return self.record

        def delete_registry(self, registry_id: str) -> None:
            self.deleted_registries.append(registry_id)
            self.registry = None

        def get_registry(self, _registry_id: str) -> dict[str, str] | None:
            return self.registry

    aws = RecoveryAws()
    evidence = RecordingEvidence()
    probe = RegistryProbe(cfg, aws, evidence)  # type: ignore[arg-type]

    def wait_stack(_wanted: set[str], timeout: int | None = None) -> dict[str, object]:
        del timeout
        if aws.delete_stack_calls == 1:
            raise SpikeError("Stack reached DELETE_FAILED")
        return {}

    monkeypatch.setattr(probe, "wait_stack", wait_stack)

    with pytest.raises(SpikeError, match="required residual recovery"):
        probe.cleanup()

    assert aws.delete_stack_calls == 2
    assert aws.deleted_records == ["record-id"]
    assert aws.deleted_registries == ["registry-id"]
    assert aws.stack is None
    assert aws.registry is None
    assert aws.record is None
    assert "cleanup_inventory_verified" in [
        event for event, _details in evidence.events
    ]
