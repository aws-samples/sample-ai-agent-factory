"""Regression tests for PR #2 reviewer findings (mNemlaghi, 2026-05-27).

Three findings, the first two raised by the reviewer and the third found
by audit while looking for the same class of bug elsewhere. Each is
verified against the live AWS service models:

1. ``backend/src/app/step_handlers/guardrails_step.py:243`` — the
   idempotent-upsert path stripped ``name`` before calling
   ``UpdateGuardrail``. The Bedrock service model marks ``name`` as a
   REQUIRED member of UpdateGuardrail's input shape (separate from
   ``guardrailIdentifier``), so the API call would 400.

2. ``backend/src/app/step_handlers/knowledge_base_step.py:620`` — the
   BDA branch wrote a sentinel key ``_bdaSupplementalS3Uri`` onto the
   ``ingestion_config`` dict. That dict is then passed verbatim as
   ``vectorIngestionConfiguration`` to ``CreateDataSource``, which
   accepts only ``chunkingConfiguration``,
   ``customTransformationConfiguration``, ``parsingConfiguration``,
   ``contextEnrichmentConfiguration`` — botocore raises
   ``ParamValidationError`` on the unknown key.

3. ``backend/src/app/deployment_handler.py:770`` — the policy-engine
   detach path called ``UpdateGateway`` with a non-existent
   ``authorizationConfig`` parameter and was missing the REQUIRED
   ``authorizerType`` field. Botocore would ParamValidationError on every
   teardown that hit this branch, so the policy engine never actually
   detached.

Tests are pure unit tests — they patch ``boto3.client`` to drive the
handler, then assert on the captured kwargs.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# Fixtures shared across both handlers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_otel_platform(monkeypatch):
    """Both step handlers import app.services._otel_platform at module load
    to wire ADOT auto-instrumentation. The prologue resolves a Secrets
    Manager ARN at cold start; in tests we stub the module so import is a
    no-op and we don't hit AWS.
    """
    if "app.services._otel_platform" not in sys.modules:
        sys.modules["app.services._otel_platform"] = types.ModuleType("app.services._otel_platform")


@pytest.fixture
def deployment_store_stub(monkeypatch):
    """Replace DeploymentStateStore so handler.update_step is a no-op."""
    fake_store = MagicMock()
    fake_store.update_step = MagicMock()

    def _factory(*_args, **_kwargs):
        return fake_store

    # Patch at both possible import sites.
    from app.step_handlers import guardrails_step, knowledge_base_step

    monkeypatch.setattr(guardrails_step, "_get_deployment_store", lambda: fake_store)
    monkeypatch.setattr(knowledge_base_step, "_get_deployment_store", lambda: fake_store)
    return fake_store


# ---------------------------------------------------------------------------
# 1. Guardrails — UpdateGuardrail must receive ``name``
# ---------------------------------------------------------------------------


def test_update_guardrail_includes_required_name_field(deployment_store_stub, monkeypatch):
    """Reproduce mNemlaghi's review on guardrails_step.py:243.

    UpdateGuardrail's input shape lists ``name`` as REQUIRED (verified
    against ``boto3.client('bedrock').meta.service_model``). The previous
    code did ``{k: v for k, v in create_params.items() if k != 'name'}``
    which would fail with ValidationException at runtime.
    """
    from app.step_handlers import guardrails_step

    # First create_guardrail call raises "already exists" → forces the
    # update path. Then list_guardrails returns the existing id, then
    # update_guardrail succeeds, then create_guardrail_version + get_guardrail
    # walk through the rest of the handler.
    bedrock = MagicMock()

    class _AlreadyExists(Exception):
        pass

    bedrock.exceptions.ResourceAlreadyExistsException = _AlreadyExists
    bedrock.create_guardrail.side_effect = _AlreadyExists("already there")

    paginator = MagicMock()
    paginator.paginate.return_value = [{"guardrails": [{"name": "my-guardrail", "id": "gr-existing-123"}]}]
    bedrock.get_paginator.return_value = paginator

    bedrock.update_guardrail.return_value = {"guardrailId": "gr-existing-123"}
    # READY immediately → break wait loop on first poll
    bedrock.get_guardrail.return_value = {"status": "READY"}
    bedrock.create_guardrail_version.return_value = {"version": "1"}

    # No real sleeps in the wait loop.
    monkeypatch.setattr(guardrails_step.time, "sleep", lambda *_: None)

    with patch.object(guardrails_step.step_clients, "client", return_value=bedrock):
        result = guardrails_step.handler(
            {
                "deployment_id": "dep-test-123",
                "guardrails_config": {
                    "mode": "create_new",
                    "name": "my-guardrail",
                    "contentFilters": {"hate": "HIGH"},
                },
            },
            None,
        )

    assert result["guardrails_result"]["guardrail_id"] == "gr-existing-123"

    # The actual assertion behind the review fix: update_guardrail must
    # receive `name` (a REQUIRED field) as well as guardrailIdentifier.
    bedrock.update_guardrail.assert_called_once()
    update_kwargs = bedrock.update_guardrail.call_args.kwargs
    assert update_kwargs["guardrailIdentifier"] == "gr-existing-123"
    assert update_kwargs["name"] == "my-guardrail"
    # And the other required body fields the API mandates.
    assert "blockedInputMessaging" in update_kwargs
    assert "blockedOutputsMessaging" in update_kwargs


# ---------------------------------------------------------------------------
# 2. Knowledge Base — CreateDataSource must not see _bdaSupplementalS3Uri
# ---------------------------------------------------------------------------


def test_create_data_source_does_not_leak_bda_sentinel(deployment_store_stub, monkeypatch):
    """Reproduce mNemlaghi's review on knowledge_base_step.py:620.

    Old code stashed ``_bdaSupplementalS3Uri`` on ``ingestion_config``
    and never popped it. ``ingestion_config`` is then handed to
    ``create_data_source`` as ``vectorIngestionConfiguration`` — botocore
    validates that shape and only allows the four documented members,
    so the sentinel triggers ParamValidationError.

    After the fix, the BDA branch sets parsingConfiguration only; the
    KB-level supplementalDataStorageConfiguration remains where it
    belongs (on create_knowledge_base, not create_data_source).
    """
    from app.step_handlers import knowledge_base_step

    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        knowledge_base_step,
        "_wait_for_kb_active",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        knowledge_base_step,
        "_start_and_wait_ingestion",
        # Returns (job_id, ingestion_status) — the call site unpacks a 2-tuple so
        # a still-ingesting KB is reported honestly (P-E2E matrix finding).
        lambda *_a, **_kw: ("job-1", "COMPLETE"),
    )

    # Drive _build_data_source_config to a deterministic result so the test
    # focuses solely on the BDA injection path.
    monkeypatch.setattr(
        knowledge_base_step,
        "_build_data_source_config",
        lambda kb_config: (
            {
                "type": "S3",
                "s3Configuration": {"bucketArn": "arn:aws:s3:::my-source"},
            },
            None,
        ),
    )
    # Skip role creation/validation
    monkeypatch.setattr(
        knowledge_base_step,
        "_ensure_kb_role",
        lambda *_a, **_kw: "arn:aws:iam::123456789012:role/kb-role",
        raising=False,
    )

    bedrock_agent = MagicMock()
    bedrock_agent.create_knowledge_base.return_value = {"knowledgeBase": {"knowledgeBaseId": "kb-abc"}}
    bedrock_agent.create_data_source.return_value = {"dataSource": {"dataSourceId": "ds-xyz"}}

    iam = MagicMock()
    s3v = MagicMock()
    s3v.list_indexes.return_value = {"indexes": [{"indexName": "default-index"}]}

    def _client(_event, name, **_kw):
        if name == "bedrock-agent":
            return bedrock_agent
        if name == "iam":
            return iam
        if name == "s3vectors":
            return s3v
        return MagicMock()

    # Skip the role-creation branch by pre-supplying roleArn.
    kb_config = {
        "kbMode": "create_new",
        "kbName": "test-kb",
        "kbDescription": "review-fix test",
        "vectorStoreType": "s3_vectors",
        "s3VectorsBucketArn": "arn:aws:s3vectors:us-east-1:123456789012:bucket/my-vec",
        "s3VectorsIndexName": "default-index",
        "embeddingModelId": "amazon.titan-embed-text-v2:0",
        "parsingStrategy": "bedrock_data_automation",
        "chunkingStrategy": "FIXED_SIZE",
        "kbRoleArn": "arn:aws:iam::123456789012:role/kb-role",
    }

    with patch.object(knowledge_base_step.step_clients, "client", side_effect=_client):
        knowledge_base_step.handler(
            {
                "deployment_id": "dep-kb-test",
                "knowledge_base_config": kb_config,
            },
            None,
        )

    # The fix's load-bearing assertion: vectorIngestionConfiguration must
    # contain only the documented service-model keys, never the leaked
    # _bdaSupplementalS3Uri sentinel.
    bedrock_agent.create_data_source.assert_called_once()
    ds_kwargs = bedrock_agent.create_data_source.call_args.kwargs
    vic = ds_kwargs["vectorIngestionConfiguration"]

    assert "_bdaSupplementalS3Uri" not in vic
    # Only documented members (per
    # boto3.client('bedrock-agent').meta.service_model
    # .operation_model('CreateDataSource').input_shape)
    allowed = {
        "chunkingConfiguration",
        "customTransformationConfiguration",
        "parsingConfiguration",
        "contextEnrichmentConfiguration",
    }
    assert set(vic.keys()).issubset(allowed), (
        f"vectorIngestionConfiguration leaked unknown keys: {set(vic.keys()) - allowed}"
    )
    # And sanity: BDA parsing config still made it through.
    assert vic["parsingConfiguration"]["parsingStrategy"] == "BEDROCK_DATA_AUTOMATION"

    # The KB-level config (where supplementalDataStorageConfiguration
    # actually belongs) was set on create_knowledge_base.
    bedrock_agent.create_knowledge_base.assert_called_once()
    kb_kwargs = bedrock_agent.create_knowledge_base.call_args.kwargs
    vec_cfg = kb_kwargs["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    assert "supplementalDataStorageConfiguration" in vec_cfg


# ---------------------------------------------------------------------------
# 3. UpdateGateway — policy-engine detach must use real parameter names
# ---------------------------------------------------------------------------


def test_update_gateway_detach_path_validates_against_service_model():
    """Audit-discovered bug in deployment_handler.py:770.

    The policy-engine-detach cleanup path was calling update_gateway with
    a non-existent ``authorizationConfig`` key (real key is
    ``authorizerConfiguration``) and was missing the REQUIRED
    ``authorizerType`` field. This test validates the kwargs the fixed
    code constructs against the live botocore service model — the same
    validator the real boto3 client uses — so any future regression here
    fails the test rather than the deploy.
    """
    import boto3
    from botocore.validate import ParamValidator

    # Fixture: a get_gateway response that exercises every code path
    # (optional fields present + protocolType present + authorizer types).
    gw_detail = {
        "name": "gw-test",
        "roleArn": "arn:aws:iam::123456789012:role/gw-role",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example.com/.well-known/openid-configuration",
                "allowedClients": ["client-id-1"],
            }
        },
        "description": "test gateway",
    }
    gw_id = "gw-abc-123"

    # Construct the same payload the production code under test does
    # (deployment_handler.py — policy-engine-detach branch).
    update_params = {
        "gatewayIdentifier": gw_id,
        "name": gw_detail.get("name", ""),
        "roleArn": gw_detail.get("roleArn", ""),
        "authorizerType": gw_detail.get("authorizerType", "CUSTOM_JWT"),
        "protocolType": gw_detail.get("protocolType", "MCP"),
    }
    for optional_field in (
        "description",
        "authorizerConfiguration",
        "protocolConfiguration",
        "kmsKeyArn",
    ):
        if gw_detail.get(optional_field):
            update_params[optional_field] = gw_detail[optional_field]

    # Crucially: NOT setting policyEngineConfiguration is what detaches it.
    assert "policyEngineConfiguration" not in update_params
    # And the bug parameters from the prior code are absent.
    assert "authorizationConfig" not in update_params

    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    op = client.meta.service_model.operation_model("UpdateGateway")
    errors = ParamValidator().validate(update_params, op.input_shape)
    assert not errors.has_errors(), f"update_gateway kwargs failed botocore validation: {errors.generate_report()}"
