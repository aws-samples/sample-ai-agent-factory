#!/usr/bin/env python3
"""Cleanup-first AgentCore Runtime+Memory compatibility spike.

A narrow, ephemeral probe of AgentCore Runtime and Memory. It creates exactly
two resources it owns -- one encrypted Memory and one Runtime built from a
caller-supplied, digest-pinned container image -- proves they reach ACTIVE and
READY, proves one deterministic ``InvokeAgentRuntime`` handshake, optionally
proves a Memory ``CreateEvent`` -> ``GetEvent`` round-trip, and then deletes
everything it created -- discovering partial creates so nothing leaks.

Strict boundary: the caller owns the container image, the execution role, and
the CMK. This probe owns ONLY the Runtime and the Memory.

Commands: ``deploy``, ``verify``, ``exercise-memory``, ``cleanup``, ``all``.

* ``verify`` may reach exactly one billable/side-effecting API,
  ``InvokeAgentRuntime`` -- and no lifecycle or data write. A static
  reachability test proves exactly that.
* ``exercise-memory`` may reach exactly the Memory event write (``create_event``;
  ``get_event`` is a read).
* ``deploy``/``cleanup`` reach the lifecycle mutations for their phase only.
* ``all`` runs cleanup in ``finally`` so a failure anywhere still tears down.

Every mutation is gated by an STS account check and, for invoke/event/delete, by
a live ownership proof: the resource must match the exact expected name,
account/region ARN scope, run-specific description, immutable inputs, and (for
Runtime) the five allocation tags read via ``ListTagsForResource``.

Evidence records booleans, statuses, hashes, and an account suffix only -- never
a payload, token, ARN, account id, or resource id.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# Standalone path bootstrap for the sibling gateway-spike helpers.
#
# The spike reuses shared helpers from ``../live-agentcore-gateway-spike``. When
# run as a standalone script from a fresh checkout there is no conftest to put
# that directory on sys.path, so do it here, safely and idempotently.
# --------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_GATEWAY_SPIKE_DIR = _THIS_DIR.parent / "live-agentcore-gateway-spike"
for _p in (_THIS_DIR, _GATEWAY_SPIKE_DIR):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import boto3  # noqa: E402
import botocore  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import runtime_memory_model as model  # noqa: E402
from gateway_spike import (  # noqa: E402
    Evidence,
    JsonStore,
    SpikeError,
    aws_error_code,
    utc_now,
)

# --------------------------------------------------------------------------
# Mutation / side-effect registry -- the contract the reachability test enforces
# --------------------------------------------------------------------------

#: State-changing SDK calls. A new mutating call must be registered here.
MUTATING_API_CALLS = frozenset(
    {
        "create_memory",
        "delete_memory",
        "create_agent_runtime",
        "delete_agent_runtime",
        "create_event",
    }
)

#: ``InvokeAgentRuntime`` is not a lifecycle/data write, but it IS billable and
#: side-effecting (it runs the agent). It is registered and scoped separately so
#: ``verify`` must opt into it explicitly and the AST guarantee can pin that
#: verify reaches exactly this one side-effecting call and no write.
SIDE_EFFECTING_API_CALLS = frozenset({"invoke_agent_runtime"})

#: All calls subject to the scope guard.
SCOPED_API_CALLS = MUTATING_API_CALLS | SIDE_EFFECTING_API_CALLS

#: Reads whose names could look mutating.
READ_ONLY_EXCEPTIONS = frozenset({"get_event"})

MEMORY_EXERCISE_MUTATIONS = frozenset({"create_event"})

#: Only this error code is tolerated during cleanup ("already gone").
NOT_FOUND_CODES = frozenset({"ResourceNotFoundException"})

# Bounded polling budgets.
READY_TIMEOUT_SECONDS = 600
ACTIVE_TIMEOUT_SECONDS = 600
ABSENT_TIMEOUT_SECONDS = 600
EVENT_CONSISTENCY_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 10
EVENT_POLL_INTERVAL_SECONDS = 3

#: Pagination guardrails: cap pages and refuse a repeated/never-advancing token.
MAX_LIST_PAGES = 100
LIST_PAGE_SIZE = 50

MEMORY_EVENT_EXPIRY_DAYS = 7


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpikeConfig:
    account_id: str
    region: str
    names: model.SpikeNames
    container_uri: str
    runtime_role_arn: str
    memory_kms_key_arn: str
    state_path: Path
    evidence_path: Path
    exercise_memory: bool

    @property
    def prefix(self) -> str:
        return self.names.prefix


def _scratch_file(scratch_path: Path, raw: str | None, default_name: str, label: str) -> Path:
    candidate = Path(raw).expanduser() if raw else scratch_path / default_name
    resolved = candidate.resolve()
    try:
        resolved.relative_to(scratch_path)
    except ValueError as error:
        raise SpikeError(f"{label} must remain under KIROCREW_SCRATCH") from error
    if resolved == scratch_path:
        raise SpikeError(f"{label} must name a file, not the scratch directory")
    return resolved


def build_config(args: argparse.Namespace) -> SpikeConfig:
    if not model.ACCOUNT_PATTERN.fullmatch(str(args.account_id)):
        raise SpikeError("--account-id must contain exactly 12 digits")
    if not model.region_is_supported(str(args.region)):
        raise SpikeError(
            f"Region {args.region!r} is not in the documented Runtime+Memory "
            f"region list {sorted(model.SUPPORTED_REGIONS)}"
        )
    names = model.SpikeNames(prefix=str(args.prefix))
    account_id = str(args.account_id)
    region = str(args.region)
    try:
        container_uri = model.validate_container_uri(
            str(args.container_uri), account_id=account_id, region=region
        )
        runtime_role_arn = model.validate_role_arn(
            str(args.runtime_role_arn), account_id=account_id
        )
        memory_kms_key_arn = model.validate_kms_key_arn(
            str(args.memory_kms_key_arn), account_id=account_id, region=region
        )
    except model.ModelError as error:
        raise SpikeError(str(error)) from error

    scratch = os.environ.get("KIROCREW_SCRATCH")
    if not scratch:
        raise SpikeError("KIROCREW_SCRATCH must be set; refusing shared /tmp state")
    scratch_path = Path(scratch).resolve()
    state_path = _scratch_file(
        scratch_path, args.state_file, f"{names.prefix}-runtime-memory-state.json",
        "--state-file",
    )
    evidence_path = _scratch_file(
        scratch_path, args.evidence_file, f"{names.prefix}-runtime-memory-evidence.json",
        "--evidence-file",
    )
    if state_path == evidence_path:
        raise SpikeError("--state-file and --evidence-file must be different files")
    return SpikeConfig(
        account_id=account_id,
        region=region,
        names=names,
        container_uri=container_uri,
        runtime_role_arn=runtime_role_arn,
        memory_kms_key_arn=memory_kms_key_arn,
        state_path=state_path,
        evidence_path=evidence_path,
        exercise_memory=bool(args.exercise_memory)
        or args.command in ("exercise-memory", "all"),
    )


# --------------------------------------------------------------------------
# Evidence with recursive value scanning + request-id fingerprinting
# --------------------------------------------------------------------------


class SpikeEvidence(Evidence):
    SCHEMA_VERSION = 3

    def __init__(
        self, path: Path, config: SpikeConfig, run_marker: str
    ) -> None:  # noqa: D107
        self.store = JsonStore(path)
        expected_run = {
            "prefix": config.prefix,
            "region": config.region,
            "accountSuffix": model.account_suffix(config.account_id),
            "runFingerprint": model.fingerprint(run_marker),
            "boto3Version": boto3.__version__,
            "botocoreVersion": botocore.__version__,
        }
        current = self.store.read()
        if current:
            try:
                model.assert_no_secret_values(current)
            except model.ModelError as error:
                raise SpikeError("Existing evidence contains a forbidden identifier") from error
            persisted_run = current.get("run")
            if (
                current.get("schemaVersion") != self.SCHEMA_VERSION
                or current.get("spike") != "agentcore-runtime-memory"
                or not isinstance(persisted_run, Mapping)
                or any(persisted_run.get(key) != value for key, value in expected_run.items())
                or not isinstance(current.get("events"), list)
                or not isinstance(current.get("unknowns"), list)
            ):
                raise SpikeError("Existing evidence does not belong to this exact run")
            self.document = current
            return
        self.document: dict[str, Any] = {
            "schemaVersion": self.SCHEMA_VERSION,
            "spike": "agentcore-runtime-memory",
            "run": {**expected_run, "startedAt": utc_now()},
            "unknowns": [],
            "events": [],
        }

    def add(self, event: str, **details: Any) -> None:
        model.assert_no_secret_values(details, path=event)
        super().add(event, **details)

    def add_unknown(self, code: str, detail: str) -> None:
        record = model.unknown(code, model.sanitize_error(detail, prefix=code))
        model.assert_no_secret_values(record, path="unknown")
        record["at"] = utc_now()
        unknowns = self.document.setdefault("unknowns", [])
        if not isinstance(unknowns, list):
            raise SpikeError("Evidence unknowns field is not an array")
        unknowns.append(record)
        self.store.write(self.document)

    def finish(self, status: str) -> None:
        current = self.document.get("status")
        if status == "cleanup-passed" and current in {"passed", "failed"}:
            self.document["lastCleanupAt"] = utc_now()
            self.store.write(self.document)
            return
        super().finish(status)


def request_fingerprint(response: Mapping[str, Any]) -> str:
    """Fingerprint a response's RequestId rather than persisting it raw."""
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, Mapping) else {}
    value = metadata.get("RequestId") if isinstance(metadata, Mapping) else None
    return model.fingerprint(str(value)) if value else "n/a"


