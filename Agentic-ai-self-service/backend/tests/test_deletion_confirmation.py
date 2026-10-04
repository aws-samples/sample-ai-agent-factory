"""A delete request is not success until a live read proves absence."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services.deletion_confirmation import (
    ConfirmationBudgetExhausted,
    DeletionFailedAfterAccept,
    delete_memory_confirmed,
    delete_policy_engine_confirmed,
    delete_vector_bucket_confirmed,
    wait_until_absent,
)
from app.services.resource_ownership import (
    OWNER_SUB_HASH_TAG_KEY,
    ResourceDeletionRefused,
    owner_sub_hash,
    owner_tags,
)

MEMORY_OWNER = "memory-delete-owner"


def _not_found(*_args, **_kwargs):
    raise RuntimeError("ResourceNotFoundException: resource does not exist")


def test_wait_until_absent_requires_a_not_found_or_empty_response(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    read = MagicMock(
        side_effect=[
            {"status": "DELETING"},
            RuntimeError("ResourceNotFoundException: resource does not exist"),
        ]
    )

    wait_until_absent(
        resource_label="test resource r-1",
        read=read,
        max_attempts=3,
        delay_seconds=1,
    )

    assert read.call_count == 2


def test_wait_until_absent_retains_a_resource_still_present(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)

    with pytest.raises(ResourceDeletionRefused, match="not confirmed"):
        wait_until_absent(
            resource_label="test resource r-2",
            read=lambda: {"status": "DELETING"},
            max_attempts=3,
            delay_seconds=1,
        )


def test_wait_until_absent_surfaces_terminal_failure_without_losing_reason():
    with pytest.raises(
        DeletionFailedAfterAccept,
        match="DeleteWorkloadIdentity denied",
    ):
        wait_until_absent(
            resource_label="harness h-1",
            read=lambda: {
                "harness": {
                    "status": "DELETE_FAILED",
                    "failureReason": "DeleteWorkloadIdentity denied",
                }
            },
            max_attempts=1,
            delay_seconds=0,
        )


def _memory_detail(status: str, *, failure_reason: str | None = None) -> dict:
    memory = {
        "id": "memory-AbCdEf1234",
        "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/memory-AbCdEf1234"),
        "status": status,
    }
    if failure_reason is not None:
        memory["failureReason"] = failure_reason
    return {"memory": memory}


def _owned_memory_client() -> MagicMock:
    client = MagicMock()
    client.list_tags_for_resource.return_value = {
        "tags": owner_tags(
            "us-east-1",
            {OWNER_SUB_HASH_TAG_KEY: owner_sub_hash(MEMORY_OWNER)},
        )
    }
    return client


def test_memory_delete_waits_through_deleting_until_not_found(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    client = _owned_memory_client()
    client.get_memory.side_effect = [
        _memory_detail("ACTIVE"),
        _memory_detail("DELETING"),
        RuntimeError("ResourceNotFoundException: memory does not exist"),
    ]

    delete_memory_confirmed(
        client,
        "memory-AbCdEf1234",
        region="us-east-1",
        owner_sub=MEMORY_OWNER,
        confirmation_attempts=3,
        delay_seconds=0,
    )

    expected_token = str(
        deletion_confirmation.uuid.uuid5(
            deletion_confirmation.uuid.NAMESPACE_URL,
            "agentcore-memory-delete:memory-AbCdEf1234",
        )
    )
    client.delete_memory.assert_called_once_with(
        memoryId="memory-AbCdEf1234",
        clientToken=expected_token,
    )
    assert client.get_memory.call_count == 3


def test_memory_delete_still_deleting_is_retained_not_reported_deleted(
    monkeypatch,
):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    client = _owned_memory_client()
    client.get_memory.side_effect = [
        _memory_detail("ACTIVE"),
        _memory_detail("DELETING"),
        _memory_detail("DELETING"),
    ]

    with pytest.raises(ResourceDeletionRefused, match="still DELETING"):
        delete_memory_confirmed(
            client,
            "memory-AbCdEf1234",
            region="us-east-1",
            owner_sub=MEMORY_OWNER,
            confirmation_attempts=2,
            delay_seconds=0,
        )


def test_memory_delete_surfaces_terminal_failed_reason(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    client = _owned_memory_client()
    client.get_memory.side_effect = [
        _memory_detail("ACTIVE"),
        _memory_detail(
            "FAILED",
            failure_reason="DeleteMemory dependency refused the request",
        ),
    ]

    with pytest.raises(
        DeletionFailedAfterAccept,
        match="DeleteMemory dependency refused",
    ):
        delete_memory_confirmed(
            client,
            "memory-AbCdEf1234",
            region="us-east-1",
            owner_sub=MEMORY_OWNER,
            confirmation_attempts=2,
            delay_seconds=0,
        )


def test_memory_already_absent_never_issues_a_delete():
    client = _owned_memory_client()
    client.get_memory.side_effect = RuntimeError("ResourceNotFoundException: memory does not exist")

    delete_memory_confirmed(
        client,
        "memory-AbCdEf1234",
        region="us-east-1",
        owner_sub=MEMORY_OWNER,
    )

    client.delete_memory.assert_not_called()


def test_memory_delete_refuses_unowned_resource_before_mutation():
    client = MagicMock()
    client.get_memory.return_value = _memory_detail("ACTIVE")
    client.list_tags_for_resource.return_value = {"tags": {}}

    with pytest.raises(ResourceDeletionRefused, match="ownership"):
        delete_memory_confirmed(
            client,
            "memory-AbCdEf1234",
            region="us-east-1",
            owner_sub=MEMORY_OWNER,
        )

    client.delete_memory.assert_not_called()


def test_memory_delete_refuses_a_different_caller_inside_the_same_stack():
    client = _owned_memory_client()
    client.get_memory.return_value = _memory_detail("ACTIVE")

    with pytest.raises(ResourceDeletionRefused, match="caller binding"):
        delete_memory_confirmed(
            client,
            "memory-AbCdEf1234",
            region="us-east-1",
            owner_sub="different-memory-owner",
        )

    client.delete_memory.assert_not_called()


def test_policy_engine_children_and_engine_are_both_confirmed_gone(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    ctrl = MagicMock()
    ctrl.list_policies.side_effect = [
        {"policies": [{"policyId": "p-1"}]},
        {"policies": []},
        {"policies": []},
    ]
    ctrl.get_policy_engine.side_effect = _not_found

    delete_policy_engine_confirmed(
        ctrl,
        "engine-1",
        child_rounds=3,
        confirmation_attempts=2,
        delay_seconds=0,
    )

    ctrl.delete_policy.assert_called_once_with(
        policyEngineId="engine-1",
        policyId="p-1",
    )
    ctrl.delete_policy_engine.assert_called_once_with(policyEngineId="engine-1")


def test_policy_engine_is_not_deleted_while_a_child_remains(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    ctrl = MagicMock()
    ctrl.list_policies.return_value = {"policies": [{"policyId": "p-stuck"}]}

    with pytest.raises(ResourceDeletionRefused, match="still contains"):
        delete_policy_engine_confirmed(
            ctrl,
            "engine-stuck",
            child_rounds=2,
            confirmation_attempts=1,
            delay_seconds=0,
        )

    ctrl.delete_policy_engine.assert_not_called()


def test_vector_bucket_is_not_deleted_while_an_index_remains(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    s3v = MagicMock()
    s3v.list_indexes.return_value = {"indexes": [{"indexName": "index-stuck"}]}

    with pytest.raises(ResourceDeletionRefused, match="still contains"):
        delete_vector_bucket_confirmed(
            s3v,
            "bucket-stuck",
            child_rounds=2,
            confirmation_attempts=1,
            delay_seconds=0,
        )

    s3v.delete_vector_bucket.assert_not_called()


def test_vector_bucket_and_indexes_are_both_confirmed_gone(monkeypatch):
    from app.services import deletion_confirmation

    monkeypatch.setattr(deletion_confirmation.time, "sleep", lambda _seconds: None)
    s3v = MagicMock()
    s3v.list_indexes.side_effect = [
        {"indexes": [{"indexName": "index-1"}]},
        {"indexes": []},
        {"indexes": []},
    ]
    s3v.get_vector_bucket.side_effect = _not_found

    delete_vector_bucket_confirmed(
        s3v,
        "bucket-1",
        child_rounds=3,
        confirmation_attempts=2,
        delay_seconds=0,
    )

    s3v.delete_index.assert_called_once_with(
        vectorBucketName="bucket-1",
        indexName="index-1",
    )
    s3v.delete_vector_bucket.assert_called_once_with(vectorBucketName="bucket-1")


def test_an_exhausted_budget_is_a_distinct_pending_refusal():
    """The service accepted the delete and had not finished: a ResourceDeletionRefused (every
    caller keeps retaining on it) of its own class, so the async teardown can confirm again later."""
    read = MagicMock(return_value={"memory": {"status": "DELETING"}})

    with pytest.raises(ConfirmationBudgetExhausted) as raised:
        wait_until_absent(resource_label="memory mem-1", read=read, max_attempts=2, delay_seconds=0)

    assert isinstance(raised.value, ResourceDeletionRefused)
    assert "still DELETING at the end of the confirmation budget" in str(raised.value)


def test_an_unreadable_final_state_is_a_plain_refusal_not_a_pending_one():
    """Uncertainty about the final state is not "the service is still working": the teardown must not
    keep re-running on it."""

    def _denied():
        raise RuntimeError("AccessDeniedException: not authorized to GetMemory")

    with pytest.raises(ResourceDeletionRefused) as raised:
        wait_until_absent(resource_label="memory mem-1", read=_denied, max_attempts=2, delay_seconds=0)

    assert not isinstance(raised.value, ConfirmationBudgetExhausted)
    assert "could not be read" in str(raised.value)
