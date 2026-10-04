"""Classify generated runtime source and select its dependency bundle.

The live Step Functions path and the customer CloudFormation export both ship
the generated Python module as a pre-built zip.  Selecting by template id is
unsafe: template metadata is caller-controlled and can drift from the source
that will actually import inside the container.  This module derives the
artifact shape from Python imports so both shipping paths can share one
fail-closed decision.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

BASE_BUNDLE_KEY = "agentcore-deps/base.zip"
STRANDS_BUNDLE_KEY = "agentcore-deps/strands-mcp.zip"
MCP_LEAN_BUNDLE_KEY = "agentcore-deps/mcp-lean.zip"

RuntimeArtifactKind = Literal["base", "strands", "mcp"]


class RuntimeArtifactError(ValueError):
    """Generated source cannot be mapped to a supported runtime artifact."""


@dataclass(frozen=True)
class RuntimeArtifact:
    """Server-derived dependency requirements for one generated module."""

    kind: RuntimeArtifactKind
    bundle_key: str
    model_provider_applicable: bool


def _imported_modules(source: str) -> frozenset[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeArtifactError(
            "Generated runtime source is not valid Python; refusing to select a dependency bundle."
        ) from exc

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return frozenset(modules)


def classify_runtime_artifact(source: str) -> RuntimeArtifact:
    """Return the only supported artifact shape implied by ``source``.

    Strands agents frequently import the MCP client as well, so Strands must
    take precedence over MCP.  Any non-Strands module importing ``mcp`` needs
    the lean MCP bundle; treating it as ``base`` would produce a green deploy
    whose container immediately fails with ``ModuleNotFoundError``.
    """

    modules = _imported_modules(source)
    uses_strands = any(module == "strands" or module.startswith("strands.") for module in modules)
    uses_mcp = any(module == "mcp" or module.startswith("mcp.") for module in modules)

    if uses_strands:
        return RuntimeArtifact(
            kind="strands",
            bundle_key=STRANDS_BUNDLE_KEY,
            model_provider_applicable=True,
        )
    if uses_mcp:
        return RuntimeArtifact(
            kind="mcp",
            bundle_key=MCP_LEAN_BUNDLE_KEY,
            model_provider_applicable=False,
        )
    return RuntimeArtifact(
        kind="base",
        bundle_key=BASE_BUNDLE_KEY,
        model_provider_applicable=False,
    )