# --------------------------------------------------------------------------
# API wrappers -- one named function per AWS operation, scope-guarded
# --------------------------------------------------------------------------


class RuntimeMemoryApi:
    def __init__(self, session: boto3.Session, config: SpikeConfig) -> None:
        self.config = config
        self.sts = session.client("sts")
        self.control = session.client(model.CONTROL_SERVICE)
        self.data = session.client(model.DATA_SERVICE)
        self.mutation_scope: frozenset[str] = frozenset()

    def _require_scope(self, operation: str) -> None:
        if operation not in SCOPED_API_CALLS:
            raise SpikeError(f"Operation {operation!r} is not in the scope registry")
        if operation not in self.mutation_scope:
            raise SpikeError(
                f"Refusing {operation!r}: the current command's scope is "
                f"{sorted(self.mutation_scope) or 'read-only'}"
            )

    def capability(self, service: str, operation_name: str, member: str) -> bool:
        client = self.control if service == model.CONTROL_SERVICE else self.data
        try:
            shape = client.meta.service_model.operation_model(operation_name).input_shape
        except Exception:  # pragma: no cover
            return False
        return bool(shape is not None and member in shape.members)

    # -- identity -------------------------------------------------------
    def get_caller_identity(self) -> Mapping[str, Any]:
        return self.sts.get_caller_identity()

    # -- Memory lifecycle ----------------------------------------------
    def create_memory(self, run_marker: str) -> tuple[Mapping[str, Any], str]:
        self._require_scope("create_memory")
        response = self.control.create_memory(
            clientToken=model.client_token(run_marker, "CreateMemory"),
            name=self.config.names.memory_name,
            description=self.config.names.ownership_description(run_marker),
            encryptionKeyArn=self.config.memory_kms_key_arn,
            eventExpiryDuration=MEMORY_EVENT_EXPIRY_DAYS,
            tags=dict(self.config.names.allocation_tags(run_marker)),
        )
        return response.get("memory", response), request_fingerprint(response)

    def get_memory(self, memory_id: str) -> Mapping[str, Any] | None:
        try:
            response = self.control.get_memory(memoryId=memory_id)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise
        return response.get("memory", response)

    def list_memories(self) -> Iterator[Mapping[str, Any]]:
        yield from _paginate(
            lambda token: self.control.list_memories(
                **_page_kwargs(token)
            ),
            *model.LIST_OUTPUT_FIELDS["ListMemories"],
        )

    def delete_memory(self, run_marker: str, memory_id: str) -> None:
        self._require_scope("delete_memory")
        try:
            self.control.delete_memory(
                clientToken=model.client_token(run_marker, "DeleteMemory"),
                memoryId=memory_id,
            )
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return
            raise

    # -- Runtime lifecycle ---------------------------------------------
    def create_agent_runtime(
        self, run_marker: str
    ) -> tuple[Mapping[str, Any], str]:
        self._require_scope("create_agent_runtime")
        response = self.control.create_agent_runtime(
            clientToken=model.client_token(run_marker, "CreateAgentRuntime"),
            agentRuntimeName=self.config.names.runtime_name,
            description=self.config.names.ownership_description(run_marker),
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": self.config.container_uri}
            },
            roleArn=self.config.runtime_role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            tags=dict(self.config.names.allocation_tags(run_marker)),
        )
        return response, request_fingerprint(response)

    def get_agent_runtime(self, runtime_id: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def list_agent_runtimes(self) -> Iterator[Mapping[str, Any]]:
        yield from _paginate(
            lambda token: self.control.list_agent_runtimes(**_page_kwargs(token)),
            *model.LIST_OUTPUT_FIELDS["ListAgentRuntimes"],
        )

    def list_tags_for_resource(self, resource_arn: str) -> dict[str, str]:
        response = self.control.list_tags_for_resource(resourceArn=resource_arn)
        tags = response.get("tags", {})
        return {str(k): str(v) for k, v in tags.items()} if isinstance(tags, Mapping) else {}

    def delete_agent_runtime(self, run_marker: str, runtime_id: str) -> None:
        self._require_scope("delete_agent_runtime")
        try:
            self.control.delete_agent_runtime(
                agentRuntimeId=runtime_id,
                clientToken=model.client_token(run_marker, "DeleteAgentRuntime"),
            )
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return
            raise

    # -- data plane -----------------------------------------------------
    def invoke_agent_runtime(
        self, runtime_arn: str, session_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self._require_scope("invoke_agent_runtime")
        response = self.data.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = response.get("response")
        raw = body.read() if hasattr(body, "read") else (body or b"")
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, Mapping) else {}

    def create_event(
        self, run_marker: str, memory_id: str, actor_id: str, session_id: str, marker: str
    ) -> Mapping[str, Any]:
        self._require_scope("create_event")
        response = self.data.create_event(
            clientToken=model.client_token(run_marker, "CreateEvent"),
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=model.build_memory_event_payload(marker),
        )
        return response.get("event", response)

    def get_event(
        self, memory_id: str, actor_id: str, session_id: str, event_id: str
    ) -> Mapping[str, Any] | None:
        try:
            response = self.data.get_event(
                memoryId=memory_id,
                sessionId=session_id,
                actorId=actor_id,
                eventId=event_id,
            )
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise
        return response.get("event", response)


def _page_kwargs(token: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"maxResults": LIST_PAGE_SIZE}
    if token:
        kwargs["nextToken"] = token
    return kwargs


def _paginate(
    call: Callable[[str | None], Mapping[str, Any]], items_key: str, token_key: str
) -> Iterator[Mapping[str, Any]]:
    """Bounded pagination with repeated-token/runaway protection."""
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_LIST_PAGES):
        response = call(token)
        for item in response.get(items_key, []) or []:
            if isinstance(item, Mapping):
                yield item
        token = response.get(token_key)
        if not token:
            return
        if token in seen_tokens:
            raise SpikeError("Pagination returned a repeated nextToken; aborting")
        seen_tokens.add(str(token))
    raise SpikeError(f"Pagination exceeded {MAX_LIST_PAGES} pages; aborting")


