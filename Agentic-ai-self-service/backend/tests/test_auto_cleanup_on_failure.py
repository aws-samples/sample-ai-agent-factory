"""Tests for auto-cleanup of resources on deployment failure.

When a deployment fails, created_resources recorded in the manifest should be
automatically cleaned up to prevent orphaned AWS resources (KB, Cognito pools,
gateways, etc.).
"""

import logging
from unittest.mock import MagicMock, call, patch

import pytest
from app.services.resource_ownership import (
    aoss_policy_owner_description,
    owner_tags,
)
from botocore.exceptions import ClientError

from tests.fake_versioned_s3 import FakeVersionedS3


@pytest.fixture
def mock_store():
    """Mock DeploymentStateStore with created_resources."""
    store = MagicMock()
    # The store.get() returns a DeploymentState object with model_dump()
    mock_state = MagicMock()
    mock_state.model_dump.return_value = {
        "deployment_id": "test-deploy-123",
        "user_id": "owner-1",
        "target_account_id": "123456789012",
        "status": "failed",
        "created_resources": [
            {"type": "gateway", "id": "gw-test-123", "name": "test-gw", "region": "us-east-1"},
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
    """A single resource cleanup failure doesn't stop the rest.

    The failure is a Lambda's: a gateway that survives freezes its graph instead,
    which tests/test_gateway_graph_stays_whole.py covers."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    attempted = []

    def failing_cleanup(res, region, event):
        attempted.append(res.get("type"))
        if res.get("type") == "lambda":
            raise Exception("Lambda delete failed")

    with patch("app.step_handlers.status_update_step._cleanup_resource", side_effect=failing_cleanup):
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    # All 4 resources are attempted despite the Lambda's failure
    assert sorted(attempted) == ["cognito_user_pool", "gateway", "iam_role", "lambda"]
    assert attempted.index("lambda") < attempted.index("iam_role")


def test_auto_cleanup_handles_empty_manifest(mock_store):
    """An empty manifest is retained because absence is not proof of no resources."""
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

    final_calls = [call for call in mock_store.update_delete_status.call_args_list if call.args[1] == "delete_retained"]
    assert len(final_calls) == 2


def test_successful_failure_cleanup_marks_the_row_deleted(mock_store):
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    with patch("app.step_handlers.status_update_step._cleanup_resource"):
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    assert mock_store.update_delete_status.call_args_list[0].args[:2] == (
        "test-deploy-123",
        "deleting",
    )
    assert mock_store.update_delete_status.call_args_list[-1].args[:2] == (
        "test-deploy-123",
        "deleted",
    )


def test_failed_failure_cleanup_keeps_a_durable_retry_row(mock_store):
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    with patch(
        "app.step_handlers.status_update_step._cleanup_resource",
        side_effect=RuntimeError("delete failed"),
    ):
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})

    assert mock_store.update_delete_status.call_args_list[-1].args[:2] == (
        "test-deploy-123",
        "delete_failed",
    )


def test_auto_cleanup_treats_already_gone_as_success(mock_store):
    """Resources that are already deleted are counted as cleaned."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    def already_gone_cleanup(res, region, event):
        raise Exception("ResourceNotFoundException: does not exist")

    with patch("app.step_handlers.status_update_step._cleanup_resource", side_effect=already_gone_cleanup):
        # Should not raise and should log success
        _auto_cleanup_on_failure(mock_store, "test-deploy-123", {})


def test_a_duplicated_manifest_row_is_deleted_once(caplog):
    """The delete path dedupes on ``(type, id)``; this path did not.

    The duplicate is real, not hypothetical: ``runtime_configure_step`` records the
    ``agent_runtime`` row and ``runtime_launch_step`` deliberately re-records the same
    one, so every deploy with a runtime carries two byte-identical rows. A second
    ``delete_agent_runtime`` for one runtime lands while the first delete is still in
    progress, which is not a "gone" error — so a correct teardown reported
    ``Auto-cleanup failed``.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-dup",
        "created_resources": [
            {"type": "agent_runtime", "id": "rt-abc", "region": "us-east-1"},
            {"type": "agent_runtime", "id": "rt-abc", "region": "us-east-1"},
            {"type": "iam_role", "name": "R", "region": "us-east-1"},
        ],
    }
    store = MagicMock()
    store.get.return_value = state

    deleted = []
    with patch(
        "app.step_handlers.status_update_step._cleanup_resource",
        side_effect=lambda res, region, event: deleted.append((res.get("type"), res.get("id") or res.get("name"))),
    ):
        with caplog.at_level("INFO", logger="app.step_handlers.status_update_step"):
            _auto_cleanup_on_failure(store, "d-dup", {})

    assert deleted == [("agent_runtime", "rt-abc"), ("iam_role", "R")]
    # And the count must be over DISTINCT resources, or a complete cleanup of a
    # manifest with a duplicate row reports as partial (2/3).
    assert "2/2 resources" in caplog.text, caplog.text


def test_two_resources_of_one_type_with_different_ids_are_both_deleted():
    """The dedupe key is (type, id), not type — or a gateway with two Lambda targets
    would have exactly one of them cleaned up."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-two",
        "created_resources": [
            {"type": "lambda", "name": "fn-a", "region": "us-east-1"},
            {"type": "lambda", "name": "fn-b", "region": "us-east-1"},
        ],
    }
    store = MagicMock()
    store.get.return_value = state

    deleted = []
    with patch(
        "app.step_handlers.status_update_step._cleanup_resource",
        side_effect=lambda res, region, event: deleted.append(res.get("name")),
    ):
        _auto_cleanup_on_failure(store, "d-two", {})

    assert deleted == ["fn-a", "fn-b"]


def test_cleanup_cognito_deletes_domain_first(monkeypatch):
    """Cognito pool cleanup must delete domain before pool."""
    from app.services.resource_ownership import owner_tags
    from app.step_handlers.status_update_step import _cleanup_resource

    monkeypatch.setenv("PROJECT_NAME", "agentcore-workflow")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    mock_cog = MagicMock()

    # describe_user_pool is now called for THREE different reasons, so a fixed
    # side_effect list is brittle: teardown first classifies the pool (ownership must
    # be PROVEN from the AgentCoreStack tag before anything is deleted), then reads the
    # attached domain, then polls until the domain is gone. Model the state instead of
    # counting calls.
    #
    # The tags are derived from owner_tags() rather than hardcoded so the double cannot
    # drift from what create_user_pool actually stamps — an untagged pool here would
    # model a pool this platform never creates, and would be (correctly) protected
    # rather than deleted.
    def _describe(**kw):
        pool = {"UserPoolTags": owner_tags("us-east-1")}
        if not mock_cog.delete_user_pool_domain.called:
            pool["Domain"] = "test-domain"
        return {"UserPool": pool}

    mock_cog.describe_user_pool.side_effect = _describe

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
    mock_ba.get_knowledge_base.side_effect = [
        {
            "knowledgeBase": {
                "knowledgeBaseId": "kb-test",
                "knowledgeBaseArn": "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/kb-test",
            }
        },
        Exception("ResourceNotFoundException"),
    ]
    mock_ba.list_tags_for_resource.return_value = {"tags": owner_tags("us-east-1")}

    with patch("app.services.step_clients.client", return_value=mock_ba):
        _cleanup_resource({"type": "knowledge_base", "id": "kb-test"}, "us-east-1", {})

    assert mock_ba.delete_data_source.call_count == 2
    mock_ba.delete_knowledge_base.assert_called_once_with(knowledgeBaseId="kb-test")


# ---------------------------------------------------------------------------
# F-29 — the failure path and the delete path are two dispatchers over one
# manifest, and they had drifted. Measured live on deployment 959b2c60
# (2026-09-21): `s3_object` and `oss_collection` were recorded by the deploy,
# handled by deployment_handler._delete_managed_resource, and handled NOWHERE on
# the failure path — where an unrecognised type fell off the end of
# _cleanup_resource and RETURNED, which the caller counted as a successful
# delete. So a ~$350/mo OpenSearch Serverless collection could be left running
# and the log would still read "N/N resources".
# ---------------------------------------------------------------------------


def _dict_type(node):
    """The ``"type"`` literal of a dict AST node, if it has a constant one."""
    import ast

    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values, strict=True):
            if isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant):
                return v.value
    return None


