"""Validation engine for workflow configurations.

This module provides the ValidationEngine class that validates:
- Component configurations (required fields, format validation)
- Connection compatibility between components
- Overall workflow structure

Requirements: 13.2
"""

from app.models import (
    # Enums
    AgentCoreComponentType,
    GatewayConfiguration,
    IdentityConfiguration,
    RuntimeConfiguration,
    ComponentConfiguration,
    # Workflow
    ComponentNode,
    ConnectionEdge,
    ValidationError,
    ValidationResult,
    WorkflowDefinition,
)


# Connection compatibility matrix defining which component types can connect
# Key: source component type, Value: list of compatible target component types
CONNECTION_COMPATIBILITY: dict[AgentCoreComponentType, list[AgentCoreComponentType]] = {
    AgentCoreComponentType.RUNTIME: [
        AgentCoreComponentType.GATEWAY,
        AgentCoreComponentType.MEMORY,
        AgentCoreComponentType.CODE_INTERPRETER,
        AgentCoreComponentType.BROWSER,
        AgentCoreComponentType.OBSERVABILITY,
        AgentCoreComponentType.IDENTITY,
        AgentCoreComponentType.EVALUATION,
        AgentCoreComponentType.POLICY,
        AgentCoreComponentType.GUARDRAILS,
        AgentCoreComponentType.A2A,
    ],
    AgentCoreComponentType.GATEWAY: [
        AgentCoreComponentType.RUNTIME,
        AgentCoreComponentType.IDENTITY,
        AgentCoreComponentType.POLICY,
        AgentCoreComponentType.TOOL,
    ],
    AgentCoreComponentType.MEMORY: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.CODE_INTERPRETER: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.BROWSER: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.OBSERVABILITY: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.IDENTITY: [
        AgentCoreComponentType.RUNTIME,
        AgentCoreComponentType.GATEWAY,
    ],
    AgentCoreComponentType.EVALUATION: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.POLICY: [
        AgentCoreComponentType.RUNTIME,
        AgentCoreComponentType.GATEWAY,
    ],
    AgentCoreComponentType.A2A: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.GUARDRAILS: [
        AgentCoreComponentType.RUNTIME,
    ],
    AgentCoreComponentType.TOOL: [
        AgentCoreComponentType.GATEWAY,
    ],
}


# Required fields per component type
REQUIRED_FIELDS: dict[AgentCoreComponentType, list[str]] = {
    AgentCoreComponentType.RUNTIME: ["name", "framework", "model", "system_prompt"],
    AgentCoreComponentType.GATEWAY: ["name", "target_type", "target_config"],
    AgentCoreComponentType.MEMORY: ["name"],
    AgentCoreComponentType.CODE_INTERPRETER: ["name"],
    AgentCoreComponentType.BROWSER: ["name"],
    AgentCoreComponentType.OBSERVABILITY: ["name"],
    AgentCoreComponentType.IDENTITY: ["name", "credential_type"],
    AgentCoreComponentType.EVALUATION: ["name"],
    AgentCoreComponentType.POLICY: ["name"],
    AgentCoreComponentType.A2A: ["name"],
    AgentCoreComponentType.GUARDRAILS: ["name"],
    AgentCoreComponentType.TOOL: ["name"],
}


