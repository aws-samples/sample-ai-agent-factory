"""Mixed gateway targets — one gateway, many target families.

Exercises ``gateway_deployer._deploy_config_targets`` (the multi-target
counterpart to ``_deploy_connector_targets`` / ``_deploy_external_mcp_targets``):
lambda + openapi + smithy entries deployed as N distinct gateway targets on the
SAME gateway, each with a unique name. boto3 is fully mocked (MagicMock control
client + patched Lambda/spec helpers), following the test_connectors.py style.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")


def _fake_ctrl() -> MagicMock:
    ctrl = MagicMock()
    ctrl.create_gateway_target.return_value = {"targetId": "tgt-1"}
    ctrl.get_gateway_target.return_value = {"status": "READY"}
    return ctrl


def test_deploy_config_targets_mixed_families_one_call_per_target():
    """A lambda + openapi + smithy list creates exactly 3 gateway targets on the
    SAME gateway, each with a distinct name and the correct targetConfiguration
    family key."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [
        {"type": "lambda", "function_arn": "arn:aws:lambda:us-west-2:123456789012:function:my-fn"},
        {"type": "openapi", "spec_content": '{"openapi": "3.0.0"}'},
        {"type": "smithy", "model_content": '{"smithy": "2.0"}'},
    ]

    with patch.object(gd, "_grant_gateway_invoke_on_lambda") as grant:
        result = gd._deploy_config_targets(
            ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="arn:aws:iam::1:role/AgentCoreGateway-gw"
        )

    # One create call per target, all on the same gateway.
    assert ctrl.create_gateway_target.call_count == 3
    calls = ctrl.create_gateway_target.call_args_list
    for c in calls:
        assert c.kwargs["gatewayIdentifier"] == "gw-1"

    # Distinct names.
    names = [c.kwargs["name"] for c in calls]
    assert len(names) == len(set(names)) == 3
    assert result["target_names"] == names

    # Correct family shapes.
    mcp_cfgs = [c.kwargs["targetConfiguration"]["mcp"] for c in calls]
    assert "lambda" in mcp_cfgs[0]
    assert mcp_cfgs[0]["lambda"]["lambdaArn"] == "arn:aws:lambda:us-west-2:123456789012:function:my-fn"
    assert "toolSchema" in mcp_cfgs[0]["lambda"]  # required by AgentCore
    assert "openApiSchema" in mcp_cfgs[1]
    assert mcp_cfgs[1]["openApiSchema"]["inlinePayload"] == '{"openapi": "3.0.0"}'
    assert "smithyModel" in mcp_cfgs[2]
    assert mcp_cfgs[2]["smithyModel"]["inlinePayload"] == '{"smithy": "2.0"}'

    # The user-supplied Lambda ARN got a gateway-role invoke grant.
    grant.assert_called_once()


def test_deploy_config_targets_two_lambdas_get_unique_names():
    """Two lambda targets on one gateway must NOT collide on target name."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [
        {"type": "lambda", "function_arn": "arn:aws:lambda:us-west-2:123456789012:function:a"},
        {"type": "lambda", "function_arn": "arn:aws:lambda:us-west-2:123456789012:function:b"},
    ]

    with patch.object(gd, "_grant_gateway_invoke_on_lambda"):
        gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="")

    names = [c.kwargs["name"] for c in ctrl.create_gateway_target.call_args_list]
    assert len(names) == 2
    assert names[0] != names[1]


def test_deploy_config_targets_skips_mcp_and_unknown_families():
    """mcp_server entries (handled elsewhere) and unknown families are skipped.

    Only these two are skipped. An mcp_server entry is deployed by
    ``_deploy_external_mcp_targets`` (secret hygiene + SSRF validation live there), so
    acting on it here would double-deploy; an unknown family is forward compatibility.
    A *known* family missing its payload is fatal — see the tests below.
    """
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [
        {"type": "mcp_server", "server_id": "aws-knowledge"},  # handled via external_mcp_servers
        {"type": "bogus"},  # unknown family
    ]

    gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="")

    ctrl.create_gateway_target.assert_not_called()


@pytest.mark.parametrize(
    ("target", "missing"),
    [
        ({"type": "lambda"}, "function_arn"),
        ({"type": "openapi"}, "spec_url or spec_content"),
        ({"type": "smithy", "model_name": "dynamodb"}, "model_content"),
    ],
)
def test_a_declared_target_with_no_payload_is_fatal(target, missing):
    """It used to be skipped with a warning, which is the worst of the options.

    Observed live: ``Gateway lambda target #0 has no function_arn; skipping`` on a deploy
    that reported **success**. The canvas hands out ``{type: 'lambda', functionArn: ''}``
    as the default for a new target and frontend validation only checked the ARN's format
    *when one was present*, so leaving the field blank was enough to ship an agent whose
    tool was silently absent. Both layers now refuse; this is the backstop for an API
    caller that bypasses the UI.
    """
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    with pytest.raises(ValueError, match=missing):
        gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", [target], gateway_role_arn="")

    ctrl.create_gateway_target.assert_not_called()


def test_a_complete_target_after_an_incomplete_one_is_not_silently_lost():
    """The failure must be loud enough that the *rest* of the list cannot be half-applied
    without anyone noticing: the gateway step fails and auto-cleanup runs."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [
        {"type": "lambda", "function_arn": "arn:aws:lambda:us-west-2:123456789012:function:a"},
        {"type": "lambda"},  # incomplete
    ]

    with patch.object(gd, "_grant_gateway_invoke_on_lambda"), pytest.raises(ValueError, match="#1"):
        gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="")

    # The first one was already created — the caller sees a failed step, not a success
    # with one of two tools attached.
    assert ctrl.create_gateway_target.call_count == 1


