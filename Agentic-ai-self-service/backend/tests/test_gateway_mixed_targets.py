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


def test_deploy_config_targets_skips_mcp_and_unknown_and_incomplete():
    """mcp_server entries (handled elsewhere), unknown families, and lambda/
    openapi/smithy entries missing their payload are skipped — no target."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    targets = [
        {"type": "mcp_server", "server_id": "aws-knowledge"},  # handled via external_mcp_servers
        {"type": "lambda"},  # no function_arn
        {"type": "openapi"},  # no spec
        {"type": "smithy", "model_name": "dynamodb"},  # no inline content
        {"type": "bogus"},  # unknown family
    ]

    gd._deploy_config_targets(ctrl, "gw-1", "us-west-2", targets, gateway_role_arn="")

    ctrl.create_gateway_target.assert_not_called()


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
