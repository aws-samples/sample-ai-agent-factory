"""A manifest row is inventory, not a deletion capability (peer finding F-8)."""

from unittest.mock import MagicMock, call, patch

import pytest


def _state(deployment_id: str, resources: list[dict]):
    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": deployment_id,
        "user_id": "owner-1",
        "target_account_id": "111111111111",
        "status": "failed",
        "target_region": "us-east-1",
        "created_resources": resources,
    }
    return state


def test_failed_redeploy_does_not_delete_a_reused_resource_while_creator_is_live(caplog):
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    store = MagicMock()
    store.get.return_value = _state(
        "new-deploy",
        [
            {
                "type": "gateway",
                "id": "gw-existing",
                "name": "existing-gw",
                "region": "us-east-1",
                "created_by_deployment": False,
            }
        ],
    )
    store.has_other_live_resource_reference.return_value = True

    with patch("app.step_handlers.status_update_step._cleanup_resource") as cleanup:
        _auto_cleanup_on_failure(store, "new-deploy", {})

    cleanup.assert_not_called()
    assert "another live deployment" in caplog.text
    store.has_other_live_resource_reference.assert_called_once()


def test_last_live_adopter_can_reclaim_a_stack_owned_reused_resource():
    """A reused row is not a permanent leak once every other reference is gone.

    This layer only establishes co-residency authority. The cleanup dispatcher
    remains responsible for proving the exact live AgentCoreStack tag before it
    makes the AWS delete call.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    store = MagicMock()
    row = {
        "type": "gateway",
        "id": "gw-existing",
        "name": "existing-gw",
        "region": "us-east-1",
        "created_by_deployment": False,
    }
    store.get.return_value = _state("last-adopter", [row])
    store.has_other_live_resource_reference.return_value = False

    with patch("app.step_handlers.status_update_step._cleanup_resource") as cleanup:
        _auto_cleanup_on_failure(store, "last-adopter", {})

    cleanup.assert_called_once()
    assert cleanup.call_args.args[:2] == (row, "us-east-1")
    assert cleanup.call_args.args[2]["deployment_id"] == "last-adopter"
    assert "_cleanup_deadline_monotonic" in cleanup.call_args.args[2]


def test_creator_cannot_delete_after_a_later_live_deploy_adopts_the_resource(caplog):
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    store = MagicMock()
    store.get.return_value = _state(
        "old-deploy",
        [
            {
                "type": "agent_runtime",
                "id": "rt-shared",
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
    )
    store.has_other_live_resource_reference.return_value = True

    with patch("app.step_handlers.status_update_step._cleanup_resource") as cleanup:
        _auto_cleanup_on_failure(store, "old-deploy", {})

    cleanup.assert_not_called()
    assert "another live deployment" in caplog.text


def test_creator_deletes_when_no_other_live_deployment_references_the_resource():
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    store = MagicMock()
    row = {
        "type": "agent_runtime",
        "id": "rt-owned",
        "region": "us-east-1",
        "created_by_deployment": True,
    }
    store.get.return_value = _state("only-deploy", [row])
    store.has_other_live_resource_reference.return_value = False

    with patch("app.step_handlers.status_update_step._cleanup_resource") as cleanup:
        _auto_cleanup_on_failure(store, "only-deploy", {})

    cleanup.assert_called_once()
    assert cleanup.call_args.args[:2] == (row, "us-east-1")
    assert cleanup.call_args.args[2]["deployment_id"] == "only-deploy"
    assert "_cleanup_deadline_monotonic" in cleanup.call_args.args[2]


def test_duplicate_provenance_still_runs_one_last_adopter_check():
    """List order must never decide deletion authority.

    Configure and launch both record the same runtime.  During a rolling
    upgrade one row can carry new provenance while its duplicate is legacy or
    stale. One explicit "reused" prevents us from treating the creator flag as
    authority, but the canonical row may still be reclaimed after co-residency
    and live AWS ownership checks.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    store = MagicMock()
    store.get.return_value = _state(
        "redeploy",
        [
            {
                "type": "agent_runtime",
                "id": "rt-existing",
                "region": "us-east-1",
                "created_by_deployment": True,
            },
            {
                "type": "agent_runtime",
                "id": "rt-existing",
                "region": "us-east-1",
                "created_by_deployment": False,
            },
        ],
    )
    store.has_other_live_resource_reference.return_value = False

    with patch("app.step_handlers.status_update_step._cleanup_resource") as cleanup:
        _auto_cleanup_on_failure(store, "redeploy", {})

    cleanup.assert_called_once()
    cleaned_row = cleanup.call_args.args[0]
    assert cleaned_row["created_by_deployment"] is False
    store.has_other_live_resource_reference.assert_called_once()