def _recorded_resource_types():
    """Every resource type the deploy path actually writes into the manifest.

    Parsed from the implementation rather than declared in a list here, because a
    declared list is the thing that drifts: the two defects this pins were both
    "somebody added a resource type and updated one of the three places".

    Scope is functions that call ``record_resource``/``_rec``, and only the dict
    literal actually PASSED to the call — walking every dict in those functions
    swept up unrelated config literals (``S3``, ``knn_vector``) and would have made
    this assertion fail for the wrong reason.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    found = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                c
                for c in ast.walk(fn)
                if isinstance(c, ast.Call)
                and (
                    (isinstance(c.func, ast.Attribute) and c.func.attr == "record_resource")
                    or (isinstance(c.func, ast.Name) and c.func.id == "_rec")
                )
            ]
            if not calls:
                continue
            # A name can be re-assigned a DIFFERENT dict later in the same function
            # (gateway_step assigns `entry` twice). Keeping only the last one silently
            # dropped litellm_gateway from this set — an oracle that under-detects is
            # exactly as useless as no oracle.
            assigned: dict[str, list] = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            assigned.setdefault(tgt.id, []).append(node.value)
            for call_node in calls:
                if not call_node.args:
                    continue
                arg = call_node.args[-1]
                candidates = [arg] if isinstance(arg, ast.Dict) else assigned.get(getattr(arg, "id", None), [])
                for cand in candidates:
                    rtype = _dict_type(cand)
                    if rtype:
                        found.setdefault(rtype, f"{path.name}:{cand.lineno}")
    return found


def _handled_resource_types(rel_path, func_name):
    """Every type a dispatcher branches on, read off its ``rtype == ...`` comparisons."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "app" / rel_path
    tree = ast.parse(src.read_text())
    handled = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == func_name):
            continue
        for cmp_node in ast.walk(fn):
            if not isinstance(cmp_node, ast.Compare):
                continue
            if not (isinstance(cmp_node.left, ast.Name) and cmp_node.left.id == "rtype"):
                continue
            for op, comparator in zip(cmp_node.ops, cmp_node.comparators, strict=True):
                if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                    handled.add(comparator.value)
                elif isinstance(op, ast.In) and isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    handled.update(e.value for e in comparator.elts if isinstance(e, ast.Constant))
    return handled


def test_every_recorded_resource_type_has_a_deleter_on_the_failure_path():
    """A type the deploy records but the failure path cannot delete is an orphan.

    This is the assertion that was missing when ``s3_object`` and ``oss_collection``
    shipped: both were recorded, both were deletable on the delete path, and a failed
    deploy left both running while reporting a complete cleanup.
    """
    recorded = _recorded_resource_types()
    assert len(recorded) >= 15, f"the oracle under-detected; only found {sorted(recorded)}"
    failure_path = _handled_resource_types("step_handlers/status_update_step.py", "_cleanup_resource")
    missing = {t: where for t, where in recorded.items() if t not in failure_path}
    assert not missing, (
        "recorded by the deploy but NOT deletable by _auto_cleanup_on_failure — a failed "
        f"deploy orphans these: {missing}"
    )


def test_the_two_teardown_dispatchers_handle_exactly_the_same_types():
    """Parity in BOTH directions between the failure path and the delete path.

    Equality, not containment: a type handled on only one path is a defect whichever
    path is missing it, and the pair drifted precisely because nothing compared them.
    """
    failure_path = _handled_resource_types("step_handlers/status_update_step.py", "_cleanup_resource")
    delete_path = _handled_resource_types("deployment_handler.py", "_delete_managed_resource")
    assert failure_path == delete_path, (
        f"only on the failure path: {sorted(failure_path - delete_path)}; "
        f"only on the delete path: {sorted(delete_path - failure_path)}"
    )


