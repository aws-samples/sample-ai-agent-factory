"""Memory deployment is journaled once and readiness is orchestrated outside Lambda."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from app.services.resource_ownership import (
    DEPLOYMENT_ID_TAG_KEY,
    OWNER_SUB_HASH_TAG_KEY,
    ForeignResourceError,
    owner_sub_hash,
    owner_tag_list,
    owner_tags,
)
from app.step_handlers import memory_step

REGION = "us-east-1"
ACCOUNT = "123456789012"
DEPLOYMENT_ID = "dep-memory-lifecycle"
OWNER_SUB = "owner-memory-lifecycle"
MEMORY_NAME = "orders"
MEMORY_ID = "orders-AbCdEf1234"
MEMORY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/{MEMORY_ID}"
ROLE_NAME = "AgentCoreMemory-orders"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"


class _AlreadyExists(Exception):
    pass


class _IamExceptions:
    EntityAlreadyExistsException = _AlreadyExists


@pytest.fixture(autouse=True)
def _stack_identity(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "memory-lifecycle-tests")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setattr(memory_step.time, "sleep", lambda _seconds: None)


def _binding(deployment_id: str = DEPLOYMENT_ID, owner_sub: str = OWNER_SUB):
    return {
        OWNER_SUB_HASH_TAG_KEY: owner_sub_hash(owner_sub),
        DEPLOYMENT_ID_TAG_KEY: deployment_id,
    }


def _detail(status: str, *, role_arn: str = ROLE_ARN) -> dict:
    return {
        "memory": {
            "id": MEMORY_ID,
            "arn": MEMORY_ARN,
            "name": MEMORY_NAME,
            "status": status,
            "memoryExecutionRoleArn": role_arn,
        }
    }


def _event(**overrides) -> dict:
    event = {
        "deployment_id": DEPLOYMENT_ID,
        "owner_sub": OWNER_SUB,
        "target_region": REGION,
        "memory_config": {"name": MEMORY_NAME},
    }
    event.update(overrides)
    return event


def _install_clients(monkeypatch, control, iam):
    iam.exceptions = _IamExceptions()

    def _client(_event, service, **_kwargs):
        return {
            "bedrock-agentcore-control": control,
            "iam": iam,
        }[service]

    monkeypatch.setattr(memory_step.step_clients, "client", _client)


def _install_store(monkeypatch) -> MagicMock:
    store = MagicMock()
    monkeypatch.setattr(memory_step, "_get_deployment_store", lambda: store)
    return store


def _manifest_rows(store: MagicMock) -> list[dict]:
    return [call.args[1] for call in store.record_resource.call_args_list]


def test_create_journals_before_readiness_and_uses_stable_tenant_bound_identity(
    monkeypatch,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.return_value = {"memories": []}
    control.create_memory.return_value = _detail("CREATING")
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": ROLE_ARN}}
    _install_clients(monkeypatch, control, iam)

    result = memory_step.handler(_event(), None)

    expected_token = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agentcore-memory:{REGION}:{DEPLOYMENT_ID}:{MEMORY_NAME}",
        )
    )
    create_call = control.create_memory.call_args.kwargs
    assert create_call["clientToken"] == expected_token
    assert create_call["tags"] == owner_tags(REGION, _binding())
    assert create_call["memoryExecutionRoleArn"] == ROLE_ARN
    assert iam.create_role.call_args.kwargs["Tags"] == owner_tag_list(
        REGION,
        _binding(),
    )
    assert control.get_memory.call_count == 0
    assert _manifest_rows(store) == [
        {
            "type": "iam_role",
            "name": ROLE_NAME,
            "region": REGION,
            "created_by_deployment": True,
        },
        {
            "type": "memory",
            "id": MEMORY_ID,
            "region": REGION,
            "created_by_deployment": True,
        },
    ]
    assert result["memory_result"] == {
        "success": True,
        "memory_id": MEMORY_ID,
        "memory_name": MEMORY_NAME,
        "status": "CREATING",
        "ready": False,
        "readiness_checks": 0,
        "active_observations": 0,
        "memory_role_name": ROLE_NAME,
        "memory_role_created_by_deployment": True,
    }
    store.update_status.assert_called_once()


@pytest.mark.parametrize(
    ("resource_deployment", "expected_created"),
    [
        (DEPLOYMENT_ID, True),
        ("dep-older-memory", False),
    ],
)
def test_adoption_preserves_tagged_provenance_and_never_mutates(
    monkeypatch,
    resource_deployment,
    expected_created,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.return_value = {"memories": [{"id": MEMORY_ID, "status": "ACTIVE"}]}
    control.get_memory.return_value = _detail("ACTIVE")
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding(resource_deployment))}
    iam = MagicMock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": ROLE_ARN,
            "Tags": owner_tag_list(REGION, _binding(resource_deployment)),
        }
    }
    _install_clients(monkeypatch, control, iam)

    result = memory_step.handler(_event(), None)

    assert _manifest_rows(store) == [
        {
            "type": "iam_role",
            "name": ROLE_NAME,
            "region": REGION,
            "created_by_deployment": expected_created,
        },
        {
            "type": "memory",
            "id": MEMORY_ID,
            "region": REGION,
            "created_by_deployment": expected_created,
        },
    ]
    assert result["memory_result"]["status"] == "ACTIVE"
    assert result["memory_result"]["active_observations"] == 1
    assert result["memory_result"]["ready"] is False
    iam.create_role.assert_not_called()
    iam.put_role_policy.assert_not_called()
    control.create_memory.assert_not_called()


def test_active_adoption_requires_one_separate_settle_check(monkeypatch):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.return_value = {"memories": [{"id": MEMORY_ID, "status": "ACTIVE"}]}
    control.get_memory.return_value = _detail("ACTIVE")
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding())}
    iam = MagicMock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": ROLE_ARN,
            "Tags": owner_tag_list(REGION, _binding()),
        }
    }
    _install_clients(monkeypatch, control, iam)

    created = memory_step.handler(_event(), None)
    store.record_resource.reset_mock()
    checked = memory_step.handler(created, None)

    assert checked["memory_result"]["status"] == "ACTIVE"
    assert checked["memory_result"]["active_observations"] == 2
    assert checked["memory_result"]["readiness_checks"] == 1
    assert checked["memory_result"]["ready"] is True
    store.record_resource.assert_not_called()
    control.create_memory.assert_not_called()


def test_transition_to_active_gets_a_full_settle_interval(monkeypatch):
    _install_store(monkeypatch)
    control = MagicMock()
    control.get_memory.side_effect = [_detail("ACTIVE"), _detail("ACTIVE")]
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding())}
    iam = MagicMock()
    _install_clients(monkeypatch, control, iam)
    event = _event(
        memory_result={
            "success": True,
            "memory_id": MEMORY_ID,
            "memory_name": MEMORY_NAME,
            "status": "CREATING",
            "ready": False,
            "readiness_checks": 0,
            "active_observations": 0,
        }
    )

    first_active = memory_step.handler(event, None)
    second_active = memory_step.handler(first_active, None)

    assert first_active["memory_result"]["active_observations"] == 1
    assert first_active["memory_result"]["ready"] is False
    assert second_active["memory_result"]["active_observations"] == 2
    assert second_active["memory_result"]["ready"] is True


@pytest.mark.parametrize("status", ["DELETING", "FAILED", "UNKNOWN", ""])
def test_adoption_refuses_every_non_transitional_non_ready_status(
    monkeypatch,
    status,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.return_value = {"memories": [{"id": MEMORY_ID, "status": status}]}
    control.get_memory.return_value = _detail(status)
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding())}
    iam = MagicMock()
    _install_clients(monkeypatch, control, iam)

    with pytest.raises(RuntimeError):
        memory_step.handler(_event(), None)

    store.record_resource.assert_not_called()
    control.create_memory.assert_not_called()


@pytest.mark.parametrize(
    "binding",
    [
        _binding(owner_sub="somebody-else"),
        {},
    ],
    ids=["different-caller", "legacy-unbound"],
)
def test_adoption_refuses_a_different_or_unbound_caller_inside_the_same_stack(
    monkeypatch,
    binding,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.return_value = {"memories": [{"id": MEMORY_ID, "status": "ACTIVE"}]}
    control.get_memory.return_value = _detail("ACTIVE")
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, binding)}
    iam = MagicMock()
    _install_clients(monkeypatch, control, iam)

    with pytest.raises(ForeignResourceError, match="authenticated caller"):
        memory_step.handler(_event(), None)

    store.record_resource.assert_not_called()
    iam.get_role.assert_not_called()


def test_create_conflict_does_not_downgrade_same_deployment_provenance(
    monkeypatch,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.list_memories.side_effect = [
        {"memories": []},
        {"memories": []},
        {"memories": []},
        {"memories": [{"id": MEMORY_ID, "status": "CREATING"}]},
    ]
    control.create_memory.side_effect = RuntimeError("Conflict: already exists")
    control.get_memory.return_value = _detail("CREATING")
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding())}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": ROLE_ARN}}
    iam.get_role.return_value = {
        "Role": {
            "Arn": ROLE_ARN,
            "Tags": owner_tag_list(REGION, _binding()),
        }
    }
    _install_clients(monkeypatch, control, iam)

    result = memory_step.handler(_event(), None)

    memory_rows = [row for row in _manifest_rows(store) if row["type"] == "memory"]
    assert memory_rows == [
        {
            "type": "memory",
            "id": MEMORY_ID,
            "region": REGION,
            "created_by_deployment": True,
        }
    ]
    assert result["memory_result"]["status"] == "CREATING"
    assert result["memory_result"]["ready"] is False


def test_readiness_budget_exhaustion_is_a_failure_not_a_false_success(
    monkeypatch,
):
    store = _install_store(monkeypatch)
    control = MagicMock()
    control.get_memory.return_value = _detail("CREATING")
    control.list_tags_for_resource.return_value = {"tags": owner_tags(REGION, _binding())}
    iam = MagicMock()
    _install_clients(monkeypatch, control, iam)

    with pytest.raises(RuntimeError, match="120 readiness checks"):
        memory_step.handler(
            _event(
                memory_result={
                    "success": True,
                    "memory_id": MEMORY_ID,
                    "memory_name": MEMORY_NAME,
                    "status": "CREATING",
                    "ready": False,
                    "readiness_checks": 119,
                    "active_observations": 0,
                }
            ),
            None,
        )

    store.update_status.assert_not_called()


def test_enabled_memory_fails_closed_without_an_authenticated_owner(monkeypatch):
    store = _install_store(monkeypatch)

    def _no_client(*_args, **_kwargs):
        raise AssertionError("no AWS client may be created before owner validation")

    monkeypatch.setattr(memory_step.step_clients, "client", _no_client)

    with pytest.raises(RuntimeError, match="authenticated owner"):
        memory_step.handler(_event(owner_sub=""), None)

    store.record_resource.assert_not_called()