def test_reference_scan_matches_scope_and_counts_every_not_deleted_record():
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    table = MagicMock()
    store._table = table
    table.scan.return_value = {
        "Items": [
            {
                "deployment_id": "failed-copy",
                "status": "failed",
                "target_account_id": "111111111111",
                "target_region": "us-east-1",
                "created_resources": [{"type": "guardrail", "id": "gr-failed", "created_by_deployment": True}],
            },
            {
                "deployment_id": "deleting-copy",
                "status": "succeeded",
                "delete_status": "deleting",
                "target_account_id": "111111111111",
                "target_region": "us-east-1",
                "created_resources": [{"type": "memory", "id": "mem-2", "created_by_deployment": True}],
            },
            {
                "deployment_id": "retained-copy",
                "status": "succeeded",
                "delete_status": "delete_retained",
                "target_account_id": "111111111111",
                "target_region": "us-east-1",
                "created_resources": [{"type": "secret", "id": "secret-retained", "created_by_deployment": True}],
            },
            {
                "deployment_id": "deleted-copy",
                "status": "succeeded",
                "delete_status": "deleted",
                "target_account_id": "111111111111",
                "target_region": "us-east-1",
                "created_resources": [{"type": "memory", "id": "mem-deleted", "created_by_deployment": True}],
            },
            {
                "deployment_id": "other-region",
                "status": "succeeded",
                "target_account_id": "111111111111",
                "target_region": "eu-west-1",
                "created_resources": [{"type": "gateway", "id": "gw-1", "created_by_deployment": False}],
            },
            {
                "deployment_id": "live-adopter",
                "status": "in_progress",
                "target_account_id": "111111111111",
                "target_region": "us-east-1",
                "created_resources": [{"type": "gateway", "id": "gw-1", "created_by_deployment": False}],
            },
        ]
    }

    assert store.has_other_live_resource_reference(
        "creator",
        {"type": "gateway", "id": "gw-1", "created_by_deployment": True},
        target_account_id="111111111111",
        target_region="us-east-1",
    )
    assert not store.has_other_live_resource_reference(
        "creator",
        {"type": "memory", "id": "mem-deleted", "created_by_deployment": True},
        target_account_id="111111111111",
        target_region="us-east-1",
    )
    assert not store.has_other_live_resource_reference(
        "creator",
        {"type": "memory", "id": "mem-2", "created_by_deployment": True},
        target_account_id="111111111111",
        target_region="us-east-1",
    )
    assert store.has_other_live_resource_reference(
        "creator",
        {"type": "guardrail", "id": "gr-failed", "created_by_deployment": True},
        target_account_id="111111111111",
        target_region="us-east-1",
    )
    assert store.has_other_live_resource_reference(
        "creator",
        {"type": "secret", "id": "secret-retained", "created_by_deployment": True},
        target_account_id="111111111111",
        target_region="us-east-1",
    )
    # Both checks are one cleanup pass.  The table is snapshotted once, not once
    # per manifest row.
    table.scan.assert_called_once()
    kwargs = table.scan.call_args.kwargs
    assert kwargs["ConsistentRead"] is True
    assert "created_resources" in kwargs["ProjectionExpression"]
    assert "ExpressionAttributeNames" not in kwargs