def test_an_unrecognised_type_is_not_counted_as_cleaned(caplog):
    """The accounting defect, separately from the missing arms.

    Before this, an unknown type returned normally and was counted, so the completion
    ratio could never be used as evidence. It must now be loud and it must not count.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-unknown",
        "user_id": "owner-1",
        "target_account_id": "123456789012",
        "created_resources": [
            {"type": "gateway", "id": "gw-1", "name": "test-gw", "region": "us-east-1"},
            {"type": "something_invented_later", "id": "x-1", "region": "us-east-1"},
        ],
    }
    store = MagicMock()
    store.get.return_value = state

    ctrl = MagicMock()
    ctrl.get_gateway.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gateway does not exist"}}, "GetGateway"
    )
    with patch("app.services.step_clients.client", return_value=ctrl):
        with caplog.at_level("INFO", logger="app.step_handlers.status_update_step"):
            _auto_cleanup_on_failure(store, "d-unknown", {})

    assert "1/2 resources" in caplog.text, caplog.text
    assert "NO deleter for type 'something_invented_later'" in caplog.text, caplog.text


def test_an_oss_collection_is_deleted_with_its_three_policies():
    """The most expensive orphan: a standing OpenSearch Serverless collection."""
    from app.step_handlers.status_update_step import _cleanup_resource

    aoss = MagicMock()
    collection = {
        "collectionDetails": [
            {
                "id": "abc123xyz",
                "arn": "arn:aws:aoss:us-east-1:123456789012:collection/abc123xyz",
            }
        ]
    }
    aoss.batch_get_collection.side_effect = [
        collection,
        {"collectionDetails": []},
    ]
    aoss.list_tags_for_resource.return_value = {
        "tags": [{"key": key, "value": value} for key, value in owner_tags("us-east-1").items()]
    }
    access_policy = {
        "accessPolicyDetail": {
            "description": aoss_policy_owner_description(
                "us-east-1",
                "test",
            )
        }
    }
    security_policy = {
        "securityPolicyDetail": {
            "description": aoss_policy_owner_description(
                "us-east-1",
                "test",
            )
        }
    }
    aoss.get_access_policy.side_effect = [
        access_policy,
        RuntimeError("ResourceNotFoundException: access policy is gone"),
    ]
    aoss.get_security_policy.side_effect = [
        security_policy,
        RuntimeError("ResourceNotFoundException: network policy is gone"),
        security_policy,
        RuntimeError("ResourceNotFoundException: encryption policy is gone"),
    ]

    with patch("app.services.step_clients.client", return_value=aoss):
        _cleanup_resource(
            {"type": "oss_collection", "name": "agentcore-kb-coll", "region": "us-east-1"}, "us-east-1", {}
        )

    # The recorded row carries the NAME; DeleteCollection takes the service-minted id,
    # so the resolve step is not optional.
    assert aoss.batch_get_collection.call_args_list == [
        call(names=["agentcore-kb-coll"]),
        call(names=["agentcore-kb-coll"]),
    ]
    aoss.delete_collection.assert_called_once_with(id="abc123xyz")
    aoss.delete_access_policy.assert_called_once_with(name="agentcore-kb-coll-acc", type="data")
    assert {c.kwargs["type"] for c in aoss.delete_security_policy.call_args_list} == {"network", "encryption"}


def test_a_staged_spec_object_is_deleted_on_the_failure_path():
    """gateway_step records an s3_object row for every connector spec too large to inline."""
    from app.step_handlers.status_update_step import _cleanup_resource

    s3 = FakeVersionedS3("Enabled")
    version = s3.put(
        "acf-artifacts", "specs/deploy-1/petstore.json", {**owner_tags("us-east-1"), "DeploymentId": "deploy-1"}
    )
    with patch("app.services.step_clients.client", return_value=s3):
        _cleanup_resource(
            {"type": "s3_object", "id": "s3://acf-artifacts/specs/deploy-1/petstore.json", "region": "us-east-1"},
            "us-east-1",
            {"deployment_id": "deploy-1"},
        )

    assert [kw for op, kw in s3.calls if op == "DeleteObject"] == [
        {"Bucket": "acf-artifacts", "Key": "specs/deploy-1/petstore.json", "VersionId": version}
    ]
    assert s3.data_versions("acf-artifacts", "specs/deploy-1/petstore.json") == []


def test_cross_account_s3_delete_is_bound_to_the_recorded_bucket_owner():
    from app.step_handlers.status_update_step import _cleanup_resource

    s3 = FakeVersionedS3("Enabled")
    s3.put("customer-artifacts", "deployments/d-1/code.zip", {**owner_tags("eu-central-1"), "DeploymentId": "d-1"})
    with patch("app.services.step_clients.client", return_value=s3):
        _cleanup_resource(
            {
                "type": "s3_object",
                "id": "s3://customer-artifacts/deployments/d-1/code.zip",
                "region": "eu-central-1",
                "account": "210987654321",
            },
            "us-east-1",
            {
                "target_account_id": "210987654321",
                "deployment_id": "d-1",
            },
        )

    assert s3.data_versions("customer-artifacts", "deployments/d-1/code.zip") == []
    assert s3.calls and all(kw.get("ExpectedBucketOwner") == "210987654321" for _, kw in s3.calls)


def test_a_non_s3_uri_on_an_s3_object_row_deletes_nothing():
    """A malformed row must not turn into a delete_object against a guessed bucket."""
    from app.step_handlers.status_update_step import _cleanup_resource

    s3 = MagicMock()
    with patch("app.services.step_clients.client", return_value=s3):
        with pytest.raises(ValueError, match="Malformed s3_object"):
            _cleanup_resource(
                {"type": "s3_object", "id": "specs/petstore.json"},
                "us-east-1",
                {},
            )

    s3.delete_object.assert_not_called()


def test_a_litellm_gateway_row_deletes_nothing_but_is_still_counted(caplog):
    """The customer's own proxy. Nothing to delete — and that has to be deliberate,
    not an unhandled type, now that an unhandled type is an error."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-ll",
        "created_resources": [
            {"type": "litellm_gateway", "id": "https://proxy.example.internal", "region": "us-east-1"}
        ],
    }
    store = MagicMock()
    store.get.return_value = state

    client = MagicMock()
    with patch("app.services.step_clients.client", return_value=client):
        with caplog.at_level("INFO", logger="app.step_handlers.status_update_step"):
            _auto_cleanup_on_failure(store, "d-ll", {})

    assert "1/1 resources" in caplog.text, caplog.text
    assert "NO deleter" not in caplog.text, caplog.text
    client.assert_not_called()


