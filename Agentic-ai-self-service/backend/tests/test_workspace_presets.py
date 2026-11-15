"""The UI's workspace presets are documents this backend accepts, and what they admit is pinned
(plan §3.B, step 5 and its risk note: "presets tuned with the business templates so every template
deploys under Standard").

The fixtures under ``fixtures/workspace_policy/presets/`` are written by the frontend
(``workspacePresets.test.ts``, a vitest file snapshot) from the real preset code. This side proves
each is a complete, valid ``WorkspacePolicy`` (so the panel never offers a document the API would
refuse) and pins the behaviour an admin is promised:

* Standard admits every gallery template; the write connector of the business canvas routes it to the
  approvers.
* Locked-down refuses that canvas (a write connector in a read-only workspace), with exact codes.
* Developer admits it, auto-approved, at the tier its tools earn.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from app.schemas.workspace_policy import WorkspacePolicy
from app.services.spec_migration import migrate_v1_flow
from app.services.workspace_admission import admit

from tests.test_workspace_admission import FIXTURES as TEMPLATES
from tests.test_workspace_admission import _business_canvas

PRESETS = pathlib.Path(__file__).parent / "fixtures" / "workspace_policy" / "presets"
PRESET_IDS = ("locked-down", "standard", "developer")


def _preset(preset_id: str) -> WorkspacePolicy:
    return WorkspacePolicy.model_validate(json.loads((PRESETS / f"{preset_id}.json").read_text()))


def _templates():
    specs = {
        path.name.removesuffix(".flow.json"): migrate_v1_flow(json.loads(path.read_text()))
        for path in sorted(TEMPLATES.glob("*.flow.json"))
    }
    assert len(specs) >= 6, "the gallery fixtures are missing"
    return specs


def test_the_ui_offers_exactly_the_three_presets_and_each_is_a_complete_valid_policy():
    assert {path.stem for path in PRESETS.glob("*.json")} == set(PRESET_IDS)
    for preset_id in PRESET_IDS:
        document = json.loads((PRESETS / f"{preset_id}.json").read_text())
        policy = WorkspacePolicy.model_validate(document)
        # Complete: the preset writes every field the schema knows, so a stored preset is never a
        # truncated document the completeness check would refuse.
        assert policy.model_dump(by_alias=True, mode="json") == document
        assert policy.workspace_id == "finance"
        assert policy.member_groups == ["g-finance-builders"]


def test_every_gallery_template_deploys_under_standard():
    standard = _preset("standard")
    decisions = {name: admit(spec, standard) for name, spec in _templates().items()}
    refused = {name: [v.code for v in d.violations] for name, d in decisions.items() if d.outcome != "admitted"}
    assert refused == {}, "Standard must admit every template the gallery offers"
    assert all(decision.tier in ("low", "medium", "high") for decision in decisions.values())


def test_standard_routes_the_business_canvas_with_a_write_connector_to_the_approvers():
    decision = admit(migrate_v1_flow(_business_canvas()), _preset("standard"))
    assert decision.outcome == "admitted"
    assert decision.tier == "medium"
    assert decision.approvers == ["g-approvers"]
    assert "write_tool" in {reason.code for reason in decision.reasons}


def test_locked_down_refuses_the_business_canvas_with_a_write_connector():
    decision = admit(migrate_v1_flow(_business_canvas()), _preset("locked-down"))
    assert decision.outcome == "denied"
    codes = {violation.code for violation in decision.violations}
    assert "tool_risk_class_not_allowed" in codes, codes


def test_developer_admits_the_business_canvas_auto_approved_at_the_tier_its_tools_earn():
    decision = admit(migrate_v1_flow(_business_canvas()), _preset("developer"))
    assert decision.outcome == "admitted"
    assert decision.approvers == []
    assert decision.tier == "medium"


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_presets_route_medium_to_the_approvers_and_high_to_security_as_documented(preset_id):
    policy = _preset(preset_id)
    if preset_id == "developer":
        assert (policy.approval.low, policy.approval.medium, policy.approval.high) == ([], [], [])
    else:
        assert policy.approval.low == []
        assert policy.approval.medium == ["g-approvers"]
        assert policy.approval.high == ["g-security", "g-approvers"]