def test_reference_scan_paginates_without_dropping_the_strong_read_contract():
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    table = MagicMock()
    store._table = table
    table.scan.side_effect = [
        {
            "Items": [],
            "LastEvaluatedKey": {"deployment_id": "page-1"},
        },
        {
            "Items": [
                {
                    "deployment_id": "live-on-page-2",
                    "status": "succeeded",
                    "target_region": "us-east-1",
                    "created_resources": [{"type": "gateway", "id": "gw-page-2"}],
                }
            ]
        },
    ]

    assert store.has_other_live_resource_reference(
        "creator",
        {"type": "gateway", "id": "gw-page-2"},
        target_region="us-east-1",
    )
    assert table.scan.call_count == 2
    first = table.scan.call_args_list[0].kwargs
    second = table.scan.call_args_list[1].kwargs
    assert first["ConsistentRead"] is True
    assert second["ConsistentRead"] is True
    assert second["ExclusiveStartKey"] == {"deployment_id": "page-1"}
    assert second["ProjectionExpression"] == first["ProjectionExpression"]


def test_reference_cache_reset_only_clears_the_matching_cleanup_snapshot():
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    store._manifest_reference_cache = ("deployment-a", {("gateway", "g", "", "")})

    store.reset_manifest_reference_cache("deployment-b")
    assert store._manifest_reference_cache is not None

    store.reset_manifest_reference_cache("deployment-a")
    assert store._manifest_reference_cache is None


def test_failure_cleanup_refreshes_reference_authority_before_first_decision():
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    deployment_id = "failure-cleanup"
    store = MagicMock()
    store.get.return_value = _state(
        deployment_id,
        [
            {
                "type": "gateway",
                "id": "gateway-1",
                "name": "gateway-one",
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
    )
    store.has_other_live_resource_reference.return_value = False

    with patch("app.step_handlers.status_update_step._cleanup_resource"):
        _auto_cleanup_on_failure(store, deployment_id, {})

    store.reset_manifest_reference_cache.assert_called_once_with(deployment_id)
    reset_index = store.mock_calls.index(call.reset_manifest_reference_cache(deployment_id))
    decision_index = next(
        index
        for index, invocation in enumerate(store.mock_calls)
        if invocation[0] == "has_other_live_resource_reference"
    )
    assert reset_index < decision_index


def test_product_delete_refreshes_reference_authority_before_first_decision(
    monkeypatch,
):
    from app import deployment_handler
    from app.services import step_clients

    runtime_id = "runtime-1"
    record = {
        # Empty skips the unrelated deterministic KB-tool compatibility sweep;
        # the cache key then correctly falls back to the cleanup identifier.
        "deployment_id": "",
        "user_id": "owner-1",
        "runtime_id": runtime_id,
        "target_region": "us-east-1",
        "resource_manifest_complete": True,
        "created_resources": [
            {
                "type": "agent_runtime",
                "id": runtime_id,
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
    }
    store = MagicMock()
    store._table = object()
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, requested: record,
    )
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: MagicMock(),
    )

    def _refusal_checked_after_reset(*args, **kwargs):
        store.reset_manifest_reference_cache.assert_called_once_with(runtime_id)
        return None

    monkeypatch.setattr(
        deployment_handler,
        "manifest_delete_refusal",
        _refusal_checked_after_reset,
    )
    monkeypatch.setattr(
        deployment_handler,
        "_delete_managed_resource",
        lambda *args, **kwargs: "[manifest] runtime deleted",
    )
    monkeypatch.setattr(
        deployment_handler,
        "destroy_runtime",
        MagicMock(side_effect=AssertionError("the authoritative manifest already handled the runtime")),
    )

    result = deployment_handler._run_delete_cleanup(
        runtime_id,
        "owner-1",
    )

    assert result.success is True
    store.reset_manifest_reference_cache.assert_called_once_with(runtime_id)


def test_partial_manifest_retention_cannot_fall_through_to_legacy_memory_delete(
    monkeypatch,
):
    """A safety refusal in an unsealed manifest must survive legacy fallback.

    Partial manifests deliberately keep the old ``*_result`` fallbacks enabled so
    a failed append cannot leak an unrecorded resource.  That must not let the
    fallback delete a resource the manifest just proved is still referenced by
    another live deployment.
    """
    from app import deployment_handler
    from app.services import step_clients

    runtime_id = "runtime-with-shared-memory"
    memory_id = "memory-shared-by-newer-deploy"
    record = {
        # Keep the deterministic KB-tool compatibility sweep out of this focused
        # test.  The cleanup identifier is still a valid cache key.
        "deployment_id": "",
        "user_id": "owner-1",
        "runtime_id": runtime_id,
        "target_region": "us-east-1",
        "resource_manifest_complete": False,
        "created_resources": [
            {
                "type": "memory",
                "id": memory_id,
                "region": "us-east-1",
                "created_by_deployment": True,
            }
        ],
        # Legacy inventory points at the same resource.  Before the fix this arm
        # ignored the manifest's co-residency refusal and deleted it anyway.
        "memory_result": {
            "memory_id": memory_id,
            "memory_role_name": "AgentCoreMemory-shared",
        },
    }
    store = MagicMock()
    store._table = object()
    store.has_other_live_resource_reference.return_value = True
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, requested: record,
    )

    agentcore = MagicMock()
    target_session = MagicMock()
    target_session.client.return_value = agentcore
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: target_session,
    )
    monkeypatch.setattr(
        deployment_handler,
        "assert_agentcore_resource_owned",
        MagicMock(),
    )
    monkeypatch.setattr(
        deployment_handler,
        "_delete_managed_resource",
        MagicMock(side_effect=AssertionError("the manifest refused this resource")),
    )
    monkeypatch.setattr(
        deployment_handler,
        "destroy_runtime",
        MagicMock(return_value={"success": True, "message": "runtime deleted"}),
    )
    delete_role = MagicMock()
    monkeypatch.setattr(
        deployment_handler,
        "delete_owned_iam_role",
        delete_role,
    )

    result = deployment_handler._run_delete_cleanup(
        runtime_id,
        "owner-1",
    )

    agentcore.delete_memory.assert_not_called()
    delete_role.assert_not_called()
    assert result.success is False
    assert result.retained is True
    assert "Resources retained by deletion-authority policy: memory" in result.message


