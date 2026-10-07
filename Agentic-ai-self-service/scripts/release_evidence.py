#!/usr/bin/env python3
"""Append-only, exact-commit evidence recorder for production-readiness runs.

The recorder stores only bounded scalar facts and file digests. It never stores
credentials, command lines, environment variables, or raw AWS/API responses.
Every mutation rechecks that the Agentic-ai-self-service Git tree is still the
clean tree captured when the run started.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "build" / "release-evidence"
CONTROLS_PATH = PROJECT_ROOT / "release" / "production-readiness-controls.json"
REGIONS_PATH = PROJECT_ROOT / "release" / "emea-agentcore-regions.json"
_STATUS_VALUES = frozenset({"pass", "fail", "blocked"})
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_REGION = re.compile(r"^(?:local|[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d)$")
_SAFE_ACCOUNT_ID = re.compile(r"^\d{12}$")
_SENSITIVE_KEY = re.compile(r"(?i)(token|secret|password|credential|authorization|cookie|private.?key|session.?key)")
_TOKEN_VALUE = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|"
    r"AWS4-HMAC-SHA256|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


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
    dirty = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        project_rel,
    )
    return {
        "repo": str(repo),
        "project_rel": project_rel,
        "commit": commit,
        "project_tree": tree,
        "branch": _git(repo, "branch", "--show-current"),
        "clean": not bool(dirty),
    }


def _require_same_clean_source(expected: dict[str, Any]) -> dict[str, Any]:
    current = _source_identity()
    for key in ("project_rel", "commit", "project_tree"):
        if current.get(key) != expected.get(key):
            raise EvidenceError(f"Source identity changed at {key}; start a new evidence run")
    if not current["clean"]:
        raise EvidenceError("Project tree is dirty; evidence must bind to a clean commit")
    return current


def _load_contract(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema_version") != 1:
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

    source_url = regions.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith("https://docs.aws.amazon.com/"):
        raise EvidenceError("Region matrix must cite an AWS Documentation source URL")
    accessed_at = regions.get("accessed_at")
    try:
        datetime.strptime(str(accessed_at), "%Y-%m-%d")
    except ValueError as exc:
        raise EvidenceError("Region matrix accessed_at must be YYYY-MM-DD") from exc

    raw_regions = regions.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise EvidenceError("Region matrix must define at least one Region")
    region_map: dict[str, dict[str, Any]] = {}
    for entry in raw_regions:
        if not isinstance(entry, dict):
            raise EvidenceError("Every Region entry must be an object")
        region = entry.get("region")
        if not isinstance(region, str) or region == "local" or not _SAFE_REGION.fullmatch(region):
            raise EvidenceError(f"Invalid AWS Region: {region}")
        if region in region_map:
            raise EvidenceError(f"Duplicate AWS Region: {region}")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise EvidenceError(f"Region {region} needs a display name")
        for selector in {
            str(control["region_selector"])
            for control in control_map.values()
            if control.get("required_region_kind") is not None
        }:
            if not isinstance(entry.get(selector), bool):
                raise EvidenceError(f"Region {region} lacks boolean selector {selector}")
        region_map[region] = entry

    core_features = regions.get("core_features")
    if (
        not isinstance(core_features, list)
        or not core_features
        or any(not isinstance(item, str) or not _SAFE_NAME.fullmatch(item) for item in core_features)
        or len(core_features) != len(set(core_features))
    ):
        raise EvidenceError("Region matrix core_features must be unique safe names")
    if not isinstance(regions.get("production_envelope"), dict):
        raise EvidenceError("Region matrix must define a production_envelope")
    return control_map, region_map


def _contract_fingerprint(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    sha256, size = _sha256_file(path)
    return {
        "path": path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix(),
        "sha256": sha256,
        "bytes": size,
        "document_digest": _digest(value),
    }


def _load_contract_bundle() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    controls = _load_contract(CONTROLS_PATH)
    regions = _load_contract(REGIONS_PATH)
    _validate_contracts(controls, regions)
    fingerprints = {
        "controls": _contract_fingerprint(CONTROLS_PATH, controls),
        "regions": _contract_fingerprint(REGIONS_PATH, regions),
    }
    return controls, regions, fingerprints


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
    if run.get("schema_version") != 1:
        raise EvidenceError("Unsupported evidence run schema")
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
    for field in ("project_rel", "commit", "project_tree"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise EvidenceError(f"Evidence run source lacks {field}")
    contracts = run.get("contracts")
    if not isinstance(contracts, dict) or set(contracts) != {"controls", "regions"}:
        raise EvidenceError("Evidence run contract fingerprints are incomplete")
    return run


def _require_run_context(
    run_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run = _load_run(run_dir)
    _require_same_clean_source(run["source"])
    controls, regions, fingerprints = _load_contract_bundle()
    if fingerprints != run["contracts"]:
        raise EvidenceError("Release contracts changed after the evidence run started")
    return run, controls, regions


def start_run(output_root: Path = DEFAULT_OUTPUT_ROOT, run_id: str | None = None) -> Path:
    identity = _source_identity()
    if not identity["clean"]:
        raise EvidenceError("Project tree is dirty; commit it before starting evidence")
    _, _, fingerprints = _load_contract_bundle()
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

    run: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": _utc(),
        "source": _public_source(identity),
        "contracts": fingerprints,
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


def _artifact_records(artifacts: dict[str, Path] | None) -> list[dict[str, Any]]:
    if len(artifacts or {}) > 32:
        raise EvidenceError("At most 32 evidence artifacts are accepted")
    records: list[dict[str, Any]] = []
    for label, path in sorted((artifacts or {}).items()):
        if not _SAFE_NAME.fullmatch(label):
            raise EvidenceError(f"Unsafe artifact label: {label}")
        sha256, size = _secure_artifact_digest(path)
        records.append({"label": label, "sha256": sha256, "bytes": size})
    return records


def _validate_event_document(
    event: dict[str, Any],
    filename: str,
    run: dict[str, Any],
    control_map: dict[str, dict[str, Any]],
    region_map: dict[str, dict[str, Any]],
) -> None:
    expected_fields = {
        "schema_version",
        "event_id",
        "recorded_at",
        "control_id",
        "kind",
        "status",
        "region",
        "account_id",
        "details",
        "artifacts",
        "supersedes",
        "source_commit",
        "project_tree",
        "run_digest",
        "event_digest",
    }
    if set(event) != expected_fields or event.get("schema_version") != 1:
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
    if not isinstance(control_id, str) or not isinstance(kind, str):
        raise EvidenceError(f"Evidence event {event_id} has an invalid scope")
    if region is not None and not isinstance(region, str):
        raise EvidenceError(f"Evidence event {event_id} has an invalid Region")
    if account_id is not None and not isinstance(account_id, str):
        raise EvidenceError(f"Evidence event {event_id} has an invalid AWS account ID")
    _validate_event_scope(control_id, kind, region, account_id, control_map, region_map)
    if event.get("status") not in _STATUS_VALUES:
        raise EvidenceError(f"Evidence event {event_id} has an invalid status")

    details = event.get("details")
    if not isinstance(details, dict) or _validate_details(details) != details:
        raise EvidenceError(f"Evidence event {event_id} has unsafe details")
    artifacts = event.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > 32:
        raise EvidenceError(f"Evidence event {event_id} has invalid artifacts")
    labels: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"label", "sha256", "bytes"}:
            raise EvidenceError(f"Evidence event {event_id} has an invalid artifact")
        label = artifact.get("label")
        sha256 = artifact.get("sha256")
        size = artifact.get("bytes")
        if (
            not isinstance(label, str)
            or not _SAFE_NAME.fullmatch(label)
            or label in labels
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > 1024 * 1024 * 1024
        ):
            raise EvidenceError(f"Evidence event {event_id} has an invalid artifact record")
        labels.add(label)

    supersedes = event.get("supersedes")
    if supersedes is not None and (not isinstance(supersedes, str) or not _SAFE_NAME.fullmatch(supersedes)):
        raise EvidenceError(f"Evidence event {event_id} has an invalid supersedes value")
    if (
        event.get("source_commit") != run["source"]["commit"]
        or event.get("project_tree") != run["source"]["project_tree"]
        or event.get("run_digest") != run["run_digest"]
    ):
        raise EvidenceError(f"Evidence event {event_id} is not bound to this run")


def _load_events(
    run_dir: Path,
    run: dict[str, Any],
    controls: dict[str, Any],
    regions: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    events_dir = _safe_existing_directory(run_dir / "events", "Evidence events directory")
    control_map, region_map = _validate_contracts(controls, regions)
    events: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(events_dir.iterdir(), key=lambda item: item.name):
        if path.suffix != ".json" or not path.is_file() or path.is_symlink():
            raise EvidenceError(f"Unexpected entry in evidence events directory: {path.name}")
        event = _read_json(path)
        _validate_event_document(event, path.name, run, control_map, region_map)
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
        scope = ("control_id", "kind", "region", "account_id")
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


def _record_event_unlocked(
    run_dir: Path,
    *,
    control_id: str,
    kind: str,
    status: str,
    region: str | None = None,
    account_id: str | None = None,
    details: dict[str, Any] | None = None,
    artifacts: dict[str, Path] | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    if (run_dir / "final.json").exists():
        raise EvidenceError("Finalized evidence runs are immutable")
    run, controls, regions = _require_run_context(run_dir)
    control_map, region_map = _validate_contracts(controls, regions)
    if status not in _STATUS_VALUES:
        raise EvidenceError(f"Evidence status must be one of: {', '.join(sorted(_STATUS_VALUES))}")
    _validate_event_scope(control_id, kind, region, account_id, control_map, region_map)
    clean_details = _validate_details(details or {})
    artifact_records = _artifact_records(artifacts)

    events, superseded = _load_events(run_dir, run, controls, regions)
    by_id = {event["event_id"]: event for event in events}
    if supersedes is not None:
        target = by_id.get(supersedes)
        if target is None:
            raise EvidenceError(f"Cannot supersede unknown event: {supersedes}")
        if supersedes in superseded:
            raise EvidenceError(f"Cannot supersede inactive event: {supersedes}")
        if (target["control_id"], target["kind"], target["region"], target["account_id"]) != (
            control_id,
            kind,
            region,
            account_id,
        ):
            raise EvidenceError("A superseding event must keep control, kind, Region, and AWS account")

    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": f"event-{uuid.uuid4().hex}",
        "recorded_at": _utc(),
        "control_id": control_id,
        "kind": kind,
        "status": status,
        "region": region,
        "account_id": account_id,
        "details": clean_details,
        "artifacts": artifact_records,
        "supersedes": supersedes,
        "source_commit": run["source"]["commit"],
        "project_tree": run["source"]["project_tree"],
        "run_digest": run["run_digest"],
    }
    event["event_digest"] = _digest(event)
    _write_new(run_dir / "events" / f"{event['event_id']}.json", event)
    return event


def _validate_run_entries(run_dir: Path, *, finalized: bool) -> None:
    allowed = {".lock", "run.json", "events"}
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
    events: list[dict[str, Any]],
    superseded: set[str],
) -> dict[str, Any]:
    control_map, region_map = _validate_contracts(controls, regions)
    active = [event for event in events if event["event_id"] not in superseded]
    passing = [event for event in active if event["status"] == "pass"]
    missing: list[dict[str, Any]] = []

    for control_id, control in sorted(control_map.items()):
        for kind in control["required_kinds"]:
            if not any(event["control_id"] == control_id and event["kind"] == kind for event in passing):
                missing.append({"scope": "control", "control_id": control_id, "kind": kind, "region": None})
        regional_kind = control.get("required_region_kind")
        selector = control.get("region_selector")
        if isinstance(regional_kind, str) and isinstance(selector, str):
            for region, entry in sorted(region_map.items()):
                if entry.get(selector) is not True:
                    continue
                if not any(
                    event["control_id"] == control_id and event["kind"] == regional_kind and event["region"] == region
                    for event in passing
                ):
                    missing.append(
                        {
                            "scope": "region",
                            "control_id": control_id,
                            "kind": regional_kind,
                            "region": region,
                        }
                    )

    for kind in controls["global_required_kinds"]:
        if not any(event["kind"] == kind for event in passing):
            missing.append({"scope": "global", "control_id": None, "kind": kind, "region": None})

    open_events = [
        {
            "event_id": event["event_id"],
            "control_id": event["control_id"],
            "kind": event["kind"],
            "region": event["region"],
            "account_id": event["account_id"],
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
    run, controls, regions = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions)
    return {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source": run["source"],
        "finalized": finalized,
        "event_set_digest": _event_set_digest(events),
        "coverage": _coverage_report(controls, regions, events, superseded),
    }


def _finalize_run_unlocked(run_dir: Path) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    if (run_dir / "final.json").exists():
        raise EvidenceError("Evidence run is already finalized")
    _validate_run_entries(run_dir, finalized=False)
    run, controls, regions = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions)
    coverage = _coverage_report(controls, regions, events, superseded)
    if not coverage["complete"]:
        raise EvidenceError(
            "Evidence coverage is incomplete "
            f"({len(coverage['missing'])} missing, {len(coverage['open_events'])} open failures)"
        )
    final: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "finalized_at": _utc(),
        "status": "pass",
        "source": run["source"],
        "contracts": run["contracts"],
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
    run, controls, regions = _require_run_context(run_dir)
    events, superseded = _load_events(run_dir, run, controls, regions)
    final = _read_json(run_dir / "final.json")
    expected_fields = {
        "schema_version",
        "run_id",
        "finalized_at",
        "status",
        "source",
        "contracts",
        "run_digest",
        "event_set_digest",
        "coverage",
        "final_digest",
    }
    if set(final) != expected_fields or final.get("schema_version") != 1:
        raise EvidenceError("Final evidence document has an invalid schema")
    _require_timestamp(final.get("finalized_at"), "Final evidence document")
    _check_self_digest(final, "final_digest", "Final evidence document")
    coverage = _coverage_report(controls, regions, events, superseded)
    expected_values = {
        "run_id": run["run_id"],
        "status": "pass",
        "source": run["source"],
        "contracts": run["contracts"],
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


def record_event(run_dir: Path, **kwargs: Any) -> dict[str, Any]:
    run_dir = _safe_existing_directory(run_dir, "Evidence run directory")
    with _run_lock(run_dir, exclusive=True):
        return _record_event_unlocked(run_dir, **kwargs)


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


def _parse_assignments(values: list[str], *, artifacts: bool = False) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in values:
        if "=" not in raw:
            noun = "artifacts" if artifacts else "details"
            raise EvidenceError(f"Evidence {noun} must use key=value")
        key, raw_value = raw.split("=", 1)
        if key in parsed:
            raise EvidenceError(f"Duplicate evidence field: {key}")
        if artifacts:
            parsed[key] = Path(raw_value)
        else:
            parsed_key, parsed_value = _parse_detail(raw)
            parsed[parsed_key] = parsed_value
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record and verify exact-commit production-readiness evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a clean exact-commit evidence run")
    start.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    start.add_argument("--run-id")

    record = subparsers.add_parser("record", help="Append one bounded evidence event")
    record.add_argument("--run-dir", type=Path, required=True)
    record.add_argument("--control", required=True)
    record.add_argument("--kind", required=True)
    record.add_argument("--status", choices=sorted(_STATUS_VALUES), required=True)
    record.add_argument("--region")
    record.add_argument("--account-id")
    record.add_argument("--detail", action="append", default=[])
    record.add_argument("--artifact", action="append", default=[])
    record.add_argument("--supersedes")

    for command in ("status", "finalize", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            run_dir = start_run(args.output_root, args.run_id)
            result: dict[str, Any] = {"run_dir": str(run_dir.resolve())}
        elif args.command == "record":
            result = record_event(
                args.run_dir,
                control_id=args.control,
                kind=args.kind,
                status=args.status,
                region=args.region,
                account_id=args.account_id,
                details=_parse_assignments(args.detail),
                artifacts=_parse_assignments(args.artifact, artifacts=True),
                supersedes=args.supersedes,
            )
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
