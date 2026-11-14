"""Tests for the risk classifier (plan §3.B).

Table-driven: every rule has a positive and a negative case, the budget modifier covers both-set /
one-unset / equal / over / capped, and the combination tests pin the tier arithmetic and the stable
reason order. The mutation script (tools/mutation_check_risk_classifier.sh) disables each rule in
turn and asserts exactly one of these tests fails.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_spec_v2 import (
    AgentSpecV2,
    BudgetSpec,
    CodeSpec,
    CustomNodeCode,
    KnowledgeSpec,
    ModelSpec,
    PolicySpec,
    ToolSpec,
    TriggerSpec,
)
from app.schemas.workspace_policy import WorkspaceBudgets, WorkspacePolicy
from app.services.risk_classifier import CLASSIFIER_VERSION, classify

_SHA = "a" * 64


def _spec(**overrides) -> AgentSpecV2:
    base: dict = {"id": "agt_test", "name": "test agent"}
    base.update(overrides)
    return AgentSpecV2(**base)


def _tool(risk: str = "unclassified", tool_id: str = "tool1", type_: str = "lambda") -> ToolSpec:
    return ToolSpec(id=tool_id, type=type_, ref="ref://x", risk_class=risk)


def _knowledge(data: str = "unclassified", kb_id: str = "kb1", type_: str = "managedKb") -> KnowledgeSpec:
    return KnowledgeSpec(id=kb_id, type=type_, data_class=data)


def _trigger(type_: str = "webhook", trg_id: str = "trg1") -> TriggerSpec:
    return TriggerSpec(id=trg_id, type=type_)


def _code(node_id: str = "node1") -> CodeSpec:
    return CodeSpec(custom_nodes={node_id: CustomNodeCode(source_ref="s3://code", sha256=_SHA)})


def _policy(**overrides) -> WorkspacePolicy:
    """A workspace that trusts builder classifications: the rule tests judge the classes a builder
    ASSERTED. The platform default does not trust them; see ``_LOCKED_POLICY`` and the trust-model
    tests at the end."""
    base: dict = {
        "workspace_id": "ws_test",
        "name": "Test Workspace",
        "defaults": {"trust_builder_classifications": True},
    }
    base.update(overrides)
    return WorkspacePolicy(**base)


_EMPTY_POLICY = _policy()
_LOCKED_POLICY = WorkspacePolicy(workspace_id="ws_locked", name="Locked")


def _codes(result) -> list[str]:
    return [reason.code for reason in result.reasons]


# --------------------------------------------------------------------------- baseline


def test_empty_spec_is_low_with_no_reasons():
    result = classify(_spec(), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []
    assert result.classifier_version == CLASSIFIER_VERSION


# --------------------------------------------------------------------------- tool rules


def test_pay_tool_is_high():
    result = classify(_spec(tools=[_tool(risk="pay")]), _EMPTY_POLICY)
    assert result.tier == "high"
    assert _codes(result) == ["pay_tool"]


def test_write_tool_is_medium():
    result = classify(_spec(tools=[_tool(risk="write")]), _EMPTY_POLICY)
    assert result.tier == "medium"
    assert _codes(result) == ["write_tool"]


def test_read_tool_is_low():
    result = classify(_spec(tools=[_tool(risk="read")]), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []


def test_unclassified_tool_floors_at_the_policys_unclassified_tier():
    """A class nobody gave cannot be shown to be harmless: medium by default, high if the admin says
    so, and low only where the admin explicitly chose it (a prototyping workspace)."""
    spec = _spec(tools=[_tool(risk="unclassified")])
    default = classify(spec, _EMPTY_POLICY)
    assert default.tier == "medium"
    assert _codes(default) == ["unclassified"]
    assert default.reasons[0].message.startswith("unclassified tool(s) ['tool1']")
    high = classify(spec, _policy(defaults={"unclassified_risk_tier": "high"}))
    assert (high.tier, _codes(high)) == ("high", ["unclassified"])
    low = classify(spec, _policy(defaults={"unclassified_risk_tier": "low"}))
    assert (low.tier, low.reasons) == ("low", [])


# --------------------------------------------------------------------------- provider rule


def test_non_bedrock_provider_is_medium():
    result = classify(_spec(model=ModelSpec(provider="openai", model_id="gpt-5")), _EMPTY_POLICY)
    assert result.tier == "medium"
    assert _codes(result) == ["non_bedrock_provider"]


@pytest.mark.parametrize("provider", ["bedrock", "Bedrock", "  bedrock  "])
def test_bedrock_provider_is_low(provider):
    result = classify(_spec(model=ModelSpec(provider=provider, model_id="claude")), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []


def test_no_model_does_not_fire_provider_rule():
    result = classify(_spec(model=None), _EMPTY_POLICY)
    assert result.tier == "low"


# --------------------------------------------------------------------------- data rule


@pytest.mark.parametrize("data_class", ["confidential", "pii"])
def test_sensitive_data_is_high(data_class):
    result = classify(_spec(knowledge=[_knowledge(data=data_class)]), _EMPTY_POLICY)
    assert result.tier == "high"
    assert _codes(result) == ["sensitive_data_class"]


@pytest.mark.parametrize("data_class", ["public", "internal"])
def test_non_sensitive_data_is_low(data_class):
    result = classify(_spec(knowledge=[_knowledge(data=data_class)]), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []


def test_unclassified_knowledge_floors_at_the_policys_unclassified_tier():
    result = classify(_spec(knowledge=[_knowledge(data="unclassified")]), _EMPTY_POLICY)
    assert (result.tier, _codes(result)) == ("medium", ["unclassified"])
    assert "knowledge ['kb1']" in result.reasons[0].message
    assert (
        classify(
            _spec(knowledge=[_knowledge(data="unclassified")]), _policy(defaults={"unclassified_risk_tier": "low"})
        ).tier
        == "low"
    )


# --------------------------------------------------------------------------- public ingress rule


@pytest.mark.parametrize("trigger_type", ["webhook", "email"])
def test_public_ingress_with_write_is_high(trigger_type):
    result = classify(_spec(tools=[_tool(risk="write")], triggers=[_trigger(type_=trigger_type)]), _EMPTY_POLICY)
    assert result.tier == "high"
    assert "public_ingress_with_write" in _codes(result)
    assert "write_tool" in _codes(result)


def test_public_ingress_without_write_is_low():
    result = classify(_spec(triggers=[_trigger(type_="webhook")]), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []


def test_non_public_trigger_with_write_stays_medium():
    result = classify(_spec(tools=[_tool(risk="write")], triggers=[_trigger(type_="schedule")]), _EMPTY_POLICY)
    assert result.tier == "medium"
    assert "public_ingress_with_write" not in _codes(result)


# --------------------------------------------------------------------------- custom code rule


def test_custom_code_is_medium():
    result = classify(_spec(code=_code()), _EMPTY_POLICY)
    assert result.tier == "medium"
    assert _codes(result) == ["custom_code"]


def test_no_custom_code_is_low():
    result = classify(_spec(code=CodeSpec()), _EMPTY_POLICY)
    assert result.tier == "low"


# --------------------------------------------------------------------------- budget modifier


def test_budget_over_default_bumps_one_tier():
    result = classify(
        _spec(policy=PolicySpec(budget=BudgetSpec(monthly_usd=200))),
        _policy(budgets=WorkspaceBudgets(per_agent_monthly_usd=100)),
    )
    assert result.tier == "medium"  # low bumped one tier
    assert _codes(result) == ["budget_above_default"]
    assert result.reasons[-1].kind == "modifier"


def test_budget_equal_default_does_not_bump():
    result = classify(
        _spec(policy=PolicySpec(budget=BudgetSpec(monthly_usd=100))),
        _policy(budgets=WorkspaceBudgets(per_agent_monthly_usd=100)),
    )
    assert result.tier == "low"
    assert result.reasons == []


def test_budget_below_default_does_not_bump():
    result = classify(
        _spec(policy=PolicySpec(budget=BudgetSpec(monthly_usd=50))),
        _policy(budgets=WorkspaceBudgets(per_agent_monthly_usd=100)),
    )
    assert result.tier == "low"
    assert result.reasons == []


def test_budget_modifier_needs_both_sides_set():
    # Agent asks for a budget but the workspace sets no default: nothing to compare, no bump.
    result = classify(_spec(policy=PolicySpec(budget=BudgetSpec(monthly_usd=999))), _EMPTY_POLICY)
    assert result.tier == "low"
    assert result.reasons == []


def test_budget_bump_from_medium_to_high():
    result = classify(
        _spec(tools=[_tool(risk="write")], policy=PolicySpec(budget=BudgetSpec(monthly_usd=200))),
        _policy(budgets=WorkspaceBudgets(per_agent_monthly_usd=100)),
    )
    assert result.tier == "high"  # write=medium, bumped to high
    assert "write_tool" in _codes(result)
    assert "budget_above_default" in _codes(result)


def test_budget_bump_capped_at_high():
    result = classify(
        _spec(tools=[_tool(risk="pay")], policy=PolicySpec(budget=BudgetSpec(monthly_usd=200))),
        _policy(budgets=WorkspaceBudgets(per_agent_monthly_usd=100)),
    )
    assert result.tier == "high"  # pay=high, bump stays high
    assert result.reasons[-1].code == "budget_above_default"
    assert result.reasons[-1].tier == "high"


# --------------------------------------------------------------------------- combinations / contract


def test_multiple_floor_rules_take_the_max():
    result = classify(
        _spec(
            tools=[_tool(risk="write")],
            knowledge=[_knowledge(data="pii")],
            model=ModelSpec(provider="openai", model_id="gpt"),
        ),
        _EMPTY_POLICY,
    )
    assert result.tier == "high"  # pii forces high despite medium floors
    assert _codes(result) == ["write_tool", "non_bedrock_provider", "sensitive_data_class"]


def test_reason_order_matches_rule_order():
    result = classify(
        _spec(
            tools=[_tool(risk="pay", tool_id="p"), _tool(risk="write", tool_id="w")],
            knowledge=[_knowledge(data="confidential", kb_id="k")],
            model=ModelSpec(provider="openai", model_id="m"),
            triggers=[_trigger(type_="webhook", trg_id="t")],
            code=_code(),
        ),
        _EMPTY_POLICY,
    )
    assert result.tier == "high"
    assert _codes(result) == [
        "pay_tool",
        "write_tool",
        "non_bedrock_provider",
        "sensitive_data_class",
        "public_ingress_with_write",
        "custom_code",
    ]


def test_result_serialises_camelcase():
    result = classify(_spec(tools=[_tool(risk="write")]), _EMPTY_POLICY)
    dumped = result.model_dump(by_alias=True)
    assert dumped["classifierVersion"] == CLASSIFIER_VERSION
    assert dumped["reasons"][0]["code"] == "write_tool"
    assert dumped["reasons"][0]["kind"] == "floor"


def test_workspace_policy_rejects_unknown_key():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        WorkspacePolicy(workspace_id="ws_test", name="W", bogus=True)


# --------------------------------------------------------------------------- catalog floor (trust model)


def _connector(connector_id: str, risk: str = "unclassified") -> ToolSpec:
    return ToolSpec(id=f"connector-{connector_id}", type="connector", ref=connector_id, risk_class=risk)


def test_the_catalog_floor_reads_what_each_connectors_operations_imply():
    from app.services.connectors_catalog import list_connectors
    from app.services.risk_classifier import catalog_risk_floor

    floors = {entry["id"]: catalog_risk_floor(entry["id"]) for entry in list_connectors()}
    assert floors["stripe"] == "pay"  # Payments category
    assert floors["jira"] == "write"  # create_ticket
    assert floors["slack"] == "write"  # send_message
    assert floors["google_drive"] == "read"  # list_files, download_file only
    assert set(floors.values()) <= {"read", "write", "pay"}
    assert catalog_risk_floor("acme-crm-not-in-catalog") is None


@pytest.mark.parametrize(
    ("connector_id", "asserted", "effective"),
    [
        ("jira", "read", "write"),  # a builder cannot label a write connector read
        ("jira", "unclassified", "write"),  # ...nor leave it unclassified
        ("jira", "pay", "pay"),  # ...but may raise it (in a workspace that trusts builder classes)
        ("stripe", "read", "pay"),  # payments are always pay
        ("google_drive", "unclassified", "read"),  # a read-only connector is read without a human
        ("google_drive", "write", "write"),  # raising stands
        ("acme-crm-not-in-catalog", "read", "read"),  # unknown to the catalog: the assertion stands
        ("acme-crm-not-in-catalog", "unclassified", "unclassified"),
    ],
)
def test_in_a_trusting_workspace_a_builder_can_raise_a_connectors_class_but_never_lower_it(
    connector_id, asserted, effective
):
    from app.services.risk_classifier import effective_risk_class

    assert effective_risk_class(_connector(connector_id, asserted), _EMPTY_POLICY) == effective


@pytest.mark.parametrize(
    ("connector_id", "asserted", "effective"),
    [
        ("jira", "read", "write"),  # the catalog floor still applies
        ("jira", "pay", "write"),  # ...and a RAISED assertion no longer counts: the catalog is the only source
        ("stripe", "read", "pay"),
        ("google_drive", "write", "read"),
        ("acme-crm-not-in-catalog", "read", "unclassified"),  # no catalog, no trust: unclassified
    ],
)
def test_by_default_only_the_catalog_classifies_a_connector(connector_id, asserted, effective):
    from app.services.risk_classifier import effective_risk_class

    assert effective_risk_class(_connector(connector_id, asserted), _LOCKED_POLICY) == effective
    assert effective_risk_class(_connector(connector_id, asserted)) == effective  # no policy = untrusted


def test_non_connector_tools_keep_the_asserted_class_only_where_builders_are_trusted():
    from app.services.risk_classifier import effective_data_class, effective_risk_class

    assert effective_risk_class(_tool(risk="read", type_="lambda"), _EMPTY_POLICY) == "read"
    assert effective_risk_class(_tool(risk="read", type_="lambda"), _LOCKED_POLICY) == "unclassified"
    assert effective_risk_class(_tool(risk="unclassified", type_="mcp"), _EMPTY_POLICY) == "unclassified"
    assert effective_data_class(_knowledge(data="public"), _EMPTY_POLICY) == "public"
    assert effective_data_class(_knowledge(data="public"), _LOCKED_POLICY) == "unclassified"
    assert effective_data_class(_knowledge(data="pii"), _LOCKED_POLICY) == "unclassified"


def test_by_default_a_builders_read_or_public_labels_do_not_lower_risk():
    """A mutating Lambda labelled ``read`` and a sensitive source labelled ``public`` are judged as
    unclassified (medium by default) in a workspace that does not trust builder classifications."""
    spec = _spec(tools=[_tool(risk="read", type_="lambda")], knowledge=[_knowledge(data="public")])
    locked = classify(spec, _LOCKED_POLICY)
    assert (locked.tier, _codes(locked)) == ("medium", ["unclassified"])
    trusting = classify(spec, _EMPTY_POLICY)
    assert (trusting.tier, trusting.reasons) == ("low", [])


def test_a_write_connector_labelled_read_still_classifies_medium():
    result = classify(_spec(tools=[_connector("jira", "read")]), _EMPTY_POLICY)
    assert (result.tier, _codes(result)) == ("medium", ["write_tool"])


def test_a_payments_connector_labelled_read_still_classifies_high():
    result = classify(_spec(tools=[_connector("stripe", "read")]), _EMPTY_POLICY)
    assert result.tier == "high"
    assert "pay_tool" in _codes(result)


def test_public_ingress_with_a_catalog_write_connector_is_high_whatever_the_label():
    result = classify(_spec(tools=[_connector("slack", "read")], triggers=[_trigger("webhook")]), _EMPTY_POLICY)
    assert result.tier == "high"
    assert "public_ingress_with_write" in _codes(result)
