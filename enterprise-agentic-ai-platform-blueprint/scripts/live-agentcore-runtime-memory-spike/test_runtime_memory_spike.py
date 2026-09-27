"""Offline conformance and orchestration tests for the runtime+memory spike.

No live AWS call. boto3 clients used for the service-model contract test are
constructed with explicit DUMMY credentials so test collection can never touch
IMDS or any other credential provider. Every orchestration test drives a fake
API recorder.

Structural guarantees exercised here:

* the scope registry is complete -- a new mutating/side-effecting SDK call
  cannot be added without registering it;
* ``verify`` reaches exactly ``invoke_agent_runtime`` and no lifecycle/data
  write;
* ``exercise-memory`` reaches only ``create_event``; ``cleanup`` reaches
  deletions but no creations;
* exact operation names, required + optional members, list/get output fields,
  pagination shape, and botocore version equality against the offline model;
* ownership proofs gate invoke/event/delete; partial-create recovery finds an
  un-persisted resource; poisoned/foreign state and exact-name collisions are
  refused, never deleted;
* every cleanup runs after a mid-run failure.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

boto3 = pytest.importorskip("boto3", reason="the live spike requires the pinned AWS SDK")

import runtime_memory_model as model  # noqa: E402
from gateway_spike import SpikeError  # noqa: E402

if boto3.__version__ != model.REQUIRED_BOTO3_VERSION:  # pragma: no cover - env guard
    pytest.skip(
        f"boto3 {model.REQUIRED_BOTO3_VERSION} is required; found {boto3.__version__}",
        allow_module_level=True,
    )

import runtime_memory_spike as spike_module  # noqa: E402

ACCOUNT = "123456789012"
WRONG_ACCOUNT = "999999999999"
REGION = "us-west-2"
PREFIX = "aiaf-rm-test"
DIGEST_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo@sha256:{'a' * 64}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/exec"
KMS_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"
NAMES = model.SpikeNames(prefix=PREFIX)
RUNTIME_ID = "aiaf_rm_test_runtime-Abc0123xyz"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
MEMORY_ID = "aiaf_rm_test_memory-Def0456uvw"
MEMORY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/{MEMORY_ID}"
EVENT_ID = "evt-0001"


# --------------------------------------------------------------------------
# Static call-graph analysis
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module_ast() -> ast.Module:
    return ast.parse(Path(spike_module.__file__).read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__"):
            found[node.name] = node  # type: ignore[assignment]
    return found


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
            elif isinstance(child.func, ast.Name):
                names.add(child.func.id)
    return names


def _reachable(functions: Mapping[str, ast.FunctionDef], entry: str) -> set[str]:
    visited: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        node = functions.get(current)
        if node is not None:
            pending.extend(_called_names(node))
    return visited


def _scoped_reachable_from(functions: Mapping[str, ast.FunctionDef], entry: str) -> set[str]:
    reachable = _reachable(functions, entry)
    called: set[str] = set()
    for name in reachable:
        node = functions.get(name)
        if node is not None:
            called |= _called_names(node)
    return (called | reachable) & spike_module.SCOPED_API_CALLS


def test_every_scoped_sdk_call_is_registered(module_ast: ast.Module) -> None:
    clients = {"control", "data", "sts"}
    prefixes = ("create_", "delete_", "update_", "put_", "add_", "invoke_")
    unregistered: set[str] = set()
    for node in ast.walk(module_ast):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Attribute) and target.attr in clients):
            continue
        name = node.func.attr
        if not name.startswith(prefixes) or name in spike_module.READ_ONLY_EXCEPTIONS:
            continue
        if name not in spike_module.SCOPED_API_CALLS:
            unregistered.add(name)
    assert not unregistered, f"unregistered scoped calls: {sorted(unregistered)}"


def test_verify_reaches_exactly_invoke_and_no_write(module_ast: ast.Module) -> None:
    reached = _scoped_reachable_from(_functions(module_ast), "verify")
    assert reached == {"invoke_agent_runtime"}, sorted(reached)
    assert not (reached & spike_module.MUTATING_API_CALLS)


def test_exercise_memory_reaches_only_the_event_write(module_ast: ast.Module) -> None:
    reached = _scoped_reachable_from(_functions(module_ast), "exercise_memory")
    assert reached == {"create_event"}, sorted(reached)


def test_cleanup_reaches_deletions_but_never_creations(module_ast: ast.Module) -> None:
    reached = _scoped_reachable_from(_functions(module_ast), "cleanup")
    assert not {n for n in reached if n.startswith("create_")}
    assert "delete_agent_runtime" in reached and "delete_memory" in reached
    assert "invoke_agent_runtime" not in reached


def test_registry_partitions() -> None:
    assert not (spike_module.MUTATING_API_CALLS & spike_module.READ_ONLY_EXCEPTIONS)
    assert not (spike_module.MUTATING_API_CALLS & spike_module.SIDE_EFFECTING_API_CALLS)
    assert spike_module.SCOPED_API_CALLS == (
        spike_module.MUTATING_API_CALLS | spike_module.SIDE_EFFECTING_API_CALLS
    )


# --------------------------------------------------------------------------
# Offline botocore contract (dummy credentials, no IMDS)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def offline_session():  # type: ignore[no-untyped-def]
    return boto3.Session(
        aws_access_key_id="AKIAOFFLINEDUMMYKEY0",
        aws_secret_access_key="offline/dummy/secret/key/for/model/only",
        aws_session_token="offline-dummy-session-token",
        region_name=REGION,
    )


@pytest.fixture(scope="module")
def control_model(offline_session):  # type: ignore[no-untyped-def]
    return offline_session.client(model.CONTROL_SERVICE).meta.service_model


@pytest.fixture(scope="module")
def data_model(offline_session):  # type: ignore[no-untyped-def]
    return offline_session.client(model.DATA_SERVICE).meta.service_model


def test_botocore_version_equality() -> None:
    import botocore

    assert boto3.__version__ == model.REQUIRED_BOTO3_VERSION
    assert botocore.__version__ == model.REQUIRED_BOTOCORE_VERSION


def _assert_members(service_model, operations, optional):  # type: ignore[no-untyped-def]
    available = set(service_model.operation_names)
    for op, required in operations.items():
        assert op in available, f"{op} missing from pinned model"
        shape = service_model.operation_model(op).input_shape
        modeled_required = set(shape.required_members) if shape else set()
        modeled_all = set(shape.members) if shape else set()
        assert set(required) == modeled_required, f"{op} required-member drift"
        assert set(optional.get(op, ())) <= modeled_all, f"{op} optional drift"


def test_runtime_contract(control_model) -> None:  # type: ignore[no-untyped-def]
    _assert_members(control_model, model.RUNTIME_OPERATIONS, model.RUNTIME_OPTIONAL_MEMBERS)


def test_memory_contract(control_model) -> None:  # type: ignore[no-untyped-def]
    _assert_members(control_model, model.MEMORY_OPERATIONS, model.MEMORY_OPTIONAL_MEMBERS)


def test_data_contract(data_model) -> None:  # type: ignore[no-untyped-def]
    _assert_members(data_model, model.DATA_PLANE_OPERATIONS, model.DATA_OPTIONAL_MEMBERS)


def test_list_output_fields_are_modeled(control_model) -> None:  # type: ignore[no-untyped-def]
    for op, (items_key, token_key) in model.LIST_OUTPUT_FIELDS.items():
        out = control_model.operation_model(op).output_shape
        assert items_key in out.members, f"{op} output missing {items_key}"
        assert token_key in out.members, f"{op} output missing {token_key}"


def test_consumed_output_fields_are_modeled(control_model, data_model) -> None:  # type: ignore[no-untyped-def]
    create_runtime = control_model.operation_model("CreateAgentRuntime").output_shape
    assert {"agentRuntimeId", "agentRuntimeArn", "status"} <= set(create_runtime.members)

    get_runtime = control_model.operation_model("GetAgentRuntime").output_shape
    assert {
        "agentRuntimeId", "agentRuntimeArn", "agentRuntimeName", "status",
        "description", "agentRuntimeArtifact", "roleArn",
    } <= set(get_runtime.members)
    runtime_container = get_runtime.members["agentRuntimeArtifact"].members[
        "containerConfiguration"
    ]
    assert "containerUri" in runtime_container.members

    for operation in ("CreateMemory", "GetMemory"):
        output = control_model.operation_model(operation).output_shape
        memory = output.members["memory"]
        assert {
            "id", "arn", "name", "status", "description",
            "encryptionKeyArn", "eventExpiryDuration",
        } <= set(memory.members)

    invoke = data_model.operation_model("InvokeAgentRuntime").output_shape
    assert "response" in invoke.members
    for operation in ("CreateEvent", "GetEvent"):
        event = data_model.operation_model(operation).output_shape.members["event"]
        assert {"eventId", "actorId", "sessionId", "memoryId", "payload"} <= set(
            event.members
        )


def test_status_enums_match_the_pinned_model(control_model) -> None:  # type: ignore[no-untyped-def]
    runtime_status = control_model.operation_model("GetAgentRuntime").output_shape.members[
        "status"
    ]
    assert set(runtime_status.enum) == {
        "CREATING", "CREATE_FAILED", "UPDATING", "UPDATE_FAILED",
        "READY", "DELETING", "DELETE_FAILED",
    }
    assert model.RUNTIME_TERMINAL_FAILURES == {
        status for status in runtime_status.enum if status.endswith("_FAILED")
    }

    memory_status = control_model.operation_model("GetMemory").output_shape.members[
        "memory"
    ].members["status"]
    assert set(memory_status.enum) == {
        "CREATING", "ACTIVE", "FAILED", "DELETING", "UPDATING",
    }
    assert model.MEMORY_TERMINAL_FAILURES == {
        status for status in memory_status.enum if status == "FAILED"
    }


def test_nested_artifact_and_network_shapes(control_model) -> None:  # type: ignore[no-untyped-def]
    artifact = control_model.operation_model("CreateAgentRuntime").input_shape.members["agentRuntimeArtifact"]
    container = artifact.members["containerConfiguration"]
    assert "containerUri" in container.members
    net = control_model.operation_model("CreateAgentRuntime").input_shape.members["networkConfiguration"]
    assert "networkMode" in net.members


def test_serialized_create_requests_match_the_model(offline_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Serialize the real create requests through botocore and assert the full
    wire shape (nested container union + enum) is accepted by the model."""
    from botocore import serialize

    control = offline_session.client(model.CONTROL_SERVICE)
    sm = control.meta.service_model
    op = sm.operation_model("CreateAgentRuntime")
    serializer = serialize.create_serializer(sm.metadata["protocol"])
    params = {
        "clientToken": model.client_token("0" * 32, "CreateAgentRuntime"),
        "agentRuntimeName": NAMES.runtime_name,
        "description": NAMES.ownership_description("0" * 32),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": DIGEST_IMAGE}},
        "roleArn": ROLE_ARN,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "tags": dict(NAMES.allocation_tags("0" * 32)),
    }
    request = serializer.serialize_to_request(params, op)
    assert DIGEST_IMAGE in json.dumps(
        request["body"] if isinstance(request["body"], (dict, list)) else str(request["body"])
    )

    memory_op = sm.operation_model("CreateMemory")
    serializer.serialize_to_request(
        {
            "clientToken": model.client_token("0" * 32, "CreateMemory"),
            "name": NAMES.memory_name,
            "description": NAMES.ownership_description("0" * 32),
            "encryptionKeyArn": KMS_ARN,
            "eventExpiryDuration": spike_module.MEMORY_EVENT_EXPIRY_DAYS,
            "tags": dict(NAMES.allocation_tags("0" * 32)),
        },
        memory_op,
    )

    from datetime import datetime, timezone

    data = offline_session.client(model.DATA_SERVICE)
    data_model = data.meta.service_model
    data_serializer = serialize.create_serializer(data_model.metadata["protocol"])
    data_serializer.serialize_to_request(
        {
            "clientToken": model.client_token("0" * 32, "CreateEvent"),
            "memoryId": MEMORY_ID,
            "actorId": model.derive_actor_id(PREFIX),
            "sessionId": model.derive_session_id("0" * 32, PREFIX),
            "eventTimestamp": datetime.now(timezone.utc),
            "payload": model.build_memory_event_payload(
                model.derive_event_marker(PREFIX)
            ),
        },
        data_model.operation_model("CreateEvent"),
    )
    data_serializer.serialize_to_request(
        {
            "agentRuntimeArn": RUNTIME_ARN,
            "runtimeSessionId": model.runtime_session_id("0" * 32),
            "contentType": "application/json",
            "accept": "application/json",
            "payload": json.dumps(model.build_invocation_payload()).encode("utf-8"),
        },
        data_model.operation_model("InvokeAgentRuntime"),
    )


