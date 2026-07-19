"""DynamoDB tables for the PlatformStack."""

from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb

from .config import PlatformConfig


@dataclass(frozen=True)
class Tables:
    """All DynamoDB tables created by the stack, grouped for later builders."""

    workflows: dynamodb.Table
    deployments: dynamodb.Table
    flows: dynamodb.Table
    agent_versions: dynamodb.Table
    runtime_slots: dynamodb.Table
    agent_registry: dynamodb.Table
    usage_events: dynamodb.Table
    hitl_requests: dynamodb.Table
    triggers: dynamodb.Table
    prompt_library: dynamodb.Table
    tag_policy: dynamodb.Table
    budget: dynamodb.Table
    audit: dynamodb.Table
    permission_requests: dynamodb.Table


def build_tables(stack: cdk.Stack, cfg: PlatformConfig) -> Tables:
    """Create every DynamoDB table (construct ids unchanged from the monolith)."""
    return Tables(
        workflows=_create_workflows_table(stack, cfg),
        deployments=_create_deployments_table(stack, cfg),
        flows=_create_flows_table(stack, cfg),
        # Phase 1 Gap 1A — versioning + slot tables.
        agent_versions=_create_agent_versions_table(stack, cfg),
        runtime_slots=_create_runtime_slots_table(stack, cfg),
        # Phase 2 Gap 2A — agent registry / catalog table.
        agent_registry=_create_agent_registry_table(stack, cfg),
        # Phase 2 Gap 2B — usage events table (optional/dormant write path for
        # explicit per-invocation usage events; primary cost path is query-time).
        usage_events=_create_usage_events_table(stack, cfg),
        # Phase 2 Gap 2D — human-in-the-loop approval requests table.
        hitl_requests=_create_hitl_requests_table(stack, cfg),
        # Phase 3 Gap 3F — scheduled / event triggers registry table.
        triggers=_create_triggers_table(stack, cfg),
        # Phase 3 Gap 3H — prompt library / catalog table.
        prompt_library=_create_prompt_library_table(stack, cfg),
        # Phase 2 (Loom) governance tagging — tag policies + tag profiles table.
        tag_policy=_create_tag_policy_table(stack, cfg),
        # Phase 4 (Loom) FinOps — cost budgets table.
        budget=_create_budget_table(stack, cfg),
        # Phase 5 (Loom) — action-audit trail table.
        audit=_create_audit_table(stack, cfg),
        permission_requests=_create_permission_requests_table(stack, cfg),
    )


