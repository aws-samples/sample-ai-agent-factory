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
from botocore.exceptions import ClientError


def _client_error(code, msg, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


def _policy(*sids):
    return json.dumps({"Statement": [{"Sid": s, "Effect": "Allow"} for s in sids]})


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
    lam = MagicMock()
    # After A's statement is removed, B's grant still remains.
    lam.get_policy.return_value = {"Policy": _policy("AllowAgentCoreInvoke-AgentCoreGateway-B")}
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A")
    lam.remove_permission.assert_called_once_with(
        FunctionName="AgentCoreDynamicTools",
        StatementId="AllowAgentCoreInvoke-AgentCoreGateway-A",
    )
    lam.delete_function.assert_not_called()
    assert "kept" in msg


def test_shared_lambda_deleted_when_last_gateway_releases():
    """When no invoke grants remain, the shared Lambda is finally deleted."""
    lam = MagicMock()
    lam.get_policy.return_value = {"Policy": _policy()}  # no statements left
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A")
    lam.delete_function.assert_called_once_with(FunctionName="AgentCoreDynamicTools")
    assert "deleted" in msg


def test_shared_lambda_deleted_when_policy_becomes_empty():
    """Refcount-zero live shape: removing the LAST grant leaves the function
    with NO resource policy, and GetPolicy then raises ResourceNotFoundException
    — the same code AWS uses for a missing function. The helper must
    disambiguate via get_function and still DELETE (verified live: the shared
    Lambda leaked as Active while teardown said 'already absent')."""
    lam = MagicMock()
    lam.get_policy.side_effect = _client_error("ResourceNotFoundException", "no policy", "GetPolicy")
    lam.get_function.return_value = {"Configuration": {"State": "Active"}}  # function EXISTS
    with (
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
    ):
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-B")
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
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-B")
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
        msg = gd._release_shared_tool_lambda(lam, "AgentCoreDynamicTools", "AgentCoreGateway-A")
    lam.delete_function.assert_not_called()
    assert "kept" in msg


def test_cleanup_uses_refcount_release_for_shared_lambda():
    """cleanup_gateway_resources routes a SHARED tool Lambda through the
    ref-counted release, not an unconditional delete_function."""
    lam = MagicMock()
    ctrl = MagicMock()
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
    rel.assert_called_once_with(lam, "AgentCoreDynamicTools", "AgentCoreGateway-mygw")
    # The shared Lambda must NOT be hard-deleted directly.
    lam.delete_function.assert_not_called()


def test_cleanup_hard_deletes_non_shared_lambda():
    """A per-gateway (non-shared) Lambda is still deleted outright."""
    lam = MagicMock()
    ctrl = MagicMock()
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
                "lambda_function_name": "AgentCoreLambdaTestFunction",
            },
        )
    rel.assert_not_called()
    lam.delete_function.assert_called_once_with(FunctionName="AgentCoreLambdaTestFunction")


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
    rel.assert_called_once_with(lam, "AgentCoreDynamicTools", "AgentCoreGateway-gwA")
    lam.delete_function.assert_not_called()
    assert "released x" in msg


def test_manifest_teardown_hard_deletes_non_shared_lambda():
    """A non-shared per-deploy Lambda is still hard-deleted. The dispatcher
    resolves clients through step_clients (its local boto3 shim), so that is
    the seam to patch."""
    import app.deployment_handler as dh

    lam = MagicMock()
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
    rel.assert_called_once_with(lam, "AgentCoreCustomerSupportTools", "AgentCoreGateway-gwB")
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