def test_legacy_result_fallbacks_all_consult_cross_deployment_references(
    monkeypatch,
):
    """Missing manifest rows are not permission to bypass co-residency checks."""
    from app import deployment_handler
    from app.services import step_clients

    record = {
        "deployment_id": "legacy-deploy-12345678",
        "user_id": "owner-1",
        "runtime_id": "runtime-main",
        "target_region": "us-east-1",
        "resource_manifest_complete": False,
        "created_resources": [],
        "mcp_server_runtime_id": "runtime-mcp",
        "policy_result": {"engine_id": "policy-shared"},
        "memory_result": {
            "memory_id": "memory-shared",
            "memory_role_name": "AgentCoreMemory-shared",
        },
        "guardrails_result": {
            "created_by_flow": True,
            "guardrail_id": "guardrail-shared",
        },
        "gateway_result": {"gateway_id": "gateway-shared"},
        "knowledge_base_result": {
            "created_by_flow": True,
            "kb_id": "kb-shared",
            "kb_role_arn": "arn:aws:iam::111111111111:role/kb-shared",
        },
    }
    store = MagicMock()
    store._table = object()
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, requested: record,
    )
    target_session = MagicMock()
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: target_session,
    )

    checked: list[tuple[str, str]] = []

    def refuse(_store, _deployment_id, resource, **_kwargs):
        checked.append((resource["type"], resource["id"]))
        return "another live deployment still references the same resource"

    monkeypatch.setattr(
        deployment_handler,
        "manifest_delete_refusal",
        refuse,
    )
    destroy_runtime = MagicMock()
    cleanup_gateway = MagicMock()
    delete_policy = MagicMock()
    delete_role = MagicMock()
    monkeypatch.setattr(deployment_handler, "destroy_runtime", destroy_runtime)
    monkeypatch.setattr(
        deployment_handler,
        "cleanup_gateway_resources",
        cleanup_gateway,
    )
    monkeypatch.setattr(
        deployment_handler,
        "delete_policy_engine_confirmed",
        delete_policy,
    )
    monkeypatch.setattr(
        deployment_handler,
        "delete_owned_iam_role",
        delete_role,
    )

    result = deployment_handler._run_delete_cleanup(
        "runtime-main",
        "owner-1",
    )

    assert {
        ("agent_runtime", "runtime-mcp"),
        ("policy_engine", "policy-shared"),
        ("memory", "memory-shared"),
        ("guardrail", "guardrail-shared"),
        ("gateway", "gateway-shared"),
        ("lambda", "AgentCore-KBTool-legacy-d"),
        ("knowledge_base", "kb-shared"),
        ("agent_runtime", "runtime-main"),
    }.issubset(set(checked))
    destroy_runtime.assert_not_called()
    cleanup_gateway.assert_not_called()
    delete_policy.assert_not_called()
    delete_role.assert_not_called()
    target_session.client.return_value.delete_memory.assert_not_called()
    target_session.client.return_value.delete_guardrail.assert_not_called()
    target_session.client.return_value.delete_knowledge_base.assert_not_called()
    target_session.client.return_value.delete_function.assert_not_called()
    assert result.success is False
    assert result.retained is True


