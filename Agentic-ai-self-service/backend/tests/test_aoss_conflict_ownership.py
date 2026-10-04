"""OpenSearch Serverless retries must never adopt a same-named foreign graph."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services.resource_ownership import (
    ResourceDeletionRefused,
    aoss_policy_owner_description,
    owner_tags,
)
from app.step_handlers import knowledge_base_step
from botocore.exceptions import ClientError

REGION = "us-east-1"
DEPLOYMENT_ID = "dep-aoss-12345678"
COLLECTION_NAME = "kbdepaoss12345678"
COLLECTION_ARN = "arn:aws:aoss:us-east-1:123456789012:collection/collection-123"


def _error(code: str, message: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


def _clients(monkeypatch, aoss: MagicMock) -> None:
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Arn": "arn:aws:iam::123456789012:role/KnowledgeBaseStepRole"}

    def _client(_event, service, **_kwargs):
        if service == "opensearchserverless":
            return aoss
        if service == "sts":
            return sts
        raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(knowledge_base_step.step_clients, "client", _client)
    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda _seconds: None)


def _active_collection(aoss: MagicMock, *, tags: dict[str, str] | None = None) -> None:
    aoss.batch_get_collection.return_value = {
        "collectionDetails": [
            {
                "id": "collection-123",
                "arn": COLLECTION_ARN,
                "name": COLLECTION_NAME,
                "type": "VECTORSEARCH",
                "status": "ACTIVE",
            }
        ]
    }
    aoss.list_tags_for_resource.return_value = {
        "tags": tags
        if tags is not None
        else {
            **owner_tags(REGION),
            "DeploymentId": DEPLOYMENT_ID,
        }
    }


def _ensure(monkeypatch, aoss: MagicMock, store: MagicMock | None = None) -> str:
    _clients(monkeypatch, aoss)
    _active_collection(aoss)
    return knowledge_base_step._ensure_oss_collection(
        REGION,
        DEPLOYMENT_ID,
        "arn:aws:iam::123456789012:role/AgentCoreKBRole",
        {"vectorStoreType": "opensearch_serverless"},
        store,
        DEPLOYMENT_ID,
        {"deployment_id": DEPLOYMENT_ID},
    )


def test_new_collection_is_bound_to_the_exact_deployment_and_journaled_early(
    monkeypatch,
):
    aoss = MagicMock()
    store = MagicMock()

    arn = _ensure(monkeypatch, aoss, store)

    assert arn == COLLECTION_ARN
    tags = {tag["key"]: tag["value"] for tag in aoss.create_collection.call_args.kwargs["tags"]}
    assert tags["DeploymentId"] == DEPLOYMENT_ID
    row = store.record_resource.call_args.args[1]
    assert row == {
        "type": "oss_collection",
        "name": COLLECTION_NAME,
        "region": REGION,
        "created_by_deployment": True,
    }


def test_owned_policy_conflict_updates_the_requested_document(monkeypatch):
    aoss = MagicMock()
    aoss.create_security_policy.side_effect = [
        _error(
            "ConflictException",
            "security policy already exists",
            "CreateSecurityPolicy",
        ),
        {},
    ]
    aoss.get_security_policy.return_value = {
        "securityPolicyDetail": {
            "description": aoss_policy_owner_description(
                REGION,
                f"Encryption policy for AgentCore KB {DEPLOYMENT_ID[:12]}",
                DEPLOYMENT_ID,
            ),
            "policyVersion": "v17",
        }
    }

    _ensure(monkeypatch, aoss)

    update = aoss.update_security_policy.call_args.kwargs
    assert update["name"] == f"{COLLECTION_NAME}-enc"
    assert update["type"] == "encryption"
    assert update["policyVersion"] == "v17"
    assert f"collection/{COLLECTION_NAME}" in update["policy"]


def test_foreign_policy_conflict_fails_before_any_update(monkeypatch):
    aoss = MagicMock()
    aoss.create_security_policy.side_effect = _error(
        "ConflictException",
        "security policy already exists",
        "CreateSecurityPolicy",
    )
    aoss.get_security_policy.return_value = {
        "securityPolicyDetail": {
            "description": (
                f"foreign policy; AgentCoreStack=another-stack-prod-us-east-1; DeploymentId={DEPLOYMENT_ID}"
            ),
            "policyVersion": "v1",
        }
    }
    _clients(monkeypatch, aoss)

    with pytest.raises(ResourceDeletionRefused, match="ownership marker"):
        knowledge_base_step._ensure_oss_collection(
            REGION,
            DEPLOYMENT_ID,
            "arn:aws:iam::123456789012:role/AgentCoreKBRole",
            {},
            MagicMock(),
            DEPLOYMENT_ID,
            {"deployment_id": DEPLOYMENT_ID},
        )

    aoss.update_security_policy.assert_not_called()
    aoss.create_collection.assert_not_called()


def test_foreign_collection_conflict_is_not_adopted(monkeypatch):
    aoss = MagicMock()
    aoss.create_collection.side_effect = _error(
        "ConflictException",
        "collection already exists",
        "CreateCollection",
    )
    _clients(monkeypatch, aoss)
    _active_collection(
        aoss,
        tags={
            **owner_tags(REGION),
            "DeploymentId": "different-deployment",
        },
    )

    with pytest.raises(ResourceDeletionRefused, match="DeploymentId"):
        knowledge_base_step._ensure_oss_collection(
            REGION,
            DEPLOYMENT_ID,
            "arn:aws:iam::123456789012:role/AgentCoreKBRole",
            {},
            MagicMock(),
            DEPLOYMENT_ID,
            {"deployment_id": DEPLOYMENT_ID},
        )

    aoss.create_index.assert_not_called()


def test_conflicting_index_schema_is_not_silently_reused(monkeypatch):
    aoss = MagicMock()
    aoss.create_index.side_effect = _error(
        "ConflictException",
        "index already exists",
        "CreateIndex",
    )
    aoss.get_index.return_value = {
        "indexSchema": {
            "settings": {"index": {"knn": False}},
            "mappings": {"properties": {}},
        }
    }

    with pytest.raises(RuntimeError, match="incompatible schema"):
        _ensure(monkeypatch, aoss)


def test_graph_is_journaled_before_a_later_policy_failure(monkeypatch):
    aoss = MagicMock()
    aoss.create_security_policy.side_effect = [
        {},
        _error(
            "AccessDeniedException",
            "not authorized",
            "CreateSecurityPolicy",
        ),
    ]
    store = MagicMock()
    _clients(monkeypatch, aoss)

    with pytest.raises(ClientError):
        knowledge_base_step._ensure_oss_collection(
            REGION,
            DEPLOYMENT_ID,
            "arn:aws:iam::123456789012:role/AgentCoreKBRole",
            {},
            store,
            DEPLOYMENT_ID,
            {"deployment_id": DEPLOYMENT_ID},
        )

    store.record_resource.assert_called_once()
    aoss.create_collection.assert_not_called()
