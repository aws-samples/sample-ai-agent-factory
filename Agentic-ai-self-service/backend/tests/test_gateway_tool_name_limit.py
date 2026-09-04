"""Gateway tool names must fit Bedrock's 64-character cap.

Found by invoking a real deployed agent, not by any local test. A LiteLLM MCP
Gateway wired as an mcpServer target settled READY, the agent discovered all 6 of
its tools, and then EVERY invoke returned 500:

    ValidationException: Value
    'mcp-custom-litellm-proxy___aws_knowledge-aws___get_regional_availability'
    at 'toolConfig.tools.2.member.toolSpec.name' failed to satisfy constraint:
    Member must have length less than or equal to 64

Two namespaces stack up: AgentCore Gateway prefixes ``<targetName>___`` onto every
tool it serves, and LiteLLM has already prefixed its own server alias. 72 > 64, and
Bedrock rejects the whole toolConfig — so one over-long name breaks every call the
agent makes, including ones that never touch that tool.

The first fix renamed ``mcp_tool.name`` and still returned 500 in the account, which
is the reason these tests look the way they do. ``strands-agents`` is deliberately
unpinned, so the deployed image installed **1.54** while this repo's environment has
**1.9**, and the two disagree about which attribute the model sees:

    strands 1.9    tool_name -> mcp_tool.name          stream() -> tool_name
    strands 1.54   tool_name -> _agent_tool_name       stream() -> mcp_tool.name

Renaming ``mcp_tool.name`` therefore fixed nothing on 1.54 — it changed the name sent
*upstream* and left the over-long name in the spec. A test that asserted on
``mcp_tool.name`` passed anyway, which is why every assertion below reads
``tool_name``/``tool_spec`` (what Bedrock measures) and why both generations are
exercised explicitly, plus the real installed class as a drift alarm.
"""

import ast
import asyncio
import re

import pytest
from app.services.code_generator import _generate_memory_agent, _generate_strands_gateway

try:
    # Imported at module load deliberately. Another suite replaces httpx with a
    # stub, and mcp's import chain (mcp -> httpx_sse -> httpx.TransportError)
    # then raises, so importing this lazily inside the test passes alone and
    # fails in a full run.
    from mcp.types import Tool as MCPTool
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
except Exception:  # pragma: no cover - the agent's own deps are optional here
    MCPTool = None
    MCPAgentTool = None

REAL_NAME = "mcp-custom-litellm-proxy___aws_knowledge-aws___get_regional_availability"
BODIES = ("strands_gateway", "memory_agent")


def _body(which: str) -> str:
    creds = {
        "url": "https://p.example.com/mcp/",
        "client_id": "",
        "client_secret": "",
        "token_endpoint": "",
        "scope": "",
    }
    if which == "strands_gateway":
        return _generate_strands_gateway("You are helpful.", "us.anthropic.claude-sonnet-5", creds)
    return _generate_memory_agent(
        "You are helpful.", "us.anthropic.claude-sonnet-5", "us-east-1", has_gateway=True, creds=creds
    )


def _emitted_fitter(which: str):
    """The `_fit_tool_names_for_bedrock` the generator actually writes into the
    agent, lifted out and made callable — so these tests cover the shipped text
    rather than a local copy of it that could drift."""
    code = _body(which)
    tree = ast.parse(code)
    src = next(
        (
            ast.get_source_segment(code, n)
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_fit_tool_names_for_bedrock"
        ),
        None,
    )
    assert src, f"{which} does not emit _fit_tool_names_for_bedrock"
    ns: dict = {}
    exec(compile(src, f"<{which}>", "exec"), ns)  # noqa: S102 — the point is to run the emitted text
    return ns["_fit_tool_names_for_bedrock"]


class _McpTool:
    def __init__(self, name):
        self.name = name


class _Client:
    """Records the name the gateway is actually asked for."""

    def __init__(self):
        self.called = []

    def call_tool_sync(self, tool_use_id, name, arguments):
        self.called.append(name)
        return {"status": "success"}

    async def call_tool_async(self, tool_use_id, name, arguments):
        self.called.append(name)
        return {"status": "success"}


