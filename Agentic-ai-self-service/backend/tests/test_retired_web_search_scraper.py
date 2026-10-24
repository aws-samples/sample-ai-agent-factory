"""The unofficial full-web scraper must never return to a shipped artifact."""

from pathlib import Path

from app.models.deployment_models import RuntimeConfig
from app.services import code_generator, codegen_templates, deployment, gateway_deployer


def _config(protocol: str = "HTTP") -> RuntimeConfig:
    return RuntimeConfig(
        name="retired-search-probe",
        protocol=protocol,
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt="Use available tools and cite managed search sources.",
    )


def test_dynamic_tool_artifact_contains_no_full_web_scraper_or_endpoint() -> None:
    source = codegen_templates.dynamic_tools_lambda_source()

    assert "_do_duckduckgo_search" not in source
    assert "html.duckduckgo.com" not in source
    assert "lite.duckduckgo.com" not in source
    assert "duckduckgo_search" not in source
    assert "_do_wikipedia_search" in source
    assert "_do_weather" in source
    assert "_do_fetch_webpage" in source
    compile(source, "<dynamic_tools_lambda>", "exec")


def test_web_search_gallery_agent_uses_gateway_discovery_not_embedded_scraping() -> None:
    source = code_generator.generate_agent_code(_config(), template_id="web-search-agent")

    assert "MCPClient" in source
    assert "GATEWAY_URL" in source
    assert "_do_duckduckgo_search" not in source
    assert "duckduckgo.com" not in source
    compile(source, "<managed_web_search_agent>", "exec")


def test_legacy_gateway_id_is_removed_before_lambda_schema_resolution() -> None:
    assert "duckduckgo_search" not in gateway_deployer.GATEWAY_TOOL_SCHEMAS
    assert gateway_deployer.without_managed_web_search_tools(["duckduckgo_search", "weather_api", "web_search"]) == [
        "weather_api"
    ]


def test_standalone_mcp_legacy_search_without_code_is_rejected_clearly() -> None:
    problems = deployment.validate_mcp_server_tools(["duckduckgo_search"])

    assert len(problems) == 1
    assert "retired scraper alias" in problems[0]
    assert "managed web_search" in problems[0]


def test_standalone_mcp_allows_a_user_supplied_legacy_named_implementation() -> None:
    source = deployment.generate_mcp_server_code(
        tools=[
            {
                "name": "duckduckgo_search",
                "description": "Use the customer's supported search service.",
                "code": "def duckduckgo_search(query: str) -> str:\n    return 'customer:' + query",
            }
        ]
    )

    assert "return 'customer:' + query" in source
    assert "html.duckduckgo.com" not in source
    assert (
        deployment.validate_mcp_server_tools(
            [
                {
                    "name": "duckduckgo_search",
                    "code": "def duckduckgo_search(query: str) -> str:\n    return query",
                }
            ]
        )
        == []
    )
    compile(source, "<customer_supplied_search>", "exec")


def test_canonical_template_source_itself_has_no_provider_endpoint() -> None:
    path = Path(codegen_templates.__file__).with_name("dynamic_tools_impl.py")
    source = path.read_text(encoding="utf-8")

    assert "duckduckgo.com" not in source
    assert "_do_duckduckgo_search" not in source
