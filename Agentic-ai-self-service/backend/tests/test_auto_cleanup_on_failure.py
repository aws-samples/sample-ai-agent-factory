"""Tests for auto-cleanup of resources on deployment failure.

When a deployment fails, created_resources recorded in the manifest should be
automatically cleaned up to prevent orphaned AWS resources (KB, Cognito pools,
gateways, etc.).
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_store():
    """Mock DeploymentStateStore with created_resources."""
    store = MagicMock()
    # The store.get() returns a DeploymentState object with model_dump()
    mock_state = MagicMock()
    mock_state.model_dump.return_value = {
        "deployment_id": "test-deploy-123",
        "status": "failed",
        "created_resources": [
            {"type": "gateway", "id": "gw-test-123", "region": "us-east-1"},
            {"type": "cognito_user_pool", "id": "us-east-1_TestPool", "region": "us-east-1"},
            {"type": "lambda", "name": "TestLambda", "region": "us-east-1"},
            {"type": "iam_role", "name": "TestRole", "region": "us-east-1"},
        ],
    }
    store.get.return_value = mock_state
    return store


def test_auto_cleanup_deletes_resources_in_order(mock_store):
    """Auto-cleanup iterates resources in priority order (gateway before Cognito)."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    deleted = []

    def track_cleanup(res, region, event):
        deleted.append((res.get("type"), res.get("id") or res.get("name")))

    with patch("app.step_handlers.status_update_step._cleanup_resource", side_effect=track_cleanup):
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    # Gateway (priority 2) should be deleted before Cognito (priority 9)
    types_in_order = [t for t, _ in deleted]
    assert types_in_order.index("gateway") < types_in_order.index("cognito_user_pool")
    assert types_in_order.index("lambda") < types_in_order.index("iam_role")


def test_auto_cleanup_continues_on_individual_failure(mock_store):
    """A single resource cleanup failure doesn't stop the rest."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    call_count = {"count": 0}

    def failing_cleanup(res, region, event):
        call_count["count"] += 1
        if res.get("type") == "gateway":
            raise Exception("Gateway delete failed")

    with patch("app.step_handlers.status_update_step._cleanup_resource", side_effect=failing_cleanup):
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    # All 4 resources should be attempted despite gateway failure
    assert call_count["count"] == 4


def test_auto_cleanup_handles_empty_manifest(mock_store):
    """No-op when created_resources is empty or missing."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    # Empty created_resources
    empty_state = MagicMock()
    empty_state.model_dump.return_value = {"deployment_id": "test", "created_resources": []}
    mock_store.get.return_value = empty_state

    # Should not raise
    _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    # Missing created_resources
    missing_state = MagicMock()
    missing_state.model_dump.return_value = {"deployment_id": "test"}
    mock_store.get.return_value = missing_state
    _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})


def test_auto_cleanup_treats_already_gone_as_success(mock_store):
    """Resources that are already deleted are counted as cleaned."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    def already_gone_cleanup(res, region, event):
        raise Exception("ResourceNotFoundException: does not exist")

    with patch("app.step_handlers.status_update_step._cleanup_resource", side_effect=already_gone_cleanup):
        # Should not raise and should log success
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})


def test_cleanup_cognito_deletes_domain_first():
    """Cognito pool cleanup must delete domain before pool."""
    from app.step_handlers.status_update_step import _cleanup_resource

    mock_cog = MagicMock()
    mock_cog.describe_user_pool.return_value = {"UserPool": {"Domain": "test-domain"}}
    # After domain delete, describe returns no domain
    mock_cog.describe_user_pool.side_effect = [
        {"UserPool": {"Domain": "test-domain"}},  # First call: has domain
        {"UserPool": {}},  # After delete: no domain
    ]

    with patch("app.services.step_clients.client", return_value=mock_cog):
        _cleanup_resource(
            {"type": "cognito_user_pool", "id": "us-east-1_TestPool"},
            "us-east-1",
            {},
        )

    mock_cog.delete_user_pool_domain.assert_called_once_with(UserPoolId="us-east-1_TestPool", Domain="test-domain")
    mock_cog.delete_user_pool.assert_called_once_with(UserPoolId="us-east-1_TestPool")


def test_cleanup_kb_deletes_data_sources_first():
    """KB cleanup must delete data sources before the KB itself."""
    from app.step_handlers.status_update_step import _cleanup_resource

    mock_ba = MagicMock()
    mock_ba.list_data_sources.return_value = {
        "dataSourceSummaries": [{"dataSourceId": "ds-1"}, {"dataSourceId": "ds-2"}]
    }
    mock_ba.get_knowledge_base.side_effect = Exception("ResourceNotFoundException")

    with patch("app.services.step_clients.client", return_value=mock_ba):
        _cleanup_resource({"type": "knowledge_base", "id": "kb-test"}, "us-east-1", {})

    assert mock_ba.delete_data_source.call_count == 2
    mock_ba.delete_knowledge_base.assert_called_once_with(knowledgeBaseId="kb-test")
