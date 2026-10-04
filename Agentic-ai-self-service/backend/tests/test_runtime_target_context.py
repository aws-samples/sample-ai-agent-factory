"""Target-account routing for runtime-name observability APIs.

The friendly runtime name and production slot live in the platform account.
The runtime, its evaluation configuration, and its logs may not.  These tests
pin the authority chain used by evaluations, traces, cost, and dashboards:

    owner-checked slot -> owner-checked version -> exact deployment record
    -> step_clients.session_for_event(...)

An ARN-shaped string is not a credential source, and the ambient platform
session must never be used for a cross-account runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.services import runtime_target_context as rtc
from app.services.agent_versions_store import AgentVersion, RuntimeSlots
from app.services.auth import _LOCAL_DEV_SUB
from fastapi import HTTPException

TARGET_ACCOUNT = "222222222222"
TARGET_REGION = "eu-west-1"
TARGET_ROLE = f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentFactoryDeploymentRole"
RUNTIME_ID = "orders-runtime-id"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/{RUNTIME_ID}"


def _version(**overrides) -> AgentVersion:
    values = {
        "runtime_name": "orders",
        "version_id": "v1",
        "owner_sub": _LOCAL_DEV_SUB,
        "created_at": "2026-09-22T00:00:00+00:00",
        "deployment_id": "dep-1",
        "agentcore_runtime_name": "orders_deadbeef",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "status": "succeeded",
    }
    values.update(overrides)
    return AgentVersion(**values)


def _deployment(**overrides):
    values = {
        "deployment_id": "dep-1",
        "version_id": "v1",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "user_id": _LOCAL_DEV_SUB,
        "status": "succeeded",
        "delete_status": None,
        "target_account_id": TARGET_ACCOUNT,
        "target_region": TARGET_REGION,
        "target_role_arn": TARGET_ROLE,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _wire(monkeypatch, *, version=None, deployment=None):
    slots_store = MagicMock()
    slots_store.get.return_value = RuntimeSlots(
        runtime_name="orders",
        owner_sub=_LOCAL_DEV_SUB,
        production_version_id="v1",
    )
    versions_store = MagicMock()
    versions_store.get.return_value = version or _version()
    deployments = MagicMock()
    deployments.get.return_value = deployment or _deployment()
    monkeypatch.setattr(rtc, "get_slots_store", lambda: slots_store)
    monkeypatch.setattr(rtc, "get_versions_store", lambda: versions_store)
    monkeypatch.setattr(rtc, "get_deployment_store", lambda: deployments)
    return slots_store, versions_store, deployments


def test_cross_account_target_uses_the_frozen_deployment_session(monkeypatch):
    slots, versions, deployments = _wire(monkeypatch)
    target_session = MagicMock(name="target_session")
    session_for_event = MagicMock(return_value=target_session)
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    target = rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    slots.get.assert_called_once_with("orders", consistent=True)
    versions.get.assert_called_once_with("orders", "v1", consistent=True)
    deployments.get.assert_called_once_with("dep-1", consistent=True)
    assert target.runtime_id == "orders-runtime-id"
    assert target.version_id == "v1"
    assert target.deployment_id == "dep-1"
    assert target.region == TARGET_REGION
    assert target.account_id == TARGET_ACCOUNT
    session_for_event.assert_called_once_with(
        {
            "target_account_id": TARGET_ACCOUNT,
            "target_region": TARGET_REGION,
            "target_role_arn": TARGET_ROLE,
        }
    )
    target.client("logs")
    target_session.client.assert_called_once_with("logs", region_name=TARGET_REGION)


def test_missing_production_slot_is_not_found(monkeypatch):
    slots = MagicMock()
    slots.get.return_value = None
    versions = MagicMock()
    monkeypatch.setattr(rtc, "get_slots_store", lambda: slots)
    monkeypatch.setattr(rtc, "get_versions_store", lambda: versions)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 404
    versions.get.assert_not_called()


def test_slot_owner_is_checked_before_the_version_is_read(monkeypatch):
    slots = MagicMock()
    slots.get.return_value = RuntimeSlots(
        runtime_name="orders",
        owner_sub="another-tenant",
        production_version_id="v1",
    )
    versions = MagicMock()
    monkeypatch.setattr(rtc, "get_slots_store", lambda: slots)
    monkeypatch.setattr(rtc, "get_versions_store", lambda: versions)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 404
    versions.get.assert_not_called()


def test_version_owner_is_checked_before_the_deployment_is_read(monkeypatch):
    _, versions, deployments = _wire(
        monkeypatch,
        version=_version(owner_sub="another-tenant"),
    )

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 404
    versions.get.assert_called_once_with("orders", "v1", consistent=True)
    deployments.get.assert_not_called()


def test_same_account_non_home_region_is_not_collapsed_to_the_home_region(monkeypatch):
    runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/orders-runtime-id"
    _wire(
        monkeypatch,
        version=_version(runtime_arn=runtime_arn),
        deployment=_deployment(
            runtime_arn=runtime_arn,
            target_account_id=None,
            target_region="us-west-2",
            target_role_arn=None,
        ),
    )
    target_session = MagicMock(name="same_account_target_session")
    session_for_event = MagicMock(return_value=target_session)
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    target = rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert target.region == "us-west-2"
    assert target.account_id is None
    session_for_event.assert_called_once_with(
        {
            "target_account_id": None,
            "target_region": "us-west-2",
            "target_role_arn": None,
        }
    )


def test_same_account_runtime_arn_must_match_the_trusted_platform_account(monkeypatch):
    monkeypatch.setenv(
        "STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:111111111111:stateMachine:platform",
    )
    foreign_arn = "arn:aws:bedrock-agentcore:us-west-2:333333333333:runtime/orders-runtime-id"
    _wire(
        monkeypatch,
        version=_version(runtime_arn=foreign_arn),
        deployment=_deployment(
            runtime_arn=foreign_arn,
            target_account_id=None,
            target_region="us-west-2",
            target_role_arn=None,
        ),
    )
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


def test_same_account_runtime_arn_accepts_the_trusted_platform_account(monkeypatch):
    monkeypatch.setenv(
        "STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:111111111111:stateMachine:platform",
    )
    runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/orders-runtime-id"
    _wire(
        monkeypatch,
        version=_version(runtime_arn=runtime_arn),
        deployment=_deployment(
            runtime_arn=runtime_arn,
            target_account_id=None,
            target_region="us-west-2",
            target_role_arn=None,
        ),
    )
    target_session = MagicMock()
    monkeypatch.setattr(
        rtc.step_clients,
        "session_for_event",
        MagicMock(return_value=target_session),
    )

    target = rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert target.account_id is None
    assert target.region == "us-west-2"


@pytest.mark.parametrize(
    ("deployment_override", "message"),
    [
        ({"runtime_id": "some-other-runtime"}, "runtime"),
        ({"version_id": "some-other-version"}, "version"),
        ({"deployment_id": "some-other-deployment"}, "deployment"),
    ],
)
def test_version_cannot_redirect_observability_to_an_unrelated_record(
    monkeypatch,
    deployment_override,
    message,
):
    _wire(monkeypatch, deployment=_deployment(**deployment_override))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    assert message not in str(exc.value.detail).lower()
    session_for_event.assert_not_called()


def test_deployment_record_owner_is_checked_independently(monkeypatch):
    _wire(monkeypatch, deployment=_deployment(user_id="another-tenant"))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 404
    session_for_event.assert_not_called()


def test_missing_or_unreadable_deployment_authority_fails_closed(monkeypatch):
    _, _, deployments = _wire(monkeypatch)
    deployments.get.side_effect = RuntimeError("DynamoDB unavailable")
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    assert "target" not in str(exc.value.detail).lower()
    session_for_event.assert_not_called()


def test_assume_role_failure_is_not_replaced_with_the_platform_session(monkeypatch):
    _wire(monkeypatch)
    session_for_event = MagicMock(side_effect=RuntimeError("sts refused"))
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    assert "sts" not in str(exc.value.detail).lower()


def test_cross_account_target_requires_the_role_frozen_at_deploy_time(monkeypatch):
    _wire(monkeypatch, deployment=_deployment(target_role_arn=None))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "failed", "superseded", ""])
def test_production_slot_refuses_a_version_that_is_not_succeeded(monkeypatch, status):
    _, _, deployments = _wire(monkeypatch, version=_version(status=status))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    deployments.get.assert_not_called()
    session_for_event.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "in_progress", "failed", None])
def test_production_slot_refuses_a_deployment_that_is_not_succeeded(monkeypatch, status):
    _wire(monkeypatch, deployment=_deployment(status=status))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


def test_deleted_or_deleting_deployment_cannot_remain_an_observability_authority(monkeypatch):
    _wire(monkeypatch, deployment=_deployment(delete_status="deleting"))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


def test_role_without_target_account_cannot_be_silently_ignored(monkeypatch):
    _wire(
        monkeypatch,
        version=_version(runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/orders-runtime-id"),
        deployment=_deployment(
            runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/orders-runtime-id",
            target_account_id=None,
            target_region="us-west-2",
        ),
    )
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


def test_historical_home_target_region_comes_from_the_bound_arn_not_ambient_home(monkeypatch):
    bound_arn = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/orders-runtime-id"
    _wire(
        monkeypatch,
        version=_version(runtime_arn=bound_arn),
        deployment=_deployment(
            runtime_arn=bound_arn,
            target_account_id=None,
            target_region=None,
            target_role_arn=None,
        ),
    )
    monkeypatch.setattr(rtc, "_home_region", lambda: "us-east-1")
    target_session = MagicMock()
    session_for_event = MagicMock(return_value=target_session)
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    target = rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert target.region == "us-west-2"
    session_for_event.assert_called_once_with(
        {
            "target_account_id": None,
            "target_region": "us-west-2",
            "target_role_arn": None,
        }
    )


def test_cross_account_target_cannot_derive_a_missing_frozen_region(monkeypatch):
    _wire(monkeypatch, deployment=_deployment(target_region=None))
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


@pytest.mark.parametrize(
    ("version_arn", "deployment_arn", "target_region", "target_account"),
    [
        (None, RUNTIME_ARN, TARGET_REGION, TARGET_ACCOUNT),
        (RUNTIME_ARN, None, TARGET_REGION, TARGET_ACCOUNT),
        (
            RUNTIME_ARN,
            f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/other-runtime",
            TARGET_REGION,
            TARGET_ACCOUNT,
        ),
        (
            "not-an-arn",
            "not-an-arn",
            TARGET_REGION,
            TARGET_ACCOUNT,
        ),
        (
            RUNTIME_ARN,
            RUNTIME_ARN,
            "us-west-2",
            TARGET_ACCOUNT,
        ),
        (
            RUNTIME_ARN,
            RUNTIME_ARN,
            TARGET_REGION,
            "333333333333",
        ),
        (
            f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/other-runtime",
            f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/other-runtime",
            TARGET_REGION,
            TARGET_ACCOUNT,
        ),
    ],
)
def test_runtime_arn_must_bind_the_version_deployment_target_and_runtime(
    monkeypatch,
    version_arn,
    deployment_arn,
    target_region,
    target_account,
):
    _wire(
        monkeypatch,
        version=_version(runtime_arn=version_arn),
        deployment=_deployment(
            runtime_arn=deployment_arn,
            target_region=target_region,
            target_account_id=target_account,
            target_role_arn=(
                f"arn:aws:iam::{target_account}:role/AgentFactoryDeploymentRole" if target_account else None
            ),
        ),
    )
    session_for_event = MagicMock()
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_target("orders", _LOCAL_DEV_SUB)

    assert exc.value.status_code == 503
    session_for_event.assert_not_called()
