"""``RBAC_ENFORCE`` must be settable through deploy.sh, not only via raw cdk.

Scope enforcement ships advisory: ``require_scopes`` logs a would-deny and allows
unless ``RBAC_ENFORCE`` is truthy, which is the documented rollout in
docs/RBAC_ROLLOUT.md. Verified live — an authenticated caller holding no Cognito
groups still read ``GET /api/registry/litellm-config`` on the deployed stack.

Turning it on therefore has to be reachable from the supported entry point. Before
this passthrough existed, the doc's own instruction (``cdk deploy -c
rbac_enforce=true``) bypassed deploy.sh — and with it the ``COGNITO_USERS``
carry-forward guard, so hardening RBAC would have deleted every provisioned user
as a side effect. That is the failure these tests pin.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOY_SH = _ROOT / "scripts" / "deploy.sh"
_ROLLOUT_DOC = _ROOT / "docs" / "RBAC_ROLLOUT.md"


def _deploy_sh() -> str:
    return _DEPLOY_SH.read_text()


def test_deploy_sh_forwards_rbac_enforce_context() -> None:
    """The cdk invocation passes the flag the stack reads (lambdas.py rbac_enforce)."""
    assert re.search(r'-c\s+rbac_enforce="\$\{RBAC_ENFORCE\}"', _deploy_sh()), (
        "deploy.sh must forward -c rbac_enforce so RBAC can be enforced without bypassing the COGNITO_USERS guard"
    )


def test_rbac_enforce_defaults_to_empty_not_true() -> None:
    """Advisory stays the default: an operator who sets nothing changes nothing."""
    assert re.search(r'^RBAC_ENFORCE="\$\{RBAC_ENFORCE:-\}"', _deploy_sh(), re.MULTILINE), (
        "RBAC_ENFORCE must default to empty; the stack turns empty into 'false'"
    )


def test_empty_value_cannot_read_as_enforcing() -> None:
    """The stack's ``or "false"`` is what makes an empty passthrough safe.

    Asserted here rather than in the stack tests because the two halves only
    compose correctly together: deploy.sh always passes the flag, so the stack
    receives "" on every ordinary deploy and must not treat that as truthy.
    """
    lambdas = (_ROOT / "infra" / "stacks" / "platform" / "lambdas.py").read_text()
    assert 'try_get_context("rbac_enforce") or "false"' in lambdas


def test_rollout_doc_prescribes_deploy_sh_and_warns_off_raw_cdk() -> None:
    """The doc used to *instruct* `Redeploy with -c rbac_enforce=true`.

    Asserted on the prescriptive sentence rather than on the bare flag: the
    warning against raw cdk necessarily quotes the flag, so a blanket
    "flag must not appear" check fails on the fix itself.
    """
    doc = _ROLLOUT_DOC.read_text()
    assert "RBAC_ENFORCE=true ./scripts/deploy.sh" in doc
    assert "Redeploy with `-c rbac_enforce=true`" not in doc
    assert "COGNITO_USERS" in doc, "the doc must say why raw cdk is unsafe here"