def _create_workflows_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Create DynamoDB table for workflow storage (kept from previous arch).

    Requirements: 7.1
    """
    return dynamodb.Table(
        stack,
        "WorkflowsTable",
        table_name=f"{cfg.project}-{cfg.env}-workflows",
        partition_key=dynamodb.Attribute(
            name="workflow_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        # Audit #9: gated on env so prod doesn't lose data on teardown.
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_deployments_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Create DynamoDB table for deployment state with TTL and GSI.

    Requirements: 4.1, 4.2, 4.3, 7.1
    """
    table = dynamodb.Table(
        stack,
        "DeploymentsTable",
        table_name=f"{cfg.project}-{cfg.env}-deployments",
        partition_key=dynamodb.Attribute(
            name="deployment_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        time_to_live_attribute="ttl",
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="workflow_id-index",
        partition_key=dynamodb.Attribute(
            name="workflow_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    table.add_global_secondary_index(
        index_name="user_id-index",
        partition_key=dynamodb.Attribute(
            name="user_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    # Audit issue #7: deployment_handler._scan_for_runtime previously did
    # a full O(N) Scan on every test/delete. Adding a runtime_id GSI lets
    # the handler use Query instead — O(1) on the GSI partition key.
    table.add_global_secondary_index(
        index_name="runtime_id-index",
        partition_key=dynamodb.Attribute(
            name="runtime_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_flows_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Create DynamoDB table for named, saveable flow persistence.

    Requirements: 7.1
    """
    return dynamodb.Table(
        stack,
        "FlowsTable",
        table_name=f"{cfg.project}-{cfg.env}-flows",
        partition_key=dynamodb.Attribute(
            name="flow_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_agent_versions_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 1 Gap 1A — DynamoDB table for AgentVersions.

    PK: ``runtime_name`` (the friendly name the user typed)
    SK: ``version_id`` (sortable id; lex order = chronological)
    GSI: ``owner_sub-version_id-index`` for list-by-user queries.

    Composite key supports list-versions-of-a-runtime via Query (newest
    first via ScanIndexForward=False). The owner_sub GSI supports the
    cross-runtime "all my versions" view for a future registry tab.
    """
    table = dynamodb.Table(
        stack,
        "AgentVersionsTable",
        table_name=f"{cfg.project}-{cfg.env}-agent-versions",
        partition_key=dynamodb.Attribute(
            name="runtime_name",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="version_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-version_id-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="version_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_runtime_slots_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 1 Gap 1A — DynamoDB table for runtime production/staging slots.

    PK: ``runtime_name``. One row per friendly name. Stores which version
    is currently in production vs. staging, plus the previous-production
    pointer used by /rollback. Owner_sub is on the row itself (not a GSI)
    because reads are always keyed on runtime_name.
    """
    return dynamodb.Table(
        stack,
        "RuntimeSlotsTable",
        table_name=f"{cfg.project}-{cfg.env}-runtime-slots",
        partition_key=dynamodb.Attribute(
            name="runtime_name",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_agent_registry_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 2 Gap 2A — DynamoDB table for the agent registry / catalog.

    PK: ``org_id``, SK: ``agent_slug``. One row per published agent.
    GSI ``owner_sub-agent_slug-index`` for list-by-publisher.
    GSI ``visibility-agent_slug-index`` for list-public discovery.

    Visibility model (private/org/public) is enforced in routers/registry.py;
    the table stores the raw entries and the router filters on read.
    """
    table = dynamodb.Table(
        stack,
        "AgentRegistryTable",
        table_name=f"{cfg.project}-{cfg.env}-agent-registry",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="agent_slug",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-agent_slug-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="agent_slug",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    table.add_global_secondary_index(
        index_name="visibility-agent_slug-index",
        partition_key=dynamodb.Attribute(
            name="visibility",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="agent_slug",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_hitl_requests_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 2 Gap 2D — DynamoDB table for human-in-the-loop approvals.

    PK ``runtime_id`` (the agent-stamped AgentCore runtime NAME), SK
    ``request_id`` (sortable). GSI ``owner_sub-request_id-index`` powers the
    tenant-scoped pending queue. Rows carry a ``ttl`` (24h) so DynamoDB
    auto-expires decided/abandoned requests — no destroy_runtime cascade.
    """
    table = dynamodb.Table(
        stack,
        "HitlRequestsTable",
        table_name=f"{cfg.project}-{cfg.env}-hitl-requests",
        partition_key=dynamodb.Attribute(
            name="runtime_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="request_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        time_to_live_attribute="ttl",
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-request_id-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="request_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_permission_requests_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Loom-study 1.6 — JIT IAM permission-request workflow table.

    PK ``org_id`` (tenant), SK ``request_id`` (sortable). GSI
    ``status-request_id-index`` powers the admin pending-review queue. A
    builder requests specific IAM actions+resources on a managed role with a
    justification; a security approver approves, and on approval the role's
    inline policy is widened. No TTL — an auditable escalation history.
    """
    table = dynamodb.Table(
        stack,
        "PermissionRequestsTable",
        table_name=f"{cfg.project}-{cfg.env}-permission-requests",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="request_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="status-request_id-index",
        partition_key=dynamodb.Attribute(
            name="status",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="request_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_triggers_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 3 Gap 3F — DynamoDB table for scheduled / event triggers.

    PK ``runtime_name`` (tenant-supplied friendly name; the router gates
    every write/list/delete through the production-slot owner, so the Bug
    122 PK-collision class is closed by ownership resolution). SK
    ``trigger_id`` (sortable hex). GSI ``owner_sub-trigger_id-index`` powers
    the owner-scoped list-across-runtimes query. No TTL — rows live until the
    trigger is deleted or destroy_runtime cleans them up (Bug 124).
    """
    table = dynamodb.Table(
        stack,
        "TriggersTable",
        table_name=f"{cfg.project}-{cfg.env}-triggers",
        partition_key=dynamodb.Attribute(
            name="runtime_name",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="trigger_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-trigger_id-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="trigger_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_prompt_library_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 3 Gap 3H — DynamoDB table for the prompt library / catalog.

    PK ``org_id``, SK ``prompt_name``. One row per saved prompt. GSI
    ``owner_sub-prompt_name-index`` for the list-by-author view. Mirrors
    _create_agent_registry_table; routers/prompts.py (mounted on the
    deployment Lambda) reads/writes this table.
    """
    table = dynamodb.Table(
        stack,
        "PromptLibraryTable",
        table_name=f"{cfg.project}-{cfg.env}-prompt-library",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="prompt_name",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-prompt_name-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="prompt_name",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table


def _create_tag_policy_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 2 (Loom) governance tagging — tag policies + tag profiles.

    PK ``org_id``, SK ``POLICY#<key>`` | ``PROFILE#<name>`` (single-table,
    record kind discriminated by SK prefix — see services/tag_policy_store).
    Low-volume org-wide config; no GSI. Mirrors _create_prompt_library_table.
    """
    return dynamodb.Table(
        stack,
        "TagPolicyTable",
        table_name=f"{cfg.project}-{cfg.env}-tag-policy",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="sk",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_budget_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 4 (Loom) FinOps — cost budgets table.

    PK ``org_id``, SK ``BUDGET#<scope>#<key>`` (single-table; see
    services/budget_store). Low-volume org config; no GSI.
    """
    return dynamodb.Table(
        stack,
        "BudgetTable",
        table_name=f"{cfg.project}-{cfg.env}-budget",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="sk",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_audit_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 5 (Loom) — action-audit trail.

    PK ``org_id``, SK ``<ts_iso>#<event_id>`` (sortable). TTL on ``ttl``
    (90-day) bounds growth. See services/audit_store.
    """
    return dynamodb.Table(
        stack,
        "AuditTable",
        table_name=f"{cfg.project}-{cfg.env}-audit",
        partition_key=dynamodb.Attribute(
            name="org_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="sk",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        time_to_live_attribute="ttl",
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )


def _create_usage_events_table(stack: cdk.Stack, cfg: PlatformConfig) -> dynamodb.Table:
    """Phase 2 Gap 2B — DynamoDB table for explicit per-invocation usage
    events (cost analytics + FinOps).

    PK ``runtime_id`` (AWS-assigned, never tenant-supplied), SK
    ``event_id`` (sortable). GSI ``owner_sub-event_id-index`` for the
    list-by-owner cross-runtime view. TTL on ``ttl`` (90-day) bounds growth.

    OPTIONAL / DORMANT in the primary flow: the cost endpoint derives
    cost at query-time from CloudWatch Logs gen_ai.usage attrs, so no
    rows are written until a future codegen span-processor hook lands.
    """
    table = dynamodb.Table(
        stack,
        "UsageEventsTable",
        table_name=f"{cfg.project}-{cfg.env}-usage-events",
        partition_key=dynamodb.Attribute(
            name="runtime_id",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="event_id",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=cfg.removal_policy,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        time_to_live_attribute="ttl",
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
    )
    table.add_global_secondary_index(
        index_name="owner_sub-event_id-index",
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="event_id",
            type=dynamodb.AttributeType.STRING,
        ),
    )
    return table