# --------------------------------------------------------------------------
# Spike orchestration
# --------------------------------------------------------------------------


class RuntimeMemorySpike:
    def __init__(self, config: SpikeConfig) -> None:
        if (
            boto3.__version__ != model.REQUIRED_BOTO3_VERSION
            or botocore.__version__ != model.REQUIRED_BOTOCORE_VERSION
        ):
            raise SpikeError(
                "Pinned SDK mismatch: required "
                f"boto3 {model.REQUIRED_BOTO3_VERSION} / "
                f"botocore {model.REQUIRED_BOTOCORE_VERSION}; found "
                f"boto3 {boto3.__version__} / botocore {botocore.__version__}"
            )
        self.config = config
        self.names = config.names
        self.session = boto3.Session(region_name=config.region)
        self.api = RuntimeMemoryApi(self.session, config)
        self.state_store = JsonStore(config.state_path)
        self.state = self.state_store.read()
        self.run_marker = self._load_or_init_run_marker()
        self.evidence = SpikeEvidence(config.evidence_path, config, self.run_marker)

    # -- run marker + provenance ---------------------------------------
    def _load_or_init_run_marker(self) -> str:
        if self.state:
            return model.assert_state_provenance(
                self.state,
                account_id=self.config.account_id,
                region=self.config.region,
                prefix=self.config.prefix,
            )
        marker = model.new_run_marker()
        # Persist the provenance header atomically BEFORE any AWS mutation.
        self.state = model.build_state_header(
            run_marker=marker,
            account_id=self.config.account_id,
            region=self.config.region,
            prefix=self.config.prefix,
        )
        self.state_store.write(self.state)
        return marker

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state_store.write(self.state)

    def require_state(self, key: str) -> str:
        value = self.state.get(key)
        if not value:
            raise SpikeError(f"State {key!r} is missing; run deploy first")
        return str(value)

    # -- scope ----------------------------------------------------------
    def set_scope(self, operations: Sequence[str]) -> None:
        unknown = set(operations) - SCOPED_API_CALLS
        if unknown:
            raise SpikeError(f"Unknown scope entries: {sorted(unknown)}")
        self.api.mutation_scope = frozenset(operations)

    # -- identity gate --------------------------------------------------
    def verify_identity(self) -> None:
        identity = self.api.get_caller_identity()
        actual = str(identity.get("Account", ""))
        if actual != self.config.account_id:
            raise SpikeError("STS account does not match --account-id")
        self.evidence.add("identity-verified", accountSuffix=model.account_suffix(actual))

    # -- ownership proofs ----------------------------------------------
    def _prove_runtime_owned(self, record: Mapping[str, Any]) -> None:
        arn = str(record.get("agentRuntimeArn", ""))
        tags = self.api.list_tags_for_resource(arn) if arn else {}
        model.assert_runtime_owned(
            record,
            names=self.names,
            run_marker=self.run_marker,
            account_id=self.config.account_id,
            region=self.config.region,
            container_uri=self.config.container_uri,
            role_arn=self.config.runtime_role_arn,
            tags=tags,
        )

    def _prove_memory_owned(self, record: Mapping[str, Any]) -> None:
        model.assert_memory_owned(
            record,
            names=self.names,
            run_marker=self.run_marker,
            account_id=self.config.account_id,
            region=self.config.region,
            kms_key_arn=self.config.memory_kms_key_arn,
            event_expiry_days=MEMORY_EVENT_EXPIRY_DAYS,
        )

    # -- bounded polling ------------------------------------------------
    def wait_memory_active(self, memory_id: str, timeout: int = ACTIVE_TIMEOUT_SECONDS) -> None:
        self._poll(
            timeout,
            lambda: self.api.get_memory(memory_id),
            classify=model.classify_memory_status,
            terminal=model.assert_not_terminal_memory,
            target="active",
            event="memory-active",
            label="Memory",
        )

    def wait_runtime_ready(self, runtime_id: str, timeout: int = READY_TIMEOUT_SECONDS) -> None:
        self._poll(
            timeout,
            lambda: self.api.get_agent_runtime(runtime_id),
            classify=model.classify_runtime_status,
            terminal=model.assert_not_terminal_runtime,
            target="ready",
            event="runtime-ready",
            label="Runtime",
        )

    def _poll(
        self, timeout, getter, *, classify, terminal, target, event, label
    ) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = getter()
            if record is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            status = str(record.get("status", ""))
            terminal(status)
            if classify(status) == target:
                self.evidence.add(event, status=status)
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise SpikeError(f"{label} did not reach {target.upper()} within {timeout}s")

    def wait_absent(
        self,
        label: str,
        getter: Callable[[], Mapping[str, Any] | None],
        terminal: Callable[[str], None],
        timeout: int = ABSENT_TIMEOUT_SECONDS,
    ) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = getter()
            if record is None:
                return
            terminal(str(record.get("status", "")))
            time.sleep(POLL_INTERVAL_SECONDS)
        raise SpikeError(f"{label} was not deleted within {timeout}s")

    # -- discovery / recovery ------------------------------------------
    def discover_owned_runtime(self) -> str | None:
        """Return the id of the exact-name owned Runtime, or refuse a collision."""
        for summary in self.api.list_agent_runtimes():
            if str(summary.get("agentRuntimeName", "")) != self.names.runtime_name:
                continue
            runtime_id = str(summary.get("agentRuntimeId", ""))
            record = self.api.get_agent_runtime(runtime_id) if runtime_id else None
            if record is None:
                continue
            if model.is_runtime_owned(
                record,
                names=self.names,
                run_marker=self.run_marker,
                account_id=self.config.account_id,
                region=self.config.region,
                container_uri=self.config.container_uri,
                role_arn=self.config.runtime_role_arn,
                tags=self.api.list_tags_for_resource(str(record.get("agentRuntimeArn", ""))),
            ):
                return runtime_id
            self.evidence.add("collision-refused", resource="runtime", ownedByRun=False)
            raise SpikeError(
                "An exact-name Runtime exists but is NOT owned by this run; refusing to delete it"
            )
        return None

    def discover_owned_memory(self) -> str | None:
        """ListMemories summaries lack name, so GetMemory each candidate."""
        for summary in self.api.list_memories():
            memory_id = str(summary.get("id") or summary.get("memoryId") or "")
            if not memory_id:
                continue
            record = self.api.get_memory(memory_id)
            if not record or str(record.get("name", "")) != self.names.memory_name:
                continue
            if model.is_memory_owned(
                record,
                names=self.names,
                run_marker=self.run_marker,
                account_id=self.config.account_id,
                region=self.config.region,
                kms_key_arn=self.config.memory_kms_key_arn,
                event_expiry_days=MEMORY_EVENT_EXPIRY_DAYS,
            ):
                return memory_id
            self.evidence.add("collision-refused", resource="memory", ownedByRun=False)
            raise SpikeError(
                "An exact-name Memory exists but is NOT owned by this run; refusing to delete it"
            )
        return None

    # -- commands -------------------------------------------------------
    def _bind_state_identity(self, key: str, live_value: str) -> None:
        persisted = self.state.get(key)
        if persisted and str(persisted) != live_value:
            raise model.ProvenanceError(
                f"Persisted {key} does not match the discovered live resource"
            )
        self.save_state(**{key: live_value})

    def _bind_runtime_arn(self, record: Mapping[str, Any]) -> str:
        live_runtime_arn = str(record.get("agentRuntimeArn", ""))
        if not live_runtime_arn:
            raise SpikeError("Ownership-proven Runtime has no ARN")
        self._bind_state_identity("runtimeArn", live_runtime_arn)
        return live_runtime_arn

    def _ensure_memory(self) -> str:
        memory_id = self.discover_owned_memory()
        if memory_id:
            self._bind_state_identity("memoryId", memory_id)
            self.evidence.add(
                "memory-recovered",
                memoryIdFingerprint=model.fingerprint(memory_id),
            )
        else:
            memory, request_fp = self.api.create_memory(self.run_marker)
            memory_id = str(memory.get("id") or memory.get("memoryId") or "")
            if not memory_id:
                raise SpikeError("CreateMemory did not return a memory id")
            self._bind_state_identity("memoryId", memory_id)
            self.evidence.add(
                "memory-created",
                requestFingerprint=request_fp,
                memoryIdFingerprint=model.fingerprint(memory_id),
            )
        self.wait_memory_active(memory_id)
        live_memory = self.api.get_memory(memory_id)
        if live_memory is None:
            raise SpikeError("Memory disappeared after reaching ACTIVE")
        self._prove_memory_owned(live_memory)
        return memory_id

    def _ensure_runtime(self) -> tuple[str, str]:
        runtime_id = self.discover_owned_runtime()
        if runtime_id:
            self._bind_state_identity("runtimeId", runtime_id)
            self.evidence.add(
                "runtime-recovered",
                runtimeIdFingerprint=model.fingerprint(runtime_id),
            )
        else:
            runtime, request_fp = self.api.create_agent_runtime(self.run_marker)
            runtime_id = str(runtime.get("agentRuntimeId") or runtime.get("id") or "")
            response_arn = str(runtime.get("agentRuntimeArn") or "")
            if not runtime_id or not response_arn:
                raise SpikeError("CreateAgentRuntime did not return id/arn")
            self._bind_state_identity("runtimeId", runtime_id)
            self._bind_state_identity("runtimeArn", response_arn)
            self.evidence.add(
                "runtime-created",
                requestFingerprint=request_fp,
                runtimeIdFingerprint=model.fingerprint(runtime_id),
            )
        self.wait_runtime_ready(runtime_id)
        live_runtime = self.api.get_agent_runtime(runtime_id)
        if live_runtime is None:
            raise SpikeError("Runtime disappeared after reaching READY")
        self._prove_runtime_owned(live_runtime)
        return runtime_id, self._bind_runtime_arn(live_runtime)

    def deploy(self) -> None:
        if self.state.get("completed") is True:
            raise SpikeError(
                "This run already completed cleanup; use fresh state/evidence files "
                "so AgentCore idempotency tokens are never reused after deletion"
            )
        self.set_scope(["create_memory", "create_agent_runtime"])
        self.verify_identity()
        self._ensure_memory()
        self._ensure_runtime()

    def verify(self) -> None:
        """Read status, prove ownership, then invoke ONE deterministic handshake.

        The only scoped call is ``invoke_agent_runtime`` -- no lifecycle/data
        write is reachable. STS account validation runs first.
        """
        self.set_scope(["invoke_agent_runtime"])
        self.verify_identity()

        memory_id = self.require_state("memoryId")
        runtime_id = self.require_state("runtimeId")

        memory = self.api.get_memory(memory_id)
        if not memory or model.classify_memory_status(str(memory.get("status", ""))) != "active":
            raise SpikeError("Memory is not ACTIVE")
        self._prove_memory_owned(memory)

        runtime = self.api.get_agent_runtime(runtime_id)
        if not runtime or model.classify_runtime_status(str(runtime.get("status", ""))) != "ready":
            raise SpikeError("Runtime is not READY")
        self._prove_runtime_owned(runtime)
        live_runtime_arn = self._bind_runtime_arn(runtime)

        session_id = model.runtime_session_id(self.run_marker)
        self.save_state(runtimeSessionId=session_id)
        payload = model.build_invocation_payload()
        response = self.api.invoke_agent_runtime(live_runtime_arn, session_id, payload)
        ok = model.handshake_is_exact(response)
        self.evidence.add(
            "handshake-invoked",
            handshakeOk=ok,
            responseFingerprint=model.fingerprint(json.dumps(response, sort_keys=True)),
        )
        if not ok:
            raise SpikeError("Runtime invocation did not return the exact handshake")

    def exercise_memory(self) -> None:
        self.set_scope(list(MEMORY_EXERCISE_MUTATIONS))
        self.verify_identity()

        memory_id = self.require_state("memoryId")
        memory = self.api.get_memory(memory_id)
        if not memory or model.classify_memory_status(str(memory.get("status", ""))) != "active":
            raise SpikeError("Memory is not ACTIVE for exercise-memory")
        self._prove_memory_owned(memory)

        # Actor/session are deterministic for this run. Persisted values may be
        # reused only when they match the derivation exactly; scratch-state
        # tampering must never steer a write into another namespace.
        actor_id = model.derive_actor_id(self.names.prefix)
        session_id = model.derive_session_id(self.run_marker, self.names.prefix)
        for key, expected in (("actorId", actor_id), ("sessionId", session_id)):
            persisted = self.state.get(key)
            if persisted and str(persisted) != expected:
                raise model.ProvenanceError(
                    f"Persisted {key} does not match this run's deterministic value"
                )
        marker = model.derive_event_marker(self.names.prefix)
        self.save_state(actorId=actor_id, sessionId=session_id)

        event = self.api.create_event(self.run_marker, memory_id, actor_id, session_id, marker)
        event_id = str(event.get("eventId") or "")
        if not event_id:
            raise SpikeError("CreateEvent did not return an eventId")
        self.save_state(eventId=event_id)
        self.evidence.add("memory-event-created", createOk=True,
                          eventIdFingerprint=model.fingerprint(event_id))

        ok = self._poll_event_round_trip(memory_id, actor_id, session_id, event_id, marker)
        self.evidence.add("memory-event-read", roundTripOk=ok,
                          eventIdFingerprint=model.fingerprint(event_id))
        if not ok:
            raise SpikeError("GetEvent did not reproduce the exact event this run wrote")

    def _poll_event_round_trip(self, memory_id, actor_id, session_id, event_id, marker) -> bool:
        import time

        deadline = time.monotonic() + EVENT_CONSISTENCY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            fetched = self.api.get_event(memory_id, actor_id, session_id, event_id)
            if fetched is None:
                time.sleep(EVENT_POLL_INTERVAL_SECONDS)
                continue
            if not model.memory_round_trip_ok(
                fetched,
                expected_event_id=event_id,
                expected_actor_id=actor_id,
                expected_session_id=session_id,
                expected_memory_id=memory_id,
                expected_marker=marker,
            ):
                raise SpikeError("GetEvent returned identity or content drift")
            return True
        return False

    def _cleanup_runtime(self) -> None:
        runtime_id = self.discover_owned_runtime() or str(
            self.state.get("runtimeId") or ""
        )
        if not runtime_id:
            return
        record = self.api.get_agent_runtime(runtime_id)
        if record is None:
            return
        self._prove_runtime_owned(record)
        self.api.delete_agent_runtime(self.run_marker, runtime_id)
        self.wait_absent(
            "runtime",
            lambda: self.api.get_agent_runtime(runtime_id),
            model.assert_not_terminal_runtime,
        )
        self.evidence.add("runtime-deleted", deleted=True)

    def _cleanup_memory(self) -> None:
        memory_id = self.discover_owned_memory() or str(
            self.state.get("memoryId") or ""
        )
        if not memory_id:
            return
        record = self.api.get_memory(memory_id)
        if record is None:
            return
        self._prove_memory_owned(record)
        self.api.delete_memory(self.run_marker, memory_id)
        self.wait_absent(
            "memory",
            lambda: self.api.get_memory(memory_id),
            model.assert_not_terminal_memory,
        )
        self.evidence.add("memory-deleted", deleted=True)

    def cleanup(self) -> None:
        """Sweep Runtime then Memory, even if one independent step fails.

        Every delete still requires live ownership proof. A foreign collision,
        API error, terminal deletion status, or uncertain inventory is retained
        as failure evidence and can never be converted into a false clean pass.
        """
        self.set_scope(["delete_agent_runtime", "delete_memory"])
        self.verify_identity()

        failed_steps: list[str] = []
        try:
            self._cleanup_runtime()
        except Exception as error:  # noqa: BLE001 - continue the owned sweep
            failed_steps.append(f"runtime:{type(error).__name__}")
            self.evidence.add(
                "cleanup-step-failed",
                resource="runtime",
                errorType=type(error).__name__,
                errorCode=(
                    aws_error_code(error) if isinstance(error, ClientError) else None
                ),
            )
        try:
            self._cleanup_memory()
        except Exception as error:  # noqa: BLE001 - continue the owned sweep
            failed_steps.append(f"memory:{type(error).__name__}")
            self.evidence.add(
                "cleanup-step-failed",
                resource="memory",
                errorType=type(error).__name__,
                errorCode=(
                    aws_error_code(error) if isinstance(error, ClientError) else None
                ),
            )

        residue = self.residual_inventory()
        self.evidence.add("residual-inventory", **residue)
        if failed_steps or any(residue.values()):
            failed = ",".join(failed_steps) if failed_steps else "none"
            raise SpikeError(
                f"Cleanup incomplete; failed steps={failed}; residual={residue}"
            )

        # A completed run is terminal: AgentCore can retain idempotency tokens
        # after deletion, so a later deploy must use fresh state/evidence files
        # and therefore a fresh run marker.
        self.state = {
            **model.build_state_header(
                run_marker=self.run_marker,
                account_id=self.config.account_id,
                region=self.config.region,
                prefix=self.config.prefix,
            ),
            "completed": True,
        }
        self.state_store.write(self.state)

    def residual_inventory(self) -> dict[str, bool]:
        """Discovery-based residue sweep; any uncertainty counts as present."""
        result: dict[str, bool] = {}
        for label, discover in (
            ("runtime", self.discover_owned_runtime),
            ("memory", self.discover_owned_memory),
        ):
            try:
                result[label] = discover() is not None
            except Exception:  # noqa: BLE001 - never convert uncertainty to zero
                result[label] = True
        return result

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

