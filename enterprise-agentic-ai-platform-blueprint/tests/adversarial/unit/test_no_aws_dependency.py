"""Mechanical proof that the harness and its unit tests touch no AWS.

Two checks:

* a static scan of every harness and unit-test source file for AWS SDK,
  networking and subprocess imports, and for credential-file access;
* a runtime check that the full harness path (catalog validation, manifest
  load, assertion, evidence build and write) completes with ``socket.socket``
  disabled.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from harness import (
    CATALOG,
    AuditEvidence,
    AuditSource,
    ObservedOutcome,
    TwinLedger,
    assert_case,
    assert_catalog_valid,
    build_record,
    load_manifest,
)
from harness.catalog import case_by_id
from harness.evidence import EvidenceWriter

pytestmark = pytest.mark.adversarial

SUITE_ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = SUITE_ROOT / "harness"
UNIT_DIR = SUITE_ROOT / "unit"
EXAMPLE_MANIFEST = SUITE_ROOT / "fixtures" / "manifest.example.json"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

#: Module roots the harness must never import.
FORBIDDEN_MODULE_ROOTS = frozenset(
    {
        "boto3",
        "botocore",
        "requests",
        "urllib",
        "urllib3",
        "http",
        "socket",
        "subprocess",
        "ssl",
    }
)

#: Module roots no unit test may import (probes are the only AWS layer).
FORBIDDEN_TEST_MODULE_ROOTS = frozenset({"boto3", "botocore", "requests", "urllib3"})

CREDENTIAL_FILE_ACCESS = (
    "credentials.csv",
    "accessKeys.csv",
    "read_csv",
    "csv.reader",
    "csv.DictReader",
)


def python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py"))


def imported_module_roots(path: Path) -> set[str]:
    """Top-level module names imported anywhere in ``path``, via AST.

    An AST walk (rather than a substring scan) means this file's own list of
    forbidden names is data, not a false positive, and it also catches imports
    nested inside functions.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


@pytest.mark.parametrize("path", python_files(HARNESS_DIR), ids=lambda p: p.name)
def test_harness_modules_import_no_aws_sdk_or_network(path: Path):
    forbidden = imported_module_roots(path) & FORBIDDEN_MODULE_ROOTS
    assert not forbidden, f"{path.name} must not import {sorted(forbidden)}"


@pytest.mark.parametrize("path", python_files(HARNESS_DIR), ids=lambda p: p.name)
def test_harness_modules_never_read_credential_files(path: Path):
    source = path.read_text(encoding="utf-8")
    for needle in CREDENTIAL_FILE_ACCESS:
        assert needle not in source, f"{path.name} must not read {needle!r}"


@pytest.mark.parametrize("path", python_files(UNIT_DIR), ids=lambda p: p.name)
def test_unit_tests_import_no_aws_sdk(path: Path):
    forbidden = imported_module_roots(path) & FORBIDDEN_TEST_MODULE_ROOTS
    assert not forbidden, f"{path.name} must not import {sorted(forbidden)}"


def test_the_forbidden_import_scan_actually_detects_an_import(tmp_path: Path):
    """Test the test: the AST scan must catch a real import."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import boto3\n\n\ndef f():\n    from botocore.exceptions import ClientError\n",
        encoding="utf-8",
    )
    assert imported_module_roots(offender) & FORBIDDEN_MODULE_ROOTS == {
        "boto3",
        "botocore",
    }


def test_harness_package_does_not_pull_in_boto3():
    import sys

    for module_name in list(sys.modules):
        if module_name.startswith("harness"):
            module = sys.modules[module_name]
            assert "boto3" not in getattr(module, "__dict__", {})


def test_full_harness_path_runs_with_networking_disabled(tmp_path: Path, monkeypatch):
    """The core path must not need a socket. Probes are the only AWS layer."""

    def no_sockets(*args, **kwargs):
        raise AssertionError("the harness must not open a socket")

    monkeypatch.setattr(socket, "socket", no_sockets)
    monkeypatch.setattr(socket, "create_connection", no_sockets)

    assert_catalog_valid(CATALOG)

    resolved = load_manifest(EXAMPLE_MANIFEST).resolve(
        {
            "AGENTICAI_ACCOUNT_MANAGEMENT": "111111111111",
            "AGENTICAI_ACCOUNT_PLATFORM": "222222222222",
            "AGENTICAI_ACCOUNT_WORKSTREAM": "333333333333",
            "AWS_ACCESS_KEY_ID_MANAGEMENT": "x",
            "AWS_SECRET_ACCESS_KEY_MANAGEMENT": "x",
            "AWS_ACCESS_KEY_ID_PLATFORM": "x",
            "AWS_SECRET_ACCESS_KEY_PLATFORM": "x",
            "AWS_ACCESS_KEY_ID_WORKSTREAM": "x",
            "AWS_SECRET_ACCESS_KEY_WORKSTREAM": "x",
            "AGENTICAI_ADVERSARIAL_EXTERNAL_ID": "external-id-placeholder",
        }
    )
    assert resolved.complete

    ledger = TwinLedger()
    positive = case_by_id("MEM-01-P")
    assert_case(
        positive,
        ObservedOutcome.from_success(request_id="req-1"),
        ledger=ledger,
        extras={"test_id": "node::MEM-01-P"},
    )

    writer = EvidenceWriter.for_run(tmp_path, resolved)
    writer.record(
        build_record(
            case=positive,
            test_id="node::MEM-01-P",
            principal=resolved.principal(positive.principal_ref),
            outcome=ObservedOutcome.from_success(request_id="req-1"),
            verdict="pass",
            region="us-east-1",
            manifest_sha=resolved.manifest_sha,
            live_mode=True,
            audit_evidence=(
                AuditEvidence(source=AuditSource.CLOUDTRAIL, locator="event-1"),
            ),
            commit=COMMIT,
        )
    )
    assert writer.flush() is not None


def test_no_aws_environment_variables_are_required_by_the_unit_suite(monkeypatch):
    """The unit suite must pass on a machine with nothing configured."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AGENTICAI_ADVERSARIAL_LIVE",
        "AGENTICAI_ADVERSARIAL_MANIFEST",
    ):
        monkeypatch.delenv(name, raising=False)
    assert_catalog_valid(CATALOG)
    assert load_manifest(EXAMPLE_MANIFEST).accounts