class _Tool19:
    """strands ~1.9's MCPAgentTool: one name serves both the model and the wire."""

    def __init__(self, name):
        self.mcp_tool = _McpTool(name)

    @property
    def tool_name(self):
        return self.mcp_tool.name

    @property
    def tool_spec(self):
        return {"name": self.tool_name}

    def invoke(self, client):
        client.call_tool_sync(tool_use_id="t", name=self.tool_name, arguments={})


class _Tool154:
    """strands ~1.54's MCPAgentTool: the model-facing name is a separate attribute
    captured at construction, and the wire name stays ``mcp_tool.name``."""

    def __init__(self, name, name_override=None):
        self.mcp_tool = _McpTool(name)
        self._agent_tool_name = name_override or name

    @property
    def tool_name(self):
        return self._agent_tool_name

    @property
    def tool_spec(self):
        return {"name": self.tool_name}

    def invoke(self, client):
        client.call_tool_sync(tool_use_id="t", name=self.mcp_tool.name, arguments={})


GENERATIONS = {"strands19": _Tool19, "strands154": _Tool154}


@pytest.mark.parametrize("which", BODIES)
@pytest.mark.parametrize("gen", sorted(GENERATIONS))
class TestTheEmittedFitter:
    """Every case runs against both generated agent bodies AND both strands
    generations, because the production 500 was a fix that worked on one of each."""

    def test_a_short_name_is_left_completely_alone(self, which, gen):
        """The overwhelmingly common case — an AgentCore Gateway with ordinary
        target and tool names — must be untouched, including the client, so no
        indirection is added to the default path."""
        fit = _emitted_fitter(which)
        tool = GENERATIONS[gen]("mcp-lambda___get_weather")
        client = _Client()
        out = fit(client, [tool])
        assert out[0].tool_name == "mcp-lambda___get_weather"
        assert out[0].mcp_tool.name == "mcp-lambda___get_weather"
        # The wrapper is installed as an instance attribute shadowing the class
        # method, so its absence from __dict__ is what "unwrapped" means. (A bound
        # method compares unequal to itself on each access, so `is` proves nothing.)
        assert "call_tool_sync" not in client.__dict__, "the client must not be wrapped when nothing was renamed"
        assert "call_tool_async" not in client.__dict__

    def test_the_name_bedrock_measures_comes_under_the_cap(self, which, gen):
        """`tool_spec["name"]` is the field in the ValidationException. Asserting on
        `mcp_tool.name` instead is precisely how the broken fix passed its tests."""
        fit = _emitted_fitter(which)
        assert len(REAL_NAME) > 64, "the fixture stops being the bug if it fits"
        tools = fit(_Client(), [GENERATIONS[gen](REAL_NAME)])
        assert len(tools[0].tool_name) <= 64
        assert len(tools[0].tool_spec["name"]) <= 64
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", tools[0].tool_name), tools[0].tool_name

    def test_the_alias_keeps_the_leaf_tool_name_the_model_needs(self, which, gen):
        """The prefixes are plumbing; ``get_regional_availability`` is what tells
        the model what the tool does. An alias that truncated from the left would
        keep the target name and throw away the meaning."""
        fit = _emitted_fitter(which)
        tools = fit(_Client(), [GENERATIONS[gen](REAL_NAME)])
        assert tools[0].tool_name.startswith("get_regional_availability")

    def test_the_gateway_is_still_called_by_the_name_it_published(self, which, gen):
        """The whole reason renaming is safe. If the alias leaked outbound, the
        gateway would answer 'unknown tool' for every call — trading a
        ValidationException for a broken tool, which is worse because it looks
        like the agent simply chose badly."""
        fit = _emitted_fitter(which)
        client = _Client()
        tools = fit(client, [GENERATIONS[gen](REAL_NAME)])
        tools[0].invoke(client)
        assert client.called == [REAL_NAME]

    def test_a_short_tool_alongside_a_long_one_still_calls_through(self, which, gen):
        """Mixed lists are the norm: any wrapper installed for the long tool must
        pass other names through unchanged rather than dropping them."""
        fit = _emitted_fitter(which)
        client = _Client()
        short = GENERATIONS[gen]("mcp-lambda___short")
        fit(client, [GENERATIONS[gen](REAL_NAME), short])
        short.invoke(client)
        assert client.called == ["mcp-lambda___short"]

    def test_two_targets_sharing_a_leaf_name_stay_distinct(self, which, gen):
        """Two MCP servers behind one gateway can both expose ``read_documentation``.
        Aliasing on the leaf alone would collide them into one tool and route half
        the calls to the wrong server."""
        fit = _emitted_fitter(which)
        a = "mcp-target-number-one-with-a-long-name___srv-a___read_documentation"
        b = "mcp-target-number-two-with-a-long-name___srv-b___read_documentation"
        client = _Client()
        tools = fit(client, [GENERATIONS[gen](a), GENERATIONS[gen](b)])
        assert tools[0].tool_name != tools[1].tool_name
        tools[1].invoke(client)
        assert client.called == [b]

    def test_the_alias_is_deterministic_across_sessions(self, which, gen):
        """The MCP session is recreated on retry and on every cold start. A random
        alias would rename tools underneath a conversation mid-flight."""
        fit = _emitted_fitter(which)
        first = fit(_Client(), [GENERATIONS[gen](REAL_NAME)])[0].tool_name
        second = fit(_Client(), [GENERATIONS[gen](REAL_NAME)])[0].tool_name
        assert first == second


