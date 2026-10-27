#!/usr/bin/env python3
"""Append-only, exact-commit evidence recorder for production-readiness runs.

Evidence is EXECUTED, never asserted. The only way an event enters a run is by
this recorder running a command the release contract declares -- an offline
suite or a live scenario -- and writing a receipt: the resolved argv, the exact
tool binaries (path + digest + reported version), exit codes, timing, the JUnit
file it parsed to verify each mapped test selector, the live provenance the
scenario emitted (account, Region, stack, deployed commit), and digests of every
artifact the command produced. A status of ``pass`` is derived from those facts
and re-derived on every load, so a hand-written event cannot earn credit.

The recorder never stores environment variable VALUES, credentials, or raw
AWS/API responses; it stores the contract's own command text, file digests, and
bounded scalars. Every mutation rechecks that the Agentic-ai-self-service tree
AND the repository's CI workflow are still the clean objects captured when the
run started.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "build" / "release-evidence"
CONTROLS_PATH = PROJECT_ROOT / "release" / "production-readiness-controls.json"
REGIONS_PATH = PROJECT_ROOT / "release" / "agentcore-regions.json"
TRACEABILITY_PATH = PROJECT_ROOT / "release" / "production-readiness-traceability.json"
#: The repository-root CI workflow is executable release infrastructure that lives OUTSIDE
#: the project subtree. Its blob hash is part of the run identity and its dirty state
#: blocks recording, exactly like the project tree.
ROOT_WORKFLOW_RELATIVE = ".github/workflows/agentic-ai-self-service-ci.yml"
RECORDER_VERSION = "2.0.0"
RUN_SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 2
_STATUS_VALUES = frozenset({"pass", "fail", "blocked"})
#: A scenario or suite command signals "cannot run here yet" (prerequisite missing,
#: scenario not implemented) with this exit status. It records as ``blocked``, never pass.
BLOCKED_EXIT_STATUS = 3
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_REGION = re.compile(r"^(?:local|[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d)$")
_SAFE_ACCOUNT_ID = re.compile(r"^\d{12}$")
_SAFE_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
#: Tool placeholders are resolved to absolute executables supplied by the operator and
#: recorded by path AND digest. Context placeholders are filled by the recorder itself.
_TOOL_PLACEHOLDERS = frozenset({"backend_python", "infra_python", "actionlint", "npm", "uv", "cdk"})
_CONTEXT_PLACEHOLDERS = frozenset({"artifact_dir", "junit_xml", "run_dir", "region", "account_id", "scenario_id"})
_PASSTHROUGH_ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "USER", "SHELL")
_MAX_ARTIFACT_FILES = 20_000
_MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
_MAX_JUNIT_BYTES = 64 * 1024 * 1024
_PROVENANCE_LABEL = "provenance"
_PROVENANCE_REQUIRED_KEYS = frozenset({"account_id", "region", "stack_id", "deployed_commit", "deployed_project_tree"})
_PROVENANCE_OPTIONAL_KEYS = frozenset({"endpoint", "archive_sha256", "artifact_manifest_sha256", "deployed_build"})
_SENSITIVE_KEY = re.compile(r"(?i)(token|secret|password|credential|authorization|cookie|private.?key|session.?key)")
_TOKEN_VALUE = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|"
    r"AWS4-HMAC-SHA256|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_REGION_MATRIX_SCHEMA_VERSION = 2
_REGION_MATRIX_PROFILES = frozenset({"full", "reduced", "excluded"})
_REGION_MATRIX_BOOLEAN_FIELDS = frozenset(
    {"core", "production_release", "memory", "agent_registry", "web_search", "harness"}
)
# The ONLY capability tuples a selected Region may carry, keyed
# (core, production_release, memory, agent_registry, web_search, harness). A "reduced"
# Region is one of exactly two reviewed shapes -- without managed Web Search but with AWS
# Agent Registry (Frankfurt, London), or without both (Paris). A generic "not all true"
# rule used to admit shapes nobody reviewed, such as Web Search without Agent Registry.
_REGION_MATRIX_PROFILE_TUPLES: dict[str, frozenset[tuple[bool, ...]]] = {
    "full": frozenset({(True, True, True, True, True, True)}),
    "reduced": frozenset(
        {
            (True, True, True, True, False, True),
            (True, True, True, False, False, True),
        }
    ),
}
_REGION_MATRIX_TUPLE_ORDER = ("core", "production_release", "memory", "agent_registry", "web_search", "harness")
_REGION_MATRIX_ENVELOPE_KEYS = frozenset(
    {"partition", "deployment_mode", "harness", "a2a", "gateway_provider", "notes"}
)
_REGION_MATRIX_CORE_FEATURES = frozenset(
    {
        "runtime_microvm",
        "gateway",
        "identity",
        "built_in_tools",
        "observability",
        "policy",
        "evaluations",
    }
)
_REGION_MATRIX_SOURCE_URL = "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html"


class EvidenceError(RuntimeError):
    """Evidence cannot be recorded or verified safely."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_timestamp(value: Any, description: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise EvidenceError(f"{description} has an invalid timestamp") from exc
    if timestamp.tzinfo is None:
        raise EvidenceError(f"{description} timestamp lacks a timezone")
    return timestamp


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _reject_json_constant(value: str) -> None:
    raise EvidenceError(f"Non-standard JSON constant is not accepted: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"Duplicate JSON key is not accepted: {key}")
        value[key] = item
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError(f"Evidence document does not exist: {path}") from exc
    if not path.is_file() or path.is_symlink() or before.st_nlink != 1:
        raise EvidenceError(f"Evidence path is not one regular single-link file: {path}")
    if before.st_size > 2 * 1024 * 1024:
        raise EvidenceError(f"Evidence document exceeds 2 MiB: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EvidenceError(f"Evidence document changed while opening: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise EvidenceError(f"Evidence document exceeds 2 MiB: {path}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise EvidenceError(f"Evidence document changed while reading: {path}")
    finally:
        os.close(fd)
    try:
        decoded = b"".join(chunks).decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"Evidence document is not UTF-8: {path}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"Evidence document is not an object: {path}")
    return value


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-P", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise EvidenceError(f"Git identity command failed: {' '.join(args[:2])}")
    return completed.stdout.strip()


def _source_identity() -> dict[str, Any]:
    repo = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel")).resolve()
    project_rel = PROJECT_ROOT.resolve().relative_to(repo).as_posix()
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{commit}:{project_rel}")
    # The CI workflow lives at the repository root, outside the project subtree. It is
    # release infrastructure (the gates that admit the candidate), so it is part of the
    # identity: its committed blob is recorded and its working copy must be clean.
    workflow_blob = _git(repo, "rev-parse", f"{commit}:{ROOT_WORKFLOW_RELATIVE}")
    if not _SAFE_GIT_OBJECT.fullmatch(workflow_blob):
        raise EvidenceError("Repository CI workflow is not a committed blob")
    dirty = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        project_rel,
        ROOT_WORKFLOW_RELATIVE,
    )
    return {
        "repo": str(repo),
        "project_rel": project_rel,
        "commit": commit,
        "project_tree": tree,
        "root_workflow": ROOT_WORKFLOW_RELATIVE,
        "root_workflow_blob": workflow_blob,
        "branch": _git(repo, "branch", "--show-current"),
        "clean": not bool(dirty),
    }


def _require_same_clean_source(expected: dict[str, Any]) -> dict[str, Any]:
    current = _source_identity()
    for key in ("project_rel", "commit", "project_tree", "root_workflow", "root_workflow_blob"):
        if current.get(key) != expected.get(key):
            raise EvidenceError(f"Source identity changed at {key}; start a new evidence run")
    if not current["clean"]:
        raise EvidenceError("Project tree or repository CI workflow is dirty; evidence must bind to a clean commit")
    return current


def _load_contract(path: Path, *, expected_schema_version: int = 1) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema_version") != expected_schema_version:
        raise EvidenceError(f"Unsupported contract schema in {path}")
    return value


def _validate_details(details: dict[str, Any]) -> dict[str, Any]:
    if len(details) > 32:
        raise EvidenceError("At most 32 evidence detail fields are accepted")
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or _SENSITIVE_KEY.search(key):
            raise EvidenceError(f"Unsafe evidence detail key: {key}")
        if isinstance(value, bool) or value is None:
            clean[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if (isinstance(value, float) and not math.isfinite(value)) or abs(value) > 10**18:
                raise EvidenceError(f"Unsafe evidence detail value for {key}")
            clean[key] = value
        elif isinstance(value, str) and len(value) <= 500 and not _TOKEN_VALUE.search(value):
            clean[key] = value
        else:
            raise EvidenceError(f"Unsafe evidence detail value for {key}")
    return clean


_REGION_REQUIRED_KINDS = frozenset(
    {"live_positive", "live_adversarial", "live_region", "ui_cloudfront", "rollback", "teardown_zero"}
)


def _public_source(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_rel": identity["project_rel"],
        "commit": identity["commit"],
        "project_tree": identity["project_tree"],
        "root_workflow": identity["root_workflow"],
        "root_workflow_blob": identity["root_workflow_blob"],
        "branch": identity["branch"],
    }


def _validate_contracts(
    controls: dict[str, Any], regions: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    kinds = controls.get("evidence_kinds")
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(not isinstance(kind, str) or not _SAFE_NAME.fullmatch(kind) for kind in kinds)
        or len(kinds) != len(set(kinds))
    ):
        raise EvidenceError("Control contract evidence_kinds must be unique safe names")
    kind_set = set(kinds)

    raw_controls = controls.get("controls")
    if not isinstance(raw_controls, list) or not raw_controls:
        raise EvidenceError("Control contract must define at least one control")
    control_map: dict[str, dict[str, Any]] = {}
    for control in raw_controls:
        if not isinstance(control, dict):
            raise EvidenceError("Every control must be an object")
        control_id = control.get("id")
        description = control.get("description")
        required = control.get("required_kinds")
        if not isinstance(control_id, str) or not _SAFE_NAME.fullmatch(control_id):
            raise EvidenceError(f"Unsafe control id: {control_id}")
        if control_id in control_map:
            raise EvidenceError(f"Duplicate control id: {control_id}")
        if not isinstance(description, str) or not description.strip():
            raise EvidenceError(f"Control {control_id} needs a description")
        if (
            not isinstance(required, list)
            or not required
            or any(kind not in kind_set for kind in required)
            or len(required) != len(set(required))
        ):
            raise EvidenceError(f"Control {control_id} has invalid required_kinds")
        regional_kind = control.get("required_region_kind")
        selector = control.get("region_selector")
        if regional_kind is None and selector is not None:
            raise EvidenceError(f"Control {control_id} has a selector without a regional kind")
        if regional_kind is not None:
            if regional_kind not in kind_set or not isinstance(selector, str):
                raise EvidenceError(f"Control {control_id} has invalid regional requirements")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", selector):
                raise EvidenceError(f"Control {control_id} has an unsafe region selector")
        control_map[control_id] = control

    global_required = controls.get("global_required_kinds")
    if (
        not isinstance(global_required, list)
        or any(kind not in kind_set for kind in global_required)
        or len(global_required) != len(set(global_required))
    ):
        raise EvidenceError("global_required_kinds must contain unique declared kinds")

    if regions.get("schema_version") != _REGION_MATRIX_SCHEMA_VERSION:
        raise EvidenceError("Region matrix must use schema_version 2")
    if regions.get("partition") != "aws":
        raise EvidenceError("Region matrix must be explicitly scoped to the commercial aws partition")
    if regions.get("source_url") != _REGION_MATRIX_SOURCE_URL:
        raise EvidenceError("Region matrix must cite the reviewed AgentCore Regions documentation URL")
    accessed_at = regions.get("accessed_at")
    try:
        datetime.strptime(str(accessed_at), "%Y-%m-%d")
    except ValueError as exc:
        raise EvidenceError("Region matrix accessed_at must be YYYY-MM-DD") from exc

    raw_regions = regions.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise EvidenceError("Region matrix must define at least one Region")
    expected_entry_fields = {"region", "name", "profile"} | set(_REGION_MATRIX_BOOLEAN_FIELDS)
    selectors = {
        str(control["region_selector"])
        for control in control_map.values()
        if control.get("required_region_kind") is not None
    }
    region_map: dict[str, dict[str, Any]] = {}
    for entry in raw_regions:
        if not isinstance(entry, dict):
            raise EvidenceError("Every Region entry must be an object")
        region = entry.get("region")
        if not isinstance(region, str) or region == "local" or not _SAFE_REGION.fullmatch(region):
            raise EvidenceError(f"Invalid AWS Region: {region}")
        if region.startswith(("cn-", "us-gov-")):
            raise EvidenceError(f"Region {region} is outside the reviewed commercial aws partition")
        if region in region_map:
            raise EvidenceError(f"Duplicate AWS Region: {region}")
        for selector in selectors:
            if selector not in _REGION_MATRIX_BOOLEAN_FIELDS:
                raise EvidenceError(f"Region selector {selector} is not a reviewed capability flag")
            if not isinstance(entry.get(selector), bool):
                raise EvidenceError(f"Region {region} lacks boolean selector {selector}")
        if set(entry) != expected_entry_fields:
            raise EvidenceError(f"Region {region} fields do not match the matrix schema")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise EvidenceError(f"Region {region} needs a display name")
        profile = entry.get("profile")
        if profile not in _REGION_MATRIX_PROFILES:
            raise EvidenceError(f"Region {region} has an invalid capability profile")
        for field in _REGION_MATRIX_BOOLEAN_FIELDS:
            if not isinstance(entry.get(field), bool):
                raise EvidenceError(f"Region {region} lacks boolean capability {field}")
        capability_tuple = tuple(entry[field] for field in _REGION_MATRIX_TUPLE_ORDER)
        if profile == "excluded":
            if entry["production_release"]:
                raise EvidenceError(f"Region {region} excluded profile cannot be selected for production")
        elif capability_tuple not in _REGION_MATRIX_PROFILE_TUPLES[profile]:
            raise EvidenceError(f"Region {region} {profile} profile is not one of the reviewed capability shapes")
        region_map[region] = entry

    core_features = regions.get("core_features")
    if (
        not isinstance(core_features, list)
        or set(core_features) != _REGION_MATRIX_CORE_FEATURES
        or len(core_features) != len(set(core_features))
    ):
        raise EvidenceError("Region matrix core_features do not match the reviewed Runtime profile")
    production_envelope = regions.get("production_envelope")
    if not isinstance(production_envelope, dict):
        raise EvidenceError("Region matrix must define a production_envelope")
    if set(production_envelope) != _REGION_MATRIX_ENVELOPE_KEYS:
        raise EvidenceError("Region matrix production envelope must carry exactly the reviewed keys")
    if (
        production_envelope.get("partition") != "aws"
        or production_envelope.get("deployment_mode") != "runtime"
        or production_envelope.get("harness") is not False
        or production_envelope.get("a2a") is not False
        or production_envelope.get("gateway_provider") != "agentcore"
    ):
        raise EvidenceError(
            "Region matrix production envelope must be commercial Runtime-only on the AgentCore gateway "
            "without Harness or A2A"
        )
    notes = production_envelope.get("notes")
    if not isinstance(notes, list) or not notes or not all(isinstance(note, str) and note.strip() for note in notes):
        raise EvidenceError("Region matrix production envelope notes must be a non-empty list of strings")
    selected = {region for region, entry in region_map.items() if entry["production_release"]}
    if not selected:
        raise EvidenceError("Region matrix selects no production release Region")
    if any(region_map[region]["profile"] == "excluded" for region in selected):
        raise EvidenceError("Region matrix selects an excluded Region for production")
    return control_map, region_map


def _validate_traceability(
    controls: dict[str, Any], traceability: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    control_map = {control["id"]: control for control in controls["controls"]}
    raw_suites = traceability.get("suites")
    if not isinstance(raw_suites, list) or not raw_suites:
        raise EvidenceError("Traceability contract must define release suites")
    suite_map: dict[str, dict[str, Any]] = {}
    for suite in raw_suites:
        if not isinstance(suite, dict):
            raise EvidenceError("Every release suite must be an object")
        suite_id = suite.get("id")
        if not isinstance(suite_id, str) or not _SAFE_NAME.fullmatch(suite_id):
            raise EvidenceError(f"Unsafe release suite id: {suite_id}")
        if suite_id in suite_map:
            raise EvidenceError(f"Duplicate release suite id: {suite_id}")
        working_directory = suite.get("working_directory")
        if not isinstance(working_directory, str):
            raise EvidenceError(f"Release suite {suite_id} lacks a working_directory")
        relative = Path(working_directory)
        if relative.is_absolute() or ".." in relative.parts:
            raise EvidenceError(f"Release suite {suite_id} has an unsafe working_directory")
        selector_type = suite.get("selector_type")
        if selector_type not in {"pytest", "javascript", "command"}:
            raise EvidenceError(f"Release suite {suite_id} has an invalid selector_type")
        commands = suite.get("commands")
        if not isinstance(commands, list) or not commands:
            raise EvidenceError(f"Release suite {suite_id} must define commands")
        junit_commands = 0
        for command in commands:
            _validate_command_array(command, f"Release suite {suite_id}")
            if any("{junit_xml}" in part for part in command):
                junit_commands += 1
        timeout = suite.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 14400:
            raise EvidenceError(f"Release suite {suite_id} has an invalid timeout")
        required_environment = suite.get("required_environment", [])
        if (
            not isinstance(required_environment, list)
            or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
                for name in required_environment
            )
            or len(required_environment) != len(set(required_environment))
        ):
            raise EvidenceError(f"Release suite {suite_id} has invalid required_environment")
        required_tools = suite.get("required_tools", {})
        if not isinstance(required_tools, dict) or any(
            not isinstance(name, str)
            or not _SAFE_NAME.fullmatch(name)
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,63}", version)
            for name, version in required_tools.items()
        ):
            raise EvidenceError(f"Release suite {suite_id} has invalid required_tools")
        check_ids = suite.get("check_ids", [])
        if (
            not isinstance(check_ids, list)
            or any(not isinstance(item, str) or not _SAFE_NAME.fullmatch(item) for item in check_ids)
            or len(check_ids) != len(set(check_ids))
        ):
            raise EvidenceError(f"Release suite {suite_id} has invalid check_ids")
        if selector_type == "command" and not check_ids:
            raise EvidenceError(f"Command suite {suite_id} must define check_ids")
        # Selector-verified suites must tell the recorder where their JUnit report lands,
        # because a pass is credited per mapped selector found passing in that report,
        # never from the exit status alone.
        if selector_type in {"pytest", "javascript"} and junit_commands != 1:
            raise EvidenceError(
                f"Release suite {suite_id} must route exactly one command's JUnit output to {{junit_xml}}"
            )
        if selector_type == "command" and junit_commands:
            raise EvidenceError(f"Command suite {suite_id} cannot claim a JUnit report")
        suite_map[suite_id] = suite

    scenario_map = _validate_scenarios(traceability)

    raw_trace_controls = traceability.get("controls")
    if not isinstance(raw_trace_controls, list):
        raise EvidenceError("Traceability contract controls must be a list")
    trace_map: dict[str, dict[str, Any]] = {}
    for traced in raw_trace_controls:
        if not isinstance(traced, dict) or set(traced) != {"id", "evidence"}:
            raise EvidenceError("Every traceability control must contain id and evidence")
        control_id = traced.get("id")
        if not isinstance(control_id, str) or control_id not in control_map:
            raise EvidenceError(f"Traceability references unknown control: {control_id}")
        if control_id in trace_map:
            raise EvidenceError(f"Duplicate traceability control: {control_id}")
        evidence = traced.get("evidence")
        expected_kinds = _control_kinds(control_map[control_id])
        if not isinstance(evidence, dict) or set(evidence) != expected_kinds:
            raise EvidenceError(f"Traceability kinds do not match control {control_id}")
        for kind, requirements in evidence.items():
            if not isinstance(requirements, list) or not requirements:
                raise EvidenceError(f"Traceability {control_id}/{kind} must not be empty")
            if kind.startswith("offline_"):
                seen: set[tuple[str, str]] = set()
                for requirement in requirements:
                    if not isinstance(requirement, dict) or set(requirement) != {"suite", "selector"}:
                        raise EvidenceError(f"Traceability {control_id}/{kind} has an invalid selector")
                    suite_id = requirement.get("suite")
                    selector = requirement.get("selector")
                    suite = suite_map.get(str(suite_id))
                    if suite is None or not isinstance(selector, str) or len(selector) > 500:
                        raise EvidenceError(f"Traceability {control_id}/{kind} has an invalid suite selector")
                    pair = (str(suite_id), selector)
                    if pair in seen:
                        raise EvidenceError(f"Traceability {control_id}/{kind} repeats selector {selector}")
                    seen.add(pair)
                    if selector in suite.get("check_ids", []):
                        continue
                    if suite["selector_type"] not in {"pytest", "javascript"} or "::" not in selector:
                        raise EvidenceError(f"Traceability selector is not declared by suite {suite_id}: {selector}")
                    path_text, test_name = selector.split("::", 1)
                    selector_path = Path(path_text)
                    if (
                        selector_path.is_absolute()
                        or ".." in selector_path.parts
                        or not test_name
                        or len(test_name) > 240
                    ):
                        raise EvidenceError(f"Traceability selector is unsafe: {selector}")
                    suite_root = str(suite["working_directory"]).rstrip("/")
                    if suite_root != "." and not path_text.startswith(f"{suite_root}/"):
                        raise EvidenceError(f"Traceability selector leaves suite {suite_id}: {selector}")
            else:
                if any(not isinstance(item, str) or not _SAFE_NAME.fullmatch(item) for item in requirements) or len(
                    requirements
                ) != len(set(requirements)):
                    raise EvidenceError(f"Traceability {control_id}/{kind} has invalid scenarios")
                undefined = sorted(set(requirements) - set(scenario_map))
                if undefined:
                    raise EvidenceError(
                        f"Traceability {control_id}/{kind} names scenarios with no executable definition: {undefined}"
                    )
        trace_map[control_id] = traced
    if set(trace_map) != set(control_map):
        missing = sorted(set(control_map) - set(trace_map))
        raise EvidenceError(f"Traceability is missing controls: {missing}")
    referenced = {
        item
        for traced in trace_map.values()
        for kind, requirements in traced["evidence"].items()
        if not kind.startswith("offline_")
        for item in requirements
    }
    orphaned = sorted(set(scenario_map) - referenced)
    if orphaned:
        raise EvidenceError(f"Traceability defines scenarios no control requires: {orphaned}")
    return suite_map, trace_map


def _validate_command_array(command: Any, description: str) -> None:
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part or len(part) > 500 or "\x00" in part for part in command)
    ):
        raise EvidenceError(f"{description} has an invalid command array")
    for part in command:
        for name in _PLACEHOLDER.findall(part):
            if name not in _TOOL_PLACEHOLDERS | _CONTEXT_PLACEHOLDERS:
                raise EvidenceError(f"{description} uses an unknown placeholder {{{name}}}")
    head = command[0]
    if not (_PLACEHOLDER.fullmatch(head) and head[1:-1] in _TOOL_PLACEHOLDERS):
        raise EvidenceError(f"{description} must start with a tool placeholder so the executable is recorded by digest")


