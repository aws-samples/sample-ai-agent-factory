"""The MCP protocol versions every AgentCore Gateway this platform builds must speak (F-62).

A gateway created without ``protocolConfiguration`` reports it as ``None`` and negotiates
only 2025-03-26: a client asking for 2025-06-18 or 2025-11-25 is silently downgraded
(measured live, 2026-09-22). Its protocol contract is then whatever the service default
is that day, and no deploy can reproduce it. So every create, adoption and CFN export
pins the set explicitly.

``update_gateway`` is a full replace: omitting ``protocolConfiguration`` resets it to
``None`` (also measured). An update must therefore send the pinned set merged over what
the gateway already has, so a session or streaming setting survives it.

2025-03-26 stays in the set: generated clients and the MCP-server prewarm
(``step_handlers/mcp_server_step.py``) still initialize with it.

2026-07-28 is deliberately NOT in the set, although the service accepts it. The
2026-07-28 schema says servers implementing that version MUST include ``resultType`` in
every result, and a pinned gateway's ``server/discover`` omits it (measured live,
2026-09-22; its tools/list and tools/call did carry it). Advertising the version would
be a conformance claim the gateway cannot back. Add it back only once
``scripts/verify-mcp-protocol.py`` passes 2026-07-28 against a live gateway.
"""

from __future__ import annotations

from typing import Any

#: A subset of what the service accepts at create; any other value is rejected there
#: ("Unsupported MCP Version(s)"). See the module docstring for why 2026-07-28 is absent.
MCP_SUPPORTED_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26")


def pinned_protocol_configuration(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """``protocolConfiguration`` for create/update: the pinned versions over *existing*.

    Every other ``mcp`` field already on the gateway (search type, instructions, session
    and streaming settings) is carried over unchanged.
    """
    mcp = dict(((existing or {}).get("mcp")) or {})
    mcp["supportedVersions"] = list(MCP_SUPPORTED_VERSIONS)
    return {"mcp": mcp}