@pytest.mark.parametrize("which", BODIES)
def test_a_name_that_cannot_be_shortened_is_not_left_silent(which):
    """A future strands could move the model-facing name a third time. Then the
    rename fails, every invoke 500s, and the only thing standing between that and a
    silent outage is this log line — so the fitter must verify its own effect."""
    fit = _emitted_fitter(which)

    class _Immovable:
        """tool_name ignores writes, the way a third generation's private
        attribute would."""

        def __init__(self):
            self.mcp_tool = _McpTool(REAL_NAME)

        @property
        def tool_name(self):
            return REAL_NAME

    logged = []

    class _Spy:
        def error(self, msg, *args):
            logged.append(msg % args)

        def warning(self, *a, **k):
            pass

    import logging

    real = logging.getLogger("agentcore.gateway")
    orig = logging.getLogger
    logging.getLogger = lambda name=None: _Spy() if name == "agentcore.gateway" else orig(name)  # type: ignore[assignment]
    try:
        fit(_Client(), [_Immovable()])
    finally:
        logging.getLogger = orig  # type: ignore[assignment]
        assert real is logging.getLogger("agentcore.gateway")
    assert logged, "an unshortenable name must be reported at ERROR, not swallowed"
    assert "64" in logged[0] and "every invocation will fail" in logged[0]


@pytest.mark.parametrize("which", BODIES)
def test_the_real_installed_strands_is_handled(which):
    """The drift alarm. The fakes above encode two known generations; this asserts
    the fitter still works against whatever ``strands-agents`` is actually resolved
    here, so a third shape fails in CI instead of in a deployed agent."""
    if MCPAgentTool is None:
        pytest.skip("strands-agents / mcp are the generated agent's deps, not the backend's")

    fit = _emitted_fitter(which)
    client = _Client()
    tool = MCPAgentTool(
        mcp_tool=MCPTool(name=REAL_NAME, description="d", inputSchema={"type": "object", "properties": {}}),
        mcp_client=client,
    )
    tools = fit(client, [tool])

    assert len(tools[0].tool_spec["name"]) <= 64, "the model-facing name must fit whatever generation is installed"
    asyncio.run(_drain(tools[0], client))
    assert client.called == [REAL_NAME], "the gateway must still be called by the name it published"


async def _drain(tool, client):
    async for _ in tool.stream({"toolUseId": "t", "name": tool.tool_name, "input": {}}, {}):
        pass


@pytest.mark.parametrize("which", BODIES)
def test_the_discovery_path_actually_calls_the_fitter(which):
    """Guards the wiring, not the helper: an emitted-but-uncalled function would
    pass every test above and still ship the 500."""
    code = _body(which)
    assert "return _fit_tool_names_for_bedrock(client, tools)" in code
