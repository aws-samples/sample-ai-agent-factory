"""F-01: the shared runtime role's connector-secret read must be bound to the platform's tags.

``build_shared_runtime_role`` granted ``secretsmanager:GetSecretValue`` on
``secret:agentcore-connector/*`` account-wide and unconditioned: every deployment in the
account, from every stack of this product, mints its raw customer API keys and OAuth client
secrets under that one prefix, and the role every ``shared``-mode agent runs as could read all
of them. The 60-line comment above the grant explained why that was forbidden, then granted it.

What CAN be pinned on a shared principal: every connector secret ``_put_connector_secret`` mints
carries ``ManagedBy=agentcore-flows`` and ``AgentCoreStack={project}-{env}-{region}``
(``governed_tag_list`` -> ``owner_tags``), so the read can be conditioned to secrets THIS
platform stack family minted -- ``aws:ResourceTag/ManagedBy`` exact plus
``aws:ResourceTag/AgentCoreStack`` like ``{project}-{env}-*`` (the region segment is the secret's
own region: a same-account deploy to an allow-listed target region stages its copy there, tagged
with that region). Both keys are supported on GetSecretValue per the Service Reference feed.

What CANNOT be pinned, stated rather than implied: the shared role is ONE principal for every
tenant, AgentCore attaches no session tags, and GetSecretValue's request context carries nothing
that identifies the calling deployment. No IAM condition on this role can tell tenant A's secret
from tenant B's. The condition closes the cross-STACK read and the read of any untagged or
foreign secret under the prefix; the cross-tenant read inside one stack is closed only by
``per_agent`` identity mode, where the role is minted per deployment with the exact ARNs
(``per_agent_identity.build_scoped_runtime_policy``). See the P1 ledger for that decision.

ARCC cnt_SFJJhkOueCPRkd (condition keys on reads of customer data) decided the shape.
"""

from __future__ import annotations

import pytest

from tests.iam_attachment import role_logical_id, statements_for_role
from tests.p1_synth import ENVIRONMENT, PROJECT, actions, all_statements, resources_text, synth

CONNECTOR = "secret:agentcore-connector/"
GET = "secretsmanager:GetSecretValue"


@pytest.fixture(scope="module")
def tpl() -> dict:
    return synth("F01SharedRuntimeConnectorReadStack")


def _connector_reads(tpl: dict, role_prefix: str) -> list[tuple[str, dict]]:
    lid = role_logical_id(tpl, role_prefix)
    return [
        (pid, st)
        for pid, st in statements_for_role(tpl, lid)
        if st.get("Effect") == "Allow" and GET in actions(st) and any(CONNECTOR in r for r in resources_text(st))
    ]


def test_the_walk_finds_the_grant_it_is_about(tpl):
    """Reach before verdict: no connector read on the shared role means the walk is broken
    (or the product's tool plane is dead), never a pass."""
    assert _connector_reads(tpl, "SharedRuntimeExecRole"), (
        "no GetSecretValue on agentcore-connector/ resolves to the shared runtime role"
    )


def test_every_connector_read_on_the_shared_role_is_pinned_to_this_platforms_tags(tpl):
    """The assertion that fails on the pre-fix tree."""
    for pid, st in _connector_reads(tpl, "SharedRuntimeExecRole"):
        cond = st.get("Condition") or {}
        assert cond.get("StringEquals", {}).get("aws:ResourceTag/ManagedBy") == "agentcore-flows", (
            f"{pid}: GetSecretValue on {CONNECTOR}* is not conditioned on aws:ResourceTag/ManagedBy; "
            "the shared role reads every connector secret in the account"
        )
        assert cond.get("StringLike", {}).get("aws:ResourceTag/AgentCoreStack") == f"{PROJECT}-{ENVIRONMENT}-*", (
            f"{pid}: the read is not pinned to this stack family's AgentCoreStack tag: {cond}"
        )


def test_the_shared_role_reads_only_shared_mode_connector_secrets(tpl):
    """F-01 (b): the backend stamps IdentityMode=<shared|per_agent> at mint time; the shared
    role may read only the `shared` ones, so a per_agent deployment's secrets are out of its
    reach entirely. Migration consequence, intended: a connector secret minted before the tag
    existed is unreadable by this role until its deployment is redeployed (fail-closed)."""
    reads = _connector_reads(tpl, "SharedRuntimeExecRole")
    assert reads
    for pid, st in reads:
        cond = st.get("Condition") or {}
        assert cond.get("StringEquals", {}).get("aws:ResourceTag/IdentityMode") == "shared", (
            f"{pid}: GetSecretValue on {CONNECTOR}* is not pinned to aws:ResourceTag/IdentityMode=shared: {cond}"
        )


def test_the_identity_mode_pin_sits_in_a_statement_that_reaches_only_the_connector_prefix(tpl):
    """Own statement: the IdentityMode condition must never share a statement with a read of a
    resource that does not carry the tag (Cognito client secrets, staged provider secrets),
    or that read is silently denied and the outage looks like a tightening."""
    lid = role_logical_id(tpl, "SharedRuntimeExecRole")
    hits = 0
    for pid, st in statements_for_role(tpl, lid):
        cond = st.get("Condition") or {}
        if "aws:ResourceTag/IdentityMode" not in (cond.get("StringEquals") or {}):
            continue
        hits += 1
        assert all(CONNECTOR in r for r in resources_text(st)), (pid, resources_text(st))
        assert set(actions(st)) <= {GET, "secretsmanager:DescribeSecret"}, (pid, actions(st))
    assert hits >= 1


def test_the_pinned_statement_carries_no_read_that_would_fail_closed(tpl):
    """Only tag-bearing actions may share a statement with a ResourceTag condition. The feed
    lists aws:ResourceTag on GetSecretValue and DescribeSecret; anything else would be denied."""
    for pid, st in _connector_reads(tpl, "SharedRuntimeExecRole"):
        assert set(actions(st)) <= {GET, "secretsmanager:DescribeSecret"}, (pid, actions(st))


def test_no_role_in_the_template_reads_the_connector_prefix_unconditioned_except_the_control_plane(tpl):
    """Template-wide guard for the over-reach direction: the only unconditioned readers of the
    connector prefix are control-plane roles (deployment Lambda, gateway/mcp_server/harness
    steps) that mint and copy those secrets. A new tenant-facing role must not join them."""
    tenant_facing = {"SharedRuntimeExecRole", "SharedMcpRuntimeExecRole"}
    for pid, st in all_statements(tpl):
        if st.get("Effect") != "Allow" or GET not in actions(st):
            continue
        if not any(CONNECTOR in r for r in resources_text(st)):
            continue
        if any(pid.startswith(t) for t in tenant_facing):
            assert st.get("Condition"), f"{pid}: tenant-facing connector read without a condition"


def test_the_mcp_shared_role_still_reads_no_secret_at_all(tpl):
    lid = role_logical_id(tpl, "SharedMcpRuntimeExecRole")
    for pid, st in statements_for_role(tpl, lid):
        assert not any(a.startswith("secretsmanager:") for a in actions(st)), (pid, actions(st))
