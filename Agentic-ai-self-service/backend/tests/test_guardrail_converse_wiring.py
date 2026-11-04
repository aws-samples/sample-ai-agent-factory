"""Regression: guardrailConfig must reach boto3 ``converse()`` templates as a
VALID single-brace dict splat — never literal ``{{`` double braces.

Bug (brace doubling): the boto3 converse host generator
(``_generate_default_agent``) is an f-string, so its ``{{``/``}}`` already collapses to single braces in the RETURNED code. ``_inject_guardrails`` then
splices ``guardrailConfig`` via a plain ``str.replace`` — NOT ``.format`` — so a
double-braced replacement string lands LITERAL ``{{...}}`` in the deployed file.
Python parses ``{{...}}`` as a *set literal containing a dict*, which raises
``TypeError: unhashable type: 'dict'`` the moment the guarded converse template
runs with a guardrail configured. The converse path was previously uncovered,
which is why the brace bug slipped through.

These tests RENDER the converse templates with a guardrail and assert: (a) the
output compiles and carries NO literal ``{{``; (b) it contains the valid
``**({"guardrailConfig": {...}} if GUARDRAIL_ID else {})`` splat; (c) a
no-guardrail render is unchanged; (d) injection is idempotent.

Run:
    cd backend && python3 -m pytest tests/test_guardrail_converse_wiring.py -q
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from types import SimpleNamespace

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError

sys.path.insert(0, "src")

from app.models.deployment_models import DeployRequest, RuntimeConfig  # noqa: E402
from app.services.cfn_template_generator import CfnTemplateGenerator  # noqa: E402
from app.services.code_generator import (  # noqa: E402
    _generate_default_agent,
    _inject_guardrails,
    generate_agent_code,
)

# The exact, valid single-brace splat that MUST appear in the deployed code.
_VALID_SPLAT = (
    '**({"guardrailConfig": {"guardrailIdentifier": GUARDRAIL_ID, '
    '"guardrailVersion": GUARDRAIL_VERSION, "trace": "disabled"}} if GUARDRAIL_ID else {})'
)


def _cfg() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "name": "cvgr00001",
            "model": {
                "provider": "bedrock",
                "modelId": "us.anthropic.claude-sonnet-5",
            },
            "systemPrompt": "You are a search agent.",
        }
    )


def test_managed_web_search_uses_strands_guardrail_hooks_not_converse_splats():
    """The gallery search template migrated from hand-rolled Converse to Gateway."""
    code = generate_agent_code(_cfg(), tools=["guardrails"], template_id="web-search-agent")

    assert "MCPClient" in code
    assert 'hooks=globals().get("_GUARDRAIL_HOOKS", [])' in code
    assert "guardrailConfig" not in code
    assert "{{" not in code
    ast.parse(code)
    compile(code, "<web-search-agent>", "exec")


def test_no_tool_converse_template_also_wires_guardrail():
    """The lightweight no-tools converse template has no toolConfig anchor; it
    must still wire guardrails via its inferenceConfig line (low-risk coverage
    gap), and the result must compile with single braces."""
    code = _inject_guardrails(_generate_default_agent("You are an agent.", "model-id", "us-east-1"))
    assert "{{" not in code
    assert _VALID_SPLAT in code
    assert code.count("guardrailConfig") == 1
    ast.parse(code)


def test_no_tool_converse_template_without_guardrail_is_unchanged():
    """The bare (un-injected) no-tools template carries no guardrail wiring."""
    code = _generate_default_agent("You are an agent.", "model-id", "us-east-1")
    assert "guardrailConfig" not in code
    assert "GUARDRAIL_ID" not in code
    assert "{{" not in code
    ast.parse(code)


@pytest.fixture(scope="module")
def scratch_guardrail_source():
    """Exercise the real download, not a separately copied Guardrail helper."""
    request = DeployRequest.model_validate(
        {
            "nodeId": "scratch-guardrails-hitl",
            "config": {
                "name": "scratch_guardrails_hitl",
                "protocol": "HTTP",
                "model": {"provider": "bedrock", "modelId": "us.anthropic.claude-sonnet-5"},
                "systemPrompt": "Validate a research idea before executing a sensitive action.",
            },
            "connectedTools": ["guardrails", "hitl"],
            "guardrailsConfig": {
                "mode": "create_new",
                "contentFilters": {"hate": "HIGH", "prompt_attack": "HIGH"},
                "piiFilters": [{"type": "EMAIL", "action": "ANONYMIZE"}],
            },
        }
    )
    bundle = CfnTemplateGenerator().generate(request)
    template = yaml.safe_load(bundle.template_yaml)
    policy = template["Resources"]["BedrockGuardrail"]["Properties"]
    assert policy["ContentPolicyConfig"]["FiltersConfig"][0]["InputStrength"] == "HIGH"
    assert policy["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"] == [{"Type": "EMAIL", "Action": "ANONYMIZE"}]
    assert template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]["GUARDRAIL_VERSION"] == {
        "Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]
    }
    tree = ast.parse(bundle.agent_code)
    assert not any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "REGION" for target in node.targets)
        for node in tree.body
    ), "The reproduced scratch path deliberately has no module-level REGION."
    compile(tree, "<scratch-guardrails-hitl-export>", "exec")
    return bundle.agent_code


def _emitted_guardrail_namespace(monkeypatch, source, actions, *, aws_region="eu-west-1", template_region=None):
    """Execute only actual emitted security AST nodes, with no model or AWS calls."""
    monkeypatch.setenv("GUARDRAIL_ID", "guardrail-test")
    monkeypatch.setenv("GUARDRAIL_VERSION", "7")
    if aws_region is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", aws_region)
    responses = iter(actions)
    client_creations = []
    requests = []

    def apply_guardrail(**request):
        requests.append(request)
        action = next(responses)
        if action == "AWS_ERROR":
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Offline test denial"}},
                "ApplyGuardrail",
            )
        return {"action": action}

    def create_client(service, **kwargs):
        assert service == "bedrock-runtime", "No unrelated AWS service may be called."
        client_creations.append((service, kwargs))
        return SimpleNamespace(apply_guardrail=apply_guardrail)

    monkeypatch.setattr(boto3, "client", create_client)
    security_nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names = [alias.asname or alias.name for alias in node.names]
            if any(name == "os" or name.startswith(("_guardrail_", "_hitl_")) for name in names):
                security_nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(name.startswith(("_guardrail_", "_GUARDRAIL_", "GUARDRAIL_")) for name in names):
                security_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name.startswith(("_guardrail_", "_hitl_")):
            security_nodes.append(node)
        elif isinstance(node, ast.Try) and any(
            isinstance(child, ast.ClassDef) and child.name in {"_ToolGuardrailHook", "_ApprovalHook"}
            for child in ast.walk(node)
        ):
            security_nodes.append(node)
    namespace = {"__name__": "emitted_scratch_security"}
    if template_region is not None:
        namespace["REGION"] = template_region
    exec(compile(ast.Module(body=security_nodes, type_ignores=[]), "<emitted-scratch-security>", "exec"), namespace)
    assert "_guardrail_client" in namespace and "_APPROVAL_HOOKS" in namespace
    return namespace, client_creations, requests


@pytest.mark.parametrize("aws_region", ["eu-west-1", "ap-southeast-2"])
def test_scratch_guardrails_allow_benign_tool_input_and_output_in_runtime_region(
    monkeypatch, scratch_guardrail_source, aws_region
):
    namespace, clients, requests = _emitted_guardrail_namespace(
        monkeypatch, scratch_guardrail_source, ["NONE", "NONE"], aws_region=aws_region
    )
    executed = []

    def handler(tool_input):
        executed.append(tool_input)
        return {"result": "safe research brief"}

    result = namespace["_guardrail_invoke_tool"](
        handler, {"toolUseId": "research-1", "input": {"query": "approved public research"}}
    )

    assert result == {"result": "safe research brief"}
    assert executed == [{"query": "approved public research"}]
    assert clients == [("bedrock-runtime", {"region_name": aws_region})], "The regional client is cached."
    assert [request["source"] for request in requests] == ["INPUT", "OUTPUT"]
    assert all(request["guardrailIdentifier"] == "guardrail-test" for request in requests)
    assert all(request["guardrailVersion"] == "7" for request in requests)


@pytest.mark.parametrize(
    ("aws_region", "template_region", "expected_region"),
    [
        ("eu-west-1", "eu-central-1", "eu-central-1"),
        ("eu-west-1", "", "eu-west-1"),
        (None, None, "us-east-1"),
    ],
)
def test_emitted_guardrail_client_keeps_explicit_template_region_precedence_and_default(
    monkeypatch, scratch_guardrail_source, aws_region, template_region, expected_region
):
    namespace, clients, requests = _emitted_guardrail_namespace(
        monkeypatch,
        scratch_guardrail_source,
        ["NONE"],
        aws_region=aws_region,
        template_region=template_region,
    )

    assert namespace["_guardrail_allows"]({"query": "safe"}, "INPUT") is True
    assert clients == [("bedrock-runtime", {"region_name": expected_region})]
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("actions", "stage", "handler_calls"),
    [
        (["GUARDRAIL_INTERVENED"], "input", 0),
        (["NONE", "GUARDRAIL_INTERVENED"], "result", 1),
        (["AWS_ERROR"], "input", 0),
        (["NONE", "AWS_ERROR"], "result", 1),
    ],
)
def test_scratch_guardrails_keep_interventions_and_aws_errors_fail_closed(
    monkeypatch, scratch_guardrail_source, actions, stage, handler_calls
):
    namespace, clients, requests = _emitted_guardrail_namespace(monkeypatch, scratch_guardrail_source, actions)
    executed = []

    def handler(tool_input):
        executed.append(tool_input)
        return "restricted-result"

    result = namespace["_guardrail_invoke_tool"](handler, {"toolUseId": "research-1", "input": {"query": "test"}})

    assert json.loads(result) == {"error": f"Tool {stage} blocked by the safety guardrail."}
    assert "restricted-result" not in result
    assert len(executed) == handler_calls
    assert clients == [("bedrock-runtime", {"region_name": "eu-west-1"})]
    assert len(requests) == len(actions)


@pytest.mark.parametrize("guardrail_action", ["NONE", "GUARDRAIL_INTERVENED", "AWS_ERROR"])
def test_scratch_emitted_guardrail_hook_screens_actual_tool_results(
    monkeypatch, scratch_guardrail_source, guardrail_action
):
    namespace, clients, requests = _emitted_guardrail_namespace(
        monkeypatch, scratch_guardrail_source, [guardrail_action]
    )
    original_result = {"status": "success", "content": [{"text": "restricted-result"}]}
    event = SimpleNamespace(tool_use={"toolUseId": "research-1"}, result=original_result)

    namespace["_GUARDRAIL_HOOKS"][0]._after_tool(event)

    assert clients == [("bedrock-runtime", {"region_name": "eu-west-1"})]
    assert requests[0]["source"] == "OUTPUT"
    if guardrail_action == "NONE":
        assert event.result is original_result
    else:
        assert event.result == {
            "toolUseId": "research-1",
            "status": "error",
            "content": [{"text": "Tool result blocked by the safety guardrail."}],
        }
        assert "restricted-result" not in json.dumps(event.result)


@pytest.mark.parametrize("guardrail_action", ["NONE", "GUARDRAIL_INTERVENED"])
def test_scratch_tool_guardrail_and_human_approval_hooks_remain_composed(
    monkeypatch, scratch_guardrail_source, guardrail_action
):
    namespace, clients, requests = _emitted_guardrail_namespace(
        monkeypatch, scratch_guardrail_source, [guardrail_action]
    )
    monkeypatch.setenv(
        "LOOM_APPROVAL_POLICIES",
        json.dumps([{"name": "sensitive-action", "tool_match": ["send_email"], "mode": "require"}]),
    )
    recorded = []

    def record_pending(tool_name, tool_input):
        recorded.append((tool_name, tool_input))
        return "offline-approval-request"

    namespace["_hitl_record_pending"] = record_pending
    agent_calls = [
        node
        for node in ast.walk(ast.parse(scratch_guardrail_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Agent"
    ]
    assert len(agent_calls) == 1
    arguments = {keyword.arg: keyword.value for keyword in agent_calls[0].keywords}
    assert any(isinstance(node, ast.Name) and node.id == "human_approval" for node in ast.walk(arguments["tools"]))
    hooks = eval(compile(ast.Expression(arguments["hooks"]), "<emitted-agent-hooks>", "eval"), namespace)
    assert [type(hook).__name__ for hook in hooks] == ["_ToolGuardrailHook", "_ApprovalHook"]
    callbacks = []
    registry = SimpleNamespace(add_callback=lambda event_type, callback: callbacks.append((event_type, callback)))
    for hook in hooks:
        hook.register_hooks(registry)
    assert [callback.__name__ for _event, callback in callbacks] == ["_before_tool", "_after_tool", "_before_tool"]
    original_calls = []

    async def invoke_original(tool_use):
        original_calls.append(tool_use)
        return {"status": "success"}

    original_tool = SimpleNamespace(tool_name="send_email", invoke=invoke_original)
    tool_use = {"toolUseId": "approval-1", "name": "send_email", "input": {"message": "safe public summary"}}
    event = SimpleNamespace(cancel_tool=False, tool_use=tool_use, selected_tool=original_tool)
    for hook in hooks:
        hook._before_tool(event)

    assert clients == [("bedrock-runtime", {"region_name": "eu-west-1"})]
    assert requests[0]["source"] == "INPUT"
    if guardrail_action == "GUARDRAIL_INTERVENED":
        assert event.cancel_tool == "Tool input blocked by the safety guardrail."
        assert event.selected_tool is original_tool
        assert recorded == [], "Human approval must not clear a Guardrail cancellation."
    else:
        assert event.cancel_tool is False
        assert recorded == [("send_email", tool_use["input"])]
        assert event.selected_tool is not original_tool, "Benign content still requires human approval."
        assert event.selected_tool.tool_name == "send_email"
        result = asyncio.run(event.selected_tool.invoke(tool_use))
        assert result["status"] == "error"
        refusal = json.loads(result["content"][0]["text"])
        assert refusal["status"] == "APPROVAL_REQUIRED"
        assert refusal["request_id"] == "offline-approval-request"
    assert original_calls == [], "Neither blocked input nor required approval may execute the original tool."