def test_deploy_config_targets_openapi_fetches_spec_url_when_no_inline():
    """An openapi target with only a spec_url fetches the spec via the SSRF-guarded
    fetcher before building the openApiSchema."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [{"type": "openapi", "spec_url": "https://example.com/openapi.json"}]

    with patch.object(gd, "_fetch_openapi_spec", return_value='{"openapi": "3.0.0"}') as fetch:
        gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="")

    fetch.assert_called_once_with("https://example.com/openapi.json")
    params = ctrl.create_gateway_target.call_args.kwargs
    assert params["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"] == '{"openapi": "3.0.0"}'


# ---------------------------------------------------------------------------
# A bring-your-own Lambda target the platform may not grant itself invoke on
# ---------------------------------------------------------------------------


def _denied(op: str = "AddPermission") -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User: arn:aws:sts::166827918465:assumed-role/"
                    "acfe2e-p0920-StepGatewayRoleAAFE0C07-m9tHG9ZTMy3k/x is not authorized to "
                    "perform: lambda:AddPermission on resource: arn:aws:lambda:us-east-1:"
                    "166827918465:function:acfe2e-llstub-22add474 because no identity-based "
                    "policy allows the lambda:AddPermission action"
                ),
            }
        },
        op,
    )


def _lambda_client_that(exc: Exception) -> MagicMock:
    """A Lambda client double whose add_permission raises *exc*.

    ``lambda_client.exceptions.X`` must be real exception CLASSES, because the code
    under test uses them in ``except`` clauses — a MagicMock attribute there raises
    TypeError and would make this test pass for the wrong reason.
    """
    from botocore.exceptions import ClientError

    class _Conflict(Exception): ...

    class _InvalidParam(Exception): ...

    lam = MagicMock()
    lam.exceptions.ResourceConflictException = _Conflict
    lam.exceptions.InvalidParameterValueException = _InvalidParam
    lam.add_permission.side_effect = exc
    assert issubclass(ClientError, Exception)
    return lam


def test_a_denied_grant_on_a_byo_lambda_explains_what_to_do():
    """Measured live 2026-09-21: a gateway target naming a customer function outside
    the platform's ``function:AgentCore*`` prefix failed the whole deploy with a bare
    ``not authorized to perform: lambda:AddPermission``.

    The prefix is a deliberate boundary — blanket AddPermission on ``function:*`` would
    let any tenant's canvas make the platform rewrite the resource policy of any
    function in the account (F-7) — so the fix is an owner-set opt-in tag, and this
    error is how the user learns that. It must name the function, the tag, the exact
    CLI call, and why the deploy stopped rather than shipping a broken tool.
    """
    from app.services import gateway_deployer as gd

    arn = "arn:aws:lambda:us-east-1:166827918465:function:acfe2e-llstub-22add474"
    lam = _lambda_client_that(_denied())

    with (
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_prune_orphaned_lambda_permissions"),
        pytest.raises(ValueError) as ei,
    ):
        gd._grant_gateway_invoke_on_lambda("us-east-1", arn, "arn:aws:iam::1:role/AgentCoreGateway-gw")

    msg = str(ei.value)
    assert arn in msg
    assert f"{gd.GATEWAY_TARGET_OPT_IN_TAG}={gd.GATEWAY_TARGET_OPT_IN_VALUE}" in msg
    assert "aws lambda tag-resource" in msg
    assert "AccessDeniedException at invoke time" in msg
    # One attempt, not eight: a denial is not a propagation race.
    assert lam.add_permission.call_count == 1


def test_a_denied_grant_fails_the_deploy_rather_than_shipping_a_broken_target():
    """The F-24 rule applied to the grant: a target the gateway cannot invoke must not
    reach a "successful" deploy. The ValueError has to escape ``_deploy_config_targets``
    (whose caller converts it into the abort inventory), and no target may be created."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    arn = "arn:aws:lambda:us-east-1:166827918465:function:someone-elses-fn"
    lam = _lambda_client_that(_denied())

    with (
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_prune_orphaned_lambda_permissions"),
        pytest.raises(ValueError, match="opted in by its owner"),
    ):
        gd._deploy_config_targets(
            ctrl,
            "gw-1",
            "us-east-1",
            [{"type": "lambda", "function_arn": arn}],
            gateway_role_arn="arn:aws:iam::1:role/AgentCoreGateway-gw",
        )

    ctrl.create_gateway_target.assert_not_called()


def test_a_denial_of_some_other_action_is_not_relabelled_as_an_opt_in_problem():
    """Only AccessDeniedException gets the tag advice. A ThrottlingException (or any
    other ClientError) must propagate untouched — telling a user to tag their function
    when the real cause is throttling sends them to fix the wrong thing.

    The branch matches on the error CODE, not a substring of the message, so an error
    quoting "AccessDeniedException" inside some other failure does not steer it either.
    """
    from app.services import gateway_deployer as gd
    from botocore.exceptions import ClientError

    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "AddPermission")
    lam = _lambda_client_that(throttled)

    with (
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_prune_orphaned_lambda_permissions"),
        pytest.raises(ClientError) as ei,
    ):
        gd._grant_gateway_invoke_on_lambda(
            "us-east-1",
            "arn:aws:lambda:us-east-1:1:function:f",
            "arn:aws:iam::1:role/AgentCoreGateway-gw",
        )

    assert ei.value.response["Error"]["Code"] == "ThrottlingException"