def _validate_scenarios(traceability: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every live/UI/rollback/teardown/clone/commit scenario is an executable definition.

    A scenario id used to be a bare label; whoever typed ``--status pass`` decided the
    outcome. Now each one names the command the recorder runs, the artifact labels the
    command must leave behind, and whether it must emit live provenance.
    """
    raw = traceability.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise EvidenceError("Traceability contract must define executable scenarios")
    scenario_map: dict[str, dict[str, Any]] = {}
    for scenario in raw:
        if not isinstance(scenario, dict) or set(scenario) != {
            "id",
            "command",
            "timeout_seconds",
            "required_artifacts",
            "provenance",
            "working_directory",
        }:
            raise EvidenceError(
                "Every scenario must define id, command, timeout_seconds, required_artifacts, provenance, working_directory"
            )
        scenario_id = scenario["id"]
        if not isinstance(scenario_id, str) or not _SAFE_NAME.fullmatch(scenario_id):
            raise EvidenceError(f"Unsafe scenario id: {scenario_id}")
        if scenario_id in scenario_map:
            raise EvidenceError(f"Duplicate scenario id: {scenario_id}")
        _validate_command_array(scenario["command"], f"Scenario {scenario_id}")
        if any("{junit_xml}" in part for part in scenario["command"]):
            raise EvidenceError(
                f"Scenario {scenario_id} cannot claim a JUnit report; scenarios verify by exit status and artifacts"
            )
        timeout = scenario["timeout_seconds"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 14400:
            raise EvidenceError(f"Scenario {scenario_id} has an invalid timeout")
        working_directory = scenario["working_directory"]
        relative = Path(working_directory) if isinstance(working_directory, str) else None
        if relative is None or relative.is_absolute() or ".." in relative.parts:
            raise EvidenceError(f"Scenario {scenario_id} has an unsafe working_directory")
        labels = scenario["required_artifacts"]
        if (
            not isinstance(labels, list)
            or any(not isinstance(label, str) or not _SAFE_NAME.fullmatch(label) for label in labels)
            or len(labels) != len(set(labels))
            or len(labels) > 16
        ):
            raise EvidenceError(f"Scenario {scenario_id} has invalid required_artifacts")
        provenance = scenario["provenance"]
        if not isinstance(provenance, bool):
            raise EvidenceError(f"Scenario {scenario_id} provenance must be boolean")
        if provenance and _PROVENANCE_LABEL not in labels:
            raise EvidenceError(
                f"Scenario {scenario_id} requires provenance but does not list the {_PROVENANCE_LABEL} artifact"
            )
        scenario_map[scenario_id] = scenario
    return scenario_map


# ---------------------------------------------------------------------------
# Execution: the recorder runs the contract's commands and writes receipts
# ---------------------------------------------------------------------------


def _resolve_tools(requested: dict[str, str], placeholders: set[str]) -> dict[str, dict[str, Any]]:
    """Resolve each tool placeholder to one absolute executable, recorded by digest."""
    resolved: dict[str, dict[str, Any]] = {}
    for name in sorted(placeholders):
        raw = requested.get(name)
        if raw is None:
            raise EvidenceError(f"Tool {{{name}}} is required by this command set; pass --tool {name}=/absolute/path")
        path = Path(raw)
        if not path.is_absolute():
            raise EvidenceError(f"Tool {{{name}}} must be an absolute path, got {raw!r}")
        try:
            resolved_path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise EvidenceError(f"Tool {{{name}}} does not exist: {raw}") from exc
        if not resolved_path.is_file() or not os.access(resolved_path, os.X_OK):
            raise EvidenceError(f"Tool {{{name}}} is not an executable file: {raw}")
        sha256, size = _secure_artifact_digest(resolved_path)
        resolved[name] = {"path": str(resolved_path), "sha256": sha256, "bytes": size}
    unknown = sorted(set(requested) - _TOOL_PLACEHOLDERS)
    if unknown:
        raise EvidenceError(f"Unknown tool placeholders: {unknown}")
    return resolved


def _tool_version_line(executable: str, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_child_environment({}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"Could not read the version of {executable}") from exc
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:200]


def _verify_required_tools(required: dict[str, str], tools: dict[str, dict[str, Any]], cwd: Path) -> dict[str, str]:
    """Pin ``required_tools`` to the executables actually used, by reported version."""
    versions: dict[str, str] = {}
    for tool_name, expected in sorted(required.items()):
        placeholder = tool_name.replace("-", "_")
        record = tools.get(placeholder)
        executable = record["path"] if record else shutil.which(tool_name)
        if executable is None:
            raise EvidenceError(
                f"Required tool {tool_name} {expected} is not available; pass --tool {placeholder}=/path"
            )
        reported = _tool_version_line(executable, cwd)
        if not re.search(rf"(?<![0-9.]){re.escape(expected)}(?![0-9.])", reported):
            raise EvidenceError(f"Required tool {tool_name} must report version {expected}; got {reported!r}")
        versions[tool_name] = reported
        if record is None:
            sha256, size = _secure_artifact_digest(Path(executable).resolve())
            tools[placeholder] = {"path": str(Path(executable).resolve()), "sha256": sha256, "bytes": size}
    return versions


def _child_environment(required_names: dict[str, str] | list[str]) -> dict[str, str]:
    """A minimal child environment: PATH-class basics plus the suite's declared names.

    Values are passed through to the child but never written to the receipt; the
    receipt records only WHICH names were set.
    """
    env: dict[str, str] = {}
    for name in _PASSTHROUGH_ENVIRONMENT:
        if name in os.environ:
            env[name] = os.environ[name]
    for name in required_names:
        if name not in os.environ:
            raise EvidenceError(f"Required environment variable {name} is not set")
        env[name] = os.environ[name]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("LANG", "C.UTF-8")
    return env


def _substitute(command: list[str], tools: dict[str, dict[str, Any]], context: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    for part in command:

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in tools:
                return tools[name]["path"]
            if name in context:
                return context[name]
            raise EvidenceError(f"Placeholder {{{name}}} is not available for this command")

        resolved.append(_PLACEHOLDER.sub(replace, part))
    return resolved


def _execute(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    """Run one resolved command with no shell, bounded by ``timeout``, capturing to files."""
    started = time.monotonic()
    started_at = _utc()
    timed_out = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    out_fd = os.open(stdout_path, flags, 0o600)
    err_fd = os.open(stderr_path, flags, 0o600)
    try:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_fd,
                stderr=err_fd,
                check=False,
                timeout=timeout,
                shell=False,
            )
            exit_status: int | None = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_status = None
        except OSError as exc:
            raise EvidenceError(f"Command could not start: {argv[0]} ({exc.strerror})") from exc
    finally:
        os.close(out_fd)
        os.close(err_fd)
    return {
        "argv": argv,
        "cwd": str(cwd),
        "started_at": started_at,
        "finished_at": _utc(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_status": exit_status,
        "timed_out": timed_out,
    }


def _junit_passed_cases(junit_path: Path) -> list[dict[str, str]]:
    """Every testcase in a JUnit report that passed outright (no failure/error/skip)."""
    try:
        size = junit_path.stat().st_size
    except FileNotFoundError as exc:
        raise EvidenceError("The suite did not produce its JUnit report") from exc
    if size == 0 or size > _MAX_JUNIT_BYTES:
        raise EvidenceError("The suite's JUnit report is empty or oversized")
    try:
        root = ElementTree.parse(junit_path).getroot()
    except ElementTree.ParseError as exc:
        raise EvidenceError("The suite's JUnit report is not well-formed XML") from exc
    passed: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        if any(child.tag in {"failure", "error", "skipped"} for child in case):
            continue
        passed.append(
            {
                "classname": case.get("classname", ""),
                "name": case.get("name", ""),
                "file": case.get("file", ""),
            }
        )
    return passed


def _selector_matches(selector: str, suite_root: str, case: dict[str, str]) -> bool:
    """Does one passed JUnit testcase satisfy one contract selector?

    pytest (xunit1): ``file`` is the module path relative to its rootdir (the suite's
    working directory); ``name`` carries any parametrization in brackets. vitest:
    ``classname`` is the file path relative to the project; ``name`` is the full title
    joined by " > ". Selectors are project-relative ``path::node``.
    """
    path_text, node = selector.split("::", 1)
    root = suite_root.rstrip("/")
    relative = path_text[len(root) + 1 :] if root != "." and path_text.startswith(f"{root}/") else path_text
    name = case["name"]
    if case["file"]:
        if case["file"] != relative:
            return False
        parts = node.split("::")
        test_name = parts[-1]
        if len(parts) > 1 and not case["classname"].endswith("." + ".".join(parts[:-1])):
            return False
        return name == test_name or name.startswith(f"{test_name}[")
    # vitest shape
    if case["classname"] != relative:
        return False
    return name == node or name.endswith(f" > {node}")


def _verify_selectors(
    selectors: list[str],
    suite: dict[str, Any],
    passed_cases: list[dict[str, str]],
    exit_ok: bool,
) -> tuple[list[str], list[str]]:
    """Split mapped selectors into verified and unverified."""
    verified: list[str] = []
    unverified: list[str] = []
    for selector in selectors:
        if selector in suite.get("check_ids", []):
            (verified if exit_ok else unverified).append(selector)
            continue
        if any(_selector_matches(selector, str(suite["working_directory"]), case) for case in passed_cases):
            verified.append(selector)
        else:
            unverified.append(selector)
    return verified, unverified


def _artifact_tree(root: Path, label: str) -> dict[str, Any]:
    """One bounded digest over every regular file a command left under ``root``."""
    root = root.resolve()
    rows: list[dict[str, Any]] = []
    total = 0
    for path in sorted(p for p in root.rglob("*")):
        if path.is_symlink():
            raise EvidenceError(f"Artifact tree {label} contains a symlink: {path.name}")
        if not path.is_file():
            continue
        if len(rows) >= _MAX_ARTIFACT_FILES:
            raise EvidenceError(f"Artifact tree {label} exceeds {_MAX_ARTIFACT_FILES} files")
        sha256, size = _secure_artifact_digest(path)
        total += size
        if total > _MAX_ARTIFACT_BYTES:
            raise EvidenceError(f"Artifact tree {label} exceeds the 1 GiB bound")
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": sha256, "bytes": size})
    return {"label": label, "type": "tree", "sha256": _digest(rows), "bytes": total, "files": len(rows)}


def _artifact_file(path: Path, label: str) -> dict[str, Any]:
    sha256, size = _secure_artifact_digest(path)
    return {"label": label, "type": "file", "sha256": sha256, "bytes": size}


def _load_provenance(path: Path, *, run: dict[str, Any], region: str) -> dict[str, Any]:
    """Validate the provenance a live scenario emitted and bind it to this run.

    The scenario -- not the operator -- names the account, Region, stack, and deployed
    commit it observed. Those must match the run's bound account, the scenario's
    Region, and the candidate commit/tree, or the receipt cannot credit anything.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise EvidenceError("The live scenario did not emit its provenance artifact") from exc
    if size == 0 or size > 64 * 1024:
        raise EvidenceError("The live scenario's provenance artifact is empty or oversized")
    document = _read_json(path)
    keys = set(document)
    if not _PROVENANCE_REQUIRED_KEYS <= keys or not keys <= _PROVENANCE_REQUIRED_KEYS | _PROVENANCE_OPTIONAL_KEYS:
        raise EvidenceError(
            "Provenance must carry account_id, region, stack_id, deployed_commit, deployed_project_tree"
        )
    for key, value in document.items():
        if not isinstance(value, str) or not value or len(value) > 500 or _TOKEN_VALUE.search(value):
            raise EvidenceError(f"Provenance field {key} is not a bounded plain string")
    bound_account = run.get("account_id")
    if bound_account is None:
        raise EvidenceError("This run is not bound to an AWS account; start it with --account-id before live scenarios")
    if document["account_id"] != bound_account:
        raise EvidenceError("Provenance names a different AWS account than this run is bound to")
    if document["region"] != region:
        raise EvidenceError("Provenance names a different Region than the scenario was run for")
    if (
        not _SAFE_GIT_OBJECT.fullmatch(document["deployed_commit"])
        or document["deployed_commit"] != run["source"]["commit"]
    ):
        raise EvidenceError(
            "The deployed build is not the candidate commit; redeploy the exact candidate before recording"
        )
    if document["deployed_project_tree"] != run["source"]["project_tree"]:
        raise EvidenceError("The deployed build's project tree is not the candidate tree")
    if not _SAFE_NAME.fullmatch(document["stack_id"].lower()) and not re.fullmatch(
        r"arn:aws:cloudformation:[a-z0-9-]+:\d{12}:stack/[A-Za-z0-9-]+/[0-9a-f-]+", document["stack_id"]
    ):
        raise EvidenceError("Provenance stack_id is neither a safe name nor a CloudFormation stack ARN")
    for digest_key in ("archive_sha256", "artifact_manifest_sha256"):
        if digest_key in document and not _SAFE_SHA256.fullmatch(document[digest_key]):
            raise EvidenceError(f"Provenance {digest_key} is not a SHA-256 digest")
    return document


def _contract_fingerprint(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    sha256, size = _sha256_file(path)
    return {
        "path": path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix(),
        "sha256": sha256,
        "bytes": size,
        "document_digest": _digest(value),
    }


def _root_workflow_fingerprint() -> dict[str, Any]:
    repo = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel")).resolve()
    path = repo / ROOT_WORKFLOW_RELATIVE
    try:
        sha256, size = _secure_artifact_digest(path)
    except EvidenceError as exc:
        raise EvidenceError(f"Repository CI workflow is missing or unsafe: {ROOT_WORKFLOW_RELATIVE}") from exc
    return {"path": ROOT_WORKFLOW_RELATIVE, "sha256": sha256, "bytes": size}


def _load_contract_bundle() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    legacy_regions_path = REGIONS_PATH.with_name("emea-agentcore-regions.json")
    if legacy_regions_path.exists() or legacy_regions_path.is_symlink():
        raise EvidenceError("Legacy EMEA Region matrix must be removed; agentcore-regions.json is authoritative")
    controls = _load_contract(CONTROLS_PATH)
    regions = _load_contract(REGIONS_PATH, expected_schema_version=_REGION_MATRIX_SCHEMA_VERSION)
    traceability = _load_contract(TRACEABILITY_PATH)
    _validate_contracts(controls, regions)
    _validate_traceability(controls, traceability)
    fingerprints = {
        "controls": _contract_fingerprint(CONTROLS_PATH, controls),
        "regions": _contract_fingerprint(REGIONS_PATH, regions),
        "traceability": _contract_fingerprint(TRACEABILITY_PATH, traceability),
        "root_workflow": _root_workflow_fingerprint(),
    }
    return controls, regions, traceability, fingerprints


def _runner_identity() -> dict[str, Any]:
    """What produced the receipt: this script by digest, plus interpreter and platform."""
    sha256, size = _secure_artifact_digest(Path(__file__).resolve())
    return {
        "tool": "release_evidence.py",
        "version": RECORDER_VERSION,
        "sha256": sha256,
        "bytes": size,
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}".lower(),
    }


def _check_self_digest(document: dict[str, Any], field: str, description: str) -> None:
    actual = document.get(field)
    if not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{64}", actual):
        raise EvidenceError(f"{description} has an invalid {field}")
    unsigned = dict(document)
    unsigned.pop(field, None)
    if _digest(unsigned) != actual:
        raise EvidenceError(f"{description} integrity digest does not match")


def _safe_existing_directory(path: Path, description: str) -> Path:
    path = path.expanduser()
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError(f"{description} does not exist: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise EvidenceError(f"{description} is not a regular directory: {path}")
    return path


def _create_lock_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _run_lock(run_dir: Path, *, exclusive: bool):
    lock_path = run_dir / ".lock"
    try:
        before = lock_path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError("Evidence run lock is missing") from exc
    if lock_path.is_symlink() or not lock_path.is_file() or before.st_nlink != 1 or before.st_size != 0:
        raise EvidenceError("Evidence run lock is not one empty regular single-link file")
    flags = os.O_RDWR if exclusive else os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EvidenceError("Evidence run lock changed while opening")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_run(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    run = _read_json(run_dir / "run.json")
    if run.get("schema_version") != RUN_SCHEMA_VERSION:
        raise EvidenceError("Unsupported evidence run schema")
    expected_fields = {
        "schema_version",
        "run_id",
        "created_at",
        "source",
        "contracts",
        "account_id",
        "runner",
        "run_digest",
    }
    if set(run) != expected_fields:
        raise EvidenceError("Evidence run document has an invalid schema")
    _require_timestamp(run.get("created_at"), "Evidence run")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not _SAFE_NAME.fullmatch(run_id):
        raise EvidenceError("Evidence run has an unsafe run_id")
    if run_dir.name != run_id:
        raise EvidenceError("Evidence run directory name does not match run_id")
    _check_self_digest(run, "run_digest", "Evidence run")
    source = run.get("source")
    if not isinstance(source, dict):
        raise EvidenceError("Evidence run has no source identity")
    for field in ("project_rel", "commit", "project_tree", "root_workflow", "root_workflow_blob"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise EvidenceError(f"Evidence run source lacks {field}")
    if source["root_workflow"] != ROOT_WORKFLOW_RELATIVE or not _SAFE_GIT_OBJECT.fullmatch(
        source["root_workflow_blob"]
    ):
        raise EvidenceError("Evidence run is not bound to the repository CI workflow")
    contracts = run.get("contracts")
    if not isinstance(contracts, dict) or set(contracts) != {"controls", "regions", "traceability", "root_workflow"}:
        raise EvidenceError("Evidence run contract fingerprints are incomplete")
    account_id = run.get("account_id")
    if account_id is not None and (not isinstance(account_id, str) or not _SAFE_ACCOUNT_ID.fullmatch(account_id)):
        raise EvidenceError("Evidence run account binding is not a 12-digit AWS account ID")
    runner = run.get("runner")
    if (
        not isinstance(runner, dict)
        or runner.get("tool") != "release_evidence.py"
        or not _SAFE_SHA256.fullmatch(str(runner.get("sha256", "")))
    ):
        raise EvidenceError("Evidence run does not record a valid runner identity")
    return run


def _require_run_context(
    run_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    run = _load_run(run_dir)
    _require_same_clean_source(run["source"])
    controls, regions, traceability, fingerprints = _load_contract_bundle()
    if fingerprints != run["contracts"]:
        raise EvidenceError("Release contracts changed after the evidence run started")
    return run, controls, regions, traceability


def start_run(
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    run_id: str | None = None,
    *,
    account_id: str | None = None,
) -> Path:
    identity = _source_identity()
    if not identity["clean"]:
        raise EvidenceError("Project tree or repository CI workflow is dirty; commit it before starting evidence")
    if account_id is not None and not _SAFE_ACCOUNT_ID.fullmatch(account_id):
        raise EvidenceError("--account-id must be a 12-digit AWS account ID")
    _, _, _, fingerprints = _load_contract_bundle()
    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"release-{stamp}-{uuid.uuid4().hex[:12]}"
    if not _SAFE_NAME.fullmatch(run_id):
        raise EvidenceError("run_id must be a safe lowercase name")

    output_root = output_root.expanduser()
    if output_root.exists() and output_root.is_symlink():
        raise EvidenceError("Evidence output root must not be a symlink")
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = output_root / run_id
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise EvidenceError(f"Evidence run already exists: {run_id}") from exc
    _create_lock_file(run_dir / ".lock")
    (run_dir / "events").mkdir(mode=0o700)
    (run_dir / "artifacts").mkdir(mode=0o700)

    run: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _utc(),
        "source": _public_source(identity),
        "contracts": fingerprints,
        # One run, one AWS account. Every live receipt's provenance must name this account,
        # so evidence from two accounts can never be stitched into one certification.
        "account_id": account_id,
        "runner": _runner_identity(),
    }
    run["run_digest"] = _digest(run)
    _write_new(run_dir / "run.json", run)
    return run_dir


def _control_kinds(control: dict[str, Any]) -> set[str]:
    kinds = set(control["required_kinds"])
    regional_kind = control.get("required_region_kind")
    if isinstance(regional_kind, str):
        kinds.add(regional_kind)
    return kinds


def _validate_event_scope(
    control_id: str,
    kind: str,
    region: str | None,
    account_id: str | None,
    control_map: dict[str, dict[str, Any]],
    region_map: dict[str, dict[str, Any]],
) -> None:
    control = control_map.get(control_id)
    if control is None:
        raise EvidenceError(f"Unknown production-readiness control: {control_id}")
    if kind not in _control_kinds(control):
        raise EvidenceError(f"Evidence kind {kind} is not required by {control_id}")
    if region is not None and not _SAFE_REGION.fullmatch(region):
        raise EvidenceError(f"Invalid evidence Region: {region}")
    if kind in _REGION_REQUIRED_KINDS:
        if region is None or region == "local":
            raise EvidenceError(f"Evidence kind {kind} requires an AWS Region")
        if account_id is None or not _SAFE_ACCOUNT_ID.fullmatch(account_id):
            raise EvidenceError(f"Evidence kind {kind} requires a 12-digit AWS account ID")
    else:
        if region not in (None, "local"):
            raise EvidenceError(f"Offline evidence kind {kind} cannot claim an AWS Region")
        if account_id is not None:
            raise EvidenceError(f"Offline evidence kind {kind} cannot claim an AWS account")
    if kind == "live_region":
        entry = region_map.get(str(region))
        selector = control.get("region_selector")
        if entry is None or not selector or entry.get(selector) is not True:
            raise EvidenceError(f"Region {region} is not selected for {control_id} live_region evidence")


def _validate_event_reference(
    control_id: str,
    kind: str,
    suite_id: str | None,
    scenario_id: str | None,
    controls: dict[str, Any],
    traceability: dict[str, Any],
) -> None:
    _, trace_map = _validate_traceability(controls, traceability)
    requirements = trace_map[control_id]["evidence"][kind]
    if kind.startswith("offline_"):
        allowed_suites = {requirement["suite"] for requirement in requirements}
        if suite_id not in allowed_suites:
            raise EvidenceError(f"Evidence {control_id}/{kind} requires one mapped suite: {sorted(allowed_suites)}")
        if scenario_id is not None:
            raise EvidenceError(f"Offline evidence {control_id}/{kind} cannot claim a live scenario")
    else:
        if scenario_id not in requirements:
            raise EvidenceError(f"Evidence {control_id}/{kind} requires one mapped scenario: {requirements}")
        if suite_id is not None:
            raise EvidenceError(f"Non-offline evidence {control_id}/{kind} cannot claim a suite")


def _secure_artifact_digest(path: Path) -> tuple[str, int]:
    path = path.expanduser()
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError(f"Evidence artifact does not exist: {path}") from exc
    if path.is_symlink() or not path.is_file() or before.st_nlink != 1:
        raise EvidenceError(f"Evidence artifact is not one regular single-link file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EvidenceError(f"Evidence artifact changed while opening: {path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 1024 * 1024 * 1024:
                raise EvidenceError("Evidence artifacts are limited to 1 GiB")
            digest.update(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise EvidenceError(f"Evidence artifact changed while hashing: {path}")
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _new_event_id() -> str:
    return f"event-{uuid.uuid4().hex}"


def _write_event(
    run_dir: Path,
    run: dict[str, Any],
    *,
    event_id: str,
    control_id: str,
    kind: str,
    region: str | None,
    account_id: str | None,
    suite_id: str | None,
    scenario_id: str | None,
    details: dict[str, Any],
    artifacts: list[dict[str, Any]],
    receipt: dict[str, Any],
    supersedes: str | None,
) -> dict[str, Any]:
    needs_provenance = kind in _REGION_REQUIRED_KINDS
    event: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "recorded_at": _utc(),
        "control_id": control_id,
        "kind": kind,
        "status": _derived_status(receipt, artifacts, needs_provenance=needs_provenance),
        "region": region,
        "account_id": account_id,
        "suite_id": suite_id,
        "scenario_id": scenario_id,
        "details": details,
        "artifacts": artifacts,
        "receipt": receipt,
        "supersedes": supersedes,
        "source_commit": run["source"]["commit"],
        "project_tree": run["source"]["project_tree"],
        "root_workflow_blob": run["source"]["root_workflow_blob"],
        "run_digest": run["run_digest"],
    }
    event["event_digest"] = _digest(event)
    _write_new(run_dir / "events" / f"{event_id}.json", event)
    return event


def _active_match(
    events: list[dict[str, Any]],
    superseded: set[str],
    *,
    control_id: str,
    kind: str,
    region: str | None,
    account_id: str | None,
    suite_id: str | None,
    scenario_id: str | None,
) -> str | None:
    """The active event this new one replaces, if the same scope was recorded before."""
    for event in events:
        if event["event_id"] in superseded:
            continue
        if (
            event["control_id"],
            event["kind"],
            event["region"],
            event["account_id"],
            event["suite_id"],
            event["scenario_id"],
        ) == (control_id, kind, region, account_id, suite_id, scenario_id):
            return event["event_id"]
    return None


def _prepare_execution(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    if (run_dir / "final.json").exists():
        raise EvidenceError("Finalized evidence runs are immutable")
    run, controls, regions, traceability = _require_run_context(run_dir)
    if _runner_identity() != run["runner"]:
        raise EvidenceError("The recorder is not the one that started this run")
    return run, controls, regions, traceability


def _run_suite_unlocked(
    run_dir: Path,
    *,
    suite_id: str,
    tools: dict[str, str],
    details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute one offline suite and record one receipt-backed event per mapped (control, kind).

    Credit is per mapped selector: every selector the traceability maps to this suite must
    appear as a passed testcase in the JUnit report the suite wrote (or, for a command suite,
    be one of its check_ids with every command exiting 0). Anything else records a fail.
    """
    run, controls, regions, traceability = _prepare_execution(run_dir)
    suite_map, trace_map = _validate_traceability(controls, traceability)
    suite = suite_map.get(suite_id)
    if suite is None:
        raise EvidenceError(f"Unknown release suite: {suite_id}")
    clean_details = _validate_details(details or {})
    event_id = _new_event_id()
    artifact_dir = run_dir / "artifacts" / event_id
    artifact_dir.mkdir(mode=0o700)
    cwd = (PROJECT_ROOT / str(suite["working_directory"])).resolve()
    if not cwd.is_dir():
        raise EvidenceError(f"Release suite {suite_id} working_directory does not exist")

    placeholders = {name for command in suite["commands"] for part in command for name in _PLACEHOLDER.findall(part)}
    required_tools = dict(suite.get("required_tools", {}))
    # A pinned tool the operator supplied explicitly is resolved and digested even when no
    # command placeholder names it, so its version is checked against THAT executable.
    pinned = {name.replace("-", "_") for name in required_tools} & set(tools) & _TOOL_PLACEHOLDERS
    tool_records = _resolve_tools(tools, (placeholders & _TOOL_PLACEHOLDERS) | pinned)
    tool_versions = _verify_required_tools(required_tools, tool_records, cwd)
    environment_names = list(suite.get("required_environment", []))
    env = _child_environment(environment_names)
    junit_path = artifact_dir / "junit.xml"
    context = {
        "artifact_dir": str(artifact_dir / "output"),
        "junit_xml": str(junit_path),
        "run_dir": str(run_dir.resolve()),
    }
    (artifact_dir / "output").mkdir(mode=0o700)

    command_records: list[dict[str, Any]] = []
    for index, command in enumerate(suite["commands"]):
        argv = _substitute(command, tool_records, context)
        record = _execute(
            argv,
            cwd=cwd,
            timeout=int(suite["timeout_seconds"]),
            env=env,
            stdout_path=artifact_dir / f"stdout-{index}.log",
            stderr_path=artifact_dir / f"stderr-{index}.log",
        )
        command_records.append(record)
        if record["timed_out"] or record["exit_status"] != 0:
            break  # a failed command is recorded; later commands are not pretended to have run

    exit_ok = all(not r["timed_out"] and r["exit_status"] == 0 for r in command_records) and len(
        command_records
    ) == len(suite["commands"])
    passed_cases: list[dict[str, str]] = []
    if suite["selector_type"] in {"pytest", "javascript"} and junit_path.exists():
        passed_cases = _junit_passed_cases(junit_path)

    # Every file artifact is addressed as artifacts/<event_id>/<label>; the label IS the filename,
    # so a later load re-digests exactly the file the receipt names.
    artifacts: list[dict[str, Any]] = []
    for index in range(len(command_records)):
        for stream in ("stdout", "stderr"):
            artifacts.append(_artifact_file(artifact_dir / f"{stream}-{index}.log", f"{stream}-{index}.log"))
    if junit_path.exists():
        artifacts.append(_artifact_file(junit_path, "junit.xml"))
    output_tree = _artifact_tree(artifact_dir / "output", "output")
    if output_tree["files"]:
        artifacts.append(output_tree)
    else:
        shutil.rmtree(artifact_dir / "output")

    events, superseded = _load_events(run_dir, run, controls, regions, traceability)
    written: list[dict[str, Any]] = []
    first = True
    for control_id, traced in sorted(trace_map.items()):
        for kind, requirements in traced["evidence"].items():
            if not kind.startswith("offline_"):
                continue
            selectors = sorted({req["selector"] for req in requirements if req["suite"] == suite_id})
            if not selectors:
                continue
            verified, unverified = _verify_selectors(selectors, suite, passed_cases, exit_ok)
            this_event_id = event_id if first else _new_event_id()
            if not first:
                # One execution, several (control, kind) events: each carries its own byte-identical
                # copy of the artifacts so every event re-verifies from disk independently.
                shutil.copytree(artifact_dir, run_dir / "artifacts" / this_event_id, symlinks=False)
            receipt = {
                "receipt_id": this_event_id,
                "runner": run["runner"],
                "tools": tool_records,
                "tool_versions": tool_versions,
                "environment_names": environment_names,
                "commands": command_records,
                "selectors_verified": verified,
                "selectors_unverified": unverified,
                "provenance": None,
            }
            supersedes = _active_match(
                events,
                superseded,
                control_id=control_id,
                kind=kind,
                region=None,
                account_id=None,
                suite_id=suite_id,
                scenario_id=None,
            )
            written.append(
                _write_event(
                    run_dir,
                    run,
                    event_id=this_event_id,
                    control_id=control_id,
                    kind=kind,
                    region=None,
                    account_id=None,
                    suite_id=suite_id,
                    scenario_id=None,
                    details=clean_details,
                    artifacts=artifacts,
                    receipt=receipt,
                    supersedes=supersedes,
                )
            )
            first = False
    if first:
        shutil.rmtree(artifact_dir)
        raise EvidenceError(f"Release suite {suite_id} is not mapped by any control; nothing to record")
    return written


def _run_scenario_unlocked(
    run_dir: Path,
    *,
    scenario_id: str,
    region: str | None,
    tools: dict[str, str],
    details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute one scenario and record one receipt-backed event per mapped (control, kind).

    Live kinds require the run to be bound to an account and the scenario to emit a
    provenance artifact naming that account, this Region, and the candidate commit/tree.
    """
    run, controls, regions, traceability = _prepare_execution(run_dir)
    _, trace_map = _validate_traceability(controls, traceability)
    scenario_map = _validate_scenarios(traceability)
    control_map, region_map = _validate_contracts(controls, regions)
    scenario = scenario_map.get(scenario_id)
    if scenario is None:
        raise EvidenceError(f"Unknown scenario: {scenario_id}")
    mapped = [
        (control_id, kind)
        for control_id, traced in sorted(trace_map.items())
        for kind, requirements in traced["evidence"].items()
        if not kind.startswith("offline_") and scenario_id in requirements
    ]
    if not mapped:
        raise EvidenceError(f"Scenario {scenario_id} is not mapped by any control; nothing to record")
    kinds = {kind for _, kind in mapped}
    regional = any(kind in _REGION_REQUIRED_KINDS for kind in kinds)
    if regional and not all(kind in _REGION_REQUIRED_KINDS for kind in kinds):
        raise EvidenceError(f"Scenario {scenario_id} is mapped to both regional and non-regional kinds")
    account_id = run.get("account_id") if regional else None
    if regional:
        if region is None or not _SAFE_REGION.fullmatch(region) or region == "local":
            raise EvidenceError(f"Scenario {scenario_id} requires --region")
        if account_id is None:
            raise EvidenceError(
                "This run is not bound to an AWS account; start it with --account-id before live scenarios"
            )
        for control_id, kind in mapped:
            _validate_event_scope(control_id, kind, region, account_id, control_map, region_map)
    elif region is not None:
        raise EvidenceError(f"Scenario {scenario_id} is offline and cannot take --region")

    clean_details = _validate_details(details or {})
    event_id = _new_event_id()
    artifact_dir = run_dir / "artifacts" / event_id
    artifact_dir.mkdir(mode=0o700)
    (artifact_dir / "output").mkdir(mode=0o700)
    cwd = (PROJECT_ROOT / str(scenario["working_directory"])).resolve()
    if not cwd.is_dir():
        raise EvidenceError(f"Scenario {scenario_id} working_directory does not exist")
    placeholders = {name for part in scenario["command"] for name in _PLACEHOLDER.findall(part)}
    tool_records = _resolve_tools(tools, placeholders & _TOOL_PLACEHOLDERS)
    context = {
        "artifact_dir": str(artifact_dir / "output"),
        "run_dir": str(run_dir.resolve()),
        "scenario_id": scenario_id,
    }
    if regional:
        context["region"] = str(region)
        context["account_id"] = str(account_id)
    argv = _substitute(scenario["command"], tool_records, context)
    record = _execute(
        argv,
        cwd=cwd,
        timeout=int(scenario["timeout_seconds"]),
        env=_child_environment([]),
        stdout_path=artifact_dir / "stdout-0.log",
        stderr_path=artifact_dir / "stderr-0.log",
    )

    artifacts = [
        _artifact_file(artifact_dir / "stdout-0.log", "stdout-0.log"),
        _artifact_file(artifact_dir / "stderr-0.log", "stderr-0.log"),
    ]
    output_dir = artifact_dir / "output"
    provenance: dict[str, Any] | None = None
    missing_labels: list[str] = []
    exit_ok = not record["timed_out"] and record["exit_status"] == 0
    for label in scenario["required_artifacts"]:
        candidate = output_dir / label
        if candidate.is_symlink():
            raise EvidenceError(f"Scenario {scenario_id} emitted {label} as a symlink")
        if candidate.is_file() or candidate.is_dir():
            # Promote the promised artifact to its canonical path artifacts/<event_id>/<label>.
            canonical = artifact_dir / label
            os.rename(candidate, canonical)
            artifacts.append(
                _artifact_file(canonical, label) if canonical.is_file() else _artifact_tree(canonical, label)
            )
        else:
            missing_labels.append(label)
    remaining = _artifact_tree(output_dir, "output")
    if remaining["files"]:
        artifacts.append(remaining)
    else:
        shutil.rmtree(output_dir)
    if exit_ok and scenario["provenance"] and _PROVENANCE_LABEL not in missing_labels:
        provenance = _load_provenance(artifact_dir / _PROVENANCE_LABEL, run=run, region=str(region))
    if exit_ok and missing_labels:
        # The command claimed success without leaving the evidence it promised: not a pass.
        record = {**record, "exit_status": 1}
        clean_details = {**clean_details, "missing_required_artifacts": ",".join(missing_labels)[:500]}

    events, superseded = _load_events(run_dir, run, controls, regions, traceability)
    written: list[dict[str, Any]] = []
    first = True
    for control_id, kind in mapped:
        this_event_id = event_id if first else _new_event_id()
        if not first:
            shutil.copytree(artifact_dir, run_dir / "artifacts" / this_event_id, symlinks=False)
        receipt = {
            "receipt_id": this_event_id,
            "runner": run["runner"],
            "tools": tool_records,
            "tool_versions": {},
            "environment_names": [],
            "commands": [record],
            "selectors_verified": [],
            "selectors_unverified": [],
            "provenance": provenance,
        }
        supersedes = _active_match(
            events,
            superseded,
            control_id=control_id,
            kind=kind,
            region=region if regional else None,
            account_id=account_id,
            suite_id=None,
            scenario_id=scenario_id,
        )
        written.append(
            _write_event(
                run_dir,
                run,
                event_id=this_event_id,
                control_id=control_id,
                kind=kind,
                region=region if regional else None,
                account_id=account_id,
                suite_id=None,
                scenario_id=scenario_id,
                details=clean_details,
                artifacts=artifacts,
                receipt=receipt,
                supersedes=supersedes,
            )
        )
        first = False
    return written


_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "recorded_at",
        "control_id",
        "kind",
        "status",
        "region",
        "account_id",
        "suite_id",
        "scenario_id",
        "details",
        "artifacts",
        "receipt",
        "supersedes",
        "source_commit",
        "project_tree",
        "root_workflow_blob",
        "run_digest",
        "event_digest",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "receipt_id",
        "runner",
        "tools",
        "tool_versions",
        "environment_names",
        "commands",
        "selectors_verified",
        "selectors_unverified",
        "provenance",
    }
)
_COMMAND_RECORD_FIELDS = frozenset(
    {"argv", "cwd", "started_at", "finished_at", "duration_seconds", "exit_status", "timed_out"}
)


def _derived_status(receipt: dict[str, Any], artifacts: list[dict[str, Any]], *, needs_provenance: bool) -> str:
    """The ONLY way an event's status is computed, at write time and again at every load."""
    commands = receipt["commands"]
    if any(command["timed_out"] for command in commands):
        return "fail"
    statuses = [command["exit_status"] for command in commands]
    if any(status == BLOCKED_EXIT_STATUS for status in statuses):
        return "blocked"
    if any(status != 0 for status in statuses):
        return "fail"
    if not artifacts:
        return "fail"
    if receipt["selectors_unverified"]:
        return "fail"
    if needs_provenance and receipt["provenance"] is None:
        return "fail"
    return "pass"


def _validate_event_document(
    event: dict[str, Any],
    filename: str,
    run: dict[str, Any],
    run_dir: Path,
    control_map: dict[str, dict[str, Any]],
    region_map: dict[str, dict[str, Any]],
    controls: dict[str, Any],
    traceability: dict[str, Any],
) -> None:
    if set(event) != _EVENT_FIELDS or event.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise EvidenceError(f"Evidence event {filename} has an invalid schema")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not _SAFE_NAME.fullmatch(event_id):
        raise EvidenceError(f"Evidence event {filename} has an invalid event_id")
    if filename != f"{event_id}.json":
        raise EvidenceError(f"Evidence event filename does not match {event_id}")
    _check_self_digest(event, "event_digest", f"Evidence event {event_id}")
    _require_timestamp(event.get("recorded_at"), f"Evidence event {event_id}")

    control_id = event.get("control_id")
    kind = event.get("kind")
    region = event.get("region")
    account_id = event.get("account_id")
    suite_id = event.get("suite_id")
    scenario_id = event.get("scenario_id")
    if not isinstance(control_id, str) or not isinstance(kind, str):
        raise EvidenceError(f"Evidence event {event_id} has an invalid scope")
    for label, value in (
        ("Region", region),
        ("AWS account ID", account_id),
        ("suite_id", suite_id),
        ("scenario_id", scenario_id),
    ):
        if value is not None and not isinstance(value, str):
            raise EvidenceError(f"Evidence event {event_id} has an invalid {label}")
    _validate_event_scope(control_id, kind, region, account_id, control_map, region_map)
    _validate_event_reference(control_id, kind, suite_id, scenario_id, controls, traceability)
    if event.get("status") not in _STATUS_VALUES:
        raise EvidenceError(f"Evidence event {event_id} has an invalid status")
    if account_id is not None and account_id != run.get("account_id"):
        raise EvidenceError(f"Evidence event {event_id} names an AWS account this run is not bound to")

    details = event.get("details")
    if not isinstance(details, dict) or _validate_details(details) != details:
        raise EvidenceError(f"Evidence event {event_id} has unsafe details")

    # Artifacts live INSIDE the run and are re-digested from disk on every load.
    artifacts = event.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > 32:
        raise EvidenceError(f"Evidence event {event_id} has invalid artifacts")
    artifact_dir = run_dir / "artifacts" / event_id
    if artifacts and (artifact_dir.is_symlink() or not artifact_dir.is_dir()):
        raise EvidenceError(f"Evidence event {event_id} artifacts directory is missing")
    labels: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not {"label", "type", "sha256", "bytes"} <= set(artifact):
            raise EvidenceError(f"Evidence event {event_id} has an invalid artifact")
        label = artifact.get("label")
        if not isinstance(label, str) or not _SAFE_NAME.fullmatch(label) or label in labels:
            raise EvidenceError(f"Evidence event {event_id} has an invalid artifact label")
        labels.add(label)
        if artifact["type"] == "file":
            if set(artifact) != {"label", "type", "sha256", "bytes"}:
                raise EvidenceError(f"Evidence event {event_id} artifact {label} has extra fields")
            actual = _artifact_file(artifact_dir / label, label)
        elif artifact["type"] == "tree":
            if set(artifact) != {"label", "type", "sha256", "bytes", "files"}:
                raise EvidenceError(f"Evidence event {event_id} artifact {label} has extra fields")
            actual = _artifact_tree(artifact_dir / label, label)
        else:
            raise EvidenceError(f"Evidence event {event_id} artifact {label} has an unknown type")
        if actual != artifact:
            raise EvidenceError(f"Evidence event {event_id} artifact {label} no longer matches its recorded digest")

    receipt = event.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise EvidenceError(f"Evidence event {event_id} has no complete execution receipt")
    if not isinstance(receipt["receipt_id"], str) or receipt["receipt_id"] != event_id:
        raise EvidenceError(f"Evidence event {event_id} receipt is not its own")
    runner = receipt["runner"]
    if runner != run["runner"]:
        raise EvidenceError(f"Evidence event {event_id} was not produced by this run's recorder")
    tools = receipt["tools"]
    if not isinstance(tools, dict) or any(
        name not in _TOOL_PLACEHOLDERS
        or not isinstance(record, dict)
        or set(record) != {"path", "sha256", "bytes"}
        or not _SAFE_SHA256.fullmatch(str(record["sha256"]))
        for name, record in tools.items()
    ):
        raise EvidenceError(f"Evidence event {event_id} records invalid tool identities")
    if not isinstance(receipt["tool_versions"], dict) or not isinstance(receipt["environment_names"], list):
        raise EvidenceError(f"Evidence event {event_id} has an invalid receipt")
    commands = receipt["commands"]
    if not isinstance(commands, list) or not commands or len(commands) > 32:
        raise EvidenceError(f"Evidence event {event_id} receipt has no commands")
    for command in commands:
        if not isinstance(command, dict) or set(command) != _COMMAND_RECORD_FIELDS:
            raise EvidenceError(f"Evidence event {event_id} receipt has an invalid command record")
        if (
            not isinstance(command["argv"], list)
            or not command["argv"]
            or not all(isinstance(p, str) for p in command["argv"])
        ):
            raise EvidenceError(f"Evidence event {event_id} receipt has an invalid argv")
        if not isinstance(command["timed_out"], bool) or (
            command["exit_status"] is not None
            and (not isinstance(command["exit_status"], int) or isinstance(command["exit_status"], bool))
        ):
            raise EvidenceError(f"Evidence event {event_id} receipt has an invalid exit record")
        _require_timestamp(command["started_at"], f"Evidence event {event_id} command")
        _require_timestamp(command["finished_at"], f"Evidence event {event_id} command")
    for key in ("selectors_verified", "selectors_unverified"):
        if not isinstance(receipt[key], list) or any(not isinstance(item, str) for item in receipt[key]):
            raise EvidenceError(f"Evidence event {event_id} receipt has invalid {key}")
    provenance = receipt["provenance"]
    needs_provenance = kind in _REGION_REQUIRED_KINDS
    if provenance is not None:
        if not isinstance(provenance, dict) or not needs_provenance:
            raise EvidenceError(f"Evidence event {event_id} carries provenance it cannot have")
        if provenance.get("account_id") != account_id or provenance.get("region") != region:
            raise EvidenceError(f"Evidence event {event_id} provenance does not match its scope")
        if (
            provenance.get("deployed_commit") != run["source"]["commit"]
            or provenance.get("deployed_project_tree") != run["source"]["project_tree"]
        ):
            raise EvidenceError(f"Evidence event {event_id} provenance names a build that is not the candidate")

    # Re-derive the status from the facts; a stored label never wins.
    if event["status"] != _derived_status(receipt, artifacts, needs_provenance=needs_provenance):
        raise EvidenceError(f"Evidence event {event_id} status does not follow from its receipt")

    supersedes = event.get("supersedes")
    if supersedes is not None and (not isinstance(supersedes, str) or not _SAFE_NAME.fullmatch(supersedes)):
        raise EvidenceError(f"Evidence event {event_id} has an invalid supersedes value")
    if (
        event.get("source_commit") != run["source"]["commit"]
        or event.get("project_tree") != run["source"]["project_tree"]
        or event.get("root_workflow_blob") != run["source"]["root_workflow_blob"]
        or event.get("run_digest") != run["run_digest"]
    ):
        raise EvidenceError(f"Evidence event {event_id} is not bound to this run")


def _load_events(
    run_dir: Path,
    run: dict[str, Any],
    controls: dict[str, Any],
    regions: dict[str, Any],
    traceability: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    events_dir = _safe_existing_directory(run_dir / "events", "Evidence events directory")
    control_map, region_map = _validate_contracts(controls, regions)
    events: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(events_dir.iterdir(), key=lambda item: item.name):
        if path.suffix != ".json" or not path.is_file() or path.is_symlink():
            raise EvidenceError(f"Unexpected entry in evidence events directory: {path.name}")
        event = _read_json(path)
        _validate_event_document(
            event,
            path.name,
            run,
            run_dir,
            control_map,
            region_map,
            controls,
            traceability,
        )
        event_id = event["event_id"]
        if event_id in by_id:
            raise EvidenceError(f"Duplicate evidence event id: {event_id}")
        by_id[event_id] = event
        events.append(event)

    superseded: set[str] = set()
    for event in events:
        target_id = event["supersedes"]
        if target_id is None:
            continue
        target = by_id.get(target_id)
        if target is None:
            raise EvidenceError(f"Evidence event {event['event_id']} supersedes an unknown event")
        if target_id in superseded:
            raise EvidenceError(f"Evidence event {target_id} is superseded more than once")
        if target_id == event["event_id"]:
            raise EvidenceError("An evidence event cannot supersede itself")
        scope = ("control_id", "kind", "region", "account_id", "suite_id", "scenario_id")
        if any(target[field] != event[field] for field in scope):
            raise EvidenceError(f"Evidence event {event['event_id']} changes scope while superseding {target_id}")
        superseded.add(target_id)

    for event in events:
        visited: set[str] = set()
        cursor: dict[str, Any] | None = event
        while cursor is not None and cursor.get("supersedes") is not None:
            target_id = cursor["supersedes"]
            if target_id in visited:
                raise EvidenceError("Evidence supersession graph contains a cycle")
            visited.add(target_id)
            cursor = by_id.get(target_id)
    return events, superseded


def _validate_run_entries(run_dir: Path, *, finalized: bool) -> None:
    allowed = {".lock", "run.json", "events", "artifacts"}
    if finalized:
        allowed.add("final.json")
    actual = {path.name for path in run_dir.iterdir()}
    unexpected = actual - allowed
    missing = allowed - actual
    if unexpected or missing:
        raise EvidenceError(
            f"Evidence run entries do not match its state (missing={sorted(missing)}, unexpected={sorted(unexpected)})"
        )


def _coverage_report(
    controls: dict[str, Any],
    regions: dict[str, Any],
    traceability: dict[str, Any],
    events: list[dict[str, Any]],
    superseded: set[str],
) -> dict[str, Any]:
    control_map, region_map = _validate_contracts(controls, regions)
    _, trace_map = _validate_traceability(controls, traceability)
    active = [event for event in events if event["event_id"] not in superseded]
    # ``status == "pass"`` is trustworthy here only because _validate_event_document re-derived
    # it from the receipt (exit statuses, artifacts on disk, verified selectors, provenance)
    # before the event was admitted into ``events``. A label alone never reaches this list.
    passing = [event for event in active if event["status"] == "pass"]
    missing: list[dict[str, Any]] = []

    # One run, one account, one deployed build: every live receipt must agree.
    live_accounts = {event["account_id"] for event in active if event["kind"] in _REGION_REQUIRED_KINDS}
    if len(live_accounts) > 1:
        raise EvidenceError(f"Live evidence names more than one AWS account: {sorted(live_accounts)}")
    deployed = {
        (event["receipt"]["provenance"]["deployed_commit"], event["receipt"]["provenance"]["deployed_project_tree"])
        for event in passing
        if event["receipt"].get("provenance")
    }
    if len(deployed) > 1:
        raise EvidenceError("Live evidence names more than one deployed build")

    def has_pass(
        control_id: str,
        kind: str,
        *,
        suite_id: str | None = None,
        scenario_id: str | None = None,
        region: str | None = None,
    ) -> bool:
        return any(
            event["control_id"] == control_id
            and event["kind"] == kind
            and (suite_id is None or event["suite_id"] == suite_id)
            and (scenario_id is None or event["scenario_id"] == scenario_id)
            and (region is None or event["region"] == region)
            for event in passing
        )

    def add_missing(
        scope: str,
        control_id: str | None,
        kind: str,
        *,
        suite_id: str | None = None,
        scenario_id: str | None = None,
        region: str | None = None,
    ) -> None:
        missing.append(
            {
                "scope": scope,
                "control_id": control_id,
                "kind": kind,
                "region": region,
                "suite_id": suite_id,
                "scenario_id": scenario_id,
            }
        )

    for control_id, control in sorted(control_map.items()):
        traced = trace_map[control_id]["evidence"]
        for kind in control["required_kinds"]:
            requirements = traced[kind]
            if kind.startswith("offline_"):
                for suite_id in sorted({requirement["suite"] for requirement in requirements}):
                    if not has_pass(control_id, kind, suite_id=suite_id):
                        add_missing("suite", control_id, kind, suite_id=suite_id)
            else:
                for scenario_id in requirements:
                    if not has_pass(control_id, kind, scenario_id=scenario_id):
                        add_missing("scenario", control_id, kind, scenario_id=scenario_id)

        regional_kind = control.get("required_region_kind")
        selector = control.get("region_selector")
        if isinstance(regional_kind, str) and isinstance(selector, str):
            for region, entry in sorted(region_map.items()):
                if entry.get(selector) is not True:
                    continue
                for scenario_id in traced[regional_kind]:
                    if not has_pass(
                        control_id,
                        regional_kind,
                        scenario_id=scenario_id,
                        region=region,
                    ):
                        add_missing(
                            "region",
                            control_id,
                            regional_kind,
                            scenario_id=scenario_id,
                            region=region,
                        )

    for kind in controls["global_required_kinds"]:
        if not any(event["kind"] == kind for event in passing):
            add_missing("global", None, kind)

    open_events = [
        {
            "event_id": event["event_id"],
            "control_id": event["control_id"],
            "kind": event["kind"],
            "region": event["region"],
            "account_id": event["account_id"],
            "suite_id": event["suite_id"],
            "scenario_id": event["scenario_id"],
            "status": event["status"],
        }
        for event in active
        if event["status"] != "pass"
    ]
    by_kind: dict[str, int] = {}
    for event in passing:
        by_kind[event["kind"]] = by_kind.get(event["kind"], 0) + 1
    return {
        "complete": not missing and not open_events,
        "control_count": len(control_map),
        "matrix_region_count": len(region_map),
        "total_event_count": len(events),
        "active_event_count": len(active),
        "superseded_event_count": len(superseded),
        "active_pass_count": len(passing),
        "active_pass_by_kind": dict(sorted(by_kind.items())),
        "missing": missing,
        "open_events": sorted(
            open_events,
            key=lambda item: (
                str(item["control_id"]),
                str(item["kind"]),
                str(item["region"]),
                str(item["suite_id"]),
                str(item["scenario_id"]),
                str(item["event_id"]),
            ),
        ),
    }


def _event_set_digest(events: list[dict[str, Any]]) -> str:
    return _digest(
        [
            {"event_id": event["event_id"], "event_digest": event["event_digest"]}
            for event in sorted(events, key=lambda item: item["event_id"])
        ]
    )


def _status_run_unlocked(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    finalized = (run_dir / "final.json").exists()
    _validate_run_entries(run_dir, finalized=finalized)
    run, controls, regions, traceability = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions, traceability)
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run["run_id"],
        "source": run["source"],
        "account_id": run["account_id"],
        "finalized": finalized,
        "event_set_digest": _event_set_digest(events),
        "coverage": _coverage_report(controls, regions, traceability, events, superseded),
    }


def _finalize_run_unlocked(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    if (run_dir / "final.json").exists():
        raise EvidenceError("Evidence run is already finalized")
    _validate_run_entries(run_dir, finalized=False)
    run, controls, regions, traceability = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions, traceability)
    coverage = _coverage_report(controls, regions, traceability, events, superseded)
    if not coverage["complete"]:
        raise EvidenceError(
            "Evidence coverage is incomplete "
            f"({len(coverage['missing'])} missing, {len(coverage['open_events'])} open failures)"
        )
    final: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run["run_id"],
        "finalized_at": _utc(),
        "status": "pass",
        "source": run["source"],
        "contracts": run["contracts"],
        "account_id": run["account_id"],
        "runner": run["runner"],
        "run_digest": run["run_digest"],
        "event_set_digest": _event_set_digest(events),
        "coverage": coverage,
    }
    final["final_digest"] = _digest(final)
    _write_new(run_dir / "final.json", final)
    return final


def _verify_run_unlocked(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    if not (run_dir / "final.json").exists():
        raise EvidenceError("Evidence run is not finalized")
    _validate_run_entries(run_dir, finalized=True)
    run, controls, regions, traceability = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions, traceability)
    final = _read_json(run_dir / "final.json")
    expected_fields = {
        "schema_version",
        "run_id",
        "finalized_at",
        "status",
        "source",
        "contracts",
        "account_id",
        "runner",
        "run_digest",
        "event_set_digest",
        "coverage",
        "final_digest",
    }
    if set(final) != expected_fields or final.get("schema_version") != RUN_SCHEMA_VERSION:
        raise EvidenceError("Final evidence document has an invalid schema")
    _require_timestamp(final.get("finalized_at"), "Final evidence document")
    _check_self_digest(final, "final_digest", "Final evidence document")
    coverage = _coverage_report(controls, regions, traceability, events, superseded)
    expected_values = {
        "run_id": run["run_id"],
        "status": "pass",
        "source": run["source"],
        "contracts": run["contracts"],
        "account_id": run["account_id"],
        "runner": run["runner"],
        "run_digest": run["run_digest"],
        "event_set_digest": _event_set_digest(events),
        "coverage": coverage,
    }
    for field, expected in expected_values.items():
        if final.get(field) != expected:
            raise EvidenceError(f"Final evidence no longer matches {field}")
    if not coverage["complete"]:
        raise EvidenceError("Final evidence coverage is no longer complete")
    return final


def run_suite(run_dir: Path, **kwargs: Any) -> list[dict[str, Any]]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=True):
        return _run_suite_unlocked(run_dir, **kwargs)


def run_scenario(run_dir: Path, **kwargs: Any) -> list[dict[str, Any]]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=True):
        return _run_scenario_unlocked(run_dir, **kwargs)


def status_run(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=False):
        return _status_run_unlocked(run_dir)


def finalize_run(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=True):
        return _finalize_run_unlocked(run_dir)


def verify_run(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=False):
        return _verify_run_unlocked(run_dir)


def _parse_detail(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise EvidenceError("Evidence details must use key=value")
    key, value_text = raw.split("=", 1)
    try:
        value = json.loads(value_text)
    except json.JSONDecodeError:
        value = value_text
    if isinstance(value, (dict, list)):
        raise EvidenceError(f"Evidence detail {key} must be a scalar")
    return key, value


def _parse_assignments(values: list[str], *, tools: bool = False) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in values:
        if "=" not in raw:
            noun = "tools" if tools else "details"
            raise EvidenceError(f"Evidence {noun} must use key=value")
        key, raw_value = raw.split("=", 1)
        if key in parsed:
            raise EvidenceError(f"Duplicate evidence field: {key}")
        if tools:
            if key not in _TOOL_PLACEHOLDERS:
                raise EvidenceError(f"Unknown tool placeholder: {key}")
            parsed[key] = raw_value
        else:
            parsed_key, parsed_value = _parse_detail(raw)
            parsed[parsed_key] = parsed_value
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute the release contract's suites and scenarios and record receipt-backed, "
            "exact-commit production-readiness evidence. There is no way to assert a pass."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a clean exact-commit evidence run")
    start.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    start.add_argument("--run-id")
    start.add_argument("--account-id", help="The one AWS account every live receipt must name")

    suite = subparsers.add_parser("run-suite", help="Execute one offline suite and record its receipts")
    suite.add_argument("--run-dir", type=Path, required=True)
    suite.add_argument("--suite-id", required=True)
    suite.add_argument("--tool", action="append", default=[], help="placeholder=/absolute/executable")
    suite.add_argument("--detail", action="append", default=[])

    scenario = subparsers.add_parser("run-scenario", help="Execute one scenario and record its receipts")
    scenario.add_argument("--run-dir", type=Path, required=True)
    scenario.add_argument("--scenario-id", required=True)
    scenario.add_argument("--region")
    scenario.add_argument("--tool", action="append", default=[], help="placeholder=/absolute/executable")
    scenario.add_argument("--detail", action="append", default=[])

    for command in ("status", "finalize", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            run_dir = start_run(args.output_root, args.run_id, account_id=args.account_id)
            result: dict[str, Any] = {"run_dir": str(run_dir.resolve())}
        elif args.command == "run-suite":
            events = run_suite(
                args.run_dir,
                suite_id=args.suite_id,
                tools=_parse_assignments(args.tool, tools=True),
                details=_parse_assignments(args.detail),
            )
            result = {"events": [{k: e[k] for k in ("event_id", "control_id", "kind", "status")} for e in events]}
        elif args.command == "run-scenario":
            events = run_scenario(
                args.run_dir,
                scenario_id=args.scenario_id,
                region=args.region,
                tools=_parse_assignments(args.tool, tools=True),
                details=_parse_assignments(args.detail),
            )
            result = {
                "events": [{k: e[k] for k in ("event_id", "control_id", "kind", "region", "status")} for e in events]
            }
        elif args.command == "status":
            result = status_run(args.run_dir)
        elif args.command == "finalize":
            result = finalize_run(args.run_dir)
        else:
            result = verify_run(args.run_dir)
    except (EvidenceError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"release_evidence: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