def test_iam_nosuchentity_is_treated_as_already_gone():
    """The live message, verbatim from deployment 959b2c60's cleanup log.

    IAM says neither "not found" nor "does not exist", so a role that had never been
    created was reported as ``Auto-cleanup failed`` and kept out of the cleaned count.
    """
    from app.step_handlers.status_update_step import _gone

    assert _gone(
        Exception(
            "An error occurred (NoSuchEntity) when calling the DeleteRole operation: "
            "The role with name AgentCoreGateway-f28live18639303 cannot be found."
        )
    )
    # And the predicate must still refuse the shape that Bug 187 was about: a gateway
    # that still has targets is NOT gone, and treating it as gone orphans it.
    assert not _gone(
        Exception(
            "An error occurred (ValidationException) when calling the DeleteGateway operation: "
            "Gateway gw-1 has targets associated with it. Delete all targets before deleting the gateway."
        )
    )


def test_the_cleanup_outcome_lines_are_visible_in_a_deployed_lambda(caplog):
    """Every assertion about the cleanup's own reporting above is made through
    ``caplog.at_level("INFO", ...)``, and that is the bug: it raises the logger's level
    for the duration of the test, so an ``info`` line that production silently discards
    is captured anyway and the tests pass either way.

    Measured live 2026-09-21. CloudWatch for a real failure-path cleanup that deleted a
    secret and failed to delete two gateways contained exactly one line — the
    ``[ERROR] ... full cause (redacted)`` one. ``Auto-cleanup completed for ...: N/N
    resources`` was absent, because the module logger is at NOTSET, the Lambda root
    logger sits at WARNING, and so ``logger.info`` never reaches a handler. The
    completion ratio is the only record that a destructive pass ran over a customer's
    account, and it did not exist where it matters. Same defect class as commit eb8da18
    (``cfn_response`` logging nothing in production).

    This test therefore does NOT touch the level. It asserts on records captured at
    pytest's default root level, which is the same WARNING the deployed Lambda applies —
    so an outcome line demoted back to ``info`` disappears here exactly as it does in
    production.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-vis",
        "user_id": "owner-1",
        "target_account_id": "123456789012",
        "created_resources": [{"type": "gateway", "id": "gw-1", "name": "test-gw", "region": "us-east-1"}],
    }
    store = MagicMock()
    store.get.return_value = state

    with patch("app.step_handlers.status_update_step._cleanup_resource"):
        _auto_cleanup_on_failure(store, "d-vis", {})

    ours = [r for r in caplog.records if r.name == "app.step_handlers.status_update_step"]
    assert ours, (
        "the cleanup reported nothing at a level a deployed Lambda emits: a destructive "
        "pass over a customer account with no record of having run"
    )
    done = [r for r in ours if "Auto-cleanup completed" in r.getMessage()]
    assert done, f"no completion line survived production log filtering: {[r.getMessage() for r in ours]}"
    assert done[0].levelno >= logging.WARNING, (
        f"the completion ratio is emitted at {done[0].levelname}, which the deployed "
        "Lambda discards — the ratio cannot be read in production at all"
    )
    assert "1/1 resources" in done[0].getMessage()


def test_an_empty_manifest_says_so_at_a_visible_level(caplog):
    """The other branch. "The manifest was empty" and "the cleanup never ran" are the
    difference between *no* orphans and *unknown* orphans, and at info level they were
    the same thing in CloudWatch: nothing.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    state = MagicMock()
    state.model_dump.return_value = {"deployment_id": "d-empty", "created_resources": []}
    store = MagicMock()
    store.get.return_value = state

    _auto_cleanup_on_failure(store, "d-empty", {})

    ours = [r for r in caplog.records if r.name == "app.step_handlers.status_update_step"]
    assert [r.getMessage() for r in ours if "No created_resources" in r.getMessage()], (
        f"an empty manifest was reported at a level production drops: {[(r.levelname, r.getMessage()) for r in ours]}"
    )


# ---------------------------------------------------------------------------
# A 200 from delete_gateway is not proof the gateway is gone
# ---------------------------------------------------------------------------
#
# Measured live 2026-09-21: DeleteGateway answered 200 with status DELETING, and the
# service then made a forward-access-session call back out under the CALLER'S OWN
# credentials to remove the gateway's workload identity. That call was denied — after the
# API had already responded — so the gateway parked in FAILED and stayed in the account,
# while the cleanup, having seen a clean return, counted it cleaned. Nothing raised.
#
# An IAM grant fixes that instance. These tests fix the shape: they pin that the cleanup
# now VERIFIES the delete, so the next permission the service needs on our behalf produces
# a loud, attributable failure rather than a silent inflated count.


def _gateway_only_store():
    """A manifest holding exactly one gateway, so the ratio is unambiguous."""
    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-gw",
        "user_id": "owner-1",
        "target_account_id": "123456789012",
        "status": "failed",
        "created_resources": [{"type": "gateway", "id": "gw-verify-1", "name": "test-gw", "region": "us-east-1"}],
    }
    store = MagicMock()
    store.get.return_value = state
    return store


