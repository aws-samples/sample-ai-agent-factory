"""Focused filesystem and resource-bound tests for the shipped artifact extractor."""

from __future__ import annotations

import io
import os
import stat
import struct
import subprocess
import sys
import types
import warnings
import zipfile
from pathlib import Path

import app.services.downloadable_artifact as artifact
import pytest


def _stdlib_verifier() -> types.ModuleType:
    module = types.ModuleType("shipped_artifact_verifier")
    module.__dict__["__name__"] = "shipped_artifact_verifier"
    exec(compile(artifact._portable_verifier_source(), "verify-artifact.py", "exec"), module.__dict__)
    return module


@pytest.fixture
def verifier() -> types.ModuleType:
    return _stdlib_verifier()


def _archive(*, content: str = "original\n", path: str = "payload.txt") -> bytes:
    return artifact.build_artifact_zip(
        prefix="bundle",
        artifact_type="python",
        files={path: content, "run.sh": "#!/bin/sh\nexit 0\n"},
        executable_paths={"run.sh"},
    )


def _write_archive(tmp_path: Path, data: bytes, name: str = "artifact.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _raw_artifact(files: dict[str, tuple[bytes, int]]) -> bytes:
    verifier_bytes = artifact._text_bytes(artifact._portable_verifier_source())
    payload = {**files, artifact.ARTIFACT_VERIFIER: (verifier_bytes, 0o100644)}
    manifest = artifact._manifest("python", payload)
    checksummed = {**payload, artifact.ARTIFACT_MANIFEST: (manifest, 0o100644)}
    sums = "".join(
        f"{artifact._sha256(content)}  {path}\n" for path, (content, _mode) in sorted(checksummed.items())
    ).encode()
    members = {**checksummed, artifact.ARTIFACT_CHECKSUMS: (sums, 0o100644)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for path, (content, mode) in sorted(members.items()):
            archive.writestr(artifact._zip_info(f"bundle/{path}", mode), content)
    return output.getvalue()


def _zip_with_entry(
    name: str,
    content: bytes,
    *,
    compression: int = zipfile.ZIP_STORED,
    mode: int = 0o100644,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression, compresslevel=9, strict_timestamps=True) as archive:
        info = artifact._zip_info(name, mode)
        info.compress_type = compression
        archive.writestr(info, content, compress_type=compression, compresslevel=9)
    return output.getvalue()


def _extract(verifier: types.ModuleType, tmp_path: Path, data: bytes | None = None) -> tuple[Path, Path]:
    archive_path = _write_archive(tmp_path, data or _archive())
    destination = tmp_path / "extract"
    root, _manifest = verifier.safe_extract(archive_path, destination, "python")
    return destination, root


def test_runtime_and_shipped_verifier_share_all_resource_bounds(verifier):
    for name in (
        "MAX_ARCHIVE_BYTES",
        "MAX_MEMBER_BYTES",
        "MAX_EXPANDED_BYTES",
        "MAX_MEMBER_COUNT",
        "MAX_COMPRESSION_RATIO",
        "MAX_PATH_BYTES",
        "MAX_PATH_COMPONENT_BYTES",
        "READ_CHUNK_BYTES",
    ):
        assert getattr(verifier, name) == getattr(artifact, name)


def test_type_mismatch_performs_zero_destination_writes(verifier, tmp_path):
    archive_path = _write_archive(tmp_path, _archive())
    destination = tmp_path / "must-not-exist"

    with pytest.raises(verifier.VerificationError, match="unexpected artifact type"):
        verifier.safe_extract(archive_path, destination, "cfn")

    assert not destination.exists()


def test_destination_must_not_preexist_and_is_not_modified(verifier, tmp_path):
    archive_path = _write_archive(tmp_path, _archive())
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep\n")

    with pytest.raises(verifier.VerificationError, match="must not preexist"):
        verifier.safe_extract(archive_path, destination, "python")

    assert marker.read_text() == "keep\n"


def test_archive_path_replacement_after_verification_cannot_change_extracted_bytes(verifier, monkeypatch, tmp_path):
    archive_path = _write_archive(tmp_path, _archive(content="verified bytes\n"))
    replacement = _write_archive(tmp_path, _archive(content="replacement bytes\n"), "replacement.zip")
    destination = tmp_path / "extract"
    reserve_destination = verifier.reserve_destination

    def replace_then_reserve(path):
        os.replace(replacement, archive_path)
        return reserve_destination(path)

    monkeypatch.setattr(verifier, "reserve_destination", replace_then_reserve)
    root, _manifest = verifier.safe_extract(archive_path, destination, "python")

    assert (root / "payload.txt").read_text() == "verified bytes\n"
    assert zipfile.ZipFile(archive_path).read("bundle/payload.txt") == b"replacement bytes\n"


def test_destination_symlink_insertion_is_rejected_and_removed(verifier, monkeypatch, tmp_path):
    archive_path = _write_archive(tmp_path, _archive())
    destination = tmp_path / "extract"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("untouched\n")
    real_open_child = verifier.open_child_directory
    real_rmdir = verifier.os.rmdir
    real_symlink = verifier.os.symlink
    inserted = False

    def racing_open_child(parent_fd, name):
        nonlocal inserted
        if name == destination.name and not inserted:
            inserted = True
            real_rmdir(name, dir_fd=parent_fd)
            real_symlink(outside, name, dir_fd=parent_fd)
        return real_open_child(parent_fd, name)

    monkeypatch.setattr(verifier, "open_child_directory", racing_open_child)
    with pytest.raises(verifier.VerificationError, match="symlink|safely open"):
        verifier.safe_extract(archive_path, destination, "python")

    assert inserted
    assert not destination.exists()
    assert marker.read_text() == "untouched\n"
    assert sorted(path.name for path in outside.iterdir()) == ["marker.txt"]


def test_child_directory_symlink_insertion_never_writes_outside_destination(verifier, monkeypatch, tmp_path):
    archive_path = _write_archive(tmp_path, _archive(path="nested/payload.txt"))
    destination = tmp_path / "extract"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("untouched\n")
    real_open_child = verifier.open_child_directory
    real_rmdir = verifier.os.rmdir
    real_symlink = verifier.os.symlink
    inserted = False

    def racing_open_child(parent_fd, name):
        nonlocal inserted
        if name == "nested" and not inserted:
            inserted = True
            real_rmdir(name, dir_fd=parent_fd)
            real_symlink(outside, name, dir_fd=parent_fd)
        return real_open_child(parent_fd, name)

    monkeypatch.setattr(verifier, "open_child_directory", racing_open_child)
    with pytest.raises(verifier.VerificationError, match="symlink|safely open"):
        verifier.safe_extract(archive_path, destination, "python")

    assert inserted
    assert not destination.exists()
    assert marker.read_text() == "untouched\n"
    assert not (outside / "payload.txt").exists()


def test_write_failure_cleans_every_partial_file_and_reserved_directory(verifier, monkeypatch, tmp_path):
    archive_path = _write_archive(tmp_path, _archive())
    destination = tmp_path / "extract"
    real_write = verifier.os.write
    writes = 0

    def partial_then_fail(descriptor, data):
        nonlocal writes
        writes += 1
        if writes == 1:
            real_write(descriptor, data[:1])
            raise OSError("injected write failure")
        return real_write(descriptor, data)

    monkeypatch.setattr(verifier.os, "write", partial_then_fail)
    with pytest.raises(OSError, match="injected write failure"):
        verifier.safe_extract(archive_path, destination, "python")

    assert writes == 1
    assert not destination.exists()


def test_outer_compression_bomb_is_rejected_before_any_member_read(verifier, monkeypatch, tmp_path):
    data = _zip_with_entry(
        "bundle/bomb.bin",
        b"\0" * (2 * 1024 * 1024),
        compression=zipfile.ZIP_DEFLATED,
    )
    archive_path = _write_archive(tmp_path, data)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("resource declarations must be rejected before decompression")

    monkeypatch.setattr(verifier, "read_member", unexpected_read)
    with pytest.raises(verifier.VerificationError, match="compression-ratio limit"):
        verifier.verify_archive(archive_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="compression-ratio limit"):
        artifact.verify_artifact_zip(data)


def test_oversized_outer_declaration_is_rejected_before_decompression(verifier, tmp_path):
    data = bytearray(_zip_with_entry("bundle/blob.bin", b"x"))
    central_header = data.rfind(b"PK\x01\x02")
    assert central_header >= 0
    struct.pack_into("<I", data, central_header + 24, artifact.MAX_MEMBER_BYTES + 1)
    archive_path = _write_archive(tmp_path, bytes(data))

    with pytest.raises(verifier.VerificationError, match="oversized member declaration"):
        verifier.verify_archive(archive_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="oversized member declaration"):
        artifact.verify_artifact_zip(bytes(data))


def test_nested_compression_bomb_is_rejected_by_runtime_and_shipped_verifier(verifier, tmp_path):
    nested = _zip_with_entry(
        "payload.bin",
        b"\0" * (2 * 1024 * 1024),
        compression=zipfile.ZIP_DEFLATED,
    )
    data = _raw_artifact({"lambda.zip": (nested, 0o100644)})
    archive_path = _write_archive(tmp_path, data)

    with pytest.raises(verifier.VerificationError, match="compression-ratio limit"):
        verifier.verify_archive(archive_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="compression-ratio limit"):
        artifact.verify_artifact_zip(data)
    with pytest.raises(artifact.DownloadableArtifactError, match="compression-ratio limit"):
        artifact.build_artifact_zip(
            prefix="bundle",
            artifact_type="python",
            files={"lambda.zip": nested},
        )


def test_member_count_bound_is_checked_before_member_reads(verifier, monkeypatch, tmp_path):
    data = _archive()
    archive_path = _write_archive(tmp_path, data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        member_count = len(archive.infolist())
    verifier.MAX_MEMBER_COUNT = member_count - 1
    monkeypatch.setattr(artifact, "MAX_MEMBER_COUNT", member_count - 1)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("member-count declarations must be rejected before reads")

    monkeypatch.setattr(verifier, "read_member", unexpected_read)
    with pytest.raises(verifier.VerificationError, match="member-count limit"):
        verifier.verify_archive(archive_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="member-count limit"):
        artifact.verify_artifact_zip(data)


def test_archive_aggregate_bound_is_checked_before_member_reads(verifier, monkeypatch, tmp_path):
    data = _archive()
    archive_path = _write_archive(tmp_path, data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        expanded = sum(info.file_size for info in archive.infolist())
    verifier.MAX_EXPANDED_BYTES = expanded - 1
    monkeypatch.setattr(artifact, "MAX_EXPANDED_BYTES", expanded - 1)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("aggregate declarations must be rejected before reads")

    monkeypatch.setattr(verifier, "read_member", unexpected_read)
    with pytest.raises(verifier.VerificationError, match="aggregate expanded-size limit"):
        verifier.verify_archive(archive_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="aggregate expanded-size limit"):
        artifact.verify_artifact_zip(data)


def test_directory_oversized_file_is_rejected_from_stat_before_read(verifier, monkeypatch, tmp_path):
    _destination, root = _extract(verifier, tmp_path)
    oversized = root / "zz-oversized.bin"
    with oversized.open("wb") as stream:
        stream.seek(verifier.MAX_MEMBER_BYTES)
        stream.write(b"x")
    read_regular_file = verifier.read_regular_file

    def guarded_read(path, rel, before):
        if rel == oversized.name:
            pytest.fail("oversized file must not be read")
        return read_regular_file(path, rel, before)

    monkeypatch.setattr(verifier, "read_regular_file", guarded_read)
    with pytest.raises(verifier.VerificationError, match="member exceeds size limit"):
        verifier.verify_directory(root, "python")


def test_directory_member_and_aggregate_bounds_are_enforced(verifier, tmp_path):
    _destination, root = _extract(verifier, tmp_path)
    payload_files = [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
    total = sum(path.stat().st_size for path in payload_files)

    verifier.MAX_MEMBER_COUNT = len(payload_files) - 1
    with pytest.raises(verifier.VerificationError, match="member-count limit"):
        verifier.verify_directory(root, "python")

    verifier.MAX_MEMBER_COUNT = artifact.MAX_MEMBER_COUNT
    verifier.MAX_EXPANDED_BYTES = total - 1
    with pytest.raises(verifier.VerificationError, match="aggregate expanded-size limit"):
        verifier.verify_directory(root, "python")


def test_control_special_duplicate_and_file_directory_conflicts_are_rejected(verifier, tmp_path):
    control = _raw_artifact({".git/config": (b"[core]\n", 0o100644)})
    control_path = _write_archive(tmp_path, control, "control.zip")
    with pytest.raises(verifier.VerificationError, match="reserved checkout metadata"):
        verifier.verify_archive(control_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="reserved checkout metadata"):
        artifact.verify_artifact_zip(control)

    conflict = _raw_artifact({"node": (b"file", 0o100644), "node/child.bin": (b"child", 0o100644)})
    conflict_path = _write_archive(tmp_path, conflict, "conflict.zip")
    with pytest.raises(verifier.VerificationError, match="file/directory path conflict"):
        verifier.verify_archive(conflict_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="file/directory path conflict"):
        artifact.verify_artifact_zip(conflict)

    special = _zip_with_entry("bundle/link", b"target", mode=stat.S_IFLNK | 0o777)
    special_path = _write_archive(tmp_path, special, "special.zip")
    with pytest.raises(verifier.VerificationError, match="unsafe or non-canonical ZIP metadata"):
        verifier.verify_archive(special_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="unsafe or non-canonical ZIP metadata"):
        artifact.verify_artifact_zip(special)

    duplicate_output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate_output, "w", compression=zipfile.ZIP_STORED) as archive:
            info = artifact._zip_info("bundle/duplicate.bin", 0o100644)
            archive.writestr(info, b"first")
            archive.writestr(info, b"second")
    duplicate = duplicate_output.getvalue()
    duplicate_path = _write_archive(tmp_path, duplicate, "duplicate.zip")
    with pytest.raises(verifier.VerificationError, match="duplicated"):
        verifier.verify_archive(duplicate_path)
    with pytest.raises(artifact.DownloadableArtifactError, match="duplicated"):
        artifact.verify_artifact_zip(duplicate)


def test_normal_archive_extraction_modes_and_real_git_checkout_metadata_succeed(verifier, tmp_path):
    data = _archive()
    artifact.verify_artifact_zip(data, expected_artifact_type="python")
    destination, root = _extract(verifier, tmp_path, data)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "payload.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((root / "run.sh").stat().st_mode) == 0o755
    assert (root / "payload.txt").read_text() == "original\n"

    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, timeout=10)
    manifest = verifier.verify_directory(root, "python")
    assert manifest["artifact_type"] == "python"

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(root / artifact.ARTIFACT_VERIFIER),
            "--directory",
            str(root),
            "--artifact-type",
            "python",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("verified python")
