"""The infra's tool-Lambda name prefix is the backend's, to the character (F-7d).

``stacks/platform/tool_lambda_names.py`` grants the gateway-step role
``function:AgentCore-<token>-*`` per supported region. The backend names every tool Lambda
with ``services/naming.py``. If the two formulas ever drift -- a different hash length, a
different separator, a region missing from one list -- the grant stops matching the names
the code creates and every gateway deploy fails on CreateFunction, or a region the API
accepts has no grant at all. Both are pinned here by computing each side with its OWN code
(the backend module loaded from the backend tree, never re-implemented) and comparing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

from stacks.platform import tool_lambda_names

_REPO = pathlib.Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend" / "src" / "app" / "services"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_token_formula_is_the_backends():
    naming = _load("backend_naming", _BACKEND / "naming.py")
    for project, env, region in (
        ("acf", "test", "us-east-1"),
        ("acfe2e", "p0920", "eu-central-1"),
        ("p", "e", "sa-east-1"),
    ):
        stack_identity = f"{project}-{env}-{region}"
        assert tool_lambda_names.stack_scope_token(project, env, region) == naming.stack_scope_token(stack_identity)
        assert tool_lambda_names.function_name_prefix(project, env, region) == naming.function_name_prefix(
            stack_identity
        )
        # And every backend-generated name of every kind falls under the granted prefix.
        prefix = tool_lambda_names.function_name_prefix(project, env, region)
        for kind, suffix in (
            ("DynamicTools", ""),
            ("CustomerSupportTools", ""),
            ("KBTool", "0" * 12),
            ("CustomTool", "t-" + "0" * 12),
        ):
            assert naming.scoped_function_name(kind, stack_identity, suffix).startswith(prefix)
    assert tool_lambda_names.STACK_SCOPE_TOKEN_LEN == naming.STACK_SCOPE_TOKEN_LEN
    assert tool_lambda_names.FUNCTION_NAME_PREFIX == naming.FUNCTION_NAME_PREFIX


def test_the_supported_region_list_is_the_backends():
    """``VALID_AWS_REGIONS`` is read off the backend source, not imported: the deployment
    module pulls boto3 and the whole service graph at import time."""
    src = (_BACKEND / "deployment.py").read_text()
    block = re.search(r"^VALID_AWS_REGIONS = \[(.*?)^\]", src, re.S | re.M)
    assert block, "VALID_AWS_REGIONS not found in backend deployment.py"
    backend_regions = re.findall(r'"([a-z]{2}-[a-z]+-\d)"', block.group(1))
    assert backend_regions, "no regions parsed"
    assert list(tool_lambda_names.SUPPORTED_TARGET_REGIONS) == backend_regions, (
        f"infra={list(tool_lambda_names.SUPPORTED_TARGET_REGIONS)} backend={backend_regions}: a region the API "
        "accepts without a Lambda grant fails every gateway deploy there; a grant for a region the API "
        "refuses is authority nothing uses"
    )
