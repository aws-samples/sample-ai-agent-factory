"""Phase 7 wiring: the per-deploy client factory (services/step_clients.py).

The critical property: with NO target on the event, the factory returns a
default-session client in the home region — byte-for-byte the previous behavior.
With a target, it delegates to deploy_target.session_for_target.
"""

from __future__ import annotations

import boto3
from app.services import step_clients as sc


def test_no_target_returns_home_region(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    monkeypatch.delenv("DEPLOY_TARGETS_ENABLED", raising=False)
    session = sc.session_for_event({})
    assert isinstance(session, boto3.Session)
    assert session.region_name == "us-east-1"


def test_event_region_overrides_home(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    session = sc.session_for_event({"target_region": "eu-west-1"})
    assert session.region_name == "eu-west-1"


def test_none_event_is_safe(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    session = sc.session_for_event(None)
    assert session.region_name == "us-east-1"


def test_client_is_default_session_when_no_target(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    c = sc.client({}, "s3")
    # A real boto3 s3 client (no assume-role happened).
    assert c.meta.service_model.service_name == "s3"


def test_target_account_delegates_to_deploy_target(monkeypatch):
    # When a target account is present, the factory MUST route through
    # deploy_target.session_for_target with require_gate=False (the deployment
    # Lambda already gated) and the threaded role_arn (no Settings lookup in the
    # step Lambda). The landed-account dry-run check still runs inside.
    called = {}

    def _fake_session_for_target(account_id=None, region=None, *, require_gate=True, role_arn=None):
        called["account_id"] = account_id
        called["region"] = region
        called["require_gate"] = require_gate
        called["role_arn"] = role_arn
        return boto3.Session(region_name=region or "us-east-1")

    monkeypatch.setattr("app.services.deploy_target.session_for_target", _fake_session_for_target)
    sc.session_for_event(
        {
            "target_account_id": "986177197847",
            "target_region": "us-east-1",
            "target_role_arn": "arn:aws:iam::986177197847:role/AgentCoreFlowsDeploymentRole",
        }
    )
    assert called["account_id"] == "986177197847"
    assert called["region"] == "us-east-1"
    assert called["require_gate"] is False  # step path trusts the deploy-time gate
    assert called["role_arn"].endswith("AgentCoreFlowsDeploymentRole")


def test_step_path_does_not_regate(monkeypatch):
    # The step path must NOT re-check targets_enabled() (the step Lambda lacks
    # the Settings env). With a threaded role_arn + require_gate=False, a disabled
    # flag does NOT block — the assume-role is attempted directly.
    seen = {}

    def _fake(account_id=None, region=None, *, require_gate=True, role_arn=None):
        seen["require_gate"] = require_gate
        return boto3.Session(region_name="us-east-1")

    monkeypatch.setattr("app.services.deploy_target.session_for_target", _fake)
    sc.session_for_event({"target_account_id": "986177197847", "target_role_arn": "arn:...role/X"})
    assert seen["require_gate"] is False
