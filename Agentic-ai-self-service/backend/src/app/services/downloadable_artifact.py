"""Deterministic, integrity-verifiable customer download bundles.

The outer archives produced by the CloudFormation and Python exporters are a
supply-chain boundary: customers commonly commit them to source control and run
the included scripts with deployment credentials.  This module therefore owns
one canonical ZIP format for both exporters and validates every archive before
it can leave the API.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

ARTIFACT_MANIFEST = "artifact-manifest.json"
ARTIFACT_CHECKSUMS = "SHA256SUMS"
ARTIFACT_VERIFIER = "verify-artifact.py"
ARTIFACT_SCHEMA = "agentcore.downloadable-artifact/v1"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REGULAR_FILE_MODES = frozenset({0o100644, 0o100755})
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_COUNT = 4096
MAX_COMPRESSION_RATIO = 100
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENT_BYTES = 255
READ_CHUNK_BYTES = 1024 * 1024

_TEXT_NAMES = frozenset({"Dockerfile", ".env.example"})
_TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICES = frozenset(
    {"aux", "con", "nul", "prn", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


class DownloadableArtifactError(ValueError):
    """The artifact is unsafe, non-canonical, or fails an integrity check."""


@dataclass(frozen=True)
class ArtifactSummary:
    """Verified whole-archive metadata returned to the API boundary."""

    artifact_type: str
    archive_sha256: str
    archive_bytes: int
    member_count: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _is_text_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in _TEXT_NAMES or PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES


def _path_label(path: str) -> str:
    return repr(path if len(path) <= 120 else f"{path[:117]}...")


def _validate_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise DownloadableArtifactError("artifact path must be a non-empty string")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DownloadableArtifactError("artifact path is not valid UTF-8") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise DownloadableArtifactError("artifact path exceeds the portable length limit")
    if "\x00" in path or "\\" in path or any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise DownloadableArtifactError(f"unsafe artifact path: {_path_label(path)}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise DownloadableArtifactError(f"absolute artifact path: {_path_label(path)}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or posixpath.normpath(path) != path:
        raise DownloadableArtifactError(f"non-canonical artifact path: {_path_label(path)}")
    if any(len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in parts):
        raise DownloadableArtifactError(f"artifact path component is too long: {_path_label(path)}")
    if any(
        ":" in part or part.endswith((" ", ".")) or part.split(".", 1)[0].lower() in _WINDOWS_DEVICES for part in parts
    ):
        raise DownloadableArtifactError(f"non-portable artifact path: {_path_label(path)}")
    return path


def _is_control_path(path: str) -> bool:
    return path.split("/", 1)[0].casefold() == ".git"


def _validate_member_topology(paths: list[str] | set[str], *, label: str) -> None:
    folded: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        if key in folded:
            raise DownloadableArtifactError(f"{label} contains duplicate portable paths")
        folded[key] = path
        if _is_control_path(path):
            raise DownloadableArtifactError(f"{label} contains reserved checkout metadata")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:index]).casefold() in folded for index in range(1, len(parts))):
            raise DownloadableArtifactError(f"{label} contains a file/directory path conflict")


def _validate_zip_resource_limits(infos: list[zipfile.ZipInfo], *, label: str) -> None:
    if not infos:
        raise DownloadableArtifactError(f"{label} contains no members")
    if len(infos) > MAX_MEMBER_COUNT:
        raise DownloadableArtifactError(f"{label} exceeds the member-count limit")
    expanded = 0
    compressed = 0
    for info in infos:
        if info.file_size < 0 or info.compress_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            raise DownloadableArtifactError(f"{label} contains an invalid or oversized member declaration")
        expanded += info.file_size
        compressed += info.compress_size
        if expanded > MAX_EXPANDED_BYTES:
            raise DownloadableArtifactError(f"{label} exceeds the aggregate expanded-size limit")
        if info.file_size and (info.compress_size == 0 or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO):
            raise DownloadableArtifactError(f"{label} exceeds the per-member compression-ratio limit")
    if expanded and expanded > max(compressed, 1) * MAX_COMPRESSION_RATIO:
        raise DownloadableArtifactError(f"{label} exceeds the aggregate compression-ratio limit")


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, label: str) -> bytes:
    chunks: list[bytes] = []
    expanded = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            expanded += len(chunk)
            if expanded > info.file_size or expanded > MAX_MEMBER_BYTES:
                raise DownloadableArtifactError(f"{label} expanded beyond its declared size")
            chunks.append(chunk)
    if expanded != info.file_size:
        raise DownloadableArtifactError(f"{label} did not match its declared size")
    return b"".join(chunks)


def _text_bytes(value: str) -> bytes:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _zip_info(path: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=FIXED_ZIP_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = mode << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _canonicalize_nested_zip(data: bytes, *, label: str) -> bytes:
    """Canonicalize one bounded, file-only ZIP nested in a customer bundle."""
    if not data or len(data) > MAX_MEMBER_BYTES:
        raise DownloadableArtifactError(f"{label} is empty or exceeds the nested ZIP size limit")
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise DownloadableArtifactError(f"{label} is not a valid nested ZIP") from exc

    rows: list[tuple[str, bytes, int]] = []
    seen: set[str] = set()
    with source:
        infos = source.infolist()
        _validate_zip_resource_limits(infos, label=f"nested ZIP {label}")
        validated_paths = [_validate_path(info.filename) for info in infos]
        _validate_member_topology(validated_paths, label=f"nested ZIP {label}")
        for info, path in zip(infos, validated_paths, strict=True):
            if info.is_dir():
                raise DownloadableArtifactError(f"{label} contains a directory entry: {_path_label(path)}")
            if path in seen:
                raise DownloadableArtifactError(f"{label} contains a duplicate entry: {_path_label(path)}")
            seen.add(path)
            raw_mode = info.external_attr >> 16
            kind = stat.S_IFMT(raw_mode)
            if kind not in {0, stat.S_IFREG}:
                raise DownloadableArtifactError(f"{label} contains a symlink or special entry: {_path_label(path)}")
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise DownloadableArtifactError(f"{label} uses unsafe nested ZIP compression or encryption")
            mode = 0o100755 if raw_mode & 0o111 else 0o100644
            payload = _read_zip_member(source, info, label=f"nested ZIP member {_path_label(path)}")
            rows.append((path, payload, mode))

    out = io.BytesIO()
    with zipfile.ZipFile(
        out,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as target:
        target.comment = b""
        for path, payload, mode in sorted(rows, key=lambda row: row[0]):
            info = _zip_info(path, mode)
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    result = out.getvalue()
    if len(result) > MAX_MEMBER_BYTES:
        raise DownloadableArtifactError(f"{label} canonical nested ZIP exceeds the member-size limit")
    return result


def _canonical_file(path: str, value: str | bytes) -> bytes:
    if isinstance(value, str):
        return _text_bytes(value)
    if not isinstance(value, bytes):
        raise DownloadableArtifactError(f"artifact member {_path_label(path)} is not text or bytes")
    if path.lower().endswith(".zip"):
        return _canonicalize_nested_zip(value, label=path)
    return value


def _portable_verifier_source() -> str:
    """Return the dependency-free verifier shipped in every download."""
    return r'''#!/usr/bin/env python3 -I
"""Verify or safely extract an AgentCore downloadable artifact (stdlib only)."""
import sys

# Executed as a script this file sits inside the untrusted artifact directory. Without
# isolated mode Python puts that directory first on sys.path, so an undeclared
# `hashlib.py` dropped next to it would be imported below instead of the standard
# library. Refuse before importing anything else. `sys` is always preloaded.
if __name__ == "__main__" and (
    not sys.flags.isolated or sys.flags.ignore_environment != 1 or sys.flags.no_user_site != 1
):
    sys.stderr.write(
        "verification failed: run `python3 -I verify-artifact.py ...`; a non-isolated "
        "interpreter can import modules from the unverified artifact directory\n"
    )
    raise SystemExit(1)

import argparse, hashlib, io, json, os, posixpath, re, stat, zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "agentcore.downloadable-artifact/v1"
MANIFEST = "artifact-manifest.json"
SUMS = "SHA256SUMS"
VERIFIER = "verify-artifact.py"
FIXED = (1980, 1, 1, 0, 0, 0)
MODES = {0o100644, 0o100755}
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_COUNT = 4096
MAX_COMPRESSION_RATIO = 100
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENT_BYTES = 255
MAX_DIRECTORY_ENTRIES = 65536
READ_CHUNK_BYTES = 1024 * 1024
TEXT_NAMES = {"Dockerfile", ".env.example"}
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
HEX = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_TYPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WINDOWS_DEVICES = {"aux", "con", "nul", "prn", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

class VerificationError(ValueError):
    pass

class OwnedZipFile(zipfile.ZipFile):
    def __init__(self, source):
        self._owned_source = source
        try:
            super().__init__(source)
        except BaseException:
            self._owned_source = None
            source.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            source = getattr(self, "_owned_source", None)
            if source is not None:
                self._owned_source = None
                source.close()

def fail(message):
    raise VerificationError(message)

def open_archive(path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or not 0 < result.st_size <= MAX_ARCHIVE_BYTES:
            fail("archive is empty, special, or exceeds the size limit")
        source = os.fdopen(descriptor, "rb")
        descriptor = None
        return OwnedZipFile(source)
    finally:
        if descriptor is not None:
            os.close(descriptor)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def canonical_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()

def path_label(path):
    return repr(path if len(path) <= 120 else f"{path[:117]}...")

def safe_path(path):
    if not isinstance(path, str) or not path:
        fail("path must be a non-empty string")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        fail("path is not valid UTF-8")
    if len(encoded) > MAX_PATH_BYTES:
        fail("path exceeds the portable length limit")
    if "\x00" in path or "\\" in path or any(ord(character) < 32 or ord(character) == 127 for character in path):
        fail(f"unsafe path: {path_label(path)}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        fail(f"absolute path: {path_label(path)}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or posixpath.normpath(path) != path:
        fail(f"non-canonical path: {path_label(path)}")
    if any(len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in parts):
        fail(f"path component is too long: {path_label(path)}")
    if any(":" in part or part.endswith((" ", ".")) or part.split(".", 1)[0].lower() in WINDOWS_DEVICES for part in parts):
        fail(f"non-portable path: {path_label(path)}")
    return path

def control_path(path):
    return path.split("/", 1)[0].casefold() == ".git"

def check_topology(paths, label):
    folded = {}
    for path in paths:
        key = path.casefold()
        if key in folded:
            fail(f"{label} contains duplicate portable paths")
        folded[key] = path
        if control_path(path):
            fail(f"{label} contains reserved checkout metadata")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:index]).casefold() in folded for index in range(1, len(parts))):
            fail(f"{label} contains a file/directory path conflict")

def zip_limits(infos, label):
    if not infos:
        fail(f"{label} contains no members")
    if len(infos) > MAX_MEMBER_COUNT:
        fail(f"{label} exceeds the member-count limit")
    expanded = 0
    compressed = 0
    for info in infos:
        if info.file_size < 0 or info.compress_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            fail(f"{label} contains an invalid or oversized member declaration")
        expanded += info.file_size
        compressed += info.compress_size
        if expanded > MAX_EXPANDED_BYTES:
            fail(f"{label} exceeds the aggregate expanded-size limit")
        if info.file_size and (info.compress_size == 0 or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO):
            fail(f"{label} exceeds the per-member compression-ratio limit")
    if expanded and expanded > max(compressed, 1) * MAX_COMPRESSION_RATIO:
        fail(f"{label} exceeds the aggregate compression-ratio limit")

def read_member(archive, info, label):
    chunks = []
    expanded = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            expanded += len(chunk)
            if expanded > info.file_size or expanded > MAX_MEMBER_BYTES:
                fail(f"{label} expanded beyond its declared size")
            chunks.append(chunk)
    if expanded != info.file_size:
        fail(f"{label} did not match its declared size")
    return b"".join(chunks)

def zip_info(path, mode):
    info = zipfile.ZipInfo(path, date_time=FIXED)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = mode << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info

def canonical_nested(data, label):
    if not data or len(data) > MAX_MEMBER_BYTES:
        fail(f"{label} is empty or exceeds the nested ZIP size limit")
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile):
        fail(f"{label} is not a valid nested ZIP")
    rows = []
    seen = set()
    with source:
        infos = source.infolist()
        zip_limits(infos, f"nested ZIP {label}")
        validated_paths = [safe_path(info.filename) for info in infos]
        check_topology(validated_paths, f"nested ZIP {label}")
        for info, path in zip(infos, validated_paths):
            if info.is_dir():
                fail(f"{label} contains a directory entry: {path_label(path)}")
            if path in seen:
                fail(f"{label} contains a duplicate entry: {path_label(path)}")
            seen.add(path)
            raw_mode = info.external_attr >> 16
            kind = stat.S_IFMT(raw_mode)
            if kind not in {0, stat.S_IFREG}:
                fail(f"{label} contains a symlink or special entry: {path_label(path)}")
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                fail(f"{label} uses unsafe nested ZIP compression or encryption")
            mode = 0o100755 if raw_mode & 0o111 else 0o100644
            payload = read_member(source, info, f"nested ZIP member {path_label(path)}")
            rows.append((path, payload, mode))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as target:
        target.comment = b""
        for path, payload, mode in sorted(rows, key=lambda row: row[0]):
            info = zip_info(path, mode)
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    result = out.getvalue()
    if len(result) > MAX_MEMBER_BYTES:
        fail(f"{label} canonical nested ZIP exceeds the member-size limit")
    return result

def text_path(path):
    candidate = PurePosixPath(path)
    return candidate.name in TEXT_NAMES or candidate.suffix.lower() in TEXT_SUFFIXES

def check_text(path, data):
    if not text_path(path):
        return
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"text is not UTF-8: {path_label(path)}")
    if b"\r" in data or b"\x00" in data or not data.endswith(b"\n"):
        fail(f"text is not canonical UTF-8/LF: {path_label(path)}")

def parse_metadata(read, paths, modes, artifact_type=None):
    required = {MANIFEST, SUMS, VERIFIER}
    if not required <= paths:
        fail(f"missing metadata files: {sorted(required - paths)}")
    manifest_bytes = read(MANIFEST)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("invalid artifact manifest")
    if canonical_json(manifest) != manifest_bytes:
        fail("artifact-manifest.json is not canonical JSON")
    if set(manifest) != {"artifact_type", "files", "schema"} or manifest["schema"] != SCHEMA:
        fail("unsupported manifest schema")
    actual_type = manifest["artifact_type"]
    if not isinstance(actual_type, str) or not ARTIFACT_TYPE.fullmatch(actual_type):
        fail("invalid artifact type")
    if artifact_type is not None and actual_type != artifact_type:
        fail("unexpected artifact type")
    rows = manifest["files"]
    if not isinstance(rows, list):
        fail("manifest files is not a list")
    if len(rows) + 2 > MAX_MEMBER_COUNT:
        fail("manifest exceeds the member-count limit")
    manifest_paths = []
    declared_total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"bytes", "mode", "path", "sha256", "type"}:
            fail("invalid manifest row shape")
        path = safe_path(row["path"])
        if row["type"] != "file" or row["mode"] not in {"100644", "100755"}:
            fail(f"invalid manifest type or mode: {path_label(path)}")
        if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_MEMBER_BYTES or not HEX.fullmatch(row["sha256"]):
            fail(f"invalid manifest size or digest: {path_label(path)}")
        declared_total += row["bytes"]
        if declared_total > MAX_EXPANDED_BYTES:
            fail("manifest exceeds the aggregate expanded-size limit")
        manifest_paths.append(path)
    if manifest_paths != sorted(manifest_paths) or len(manifest_paths) != len(set(manifest_paths)):
        fail("manifest paths are unsorted or duplicated")
    check_topology(manifest_paths, "manifest")
    if MANIFEST in manifest_paths or SUMS in manifest_paths:
        fail("manifest/checksum self-recursion is forbidden")

    try:
        sums_text = read(SUMS).decode("utf-8")
    except UnicodeDecodeError:
        fail("invalid SHA256SUMS encoding")
    if "\r" in sums_text or not sums_text.endswith("\n"):
        fail("SHA256SUMS is not LF terminated")
    if sums_text.count("\n") > MAX_MEMBER_COUNT:
        fail("SHA256SUMS exceeds the member-count limit")
    sums = {}
    sum_paths = []
    for line in sums_text.splitlines():
        if "  " not in line:
            fail("invalid SHA256SUMS row")
        digest, path = line.split("  ", 1)
        path = safe_path(path)
        if not HEX.fullmatch(digest) or path in sums:
            fail(f"invalid or duplicate checksum row: {path_label(path)}")
        sums[path] = digest
        sum_paths.append(path)
    if sum_paths != sorted(sum_paths):
        fail("SHA256SUMS rows are not sorted")
    check_topology(sum_paths, "SHA256SUMS")
    expected_sums = set(manifest_paths) | {MANIFEST}
    if set(sums) != expected_sums or set(paths) != expected_sums | {SUMS}:
        fail("missing or extra artifact members")

    by_path = {row["path"]: row for row in rows}
    for path in sorted(paths):
        data = read(path)
        check_text(path, data)
        if path != SUMS and sha(data) != sums[path]:
            fail(f"checksum mismatch: {path_label(path)}")
        if path in by_path:
            row = by_path[path]
            if len(data) != row["bytes"] or sha(data) != row["sha256"]:
                fail(f"manifest mismatch: {path_label(path)}")
            if modes[path] != int(row["mode"], 8):
                fail(f"mode mismatch: {path_label(path)}")
        if path.lower().endswith(".zip") and canonical_nested(data, path) != data:
            fail(f"nested ZIP is not canonical: {path_label(path)}")
    return manifest

def verify_archive(path, artifact_type=None):
    try:
        archive = open_archive(path)
    except (OSError, zipfile.BadZipFile):
        fail("invalid ZIP archive")
    try:
        if archive.comment:
            fail("ZIP archive comment is not canonical")
        infos = archive.infolist()
        zip_limits(infos, "ZIP archive")
        names = [info.filename for info in infos]
        if names != sorted(names) or len(names) != len(set(names)):
            fail("ZIP entries are unsorted or duplicated")
        roots = set()
        relative = {}
        modes = {}
        for info in infos:
            safe_path(info.filename)
            if "/" not in info.filename:
                fail("ZIP member has no artifact root")
            root, rel = info.filename.split("/", 1)
            safe_path(root)
            safe_path(rel)
            roots.add(root)
            if rel in relative:
                fail(f"duplicate relative member: {path_label(rel)}")
            mode = info.external_attr >> 16
            if (info.is_dir() or stat.S_IFMT(mode) != stat.S_IFREG or mode not in MODES
                    or info.date_time != FIXED or info.create_system != 3
                    or info.compress_type != zipfile.ZIP_STORED or info.extra or info.comment
                    or info.flag_bits & 1 or info.compress_size != info.file_size):
                fail(f"unsafe or non-canonical ZIP metadata: {path_label(rel)}")
            relative[rel] = info
            modes[rel] = mode
        if len(roots) != 1:
            fail("ZIP must contain exactly one artifact root")
        check_topology(list(relative), "ZIP archive")
        manifest = parse_metadata(
            lambda rel: read_member(archive, relative[rel], f"ZIP member {path_label(rel)}"),
            set(relative),
            modes,
            artifact_type,
        )
        return archive, relative, modes, manifest
    except BaseException:
        archive.close()
        raise

def reparse_point(result):
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(result, "st_file_attributes", 0) & marker)

def same_node(left, right):
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino

def read_regular_file(path, rel, before):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail(f"could not safely open directory member: {path_label(rel)}")
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or reparse_point(opened) or not same_node(before, opened)
                or opened.st_size != before.st_size or opened.st_size > MAX_MEMBER_BYTES):
            fail(f"directory member changed or is unsafe: {path_label(rel)}")
        remaining = opened.st_size
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                fail(f"directory member shrank while reading: {path_label(rel)}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"directory member grew while reading: {path_label(rel)}")
        after = os.fstat(descriptor)
        if (not same_node(opened, after) or opened.st_size != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns or opened.st_mode != after.st_mode):
            fail(f"directory member changed while reading: {path_label(rel)}")
        return b"".join(chunks), stat.S_IFREG | (opened.st_mode & 0o777)
    finally:
        os.close(descriptor)

def verify_directory(path, artifact_type=None):
    requested = Path(path)
    try:
        root_stat = requested.lstat()
    except OSError:
        fail("verification directory does not exist")
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode) or reparse_point(root_stat):
        fail("verification directory is not a real directory")
    root = requested.resolve()
    files = {}
    modes = {}
    stack = [root]
    visited = 0
    payload_count = 0
    aggregate = 0
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            fail("could not enumerate verification directory")
        with entries:
            for entry in entries:
                visited += 1
                if visited > MAX_DIRECTORY_ENTRIES:
                    fail("directory exceeds the traversal-entry limit")
                candidate = Path(entry.path)
                rel = candidate.relative_to(root).as_posix()
                safe_path(rel)
                try:
                    before = entry.stat(follow_symlinks=False)
                except OSError:
                    fail(f"could not stat directory member: {path_label(rel)}")
                if stat.S_ISLNK(before.st_mode) or reparse_point(before):
                    fail(f"directory contains symlink or reparse point: {path_label(rel)}")
                is_git_metadata = rel == ".git" or rel.startswith(".git/")
                if stat.S_ISDIR(before.st_mode):
                    stack.append(candidate)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    fail(f"directory contains special entry: {path_label(rel)}")
                if is_git_metadata:
                    continue
                payload_count += 1
                if payload_count > MAX_MEMBER_COUNT:
                    fail("directory exceeds the member-count limit")
                if before.st_size < 0 or before.st_size > MAX_MEMBER_BYTES:
                    fail(f"member exceeds size limit: {path_label(rel)}")
                aggregate += before.st_size
                if aggregate > MAX_EXPANDED_BYTES:
                    fail("directory exceeds the aggregate expanded-size limit")
                data, mode = read_regular_file(candidate, rel, before)
                files[rel] = data
                modes[rel] = mode
    check_topology(list(files), "directory")
    return parse_metadata(files.__getitem__, set(files), modes, artifact_type)

def require_secure_extraction():
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if (os.name != "posix" or not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0)
            or not hasattr(os, "fchmod") or any(function not in os.supports_dir_fd for function in required_dir_fd)
            or os.stat not in os.supports_follow_symlinks or os.listdir not in os.supports_fd):
        fail("safe extraction is unsupported on this platform; verification remains available")

def directory_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

def open_child_directory(parent_fd, name):
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or reparse_point(before):
            fail("extraction path contains a symlink or non-directory")
        descriptor = os.open(name, directory_flags(), dir_fd=parent_fd)
    except OSError:
        fail("could not safely open extraction directory")
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or reparse_point(opened) or not same_node(before, opened):
        os.close(descriptor)
        fail("extraction directory changed while opening")
    return descriptor

def remove_tree_at(parent_fd, name):
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(result.st_mode) or stat.S_ISLNK(result.st_mode) or reparse_point(result):
        os.unlink(name, dir_fd=parent_fd)
        return
    descriptor = open_child_directory(parent_fd, name)
    try:
        for child in os.listdir(descriptor):
            remove_tree_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)

def reserve_destination(destination):
    require_secure_extraction()
    destination_path = Path(os.path.abspath(os.fspath(destination)))
    parent_path = destination_path.parent
    name = destination_path.name
    if not name:
        fail("extraction destination must have a basename")
    try:
        parent_fd = os.open(parent_path, directory_flags())
    except OSError:
        fail("extraction destination parent is not a safe existing directory")
    created = False
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            fail("extraction destination must not preexist")
        created = True
        destination_fd = open_child_directory(parent_fd, name)
        os.fchmod(destination_fd, 0o700)
        if stat.S_IMODE(os.fstat(destination_fd).st_mode) != 0o700:
            os.close(destination_fd)
            fail("could not set private extraction destination mode")
        return destination_path, parent_fd, name, destination_fd
    except BaseException as cause:
        if created:
            try:
                remove_tree_at(parent_fd, name)
            except BaseException as cleanup_error:
                os.close(parent_fd)
                raise VerificationError("failed to clean extraction destination") from cleanup_error
        os.close(parent_fd)
        raise cause

def create_directory(parent_fd, name, mode):
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        fail("archive paths conflict during extraction")
    descriptor = open_child_directory(parent_fd, name)
    os.fchmod(descriptor, mode)
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        os.close(descriptor)
        fail("could not set exact extraction directory mode")
    return descriptor

def open_payload_parent(root_fd, parts):
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_fd = create_directory(current, part, 0o755)
            except VerificationError as error:
                if str(error) != "archive paths conflict during extraction":
                    raise
                next_fd = open_child_directory(current, part)
                if stat.S_IMODE(os.fstat(next_fd).st_mode) != 0o755:
                    os.close(next_fd)
                    fail("existing extraction directory has an unexpected mode")
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise

def write_all(descriptor, data):
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            fail("short write during extraction")
        offset += written

def write_member(archive, info, parent_fd, name, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, mode & 0o777, dir_fd=parent_fd)
    except OSError:
        fail("could not exclusively create extraction file")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or reparse_point(opened) or opened.st_nlink != 1:
            fail("extraction target is not an exclusive regular file")
        expanded = 0
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                expanded += len(chunk)
                if expanded > info.file_size or expanded > MAX_MEMBER_BYTES:
                    fail("ZIP member expanded beyond its declared size during extraction")
                write_all(descriptor, chunk)
        if expanded != info.file_size:
            fail("ZIP member did not match its declared size during extraction")
        os.fchmod(descriptor, mode & 0o777)
        result = os.fstat(descriptor)
        if (not stat.S_ISREG(result.st_mode) or result.st_nlink != 1 or result.st_size != info.file_size
                or stat.S_IMODE(result.st_mode) != mode & 0o777):
            fail("extracted file size, type, link count, or mode is incorrect")
    finally:
        os.close(descriptor)

def safe_extract(archive_path, destination, artifact_type=None):
    checked, relative, modes, manifest = verify_archive(archive_path, artifact_type)
    parent_fd = None
    destination_fd = None
    root_fd = None
    destination_name = None
    reserved = False
    try:
        destination_path, parent_fd, destination_name, destination_fd = reserve_destination(destination)
        reserved = True
        root_name = next(iter({info.filename.split("/", 1)[0] for info in relative.values()}))
        root_fd = create_directory(destination_fd, root_name, 0o755)
        for rel in sorted(relative):
            parts = rel.split("/")
            payload_parent = open_payload_parent(root_fd, parts[:-1])
            try:
                write_member(checked, relative[rel], payload_parent, parts[-1], modes[rel])
            finally:
                os.close(payload_parent)
        return destination_path / root_name, manifest
    except BaseException as cause:
        if root_fd is not None:
            os.close(root_fd)
            root_fd = None
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        if reserved and parent_fd is not None and destination_name is not None:
            try:
                remove_tree_at(parent_fd, destination_name)
            except BaseException as cleanup_error:
                raise VerificationError("failed to clean partial extraction destination") from cleanup_error
        raise cause
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        checked.close()

def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive")
    source.add_argument("--directory")
    parser.add_argument("--extract")
    parser.add_argument("--artifact-type")
    args = parser.parse_args()
    if args.extract and not args.archive:
        parser.error("--extract requires --archive")
    if args.archive:
        if args.extract:
            _, manifest = safe_extract(args.archive, args.extract, args.artifact_type)
        else:
            archive, _, _, manifest = verify_archive(args.archive, args.artifact_type)
            archive.close()
    else:
        manifest = verify_directory(args.directory, args.artifact_type)
    print(f"verified {manifest['artifact_type']} ({len(manifest['files'])} payload files)")

if __name__ == "__main__":
    try:
        main()
    except (OSError, VerificationError, zipfile.BadZipFile) as exc:
        print(f"artifact verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
'''


def _manifest(artifact_type: str, files: Mapping[str, tuple[bytes, int]]) -> bytes:
    rows = [
        {
            "bytes": len(content),
            "mode": f"{mode:o}",
            "path": path,
            "sha256": _sha256(content),
            "type": "file",
        }
        for path, (content, mode) in sorted(files.items())
    ]
    return _canonical_json({"artifact_type": artifact_type, "files": rows, "schema": ARTIFACT_SCHEMA})


def build_artifact_zip(
    *,
    prefix: str,
    artifact_type: str,
    files: Mapping[str, str | bytes],
    executable_paths: frozenset[str] | set[str] = frozenset(),
) -> bytes:
    """Build and self-verify one canonical file-only ZIP archive."""
    _validate_path(prefix)
    if "/" in prefix:
        raise DownloadableArtifactError("artifact prefix must be one path segment")
    if not artifact_type or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", artifact_type):
        raise DownloadableArtifactError("artifact type is not canonical")
    if len(files) + 3 > MAX_MEMBER_COUNT:
        raise DownloadableArtifactError("artifact exceeds the member-count limit")

    reserved = {ARTIFACT_MANIFEST, ARTIFACT_CHECKSUMS, ARTIFACT_VERIFIER}
    if {path.casefold() for path in reserved} & {path.casefold() for path in files}:
        raise DownloadableArtifactError("artifact payload attempted to replace integrity metadata")

    normalized: dict[str, tuple[bytes, int]] = {}
    expanded = 0
    for raw_path, value in files.items():
        path = _validate_path(raw_path)
        if path in normalized:
            raise DownloadableArtifactError(f"duplicate artifact path: {_path_label(path)}")
        content = _canonical_file(path, value)
        if len(content) > MAX_MEMBER_BYTES:
            raise DownloadableArtifactError(f"artifact member is too large: {_path_label(path)}")
        expanded += len(content)
        if expanded > MAX_EXPANDED_BYTES:
            raise DownloadableArtifactError("artifact exceeds the aggregate expanded-size limit")
        mode = 0o100755 if path in executable_paths else 0o100644
        normalized[path] = (content, mode)
    _validate_member_topology(list(normalized), label="artifact")

    verifier = _text_bytes(_portable_verifier_source())
    normalized[ARTIFACT_VERIFIER] = (verifier, 0o100644)
    manifest = _manifest(artifact_type, normalized)
    checksummed = {**normalized, ARTIFACT_MANIFEST: (manifest, 0o100644)}
    sums = "".join(f"{_sha256(content)}  {path}\n" for path, (content, _mode) in sorted(checksummed.items())).encode(
        "utf-8"
    )
    members = {**checksummed, ARTIFACT_CHECKSUMS: (sums, 0o100644)}
    if len(members) > MAX_MEMBER_COUNT or any(len(content) > MAX_MEMBER_BYTES for content, _mode in members.values()):
        raise DownloadableArtifactError("artifact metadata exceeds a member safety bound")
    if sum(len(content) for content, _mode in members.values()) > MAX_EXPANDED_BYTES:
        raise DownloadableArtifactError("artifact exceeds the aggregate expanded-size limit")
    _validate_member_topology(list(members), label="artifact")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        archive.comment = b""
        for path, (content, mode) in sorted(members.items()):
            archive.writestr(_zip_info(f"{prefix}/{path}", mode), content)
    result = out.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise DownloadableArtifactError("artifact archive exceeds the 128 MiB safety bound")
    verify_artifact_zip(result, expected_artifact_type=artifact_type)
    return result


def _read_archive_files(data: bytes) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo], dict[str, int]]:
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise DownloadableArtifactError("archive is empty or exceeds the 128 MiB safety bound")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise DownloadableArtifactError("invalid ZIP archive") from exc
    try:
        if archive.comment:
            raise DownloadableArtifactError("ZIP archive comment is not canonical")
        infos = archive.infolist()
        _validate_zip_resource_limits(infos, label="ZIP archive")
        names = [info.filename for info in infos]
        if names != sorted(names) or len(names) != len(set(names)):
            raise DownloadableArtifactError("ZIP entries are unsorted or duplicated")

        roots: set[str] = set()
        relative: dict[str, zipfile.ZipInfo] = {}
        modes: dict[str, int] = {}
        for info in infos:
            _validate_path(info.filename)
            if "/" not in info.filename:
                raise DownloadableArtifactError("ZIP member has no artifact root")
            root, path = info.filename.split("/", 1)
            _validate_path(root)
            _validate_path(path)
            roots.add(root)
            if path in relative:
                raise DownloadableArtifactError(f"duplicate relative ZIP path: {_path_label(path)}")
            mode = info.external_attr >> 16
            if (
                info.is_dir()
                or stat.S_IFMT(mode) != stat.S_IFREG
                or mode not in REGULAR_FILE_MODES
                or info.date_time != FIXED_ZIP_TIME
                or info.create_system != 3
                or info.compress_type != zipfile.ZIP_STORED
                or info.extra
                or info.comment
                or info.flag_bits & 1
                or info.compress_size != info.file_size
            ):
                raise DownloadableArtifactError(f"unsafe or non-canonical ZIP metadata: {_path_label(path)}")
            relative[path] = info
            modes[path] = mode
        if len(roots) != 1:
            raise DownloadableArtifactError("ZIP must contain exactly one artifact root")
        _validate_member_topology(list(relative), label="ZIP archive")
        return archive, relative, modes
    except BaseException:
        archive.close()
        raise


def _parse_manifest(data: bytes, *, expected_artifact_type: str | None) -> tuple[str, dict[str, dict]]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadableArtifactError("artifact manifest is not valid UTF-8 JSON") from exc
    if _canonical_json(manifest) != data:
        raise DownloadableArtifactError("artifact manifest is not canonical JSON")
    if set(manifest) != {"artifact_type", "files", "schema"} or manifest["schema"] != ARTIFACT_SCHEMA:
        raise DownloadableArtifactError("unsupported artifact manifest schema")
    artifact_type = manifest["artifact_type"]
    if not isinstance(artifact_type, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", artifact_type):
        raise DownloadableArtifactError("artifact manifest type is not canonical")
    if expected_artifact_type is not None and artifact_type != expected_artifact_type:
        raise DownloadableArtifactError("unexpected artifact type")
    rows = manifest["files"]
    if not isinstance(rows, list):
        raise DownloadableArtifactError("artifact manifest files must be a list")
    if len(rows) + 2 > MAX_MEMBER_COUNT:
        raise DownloadableArtifactError("artifact manifest exceeds the member-count limit")
    by_path: dict[str, dict] = {}
    ordered: list[str] = []
    declared_total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"bytes", "mode", "path", "sha256", "type"}:
            raise DownloadableArtifactError("invalid artifact manifest row")
        path = _validate_path(row["path"])
        if path in by_path or row["type"] != "file" or row["mode"] not in {"100644", "100755"}:
            raise DownloadableArtifactError(f"invalid manifest path, type, or mode: {_path_label(path)}")
        if (
            type(row["bytes"]) is not int
            or not 0 <= row["bytes"] <= MAX_MEMBER_BYTES
            or not _SHA256_RE.fullmatch(row["sha256"])
        ):
            raise DownloadableArtifactError(f"invalid manifest size or digest: {_path_label(path)}")
        declared_total += row["bytes"]
        if declared_total > MAX_EXPANDED_BYTES:
            raise DownloadableArtifactError("artifact manifest exceeds the aggregate expanded-size limit")
        by_path[path] = row
        ordered.append(path)
    if ordered != sorted(ordered):
        raise DownloadableArtifactError("artifact manifest rows are not sorted")
    _validate_member_topology(ordered, label="manifest")
    if {ARTIFACT_MANIFEST, ARTIFACT_CHECKSUMS} & set(by_path):
        raise DownloadableArtifactError("manifest/checksum self-recursion is forbidden")
    return artifact_type, by_path


def _parse_sums(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadableArtifactError("SHA256SUMS is not UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise DownloadableArtifactError("SHA256SUMS is not canonical LF text")
    if text.count("\n") > MAX_MEMBER_COUNT:
        raise DownloadableArtifactError("SHA256SUMS exceeds the member-count limit")
    result: dict[str, str] = {}
    ordered: list[str] = []
    for line in text.splitlines():
        if "  " not in line:
            raise DownloadableArtifactError("invalid SHA256SUMS row")
        digest, path = line.split("  ", 1)
        path = _validate_path(path)
        if not _SHA256_RE.fullmatch(digest) or path in result:
            raise DownloadableArtifactError(f"invalid or duplicate SHA256SUMS row: {_path_label(path)}")
        result[path] = digest
        ordered.append(path)
    if ordered != sorted(ordered):
        raise DownloadableArtifactError("SHA256SUMS rows are not sorted")
    _validate_member_topology(ordered, label="SHA256SUMS")
    return result


def verify_artifact_zip(data: bytes, *, expected_artifact_type: str | None = None) -> ArtifactSummary:
    """Fail closed on unsafe metadata, drift, tampering, or non-determinism."""
    archive, infos, modes = _read_archive_files(data)
    try:
        required = {ARTIFACT_MANIFEST, ARTIFACT_CHECKSUMS, ARTIFACT_VERIFIER}
        if not required <= set(infos):
            raise DownloadableArtifactError(f"artifact metadata is missing: {sorted(required - set(infos))}")
        artifact_type, rows = _parse_manifest(
            _read_zip_member(archive, infos[ARTIFACT_MANIFEST], label=ARTIFACT_MANIFEST),
            expected_artifact_type=expected_artifact_type,
        )
        sums = _parse_sums(_read_zip_member(archive, infos[ARTIFACT_CHECKSUMS], label=ARTIFACT_CHECKSUMS))
        expected_sums = set(rows) | {ARTIFACT_MANIFEST}
        if set(sums) != expected_sums or set(infos) != expected_sums | {ARTIFACT_CHECKSUMS}:
            raise DownloadableArtifactError("artifact has missing or extra members")

        for path in sorted(infos):
            content = _read_zip_member(archive, infos[path], label=path)
            if _is_text_path(path):
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DownloadableArtifactError(f"text member is not UTF-8: {_path_label(path)}") from exc
                if b"\r" in content or b"\x00" in content or not content.endswith(b"\n"):
                    raise DownloadableArtifactError(f"text member is not canonical UTF-8/LF: {_path_label(path)}")
            if path != ARTIFACT_CHECKSUMS and _sha256(content) != sums[path]:
                raise DownloadableArtifactError(f"checksum mismatch: {_path_label(path)}")
            if path in rows:
                row = rows[path]
                if len(content) != row["bytes"] or _sha256(content) != row["sha256"]:
                    raise DownloadableArtifactError(f"manifest mismatch: {_path_label(path)}")
                if modes[path] != int(row["mode"], 8):
                    raise DownloadableArtifactError(f"mode mismatch: {_path_label(path)}")
            if path.lower().endswith(".zip") and _canonicalize_nested_zip(content, label=path) != content:
                raise DownloadableArtifactError(f"nested ZIP is not canonical: {_path_label(path)}")
    finally:
        archive.close()

    return ArtifactSummary(
        artifact_type=artifact_type,
        archive_sha256=_sha256(data),
        archive_bytes=len(data),
        member_count=len(infos),
    )
