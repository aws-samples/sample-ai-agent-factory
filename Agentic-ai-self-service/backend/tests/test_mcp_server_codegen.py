"""Bug 173: the generated MCP server must bind port 8000.

AgentCore Runtime with serverProtocol=MCP proxies the container ingress to port
8000 (the documented MCP-runtime contract, matching the AWS agentcore-samples
MCP-server-as-a-target workshop which uses the FastMCP default 8000). Binding
8080 left the server unreachable behind the runtime's MCP ingress, so the
Gateway's tool-discovery probe timed out ("Runtime initialization time exceeded
... 30s") and the gateway served 0 tools. These tests pin the contract.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
from unittest import mock

import pytest
from app.services import deployment
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


def _assert_registers(code: str, fn_name: str):
    assert "@mcp.tool()" in code, "no tool registered"
    assert f"def {fn_name}" in code, f"{fn_name} not defined"
    ast.parse(code)  # must be valid python


def test_custom_tool_name_plus_code_full_def():
    """{name, description, code=<full def>} — the shape callers/tests send."""
    code = generate_mcp_server_code(
        server_name="t",
        tools=[
            {
                "name": "get_canary",
                "description": "Returns the canary token",
                "code": 'def get_canary() -> str:\n    """Return it."""\n    return "CANARY-Z"',
            }
        ],
    )
    _assert_registers(code, "get_canary")
    assert "CANARY-Z" in code
    assert "no tools" not in code  # sanity


def test_custom_tool_toolname_plus_implementation_body():
    """Legacy shape {toolName, implementation=<body>} still works."""
    code = generate_mcp_server_code(
        server_name="t",
        tools=[
            {
                "toolName": "do_thing",
                "description": "does a thing",
                "implementation": "return 'done'",
            }
        ],
    )
    _assert_registers(code, "do_thing")
    assert "return 'done'" in code


def test_custom_tool_name_plus_code_body_only():
    """{name, code=<body, no def>} — code treated as the function body."""
    code = generate_mcp_server_code(
        server_name="t",
        tools=[
            {
                "name": "calc",
                "code": "return str(2 + 2)",
            }
        ],
    )
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


class TestEveryToolCarriesADescription:
    """A tool with no docstring has no description, and the gateway invents one.

    Observed live against a deployed export: the gateway advertised "Tool which
    performs MCPServerRuntime___msv_probe_marker" for a tool whose function had no
    docstring. FastMCP reads the description from the docstring, so an empty one is
    not cosmetic -- that fabricated sentence is what the model reads when it decides
    whether the tool is the right one to call.

    Only the verbatim-code path had this. A tool defined as {name, description,
    implementation} always got the description emitted as its docstring; a tool
    supplying a complete ``def`` was emitted untouched, so the canvas's description
    was silently dropped on the floor.
    """

    @staticmethod
    def _node(code, name):
        tree = ast.parse(code)
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name)

    @classmethod
    def _docstring(cls, code, name):
        return ast.get_docstring(cls._node(code, name))

    @classmethod
    def _defined_function(cls, code, name):
        """The generated tool as a callable, without importing the whole module.

        Executing the module itself would need FastMCP and a port; the question here is
        only whether the function body survived being rewritten. ``get_source_segment``
        starts at the ``def``, so the ``@mcp.tool()`` decorator is excluded and no MCP
        machinery is needed.
        """
        segment = ast.get_source_segment(code, cls._node(code, name))
        namespace: dict = {}
        exec(segment, namespace)  # noqa: S102 - generated code under test, not input
        return namespace[name]

    def test_a_verbatim_def_gets_the_canvas_description(self):
        code = generate_mcp_server_code(
            server_name="t",
            tools=[
                {
                    "toolName": "lookup_widget",
                    "description": "Looks up a widget by id.",
                    "code": "def lookup_widget(widget_id: str) -> str:\n    return widget_id\n",
                }
            ],
        )
        assert self._docstring(code, "lookup_widget") == "Looks up a widget by id."

    def test_the_authors_own_docstring_wins(self):
        """It was written against this function; the canvas field is a label."""
        code = generate_mcp_server_code(
            server_name="t",
            tools=[
                {
                    "toolName": "own_doc",
                    "description": "generic canvas text",
                    "code": 'def own_doc() -> str:\n    """Author wins."""\n    return "x"\n',
                }
            ],
        )
        assert self._docstring(code, "own_doc") == "Author wins."

    def test_a_multiline_signature_is_not_broken_by_the_insertion(self):
        """The docstring goes before the first statement, not after the first line.

        A signature wrapped over several lines is the case that a naive
        "insert after the def line" would turn into a syntax error, which would take
        down the whole generated server rather than one tool.
        """
        code = generate_mcp_server_code(
            server_name="t",
            tools=[
                {
                    "toolName": "wrapped",
                    "description": "Wrapped signature.",
                    "code": "def wrapped(\n    a: str,\n    b: int = 3,\n) -> str:\n    return a\n",
                }
            ],
        )
        compile(code, "<mcp>", "exec")
        assert self._docstring(code, "wrapped") == "Wrapped signature."

    def test_an_async_tool_is_handled_too(self):
        code = generate_mcp_server_code(
            server_name="t",
            tools=[
                {
                    "toolName": "fetch_it",
                    "description": "Fetches it.",
                    "code": "async def fetch_it(url: str) -> str:\n    return url\n",
                }
            ],
        )
        compile(code, "<mcp>", "exec")
        assert self._docstring(code, "fetch_it") == "Fetches it."

    # Shapes where the function body does not begin on its own line. Plausible from a
    # canvas -- a one-line tool is the first thing anyone writes -- and the case that
    # "insert a line in front of the first statement" gets wrong, because there is no
    # line in front of it. The docstring landed above the `def` at the body's column and
    # the module failed to compile with `unexpected indent`, so one tool written on one
    # line took down every tool in the export.
    ONE_LINE_BODIES = [
        "def quick() -> str: return 'x'",
        "def quick(): return 'x'",
        "async def quick() -> str: return 'x'",
        "def quick() -> str: a = 'x'; return a",  # semicolons must survive the split
    ]

    @pytest.mark.parametrize("code", ONE_LINE_BODIES)
    def test_a_body_on_the_def_line_still_compiles_and_keeps_its_description(self, code):
        generated = generate_mcp_server_code(
            server_name="t",
            tools=[{"toolName": "quick", "description": "Quick.", "code": code}],
        )
        compile(generated, "<mcp>", "exec")  # the whole module
        assert self._docstring(generated, "quick") == "Quick."

    @pytest.mark.parametrize("code", ONE_LINE_BODIES)
    def test_splitting_the_line_does_not_change_what_the_tool_does(self, code):
        """Rewriting someone's function is only acceptable if it still behaves.

        Compiling proves the module loads; it does not prove the body survived the
        split. So run the function and check its answer, which is the thing a caller
        would notice if a statement were dropped or re-indented into the wrong block.
        """
        generated = generate_mcp_server_code(
            server_name="t",
            tools=[{"toolName": "quick", "description": "Quick.", "code": code}],
        )
        func = self._defined_function(generated, "quick")
        result = asyncio.run(func()) if inspect.iscoroutinefunction(func) else func()
        assert result == "x"

    def test_unparseable_code_is_left_exactly_as_it_was(self):
        """Improving a description must never be the reason an export fails.

        Code that does not parse is a pre-existing problem with its own handling;
        this feature has no business turning it into a different error.
        """
        broken = "def broken(:\n    ???\n"
        code = generate_mcp_server_code(
            server_name="t",
            tools=[{"toolName": "broken", "description": "d", "code": broken}],
        )
        assert broken.strip() in code

    def test_a_tool_with_no_description_anywhere_warns(self, caplog):
        """Nothing can be invented for it, so the export says so rather than
        shipping a tool the model cannot choose sensibly."""
        with caplog.at_level(logging.WARNING, logger="app.services.deployment"):
            code = generate_mcp_server_code(
                server_name="t",
                tools=[{"toolName": "no_desc", "code": "def no_desc() -> str:\n    return 'x'\n"}],
            )
        assert self._docstring(code, "no_desc") is None, "there is nothing truthful to put here"
        assert "neither a docstring nor a description" in caplog.text
        assert "no_desc" in caplog.text

    # Descriptions come from a free-text canvas field, so every character in them is
    # attacker-adjacent at best and typo-adjacent always. A description that breaks the
    # docstring literal does not break one tool: the module fails to import and the
    # server serves nothing.
    HOSTILE_DESCRIPTIONS = [
        'Looks up the order by "id"',  # trailing quote closes the literal early
        'Ends with a quote"',
        'Says """hello""" loudly',  # an embedded triple quote
        "Windows path C:\\temp\\",  # trailing backslash escapes the closing quote
        'Both \\ and " together',
        "Line one\nLine two",  # a real newline, which is legal and must stay
        'A "quoted" phrase mid-sentence',
    ]

    @pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
    def test_a_description_cannot_break_the_generated_module(self, description):
        """Both emission paths, because they used to escape differently.

        The verbatim-``def`` path is the new one; the ``implementation`` path has
        emitted the description as a docstring since long before it, with the same
        incomplete escaping.
        """
        for tool in (
            {"toolName": "verbatim", "description": description, "code": "def verbatim() -> str:\n    return 'x'\n"},
            {"toolName": "from_body", "description": description, "implementation": "return 'x'"},
        ):
            code = generate_mcp_server_code(server_name="t", tools=[tool])
            compile(code, "<mcp>", "exec")  # the whole module, not just the tool

    @pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
    def test_the_description_reaches_the_model_unchanged(self, description):
        """Escaping must survive into ``__doc__``, or the fix traded a crash for a lie.

        FastMCP advertises the docstring as the tool's description, so what matters is
        not that the source compiles but that the string it compiles to is the one the
        author typed -- escapes resolved, nothing dropped.
        """
        code = generate_mcp_server_code(
            server_name="t",
            tools=[
                {
                    "toolName": "verbatim",
                    "description": description,
                    "code": "def verbatim() -> str:\n    return 'x'\n",
                }
            ],
        )
        assert self._docstring(code, "verbatim") == description

    def test_a_documented_tool_does_not_warn(self):
        # The precondition that makes the test above mean something.
        with mock.patch.object(deployment.logger, "warning") as warned:
            generate_mcp_server_code(
                server_name="t",
                tools=[
                    {
                        "toolName": "fine",
                        "description": "Fine.",
                        "code": "def fine() -> str:\n    return 'x'\n",
                    }
                ],
            )
        assert not warned.called, warned.call_args_list
