"""A gateway node on the canvas must reach the GENERATOR, not just the deployer.

The direct deploy path (``WorkflowExecutor.deploy``, what ``POST /api/workflows/{id}/deploy``
runs) held two different definitions of "this canvas has a gateway":

* the deploy decision counted the gateway NODE, so it created the gateway and injected
  ``GATEWAY_URL`` / ``GATEWAY_AUTH_MODE`` / ``GATEWAY_MCP_SERVERS`` /
  ``GATEWAY_API_KEY_SECRET_ARN`` into the runtime environment and granted the runtime role
  the connector secret;
* the codegen decision required ``"gateway" in connected_tools``, and nothing ever derived
  that list from the canvas while the route passes none.

Measured live on 2026-09-20 against runtime ``llgw_direct_22add474-sM5DJrATZS``: deploy
``succeeded``, runtime ``READY``, invoke returned 200, all four env vars present, the IAM
grant verified with ``simulate-principal-policy`` — and ``grep -c GATEWAY_URL agent.py`` on
the artifact actually deployed to S3 returned **0**. The agent had no gateway. The
generator's own zero-tools wiring proof could not fire, because it is emitted only inside
the gateway branch that never ran.

So the test below is deliberately not a test of the helper. It drives the real
``deploy()`` with the real generator and asserts on the source that generator returned,
because a test of ``canvas_connected_tools`` alone would have passed on the broken code:
the helper is not where the bug was, the disagreement between two call sites was.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from app.models.components import GatewayConfiguration, RuntimeConfiguration
from app.models.workflow import (
    AgentCoreComponentType,
    ComponentNode,
    DeploymentConfig,
    Position,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.services.deployment import canvas_connected_tools

# Shape copied from litellm_gateway_deployer.deploy_litellm_gateway's real return value
# (measured against a live LiteLLM 1.102.0 proxy): no gateway_id, no gateway_arn, and a
# client_info carrying the connector secret ARN rather than any key material.
_LITELLM_RESULT = {
    "success": True,
    "gateway_url": "https://proxy.example.invalid/aws_knowledge/mcp",
    "gateway_id": None,
    "gateway_arn": None,
    "gateway_name": "gwprobe",
    "gateway_provider": "litellm",
    "litellm_servers": ["aws_knowledge"],
    "client_info": {
        "provider": "litellm",
        "api_key_ref": "arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-connector/x/y-AAAAAA",
    },
    "connector_secret_arns": ["arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-connector/x/y-AAAAAA"],
}


def _gateway_canvas() -> WorkflowDefinition:
    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id="wf-canvas-decides",
        name="Gateway canvas",
        version="1.0.0",
        nodes=[
            ComponentNode(
                id="rt-1",
                type=AgentCoreComponentType.RUNTIME,
                position=Position(x=0, y=0),
                data=RuntimeConfiguration(
                    name="canvas_decides_probe",
                    system_prompt="You are a probe.",
                    model={"model_id": "us.anthropic.claude-sonnet-5"},
                ),
            ),
            ComponentNode(
                id="gw-1",
                type=AgentCoreComponentType.GATEWAY,
                position=Position(x=200, y=0),
                data=GatewayConfiguration(
                    name="gwprobe",
                    gateway_provider="litellm",
                    litellm_base_url="https://proxy.example.invalid",
                    litellm_api_key_ref=_LITELLM_RESULT["client_info"]["api_key_ref"],
                    litellm_servers=["aws_knowledge"],
                ),
            ),
        ],
        edges=[],
        metadata=WorkflowMetadata(author="test", aws_region="us-east-1"),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_a_gateway_node_alone_makes_the_generator_emit_gateway_wiring():
    """The route passes no connected_tools. The canvas must still be enough.

    Only the gateway deployer is stubbed — everything from there to the generator is the
    real code, and the generator itself is real. The deploy is expected to fail after
    this point (it would need S3 and AgentCore next); the assertion is on what the
    generator was handed and what it produced, which is the part that was broken.
    """
    from app.services import deployment as deployment_module

    captured: dict = {}
    real_generate = deployment_module.generate_unified_agent_code

    def spy(runtime_config, **kwargs):
        code = real_generate(runtime_config, **kwargs)
        captured["connected_tools"] = list(kwargs.get("connected_tools") or [])
        captured["gateway_result"] = kwargs.get("gateway_result")
        captured["code"] = code
        return code

    executor = deployment_module.WorkflowExecutor(region="us-east-1")

    with (
        patch(
            "app.services.litellm_gateway_deployer.deploy_litellm_gateway",
            return_value=_LITELLM_RESULT,
        ),
        patch.object(deployment_module, "generate_unified_agent_code", spy),
    ):
        await executor.deploy(_gateway_canvas(), DeploymentConfig(aws_region="us-east-1"))

    assert captured, (
        "the generator was never reached, so this test proves nothing about it; the deploy "
        "failed earlier than codegen and the stub list above needs extending"
    )
    assert "gateway" in captured["connected_tools"], (
        "the gateway node did not reach the codegen decision: connected_tools was "
        f"{captured['connected_tools']!r}. This is the defect — deploy() created the gateway "
        "and injected its env vars off the NODE, while codegen keyed off this list."
    )
    code = captured["code"]
    assert "GATEWAY_URL" in code, (
        "the generated agent reads no GATEWAY_URL, so the deployed runtime has a gateway "
        "URL, a virtual key and an IAM grant it can never use"
    )
    # The generator's non-vacuity guard: it raises when the gateway yields zero tools.
    # Its presence is what makes a broken hop loud at invoke time instead of silent.
    assert "_discover_gateway_tools" in code or "_get_gateway_tools" in code, (
        "no gateway tool discovery in the generated agent, so the zero-tools wiring proof cannot fire"
    )


def test_the_unified_generator_speaks_static_bearer_not_only_cognito():
    """The second defect this canvas exposed, guarded separately.

    ``generate_unified_agent_code`` implemented ONLY the Cognito OAuth transport, while
    ``code_generator.py`` implements ``static_bearer`` in both of its variants. Fixing the
    list alone would therefore have produced an agent that attempted a token exchange
    against a LiteLLM proxy with no Cognito to exchange against: empty token, no headers,
    no tools — a different silent failure at the same place.

    The wire contract asserted here was measured against a real LiteLLM 1.102.0 proxy: the
    key travels in ``x-litellm-api-key`` WITH a ``Bearer `` prefix (the ``/mcp/`` endpoint
    rejects a bare key), servers are scoped by ``x-mcp-servers``, and the key itself is
    read from Secrets Manager at the moment of use because ``GetAgentRuntime`` returns
    runtime environment variables in plaintext.
    """
    from app.models.components import RuntimeConfiguration as RC
    from app.services.deployment import generate_unified_agent_code

    code = generate_unified_agent_code(
        RC(name="p", system_prompt="probe", model={"model_id": "us.anthropic.claude-sonnet-5"}),
        connected_tools=["gateway"],
        gateway_result=_LITELLM_RESULT,
        region="us-east-1",
    )
    compile(code, "agent.py", "exec")  # a generated module, not the generator

    # Anchored PER FUNCTION, not by substring over the whole file. A plain
    # `"static_bearer" in code` check is satisfied by whichever of the two call sites
    # still has it, so deleting the branch from either one survived the first version of
    # this test. Measured: that mutation passed 3/3 until this became an ast walk.
    bodies = {node.name: ast.unparse(node) for node in ast.walk(ast.parse(code)) if isinstance(node, ast.FunctionDef)}
    for fn, fragments in {
        # The credential side: static_bearer must resolve the virtual key, because there
        # is no token exchange to perform against a LiteLLM proxy.
        "_get_oauth_token": ("static_bearer", "_resolve_gateway_key()"),
        # The transport side: LiteLLM's own header, the Bearer prefix its /mcp/ endpoint
        # requires, server scoping, and the oauth2 fallback left intact.
        "_create_transport": ("static_bearer", "x-litellm-api-key", "Bearer ", "x-mcp-servers", "Authorization"),
        # The resolver itself: by reference, never a plaintext env var.
        "_resolve_gateway_key": ("GATEWAY_API_KEY_SECRET_ARN", "secretsmanager", "get_secret_value"),
    }.items():
        assert fn in bodies, f"the generated agent has no {fn}, so it cannot talk to a LiteLLM gateway"
        for fragment in fragments:
            assert fragment in bodies[fn], f"{fn} in the generated agent is missing {fragment!r}"

    assert 'GATEWAY_AUTH_MODE = os.environ.get("GATEWAY_AUTH_MODE", "oauth2")' in code, (
        "the auth mode is not read from the environment, or no longer defaults to oauth2 — "
        "every existing AgentCore gateway deployment reads that default"
    )
    # Backward compatibility: a runtime deployed before the key moved to Secrets Manager
    # still carries GATEWAY_API_KEY.
    assert 'os.environ.get("GATEWAY_API_KEY", "")' in code


@pytest.mark.asyncio
async def test_the_direct_path_injects_the_litellm_env_by_reference():
    """Parity with ``TestRuntimeConfigureAuthMode`` in test_litellm_gateway.py.

    Those tests drive ``runtime_configure_step`` — the Step Functions path. The direct
    path builds its runtime environment inline in ``deploy()``, and until this session it
    built no LiteLLM environment at all and granted the runtime role neither secret. That
    fix was verified live (``GetAgentRuntime`` returns env vars in plaintext, so "no
    plaintext key is present" is a measurement there); this pins the same contract without
    an AWS account, so the two paths cannot drift again.

    moto supplies STS and IAM, so the runtime role is built by the real code. Two calls are
    stubbed and neither is the subject: ``create_agent_runtime``, to capture the environment
    the real code assembled, and ``wait_for_runtime_ready``, which otherwise polls a runtime
    that does not exist on a 15-second cycle until its timeout (measured: the first version
    of this test hung there, ``runtime_deployer.py:821``).
    """
    from app.services import deployment as deployment_module
    from moto import mock_aws

    captured: dict = {}

    def fake_create(_ctrl, name, role_arn, *_args, **_kwargs):
        captured["env"] = dict(_args[-1] if _args and isinstance(_args[-1], dict) else _kwargs.get("env_vars") or {})
        captured["role_arn"] = role_arn
        return {
            "runtime_id": f"{name}-AAAAAAAA",
            "runtime_arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{name}-AAAAAAAA",
        }

    executor = deployment_module.WorkflowExecutor(region="us-east-1")
    with mock_aws():
        with (
            patch(
                "app.services.litellm_gateway_deployer.deploy_litellm_gateway",
                return_value=_LITELLM_RESULT,
            ),
            patch.object(deployment_module, "create_agent_runtime", fake_create),
            patch.object(
                deployment_module,
                "wait_for_runtime_ready",
                lambda _ctrl, runtime_id, **_kw: {"success": True, "runtime_id": runtime_id, "status": "READY"},
            ),
        ):
            await executor.deploy(_gateway_canvas(), DeploymentConfig(aws_region="us-east-1"))

    assert "env" in captured, "create_agent_runtime was never reached, so no environment was measured"
    env = captured["env"]
    assert env["GATEWAY_URL"] == _LITELLM_RESULT["gateway_url"]
    assert env["GATEWAY_AUTH_MODE"] == "static_bearer"
    assert env["GATEWAY_MCP_SERVERS"] == "aws_knowledge"
    assert env["GATEWAY_API_KEY_SECRET_ARN"] == _LITELLM_RESULT["client_info"]["api_key_ref"]
    # The reference, never the key: GetAgentRuntime returns these in plaintext, and every
    # Task in the state machine re-emits them into the execution history.
    assert "GATEWAY_API_KEY" not in env, "the virtual key must not travel as an env var"
    # No Cognito exchange exists for a LiteLLM gateway; injecting these would make the
    # generated agent call a token endpoint that is not there.
    assert "COGNITO_CLIENT_ID" not in env
    assert "COGNITO_TOKEN_ENDPOINT" not in env


def test_the_canvas_union_preserves_caller_order_and_dedupes():
    """The caller's list is not discarded and not reordered.

    The frontend names tool types that are not nodes (a gateway target's own tool type,
    for one), so this has to be a union rather than a replacement.
    """
    workflow = _gateway_canvas()
    merged = canvas_connected_tools(workflow, ["memory", "gateway"])
    assert merged[:2] == ["memory", "gateway"], f"caller order not preserved: {merged!r}"
    assert merged.count("gateway") == 1, f"gateway duplicated: {merged!r}"
    assert "runtime" in merged, f"canvas types not derived: {merged!r}"
    assert canvas_connected_tools(workflow, None).count("gateway") == 1
    # A workflow with no nodes must not invent any.
    assert canvas_connected_tools(None, ["gateway"]) == ["gateway"]
