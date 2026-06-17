"""Storage services for flow persistence.

This module provides both in-memory and DynamoDB-backed storage
for Flow entities. Follows the same patterns as storage.py
and dynamodb_storage.py for workflows.

Requirements: 1.1, 2.1, 6.2, 6.4, 7.1, 7.2
"""

from datetime import datetime, timezone
from typing import Optional
import uuid
import logging

import boto3

from app.models import Flow
from app.models.enums import DeploymentStatus
from .dynamodb_storage import (
    _convert_floats_to_decimals,
    _convert_decimals_to_floats,
    _get_dynamodb_resource,
    _get_table,
    _put_item,
    _get_item,
    _delete_item,
    _scan_table,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Serialization Helpers
# ============================================================================


def _serialize_flow(flow: Flow) -> dict:
    """Serialize a Flow to a DynamoDB-compatible dict.

    Uses Pydantic's model_dump with mode="json" to produce a
    JSON-serializable dict. The flow id is stored as the partition key.

    Args:
        flow: The Flow to serialize

    Returns:
        Dict suitable for DynamoDB put_item
    """
    item = flow.model_dump(mode="json")
    item["flow_id"] = item.pop("id")
    item = _convert_floats_to_decimals(item)
    return item


def _deserialize_flow(item: dict) -> Flow:
    """Deserialize a DynamoDB item back to a Flow.

    Maps the DynamoDB partition key (flow_id) back to the
    Pydantic model's 'id' field and validates the full object.

    Args:
        item: DynamoDB item dict

    Returns:
        Validated Flow instance
    """
    data = dict(item)
    data["id"] = data.pop("flow_id")
    data = _convert_decimals_to_floats(data)
    return Flow.model_validate(data)


def _create_empty_workflow() -> dict:
    """Create an empty workflow dict with sensible defaults.

    Returns:
        A new workflow dict with empty nodes/edges and default metadata.
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "name": "Untitled Workflow",
        "version": "1.0.0",
        "description": "",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "metadata": {
            "author": "system",
            "aws_region": "us-east-1",
            "tags": [],
            "deployment_status": "not_deployed",
        },
        "created_at": now,
        "updated_at": now,
    }


# ============================================================================
# In-Memory Flow Storage
# ============================================================================


class FlowStorage:
    """In-memory storage for flows.

    This is a simple implementation for development/testing.
    In production, this would be replaced with DynamoDBFlowStorage.

    Requirements: 1.1, 2.1, 6.2, 6.4
    """

    def __init__(self) -> None:
        """Initialize empty storage."""
        self._flows: dict[str, Flow] = {}

    def create(self, name: str, owner_sub: Optional[str] = None) -> Flow:
        """Create a new flow with an empty workflow.

        Args:
            name: The flow name
            owner_sub: Cognito sub of the creating user (tenant isolation)

        Returns:
            The created Flow with generated ID and timestamps

        Raises:
            ValueError: If a flow with the same ID already exists
        """
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        workflow = _create_empty_workflow()

        flow = Flow(
            id=flow_id,
            name=name,
            workflow=workflow,
            deployment_status=DeploymentStatus.NOT_DEPLOYED,
            created_at=now,
            updated_at=now,
            owner_sub=owner_sub,
        )

        self._flows[flow_id] = flow
        return flow

    def get(self, flow_id: str) -> Optional[Flow]:
        """Get a flow by ID.

        Args:
            flow_id: The flow ID

        Returns:
            The flow if found, None otherwise
        """
        return self._flows.get(flow_id)

    def update(
        self,
        flow_id: str,
        name: Optional[str] = None,
        workflow: Optional[dict] = None,
    ) -> Optional[Flow]:
        """Update an existing flow with partial fields.

        Preserves unchanged fields and advances updated_at.

        Args:
            flow_id: The ID of the flow to update
            name: New name (optional, preserves existing if None)
            workflow: New workflow (optional, preserves existing if None)

        Returns:
            The updated flow if found, None otherwise
        """
        if flow_id not in self._flows:
            return None

        existing = self._flows[flow_id]
        updates: dict = {"updated_at": datetime.now(timezone.utc)}

        if name is not None:
            updates["name"] = name
        if workflow is not None:
            updates["workflow"] = workflow

        updated = existing.model_copy(update=updates)
        self._flows[flow_id] = updated
        return updated

    def delete(self, flow_id: str) -> bool:
        """Delete a flow by ID.

        Args:
            flow_id: The flow ID

        Returns:
            True if deleted, False if not found
        """
        if flow_id in self._flows:
            del self._flows[flow_id]
            return True
        return False

    def list_all(self) -> list[Flow]:
        """List all flows sorted by updated_at descending.

        Returns:
            List of all flows, most recently updated first
        """
        return sorted(
            self._flows.values(),
            key=lambda c: c.updated_at,
            reverse=True,
        )

    def list_by_owner(self, owner_sub: str) -> list[Flow]:
        """List flows owned by ``owner_sub`` (strict equality).

        Tenant-isolation (Critic Finding 3): legacy / un-owned rows are
        excluded — they have no owner so cannot match any caller.
        """
        return sorted(
            (f for f in self._flows.values() if getattr(f, "owner_sub", None) == owner_sub),
            key=lambda c: c.updated_at,
            reverse=True,
        )

    def clear(self) -> None:
        """Clear all flows (for testing)."""
        self._flows.clear()


# Global storage instance
flow_storage = FlowStorage()

# Runtime-swappable storage reference
_active_storage = flow_storage


def get_flow_storage():
    """Get the active flow storage instance."""
    return _active_storage


def set_flow_storage(storage):
    """Set the active flow storage instance (called by main.py at startup)."""
    global _active_storage
    _active_storage = storage



# ============================================================================
# DynamoDB Flow Storage
# ============================================================================


class DynamoDBFlowStorage:
    """DynamoDB-backed storage for flows.

    Implements the same interface as the in-memory FlowStorage
    but persists flows to a DynamoDB table.

    Requirements: 1.1, 2.1, 6.2, 6.4, 7.1, 7.2
    """

    def __init__(self, table_name: str, region: str) -> None:
        """Initialize DynamoDB storage.

        Args:
            table_name: Name of the DynamoDB table
            region: AWS region where the table exists
        """
        self._table_name = table_name
        self._region = region
        self._dynamodb = _get_dynamodb_resource(region)
        self._table = _get_table(self._dynamodb, table_name)
        logger.info(
            "Initialized DynamoDB flow storage: table=%s, region=%s",
            table_name,
            region,
        )

    def create(self, name: str, owner_sub: Optional[str] = None) -> Flow:
        """Create a new flow in DynamoDB.

        Generates a UUID, creates a Flow with an empty workflow,
        sets timestamps, and writes to DynamoDB.

        Args:
            name: The flow name
            owner_sub: Cognito sub of the creating user (tenant isolation)

        Returns:
            The created Flow with generated ID and timestamps
        """
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        workflow = _create_empty_workflow()

        flow = Flow(
            id=flow_id,
            name=name,
            workflow=workflow,
            deployment_status=DeploymentStatus.NOT_DEPLOYED,
            created_at=now,
            updated_at=now,
            owner_sub=owner_sub,
        )

        item = _serialize_flow(flow)
        _put_item(self._table, item)
        logger.info("Created flow: %s", flow_id)
        return flow

    def get(self, flow_id: str) -> Optional[Flow]:
        """Get a flow by ID from DynamoDB.

        Args:
            flow_id: The flow ID

        Returns:
            The flow if found, None otherwise
        """
        item = _get_item(self._table, {"flow_id": flow_id})
        if item is None:
            return None
        return _deserialize_flow(item)

    def update(
        self,
        flow_id: str,
        name: Optional[str] = None,
        workflow: Optional[dict] = None,
    ) -> Optional[Flow]:
        """Update an existing flow in DynamoDB.

        Accepts partial fields, preserves unchanged fields,
        and advances updated_at.

        Args:
            flow_id: The ID of the flow to update
            name: New name (optional, preserves existing if None)
            workflow: New workflow (optional, preserves existing if None)

        Returns:
            The updated flow if found, None otherwise
        """
        existing_item = _get_item(self._table, {"flow_id": flow_id})
        if existing_item is None:
            return None

        existing = _deserialize_flow(existing_item)
        updates: dict = {"updated_at": datetime.now(timezone.utc)}

        if name is not None:
            updates["name"] = name
        if workflow is not None:
            updates["workflow"] = workflow

        updated = existing.model_copy(update=updates)
        item = _serialize_flow(updated)
        _put_item(self._table, item)
        logger.info("Updated flow: %s", flow_id)
        return updated

    def delete(self, flow_id: str) -> bool:
        """Delete a flow by ID from DynamoDB.

        Args:
            flow_id: The flow ID

        Returns:
            True if deleted, False if not found
        """
        existing = _get_item(self._table, {"flow_id": flow_id})
        if existing is None:
            return False

        _delete_item(self._table, {"flow_id": flow_id})
        logger.info("Deleted flow: %s", flow_id)
        return True

    def list_all(self) -> list[Flow]:
        """List all flows from DynamoDB sorted by updated_at descending.

        Scans the entire table, deserializes all items, and sorts.

        Returns:
            List of all flows, most recently updated first
        """
        items = _scan_table(self._table)
        flows = []
        for item in items:
            try:
                flows.append(_deserialize_flow(item))
            except Exception as e:
                logger.warning("Failed to deserialize flow item: %s", e)

        return sorted(
            flows,
            key=lambda c: c.updated_at,
            reverse=True,
        )

    def list_by_owner(self, owner_sub: str) -> list[Flow]:
        """List flows owned by ``owner_sub`` (strict equality).

        Tenant-isolation (Critic Finding 3): pushes the owner filter to
        DynamoDB via a FilterExpression so non-matching rows never leave
        the database — fewer bytes on the wire AND the in-Python loop
        never sees other tenants' rows even if a serialization bug landed.

        Note: this is still a Scan, not a Query. Adding a GSI on
        ``owner_sub`` is tracked under Critic Finding 9 / a separate
        infra-change PR; we explicitly do not introduce that change here
        to keep this fix minimal in scope.
        """
        from boto3.dynamodb.conditions import Attr

        items: list[dict] = []
        scan_kwargs = {"FilterExpression": Attr("owner_sub").eq(owner_sub)}
        response = self._table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"], **scan_kwargs
            )
            items.extend(response.get("Items", []))

        flows = []
        for item in items:
            try:
                flow = _deserialize_flow(item)
            except Exception as e:
                logger.warning("Failed to deserialize flow item: %s", e)
                continue
            # Defence-in-depth: re-check after deserialization in case a
            # rogue row sneaks past the FilterExpression (legacy items
            # missing the attribute are correctly excluded by Attr.eq).
            if getattr(flow, "owner_sub", None) == owner_sub:
                flows.append(flow)

        return sorted(flows, key=lambda c: c.updated_at, reverse=True)

    def clear(self) -> None:
        """Clear all flows from DynamoDB (for testing).

        Scans the table and deletes each item individually.
        """
        items = _scan_table(self._table)
        for item in items:
            _delete_item(self._table, {"flow_id": item["flow_id"]})
        logger.info("Cleared all flows from table %s", self._table_name)
