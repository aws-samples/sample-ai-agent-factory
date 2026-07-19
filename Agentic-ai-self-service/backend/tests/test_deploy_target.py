"""Phase 7: multi-region/account deployment targets (opt-in).

Verifies the OFF-BY-DEFAULT gate, region allowlist enforcement, and that
same-account resolution returns the default session unchanged. moto-backed
settings table (reuses the tag-policy table shape).
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest

moto = pytest.importorskip("moto")
from app.services import deploy_target as dt  # noqa: E402
from moto import mock_aws  # noqa: E402


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="TagPolicy",
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TAG_POLICY_TABLE_NAME", "TagPolicy")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    # Ensure the env override isn't accidentally on.
    monkeypatch.delenv("DEPLOY_TARGETS_ENABLED", raising=False)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _create_table()
        yield


# -- feature gate: OFF by default --------------------------------------------


def test_disabled_by_default(aws):
    assert dt.targets_enabled() is False


def test_enable_then_disabled_flag(aws):
    dt.set_targets_enabled(True)
    assert dt.targets_enabled() is True
    dt.set_targets_enabled(False)
    assert dt.targets_enabled() is False


def test_env_override_enables(aws, monkeypatch):
    monkeypatch.setenv("DEPLOY_TARGETS_ENABLED", "true")
    assert dt.targets_enabled() is True


# -- region resolution -------------------------------------------------------


def test_resolve_region_home_when_none(aws):
    assert dt.resolve_region(None) == "us-east-1"


def test_resolve_region_same_as_home_ok(aws):
    assert dt.resolve_region("us-east-1") == "us-east-1"


def test_resolve_other_region_blocked_when_disabled(aws):
    with pytest.raises(dt.TargetError, match="disabled"):
        dt.resolve_region("us-west-2")


def test_resolve_other_region_blocked_when_not_allowlisted(aws):
    dt.set_targets_enabled(True)
    with pytest.raises(dt.TargetError, match="allowlist"):
        dt.resolve_region("us-west-2")


def test_resolve_other_region_ok_when_allowlisted(aws):
    dt.set_targets_enabled(True)
    dt.add_region("us-west-2")
    assert dt.resolve_region("us-west-2") == "us-west-2"


# -- session resolution ------------------------------------------------------


def test_home_account_returns_default_session(aws):
    # No account_id → default session (unchanged path), even when disabled.
    sess = dt.session_for_target(account_id=None, region=None)
    assert isinstance(sess, boto3.Session)


def test_cross_account_blocked_when_disabled(aws):
    with pytest.raises(dt.TargetError, match="disabled"):
        dt.session_for_target(account_id="123456789012", region="us-east-1")


def test_cross_account_unregistered_rejected(aws):
    dt.set_targets_enabled(True)
    with pytest.raises(dt.TargetError, match="not a registered"):
        dt.session_for_target(account_id="123456789012", region="us-east-1")


def test_account_registry_roundtrip(aws):
    dt.set_targets_enabled(True)
    dt.add_account("123456789012", "arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole", "us-east-1")
    got = dt.get_account("123456789012")
    assert got["role_arn"].endswith("AgentCoreFlowsDeploymentRole")
    assert len(dt.list_accounts()) == 1
