"""The compact, immutable record of what admission judged (plan §3.B), carried on the decision.

Admission judges a Spec v2 document built from the deploy request. Anything attached to the agent
later (today: a trigger) has to be judged again, under the policy of THAT day, against the same agent.
The full spec is too large to keep on a version row and much of it (the canvas, instructions) plays no
part in admission, so the decision records this inventory instead: exactly the fields the classifier
and the allow-list rules read, with every classification kept **as the builder declared it** (the
label, never the effective class), so a re-admission applies the current policy's trust setting too.

``to_spec`` rebuilds a spec the admission functions accept; a test pins that admitting the rebuilt spec
gives the same tier, reasons, violations and ingress-sensitive capabilities as admitting the original.

Every field is REQUIRED TO BE PRESENT (an explicit empty list or ``null`` where the agent has none):
``from_spec`` always writes the complete record, so an absent key is truncation or tampering, and a
truncated inventory must not reconstruct an agent with fewer capabilities than the one that was judged
(a writer whose ``tools`` went missing would re-admit as a toolless, low-risk agent and get its webhook
without the security approval). Validation therefore fails and the attachment blocks (409) instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import (
    AgentSpecV2,
    ChannelType,
    DataClass,
    KnowledgeType,
    RiskClass,
    ToolType,
    TriggerSpec,
    TriggerType,
)


class _Record(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class InventoryModel(_Record):
    provider: str
    model_id: str


class InventoryTool(_Record):
    id: str
    type: ToolType
    ref: str
    #: The builder's label as written; the classifier derives the effective class under the current policy.
    risk_class: RiskClass


class InventoryKnowledge(_Record):
    id: str
    type: KnowledgeType
    data_class: DataClass


class InventoryChannel(_Record):
    id: str
    type: ChannelType


class InventoryTrigger(_Record):
    id: str
    type: TriggerType


class InventoryCode(_Record):
    id: str
    language: str
    source_ref: str
    sha256: str


class AdmissionInventory(_Record):
    """No field has a default: a key the writer always produces but the reader cannot find is an
    incomplete record, never "none of these"."""

    id: str
    name: str
    workspace_id: str | None
    model: InventoryModel | None
    tools: list[InventoryTool]
    knowledge: list[InventoryKnowledge]
    channels: list[InventoryChannel]
    triggers: list[InventoryTrigger]
    custom_code: list[InventoryCode]
    region: str | None
    monthly_budget_usd: float | None

    @classmethod
    def from_spec(cls, spec: AgentSpecV2) -> AdmissionInventory:
        return cls(
            id=spec.id,
            name=spec.name,
            workspace_id=spec.workspace_id,
            model=InventoryModel(provider=spec.model.provider, model_id=spec.model.model_id) if spec.model else None,
            tools=[InventoryTool(id=t.id, type=t.type, ref=t.ref, risk_class=t.risk_class) for t in spec.tools],
            knowledge=[InventoryKnowledge(id=k.id, type=k.type, data_class=k.data_class) for k in spec.knowledge],
            channels=[InventoryChannel(id=c.id, type=c.type) for c in spec.channels],
            triggers=[InventoryTrigger(id=t.id, type=t.type) for t in spec.triggers],
            custom_code=[
                InventoryCode(id=node_id, language=code.language, source_ref=code.source_ref, sha256=code.sha256)
                for node_id, code in spec.code.custom_nodes.items()
            ],
            region=spec.deployment.region,
            monthly_budget_usd=spec.policy.budget.monthly_usd,
        )

    def to_spec(self, *, extra_triggers: Iterable[TriggerSpec] = ()) -> AgentSpecV2:
        """A spec the admission functions accept: the inventory, plus ``extra_triggers`` (what is being
        attached now). Fields admission never reads are left at their defaults."""
        triggers: list[dict[str, Any]] = [{"id": t.id, "type": t.type} for t in self.triggers]
        triggers.extend(trigger.model_dump(by_alias=True) for trigger in extra_triggers)
        document: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "workspaceId": self.workspace_id,
            "model": {"provider": self.model.provider, "modelId": self.model.model_id} if self.model else None,
            "tools": [{"id": t.id, "type": t.type, "ref": t.ref, "riskClass": t.risk_class} for t in self.tools],
            "knowledge": [{"id": k.id, "type": k.type, "dataClass": k.data_class} for k in self.knowledge],
            "channels": [{"id": c.id, "type": c.type} for c in self.channels],
            "triggers": triggers,
            "code": {
                "customNodes": {
                    code.id: {"language": code.language, "sourceRef": code.source_ref, "sha256": code.sha256}
                    for code in self.custom_code
                }
            },
            "deployment": {"region": self.region},
            "policy": {"budget": {"monthlyUsd": self.monthly_budget_usd}},
        }
        return AgentSpecV2.model_validate(document)