def _ctrl_for(get_gateway_side_effect):
    """A bedrock-agentcore-control double whose delete always succeeds.

    The delete succeeding is the whole point: this failure mode is invisible precisely
    because ``delete_gateway`` returns normally, so a double that raised would be testing
    a different bug.
    """
    ctrl = MagicMock()
    ctrl.list_gateway_targets.return_value = {"items": []}
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags("us-east-1")}
    remaining = list(get_gateway_side_effect) if isinstance(get_gateway_side_effect, list) else get_gateway_side_effect
    deleted = {"value": False}

    def _delete_gateway(**_kwargs):
        deleted["value"] = True
        return {"gatewayId": "gw-verify-1", "status": "DELETING"}

    ctrl.delete_gateway.side_effect = _delete_gateway

    def _get_gateway(**kwargs):
        # READY and ours until the delete: the ownership reads (the teardown name
        # hold's proof, F-66f, and the dispatcher's own) see the live gateway.
        if not deleted["value"]:
            return {
                "gatewayId": kwargs["gatewayIdentifier"],
                "name": "test-gw",
                "gatewayArn": (
                    "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/" + kwargs["gatewayIdentifier"]
                ),
                "status": "READY",
            }
        if isinstance(remaining, list):
            value = remaining.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if callable(remaining):
            return remaining(**kwargs)
        if isinstance(remaining, Exception):
            raise remaining
        return remaining

    ctrl.get_gateway.side_effect = _get_gateway
    return ctrl


def _run_gateway_cleanup(ctrl, caplog):
    """Run the real _auto_cleanup_on_failure over one gateway against *ctrl*.

    The budget is exercised on a fake clock rather than a real one: ``sleep`` advances
    ``monotonic`` by exactly the interval it was asked to wait. That keeps the arithmetic
    real — the loop still terminates by reaching the deadline, not by running out of
    canned responses — without adding the real 6s to the suite.

    A no-op ``sleep`` alone is NOT sufficient and is the trap this helper exists to avoid:
    with ``monotonic`` left real, the deadline never arrives, the loop spins until the
    mock's side-effect list is exhausted, and ``StopIteration`` lands in the read-failure
    branch. The test then passes or fails for a reason that has nothing to do with the
    budget.
    """
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    clock = {"t": 0.0}

    def _advance(seconds):
        clock["t"] += seconds

    with (
        patch("app.step_handlers.status_update_step.step_clients.client", return_value=ctrl),
        patch("app.step_handlers.status_update_step.time.monotonic", side_effect=lambda: clock["t"]),
        patch("app.step_handlers.status_update_step.time.sleep", side_effect=_advance),
    ):
        _auto_cleanup_on_failure(_gateway_only_store(), "d-gw", {})
    return [r for r in caplog.records if r.name == "app.step_handlers.status_update_step"]


def _ratio_line(records):
    return next((r for r in records if "Auto-cleanup completed" in r.getMessage()), None)


def test_a_gateway_that_parks_in_failed_is_not_counted_as_cleaned(caplog):
    """The regression this whole change exists for.

    Before the verification the sequence was: delete_gateway returns 200, no exception,
    ``cleaned += 1``, ratio reads 1/1. The gateway was still in the account. The ratio was
    not merely imprecise — it asserted the opposite of the truth, which is why it could
    never be used as teardown evidence.
    """
    ctrl = _ctrl_for(
        [
            {
                "status": "FAILED",
                "statusReasons": [
                    "Failed to delete gateway: not authorized to perform: bedrock-agentcore:DeleteWorkloadIdentity"
                ],
            },
            {
                "status": "FAILED",
                "statusReasons": [
                    "Failed to delete gateway: not authorized to perform: bedrock-agentcore:DeleteWorkloadIdentity"
                ],
            },
        ]
    )
    records = _run_gateway_cleanup(ctrl, caplog)

    ratio = _ratio_line(records)
    assert ratio is not None, "no completion line at all"
    assert "0/1 resources" in ratio.getMessage(), (
        f"a gateway that is still in the account was counted as cleaned: {ratio.getMessage()}"
    )

    # And the service's own reason has to survive to the log, because it is the only place
    # the missing action is ever named — nothing we raise ourselves knows it.
    loud = [r for r in records if r.levelno >= logging.ERROR and "did NOT delete" in r.getMessage()]
    assert loud, f"the failure was not reported at ERROR: {[(r.levelname, r.getMessage()) for r in records]}"
    assert "DeleteWorkloadIdentity" in loud[0].getMessage(), (
        f"the reason naming the missing permission was dropped: {loud[0].getMessage()}"
    )
    assert "gw-verify-1" in loud[0].getMessage(), "the log does not say WHICH gateway survived"


def test_a_gateway_that_is_actually_gone_is_counted(caplog):
    """The other half, and the reason it is a separate test.

    A verification step that reported every delete as unconfirmed would pass the test
    above while making the ratio useless in the normal case — the refusal-only-suite trap.
    Live, the delete converges in about 1.5s, so ``get_gateway`` raising not-found is the
    ordinary outcome and must count.
    """
    ctrl = _ctrl_for(Exception("ResourceNotFoundException: Failed to retrieve gateway because it doesn't exist"))
    records = _run_gateway_cleanup(ctrl, caplog)

    ratio = _ratio_line(records)
    assert ratio is not None and "1/1 resources" in ratio.getMessage(), (
        f"a confirmed-deleted gateway was not counted: {ratio and ratio.getMessage()}"
    )
    assert not [r for r in records if r.levelno >= logging.ERROR], (
        f"a successful cleanup logged an error: {[(r.levelname, r.getMessage()) for r in records]}"
    )


