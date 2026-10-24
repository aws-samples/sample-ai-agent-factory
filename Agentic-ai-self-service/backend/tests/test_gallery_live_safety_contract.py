"""Live-gallery safety and admission regressions found by real CloudFront tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.services import cfn_template_generator, gateway_deployer
from app.services.codegen_templates import customer_support_impl, customer_support_lambda

from tests.integration import test_template_deployments as matrix


def _legacy_context() -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "CustomerSupportTools___lookup_customer"})
    )


def _legacy_body(event: dict[str, Any]) -> dict[str, Any]:
    response = customer_support_lambda.lambda_handler(event, _legacy_context())
    return json.loads(response["body"])


def test_customer_tools_return_useful_non_pii_data() -> None:
    canonical = json.loads(customer_support_impl._do_get_customer({"customer_id": "CUST-001"}))
    preferred = _legacy_body({"customer_id": "CUST-001"})
    compatibility = _legacy_body({"email": "john@example.com"})

    assert matrix._is_customer_payload(canonical)
    assert matrix._is_legacy_customer_payload(preferred)
    assert compatibility == preferred
    assert all("email" not in value for value in (canonical, preferred, compatibility))


def test_unknown_customer_never_echoes_email_pii() -> None:
    result = _legacy_body({"email": "private-person@example.com"})

    assert result == {"error": "Customer not found. Use a valid demo customer ID."}
    assert "private-person" not in json.dumps(result)


def test_live_and_exported_legacy_schemas_prefer_customer_id() -> None:
    live = next(
        item
        for item in gateway_deployer.CUSTOMER_SUPPORT_TOOLS_SCHEMA["inlinePayload"]
        if item["name"] == "lookup_customer"
    )
    exported = cfn_template_generator._LEGACY_TOOL_SCHEMAS["lookup_customer"]

    assert live == exported
    assert set(live["inputSchema"]["properties"]) == {"customer_id"}
    assert live["inputSchema"]["required"] == ["customer_id"]
    assert live["description"] == "Look up customer information by customer ID."


def test_email_input_schema_removal_mutation_is_detected() -> None:
    live = next(
        item
        for item in gateway_deployer.CUSTOMER_SUPPORT_TOOLS_SCHEMA["inlinePayload"]
        if item["name"] == "lookup_customer"
    )
    mutant = json.loads(json.dumps(live))
    mutant["inputSchema"]["properties"]["email"] = {"type": "string"}

    with pytest.raises(AssertionError):
        assert set(mutant["inputSchema"]["properties"]) == {"customer_id"}


def _receipt(invocation: matrix.ToolInvocation, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": invocation.tool_name,
        "status": "success",
        "input_sha256": matrix._receipt_digest(arguments),
        "argument_sha256": {key: matrix._receipt_digest(value) for key, value in arguments.items()},
    }


def test_gallery_accepts_only_exact_successful_tool_receipts() -> None:
    invocation = matrix._DYNAMIC_WEB_INVOCATIONS[0]
    arguments = {"query": "Amazon Bedrock AgentCore", "maxResults": 5}

    matrix._require_exact_tool_receipt(
        {"toolReceipts": [_receipt(invocation, arguments)]},
        invocation,
    )

    with pytest.raises(AssertionError, match="lacks a successful exact-argument receipt"):
        matrix._require_exact_tool_receipt(
            {"toolReceipts": [_receipt(invocation, {"query": "different query"})]},
            invocation,
        )


def test_gallery_prompts_are_customer_requests_not_injection_commands() -> None:
    prompts = [invocation.prompt for case in matrix.TEMPLATE_CASES for invocation in case.invocations]

    assert prompts
    assert all(prompt.startswith("Please use ") for prompt in prompts)
    assert all("Return only" not in prompt for prompt in prompts)
    assert all("no commentary" not in prompt for prompt in prompts)
    source = Path(matrix.__file__).read_text(encoding="utf-8")
    assert "verification nonce exactly" not in source
    assert "Reply only STORED" not in source


class _Cleanup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def bind_runtime(self, _record: Any, runtime_id: str) -> None:
        self.calls.append(f"bind:{runtime_id}")

    def delete_and_verify(self, _record: Any) -> dict[str, str]:
        self.calls.append("delete")
        return {"delete_status": "deleted"}


class _Budget:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active = False

    def authorize(self, runtime_name: str) -> None:
        assert not self.active
        self.active = True
        self.calls.append(f"authorize:{runtime_name}")

    def release(self, runtime_name: str) -> None:
        assert self.active
        self.active = False
        self.calls.append(f"release:{runtime_name}")


@pytest.mark.parametrize("invoke_fails", [False, True], ids=["success", "failure"])
def test_standalone_mcp_budget_wraps_every_runtime_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    invoke_fails: bool,
) -> None:
    case = next(item for item in matrix.TEMPLATE_CASES if item.protocol == "MCP")
    record = SimpleNamespace(deployment_id="deployment-1", last_status={})
    payload = {"config": {"protocol": "MCP", "name": "runtime-1"}}
    status = {
        "status": "succeeded",
        "runtime_protocol": "MCP",
        "runtime_id": "runtime-id-1",
        "runtime_endpoint": "https://runtime.example.test",
        "runtime_arn": "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/runtime-id-1",
        "created_resources": [{"type": "agent_runtime", "id": "runtime-id-1"}],
    }
    cleanup = _Cleanup()
    budget = _Budget()
    observed: list[str] = []
    monkeypatch.setattr(matrix, "_deploy_template", lambda *_args, **_kwargs: (record, payload))

    def invoke(*_args: Any, **_kwargs: Any) -> None:
        assert budget.active
        observed.append("invoke")
        if invoke_fails:
            raise RuntimeError("probe failure")

    monkeypatch.setattr(matrix, "_invoke_standalone_mcp", invoke)

    if invoke_fails:
        with pytest.raises(RuntimeError, match="probe failure"):
            matrix.test_gallery_template_deploy_invoke_and_verified_cleanup(
                case,
                object(),
                cleanup,
                budget,
                lambda _deployment_id: status,
            )
    else:
        matrix.test_gallery_template_deploy_invoke_and_verified_cleanup(
            case,
            object(),
            cleanup,
            budget,
            lambda _deployment_id: status,
        )

    assert observed == ["invoke"]
    assert budget.calls == ["authorize:runtime-1", "release:runtime-1"]
    assert budget.active is False
    assert cleanup.calls == ["bind:runtime-id-1"] + ([] if invoke_fails else ["delete"])
