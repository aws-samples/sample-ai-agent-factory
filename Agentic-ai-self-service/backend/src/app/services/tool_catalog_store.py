"""DynamoDB storage adapter for the Tool Catalog.

Provides CRUD operations and GSI queries for ``CatalogTool`` records.
Follows the same patterns as ``deployment_state_store.py``.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from app.models.catalog_models import CatalogTool, ToolStatus

logger = logging.getLogger(__name__)


# ============================================================================
# Boto3 Wrapper Functions
# ============================================================================


def _get_dynamodb_resource(region: str):
    return boto3.resource("dynamodb", region_name=region)


def _get_table(dynamodb_resource, table_name: str):
    return dynamodb_resource.Table(table_name)


def _put_item(table, item: dict) -> dict:
    return table.put_item(Item=item)


def _get_item(table, key: dict) -> dict | None:
    response = table.get_item(Key=key)
    return response.get("Item")


def _update_item(
    table,
    key: dict,
    update_expr: str,
    expr_values: dict,
    expr_names: dict | None = None,
    condition_expr: str | None = None,
) -> dict:
    kwargs = {
        "Key": key,
        "UpdateExpression": update_expr,
        "ExpressionAttributeValues": expr_values,
    }
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names
    if condition_expr:
        kwargs["ConditionExpression"] = condition_expr
    return table.update_item(**kwargs)


def _delete_item(table, key: dict) -> dict:
    return table.delete_item(Key=key)


def _query_index(table, index_name: str, key_expr: str, expr_values: dict) -> list[dict]:
    items = []
    kwargs = {
        "IndexName": index_name,
        "KeyConditionExpression": key_expr,
        "ExpressionAttributeValues": expr_values,
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


# ============================================================================
# Serialization Helpers
# ============================================================================


def _convert_floats_to_decimals(obj):
    if isinstance(obj, float):
        if obj != 0.0 and abs(obj) < 1e-130:
            return Decimal("0")
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _convert_floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_floats_to_decimals(v) for v in obj]
    return obj


def _convert_decimals_to_floats(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals_to_floats(v) for v in obj]
    return obj


def serialize_catalog_tool(tool: CatalogTool) -> dict:
    item = tool.model_dump(mode="json")
    # DynamoDB GSI keys cannot be NULL — strip None values so items
    # missing a sort-key attribute are simply omitted from the index.
    item = {k: v for k, v in item.items() if v is not None}
    return _convert_floats_to_decimals(item)


def deserialize_catalog_tool(item: dict) -> CatalogTool:
    data = _convert_decimals_to_floats(dict(item))
    return CatalogTool.model_validate(data)


# ============================================================================
# Tool Catalog Store Class
# ============================================================================


class ToolCatalogStore:
    """DynamoDB-backed store for the Tool Catalog.

    Supports CRUD, status-based queries (via GSI), and creator-based queries.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._dynamodb = _get_dynamodb_resource(region)
        self._table = _get_table(self._dynamodb, table_name)
        logger.info("Initialized ToolCatalogStore: table=%s, region=%s", table_name, region)

    def create(self, tool: CatalogTool) -> CatalogTool:
        item = serialize_catalog_tool(tool)
        _put_item(self._table, item)
        logger.info("Created catalog tool: %s (%s)", tool.tool_id, tool.tool_name)
        return tool

    def get(self, tool_id: str) -> CatalogTool | None:
        item = _get_item(self._table, {"tool_id": tool_id})
        if item is None:
            return None
        return deserialize_catalog_tool(item)

    def update(self, tool_id: str, updates: dict) -> CatalogTool | None:
        """Update specific fields on a catalog tool.

        Args:
            tool_id: The tool's partition key.
            updates: Dict of field_name -> new_value to apply.

        Returns:
            The updated CatalogTool, or None if not found.
        """
        existing = self.get(tool_id)
        if existing is None:
            return None

        # Always bump updated_at
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_parts = []
        expr_values = {}
        expr_names = {}

        for i, (field, value) in enumerate(updates.items()):
            placeholder = f":v{i}"
            name_placeholder = f"#n{i}"
            set_parts.append(f"{name_placeholder} = {placeholder}")
            expr_values[placeholder] = _convert_floats_to_decimals(value)
            expr_names[name_placeholder] = field

        update_expr = "SET " + ", ".join(set_parts)

        _update_item(
            self._table,
            key={"tool_id": tool_id},
            update_expr=update_expr,
            expr_values=expr_values,
            expr_names=expr_names,
        )
        logger.info("Updated catalog tool: %s", tool_id)
        return self.get(tool_id)

    def update_with_condition(self, tool_id: str, updates: dict, expected_status: ToolStatus) -> CatalogTool | None:
        """Update a tool only if its current status matches expected_status.

        Uses DynamoDB ConditionExpression to prevent race conditions on
        concurrent approve/reject operations.
        """
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_parts = []
        expr_values = {":expected_status": expected_status.value}
        expr_names = {}

        for i, (field, value) in enumerate(updates.items()):
            placeholder = f":v{i}"
            name_placeholder = f"#n{i}"
            set_parts.append(f"{name_placeholder} = {placeholder}")
            expr_values[placeholder] = _convert_floats_to_decimals(value)
            expr_names[name_placeholder] = field

        update_expr = "SET " + ", ".join(set_parts)
        condition_expr = "#status_field = :expected_status"
        expr_names["#status_field"] = "status"

        try:
            _update_item(
                self._table,
                key={"tool_id": tool_id},
                update_expr=update_expr,
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=condition_expr,
            )
            logger.info("Conditionally updated catalog tool: %s", tool_id)
            return self.get(tool_id)
        except self._dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            logger.warning(
                "Conditional update failed for tool %s (expected status: %s)",
                tool_id,
                expected_status.value,
            )
            return None

    def delete(self, tool_id: str) -> bool:
        try:
            _delete_item(self._table, {"tool_id": tool_id})
            logger.info("Deleted catalog tool: %s", tool_id)
            return True
        except Exception as e:
            logger.warning("Failed to delete tool %s: %s", tool_id, e)
            return False

    def list_by_status(self, status: ToolStatus) -> list[CatalogTool]:
        items_raw = []
        kwargs = {
            "IndexName": "status-index",
            "KeyConditionExpression": "#s = :status",
            "ExpressionAttributeValues": {":status": status.value},
            "ExpressionAttributeNames": {"#s": "status"},
        }
        while True:
            response = self._table.query(**kwargs)
            items_raw.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        return [deserialize_catalog_tool(item) for item in items_raw]

    def list_by_creator(self, created_by: str) -> list[CatalogTool]:
        items_raw = []
        kwargs = {
            "IndexName": "created_by-index",
            "KeyConditionExpression": "created_by = :cb",
            "ExpressionAttributeValues": {":cb": created_by},
        }
        while True:
            response = self._table.query(**kwargs)
            items_raw.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        return [deserialize_catalog_tool(item) for item in items_raw]

    def list_approved(self) -> list[CatalogTool]:
        return self.list_by_status(ToolStatus.APPROVED)
