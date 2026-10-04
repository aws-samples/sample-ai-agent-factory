"""Matrix-run defect fixes (multi-target / multi-gateway hardening).

Covers the three code-path defects the exhaustive matrix tester surfaced live:

* Defect B — an OpenAPI target in the multi-target ``targets[]`` path must NOT
  use ``GATEWAY_IAM_ROLE`` (AgentCore rejects it: "IamCredentialProvider is
  required for openApiSchema targets"). A public spec omits the credential block;
  api_key / oauth are honored when supplied.
* Defect C — a SHARED singleton tool Lambda (AgentCoreDynamicTools /
  AgentCoreCustomerSupportTools) must be released by REFERENCE COUNT on teardown:
  remove only this gateway's invoke statement; delete the function only when no
  other gateway's ``AllowAgentCoreInvoke-*`` statement remains.
* Defect A (regression guard) — the orphan-permission prune must surface, not
  silently swallow, an AccessDenied on ``lambda:GetPolicy`` (which would leave it
  inert and re-brick reused Lambdas).

Pure unit tests — all AWS clients are MagicMocks.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.services import gateway_deployer as gd
from app.services.resource_ownership import OWNER_TAG_KEY, stack_id
from botocore.exceptions import ClientError

REGION = "us-east-1"


def _client_error(code, msg, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


def _policy(*sids):
    return json.dumps({"Statement": [{"Sid": s, "Effect": "Allow"} for s in sids]})


def _own(lam, name, region=REGION):
    """Make *lam* report *name* as a function THIS deployment owns.

    Required by every test below that expects a delete: the refcount is no longer the
    only gate, because a refcount of zero only means no gateway of ours still needs the
    function, not that the function is ours (F-7c). A real function of ours carries these
    tags from ``create_function(Tags=...)``, so this fixture makes the mock match
    production rather than relaxing the assertion.
    """
    lam.get_function.return_value = {
        "Configuration": {
            "FunctionArn": f"arn:aws:lambda:{region}:123456789012:function:{name}",
            "State": "Active",
        }
    }
    lam.list_tags.return_value = {"Tags": {OWNER_TAG_KEY: stack_id(region)}}
    return lam


def _own_gateway(ctrl, gateway_id="gw-1", region=REGION):
    detail = {"gatewayArn": (f"arn:aws:bedrock-agentcore:{region}:123456789012:gateway/{gateway_id}")}
    # Ownership is read twice, to decide and then under the write lock (F-66e);
    # then the delete's proof of absence reads it gone.
    ctrl.get_gateway.side_effect = [
        detail,
        detail,
        _client_error("ResourceNotFoundException", "gateway is gone", "GetGateway"),
    ]
    ctrl.list_tags_for_resource.return_value = {"tags": {OWNER_TAG_KEY: stack_id(region)}}
    return ctrl


# ---------------------------------------------------------------------------
# Defect B — OpenAPI target credential provider
# ---------------------------------------------------------------------------


def test_openapi_public_spec_has_no_credential_block():
    """A public OpenAPI target returns None => the deploy omits the cred block
    entirely (NOT GATEWAY_IAM_ROLE, which AgentCore rejects)."""
    assert gd._openapi_target_cred_config(MagicMock(), {"type": "openapi"}, "gw-openapi-0") is None
    assert gd._openapi_target_cred_config(MagicMock(), {"type": "openapi", "authType": "none"}, "x") is None


def test_openapi_never_emits_gateway_iam_role():
    """Whatever the auth, the openapi cred config must never be GATEWAY_IAM_ROLE."""
    for target in (
        {"type": "openapi"},
        {"type": "openapi", "authType": "api_key"},  # no secret -> falls back to public
        {"type": "openapi", "authType": "oauth2_client_credentials"},  # no provider -> public
    ):
        cfg = gd._openapi_target_cred_config(MagicMock(), target, "gw-openapi-0")
        assert cfg is None or cfg.get("credentialProviderType") != "GATEWAY_IAM_ROLE"


def test_openapi_api_key_builds_api_key_provider():
    ctrl = MagicMock()
    with patch.object(gd, "_ensure_api_key_credential_provider", return_value="arn:prov:apikey") as mk:
        cfg = gd._openapi_target_cred_config(
            ctrl,
            {"type": "openapi", "authType": "api_key", "secretArn": "arn:secret:x"},
            "gw-openapi-1",
        )
    mk.assert_called_once()
    assert cfg["credentialProviderType"] == "API_KEY"
    assert cfg["credentialProvider"]["apiKeyCredentialProvider"]["providerArn"] == "arn:prov:apikey"


def test_openapi_oauth_builds_oauth_provider():
    cfg = gd._openapi_target_cred_config(
        MagicMock(),
        {"type": "openapi", "authType": "oauth2_client_credentials", "oauthProviderArn": "arn:prov:oauth"},
        "gw-openapi-2",
    )
    assert cfg["credentialProviderType"] == "OAUTH"
    assert cfg["credentialProvider"]["oauthCredentialProvider"]["providerArn"] == "arn:prov:oauth"


# ---------------------------------------------------------------------------
# Defect C — reference-counted release of a shared tool Lambda
# ---------------------------------------------------------------------------


def test_shared_lambda_kept_when_other_gateway_grant_remains():
    """Releasing gateway A must NOT delete the Lambda while gateway B's invoke
    grant is still on the policy."""
    # Owned by this deployment: since F-7d the release proves ownership BEFORE it touches
    # the resource policy, so an unowned function would be kept untouched instead.
    lam = _own(MagicMock(), "AgentCoreDynamicTools")
    # After A's statement is removed, B's grant still remains.
    lam.get_policy.return_value = {"Policy": _policy("AllowAgentCoreInvoke-AgentCoreGateway-B")}
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A", REGION)
    lam.remove_permission.assert_called_once_with(
        FunctionName="AgentCoreDynamicTools",
        StatementId="AllowAgentCoreInvoke-AgentCoreGateway-A",
    )
    order = [c[0] for c in lam.mock_calls]
    assert order.index("list_tags") < order.index("remove_permission"), "ownership before the policy mutation"
    lam.delete_function.assert_not_called()
    assert "kept" in msg


def test_shared_lambda_deleted_when_last_gateway_releases():
    """When no invoke grants remain, the shared Lambda is finally deleted."""
    lam = _own(MagicMock(), "AgentCoreDynamicTools")
    lam.get_policy.return_value = {"Policy": _policy()}  # no statements left
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A", REGION)
    lam.delete_function.assert_called_once_with(FunctionName="AgentCoreDynamicTools")
    assert "deleted" in msg


def test_shared_lambda_deleted_when_policy_becomes_empty():
    """Refcount-zero live shape: removing the LAST grant leaves the function
    with NO resource policy, and GetPolicy then raises ResourceNotFoundException
    — the same code AWS uses for a missing function. The helper must
    disambiguate via get_function and still DELETE (verified live: the shared
    Lambda leaked as Active while teardown said 'already absent')."""
    lam = _own(MagicMock(), "AgentCoreDynamicTools")  # function EXISTS, and is ours
    lam.get_policy.side_effect = _client_error("ResourceNotFoundException", "no policy", "GetPolicy")
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-B", REGION)
    lam.delete_function.assert_called_once_with(FunctionName="AgentCoreDynamicTools")
    assert "deleted" in msg


def test_shared_lambda_absent_when_function_gone_too():
    """When GetPolicy 404s AND the function itself is gone, report absent."""
    lam = MagicMock()
    lam.get_policy.side_effect = _client_error("ResourceNotFoundException", "no fn", "GetPolicy")
    lam.get_function.side_effect = _client_error("ResourceNotFoundException", "no fn", "GetFunction")
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-B", REGION)
    lam.delete_function.assert_not_called()
    assert "already absent" in msg


def test_shared_lambda_not_deleted_when_policy_unreadable():
    """If GetPolicy is denied we must NOT risk deleting a Lambda other gateways
    may still need."""
    lam = MagicMock()
    lam.get_policy.side_effect = _client_error("AccessDeniedException", "no getpolicy", "GetPolicy")
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A", REGION)
    lam.delete_function.assert_not_called()
    assert "kept" in msg


def test_cleanup_uses_refcount_release_for_shared_lambda():
    """cleanup_gateway_resources routes a SHARED tool Lambda through the
    ref-counted release, not an unconditional delete_function."""
    lam = MagicMock()
    ctrl = _own_gateway(MagicMock())
    ctrl.list_gateway_targets.return_value = {"items": []}
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_release_shared_tool_lambda", return_value="released") as rel,
        patch.object(gd, "time", MagicMock()),
    ):
        gd.cleanup_gateway_resources(
            "rt-x",
            "us-east-1",
            {
                "gateway_id": "gw-1",
                "gateway_name": "mygw",
                "lambda_function_name": "AgentCoreDynamicTools",
            },
        )
    # The region is threaded, and that is load-bearing rather than cosmetic: the release
    # now reads the function's ownership tag, and stack_id embeds the region, so passing
    # None here would have the teardown ask about whatever region the process happens to
    # default to and read its own function as another deployment's.
    rel.assert_called_once_with(lam, "AgentCoreDynamicTools", "AgentCoreGateway-mygw", "us-east-1")
    # The shared Lambda must NOT be hard-deleted directly.
    lam.delete_function.assert_not_called()


def test_cleanup_hard_deletes_non_shared_lambda():
    """A per-gateway (non-shared) Lambda is still deleted outright — when it is ours."""
    lam = _own(MagicMock(), "AgentCoreKBQuery-mygw")
    ctrl = _own_gateway(MagicMock())
    ctrl.list_gateway_targets.return_value = {"items": []}
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_release_shared_tool_lambda") as rel,
        patch.object(gd, "time", MagicMock()),
    ):
        gd.cleanup_gateway_resources(
            "rt-x",
            "us-east-1",
            {
                "gateway_id": "gw-1",
                "gateway_name": "mygw",
                # A per-gateway name. This used to read "AgentCoreLambdaTestFunction",
                # which is the PLACEHOLDER for "this deploy created no tool Lambda" —
                # so the test pinned the F-7 defect it was meant to be neutral about.
                # The behaviour under test is "not shared => deleted outright".
                "lambda_function_name": "AgentCoreKBQuery-mygw",
            },
        )
    rel.assert_not_called()
    lam.delete_function.assert_called_once_with(FunctionName="AgentCoreKBQuery-mygw")


def test_cleanup_deletes_no_lambda_when_the_deploy_created_none():
    """The placeholder must never become a delete.

    ``lambda_function_name`` is "" for a gateway whose targets are all config-driven
    (the multi-target feature builds no tool Lambda). It used to default to the literal
    ``AgentCoreLambdaTestFunction`` here and at the deploy's own abort path, so an
    unrelated function of that name — anywhere in the account — was a delete target for
    a deploy that never created it. Confirmed live in CloudTrail on 2026-09-21: two
    DeleteFunction calls under two different step roles, both
    ResourceNotFoundException.
    """
    lam = MagicMock()
    ctrl = _own_gateway(MagicMock())
    ctrl.list_gateway_targets.return_value = {"items": []}
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_release_shared_tool_lambda") as rel,
        patch.object(gd, "time", MagicMock()),
    ):
        log = gd.cleanup_gateway_resources(
            "rt-x",
            "us-east-1",
            {"gateway_id": "gw-1", "gateway_name": "mygw", "lambda_function_name": ""},
        )
    lam.delete_function.assert_not_called()
    rel.assert_not_called()
    assert any("nothing to delete" in line for line in log), log


def test_cleanup_deletes_no_lambda_when_the_field_is_absent_entirely():
    """Same guarantee through the other door: a config with no ``lambda_function_name``
    key at all (every LiteLLM gateway, and any caller built before the field existed).
    A ``.get(key, default)`` here is what made the absent case indistinguishable from a
    real function name."""
    lam = MagicMock()
    ctrl = _own_gateway(MagicMock())
    ctrl.list_gateway_targets.return_value = {"items": []}
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_release_shared_tool_lambda") as rel,
        patch.object(gd, "time", MagicMock()),
    ):
        gd.cleanup_gateway_resources("rt-x", "us-east-1", {"gateway_id": "gw-1", "gateway_name": "mygw"})
    lam.delete_function.assert_not_called()
    rel.assert_not_called()


# ---------------------------------------------------------------------------
# Defect A (regression guard) — prune must not silently swallow AccessDenied
# ---------------------------------------------------------------------------


def test_prune_warns_on_access_denied_getpolicy(caplog):
    """AccessDenied on GetPolicy => prune is inert; it must WARN (not be silent),
    so a missing lambda:GetPolicy permission is diagnosable."""
    lam = MagicMock()
    lam.get_policy.side_effect = _client_error("AccessDeniedException", "denied", "GetPolicy")
    with patch.object(gd, "_create_iam_client", return_value=MagicMock()):
        import logging

        with caplog.at_level(logging.WARNING):
            pruned = gd._prune_orphaned_lambda_permissions(lam, "AgentCoreDynamicTools")
    assert pruned == 0
    assert any("GetPolicy" in r.message for r in caplog.records)


def test_prune_silent_on_resource_not_found():
    """A genuine 'no policy yet' (ResourceNotFound) is benign and must NOT warn."""
    lam = MagicMock()
    lam.get_policy.side_effect = _client_error("ResourceNotFoundException", "no policy", "GetPolicy")
    with patch.object(gd, "_create_iam_client", return_value=MagicMock()):
        assert gd._prune_orphaned_lambda_permissions(lam, "fn") == 0


# ---------------------------------------------------------------------------
# Defect C — MANIFEST teardown paths (the live re-verification caught the
# shared Lambda still being hard-deleted via created_resources[], which
# bypasses cleanup_gateway_resources entirely)
# ---------------------------------------------------------------------------


def test_manifest_teardown_refcounts_shared_lambda():
    """deployment_handler._delete_managed_resource must route a shared tool
    Lambda through the ref-counted release, passing the recorded gateway_role.

    NOTE: _delete_managed_resource shadows boto3 with a cross-account shim that
    resolves clients via step_clients.client — that is the seam to patch.
    """
    import app.deployment_handler as dh

    lam = MagicMock()
    with (
        patch("app.services.step_clients.client", return_value=lam),
        patch.object(dh, "_release_shared_tool_lambda", return_value="released x") as rel,
    ):
        msg = dh._delete_managed_resource(
            {
                "type": "lambda",
                "name": "AgentCoreDynamicTools",
                "gateway_role": "AgentCoreGateway-gwA",
                "region": "us-east-1",
            },
            "us-east-1",
        )
    rel.assert_called_once_with(lam, "AgentCoreDynamicTools", "AgentCoreGateway-gwA", "us-east-1")
    lam.delete_function.assert_not_called()
    assert "released x" in msg


def test_manifest_teardown_hard_deletes_non_shared_lambda():
    """A non-shared per-deploy Lambda is still hard-deleted. The dispatcher
    resolves clients through step_clients (its local boto3 shim), so that is
    the seam to patch."""
    import app.deployment_handler as dh

    lam = _own(MagicMock(), "AgentCore-KBTool-abc12345")
    with (
        patch("app.services.step_clients.client", return_value=lam),
        patch.object(dh, "_release_shared_tool_lambda") as rel,
    ):
        msg = dh._delete_managed_resource(
            {"type": "lambda", "name": "AgentCore-KBTool-abc12345", "region": "us-east-1"},
            "us-east-1",
        )
    rel.assert_not_called()
    lam.delete_function.assert_called_once_with(FunctionName="AgentCore-KBTool-abc12345")
    assert "deleted" in msg


def test_failure_path_manifest_teardown_refcounts_shared_lambda():
    """status_update_step._cleanup_resource (failure-path auto-cleanup) must
    also ref-count the shared Lambda, not hard-delete it."""
    from app.step_handlers import status_update_step as sus

    lam = MagicMock()
    with (
        patch.object(sus.step_clients, "client", return_value=lam),
        patch.object(sus, "_release_shared_tool_lambda", return_value="released y") as rel,
    ):
        sus._cleanup_resource(
            {
                "type": "lambda",
                "name": "AgentCoreCustomerSupportTools",
                "gateway_role": "AgentCoreGateway-gwB",
                "region": "us-east-1",
            },
            "us-east-1",
            {},
        )
    rel.assert_called_once_with(lam, "AgentCoreCustomerSupportTools", "AgentCoreGateway-gwB", "us-east-1")
    lam.delete_function.assert_not_called()


def test_gateway_step_records_gateway_role_for_shared_lambda():
    """The manifest entry for a shared tool Lambda must carry gateway_role so
    teardown can drop the right invoke grant."""
    from app.step_handlers import gateway_step as gs

    store = MagicMock()
    gs._record_gateway_resources(
        store,
        "dep-1",
        "us-east-1",
        {
            "gateway_id": "gw-1",
            "gateway_name": "mygw",
            "lambda_function_name": "AgentCoreDynamicTools",
            "client_info": {},
        },
    )
    lambda_entries = [c.args[1] for c in store.record_resource.call_args_list if c.args[1].get("type") == "lambda"]
    assert lambda_entries, "no lambda manifest entry recorded"
    assert lambda_entries[0]["gateway_role"] == "AgentCoreGateway-mygw"