def test_a_gateway_still_deleting_at_the_budget_is_reported_unconfirmed(caplog):
    """The third outcome, which is neither success nor failure and is logged as such.

    DELETING is the state both the success and the failure path pass through, so when the
    budget expires there the honest answer is "unknown". Unknown is retained, not counted
    as cleaned, so the deployment record remains a durable retry handle.
    """
    # A callable rather than a finite list: a list long enough to "probably" cover the
    # budget would let exhaustion masquerade as the deadline, which is the failure this
    # test first produced. This gateway is DELETING forever, so only the budget can stop
    # the loop.
    #
    # The ceiling raises a BaseException on purpose. An unbounded confirmation loop is a
    # HANG, not a failure — a plain Exception would be absorbed by the read-failure
    # handler and a hang would stall the suite with no useful message. A BaseException
    # escapes every `except Exception` in the handler, so "the poll is not bounded"
    # arrives as a named, immediate failure.
    calls = {"n": 0}

    class _PollBudgetExceeded(BaseException):
        pass

    def _forever_deleting(**_kw):
        calls["n"] += 1
        if calls["n"] > 12:
            raise _PollBudgetExceeded(f"confirmation poll made {calls['n']} calls without stopping")
        return {"status": "DELETING"}

    ctrl = _ctrl_for(_forever_deleting)
    records = _run_gateway_cleanup(ctrl, caplog)

    unconfirmed = [r for r in records if "NOT confirmed" in r.getMessage()]
    assert unconfirmed, f"an unresolved delete was not flagged: {[r.getMessage() for r in records]}"
    assert unconfirmed[0].levelno >= logging.WARNING, (
        "the unconfirmed notice is below WARNING, so the deployed Lambda drops it"
    )
    # Bounded, not patient: the poll must stop rather than run until the Lambda times out.
    assert ctrl.get_gateway.call_count <= 9, (
        f"the confirmation poll is not bounded — it made {ctrl.get_gateway.call_count} calls"
    )
    ratio = _ratio_line(records)
    assert ratio is not None and "0/1 resources" in ratio.getMessage()


def test_an_unreadable_gateway_status_is_not_treated_as_a_leak(caplog):
    """A missing READ permission must not be reported as a surviving resource.

    If ``get_gateway`` cannot be called at all, the delete is unverified. That is not
    reported as a confirmed leak, but it also must not be counted as cleaned.
    """
    ctrl = _ctrl_for(Exception("AccessDeniedException: not authorized to perform: bedrock-agentcore:GetGateway"))
    records = _run_gateway_cleanup(ctrl, caplog)

    ratio = _ratio_line(records)
    assert ratio is not None and "0/1 resources" in ratio.getMessage(), (
        f"an unverifiable delete was counted as cleaned: {ratio and ratio.getMessage()}"
    )
    assert not [r for r in records if "did NOT delete" in r.getMessage()], (
        "an unreadable status was reported as a surviving resource"
    )
    assert [r for r in records if "could not be confirmed" in r.getMessage()], (
        f"the failed confirmation was not mentioned at all: {[r.getMessage() for r in records]}"
    )


def test_the_other_terminal_failure_spelling_is_also_caught(caplog):
    """``FAILED`` is not the only way this service family says a delete failed.

    A sibling AgentCore resource reports ``DELETE_FAILED`` with ``statusReasons`` null and
    the 403 in ``failureReason`` — a string, not a list. One missing permission, three
    differences in how the same service family expresses the same failure. An equality
    check on ``"FAILED"`` plus a read of ``statusReasons`` alone would miss the state AND
    drop the reason, so both are widened here.

    This is pinned even though only the gateway is polled today, because the cost of being
    wrong is a silent detection: an error that names a leaked resource but cannot say why
    is the dead end the original bug already created once.
    """
    ctrl = _ctrl_for(
        [
            {
                "status": "DELETE_FAILED",
                "statusReasons": None,
                "failureReason": "not authorized to perform: bedrock-agentcore:DeleteWorkloadIdentity",
            }
        ]
    )
    records = _run_gateway_cleanup(ctrl, caplog)

    ratio = _ratio_line(records)
    assert ratio is not None and "0/1 resources" in ratio.getMessage(), (
        f"DELETE_FAILED was not recognised as a failed delete: {ratio and ratio.getMessage()}"
    )
    loud = [r for r in records if r.levelno >= logging.ERROR and "did NOT delete" in r.getMessage()]
    assert loud, f"DELETE_FAILED was not reported at ERROR: {[(r.levelname, r.getMessage()) for r in records]}"
    assert "DeleteWorkloadIdentity" in loud[0].getMessage(), (
        f"the reason was in failureReason and was dropped: {loud[0].getMessage()}"
    )


def test_a_failure_with_no_reason_still_says_which_resource_survived(caplog):
    """The reason field can be empty, and the error must still be actionable.

    Without this, a service that reports the failure but populates neither field would
    produce an ERROR whose reason is the empty string — technically loud, operationally
    useless. The resource id and the status have to survive on their own.
    """
    ctrl = _ctrl_for([{"status": "FAILED"}])
    records = _run_gateway_cleanup(ctrl, caplog)

    loud = [r for r in records if r.levelno >= logging.ERROR and "did NOT delete" in r.getMessage()]
    assert loud, "a reasonless failure was not reported at all"
    msg = loud[0].getMessage()
    assert "gw-verify-1" in msg and "FAILED" in msg, f"the error is not actionable on its own: {msg}"
    assert not msg.rstrip().endswith(":"), f"the reason rendered as an empty string: {msg!r}"


# ---------------------------------------------------------------------------
# The two deliberate NON-deletions must be readable in a deployed Lambda
# ---------------------------------------------------------------------------
#
# Both of these lines report that cleanup decided, on purpose, not to delete something.
# That is precisely the outcome an operator cannot reconstruct from anything else: a
# resource left standing on purpose and a resource left standing by a bug look identical
# in the account. Both were raised from info to warning for that reason, and neither was
# pinned by a test — a peer's mutation run demoted each back to info and the whole suite
# stayed green. These close that.


def test_a_deliberately_undeleted_litellm_gateway_says_so_at_a_visible_level(caplog):
    """A LiteLLM gateway is the customer's own proxy, so cleanup correctly deletes nothing.

    "Nothing to delete" and "the delete never ran" are indistinguishable from the account
    afterwards, so this line is the only evidence the row was handled deliberately. At info
    it never reaches CloudWatch, because the deployed Lambda's root logger sits at WARNING.
    """
    from app.step_handlers.status_update_step import _cleanup_resource

    with caplog.at_level(logging.DEBUG, logger="app.step_handlers.status_update_step"):
        _cleanup_resource({"type": "litellm_gateway", "id": "https://proxy.example.invalid"}, "us-east-1", {})

    ours = [r for r in caplog.records if r.name == "app.step_handlers.status_update_step"]
    kept = [r for r in ours if "nothing to delete" in r.getMessage()]
    assert kept, f"the deliberate non-deletion was not logged at all: {[r.getMessage() for r in ours]}"
    assert kept[0].levelno >= logging.WARNING, (
        f"logged at {kept[0].levelname}, which the deployed Lambda discards — the only record "
        "that this row was handled on purpose would be invisible"
    )


