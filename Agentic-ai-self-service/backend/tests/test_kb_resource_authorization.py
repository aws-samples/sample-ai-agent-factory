"""Knowledge Base customer-resource references are inventory, not authority."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.services.resource_ownership import (
    ACCESS_TAG_KEY,
    ACCESS_TAG_VALUE,
    DEPLOYMENT_ID_TAG_KEY,
    OWNER_SUB_HASH_TAG_KEY,
    ResourceAccessRefused,
    assert_resource_access_allowed,
    owner_sub_hash,
    owner_tags,
)
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = "111122223333"
OWNER = "54381418-7021-708e-4f3b-30505a2b82ec"
OTHER_OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
DEPLOYMENT = "dep-kb-auth-123"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "kb-auth-tests")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", REGION)


def _allow_tags(*, owner: str | None = None) -> dict[str, str]:
    tags = {ACCESS_TAG_KEY: ACCESS_TAG_VALUE}
    if owner:
        tags[OWNER_SUB_HASH_TAG_KEY] = owner_sub_hash(owner)
    return tags


def _bound_tags() -> dict[str, str]:
    return owner_tags(
        REGION,
        extra={
            DEPLOYMENT_ID_TAG_KEY: DEPLOYMENT,
            OWNER_SUB_HASH_TAG_KEY: owner_sub_hash(OWNER),
        },
    )


def _index_detail(bucket_arn: str, index_name: str) -> dict:
    return {
        "indexName": index_name,
        "indexArn": f"{bucket_arn}/index/{index_name}",
        "dataType": "float32",
        "dimension": 1024,
        "distanceMetric": "cosine",
    }


def _oss_index_schema(
    *,
    vector_field: str = "bedrock-knowledge-base-default-vector",
    text_field: str = "AMAZON_BEDROCK_TEXT_CHUNK",
    metadata_field: str = "AMAZON_BEDROCK_METADATA",
) -> dict:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                vector_field: {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                    },
                },
                text_field: {"type": "text"},
                metadata_field: {"type": "text"},
            }
        },
    }


def _client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


def _install_create_new_harness(
    monkeypatch,
    *,
    kb_config: dict | None = None,
) -> SimpleNamespace:
    """Install a no-network handler harness with realistic AWS response shapes."""
    from app.step_handlers import knowledge_base_step

    bedrock = MagicMock()
    bedrock.list_knowledge_bases.return_value = {"knowledgeBaseSummaries": []}
    bedrock.create_knowledge_base.return_value = {"knowledgeBase": {"knowledgeBaseId": "KBNEW12345"}}
    bedrock.create_data_source.return_value = {"dataSource": {"dataSourceId": "DSNEW12345"}}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreKBRole-test"}}
    s3v = MagicMock()
    aoss = MagicMock()
    store = MagicMock()
    requested_services: list[str] = []

    def _client(_event, service, **_kwargs):
        requested_services.append(service)
        clients = {
            "bedrock-agent": bedrock,
            "iam": iam,
            "s3vectors": s3v,
            "opensearchserverless": aoss,
        }
        if service not in clients:
            pytest.fail(f"unexpected client {service}")
        return clients[service]

    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        knowledge_base_step,
        "_get_deployment_store",
        lambda: store,
    )
    monkeypatch.setattr(
        knowledge_base_step,
        "_wait_for_kb_active",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        knowledge_base_step,
        "_build_data_source_config",
        lambda _config: (
            {
                "type": "S3",
                "s3Configuration": {"bucketArn": "arn:aws:s3:::platform-artifacts"},
            },
            None,
        ),
    )
    monkeypatch.setattr(
        knowledge_base_step,
        "_start_and_wait_ingestion",
        lambda *_a, **_kw: ("JOB12345", "COMPLETE"),
    )
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "account_id_for_event",
        lambda _event: ACCOUNT,
    )
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "artifacts_bucket_for_event",
        lambda *_a, **_kw: "platform-artifacts",
    )
    monkeypatch.setattr(knowledge_base_step.step_clients, "client", _client)

    config = {
        "kbMode": "create_new",
        "kbName": "authorization-test-kb",
        "dataSourceType": "s3",
        "s3BucketUri": "s3://platform-artifacts/input/",
        "vectorStoreType": "s3_vectors",
        "embeddingModelId": "amazon.titan-embed-text-v2:0",
    }
    config.update(kb_config or {})
    event = {
        "deployment_id": DEPLOYMENT,
        "owner_sub": OWNER,
        "target_account_id": ACCOUNT,
        "target_region": REGION,
        "target_artifact_bucket": "platform-artifacts",
        "knowledge_base_config": config,
    }
    return SimpleNamespace(
        module=knowledge_base_step,
        event=event,
        bedrock=bedrock,
        iam=iam,
        s3v=s3v,
        aoss=aoss,
        store=store,
        requested_services=requested_services,
    )


def test_explicit_opt_in_can_be_shared_or_scoped_to_the_authenticated_caller():
    assert_resource_access_allowed(
        "shared bucket",
        _allow_tags(),
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        region=REGION,
    )
    assert_resource_access_allowed(
        "private bucket",
        _allow_tags(owner=OWNER),
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        region=REGION,
    )

    with pytest.raises(ResourceAccessRefused, match="caller binding"):
        assert_resource_access_allowed(
            "private bucket",
            _allow_tags(owner=OTHER_OWNER),
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            region=REGION,
        )


def test_platform_owned_resource_needs_exact_caller_or_deployment_binding():
    with pytest.raises(ResourceAccessRefused, match=ACCESS_TAG_KEY):
        assert_resource_access_allowed(
            "legacy stack-only resource",
            owner_tags(REGION),
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            region=REGION,
        )

    assert_resource_access_allowed(
        "deployment resource",
        _bound_tags(),
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        region=REGION,
    )


def test_customer_secret_requires_opt_in_and_is_copied_without_reformatting():
    from app.services.gateway_deployer import (
        ConnectorSecretBindingError,
        stage_customer_secret_for_deployment,
    )

    source_arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:customer/kb/rds-AbCdEf"
    target_arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/copy-AbCdEf"
    raw = '{"username":"db_user","password":"do-not-reformat"}'
    source = MagicMock()
    source.describe_secret.return_value = {"Tags": _allow_tags(owner=OWNER)}
    source.get_secret_value.return_value = {"SecretString": raw}
    target = MagicMock()
    target.create_secret.return_value = {"ARN": target_arn}

    copied = stage_customer_secret_for_deployment(
        source_secret_ref=source_arn,
        purpose="knowledge-base-rds",
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        target_region=REGION,
        source_secrets_client=source,
        target_secrets_client=target,
    )

    assert copied == target_arn
    create = target.create_secret.call_args.kwargs
    assert create["SecretString"] == raw
    tags = {item["Key"]: item["Value"] for item in create["Tags"]}
    assert tags[DEPLOYMENT_ID_TAG_KEY] == DEPLOYMENT
    assert tags[OWNER_SUB_HASH_TAG_KEY] == owner_sub_hash(OWNER)

    source.reset_mock()
    source.describe_secret.return_value = {"Tags": {}}
    with pytest.raises(ConnectorSecretBindingError, match=ACCESS_TAG_KEY):
        stage_customer_secret_for_deployment(
            source_secret_ref=source_arn,
            purpose="knowledge-base-rds",
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            target_region=REGION,
            source_secrets_client=source,
            target_secrets_client=target,
        )
    source.get_secret_value.assert_not_called()


def test_prepare_kb_credentials_stages_only_active_fields(monkeypatch):
    from app import deployment_handler
    from app.services import step_clients

    source_refs = {
        "sharePointCredentialsSecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:customer/kb/sharepoint-AbCdEf"
        ),
        "rdsCredentialsSecretArn": (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:customer/kb/rds-AbCdEf"),
        "confluenceCredentialsSecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:customer/kb/confluence-AbCdEf"
        ),
        "salesforceCredentialsSecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:customer/kb/salesforce-AbCdEf"
        ),
    }
    calls: list[tuple[str, str]] = []

    def _stage(**kwargs):
        calls.append((kwargs["source_secret_ref"], kwargs["purpose"]))
        suffix = kwargs["purpose"].rsplit("-", 1)[-1]
        return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/{suffix}-AbCdEf"

    class _Session:
        def client(self, service, **kwargs):
            assert service == "secretsmanager"
            assert kwargs.get("region_name") == REGION
            return object()

    class _Store:
        def __init__(self):
            self.rows = []

        def record_resource_strict(self, deployment_id, row):
            self.rows.append((deployment_id, row))

    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda _event: _Session(),
    )
    monkeypatch.setattr(
        deployment_handler,
        "stage_customer_secret_for_deployment",
        _stage,
    )
    store = _Store()
    prepared, staged = deployment_handler._prepare_knowledge_base_credentials(
        knowledge_base_config={
            "dataSourceType": "sharepoint",
            "vectorStoreType": "rds",
            **source_refs,
        },
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id=ACCOUNT,
        target_region=REGION,
        target_role_arn=f"arn:aws:iam::{ACCOUNT}:role/Deploy",
        store=store,
    )

    assert prepared is not None
    assert {ref for ref, _purpose in calls} == {
        source_refs["sharePointCredentialsSecretArn"],
        source_refs["rdsCredentialsSecretArn"],
    }
    assert "confluenceCredentialsSecretArn" not in prepared
    assert "salesforceCredentialsSecretArn" not in prepared
    assert all(source not in prepared.values() for source in source_refs.values())
    assert len(staged) == 2
    assert len(store.rows) == 2
    assert all(row["created_by_deployment"] is True for _dep, row in store.rows)


def test_existing_kb_requires_live_opt_in_before_it_is_recorded(monkeypatch):
    from app.step_handlers import knowledge_base_step

    bedrock = MagicMock()
    bedrock.get_knowledge_base.return_value = {
        "knowledgeBase": {
            "knowledgeBaseId": "KB12345678",
            "knowledgeBaseArn": (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/KB12345678"),
            "status": "ACTIVE",
        }
    }
    bedrock.list_tags_for_resource.return_value = {"tags": {}}
    store = MagicMock()

    monkeypatch.setattr(knowledge_base_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "account_id_for_event",
        lambda _event: ACCOUNT,
    )
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: (
            bedrock if service == "bedrock-agent" else pytest.fail(f"unexpected client {service}")
        ),
    )

    with pytest.raises(ResourceAccessRefused, match=ACCESS_TAG_KEY):
        knowledge_base_step.handler(
            {
                "deployment_id": DEPLOYMENT,
                "owner_sub": OWNER,
                "target_region": REGION,
                "knowledge_base_config": {
                    "kbMode": "existing",
                    "knowledgeBaseId": "KB12345678",
                },
            },
            None,
        )

    store.record_resource.assert_not_called()


def test_existing_kb_with_live_opt_in_is_recorded_as_customer_owned(monkeypatch):
    from app.step_handlers import knowledge_base_step

    bedrock = MagicMock()
    bedrock.get_knowledge_base.return_value = {
        "knowledgeBase": {
            "knowledgeBaseId": "KB12345678",
            "knowledgeBaseArn": (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/KB12345678"),
            "status": "ACTIVE",
        }
    }
    bedrock.list_tags_for_resource.return_value = {"tags": _allow_tags(owner=OWNER)}
    store = MagicMock()
    monkeypatch.setattr(knowledge_base_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "account_id_for_event",
        lambda _event: ACCOUNT,
    )
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: (
            bedrock if service == "bedrock-agent" else pytest.fail(f"unexpected client {service}")
        ),
    )

    result = knowledge_base_step.handler(
        {
            "deployment_id": DEPLOYMENT,
            "owner_sub": OWNER,
            "target_region": REGION,
            "knowledge_base_config": {
                "kbMode": "existing",
                "knowledgeBaseId": "KB12345678",
            },
        },
        None,
    )

    assert result["knowledge_base_result"]["kb_id"] == "KB12345678"
    row = store.record_resource.call_args.args[1]
    assert row["created_by_deployment"] is False


def test_untagged_s3_source_is_rejected_before_any_iam_role_mutation(
    monkeypatch,
):
    from app.step_handlers import knowledge_base_step

    clients_requested: list[str] = []
    s3 = MagicMock()
    s3.get_bucket_tagging.return_value = {"TagSet": []}

    def _client(_event, service, **_kwargs):
        clients_requested.append(service)
        if service == "bedrock-agent":
            return MagicMock()
        if service == "s3":
            return s3
        if service == "iam":
            pytest.fail("IAM must not be reached before customer-resource authorization")
        return MagicMock()

    monkeypatch.setattr(knowledge_base_step, "_get_deployment_store", MagicMock)
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "account_id_for_event",
        lambda _event: ACCOUNT,
    )
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "artifacts_bucket_for_event",
        lambda *_args, **_kwargs: "platform-artifacts",
    )
    monkeypatch.setattr(knowledge_base_step.step_clients, "client", _client)

    with pytest.raises(ResourceAccessRefused, match=ACCESS_TAG_KEY):
        knowledge_base_step.handler(
            {
                "deployment_id": DEPLOYMENT,
                "owner_sub": OWNER,
                "target_region": REGION,
                "knowledge_base_config": {
                    "kbMode": "create_new",
                    "kbName": "customer-docs",
                    "dataSourceType": "s3",
                    "s3BucketUri": "s3://customer-docs/input/",
                    "vectorStoreType": "s3_vectors",
                    "embeddingModelId": "amazon.titan-embed-text-v2:0",
                },
            },
            None,
        )

    assert "iam" not in clients_requested


def test_every_supported_customer_reference_is_live_authorized(monkeypatch):
    from app.step_handlers import knowledge_base_step

    s3 = MagicMock()
    s3.get_bucket_tagging.return_value = {
        "TagSet": [
            {"Key": ACCESS_TAG_KEY, "Value": ACCESS_TAG_VALUE},
        ]
    }
    rds = MagicMock()
    rds.list_tags_for_resource.return_value = {"TagList": _allow_tags()}
    lambda_client = MagicMock()
    lambda_client.list_tags.return_value = {"Tags": _allow_tags()}
    kms = MagicMock()
    kms.list_resource_tags.return_value = {
        "Tags": [
            {"TagKey": ACCESS_TAG_KEY, "TagValue": ACCESS_TAG_VALUE},
        ],
        "Truncated": False,
    }
    secrets = MagicMock()
    secrets.describe_secret.return_value = {"Tags": _bound_tags()}
    clients = {
        "s3": s3,
        "rds": rds,
        "lambda": lambda_client,
        "kms": kms,
        "secretsmanager": secrets,
    }
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: clients[service],
    )
    config = {
        "dataSourceType": "s3",
        "s3BucketUri": "s3://customer-docs/input/",
        "vectorStoreType": "rds",
        "rdsResourceArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:cluster:kb",
        "rdsCredentialsSecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/rds-AbCdEf"
        ),
        "transformationLambdaArn": (f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kb-transform"),
        "transformationS3Uri": "s3://kb-transform/intermediate/",
        "parsingStrategy": "bedrock_data_automation",
        "bdaSupplementalS3Uri": "s3://kb-bda-output",
        "kmsKeyArn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/1234abcd",
    }

    knowledge_base_step._authorize_create_new_resources(
        {},
        config,
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        region=REGION,
        account_id=ACCOUNT,
        trusted_s3_buckets=set(),
    )

    assert s3.get_bucket_tagging.call_count == 3
    rds.list_tags_for_resource.assert_called_once()
    lambda_client.list_tags.assert_called_once()
    kms.list_resource_tags.assert_called_once()
    secrets.describe_secret.assert_called_once()


@pytest.mark.parametrize(
    ("vector_config", "service"),
    [
        (
            {
                "vectorStoreType": "s3_vectors",
                "s3VectorsBucketArn": (f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/customer-vectors"),
            },
            "s3vectors",
        ),
        (
            {
                "vectorStoreType": "opensearch_serverless",
                "opensearchCollectionArn": (f"arn:aws:aoss:{REGION}:{ACCOUNT}:collection/abc123"),
            },
            "opensearchserverless",
        ),
    ],
)
def test_vector_store_references_require_live_opt_in(
    monkeypatch,
    vector_config,
    service,
):
    from app.step_handlers import knowledge_base_step

    client = MagicMock()
    client.list_tags_for_resource.return_value = {"tags": _allow_tags()}
    if service == "s3vectors":
        index_name = "bedrock-knowledge-base-default-index"
        index_arn = f"{vector_config['s3VectorsBucketArn']}/index/{index_name}"
        client.list_indexes.return_value = {"indexes": [{"indexName": index_name}]}
        client.get_index.return_value = {
            "index": {
                "indexName": index_name,
                "indexArn": index_arn,
                "dataType": "float32",
                "dimension": 1024,
                "distanceMetric": "cosine",
            }
        }
    elif service == "opensearchserverless":
        client.get_index.return_value = {"indexSchema": _oss_index_schema()}
    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "client",
        lambda _event, requested, **_kwargs: (
            client if requested == service else pytest.fail(f"unexpected client {requested}")
        ),
    )

    knowledge_base_step._authorize_create_new_resources(
        {},
        {"dataSourceType": "web_crawler", **vector_config},
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        region=REGION,
        account_id=ACCOUNT,
        trusted_s3_buckets=set(),
    )
    client.list_tags_for_resource.assert_called_once()

    client.list_tags_for_resource.return_value = {"tags": {}}
    with pytest.raises(ResourceAccessRefused, match=ACCESS_TAG_KEY):
        knowledge_base_step._authorize_create_new_resources(
            {},
            {"dataSourceType": "web_crawler", **vector_config},
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            region=REGION,
            account_id=ACCOUNT,
            trusted_s3_buckets=set(),
        )


def test_transform_bucket_is_not_granted_when_no_transform_is_active():
    from app.step_handlers.knowledge_base_step import _put_kb_role_policy

    iam = MagicMock()
    _put_kb_role_policy(
        iam,
        "AgentCoreKBRole-test",
        {
            "dataSourceType": "web_crawler",
            "vectorStoreType": "s3_vectors",
            "transformationS3Uri": "s3://stale-ui-value/intermediate/",
        },
        region=REGION,
        account_id=ACCOUNT,
    )

    policy = json.loads(iam.put_role_policy.call_args.kwargs["PolicyDocument"])
    assert "stale-ui-value" not in json.dumps(policy)


def test_idempotency_inventory_failure_propagates_instead_of_creating_blindly():
    from app.step_handlers.knowledge_base_step import _find_existing_kb

    bedrock = MagicMock()
    bedrock.list_knowledge_bases.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError, match="denied"):
        _find_existing_kb(bedrock, "customer-kb")


def test_customer_vector_index_is_validated_before_iam_mutation(monkeypatch):
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/customer-vectors"
    harness = _install_create_new_harness(
        monkeypatch,
        kb_config={
            "s3VectorsBucketArn": bucket_arn,
            "s3VectorsIndexName": "customer-index",
        },
    )
    harness.s3v.list_tags_for_resource.return_value = {"tags": _allow_tags(owner=OWNER)}
    harness.s3v.list_indexes.return_value = {"indexes": []}

    with pytest.raises(ValueError, match="does not exist"):
        harness.module.handler(harness.event, None)

    assert "iam" not in harness.requested_services
    harness.iam.create_role.assert_not_called()
    harness.s3v.create_index.assert_not_called()


def test_customer_opensearch_index_is_validated_before_iam_mutation(
    monkeypatch,
):
    collection_arn = f"arn:aws:aoss:{REGION}:{ACCOUNT}:collection/customer123"
    harness = _install_create_new_harness(
        monkeypatch,
        kb_config={
            "vectorStoreType": "opensearch_serverless",
            "opensearchCollectionArn": collection_arn,
            "opensearchVectorIndexName": "customer-index",
        },
    )
    harness.aoss.list_tags_for_resource.return_value = {"tags": _allow_tags(owner=OWNER)}
    harness.aoss.get_index.side_effect = _client_error(
        "ResourceNotFoundException",
        "index not found",
        "GetIndex",
    )

    with pytest.raises(ValueError, match="does not exist"):
        harness.module.handler(harness.event, None)

    assert "iam" not in harness.requested_services
    harness.iam.create_role.assert_not_called()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {
                "vectorStoreType": "s3_vectors",
                "s3VectorsBucketArn": (f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/customer/index/not-a-bucket"),
            },
            "identifies an index",
        ),
        (
            {
                "vectorStoreType": "opensearch_serverless",
                "opensearchCollectionArn": (f"arn:aws:aoss:{REGION}:{ACCOUNT}:access-policy/not-a-collection"),
            },
            "expected resource type",
        ),
        (
            {
                "vectorStoreType": "rds",
                "rdsResourceArn": (f"arn:aws:rds:{REGION}:{ACCOUNT}:db:not-a-cluster"),
                "rdsCredentialsSecretArn": (
                    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/rds-AbCdEf"
                ),
            },
            "expected resource type",
        ),
    ],
)
def test_wrong_resource_family_is_rejected_before_any_live_read(
    monkeypatch,
    config,
    message,
):
    from app.step_handlers import knowledge_base_step

    monkeypatch.setattr(
        knowledge_base_step.step_clients,
        "client",
        lambda *_a, **_kw: pytest.fail("wrong-family ARN must be refused before an AWS client is used"),
    )

    with pytest.raises(ResourceAccessRefused, match=message):
        knowledge_base_step._authorize_create_new_resources(
            {},
            {"dataSourceType": "web_crawler", **config},
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            region=REGION,
            account_id=ACCOUNT,
            trusted_s3_buckets=set(),
        )


def test_retry_reauthorizes_hydrated_storage_before_iam_mutation(monkeypatch):
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/replaced-vectors"
    index_name = "bedrock-knowledge-base-default-index"
    harness = _install_create_new_harness(monkeypatch)
    harness.bedrock.list_knowledge_bases.return_value = {
        "knowledgeBaseSummaries": [
            {
                "name": "authorization-test-kb",
                "knowledgeBaseId": "KBEXIST123",
                "status": "ACTIVE",
            }
        ]
    }
    harness.bedrock.get_knowledge_base.return_value = {
        "knowledgeBase": {
            "knowledgeBaseId": "KBEXIST123",
            "knowledgeBaseArn": (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/KBEXIST123"),
            "status": "ACTIVE",
            "storageConfiguration": {
                "type": "S3_VECTORS",
                "s3VectorsConfiguration": {
                    "vectorBucketArn": bucket_arn,
                    "indexArn": f"{bucket_arn}/index/{index_name}",
                    "indexName": index_name,
                },
            },
        }
    }
    harness.bedrock.list_tags_for_resource.return_value = {"tags": _bound_tags()}
    harness.s3v.list_tags_for_resource.return_value = {"tags": {}}

    with pytest.raises(ResourceAccessRefused, match=ACCESS_TAG_KEY):
        harness.module.handler(harness.event, None)

    assert "iam" not in harness.requested_services
    harness.iam.create_role.assert_not_called()


def test_same_deployment_kb_retry_reuses_only_exact_bound_storage(monkeypatch):
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/owned-vectors"
    index_name = "bedrock-knowledge-base-default-index"
    index_arn = f"{bucket_arn}/index/{index_name}"
    harness = _install_create_new_harness(monkeypatch)
    harness.bedrock.list_knowledge_bases.return_value = {
        "knowledgeBaseSummaries": [
            {
                "name": "authorization-test-kb",
                "knowledgeBaseId": "KBEXIST123",
                "status": "ACTIVE",
            }
        ]
    }
    harness.bedrock.get_knowledge_base.return_value = {
        "knowledgeBase": {
            "knowledgeBaseId": "KBEXIST123",
            "knowledgeBaseArn": (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/KBEXIST123"),
            "status": "ACTIVE",
            "storageConfiguration": {
                "type": "S3_VECTORS",
                "s3VectorsConfiguration": {
                    "vectorBucketArn": bucket_arn,
                    "indexArn": index_arn,
                    "indexName": index_name,
                },
            },
        }
    }
    harness.bedrock.list_tags_for_resource.return_value = {"tags": _bound_tags()}
    harness.s3v.list_tags_for_resource.side_effect = [
        {"tags": _bound_tags()},
        {"tags": _bound_tags()},
    ]
    harness.s3v.list_indexes.return_value = {"indexes": [{"indexName": index_name}]}
    harness.s3v.get_index.return_value = {"index": _index_detail(bucket_arn, index_name)}

    result = harness.module.handler(harness.event, None)

    assert result["knowledge_base_result"]["kb_id"] == "KBEXIST123"
    harness.bedrock.create_knowledge_base.assert_not_called()
    recorded = [call.args[1] for call in harness.store.record_resource.call_args_list]
    kb_rows = [row for row in recorded if row["type"] == "knowledge_base"]
    assert kb_rows == [
        {
            "type": "knowledge_base",
            "id": "KBEXIST123",
            "region": REGION,
            "created_by_deployment": True,
        }
    ]


def test_auto_bucket_name_collision_never_adopts_foreign_resource(
    monkeypatch,
):
    harness = _install_create_new_harness(monkeypatch)
    bucket_name = f"agentcore-kbvec-{DEPLOYMENT[:12]}"
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{bucket_name}"
    harness.s3v.create_vector_bucket.side_effect = _client_error(
        "ConflictException",
        "Vector bucket already exists",
        "CreateVectorBucket",
    )
    harness.s3v.get_vector_bucket.return_value = {"vectorBucket": {"vectorBucketArn": bucket_arn}}
    harness.s3v.list_tags_for_resource.return_value = {"tags": {}}

    with pytest.raises(ResourceAccessRefused, match="exact stack ownership"):
        harness.module.handler(harness.event, None)

    recorded = [call.args[1] for call in harness.store.record_resource.call_args_list]
    assert not any(row["type"] == "s3_vectors_bucket" for row in recorded)
    harness.s3v.create_index.assert_not_called()


def test_auto_bucket_is_journaled_before_missing_arn_failure(monkeypatch):
    harness = _install_create_new_harness(monkeypatch)
    harness.s3v.create_vector_bucket.return_value = {}
    harness.s3v.get_vector_bucket.return_value = {"vectorBucket": {}}

    with pytest.raises(RuntimeError, match="contained no ARN"):
        harness.module.handler(harness.event, None)

    recorded = [call.args[1] for call in harness.store.record_resource.call_args_list]
    assert any(row["type"] == "s3_vectors_bucket" and row["created_by_deployment"] is True for row in recorded)
    harness.s3v.create_index.assert_not_called()


def test_existing_auto_index_requires_exact_deployment_binding(monkeypatch):
    harness = _install_create_new_harness(monkeypatch)
    bucket_name = f"agentcore-kbvec-{DEPLOYMENT[:12]}"
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{bucket_name}"
    index_name = "bedrock-knowledge-base-default-index"
    harness.s3v.create_vector_bucket.side_effect = _client_error(
        "ConflictException",
        "Vector bucket already exists",
        "CreateVectorBucket",
    )
    harness.s3v.get_vector_bucket.return_value = {"vectorBucket": {"vectorBucketArn": bucket_arn}}
    harness.s3v.list_tags_for_resource.side_effect = [
        {"tags": _bound_tags()},
        {"tags": {}},
    ]
    harness.s3v.list_indexes.return_value = {"indexes": [{"indexName": index_name}]}
    harness.s3v.get_index.return_value = {"index": _index_detail(bucket_arn, index_name)}

    with pytest.raises(
        ResourceAccessRefused,
        match="exact stack ownership",
    ):
        harness.module.handler(harness.event, None)

    harness.s3v.create_index.assert_not_called()


def test_role_policy_tightening_failure_is_fatal_before_kb_create(monkeypatch):
    harness = _install_create_new_harness(monkeypatch)
    bucket_name = f"agentcore-kbvec-{DEPLOYMENT[:12]}"
    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{bucket_name}"
    index_name = "bedrock-knowledge-base-default-index"
    harness.s3v.create_vector_bucket.return_value = {}
    harness.s3v.get_vector_bucket.return_value = {"vectorBucket": {"vectorBucketArn": bucket_arn}}
    harness.s3v.list_indexes.side_effect = [
        {"indexes": []},
        {"indexes": [{"indexName": index_name}]},
    ]
    harness.s3v.create_index.return_value = {}
    harness.s3v.get_index.return_value = {"index": _index_detail(bucket_arn, index_name)}
    harness.iam.put_role_policy.side_effect = [
        None,
        RuntimeError("tighten denied"),
    ]

    with pytest.raises(RuntimeError, match="tighten denied"):
        harness.module.handler(harness.event, None)

    assert harness.iam.put_role_policy.call_count == 2
    harness.bedrock.create_knowledge_base.assert_not_called()


def test_new_kb_role_trust_is_account_and_knowledge_base_scoped(monkeypatch):
    from app.step_handlers import knowledge_base_step

    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreKBRole-test"}}
    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda *_: None)

    knowledge_base_step._create_kb_role(
        iam,
        "AgentCoreKBRole-test",
        {"vectorStoreType": "s3_vectors"},
        REGION,
        account_id=ACCOUNT,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
    )

    trust = json.loads(iam.create_role.call_args.kwargs["AssumeRolePolicyDocument"])
    statement = trust["Statement"][0]
    assert statement["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
        "ArnLike": {"aws:SourceArn": (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/*")},
    }
    iam.update_assume_role_policy.assert_not_called()


def test_kb_s3_vectors_role_has_only_exact_index_data_plane_access():
    from app.step_handlers.knowledge_base_step import _put_kb_role_policy

    bucket_arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/customer-vectors"
    index_arn = f"{bucket_arn}/index/customer-index"
    iam = MagicMock()

    _put_kb_role_policy(
        iam,
        "AgentCoreKBRole-test",
        {
            "dataSourceType": "web_crawler",
            "vectorStoreType": "s3_vectors",
            "s3VectorsBucketArn": bucket_arn,
            "s3VectorsIndexName": "customer-index",
            "s3VectorsIndexArn": index_arn,
        },
        region=REGION,
        account_id=ACCOUNT,
    )

    policy = json.loads(iam.put_role_policy.call_args.kwargs["PolicyDocument"])
    s3vectors = [
        statement
        for statement in policy["Statement"]
        if any(str(action).startswith("s3vectors:") for action in statement["Action"])
    ]
    assert s3vectors == [
        {
            "Effect": "Allow",
            "Action": [
                "s3vectors:PutVectors",
                "s3vectors:GetVectors",
                "s3vectors:QueryVectors",
                "s3vectors:DeleteVectors",
                "s3vectors:GetIndex",
            ],
            "Resource": [index_arn],
        }
    ]


def test_existing_exact_role_remains_lifecycle_owned(monkeypatch):
    from app.step_handlers import knowledge_base_step

    class _AlreadyExists(Exception):
        pass

    iam = MagicMock()
    iam.exceptions = SimpleNamespace(EntityAlreadyExistsException=_AlreadyExists)
    iam.create_role.side_effect = _AlreadyExists()
    iam.get_role.return_value = {
        "Role": {
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreKBRole-test",
            "Tags": [{"Key": key, "Value": value} for key, value in _bound_tags().items()],
        }
    }
    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda *_: None)

    role_arn, lifecycle_owned = knowledge_base_step._create_kb_role(
        iam,
        "AgentCoreKBRole-test",
        {
            "vectorStoreType": "s3_vectors",
            "s3VectorsBucketArn": (f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/owned-vectors"),
        },
        REGION,
        account_id=ACCOUNT,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
    )

    assert role_arn.endswith("role/AgentCoreKBRole-test")
    assert lifecycle_owned is True
    iam.put_role_policy.assert_called_once()
    iam.update_assume_role_policy.assert_called_once()
    updated_trust = json.loads(iam.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
    assert updated_trust["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"] == ACCOUNT


@pytest.mark.parametrize(
    ("storage", "missing_field"),
    [
        (
            {
                "type": "S3_VECTORS",
                "s3VectorsConfiguration": {
                    "indexName": "default-index",
                },
            },
            "s3VectorsBucketArn",
        ),
        (
            {
                "type": "OPENSEARCH_SERVERLESS",
                "opensearchServerlessConfiguration": {
                    "vectorIndexName": "default-index",
                },
            },
            "opensearchCollectionArn",
        ),
        (
            {
                "type": "RDS",
                "rdsConfiguration": {
                    "credentialsSecretArn": (
                        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/rds-AbCdEf"
                    ),
                    "databaseName": "postgres",
                    "tableName": "embeddings",
                },
            },
            "rdsResourceArn",
        ),
    ],
)
def test_retry_refuses_storage_that_cannot_scope_the_role(
    storage,
    missing_field,
):
    from app.step_handlers.knowledge_base_step import (
        _hydrate_storage_from_existing_kb,
    )

    with pytest.raises(ResourceAccessRefused, match=missing_field):
        _hydrate_storage_from_existing_kb(
            {"vectorStoreType": storage["type"].lower()},
            {"knowledgeBase": {"storageConfiguration": storage}},
        )
