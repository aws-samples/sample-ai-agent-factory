from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.services import deploy_target as dt
from app.services import runtime_target_context as rtc
from app.services.agent_versions_store import AgentVersion
from fastapi import HTTPException

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "444455556666"
REGION = "eu-west-1"
KEY_ID = "12345678-1234-1234-1234-1234567890ab"
KEY_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{KEY_ID}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{dt.DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME}"
RUNTIME_ID = "target_runtime_1"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"


def _key_policy(*, include_service: bool = True) -> dict:
    statements = [
        {
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
            "Action": "kms:*",
            "Resource": "*",
        }
    ]
    if include_service:
        statements.append(
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT},
                    "ArnLike": {"aws:SourceArn": (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:batch-evaluate/*")},
                },
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _kms_session(*, metadata: dict | None = None, policy: dict | None = None):
    kms = MagicMock()
    kms.describe_key.return_value = {
        "KeyMetadata": metadata
        or {
            "Arn": KEY_ARN,
            "AWSAccountId": ACCOUNT,
            "KeyManager": "CUSTOMER",
            "KeySpec": "SYMMETRIC_DEFAULT",
            "KeyUsage": "ENCRYPT_DECRYPT",
            "KeyState": "Enabled",
            "Enabled": True,
        }
    }
    kms.get_key_policy.return_value = {"Policy": json.dumps(policy or _key_policy())}
    session = MagicMock()
    session.client.return_value = kms
    return session, kms


def test_valid_target_evaluation_key_is_verified_without_key_material():
    session, kms = _kms_session()

    assert (
        dt.validate_evaluation_kms_key(
            session,
            account_id=ACCOUNT,
            region=REGION,
            key_arn=KEY_ARN,
            deployment_role_arn=ROLE_ARN,
        )
        == KEY_ARN
    )

    session.client.assert_called_once_with("kms", region_name=REGION)
    kms.describe_key.assert_called_once_with(KeyId=KEY_ARN)
    kms.get_key_policy.assert_called_once_with(KeyId=KEY_ARN, PolicyName="default")


@pytest.mark.parametrize(
    ("key_arn", "account_id", "region", "message"),
    [
        (KEY_ARN, OTHER_ACCOUNT, REGION, "not target account"),
        (KEY_ARN, ACCOUNT, "us-west-2", "not target region"),
    ],
)
def test_target_evaluation_key_rejects_wrong_account_or_region(
    key_arn: str,
    account_id: str,
    region: str,
    message: str,
):
    session = MagicMock()
    with pytest.raises(dt.TargetError, match=message):
        dt.validate_evaluation_kms_key(
            session,
            account_id=account_id,
            region=region,
            key_arn=key_arn,
            deployment_role_arn=ROLE_ARN,
        )
    session.client.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"KeyState": "Disabled", "Enabled": False}, "must be enabled"),
        ({"KeySpec": "RSA_2048", "KeyUsage": "SIGN_VERIFY"}, "symmetric ENCRYPT_DECRYPT"),
    ],
)
def test_target_evaluation_key_rejects_disabled_or_asymmetric(overrides: dict, message: str):
    metadata = {
        "Arn": KEY_ARN,
        "AWSAccountId": ACCOUNT,
        "KeyManager": "CUSTOMER",
        "KeySpec": "SYMMETRIC_DEFAULT",
        "KeyUsage": "ENCRYPT_DECRYPT",
        "KeyState": "Enabled",
        "Enabled": True,
        **overrides,
    }
    session, kms = _kms_session(metadata=metadata)

    with pytest.raises(dt.TargetError, match=message):
        dt.validate_evaluation_kms_key(
            session,
            account_id=ACCOUNT,
            region=REGION,
            key_arn=KEY_ARN,
            deployment_role_arn=ROLE_ARN,
        )
    kms.get_key_policy.assert_not_called()


def test_target_evaluation_key_rejects_missing_service_policy():
    session, _kms = _kms_session(policy=_key_policy(include_service=False))

    with pytest.raises(dt.TargetError, match="bedrock-agentcore.amazonaws.com"):
        dt.validate_evaluation_kms_key(
            session,
            account_id=ACCOUNT,
            region=REGION,
            key_arn=KEY_ARN,
            deployment_role_arn=ROLE_ARN,
        )


def _version(*, evaluation_key: str | None) -> AgentVersion:
    return AgentVersion(
        runtime_name="release_bot",
        version_id="01a0d26eb4459a9516d12064ac4a8687",
        owner_sub="owner-1",
        created_at="2026-10-07T08:00:00+00:00",
        deployment_id="dep-1",
        agentcore_runtime_name="release_bot_1234",
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        status="succeeded",
        production_gate_required=True,
        evaluation_kms_key_arn=evaluation_key,
    )


def _deployment(*, evaluation_key: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        deployment_id="dep-1",
        version_id="01a0d26eb4459a9516d12064ac4a8687",
        user_id="owner-1",
        status="succeeded",
        delete_status=None,
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        runtime_protocol="HTTP",
        target_account_id=ACCOUNT,
        target_region=REGION,
        target_role_arn=ROLE_ARN,
        evaluation_kms_key_arn=evaluation_key,
    )


def _wire_authority(monkeypatch, *, version: AgentVersion, deployment: SimpleNamespace):
    version_store = MagicMock()
    version_store.get.return_value = version
    deployment_store = MagicMock()
    deployment_store.get.return_value = deployment
    monkeypatch.setattr(rtc, "get_versions_store", lambda: version_store)
    monkeypatch.setattr(rtc, "get_deployment_store", lambda: deployment_store)
    monkeypatch.setattr(rtc, "assert_owner", lambda owner, caller: None)
    session_for_event = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(rtc.step_clients, "session_for_event", session_for_event)
    return session_for_event


def test_legacy_gated_version_without_frozen_key_fails_closed(monkeypatch):
    session_for_event = _wire_authority(
        monkeypatch,
        version=_version(evaluation_key=None),
        deployment=_deployment(evaluation_key=None),
    )

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_version_target(
            "release_bot",
            "01a0d26eb4459a9516d12064ac4a8687",  # pragma: allowlist secret
            "owner-1",
        )
    assert exc.value.status_code == 503
    session_for_event.assert_not_called()


def test_adversarial_target_row_mutation_cannot_replace_version_key(monkeypatch):
    mutated_key_id = "-".join(("a" * 8, "b" * 4, "c" * 4, "d" * 4, "e" * 12))
    mutated_key = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{mutated_key_id}"
    session_for_event = _wire_authority(
        monkeypatch,
        version=_version(evaluation_key=KEY_ARN),
        deployment=_deployment(evaluation_key=mutated_key),
    )

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_version_target(
            "release_bot",
            "01a0d26eb4459a9516d12064ac4a8687",  # pragma: allowlist secret
            "owner-1",
        )
    assert exc.value.status_code == 503
    session_for_event.assert_not_called()