def test_the_shared_tool_lambda_release_decision_is_visible_in_a_deployed_lambda(caplog):
    """The shared-tool release has three outcomes and one log line to tell them apart.

    Deleted, kept because another live gateway still holds an invoke grant, or kept because
    ownership could not be proven. Getting this wrong in production means a shared Lambda
    that other gateways still invoke is either deleted (they serve zero tools) or retained
    (it bills forever) with no way to tell which happened. The "kept" branch is used here
    because it is the one that leaves a resource behind.
    """
    import json as _json

    from app.step_handlers.status_update_step import (
        _cleanup_resource,
        _ResourceRetained,
    )

    lam = MagicMock()
    # Another gateway's invoke grant survives, so the refcount is non-zero and the function
    # must be KEPT. A delete here would be the live "tear down A and B goes dead" failure.
    lam.get_policy.return_value = {
        "Policy": _json.dumps({"Statement": [{"Sid": "AllowAgentCoreInvoke-SomeOtherGatewayRole"}]})
    }

    with (
        patch("app.step_handlers.status_update_step.step_clients.client", return_value=lam),
        caplog.at_level(logging.DEBUG, logger="app.step_handlers.status_update_step"),
    ):
        with pytest.raises(_ResourceRetained, match="kept") as retained:
            _cleanup_resource(
                {
                    "type": "lambda",
                    "name": "AgentCoreDynamicTools",
                    "gateway_role": "AgentCoreGateway-x",
                },
                "us-east-1",
                {},
            )

    lam.delete_function.assert_not_called()
    assert "Shared tool Lambda" in retained.value.reason


# The same family again, and these two were NOT reported — they were found by extending a
# peer's mutation run past the mutants it wrote. Both Cognito refusals demote from warning
# to info with the whole suite green, so they had the identical defect as the two above.
#
# These are the worst two in the group to lose. A user pool holds the app client of EVERY
# gateway deployed against the platform, so "refused to delete" and "never tried" differ by
# whether every other agent still has gateway access. The account cannot answer which
# happened afterwards: a surviving pool looks the same either way.


def test_refusing_to_delete_the_shared_gateway_auth_pool_says_so_at_a_visible_level(caplog, monkeypatch):
    """The one pool that must never be deleted must say out loud that it was spared.

    ``is_platform_owned_user_pool`` is a pure env/id comparison, so this refusal happens
    before any client is constructed — which is the point, but it also means there is no
    API call anywhere to infer the decision from. The log line is the only artifact.
    """
    from app.step_handlers.status_update_step import (
        _cleanup_resource,
        _ResourceRetained,
    )

    shared_pool = "us-east-1_sharedAuth1"
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", shared_pool)

    made_a_client = MagicMock()
    with (
        patch("app.step_handlers.status_update_step.step_clients.client", made_a_client),
        caplog.at_level(logging.DEBUG, logger="app.step_handlers.status_update_step"),
    ):
        with pytest.raises(_ResourceRetained, match="shared platform gateway-auth pool") as retained:
            _cleanup_resource(
                {"type": "cognito_user_pool", "id": shared_pool},
                "us-east-1",
                {},
            )

    # The guard has to win before any client exists, or a broken classifier could still
    # reach the shared pool.
    made_a_client.assert_not_called()

    assert retained.value.rid == shared_pool


def test_an_unprovable_pool_is_refused_at_a_visible_level(caplog, monkeypatch):
    """A pool whose ownership cannot be proven is left standing, and that must be loud.

    This is the branch that leaves a stranger's pool alone. It is also the branch that
    fires when tag reads fail for an operational reason, which is a genuine cleanup gap
    an operator needs to see — at info it is indistinguishable from a clean teardown.
    """
    from app.services.gateway_deployer import POOL_FOREIGN_OR_UNKNOWN
    from app.step_handlers.status_update_step import (
        _cleanup_resource,
        _ResourceRetained,
    )

    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", "us-east-1_somethingElse")
    cog = MagicMock()

    with (
        patch("app.step_handlers.status_update_step.step_clients.client", return_value=cog),
        patch(
            "app.services.gateway_deployer.classify_user_pool",
            return_value=POOL_FOREIGN_OR_UNKNOWN,
        ),
        caplog.at_level(logging.DEBUG, logger="app.step_handlers.status_update_step"),
    ):
        with pytest.raises(_ResourceRetained, match="ownership could not be proven"):
            _cleanup_resource(
                {"type": "cognito_user_pool", "id": "us-east-1_notOurs99"},
                "us-east-1",
                {},
            )

    # Zero mutating calls against a pool we could not prove is ours.
    cog.delete_user_pool.assert_not_called()
    cog.delete_user_pool_domain.assert_not_called()


def test_a_pool_this_deployment_owns_is_still_deleted(caplog, monkeypatch):
    """The positive control for the two refusals above.

    Without it, both refusal tests are satisfied by a branch that refuses every pool —
    which would silently retire the feature and leak a pool per deployment. This is the
    same trap as the refusal-only suite that hid a dead happy path.
    """
    from app.services.gateway_deployer import POOL_OWNED_BY_STACK
    from app.step_handlers.status_update_step import _cleanup_resource

    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", "us-east-1_somethingElse")
    cog = MagicMock()
    cog.describe_user_pool.return_value = {"UserPool": {}}  # no domain to remove

    with (
        patch("app.step_handlers.status_update_step.step_clients.client", return_value=cog),
        patch(
            "app.services.gateway_deployer.classify_user_pool",
            return_value=POOL_OWNED_BY_STACK,
        ),
        caplog.at_level(logging.DEBUG, logger="app.step_handlers.status_update_step"),
    ):
        _cleanup_resource({"type": "cognito_user_pool", "id": "us-east-1_ours0001"}, "us-east-1", {})

    cog.delete_user_pool.assert_called_once()
    assert cog.delete_user_pool.call_args.kwargs["UserPoolId"] == "us-east-1_ours0001"


