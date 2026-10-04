"""``RBAC_ENFORCE`` must be settable through deploy.sh, not only via raw cdk.

Scope enforcement is on by default; ``RBAC_ENFORCE=false`` is the advisory escape
hatch for an upgrade whose existing users have no group yet (docs/RBAC_ROLLOUT.md).
While it shipped advisory, an authenticated caller holding no Cognito groups read
``GET /api/registry/litellm-config`` on the deployed stack.

Either setting has to be reachable from the supported entry point. Before this
passthrough existed, the doc's own instruction (``cdk deploy -c rbac_enforce=...``)
bypassed deploy.sh — and with it the ``COGNITO_USERS`` carry-forward guard, so
changing RBAC would have deleted every provisioned user as a side effect. That is
the failure these tests pin.
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


def test_rbac_enforce_defaults_to_empty() -> None:
    """An operator who sets nothing passes "", which the stack reads as enforcing."""
    assert re.search(r'^RBAC_ENFORCE="\$\{RBAC_ENFORCE:-\}"', _deploy_sh(), re.MULTILINE), (
        "RBAC_ENFORCE must default to empty; the stack turns empty into 'true'"
    )


def test_empty_value_reads_as_enforcing() -> None:
    """The stack's ``or "true"`` is what makes an empty passthrough fail closed.

    Asserted here rather than in the stack tests because the two halves only
    compose correctly together: deploy.sh always passes the flag, so the stack
    receives "" on every ordinary deploy and must turn that into enforcing.
    """
    lambdas = (_ROOT / "infra" / "stacks" / "platform" / "lambdas.py").read_text()
    assert 'try_get_context("rbac_enforce") or "true"' in lambdas
    assert 'try_get_context("rbac_enforce") or "false"' not in lambdas


def test_rollout_doc_prescribes_deploy_sh_and_warns_off_raw_cdk() -> None:
    """The doc used to *instruct* `Redeploy with -c rbac_enforce=true`.

    Asserted on the prescriptive sentence rather than on the bare flag: the
    warning against raw cdk necessarily quotes the flag, so a blanket
    "flag must not appear" check fails on the fix itself.
    """
    doc = _ROLLOUT_DOC.read_text()
    assert "RBAC_ENFORCE=false ./scripts/deploy.sh" in doc
    assert "Redeploy with `-c rbac_enforce=true`" not in doc
    assert "COGNITO_USERS" in doc, "the doc must say why raw cdk is unsafe here"
