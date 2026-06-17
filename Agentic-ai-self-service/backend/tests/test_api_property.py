"""Property-based tests for API endpoints.

These tests verify:
- Import Schema Validation
- Invalid Import Error Display
- CRUD Operations

Requirements: 9.1, 9.5, 14.3, 14.4
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone
from typing import Any

from hypothesis import given, settings, strategies as st
from fastapi.testclient import TestClient

from app.main import app
from app.services.storage import get_workflow_storage


# Test client
client = TestClient(app)


# ============================================================================
# Hypothesis Strategies for Valid Data Generation
# ============================================================================

valid_name_st = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

valid_id_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

valid_version_st = st.from_regex(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", fullmatch=True)

valid_aws_region_st = st.sampled_from(
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-central-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
    ]
)


def make_valid_workflow_json(
    workflow_id: str = "test-id",
    name: str = "Test Workflow",
    version: str = "1.0.0",
    aws_region: str = "us-east-1",
) -> dict[str, Any]:
    """Create a valid workflow JSON for testing."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": workflow_id,
        "name": name,
        "description": "Test workflow description",
        "version": version,
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "metadata": {
            "author": "test-author",
            "tags": ["test"],
            "aws_region": aws_region,
            "deployment_status": "not_deployed",
        },
        "created_at": now,
        "updated_at": now,
    }


# ============================================================================
# Health Check Tests
# ============================================================================


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    def test_health_returns_healthy(self):
        """Health endpoint should return healthy status."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


# ============================================================================
# Workflow CRUD Tests
# ============================================================================


class TestWorkflowCRUD:
    """Tests for workflow CRUD operations."""

    def setup_method(self):
        """Clear storage before each test."""
        get_workflow_storage().clear()

    def test_create_workflow(self):
        """Should create a new workflow."""
        response = client.post(
            "/api/workflows",
            json={
                "name": "Test Workflow",
                "description": "A test workflow",
                "version": "1.0.0",
                "nodes": [],
                "edges": [],
                "metadata": {
                    "author": "test-author",
                    "aws_region": "us-west-2",
                },
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["workflow"]["name"] == "Test Workflow"
        assert "id" in data["workflow"]

    def test_get_workflow_not_found(self):
        """Should return 404 for non-existent workflow."""
        response = client.get("/api/workflows/non-existent-id")
        assert response.status_code == 404

    def test_delete_workflow_not_found(self):
        """Should return 404 when deleting non-existent workflow."""
        response = client.delete("/api/workflows/non-existent-id")
        assert response.status_code == 404


# ============================================================================
# Import/Export Tests
# ============================================================================


class TestWorkflowImportExport:
    """Tests for workflow import/export functionality."""

    def setup_method(self):
        """Clear storage before each test."""
        get_workflow_storage().clear()

    def test_import_valid_workflow(self):
        """Should successfully import a valid workflow JSON."""
        workflow_json = make_valid_workflow_json()
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Workflow imported successfully"
        assert "workflow" in data

    def test_import_invalid_version_format(self):
        """Should reject workflow with invalid version format."""
        workflow_json = make_valid_workflow_json(version="invalid")
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data["detail"]

    def test_import_invalid_aws_region(self):
        """Should reject workflow with invalid AWS region."""
        workflow_json = make_valid_workflow_json(aws_region="invalid-region")
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 400

    def test_import_missing_required_field(self):
        """Should reject workflow missing required fields."""
        workflow_json = {"name": "Test"}  # Missing most required fields
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data["detail"]

    def test_export_workflow(self):
        """Should export an existing workflow."""
        # First create a workflow
        create_response = client.post(
            "/api/workflows",
            json={
                "name": "Export Test",
                "version": "1.0.0",
                "metadata": {
                    "author": "test",
                    "aws_region": "us-west-2",
                },
            },
        )
        workflow_id = create_response.json()["workflow"]["id"]

        # Then export it
        response = client.get(f"/api/workflows/{workflow_id}/export")
        assert response.status_code == 200
        data = response.json()
        assert "workflow_json" in data
        assert data["workflow_json"]["name"] == "Export Test"

    def test_export_not_found(self):
        """Should return 404 for non-existent workflow export."""
        response = client.get("/api/workflows/non-existent/export")
        assert response.status_code == 404

    @given(valid_version_st)
    @settings(max_examples=20)
    def test_import_accepts_valid_versions(self, version: str):
        """Should accept any valid semantic version."""
        get_workflow_storage().clear()
        workflow_json = make_valid_workflow_json(version=version)
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 200

    @given(valid_aws_region_st)
    @settings(max_examples=10)
    def test_import_accepts_valid_regions(self, region: str):
        """Should accept any valid AWS region."""
        get_workflow_storage().clear()
        workflow_json = make_valid_workflow_json(aws_region=region)
        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": workflow_json},
        )
        assert response.status_code == 200


# ============================================================================
# Validation Endpoint Tests
# ============================================================================


class TestValidationEndpoint:
    """Tests for workflow validation endpoint."""

    def setup_method(self):
        """Clear storage before each test."""
        get_workflow_storage().clear()

    def test_validate_empty_workflow(self):
        """Should validate an empty workflow."""
        # Create workflow first
        create_response = client.post(
            "/api/workflows",
            json={
                "name": "Validation Test",
                "version": "1.0.0",
                "metadata": {
                    "author": "test",
                    "aws_region": "us-west-2",
                },
            },
        )
        workflow_id = create_response.json()["workflow"]["id"]

        # Validate it
        response = client.post(f"/api/workflows/{workflow_id}/validate")
        assert response.status_code == 200
        data = response.json()
        assert "is_valid" in data

    def test_validate_not_found(self):
        """Should return 404 for non-existent workflow validation."""
        response = client.post("/api/workflows/non-existent/validate")
        assert response.status_code == 404


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def setup_method(self):
        """Clear storage before each test."""
        get_workflow_storage().clear()

    def test_empty_name_rejected(self):
        """Should reject workflow with empty name."""
        response = client.post(
            "/api/workflows",
            json={
                "name": "",
                "version": "1.0.0",
                "metadata": {
                    "author": "test",
                    "aws_region": "us-west-2",
                },
            },
        )
        assert response.status_code == 422  # Validation error

    def test_name_too_long_rejected(self):
        """Should reject workflow with name exceeding max length."""
        response = client.post(
            "/api/workflows",
            json={
                "name": "x" * 201,  # Max is 200
                "version": "1.0.0",
                "metadata": {
                    "author": "test",
                    "aws_region": "us-west-2",
                },
            },
        )
        assert response.status_code == 422

    def test_description_too_long_rejected(self):
        """Should reject workflow with description exceeding max length."""
        response = client.post(
            "/api/workflows",
            json={
                "name": "Test",
                "description": "x" * 2001,  # Max is 2000
                "version": "1.0.0",
                "metadata": {
                    "author": "test",
                    "aws_region": "us-west-2",
                },
            },
        )
        assert response.status_code == 422
