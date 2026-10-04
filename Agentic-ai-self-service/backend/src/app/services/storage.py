"""In-memory storage service for workflow persistence.

This module provides a simple in-memory storage for workflows.
In production, this would be replaced with a database.

Requirements: 9.1, 9.5
"""

import uuid
from datetime import datetime, timezone

from app.models import WorkflowDefinition


class WorkflowRevisionConflict(Exception):
    """A save was built on a ``WorkflowDefinition.revision`` the row no longer has (F-15).

    Raised by both stores instead of overwriting. ``current`` is the row as it is NOW (re-read
    after the refused write), so the router can tell the client which revision to reload or to
    adopt before saving again; ``None`` when the row vanished underneath the save.
    """

    def __init__(self, workflow_id: str, expected: int, current: WorkflowDefinition | None):
        self.workflow_id = workflow_id
        self.expected = expected
        self.current = current
        actual = "missing" if current is None else str(current.revision)
        super().__init__(f"Workflow '{workflow_id}' is at revision {actual}, save was built on revision {expected}")


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
                "revision": 0,
            }
        )

        self._workflows[workflow.id] = workflow
        return workflow

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        """Get a workflow by ID.

        Args:
            workflow_id: The workflow ID

        Returns:
            The workflow if found, None otherwise
        """
        return self._workflows.get(workflow_id)

    def get_owner_sub_unvalidated(self, workflow_id: str) -> tuple[bool, str | None]:
        """Read one row's ``owner_sub`` without parsing it into a model.

        Part of the storage interface so the router's "delete a row I cannot read"
        path does not have to ask whether the backend supports it. This backend
        holds already-parsed models, so it can never HAVE an unreadable row and this
        is equivalent to ``get``; it is the DynamoDB implementation that matters.
        See ``DynamoDBWorkflowStorage.get_owner_sub_unvalidated``.
        """
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            return (False, None)
        owner_sub = getattr(workflow, "owner_sub", None)
        return (True, owner_sub if isinstance(owner_sub, str) else None)

    def update(
        self,
        workflow_id: str,
        workflow: WorkflowDefinition,
        *,
        expected_revision: int | None = None,
    ) -> WorkflowDefinition | None:
        """Update an existing workflow as a compare-and-set on its revision (F-15).

        Args:
            workflow_id: The ID of the workflow to update
            workflow: The updated workflow data
            expected_revision: The ``revision`` the caller built this save on; when given and
                different from the stored row's, nothing is written. ``None`` (a client that
                sends no revision) saves against whatever is stored.

        Returns:
            The updated workflow if found, None otherwise

        Raises:
            WorkflowRevisionConflict: the row is not at ``expected_revision``
        """
        if workflow_id not in self._workflows:
            return None

        existing = self._workflows[workflow_id]
        if expected_revision is not None and existing.revision != expected_revision:
            raise WorkflowRevisionConflict(workflow_id, expected_revision, existing)
        updated = workflow.model_copy(
            update={
                "id": workflow_id,
                "created_at": existing.created_at,
                "updated_at": datetime.now(timezone.utc),
                "revision": existing.revision + 1,
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
