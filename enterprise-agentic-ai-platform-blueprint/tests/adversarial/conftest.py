"""Shared fixtures for the adversarial suite.

This conftest deliberately makes **no** AWS calls at import or collection time.
The default identity probe is constructed lazily and is only ever invoked once
the account mapping already resolves from the environment, so a developer
machine with no credentials never touches the network.

Markers are registered here rather than in ``pytest.ini`` so that this subtree
owns its own configuration.

Environment contract (see ``README.md`` in this directory):

  AGENTICAI_ADVERSARIAL_LIVE=1            enable live cases
  AGENTICAI_ADVERSARIAL_REQUIRE_LIVE=1    fail the run if live is unavailable
  AGENTICAI_ADVERSARIAL_MANIFEST=<path>   account/role manifest JSON
  AGENTICAI_ADVERSARIAL_EVIDENCE_DIR=<d>  where the evidence bundle is written
  AGENTICAI_ACCOUNT_MANAGEMENT|PLATFORM|WORKSTREAM
                                          12-digit account ids (never committed)
  AWS_ACCESS_KEY_ID_<PREFIX> / AWS_SECRET_ACCESS_KEY_<PREFIX>
    or AWS_PROFILE_<PREFIX>               short-lived credentials per account

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SUITE_ROOT = Path(__file__).resolve().parent
if str(_SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUITE_ROOT))

from harness import (  # noqa: E402  (path bootstrap must run first)
    CATALOG,
    EvidenceWriter,
    LiveModeStatus,
    TwinLedger,
    assert_catalog_valid,
    live_mode_status,
)
from harness.livemode import ENV_EVIDENCE_DIR  # noqa: E402

EXAMPLE_MANIFEST = _SUITE_ROOT / "fixtures" / "manifest.example.json"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "adversarial: adversarial verification case or harness unit test",
    )
    config.addinivalue_line(
        "markers",
        "adversarial_live: requires a live three-account deployment; skipped "
        "unless AGENTICAI_ADVERSARIAL_LIVE=1, and errors if live mode is "
        "requested but unavailable",
    )


def _default_identity_probe():
    """Build an STS-backed identity probe, or ``None`` if boto3 is absent.

    Imported lazily so the harness unit tests never pull in an AWS SDK.
    """
    try:
        import boto3  # noqa: PLC0415 - intentional lazy import
    except ImportError:
        return None

    def probe(account_key: str, expected_account_id: str) -> str:
        prefix = account_key.split("-", 1)[0].upper()
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError(
                "AWS_REGION or AWS_DEFAULT_REGION is required for adversarial live mode"
            )
        access_key = os.environ.get(f"AWS_ACCESS_KEY_ID_{prefix}")
        secret_key = os.environ.get(f"AWS_SECRET_ACCESS_KEY_{prefix}")
        token = os.environ.get(f"AWS_SESSION_TOKEN_{prefix}")
        if access_key and secret_key:
            session = boto3.session.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=token,
                region_name=region,
            )
        else:
            session = boto3.session.Session(
                profile_name=os.environ.get(f"AWS_PROFILE_{prefix}"),
                region_name=region,
            )
        return session.client("sts").get_caller_identity()["Account"]

    return probe


@pytest.fixture(scope="session")
def live_mode() -> LiveModeStatus:
    """Evaluate the live-mode gate once per session.

    Returns the status; it does not raise. ``cases/conftest.py`` turns an
    unsatisfied live request into an error, and the harness unit tests assert
    the gate's behaviour directly with synthetic environments.
    """
    return live_mode_status(identity_probe=_default_identity_probe())


@pytest.fixture(scope="session")
def region(live_mode: LiveModeStatus) -> str:
    if live_mode.resolved is not None:
        return live_mode.resolved.manifest.primary_region
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "offline-test-region"
    )


@pytest.fixture(scope="session")
def twin_ledger() -> TwinLedger:
    """One ledger per session: negatives consult it for their twin."""
    return TwinLedger()


@pytest.fixture(scope="session")
def evidence_writer(live_mode: LiveModeStatus):
    """Collects evidence records and writes the bundle at session end."""
    writer = EvidenceWriter.for_run(
        os.environ.get(ENV_EVIDENCE_DIR) or None, live_mode.resolved
    )
    yield writer
    writer.flush()


@pytest.fixture(scope="session")
def catalog():
    """The validated case catalog.

    Validation runs once and fails loudly: an inconsistent catalog (a denial
    with no positive twin, a forbidden proof code, an uncovered domain) must
    stop the suite rather than quietly under-test.
    """
    assert_catalog_valid(CATALOG)
    return CATALOG