# --------------------------------------------------------------------------
# Standalone import + --help smoke test
# --------------------------------------------------------------------------


def test_help_runs_standalone_from_a_different_cwd(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(spike_module.__file__).resolve()), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "deploy" in result.stdout and "exercise-memory" in result.stdout


def test_agent_dockerfile_uses_valid_install_and_minimal_direct_dependencies() -> None:
    agent_dir = Path(spike_module.__file__).resolve().parent / "agent"
    dockerfile = (agent_dir / "Dockerfile").read_text(encoding="utf-8")
    normalized = " ".join(dockerfile.replace("\\", " ").split())
    expected_base = (
        "FROM public.ecr.aws/lambda/python:3.13@sha256:"
        "c78a03b745f2c27b349377ae979d88b96bc68b457668705cd24a32b5f433c9cf"
    )
    assert dockerfile.count("\nFROM ") == 1
    assert expected_base in dockerfile
    assert "FROM --platform" not in dockerfile
    assert "useradd" not in dockerfile and "adduser" not in dockerfile
    assert "\nUSER 10001\n" in dockerfile
    assert dockerfile.rstrip().endswith('ENTRYPOINT ["python", "-u", "agent.py"]\nCMD []')
    assert "--require-hashes=false" not in dockerfile
    assert "pip install --no-cache-dir --requirement /app/requirements.txt" in normalized
    assert "&& pip check" in normalized

    dependencies = [
        line.strip()
        for line in (agent_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert dependencies == ["bedrock-agentcore==1.23.1"]
    assert (agent_dir / ".dockerignore").read_text(encoding="utf-8").splitlines() == [
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
    ]


# --------------------------------------------------------------------------
# Fake API recorder
# --------------------------------------------------------------------------


class FakeApi:
    def __init__(self, config: spike_module.SpikeConfig) -> None:
        self.config = config
        self.mutation_scope = frozenset()
        self.calls: list[str] = []
        self.memory_status = ["ACTIVE"]
        self.runtime_status = ["READY"]
        self.memory_exists = True
        self.runtime_exists = True
        self.account = ACCOUNT
        self.run_marker = "0" * 32
        # discovery inventories
        self.runtime_list: list[dict] = []
        self.memory_list: list[dict] = []
        self.handshake = {
            "marker": model.HANDSHAKE_MARKER,
            "echoFingerprint": model.expected_ping_fingerprint(),
            "runtimeReady": True,
        }
        self.event_record = None

    def _require_scope(self, op: str) -> None:
        if op not in spike_module.SCOPED_API_CALLS:
            raise SpikeError(f"{op} not registered")
        if op not in self.mutation_scope:
            raise SpikeError(f"scope refuses {op}")

    def get_caller_identity(self) -> Mapping[str, Any]:
        self.calls.append("get_caller_identity")
        return {"Account": self.account}

    def _runtime_record(self) -> dict:
        return {
            "agentRuntimeName": NAMES.runtime_name,
            "agentRuntimeArn": RUNTIME_ARN,
            "agentRuntimeId": RUNTIME_ID,
            "status": self.runtime_status[0] if len(self.runtime_status) == 1 else self.runtime_status.pop(0),
            "description": NAMES.ownership_description(self.run_marker),
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": DIGEST_IMAGE}},
            "roleArn": ROLE_ARN,
        }

    def _memory_record(self) -> dict:
        return {
            "id": MEMORY_ID,
            "name": NAMES.memory_name,
            "arn": MEMORY_ARN,
            "status": self.memory_status[0] if len(self.memory_status) == 1 else self.memory_status.pop(0),
            "description": NAMES.ownership_description(self.run_marker),
            "encryptionKeyArn": KMS_ARN,
            "eventExpiryDuration": 7,
        }

    def create_memory(self, run_marker: str):  # type: ignore[no-untyped-def]
        self._require_scope("create_memory")
        self.calls.append("create_memory")
        return {"id": MEMORY_ID, "status": "CREATING"}, "rf-mem"

    def get_memory(self, memory_id: str):  # type: ignore[no-untyped-def]
        self.calls.append("get_memory")
        return self._memory_record() if self.memory_exists else None

    def list_memories(self) -> Iterator[Mapping[str, Any]]:
        self.calls.append("list_memories")
        yield from self.memory_list

    def delete_memory(self, run_marker: str, memory_id: str) -> None:
        self._require_scope("delete_memory")
        self.calls.append("delete_memory")
        self.memory_exists = False
        self.memory_list = []

    def create_agent_runtime(self, run_marker: str):  # type: ignore[no-untyped-def]
        self._require_scope("create_agent_runtime")
        self.calls.append("create_agent_runtime")
        return {"agentRuntimeId": RUNTIME_ID, "agentRuntimeArn": RUNTIME_ARN}, "rf-rt"

    def get_agent_runtime(self, runtime_id: str):  # type: ignore[no-untyped-def]
        self.calls.append("get_agent_runtime")
        return self._runtime_record() if self.runtime_exists else None

    def list_agent_runtimes(self) -> Iterator[Mapping[str, Any]]:
        self.calls.append("list_agent_runtimes")
        yield from self.runtime_list

    def list_tags_for_resource(self, resource_arn: str) -> dict:
        self.calls.append("list_tags_for_resource")
        return dict(NAMES.allocation_tags(self.run_marker))

    def delete_agent_runtime(self, run_marker: str, runtime_id: str) -> None:
        self._require_scope("delete_agent_runtime")
        self.calls.append("delete_agent_runtime")
        self.runtime_exists = False
        self.runtime_list = []

    def invoke_agent_runtime(self, runtime_arn: str, session_id: str, payload):  # type: ignore[no-untyped-def]
        self._require_scope("invoke_agent_runtime")
        self.calls.append("invoke_agent_runtime")
        self.last_runtime_arn = runtime_arn
        self.last_session_id = session_id
        return dict(self.handshake)

    def create_event(self, run_marker, memory_id, actor_id, session_id, marker):  # type: ignore[no-untyped-def]
        self._require_scope("create_event")
        self.calls.append("create_event")
        self.event_record = {
            "eventId": EVENT_ID, "actorId": actor_id, "sessionId": session_id,
            "memoryId": memory_id,
            "payload": [{"conversational": {"role": "USER", "content": {"text": marker}}}],
        }
        return {"eventId": EVENT_ID}

    def get_event(self, memory_id, actor_id, session_id, event_id):  # type: ignore[no-untyped-def]
        self.calls.append("get_event")
        return self.event_record


def _config(tmp_path: Path) -> spike_module.SpikeConfig:
    return spike_module.SpikeConfig(
        account_id=ACCOUNT, region=REGION, names=NAMES, container_uri=DIGEST_IMAGE,
        runtime_role_arn=ROLE_ARN, memory_kms_key_arn=KMS_ARN,
        state_path=tmp_path / "state.json", evidence_path=tmp_path / "evidence.json",
        exercise_memory=True,
    )


def _spike(tmp_path: Path, *, seed_state: dict | None = None) -> spike_module.RuntimeMemorySpike:
    config = _config(tmp_path)
    from gateway_spike import JsonStore

    store = JsonStore(config.state_path)
    if seed_state is not None:
        store.write(seed_state)
    spike = spike_module.RuntimeMemorySpike.__new__(spike_module.RuntimeMemorySpike)
    spike.config = config
    spike.names = config.names
    spike.state_store = store
    spike.state = store.read()
    spike.run_marker = spike._load_or_init_run_marker()
    spike.evidence = spike_module.SpikeEvidence(
        config.evidence_path, config, spike.run_marker
    )
    spike.api = FakeApi(config)
    spike.api.run_marker = spike.run_marker
    return spike


def _fresh_header(marker: str = "0" * 32) -> dict:
    return model.build_state_header(run_marker=marker, account_id=ACCOUNT, region=REGION, prefix=PREFIX)


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


def _args(tmp_path: Path, **over: Any):  # type: ignore[no-untyped-def]
    base = dict(command="deploy", account_id=ACCOUNT, region=REGION, prefix=PREFIX,
                container_uri=DIGEST_IMAGE, runtime_role_arn=ROLE_ARN, memory_kms_key_arn=KMS_ARN,
                state_file=None, evidence_file=None, exercise_memory=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_build_config_requires_scratch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_SCRATCH", raising=False)
    with pytest.raises(SpikeError):
        spike_module.build_config(_args(tmp_path))


def test_build_config_rejects_paths_outside_scratch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    with pytest.raises(SpikeError):
        spike_module.build_config(_args(tmp_path, state_file="/etc/passwd"))


def test_build_config_rejects_same_state_and_evidence_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    shared = str(tmp_path / "shared.json")
    with pytest.raises(SpikeError, match="must be different"):
        spike_module.build_config(
            _args(tmp_path, state_file=shared, evidence_file=shared)
        )


def test_build_config_rejects_bad_region_account_and_tag_image(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    with pytest.raises(SpikeError):
        spike_module.build_config(_args(tmp_path, region="ap-south-1"))
    with pytest.raises(SpikeError):
        spike_module.build_config(_args(tmp_path, account_id="12345"))
    tag_image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo:v1"
    with pytest.raises(SpikeError):
        spike_module.build_config(_args(tmp_path, container_uri=tag_image))


# --------------------------------------------------------------------------
# Run marker / provenance
# --------------------------------------------------------------------------


def test_run_marker_persisted_before_any_mutation(tmp_path: Path) -> None:
    spike = _spike(tmp_path)  # fresh
    persisted = json.loads(Path(spike.config.state_path).read_text())
    assert model.RUN_MARKER_PATTERN.fullmatch(persisted["runMarker"])
    assert persisted["accountId"] == ACCOUNT and persisted["prefix"] == PREFIX
    # No AWS mutation has happened yet.
    assert not spike.api.calls


def test_resume_refuses_foreign_state(tmp_path: Path) -> None:
    foreign = model.build_state_header(run_marker="a" * 32, account_id=WRONG_ACCOUNT, region=REGION, prefix=PREFIX)
    with pytest.raises(model.ProvenanceError):
        _spike(tmp_path, seed_state=foreign)


def test_tokens_derive_from_run_marker(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state=_fresh_header("b" * 32))
    assert spike.run_marker == "b" * 32
    assert model.client_token("b" * 32, "CreateMemory") == model.client_token(spike.run_marker, "CreateMemory")


# --------------------------------------------------------------------------
# STS gate
# --------------------------------------------------------------------------


def test_deploy_checks_identity_before_any_mutation(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.account = WRONG_ACCOUNT
    with pytest.raises(SpikeError):
        spike.deploy()
    assert "create_memory" not in spike.api.calls


def test_verify_keeps_sts_before_invoke(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "memoryId": MEMORY_ID,
                                         "runtimeId": RUNTIME_ID, "runtimeArn": RUNTIME_ARN})
    spike.api.account = WRONG_ACCOUNT
    with pytest.raises(SpikeError):
        spike.verify()
    assert "invoke_agent_runtime" not in spike.api.calls


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_deploy_creates_memory_before_runtime(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.memory_status = ["ACTIVE"]
    spike.api.runtime_status = ["READY"]
    spike.deploy()
    assert spike.api.calls.index("create_memory") < spike.api.calls.index("create_agent_runtime")
    assert spike.state["memoryId"] == MEMORY_ID and spike.state["runtimeId"] == RUNTIME_ID


def test_deploy_recovers_partial_creates_without_duplicate_mutation(
    tmp_path: Path,
) -> None:
    spike = _spike(tmp_path)
    spike.api.memory_list = [{"id": MEMORY_ID}]
    spike.api.runtime_list = [
        {"agentRuntimeName": NAMES.runtime_name, "agentRuntimeId": RUNTIME_ID}
    ]
    spike.deploy()
    assert "create_memory" not in spike.api.calls
    assert "create_agent_runtime" not in spike.api.calls
    assert spike.state["memoryId"] == MEMORY_ID
    assert spike.state["runtimeId"] == RUNTIME_ID
    assert spike.state["runtimeArn"] == RUNTIME_ARN


def test_deploy_refuses_completed_run_token_reuse(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.runtime_exists = False
    spike.api.memory_exists = False
    spike.cleanup()
    calls_before = list(spike.api.calls)
    with pytest.raises(SpikeError, match="idempotency tokens"):
        spike.deploy()
    assert spike.api.calls == calls_before


def test_verify_scope_is_invoke_only_and_uses_session_id(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "memoryId": MEMORY_ID,
                                         "runtimeId": RUNTIME_ID, "runtimeArn": RUNTIME_ARN})
    spike.verify()
    assert spike.api.mutation_scope == frozenset({"invoke_agent_runtime"})
    assert spike.api.last_runtime_arn == RUNTIME_ARN
    assert model.RUNTIME_SESSION_PATTERN.fullmatch(spike.api.last_session_id)
    assert spike.evidence.has_event("handshake-invoked")


def test_verify_uses_live_arn_when_state_arn_is_missing(tmp_path: Path) -> None:
    spike = _spike(
        tmp_path,
        seed_state={
            **_fresh_header(),
            "memoryId": MEMORY_ID,
            "runtimeId": RUNTIME_ID,
        },
    )
    spike.verify()
    assert spike.api.last_runtime_arn == RUNTIME_ARN
    assert spike.state["runtimeArn"] == RUNTIME_ARN


def test_verify_refuses_poisoned_persisted_runtime_arn(tmp_path: Path) -> None:
    foreign_arn = RUNTIME_ARN.replace(RUNTIME_ID, "foreign_runtime-Abc0123xyz")
    spike = _spike(
        tmp_path,
        seed_state={
            **_fresh_header(),
            "memoryId": MEMORY_ID,
            "runtimeId": RUNTIME_ID,
            "runtimeArn": foreign_arn,
        },
    )
    with pytest.raises(model.ProvenanceError):
        spike.verify()
    assert "invoke_agent_runtime" not in spike.api.calls


def test_verify_fails_on_non_exact_handshake(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "memoryId": MEMORY_ID,
                                         "runtimeId": RUNTIME_ID, "runtimeArn": RUNTIME_ARN})
    spike.api.handshake = {"marker": model.HANDSHAKE_MARKER, "echoFingerprint": "wrong", "runtimeReady": True}
    with pytest.raises(SpikeError):
        spike.verify()


def test_verify_refuses_unowned_runtime(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "memoryId": MEMORY_ID,
                                         "runtimeId": RUNTIME_ID, "runtimeArn": RUNTIME_ARN})
    # Break ownership: runtime description names a different run marker.
    other = model.SpikeNames(prefix=PREFIX).ownership_description("f" * 32)
    orig = spike.api._runtime_record

    def broken():  # type: ignore[no-untyped-def]
        rec = orig()
        rec["description"] = other
        return rec

    spike.api._runtime_record = broken  # type: ignore[assignment]
    with pytest.raises(model.OwnershipError):
        spike.verify()


def test_exercise_memory_round_trip_and_reuses_session(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "memoryId": MEMORY_ID})
    spike.exercise_memory()
    assert spike.api.calls.index("create_event") < spike.api.calls.index("get_event")
    assert spike.evidence.has_event("memory-event-read")
    first_session = spike.state["sessionId"]
    # Retry reuses the persisted session/actor.
    spike.exercise_memory()
    assert spike.state["sessionId"] == first_session


@pytest.mark.parametrize("field", ["actorId", "sessionId"])
def test_exercise_memory_refuses_poisoned_namespace_state(
    tmp_path: Path, field: str
) -> None:
    spike = _spike(
        tmp_path,
        seed_state={
            **_fresh_header(),
            "memoryId": MEMORY_ID,
            field: "foreign-namespace",
        },
    )
    with pytest.raises(model.ProvenanceError):
        spike.exercise_memory()
    assert "create_event" not in spike.api.calls


# --------------------------------------------------------------------------
# Terminal status
# --------------------------------------------------------------------------


def test_deploy_fails_fast_on_terminal_memory(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.memory_status = ["FAILED"]
    with pytest.raises(model.StatusError):
        spike.deploy()
    assert "create_agent_runtime" not in spike.api.calls


@pytest.mark.parametrize(
    ("label", "record", "terminal"),
    [
        ("runtime", {"status": "DELETE_FAILED"}, model.assert_not_terminal_runtime),
        ("memory", {"status": "FAILED"}, model.assert_not_terminal_memory),
    ],
)
def test_wait_absent_fails_fast_on_terminal_status(
    tmp_path: Path, label: str, record: dict, terminal
) -> None:  # type: ignore[no-untyped-def]
    spike = _spike(tmp_path)
    with pytest.raises(model.StatusError):
        spike.wait_absent(label, lambda: record, terminal, timeout=1)


# --------------------------------------------------------------------------
# Cleanup: order, discovery, collision, residue, cleanup-after-failure
# --------------------------------------------------------------------------


def test_cleanup_deletes_runtime_before_memory(tmp_path: Path) -> None:
    spike = _spike(tmp_path, seed_state={**_fresh_header(), "runtimeId": RUNTIME_ID, "memoryId": MEMORY_ID})
    spike.cleanup()
    assert spike.api.calls.index("delete_agent_runtime") < spike.api.calls.index("delete_memory")
    # provenance retained, resource ids dropped, and token reuse blocked
    assert "runtimeId" not in spike.state and spike.state["runMarker"] == spike.run_marker
    assert spike.state["completed"] is True


def test_cleanup_recovers_partial_create_via_discovery(tmp_path: Path) -> None:
    # State header only -- the create "succeeded" but its id was never persisted.
    spike = _spike(tmp_path)
    spike.api.runtime_list = [{"agentRuntimeName": NAMES.runtime_name, "agentRuntimeId": RUNTIME_ID}]
    spike.api.memory_list = [{"id": MEMORY_ID}]
    spike.cleanup()
    assert "delete_agent_runtime" in spike.api.calls and "delete_memory" in spike.api.calls


def test_cleanup_refuses_foreign_collision_and_never_deletes(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    # An exact-name runtime exists but belongs to another run (wrong description).
    spike.api.runtime_list = [{"agentRuntimeName": NAMES.runtime_name, "agentRuntimeId": RUNTIME_ID}]

    orig = spike.api._runtime_record

    def foreign():  # type: ignore[no-untyped-def]
        rec = orig()
        rec["description"] = "someone else's run"
        return rec

    spike.api._runtime_record = foreign  # type: ignore[assignment]
    with pytest.raises(SpikeError):
        spike.cleanup()
    assert "delete_agent_runtime" not in spike.api.calls


def test_cleanup_sweeps_memory_after_runtime_step_failure(tmp_path: Path) -> None:
    spike = _spike(
        tmp_path,
        seed_state={
            **_fresh_header(),
            "runtimeId": RUNTIME_ID,
            "memoryId": MEMORY_ID,
        },
    )

    def fail_runtime_cleanup() -> None:
        raise SpikeError("simulated runtime cleanup failure")

    spike._cleanup_runtime = fail_runtime_cleanup  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="runtime:SpikeError"):
        spike.cleanup()
    assert "delete_memory" in spike.api.calls
    assert spike.evidence.has_event("cleanup-step-failed")


def test_cleanup_is_idempotent_on_empty_account(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.runtime_exists = False
    spike.api.memory_exists = False
    spike.cleanup()
    assert "delete_agent_runtime" not in spike.api.calls
    assert "delete_memory" not in spike.api.calls


def test_run_command_all_cleans_up_after_verify_failure(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.api.memory_status = ["ACTIVE"]
    spike.api.runtime_status = ["READY"]
    spike.api.handshake = {"marker": "no", "echoFingerprint": "no", "runtimeReady": False}
    # discovery inventories so cleanup can find + delete what deploy created
    spike.api.runtime_list = [{"agentRuntimeName": NAMES.runtime_name, "agentRuntimeId": RUNTIME_ID}]
    spike.api.memory_list = [{"id": MEMORY_ID}]
    with pytest.raises(SpikeError):
        spike_module.run_command(spike, "all")
    assert "delete_agent_runtime" in spike.api.calls and "delete_memory" in spike.api.calls


# --------------------------------------------------------------------------
# Scope enforcement
# --------------------------------------------------------------------------


def test_scope_blocks_unscoped_mutation(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.set_scope([])
    with pytest.raises(SpikeError):
        spike.api.create_memory(spike.run_marker)


def test_scope_blocks_invoke_unless_scoped(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.set_scope([])
    with pytest.raises(SpikeError):
        spike.api.invoke_agent_runtime(RUNTIME_ARN, model.runtime_session_id(spike.run_marker), {})


def test_set_scope_rejects_unknown_operation(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    with pytest.raises(SpikeError):
        spike.set_scope(["not_a_real_op"])


# --------------------------------------------------------------------------
# Evidence sanitization + request-id fingerprinting
# --------------------------------------------------------------------------


def test_evidence_refuses_secret_or_id_fields(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    with pytest.raises((SpikeError, model.SecretLeakError)):
        spike.evidence.add("leak", token="abc")
    with pytest.raises((SpikeError, model.SecretLeakError)):
        spike.evidence.add("leak", arn=RUNTIME_ARN)


def test_evidence_has_no_raw_ids_after_deploy(tmp_path: Path) -> None:
    spike = _spike(tmp_path)
    spike.deploy()
    blob = Path(spike.config.evidence_path).read_text()
    assert MEMORY_ID not in blob and RUNTIME_ID not in blob and RUNTIME_ARN not in blob
    assert ACCOUNT not in blob


def test_evidence_resume_is_bound_to_the_exact_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    marker = "0" * 32
    evidence = spike_module.SpikeEvidence(config.evidence_path, config, marker)
    evidence.add("safe-event", ok=True)

    resumed = spike_module.SpikeEvidence(config.evidence_path, config, marker)
    assert resumed.has_event("safe-event")
    with pytest.raises(SpikeError, match="exact run"):
        spike_module.SpikeEvidence(config.evidence_path, config, "f" * 32)


def test_evidence_resume_refuses_identifier_bearing_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    marker = "0" * 32
    evidence = spike_module.SpikeEvidence(config.evidence_path, config, marker)
    evidence.add("safe-event", ok=True)
    document = evidence.store.read()
    document["events"].append({"event": "poisoned", "note": RUNTIME_ARN})
    evidence.store.write(document)

    with pytest.raises(SpikeError, match="forbidden identifier"):
        spike_module.SpikeEvidence(config.evidence_path, config, marker)


def test_request_fingerprint_never_returns_raw_id() -> None:
    fp = spike_module.request_fingerprint({"ResponseMetadata": {"RequestId": "12345678-1234-1234-1234-123456789012"}})
    assert fp != "12345678-1234-1234-1234-123456789012" and len(fp) == 32


# --------------------------------------------------------------------------
# Pagination guardrails
# --------------------------------------------------------------------------


def test_pagination_refuses_repeated_token() -> None:
    def call(token):  # type: ignore[no-untyped-def]
        return {"agentRuntimes": [{"agentRuntimeName": "x"}], "nextToken": "same"}

    with pytest.raises(SpikeError):
        list(spike_module._paginate(call, "agentRuntimes", "nextToken"))


def test_pagination_caps_pages() -> None:
    def call(token):  # type: ignore[no-untyped-def]
        n = int(token) + 1 if token else 1
        return {"agentRuntimes": [], "nextToken": str(n)}

    with pytest.raises(SpikeError):
        list(spike_module._paginate(call, "agentRuntimes", "nextToken"))
