"""Bug 173: the generated MCP server must bind port 8000.

AgentCore Runtime with serverProtocol=MCP proxies the container ingress to port
8000 (the documented MCP-runtime contract, matching the AWS agentcore-samples
MCP-server-as-a-target workshop which uses the FastMCP default 8000). Binding
8080 left the server unreachable behind the runtime's MCP ingress, so the
Gateway's tool-discovery probe timed out ("Runtime initialization time exceeded
... 30s") and the gateway served 0 tools. These tests pin the contract.
"""

from __future__ import annotations

from app.services.deployment import generate_mcp_server_code


def test_mcp_server_binds_port_8000():
    code = generate_mcp_server_code(server_name="t", tools=["get_order"])
    assert 'os.environ.get("PORT", "8000")' in code
    assert '"8080"' not in code  # must not default to the wrong port


def test_mcp_server_uses_streamable_http_and_all_interfaces():
    code = generate_mcp_server_code(server_name="t", tools=["get_order"])
    assert 'host="0.0.0.0"' in code
    assert 'transport="streamable-http"' in code
    assert "stateless_http=True" in code


def test_mcp_server_exposes_requested_tool():
    code = generate_mcp_server_code(server_name="t", tools=["get_order"])
    assert "@mcp.tool()" in code
    assert "def get_order" in code


# --- Bug 174: custom-tool shapes must all register a real tool --------------

import ast


def _assert_registers(code: str, fn_name: str):
    assert "@mcp.tool()" in code, "no tool registered"
    assert f"def {fn_name}" in code, f"{fn_name} not defined"
    ast.parse(code)  # must be valid python


def test_custom_tool_name_plus_code_full_def():
    """{name, description, code=<full def>} — the shape callers/tests send."""
    code = generate_mcp_server_code(server_name="t", tools=[{
        "name": "get_canary",
        "description": "Returns the canary token",
        "code": 'def get_canary() -> str:\n    """Return it."""\n    return "CANARY-Z"',
    }])
    _assert_registers(code, "get_canary")
    assert "CANARY-Z" in code
    assert "no tools" not in code  # sanity


def test_custom_tool_toolname_plus_implementation_body():
    """Legacy shape {toolName, implementation=<body>} still works."""
    code = generate_mcp_server_code(server_name="t", tools=[{
        "toolName": "do_thing",
        "description": "does a thing",
        "implementation": "return 'done'",
    }])
    _assert_registers(code, "do_thing")
    assert "return 'done'" in code


def test_custom_tool_name_plus_code_body_only():
    """{name, code=<body, no def>} — code treated as the function body."""
    code = generate_mcp_server_code(server_name="t", tools=[{
        "name": "calc",
        "code": "return str(2 + 2)",
    }])
    _assert_registers(code, "calc")


def test_custom_tools_never_emit_empty_server():
    """Any non-empty tools input must register at least one @mcp.tool()."""
    for tools in (
        [{"name": "a", "code": "def a() -> str:\n    return 'x'"}],
        [{"toolName": "b", "implementation": "return 'y'"}],
        ["get_order"],
    ):
        code = generate_mcp_server_code(server_name="t", tools=tools)
        assert code.count("@mcp.tool()") >= 1
