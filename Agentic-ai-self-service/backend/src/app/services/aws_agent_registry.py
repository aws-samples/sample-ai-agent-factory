"""AWS Bedrock AgentCore Agent Registry adapter (Phase 6 — Loom-inspired).

Federates deployed agents into the AWS-native Agent Registry — the org-wide
catalog with an approval gate — on top of our internal registry. OPT-IN: does
nothing unless an admin configures a registryId in Settings.

Verified against boto3 1.43.8 (bedrock-agentcore-control + bedrock-agentcore):
  control: CreateRegistry, CreateRegistryRecord, GetRegistryRecord,
           ListRegistryRecords, SubmitRegistryRecordForApproval,
           UpdateRegistryRecordStatus, DeleteRegistryRecord
  data:    SearchRegistryRecords
  descriptorType ∈ {MCP, A2A, CUSTOM, AGENT_SKILLS}
  status    ∈ {DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, DEPRECATED,
               CREATING, UPDATING, CREATE_FAILED, UPDATE_FAILED}

Degrades gracefully: AWS Agent Registry is public preview and may be absent in a
region or on an account. Every call is best-effort; failures are logged and
surfaced as a disabled feature, never a 500 on the deploy path.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

A2A_CARD_SCHEMA_VERSION = "0.3"


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _record_id_from_arn(arn: str) -> str:
    """AWS returns recordArn (no recordId); the id is the last ARN segment."""
    return arn.rsplit("/", 1)[-1] if arn else ""


def build_a2a_descriptor(name: str, description: str, url: str,
                         skills: Optional[list] = None) -> dict:
    """A2A agentCard descriptor (schemaVersion 0.3) as an inlineContent JSON.

    Reuses the shape our runtime already serves at /.well-known/agent-card.json.
    """
    card = {
        "protocolVersion": A2A_CARD_SCHEMA_VERSION,
        "name": name,
        "description": (description or name)[:100],
        "version": "1.0",
        "url": url,
        "capabilities": {"streaming": True},
        "skills": skills or [],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }
    # The API's `descriptors` map is keyed by the (lowercased) descriptor type:
    # {"a2a": {"agentCard": {...}}} — NOT a bare agentCard. (Caught live: passing
    # the inner structure fails with "Unknown parameter in descriptors:
    # agentCard, must be one of: mcp, a2a, custom, agentSkills".)
    return {"a2a": {"agentCard": {"schemaVersion": A2A_CARD_SCHEMA_VERSION,
                                  "inlineContent": json.dumps(card)}}}


def build_custom_descriptor(payload: dict) -> dict:
    """CUSTOM descriptor — arbitrary inlineContent JSON (our own agent metadata).

    Returned wrapped under the ``custom`` type key (see build_a2a_descriptor).
    """
    return {"custom": {"inlineContent": json.dumps(payload)}}


class AwsAgentRegistry:
    """Thin adapter over the AWS Agent Registry control + data planes."""

    def __init__(self, registry_id: str, region: Optional[str] = None) -> None:
        self.registry_id = registry_id
        region = region or _region()
        self.control = boto3.client("bedrock-agentcore-control", region_name=region)
        self.data = boto3.client("bedrock-agentcore", region_name=region)

    # -- health ----------------------------------------------------------

    def available(self) -> bool:
        """True if the configured registry exists + is reachable (feature gate)."""
        try:
            self.control.get_registry(registryId=self.registry_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.info("AWS Agent Registry unavailable: %s", str(e)[:120])
            return False

    # -- records ---------------------------------------------------------

    def register(self, name: str, descriptor_type: str, descriptors: dict,
                 description: str = "", record_version: str = "1") -> dict:
        """Create a record (DRAFT/CREATING) and return {record_id, arn, status}."""
        resp = self.control.create_registry_record(
            registryId=self.registry_id,
            name=name,
            description=description or name,
            descriptorType=descriptor_type,
            descriptors=descriptors,
            recordVersion=record_version,
        )
        arn = resp.get("recordArn", "")
        return {
            "record_id": resp.get("recordId") or _record_id_from_arn(arn),
            "arn": arn,
            "status": resp.get("status", ""),
        }

    def submit_for_approval(self, record_id: str) -> None:
        self.control.submit_registry_record_for_approval(
            registryId=self.registry_id, recordId=record_id
        )

    def set_status(self, record_id: str, status: str, reason: str) -> None:
        """APPROVED / REJECTED / DEPRECATED — statusReason is required by the API."""
        self.control.update_registry_record_status(
            registryId=self.registry_id, recordId=record_id,
            status=status, statusReason=reason,
        )

    def get(self, record_id: str) -> Optional[dict]:
        try:
            return self.control.get_registry_record(
                registryId=self.registry_id, recordId=record_id
            )
        except Exception as e:  # noqa: BLE001
            logger.info("get_registry_record failed: %s", str(e)[:120])
            return None

    def list_records(self) -> list[dict]:
        try:
            resp = self.control.list_registry_records(registryId=self.registry_id)
            return resp.get("registryRecords") or resp.get("items") or []
        except Exception as e:  # noqa: BLE001
            logger.info("list_registry_records failed: %s", str(e)[:120])
            return []

    def delete(self, record_id: str) -> bool:
        try:
            self.control.delete_registry_record(
                registryId=self.registry_id, recordId=record_id
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("delete_registry_record failed: %s", str(e)[:120])
            return False

    def search(self, query: str, max_results: int = 20) -> list[dict]:
        try:
            resp = self.data.search_registry_records(
                registryIds=[self.registry_id],
                searchQuery=query,
                maxResults=max_results,
            )
            return resp.get("registryRecords") or resp.get("items") or resp.get("results") or []
        except Exception as e:  # noqa: BLE001
            logger.info("search_registry_records failed: %s", str(e)[:120])
            return []


# ---------------------------------------------------------------------------
# Opt-in config (a Settings row holds the registryId; feature off when unset).
# Reuses the TagPolicy table as a generic Settings store to avoid a new table.
# ---------------------------------------------------------------------------

_SETTINGS_SK = "SETTING#aws_registry_id"


def get_configured_registry_id() -> Optional[str]:
    """Return the configured AWS registryId, or None (feature disabled).

    Env override AWS_AGENT_REGISTRY_ID wins (useful for tests / static config);
    otherwise read the Settings row from the tag-policy table.
    """
    env = os.environ.get("AWS_AGENT_REGISTRY_ID")
    if env:
        return env
    try:
        table_name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
        table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
        item = table.get_item(Key={"org_id": "default", "sk": _SETTINGS_SK}).get("Item")
        return item.get("value") if item else None
    except Exception as e:  # noqa: BLE001
        logger.info("get_configured_registry_id failed: %s", str(e)[:120])
        return None


def set_configured_registry_id(registry_id: str) -> None:
    table_name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
    table.put_item(Item={"org_id": "default", "sk": _SETTINGS_SK, "value": registry_id})


def get_registry() -> Optional[AwsAgentRegistry]:
    """Return a configured adapter, or None when the feature is disabled."""
    rid = get_configured_registry_id()
    if not rid:
        return None
    return AwsAgentRegistry(rid)


def unapproved_integrations(identifiers: list[str]) -> list[str]:
    """Integration gating (Loom-study 1.4): of the given external MCP/A2A
    identifiers (server name or endpoint URL), return those that are NOT
    APPROVED in the AWS Agent Registry.

    No-op ([]) when federation is disabled — gating only applies once an org
    opts into registry governance. Matching is by record name OR by a URL
    substring within any descriptor, so a connected server is considered
    approved when an APPROVED record names it or points at it. An identifier
    with NO matching record at all is treated as UNAPPROVED (fail-closed: an
    unreviewed integration must not ship into a governed deployment).
    """
    if not identifiers:
        return []
    reg = get_registry()
    if reg is None:
        return []  # federation off → no gating

    records = reg.list_records()
    approved_names: set[str] = set()
    approved_blobs: list[str] = []
    for r in records:
        if (r.get("status") or "").upper() != "APPROVED":
            continue
        nm = r.get("name") or r.get("recordName")
        if nm:
            approved_names.add(str(nm))
        # keep a coarse text blob per record for URL substring matching
        approved_blobs.append(json.dumps(r))

    unapproved: list[str] = []
    for ident in identifiers:
        if not ident:
            continue
        if ident in approved_names:
            continue
        if any(ident in blob for blob in approved_blobs):
            continue
        unapproved.append(ident)
    return unapproved