def test_legacy_harness_fallback_consults_cross_deployment_references(
    monkeypatch,
):
    from app import deployment_handler
    from app.services import step_clients

    record = {
        "deployment_id": "",
        "user_id": "owner-1",
        "runtime_id": "harness-shared",
        "harness_id": "harness-shared",
        "deployment_mode": "harness",
        "target_region": "us-east-1",
        "resource_manifest_complete": False,
        "created_resources": [],
    }
    store = MagicMock()
    store._table = object()
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, requested: record,
    )
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: MagicMock(),
    )
    monkeypatch.setattr(
        deployment_handler,
        "manifest_delete_refusal",
        lambda _store, _deployment_id, resource, **_kwargs: (
            "another live deployment still references the same resource" if resource["type"] == "harness" else None
        ),
    )
    destroy_harness = MagicMock()
    monkeypatch.setattr(
        deployment_handler,
        "destroy_harness",
        destroy_harness,
    )

    result = deployment_handler._run_delete_cleanup(
        "harness-shared",
        "owner-1",
    )

    destroy_harness.assert_not_called()
    assert result.success is False
    assert result.retained is True
    assert "Legacy resources retained by live-ownership policy: harness" in result.message


def test_iam_identity_is_account_global_and_normalizes_arn_to_role_name():
    from app.services.deployment_state_store import manifest_resource_key

    arn_row = {
        "type": "iam_role",
        "id": "arn:aws:iam::111111111111:role/service/AgentCoreRuntime-demo",
        "region": "eu-west-1",
    }
    name_row = {
        "type": "iam_role",
        "name": "AgentCoreRuntime-demo",
        "region": "us-east-1",
    }

    assert manifest_resource_key(
        arn_row,
        default_account="999999999999",
        default_region="eu-west-1",
    ) == manifest_resource_key(
        name_row,
        default_account="111111111111",
        default_region="us-east-1",
    )


def test_lambda_arn_and_name_match_but_different_regions_do_not():
    from app.services.deployment_state_store import manifest_resource_key

    arn_key = manifest_resource_key(
        {
            "type": "lambda",
            "id": "arn:aws:lambda:us-east-1:111111111111:function:AgentCoreTool:7",
        }
    )
    name_key = manifest_resource_key(
        {"type": "lambda", "name": "AgentCoreTool"},
        default_account="111111111111",
        default_region="us-east-1",
    )
    other_region = manifest_resource_key(
        {"type": "lambda", "name": "AgentCoreTool"},
        default_account="111111111111",
        default_region="eu-west-1",
    )

    assert arn_key == name_key
    assert other_region != name_key


def test_secret_arn_matches_the_same_bare_secret_name():
    from app.services.deployment_state_store import manifest_resource_key

    assert manifest_resource_key(
        {
            "type": "secret",
            "id": ("arn:aws:secretsmanager:us-east-1:111111111111:secret:agentcore-connector/demo-Ab12Cd"),
        }
    ) == manifest_resource_key(
        {"type": "secret", "name": "agentcore-connector/demo"},
        default_account="111111111111",
        default_region="us-east-1",
    )


