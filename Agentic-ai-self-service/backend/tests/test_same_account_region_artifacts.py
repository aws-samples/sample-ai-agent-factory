"""Same-account multi-region deploys must use a validated regional S3 bucket.

Cross-account onboarding already validates and freezes a per-region artifacts
bucket.  A region-only target used to store only the region, so step Lambdas
silently reused the platform's home-region bucket while creating AgentCore
resources in another region.  These tests pin the equivalent same-account
admission and event-routing contract.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest
from app import deployment_handler
from app.routers import admin
from app.services import deploy_target as dt
from app.services import step_clients


class _SettingsTable:
    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, *, Item):
        self.items[(Item["org_id"], Item["sk"])] = dict(Item)

    def get_item(self, *, Key):
        item = self.items.get((Key["org_id"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def query(self, **_kwargs):
        return {"Items": [dict(item) for (_org_id, sk), item in self.items.items() if sk.startswith("TARGET#region#")]}


@pytest.fixture(autouse=True)
def _home_region(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")


def test_region_registration_persists_its_validated_bucket_and_account(monkeypatch):
    table = _SettingsTable()
    monkeypatch.setattr(dt, "_settings_table", lambda: table)

    dt.add_region(
        "eu-west-1",
        account_id="123456789012",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
    )

    assert dt.get_region_target("eu-west-1") == {
        "org_id": "default",
        "sk": "TARGET#region#eu-west-1",
        "region": "eu-west-1",
        "account_id": "123456789012",
        "artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
    }
    assert dt.list_regions() == ["eu-west-1"]
    assert dt.list_region_targets() == [dt.get_region_target("eu-west-1")]


def test_live_region_resolution_revalidates_account_bucket_and_region(monkeypatch):
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts
    session_for_target = MagicMock(return_value=session)
    validate_bucket = MagicMock(return_value="agentcore-flows-artifacts-123456789012-eu-west-1")

    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "get_region_target",
        lambda _region: {
            "region": "eu-west-1",
            "account_id": "123456789012",
            "artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
        },
    )
    monkeypatch.setattr(dt, "session_for_target", session_for_target)
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)

    assert dt.resolve_registered_region_target("eu-west-1") == {
        "account_id": "123456789012",
        "region": "eu-west-1",
        "artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
    }
    session_for_target.assert_called_once_with(
        account_id=None,
        region="eu-west-1",
        require_gate=False,
    )
    validate_bucket.assert_called_once_with(
        session,
        account_id="123456789012",
        region="eu-west-1",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
        same_account=True,
    )


def test_live_region_resolution_rejects_a_changed_platform_account(monkeypatch):
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "get_region_target",
        lambda _region: {
            "region": "eu-west-1",
            "account_id": "123456789012",
            "artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
        },
    )
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)

    with pytest.raises(dt.TargetError, match="registered for account.*123456789012"):
        dt.resolve_registered_region_target("eu-west-1")


def test_legacy_region_without_a_bucket_is_not_deployable(monkeypatch):
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "get_region_target",
        lambda _region: {"region": "eu-west-1"},
    )

    with pytest.raises(dt.TargetError, match="artifact bucket.*re-register"):
        dt.resolve_registered_region_target("eu-west-1")


def test_admin_region_onboarding_validates_before_persisting(monkeypatch):
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    add_region = MagicMock()
    validate_bucket = MagicMock(return_value="agentcore-flows-artifacts-123456789012-eu-west-1")
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)
    monkeypatch.setattr(dt, "add_region", add_region)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "list_region_targets",
        lambda: [
            {
                "region": "eu-west-1",
                "account_id": "123456789012",
                "artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
            }
        ],
    )

    body = admin.RegionTargetRequest(
        region="eu-west-1",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
    )
    result = asyncio.run(admin.add_region_target(body, _caller_sub="admin"))

    validate_bucket.assert_called_once_with(
        session,
        account_id="123456789012",
        region="eu-west-1",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
        same_account=True,
    )
    add_region.assert_called_once_with(
        "eu-west-1",
        account_id="123456789012",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
    )
    assert result["validated"] is True
    assert result["artifact_bucket"] == "agentcore-flows-artifacts-123456789012-eu-west-1"


def test_admin_region_onboarding_fails_closed_before_persisting(monkeypatch):
    add_region = MagicMock()
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(
        dt,
        "validate_artifact_bucket",
        MagicMock(side_effect=dt.TargetError("bucket is not regional")),
    )
    monkeypatch.setattr(dt, "add_region", add_region)

    body = admin.RegionTargetRequest(
        region="eu-west-1",
        artifact_bucket="agentcore-flows-artifacts-123456789012-eu-west-1",
    )
    with pytest.raises(admin.HTTPException, match="bucket is not regional"):
        asyncio.run(admin.add_region_target(body, _caller_sub="admin"))

    add_region.assert_not_called()


def test_region_only_step_event_uses_the_frozen_target_bucket(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts-us-east-1")

    assert (
        step_clients.artifacts_bucket_for_event(
            {
                "target_region": "eu-west-1",
                "target_artifact_bucket": "agentcore-flows-artifacts-123456789012-eu-west-1",
            }
        )
        == "agentcore-flows-artifacts-123456789012-eu-west-1"
    )


def test_region_only_step_event_fails_closed_without_the_frozen_bucket(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts-us-east-1")

    with pytest.raises(ValueError, match="Same-account.*target_artifact_bucket"):
        step_clients.artifacts_bucket_for_event({"target_region": "eu-west-1"})


def test_home_region_step_event_keeps_the_platform_bucket(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts-us-east-1")

    assert step_clients.artifacts_bucket_for_event({"target_region": "us-east-1"}) == "platform-artifacts-us-east-1"


def test_deploy_admission_uses_the_registered_region_target_not_a_bare_allowlist():
    source = inspect.getsource(deployment_handler.handle_deploy)
    assert "resolve_registered_region_target" in source
    assert 'target_artifact_bucket = target["artifact_bucket"]' in source


# F-41b: the stack grants its roles agentcore-flows-artifacts-{account}-* only
# (infra/stacks/platform/regional_artifact_bucket_grant.py), so a same-account bucket
# outside that namespace would register and then fail inside Step Functions.


@pytest.mark.parametrize(
    "bucket",
    [
        "platform-artifacts-eu-west-1",
        # Another account's namespace: the grants' aws:ResourceAccount would not match.
        "agentcore-flows-artifacts-999999999999-eu-west-1",
        # The prefix without the account boundary.
        "agentcore-flows-artifacts-eu-west-1",
    ],
)
def test_admin_region_onboarding_refuses_a_bucket_outside_the_namespace(monkeypatch, bucket):
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    add_region = MagicMock()
    validate_bucket = MagicMock(return_value=bucket)
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)
    monkeypatch.setattr(dt, "add_region", add_region)

    body = admin.RegionTargetRequest(region="eu-west-1", artifact_bucket=bucket)
    with pytest.raises(admin.HTTPException) as exc:
        asyncio.run(admin.add_region_target(body, _caller_sub="admin"))

    assert exc.value.status_code == 400
    assert "agentcore-flows-artifacts-123456789012-" in exc.value.detail
    validate_bucket.assert_not_called()
    add_region.assert_not_called()


def test_the_default_bucket_name_is_inside_the_namespace():
    assert (
        dt.require_platform_bucket_namespace("123456789012", "eu-west-1", None)
        == "agentcore-flows-artifacts-123456789012-eu-west-1"
    )
    assert (
        dt.require_platform_bucket_namespace("123456789012", "eu-west-1", "agentcore-flows-artifacts-123456789012-x")
        == "agentcore-flows-artifacts-123456789012-x"
    )


def test_live_region_resolution_refuses_a_legacy_row_outside_the_namespace(monkeypatch):
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    validate_bucket = MagicMock()
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "get_region_target",
        lambda _region: {
            "region": "eu-west-1",
            "account_id": "123456789012",
            "artifact_bucket": "platform-artifacts-eu-west-1",
        },
    )
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)

    with pytest.raises(dt.TargetError, match="agentcore-flows-artifacts-123456789012-"):
        dt.resolve_registered_region_target("eu-west-1")
    validate_bucket.assert_not_called()


def test_a_cross_account_target_keeps_its_custom_bucket(monkeypatch):
    """The namespace is the PLATFORM's grant; a target account's role policy is its owner's."""
    validate_bucket = MagicMock(return_value="customer-owned-bucket")
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1"])
    monkeypatch.setattr(
        dt,
        "get_account",
        lambda _a: {
            "region": "eu-west-1",
            "role_arn": "arn:aws:iam::222222222222:role/AgentCoreFlowsDeploymentRole",
            "artifact_bucket": "customer-owned-bucket",
        },
    )
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(dt, "validate_execution_roles", lambda *_a, **_k: ("r", "m", "h"))
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)

    assert dt.resolve_registered_account_target("222222222222")["artifact_bucket"] == "customer-owned-bucket"
    assert validate_bucket.call_args.kwargs.get("same_account", False) is False
