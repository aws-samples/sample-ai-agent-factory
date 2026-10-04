"""Which gallery templates can compose with other connected components.

One rule, read by both ``generate_agent_code`` and the ``DeployRequest`` validator, so
the request boundary refuses exactly what code generation would refuse -- before Step
Functions provisions a Memory, Gateway or Knowledge Base the agent would never use.

The four gateway templates generate the same Strands MCP agent as the unified gateway
path, so a connected Memory, Browser, Code Interpreter or Knowledge Base composes into
it. Each template also implies the components it advertises -- Gateway for all four,
plus Memory for the two customer-support templates -- exactly as the gallery, the
CloudFormation generator and the Python exporter already read them, so a live deploy
never needs a redundant ``connectedTools`` entry to keep them. The two standalone templates generate a different program shape, and anything
connected beside them is refused by name rather than silently left out.
"""

from __future__ import annotations

GATEWAY_TEMPLATE_IDS = frozenset(
    {
        "strands-gateway-agent",
        "mcp-server-gateway-target",
        "customer-support-assistant",
        "customer-support-blueprint",
    }
)

_CUSTOMER_SUPPORT_TEMPLATE_IDS = frozenset({"customer-support-assistant", "customer-support-blueprint"})

COMPOSABLE_CAPABILITIES = frozenset({"memory", "gateway", "browser", "code_interpreter", "knowledge_base"})

_STANDALONE_TEMPLATE_SHAPES = {
    "web-search-agent": "a LangChain web-search agent with its own search tool",
    "mcp-server-runtime": "an MCP server that exposes tools rather than an agent that calls them",
}


def template_implied_capabilities(template_id: str | None) -> frozenset[str]:
    """The components ``template_id`` advertises, which it keeps whatever else is connected."""
    implied: set[str] = set()
    if template_id in GATEWAY_TEMPLATE_IDS:
        implied.add("gateway")
    if template_id in _CUSTOMER_SUPPORT_TEMPLATE_IDS:
        implied.add("memory")
    return frozenset(implied)


def template_composition_refusal(template_id: str | None, capabilities) -> str | None:
    """The refusal for ``template_id`` plus ``capabilities``, or None when they compose."""
    shape = _STANDALONE_TEMPLATE_SHAPES.get(template_id or "")
    conflicting = sorted(set(capabilities) & COMPOSABLE_CAPABILITIES)
    if not shape or not conflicting:
        return None
    names = ", ".join(capability.replace("_", " ") for capability in conflicting)
    return (
        f"Template {template_id!r} generates {shape}, which cannot also use the connected "
        f"capabilities ({names}). Disconnect them or start from a different template; "
        "none will be silently omitted."
    )
