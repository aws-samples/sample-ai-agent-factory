"""F-24: the deployment Lambda's grant on ``secret:bedrock-agentcore-*`` and ``secret:AgentCore*``
is delete-only.

Both prefixes sat in the statement that also grants the trigger/connector/registry secrets
Create/Get/Put/Delete/Describe, so the API Lambda could READ every AgentCore identity
credential-provider secret in the account. The justification in the source (Bug 184) covers
one verb: ``delete_oauth2_credential_provider`` cascade-deletes the provider's backing secret
as the caller, so teardown needs ``DeleteSecret`` on it. ``DescribeSecret`` is the existence
check before the delete. Nothing on this Lambda reads or writes those namespaces: the service
writes them during the STEP role's ``CreateOauth2CredentialProvider``, and every credential the
API resolves is ``agentcore-connector/``. The step roles' own grants on ``bedrock-agentcore-*``
are untouched (that write goes through the caller's permissions). ARCC cnt_SFJJhkOueCPRkd.
"""

from __future__ import annotations

import pytest

from tests.iam_attachment import role_logical_id, statements_for_role
from tests.p1_synth import actions, resources_text, synth

PREFIXES = ("secret:bedrock-agentcore-*", "secret:AgentCore*")
DELETE_ONLY = {"secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret"}
#: The value-bearing verbs F-24 is about. secretsmanager:TagResource on these prefixes is a
#: separate, pre-existing statement (tag parity for the service-written provider secret) and
#: exposes no secret value; it is left as it was.
VALUE_VERBS = {
    "secretsmanager:CreateSecret",
    "secretsmanager:GetSecretValue",
    "secretsmanager:PutSecretValue",
    "secretsmanager:UpdateSecret",
    "secretsmanager:RestoreSecret",
    "secretsmanager:*",
}


@pytest.fixture(scope="module")
def deployment_statements() -> list[tuple[str, dict]]:
    tpl = synth("F24IdentitySecretsStack")
    return statements_for_role(tpl, role_logical_id(tpl, "DeploymentLambdaRole"))


def _on_prefixes(statements):
    return [
        (pid, st)
        for pid, st in statements
        if st.get("Effect") == "Allow" and any(any(p in r for p in PREFIXES) for r in resources_text(st))
    ]


def test_teardown_can_still_delete_the_providers_secret(deployment_statements):
    hits = _on_prefixes(deployment_statements)
    assert hits, "no grant on the identity secret prefixes at all; Bug 184's teardown leak is back"
    for p in PREFIXES:
        assert any(
            "secretsmanager:DeleteSecret" in actions(st) and any(p in r for r in resources_text(st))
            for _pid, st in hits
        ), p


def test_the_identity_prefixes_carry_nothing_but_delete_and_describe(deployment_statements):
    """The assertion that fails on the pre-fix tree."""
    for pid, st in _on_prefixes(deployment_statements):
        leaked = set(actions(st)) & VALUE_VERBS
        assert not leaked, f"{pid}: {sorted(leaked)} granted on {PREFIXES}; the justification covers DeleteSecret only"
        if "secretsmanager:DeleteSecret" in actions(st):
            assert set(actions(st)) <= DELETE_ONLY, (pid, actions(st))


def test_the_connector_and_trigger_reads_were_not_narrowed_by_accident(deployment_statements):
    """The split must not have taken GetSecretValue away from the namespaces the API does read."""
    reads = [
        st
        for _pid, st in deployment_statements
        if st.get("Effect") == "Allow" and "secretsmanager:GetSecretValue" in actions(st)
    ]
    flat = " ".join(" ".join(resources_text(st)) for st in reads)
    for needed in ("secret:agentcore-connector/", "secret:agentcore-trigger/", "secret:agentcore-registry/"):
        assert needed in flat, needed