# ---------------------------------------------------------------------------
# F-29b — the other half of the same silence. F-29 made the FAILURE path loud
# about a type it cannot delete (`_NoDeleterFor`, logged, not counted) and left
# the user-initiated DELETE path silent: `_delete_managed_resource` returned ""
# for an unknown type and the caller does `if _msg: cleanup_messages.append(_msg)`,
# so the row landed in neither `cleanup_messages` nor `cleanup_failures` and the
# teardown reported success without ever mentioning the resource it skipped.
# The two dispatchers agreed about which types they handle (there is a test for
# that) and disagreed about honesty, which is the harder kind of drift to notice.
# ---------------------------------------------------------------------------


def test_the_delete_path_says_out_loud_that_it_skipped_an_unknown_type(caplog):
    """An unrecognised manifest row must produce a message, not an empty string.

    Deliberately still a no-op rather than a raise: failing the whole teardown over
    one stale row would strand every resource ordered after it. The requirement is
    that the operator is told, which is the part that was missing.
    """
    from app import deployment_handler as dh

    with caplog.at_level(logging.ERROR, logger="app.deployment_handler"):
        msg = dh._delete_managed_resource(
            {"type": "something_invented_later", "id": "x-1", "region": "us-east-1"},
            "us-east-1",
        )

    # The caller appends only a truthy message, so "" is indistinguishable from
    # "there was nothing to report" -- that is the whole defect.
    assert msg, "an unknown type must not return an empty string"
    assert "something_invented_later" in msg
    assert "x-1" in msg
    assert "SKIPPED" in msg
    # Say where the resource actually is, not just that a step was skipped.
    assert "still in the account" in msg
    assert any(r.levelno >= logging.ERROR and "NO deleter" in r.getMessage() for r in caplog.records), (
        f"expected an ERROR naming the missing deleter; got {[r.getMessage() for r in caplog.records]}"
    )


def test_both_teardown_dispatchers_are_loud_about_a_type_they_cannot_delete():
    """Parity of *honesty*, to sit beside the existing parity of handled types.

    Asserted against the source of both arms rather than by calling them, because the
    failure path signals by raising and the delete path by returning a string: there is
    no single call whose result compares the two. What must stay true is that neither
    arm's unknown-type branch is a bare, silent return.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    delete_path = (root / "deployment_handler.py").read_text()
    failure_path = (root / "step_handlers" / "status_update_step.py").read_text()

    assert 'return ""\n    except Exception as e:' not in delete_path, (
        'the delete path\'s unknown-type branch is a bare `return ""` again -- an '
        "unrecognised manifest row would be skipped without telling anyone"
    )
    assert "NO deleter" in delete_path, "the delete path must log the missing deleter"
    assert "_NoDeleterFor" in failure_path, "the failure path must still raise _NoDeleterFor"


# --------------------------------------------------------------------------- online_evaluation_config arm


def _eval_cleanup(monkeypatch, *, get_side_effect, delete_side_effect=None):
    import app.step_handlers.status_update_step as sus

    ctrl = MagicMock()
    ctrl.get_online_evaluation_config.side_effect = get_side_effect
    if delete_side_effect is not None:
        ctrl.delete_online_evaluation_config.side_effect = delete_side_effect
    logs = MagicMock()
    monkeypatch.setattr(
        sus.step_clients,
        "client",
        lambda event, service, **_k: {"bedrock-agentcore-control": ctrl, "logs": logs}[service],
    )
    monkeypatch.setattr(sus.time, "sleep", lambda *_a: None, raising=False)
    return sus, ctrl, logs


def test_the_failure_path_deletes_an_evaluation_config_by_exact_id_and_its_result_log_group(monkeypatch):
    sus, ctrl, logs = _eval_cleanup(monkeypatch, get_side_effect=RuntimeError("ResourceNotFoundException: not found"))
    sus._cleanup_resource(
        {"type": "online_evaluation_config", "id": "cfg-1", "name": "eval_x", "region": "us-east-1"}, "us-east-1", {}
    )
    ctrl.delete_online_evaluation_config.assert_called_once_with(onlineEvaluationConfigId="cfg-1")
    ctrl.get_online_evaluation_config.assert_called_with(onlineEvaluationConfigId="cfg-1")
    logs.delete_log_group.assert_called_once_with(logGroupName="/aws/bedrock-agentcore/evaluations/results/cfg-1")


def test_the_failure_path_treats_an_already_gone_config_as_cleaned(monkeypatch):
    sus, ctrl, logs = _eval_cleanup(
        monkeypatch,
        get_side_effect=RuntimeError("ResourceNotFoundException: not found"),
        delete_side_effect=RuntimeError("ResourceNotFoundException: config does not exist"),
    )
    sus._cleanup_resource({"type": "online_evaluation_config", "id": "cfg-1", "region": "us-east-1"}, "us-east-1", {})
    logs.delete_log_group.assert_called_once()


def test_the_failure_path_does_not_report_a_config_deleted_while_it_still_reads(monkeypatch):
    import app.services.deletion_confirmation as dc
    from app.services.resource_ownership import ResourceDeletionRefused

    monkeypatch.setattr(dc.time, "sleep", lambda *_a: None)
    sus, ctrl, logs = _eval_cleanup(monkeypatch, get_side_effect=None)
    ctrl.get_online_evaluation_config.return_value = {"onlineEvaluationConfigId": "cfg-1", "status": "DELETING"}
    with pytest.raises(ResourceDeletionRefused):
        sus._cleanup_resource(
            {"type": "online_evaluation_config", "id": "cfg-1", "region": "us-east-1"}, "us-east-1", {}
        )
    logs.delete_log_group.assert_not_called()
