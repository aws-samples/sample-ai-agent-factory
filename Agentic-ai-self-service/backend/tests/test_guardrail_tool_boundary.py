"""Guardrails screen every generated tool argument and result fail closed."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.session
import pytest
from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import (
    _GUARDRAIL_STRANDS_HOOK_SRC,
    _GUARDRAIL_TOOL_CORE_SRC,
    generate_agent_code,
)
from botocore.validate import validate_parameters


def _core_namespace(actions):
    class _Bedrock:
        def __init__(self):
            self.requests = []

        def apply_guardrail(self, **kwargs):
            self.requests.append(kwargs)
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            return {"action": action}

    namespace = {
        "GUARDRAIL_ID": "gr-1",
        "GUARDRAIL_VERSION": "7",
        "REGION": "eu-west-1",
    }
    exec(_GUARDRAIL_TOOL_CORE_SRC, namespace)
    client = _Bedrock()
    namespace["_guardrail_runtime_client"] = client
    return namespace, client


def test_direct_tool_wrapper_screens_input_and_output():
    namespace, client = _core_namespace(["NONE", "NONE"])
    handler = MagicMock(return_value={"result": "safe"})
    tool_use = {
        "toolUseId": "tool-1",
        "name": "lookup",
        "input": {"customer": "123"},
    }

    result = namespace["_guardrail_invoke_tool"](handler, tool_use)

    assert result == {"result": "safe"}
    handler.assert_called_once_with({"customer": "123"})
    assert [request["source"] for request in client.requests] == ["INPUT", "OUTPUT"]
    assert client.requests[0]["content"] == [
        {"text": {"text": json.dumps({"customer": "123"}, separators=(",", ":"), sort_keys=True)}}
    ]
    operation = botocore.session.get_session().get_service_model("bedrock-runtime").operation_model("ApplyGuardrail")
    validate_parameters(client.requests[0], operation.input_shape)


def test_input_intervention_prevents_tool_execution():
    namespace, _client = _core_namespace(["GUARDRAIL_INTERVENED"])
    handler = MagicMock()

    result = namespace["_guardrail_invoke_tool"](
        handler,
        {"toolUseId": "tool-1", "input": {"secret": "blocked"}},  # pragma: allowlist secret
    )

    handler.assert_not_called()
    assert json.loads(result) == {"error": "Tool input blocked by the safety guardrail."}


def test_output_intervention_hides_original_tool_result():
    namespace, _client = _core_namespace(["NONE", "GUARDRAIL_INTERVENED"])
    handler = MagicMock(return_value="sensitive-result")

    result = namespace["_guardrail_invoke_tool"](
        handler,
        {"toolUseId": "tool-1", "input": {"query": "safe"}},
    )

    assert "sensitive-result" not in result
    assert json.loads(result) == {"error": "Tool result blocked by the safety guardrail."}


@pytest.mark.parametrize("actions", [[RuntimeError("outage")], ["NONE", RuntimeError("outage")]])
def test_guardrail_dependency_failure_blocks_tool_boundary(actions):
    namespace, _client = _core_namespace(actions)
    handler = MagicMock(return_value="result")

    result = namespace["_guardrail_invoke_tool"](
        handler,
        {"toolUseId": "tool-1", "input": {"query": "safe"}},
    )

    assert "blocked by the safety guardrail" in result


def test_oversized_tool_input_is_blocked_without_service_call():
    namespace, client = _core_namespace([])
    handler = MagicMock()

    result = namespace["_guardrail_invoke_tool"](
        handler,
        {"toolUseId": "tool-1", "input": {"value": "x" * (64 * 1024)}},
    )

    handler.assert_not_called()
    assert client.requests == []
    assert "Tool input blocked" in result


def test_strands_hook_cancels_input_and_replaces_output():
    namespace, _client = _core_namespace(["GUARDRAIL_INTERVENED", "GUARDRAIL_INTERVENED"])
    exec(_GUARDRAIL_STRANDS_HOOK_SRC, namespace)
    hook = namespace["_GUARDRAIL_HOOKS"][0]
    before = SimpleNamespace(
        cancel_tool=False,
        tool_use={"toolUseId": "tool-1", "input": {"secret": "blocked"}},  # pragma: allowlist secret
    )
    hook._before_tool(before)
    assert before.cancel_tool == "Tool input blocked by the safety guardrail."

    after = SimpleNamespace(
        tool_use={"toolUseId": "tool-1"},
        result={"status": "success", "content": [{"text": "sensitive"}]},
    )
    hook._after_tool(after)
    assert after.result == {
        "toolUseId": "tool-1",
        "status": "error",
        "content": [{"text": "Tool result blocked by the safety guardrail."}],
    }


def _config():
    return RuntimeConfig(
        name="guarded-agent",
        model={"modelId": "eu.anthropic.claude-sonnet-5"},
    )


def _assert_tool_boundary(code):
    assert "class _ToolGuardrailHook" in code
    assert "apply_guardrail(" in code
    assert 'hooks=globals().get("_GUARDRAIL_HOOKS", [])' in code
    assert "event.cancel_tool" in code
    assert "event.result = _guardrail_error_result" in code


def test_generated_strands_gateway_has_pre_and_post_tool_guardrails():
    code = generate_agent_code(
        _config(),
        tools=["gateway", "guardrails"],
        template_id="strands-gateway-agent",
        portable=True,
    )

    compile(code, "guarded-gateway.py", "exec")
    _assert_tool_boundary(code)


def test_guardrail_and_hitl_hooks_compose_guardrail_first():
    code = generate_agent_code(
        _config(),
        tools=["gateway", "guardrails", "hitl"],
        template_id="strands-gateway-agent",
        portable=True,
    )

    compile(code, "guarded-hitl.py", "exec")
    assert 'hooks=globals().get("_GUARDRAIL_HOOKS", []) + _APPROVAL_HOOKS' in code
    assert 'if getattr(event, "cancel_tool", False):' in code


def test_managed_web_search_uses_strands_tool_guardrail_hooks():
    code = generate_agent_code(
        _config(),
        tools=["guardrails"],
        template_id="web-search-agent",
    )

    compile(code, "guarded-web-search.py", "exec")
    assert "MCPClient" in code
    _assert_tool_boundary(code)


def test_forced_code_interpreter_path_uses_guarded_wrapper():
    code = generate_agent_code(
        _config(),
        tools=["code_interpreter", "guardrails"],
    )

    compile(code, "guarded-code-interpreter.py", "exec")
    assert "stdout = _guardrail_invoke_tool(" in code
    assert "stdout = execute_python(code)" not in code


def test_mutation_removing_tool_hooks_is_detected():
    code = generate_agent_code(
        _config(),
        tools=["gateway", "guardrails"],
        template_id="strands-gateway-agent",
        portable=True,
    )
    mutant = code.replace(
        'hooks=globals().get("_GUARDRAIL_HOOKS", [])',
        "",
    )

    with pytest.raises(AssertionError):
        _assert_tool_boundary(mutant)
