"""Runtime conversation logs must never default to infinite retention."""

from unittest.mock import MagicMock, call

import pytest
from app.services.runtime_deployer import (
    RUNTIME_LOG_RETENTION_DAYS,
    default_runtime_log_group_name,
    govern_default_runtime_log_group,
)
from botocore.exceptions import ClientError


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}},
        operation,
    )


def test_new_default_runtime_group_is_created_before_retention_is_applied():
    logs = MagicMock()

    name = govern_default_runtime_log_group(logs, "runtime_AbCdEf1234")

    expected = "/aws/bedrock-agentcore/runtimes/runtime_AbCdEf1234-DEFAULT"
    assert name == expected
    assert logs.mock_calls == [
        call.create_log_group(logGroupName=expected),
        call.put_retention_policy(
            logGroupName=expected,
            retentionInDays=RUNTIME_LOG_RETENTION_DAYS,
        ),
    ]
    assert RUNTIME_LOG_RETENTION_DAYS == 30


def test_existing_default_runtime_group_still_receives_retention():
    logs = MagicMock()
    logs.create_log_group.side_effect = _client_error(
        "ResourceAlreadyExistsException",
        "CreateLogGroup",
    )

    govern_default_runtime_log_group(logs, "runtime-existing")

    logs.put_retention_policy.assert_called_once_with(
        logGroupName="/aws/bedrock-agentcore/runtimes/runtime-existing-DEFAULT",
        retentionInDays=30,
    )


@pytest.mark.parametrize(
    "runtime_id",
    [
        "",
        "../other",
        "runtime/other",
        "runtime\nother",
        "runtime other",
    ],
)
def test_invalid_runtime_id_is_rejected_before_any_aws_call(runtime_id):
    logs = MagicMock()

    with pytest.raises(ValueError, match="invalid runtime id"):
        govern_default_runtime_log_group(logs, runtime_id)

    assert logs.mock_calls == []


def test_an_unexpected_create_error_fails_closed_without_masking_it():
    logs = MagicMock()
    denied = _client_error("AccessDeniedException", "CreateLogGroup")
    logs.create_log_group.side_effect = denied

    with pytest.raises(ClientError) as raised:
        govern_default_runtime_log_group(logs, "runtime-denied")

    assert raised.value is denied
    logs.put_retention_policy.assert_not_called()


def test_a_retention_failure_is_not_downgraded_to_a_success():
    logs = MagicMock()
    denied = _client_error("AccessDeniedException", "PutRetentionPolicy")
    logs.put_retention_policy.side_effect = denied

    with pytest.raises(ClientError) as raised:
        govern_default_runtime_log_group(logs, "runtime-retention-denied")

    assert raised.value is denied


def test_cloudwatch_name_limit_is_enforced_locally():
    with pytest.raises(ValueError, match="exceeds"):
        default_runtime_log_group_name("r" * 512)
