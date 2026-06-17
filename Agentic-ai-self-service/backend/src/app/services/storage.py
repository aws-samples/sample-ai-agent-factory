"""In-memory storage service for workflow persistence.

This module provides a simple in-memory storage for workflows.
In production, this would be replaced with a database.

Requirements: 9.1, 9.5
"""

from datetime import datetime, timezone
from typing import Optional
import uuid

from app.models import WorkflowDefinition


class WorkflowStorage:
    """In-memory storage for workflows.

    This is a simple implementation for development/testing.
    In production, this would be replaced with DynamoDB or similar.
    """

    def __init__(self) -> None:
        """Initialize empty storage."""
        self._workflows: dict[str, WorkflowDefinition] = {}

    def create(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        """Create a new workflow.

        Args:
            workflow: The workflow to create

        Returns:
            The created workflow with generated ID if not provided

        Raises:
            ValueError: If workflow with same ID already exists
        """
        if not workflow.id:
            workflow = workflow.model_copy(update={"id": str(uuid.uuid4())})

        if workflow.id in self._workflows:
            raise ValueError(f"Workflow with ID '{workflow.id}' already exists")

        now = datetime.now(timezone.utc)
        workflow = workflow.model_copy(
            update={
                "created_at": now,
                "updated_at": now,
            }
        )

        self._workflows[workflow.id] = workflow
        return workflow

    def get(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        """Get a workflow by ID.

        Args:
            workflow_id: The workflow ID

        Returns:
            The workflow if found, None otherwise
        """
        return self._workflows.get(workflow_id)

    def update(self, workflow_id: str, workflow: WorkflowDefinition) -> Optional[WorkflowDefinition]:
        """Update an existing workflow.

        Args:
            workflow_id: The ID of the workflow to update
            workflow: The updated workflow data

        Returns:
            The updated workflow if found, None otherwise
        """
        if workflow_id not in self._workflows:
            return None

        existing = self._workflows[workflow_id]
        updated = workflow.model_copy(
            update={
                "id": workflow_id,
                "created_at": existing.created_at,
                "updated_at": datetime.now(timezone.utc),
            }
        )

        self._workflows[workflow_id] = updated
        return updated

    def delete(self, workflow_id: str) -> bool:
        """Delete a workflow by ID.

        Args:
            workflow_id: The workflow ID

        Returns:
            True if deleted, False if not found
        """
        if workflow_id in self._workflows:
            del self._workflows[workflow_id]
            return True
        return False

    def list_all(self) -> list[WorkflowDefinition]:
        """List all workflows.

        Returns:
            List of all workflows
        """
        return list(self._workflows.values())

    def clear(self) -> None:
        """Clear all workflows (for testing)."""
        self._workflows.clear()


# Global storage instance
workflow_storage = WorkflowStorage()

# Runtime-swappable storage reference
_active_storage = workflow_storage


def get_workflow_storage():
    """Get the active workflow storage instance."""
    return _active_storage


def set_workflow_storage(storage):
    """Set the active workflow storage instance (called by main.py at startup)."""
    global _active_storage
    _active_storage = storage