def test_s3_object_uri_and_arn_are_account_global():
    from app.services.deployment_state_store import manifest_resource_key

    assert manifest_resource_key(
        {
            "type": "s3_object",
            "id": "s3://artifact-bucket/deployments/demo/code.zip",
            "region": "eu-west-1",
        },
        default_account="111111111111",
    ) == manifest_resource_key(
        {
            "type": "s3_object",
            "id": "arn:aws:s3:::artifact-bucket/deployments/demo/code.zip",
            "region": "us-east-1",
        },
        default_account="111111111111",
    )


def test_cognito_child_identity_includes_its_pool():
    from app.services.deployment_state_store import manifest_resource_key

    first_pool = manifest_resource_key(
        {
            "type": "cognito_resource_server",
            "id": "gateway/read",
            "pool_id": "us-east-1_POOL_A",
        },
        default_account="111111111111",
        default_region="us-east-1",
    )
    second_pool = manifest_resource_key(
        {
            "type": "cognito_resource_server",
            "id": "gateway/read",
            "pool_id": "us-east-1_POOL_B",
        },
        default_account="111111111111",
        default_region="us-east-1",
    )

    assert first_pool != second_pool


def test_account_is_a_discriminating_part_of_the_identity():
    from app.services.deployment_state_store import manifest_resource_key

    row = {"type": "gateway", "id": "gw-1"}
    assert manifest_resource_key(
        row,
        default_account="111111111111",
        default_region="us-east-1",
    ) != manifest_resource_key(
        row,
        default_account="222222222222",
        default_region="us-east-1",
    )


def test_legacy_row_without_provenance_still_runs_the_co_residency_gate():
    from app.services.deployment_state_store import manifest_delete_refusal

    store = MagicMock()
    store.has_other_live_resource_reference.return_value = True
    row = {"type": "gateway", "id": "gw-legacy", "region": "us-east-1"}

    reason = manifest_delete_refusal(
        store,
        "legacy-deploy",
        row,
        target_region="us-east-1",
    )

    assert reason == "another live deployment still references the same resource"
    store.has_other_live_resource_reference.assert_called_once_with(
        "legacy-deploy",
        row,
        target_account_id=None,
        target_region="us-east-1",
    )


def test_reference_scan_fails_closed_for_an_unidentifiable_manifest_row():
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    store._table = MagicMock()

    assert store.has_other_live_resource_reference(
        "creator",
        {"type": "gateway", "created_by_deployment": True},
        target_region="us-east-1",
    )
    store._table.scan.assert_not_called()


def test_reference_scan_failure_refuses_deletion():
    from app.services.deployment_state_store import manifest_delete_refusal

    store = MagicMock()
    store.has_other_live_resource_reference.side_effect = RuntimeError("DynamoDB unavailable")
    reason = manifest_delete_refusal(
        store,
        "creator",
        {
            "type": "gateway",
            "id": "gw-1",
            "region": "us-east-1",
            "created_by_deployment": True,
        },
        target_region="us-east-1",
    )

    assert reason is not None
    assert "could not prove" in reason
    assert "DynamoDB unavailable" not in reason


@pytest.mark.parametrize(
    "row",
    [
        {"type": "gateway", "id": "gw-without-provenance"},
        {
            "type": "gateway",
            "id": "gw-with-ambiguous-provenance",
            "created_by_deployment": None,
        },
    ],
)
def test_new_manifest_writes_require_explicit_boolean_provenance(row):
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    store._table = MagicMock()

    with pytest.raises(ValueError, match="created_by_deployment"):
        store.record_resource_strict("deployment", row)

    store._table.update_item.assert_not_called()


def test_best_effort_writer_does_not_swallow_a_provenance_programming_error():
    from app.services.deployment_state_store import DeploymentStateStore

    store = object.__new__(DeploymentStateStore)
    store._table = MagicMock()

    with pytest.raises(ValueError, match="created_by_deployment"):
        store.record_resource(
            "deployment",
            {"type": "gateway", "id": "gw-without-provenance"},
        )

    store._table.update_item.assert_not_called()
