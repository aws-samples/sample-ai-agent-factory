"""The bundled `mcp` major version must match the API the codegen emits.

`scripts/install-agentcore-deps.sh` installed `mcp` unpinned, so the runtime
bundles floated to mcp 2.x (2.1.1 in strands-mcp.zip and 2.2.0 in mcp-lean.zip
from a *single* build run) while every string the code generators emit stayed on
the mcp 1.x API. 2.x renamed both entry points with no back-compat alias:

    mcp.client.streamable_http.streamablehttp_client -> streamable_http_client
    mcp.server.fastmcp.FastMCP                      -> mcp.server.mcpserver.MCPServer

The generated container therefore died at import. Nothing surfaced that to the
user except InvokeAgentRuntime reporting "Runtime initialization time exceeded.
Please make sure that initialization completes in 30s", which reads as a
cold-start problem and hides the real cause. Unit tests could not catch it
either: the dev virtualenv had mcp 1.23.3, so the emitted imports resolved fine
locally and only the deployed bundle was broken.

These tests tie the two together, so bumping one without the other fails here
instead of in a live deployment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPS_SCRIPT = REPO_ROOT / "scripts" / "install-agentcore-deps.sh"
CODEGEN_SOURCES = (
    REPO_ROOT / "backend" / "src" / "app" / "services" / "code_generator.py",
    REPO_ROOT / "backend" / "src" / "app" / "services" / "deployment.py",
)

# Import spellings that exist only in mcp 1.x.
V1_ONLY_IMPORTS = ("streamablehttp_client", "mcp.server.fastmcp")


@pytest.fixture(scope="module")
def deps_script() -> str:
    return DEPS_SCRIPT.read_text()


@pytest.fixture(scope="module")
def codegen_text() -> str:
    return "\n".join(p.read_text() for p in CODEGEN_SOURCES)


def test_codegen_still_emits_the_v1_api(codegen_text: str) -> None:
    """Guard the premise of the pin below.

    If this fails, the generators were migrated to mcp 2.x — then the pin in
    install-agentcore-deps.sh must be raised and this whole module rewritten.
    """
    still_v1 = [name for name in V1_ONLY_IMPORTS if name in codegen_text]
    assert still_v1, (
        "no mcp 1.x-only import spelling found in the code generators; if they were "
        f"migrated to mcp 2.x, raise the mcp pin in {DEPS_SCRIPT.name} and update this test"
    )


def test_every_mcp_install_is_pinned(deps_script: str) -> None:
    """No install_packages call may pass a bare, unpinned `mcp`."""
    offenders = [
        line.strip()
        for line in deps_script.splitlines()
        if "install_packages" in line and re.search(r"(^|\s)mcp(\s|$)", line)
    ]
    assert not offenders, f"unpinned bare 'mcp' passed to install_packages: {offenders}"


def test_pin_excludes_mcp_2(deps_script: str, codegen_text: str) -> None:
    """While the generators emit the v1 API, the bundle must resolve to mcp 1.x."""
    if not any(name in codegen_text for name in V1_ONLY_IMPORTS):
        pytest.skip("generators no longer emit the mcp 1.x API")
    assert 'mcp_pin="mcp<2"' in deps_script, (
        "the code generators emit the mcp 1.x API, so the bundles must pin mcp<2; "
        f'expected mcp_pin="mcp<2" in {DEPS_SCRIPT.name}'
    )