class ValidationEngine:
    """Validates workflow configurations.

    This class provides methods to validate:
    - Individual component configurations
    - Connection compatibility between components
    - Complete workflow definitions

    Requirements: 13.2
    """

    def validate_workflow(self, workflow: WorkflowDefinition) -> ValidationResult:
        """Validate entire workflow including all components and connections.

        Args:
            workflow: The workflow definition to validate

        Returns:
            ValidationResult with is_valid flag and any errors/warnings
        """
        errors: list[ValidationError] = []
        warnings: list[ValidationError] = []

        # Build node lookup for edge validation
        node_map: dict[str, ComponentNode] = {  # noqa: F841
            node.id: node for node in workflow.nodes
        }

        # Validate each component
        for node in workflow.nodes:
            node_errors = self.validate_component(node)
            errors.extend(node_errors)

        # Validate each connection
        for edge in workflow.edges:
            edge_errors = self.validate_connection(edge, workflow.nodes)
            errors.extend(edge_errors)

        # Check for orphaned nodes (nodes with no connections) - this is a warning
        connected_node_ids = set()
        for edge in workflow.edges:
            connected_node_ids.add(edge.source)
            connected_node_ids.add(edge.target)

        for node in workflow.nodes:
            if node.id not in connected_node_ids and len(workflow.nodes) > 1:
                warnings.append(
                    ValidationError(
                        component_id=node.id,
                        field="connections",
                        message=f"Node '{node.data.name}' has no connections",
                        severity="warning",
                    )
                )

        is_valid = len(errors) == 0

        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
        )

    def validate_component(self, component: ComponentNode) -> list[ValidationError]:
        """Validate a single component configuration.

        Args:
            component: The component node to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[ValidationError] = []

        # Get required fields for this component type
        required_fields = REQUIRED_FIELDS.get(component.type, [])

        # Check required fields
        for field in required_fields:
            field_errors = self._validate_required_field(component.id, component.data, field)
            errors.extend(field_errors)

        # Type-specific validation
        type_errors = self._validate_component_type_specific(component)
        errors.extend(type_errors)

        return errors

    def _validate_required_field(
        self,
        component_id: str,
        config: ComponentConfiguration,
        field: str,
    ) -> list[ValidationError]:
        """Validate that a required field is present and non-empty.

        Args:
            component_id: ID of the component being validated
            config: The component configuration
            field: The field name to check

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[ValidationError] = []

        # Get the field value
        value = getattr(config, field, None)

        if value is None:
            errors.append(
                ValidationError(
                    component_id=component_id,
                    field=field,
                    message=f"Required field '{field}' is missing",
                    severity="error",
                )
            )
        elif isinstance(value, str) and not value.strip():
            errors.append(
                ValidationError(
                    component_id=component_id,
                    field=field,
                    message=f"Required field '{field}' cannot be empty",
                    severity="error",
                )
            )

        return errors

    def _validate_component_type_specific(self, component: ComponentNode) -> list[ValidationError]:
        """Perform type-specific validation for a component.

        Args:
            component: The component node to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[ValidationError] = []

        if component.type == AgentCoreComponentType.RUNTIME:
            errors.extend(self._validate_runtime_config(component))
        elif component.type == AgentCoreComponentType.GATEWAY:
            errors.extend(self._validate_gateway_config(component))
        elif component.type == AgentCoreComponentType.IDENTITY:
            errors.extend(self._validate_identity_config(component))
        # Memory, CodeInterpreter, Browser, Observability have minimal validation

        return errors

    def _validate_runtime_config(self, component: ComponentNode) -> list[ValidationError]:
        """Validate RuntimeConfiguration specific rules."""
        errors: list[ValidationError] = []
        config = component.data

        if not isinstance(config, RuntimeConfiguration):
            return errors

        # Validate model configuration
        if config.model:
            if not config.model.model_id:
                errors.append(
                    ValidationError(
                        component_id=component.id,
                        field="model.model_id",
                        message="Model ID is required",
                        severity="error",
                    )
                )

        return errors

    def _validate_gateway_config(self, component: ComponentNode) -> list[ValidationError]:
        """Validate GatewayConfiguration specific rules."""
        errors: list[ValidationError] = []
        config = component.data

        if not isinstance(config, GatewayConfiguration):
            return errors

        # Target config type must match target_type
        if config.target_config.type != config.target_type.value:
            errors.append(
                ValidationError(
                    component_id=component.id,
                    field="target_config",
                    message=f"Target config type '{config.target_config.type}' does not match target_type '{config.target_type.value}'",
                    severity="error",
                )
            )

        return errors

    def _validate_identity_config(self, component: ComponentNode) -> list[ValidationError]:
        """Validate IdentityConfiguration specific rules."""
        errors: list[ValidationError] = []
        config = component.data

        if not isinstance(config, IdentityConfiguration):
            return errors

        # Check that appropriate config is provided based on credential_type
        if config.credential_type == "oauth2" and config.oauth2_config is None:
            errors.append(
                ValidationError(
                    component_id=component.id,
                    field="oauth2_config",
                    message="OAuth2 configuration is required when credential_type is 'oauth2'",
                    severity="error",
                )
            )
        elif config.credential_type == "api_key" and config.api_key_config is None:
            errors.append(
                ValidationError(
                    component_id=component.id,
                    field="api_key_config",
                    message="API key configuration is required when credential_type is 'api_key'",
                    severity="error",
                )
            )

        return errors

    def validate_connection(self, edge: ConnectionEdge, nodes: list[ComponentNode]) -> list[ValidationError]:
        """Validate a connection between two components.

        Args:
            edge: The connection edge to validate
            nodes: List of all nodes in the workflow

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[ValidationError] = []

        # Build node lookup
        node_map: dict[str, ComponentNode] = {node.id: node for node in nodes}

        # Check that source and target nodes exist
        source_node = node_map.get(edge.source)
        target_node = node_map.get(edge.target)

        if source_node is None:
            errors.append(
                ValidationError(
                    component_id=edge.id,
                    field="source",
                    message=f"Source node '{edge.source}' does not exist",
                    severity="error",
                )
            )
            return errors

        if target_node is None:
            errors.append(
                ValidationError(
                    component_id=edge.id,
                    field="target",
                    message=f"Target node '{edge.target}' does not exist",
                    severity="error",
                )
            )
            return errors

        # Check connection compatibility
        if not self.get_compatible_ports(source_node.type, target_node.type):
            errors.append(
                ValidationError(
                    component_id=edge.id,
                    field="connection",
                    message=f"Incompatible connection: {source_node.type.value} cannot connect to {target_node.type.value}",
                    severity="error",
                )
            )

        return errors

    def get_compatible_ports(self, source_type: AgentCoreComponentType, target_type: AgentCoreComponentType) -> bool:
        """Check if two component types can be connected.

        Args:
            source_type: The source component type
            target_type: The target component type

        Returns:
            True if the connection is compatible, False otherwise
        """
        compatible_targets = CONNECTION_COMPATIBILITY.get(source_type, [])
        return target_type in compatible_targets