COMMANDS = ("deploy", "verify", "exercise-memory", "cleanup", "all")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", default="aiaf-rm-spike")
    parser.add_argument("--container-uri", required=True,
                        help="Caller-published, @sha256 digest-pinned ECR image URI")
    parser.add_argument("--runtime-role-arn", required=True,
                        help="Existing AgentCore Runtime execution role ARN")
    parser.add_argument("--memory-kms-key-arn", required=True,
                        help="Existing CMK ARN for Memory encryption")
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    parser.add_argument("--exercise-memory", action="store_true",
                        help="Also prove a CreateEvent -> GetEvent round-trip")
    return parser.parse_args(argv)


def run_command(spike: RuntimeMemorySpike, command: str) -> str:
    if command == "deploy":
        spike.deploy()
        return "deploy-passed"
    if command == "verify":
        spike.verify()
        return "verify-passed"
    if command == "exercise-memory":
        spike.exercise_memory()
        return "exercise-memory-passed"
    if command == "cleanup":
        spike.cleanup()
        return "cleanup-passed"
    primary: Exception | None = None
    try:
        spike.deploy()
        spike.verify()
        spike.exercise_memory()
    except Exception as error:  # noqa: BLE001 - cleanup must still run
        primary = error
    finally:
        try:
            spike.cleanup()
        except Exception as cleanup_error:  # noqa: BLE001
            if primary is not None:
                raise SpikeError(
                    f"Run failed: {primary}; cleanup also failed: {cleanup_error}"
                ) from cleanup_error
            raise
    if primary is not None:
        raise primary
    return "passed"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except Exception as error:  # noqa: BLE001 - no evidence file exists yet
        message = model.sanitize_error(str(error), prefix=args.command)
        print(f"FAIL: {message}", file=sys.stderr)
        return 1

    spike: RuntimeMemorySpike | None = None
    try:
        spike = RuntimeMemorySpike(config)
        status = run_command(spike, args.command)
        spike.evidence.finish(status)
        print(f"Evidence: {config.evidence_path}")
        return 0
    except Exception as error:  # noqa: BLE001 - single fail-closed exit path
        message = model.sanitize_error(str(error), prefix=args.command)
        if spike is not None:
            spike.evidence.add(
                "failure",
                errorType=type(error).__name__,
                errorCode=(
                    aws_error_code(error) if isinstance(error, ClientError) else None
                ),
                message=message,
            )
            spike.evidence.finish("failed")
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    finally:
        if spike is not None:
            spike.close()


if __name__ == "__main__":
    raise SystemExit(main())
