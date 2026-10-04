"""Per-step IAM roles (1:1) + step Lambda functions for the deploy pipeline."""

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3

from .buckets import deny_writes_to_agentcore_deps
from .cognito_client_secret_grant import grant_client_secret_read
from .config import (
    ATTACHABLE_MANAGED_POLICIES,
    GOVERNANCE_TAG_KEY_PREFIXES_ENV,
    PlatformConfig,
    governance_tag_key_globs,
    governance_tag_key_prefixes_env_value,
)
from .otel import OtelConfig
from .regional_artifact_bucket_grant import (
    LIST_VERSION_ACTIONS,
    READ_TAG_ACTIONS,
    grant_regional_artifact_buckets,
)
from .role_boundary import BOUNDARY_ARN_ENV, boundary_conditions, grant_boundary_retrofit
from .shared_role_guard import deny_mutating_the_shared_runtime_roles
from .tables import RECOVERY_READ_ATTRIBUTES, Tables
from .tool_lambda_names import tool_function_arn_patterns, tool_function_log_group_arn_patterns

#: AgentCore resource type -> the ARN tail a ``bedrock-agentcore:TagResource`` grant must
#: name for it. Every id is wildcarded because AgentCore mints it at create time and none
#: is knowable at synth time; the account stays pinned by the caller.
#:
#: Read from the AWS Service Reference feed's own ``ARNFormats``
#: (``https://servicereference.us-east-1.amazonaws.com/v1/bedrock-agentcore/bedrock-agentcore.json``),
#: which is the sanctioned oracle for whether a resource shape exists -- not the docs, and
#: not a live ``list-*`` call. Two of these were NOT observable from the account at all:
#: ``list-policy-engines`` returns ``null`` where none exist and ``list-gateways`` returned
#: ``gatewayArn: None``, so guessing ``policy-engine/*`` and ``harness/*`` from the others
#: would have been a guess. The four that WERE observable agree with the feed exactly,
#: which is what makes the feed trustworthy for the two that were not.
#:
#: ``default`` is a literal rather than ``*`` in the two nested shapes: it is the only token
#: vault and the only workload-identity directory AgentCore creates, so a wildcard there
#: would extend the grant to a second one nobody has.
#:
#: Each value is a TUPLE because a nested shape needs TWO ARNs, not one. AgentCore performs
#: two separate authorizations for one logical call -- once against the CONTAINER
#: (``token-vault/default``, ``workload-identity-directory/default``) and once against the
#: per-resource CHILD -- and the 403 names whichever check it reached first. A grant naming
#: only the child does not match the container, so it is a live outage that reads like a
#: typo in the policy. This is the same mechanism, on the same two shapes, that the
#: DeleteWorkloadIdentity statement below already documents from five throwaway roles
#: (search this file for "BOTH resource ARNs are required"); it was fixed there for the
#: delete verbs in 2026-09-21 and left unfixed here for TagResource.
#:
#: Measured live 2026-09-22 on acfe2e-p0920, both halves independently:
#:   StepRuntimeConfigureRole, CreateAgentRuntime -> AccessDeniedException ... not
#:   authorized to perform: bedrock-agentcore:TagResource on resource:
#:   arn:aws:bedrock-agentcore:us-east-1:...:workload-identity-directory/default
#:   StepHarnessRole, CreateOauth2CredentialProvider -> ... TagResource on resource:
#:   arn:aws:bedrock-agentcore:us-east-1:...:token-vault/default
#: Both name the container with NO child segment, against roles that held the child glob.
#: The runtime one blocked every AgentCore runtime deploy outright.
#:
#: The flat shapes stay single-element: their ARN has no container segment to be checked
#: against, so adding one would be inventing a resource.
AGENTCORE_TYPE_ARN_TAIL: dict[str, tuple[str, ...]] = {
    "runtime": ("runtime/*",),
    "workload-identity": (
        "workload-identity-directory/default",
        "workload-identity-directory/default/workload-identity/*",
    ),
    "gateway": ("gateway/*",),
    "memory": ("memory/*",),
    "policy-engine": ("policy-engine/*",),
    "harness": ("harness/*",),
    "oauth2credentialprovider": (
        "token-vault/default",
        "token-vault/default/oauth2credentialprovider/*",
    ),
    "apikeycredentialprovider": (
        "token-vault/default",
        "token-vault/default/apikeycredentialprovider/*",
    ),
}

#: AgentCore resource type -> the service-specific getter ``assert_agentcore_resource_owned``
#: calls BEFORE ``ListTagsForResource``.
#:
#: This table exists because the ownership read is TWO calls and only the second one was
#: ever granted. ``assert_agentcore_resource_owned``
#: (services/resource_ownership.py:410-440) resolves the resource's ARN by calling the
#: type's own getter first, because ``ListTagsForResource`` needs an ARN and the caller
#: only has an id. So the getter is a hard prerequisite, not a nicety: without it the
#: ownership read fails before it can ever reach the tag call.
#:
#: Found live, not by reading. CloudTrail, 2026-09-22T15:35:52+01:00, during a failed
#: deploy's automatic cleanup:
#:
#:     StepStatusUpdateRole -> GetMemory(memoryId=mem_d47c3d4e52-XJDiI2HKWr)
#:     -> AccessDenied
#:
#: The deployed role held ``ListTagsForResource`` on ``memory/*`` and three getters
#: (GetGateway, GetAgentRuntime, GetHarness) out of the seven types ``status_update``
#: declares it reads. Four of its seven reachable types therefore could not complete an
#: ownership read at all, which is why auto-cleanup of a failed deploy died and left the
#: operator a ``delete_failed`` record.
#:
#: Why the earlier tests could not catch it: they asserted the ``ListTagsForResource``
#: grant per reachable type and nothing else, so they modelled only the second half of a
#: two-call sequence. A test that checks one half of a prerequisite chain is structurally
#: incapable of finding a missing prerequisite. ``test_ownership_read_grants_both_calls``
#: adds the missing axis by deriving BOTH actions from this table and the one next to it.
#:
#: Deliberately NOT ``bedrock-agentcore:Get*``. Four missing actions is exactly when a
#: wildcard is tempting, and ARCC least-privilege guidance (cnt_BBrFTwAEgWxA30) says to
#: work upward from zero to the minimum set that achieves the function rather than
#: downward from full access (cnt_AGx9pUNpmdOVZB). There is a second, test-specific
#: reason: a wildcard satisfies every per-type assertion whether or not the type is
#: actually modelled, so it would make the new invariant unfalsifiable -- it would pass
#: on a type nobody had thought about.
#:
#: The keys are the ``AGENTCORE_TYPE_ARN_TAIL`` vocabulary, not the backend's
#: ``_AGENTCORE_OWNERSHIP_READS`` keys, because this module's other tables already use
#: it. The two spellings are reconciled by a test rather than by a comment, so the
#: mapping cannot drift from the code that actually makes the call.
AGENTCORE_OWNERSHIP_GETTER: dict[str, str] = {
    "runtime": "bedrock-agentcore:GetAgentRuntime",
    "gateway": "bedrock-agentcore:GetGateway",
    "memory": "bedrock-agentcore:GetMemory",
    "policy-engine": "bedrock-agentcore:GetPolicyEngine",
    "harness": "bedrock-agentcore:GetHarness",
    "oauth2credentialprovider": "bedrock-agentcore:GetOauth2CredentialProvider",
    "apikeycredentialprovider": "bedrock-agentcore:GetApiKeyCredentialProvider",  # pragma: allowlist secret
}

#: step name -> the AgentCore resource types that step's handler tags while CREATING them.
#:
#: Passing ``tags=`` to a ``bedrock-agentcore`` create is authorized as a SEPARATE
#: ``bedrock-agentcore:TagResource`` on every resource the create tags -- including
#: resources the caller never names. ARCC ``cnt_ZaMOLOrjBjVPx6`` is explicit that tag-on-
#: create happens "in the same call as the resource's Create API"; the account is equally
#: explicit that the call is authorized twice. This repo has now paid for that assumption
#: five times (the sandbox function, CreateLogGroup's tags, the gateway target Lambda, the
#: runtime itself, and this table).
#:
#: ``workload-identity`` is the entry that proves the point: nothing in this codebase
#: creates a workload identity, yet ``CreateAgentRuntime`` and ``CreateGateway`` each mint
#: one for the resource and tag it too, so a ``runtime/*``-only grant is a live outage of
#: the runtime path. Observed, not inferred:
#:   AccessDeniedException ... not authorized to perform: bedrock-agentcore:TagResource on
#:   resource: arn:aws:bedrock-agentcore:us-east-1:...:workload-identity-directory/default/
#:   workload-identity/*
#:
#: Derived by walking each handler's transitive call graph rather than by reading step
#: names, and audited that way by
#: ``test_agentcore_tag_on_create_grants_match_the_call_graph``. Two entries the walker
#: cannot see are marked there as blind spots with the call site that proves them, because
#: it does not follow a call made on an imported MODULE object.
#:
#: A step absent from this table gets no ``bedrock-agentcore:TagResource`` at all. Nine of
#: the fifteen steps are absent and that is the intended shape: ``codegen`` in particular
#: runs model-authored code and must never hold a tagging primitive.
AGENTCORE_TAG_ON_CREATE_TYPES: dict[str, tuple[str, ...]] = {
    # runtime_deployer.create_agent_runtime, reached from both runtime-creating steps.
    "runtime_configure": ("runtime", "workload-identity"),
    "mcp_server": ("runtime", "workload-identity"),
    # gateway_deployer.deploy_gateway: create_gateway, plus the two outbound credential
    # providers it ensures for a gateway's own connectors.
    "gateway": ("gateway", "workload-identity", "oauth2credentialprovider", "apikeycredentialprovider"),
    # harness_deployer.create_harness, and ensure_gateway_outbound_provider's
    # create_oauth2_credential_provider (harness_step.py:146 -> harness_deployer.py:464).
    #
    # `runtime` and `workload-identity` are here because CreateHarness is built on
    # CreateAgentRuntime and tags that backing runtime asynchronously under THIS role,
    # after its own 200. Measured live: the harness reached CREATE_FAILED with the denial
    # present in no log group at all -- only in the delete_harness failureReason. See
    # AGENTCORE_CREATE_TO_TAGGED_TYPES["create_harness"] in infra/tests/
    # handler_call_graph.py for the verbatim 403 and why workload-identity is derived
    # rather than measured.
    #
    # `memory` is here for the same reason and was missed by the round that added the other
    # two: CreateHarness ALSO auto-provisions a DEFAULT memory (harness_deployer.py:276 says
    # so in prose, about the role that needs to READ it) and tags it asynchronously under
    # THIS role. The repo already knew the memory existed; nobody carried that across to the
    # tag-on-create table, so the whole harness path was dead. Measured live 2026-09-24 on
    # acfe2e-p0920, deployment d22088ec, the FIRST live harness deploy after create_harness
    # started passing `tags=`:
    #   User: .../acfe2e-p0920-step-harness is not authorized to perform:
    #   bedrock-agentcore:TagResource on resource:
    #   arn:aws:bedrock-agentcore:us-east-1:...:memory/p0bharn1790231300_302f262f-*
    #   (Service: GenesisMemoryControlPlane, Status Code: 403)
    # Note the memory's name: `<harnessName>_<hex>`, with NO `harness_` prefix, so memory/*
    # is the only tail that matches it. No condition change is needed -- the memory carries
    # the harness's own propagated tag set, which this statement's allowlist already admits
    # (the backing runtime's tagging succeeded on that same set before the memory's failed).
    "harness": ("harness", "oauth2credentialprovider", "runtime", "workload-identity", "memory"),
    "memory": ("memory",),
    "policy": ("policy-engine",),
}

#: step name -> the AgentCore resource types that step's handler READS TAGS FROM, to decide
#: whether a resource it found is this deployment's before adopting or deleting it.
#:
#: This is the read half of the tagging model, and it shipped missing. Every role granted
#: bedrock-agentcore:TagResource above held ZERO bedrock-agentcore:ListTagsForResource, so
#: the platform could stamp ownership and then never read it back. Fail-closed is the right
#: behaviour and it is what happens -- but it means the ownership, adoption and teardown
#: gates could not work AT ALL, and a refused teardown leaves the resource running.
#:
#: Measured live 2026-09-22 on acfe2e-p0920, the memory step's own retry:
#:   AccessDeniedException ... not authorized to perform:
#:   bedrock-agentcore:ListTagsForResource on resource:
#:   arn:aws:bedrock-agentcore:us-east-1:...:memory/mem_f46d1ded29-I80I66817h
#:   -> ResourceDeletionRefused: Deletion refused for memory mem_f46d1ded29-I80I66817h:
#:      live ownership could not be read (AccessDeniedException). The resource was left in
#:      place.
#: That memory was left ACTIVE and billing. The refusal is correct; the denial is not.
#:
#: DERIVED FROM THE CALL SITES by tests/ownership_read_graph.py, which walks each handler's
#: transitive calls and records the resource-type argument of every reachable
#: assert_agentcore_resource_owned. Transcribed by hand this table was WRONG, in the
#: direction that causes the outage: a `grep` scoped to step_handlers/ finds three steps,
#: because nine more call sites live in runtime_deployer, gateway_deployer and
#: harness_deployer, reached from the steps that import them. The walker finds six. Do not
#: edit this table by hand -- change the code and let the test re-derive it.
#:
#: Two subtleties the derivation settles that reading cannot:
#:
#: 1. codegen, iam and runtime_launch each import a deployer module and are each correctly
#:    absent. codegen reaches 85 functions and not one is an ownership read, which matters
#:    because codegen runs model-authored code and must never hold a tagging primitive.
#:    "Imports a deployer" is not "reaches the read".
#: 2. status_update and the deployment Lambda need BOTH provider types even though no call
#:    site names api_key_credential_provider literally. delete_owned_credential_provider
#:    (resource_ownership.py:456) loops over both namespaces, for back-compat with older
#:    manifests that recorded an API-key provider as OAuth. It only `continue`s when the
#:    error is a MISSING resource; AccessDenied re-raises -- and oauth2 is probed and
#:    DELETED first, so a role holding the read for only one type tears down half the
#:    providers and then fails. That is worse than failing closed.
#:
#: The read set is not the write set, in both directions, and both asymmetries are real:
#: status_update reads seven types and tags none (it is teardown), while runtime_configure
#: tags four types and reads only runtime. Granting the read to every role that got
#: TagResource would be the easy uniform fix and is what the tests below forbid.
#:
#: Least privilege per ARCC cnt_L4ZLZgjrCctfxl (Prevent Privilege Escalation) and
#: cnt_a9beEXtEalSwBG (Implement Authorization): the action is enumerated, never
#: bedrock-agentcore:* and never a resource wildcard, and it is scoped to the same resource
#: ARNs the role already touches. Per the latter, the tag read is defence in depth and NOT
#: the authorization itself -- the ownership assert stays exactly where it is; this grant
#: exists only so that check can RUN, rather than degrading from a decision into a denial.
#:
#: bedrock-agentcore:UntagResource is deliberately NOT granted anywhere. Nothing in this
#: codebase calls it -- `grep -rn untag_resource backend/src/app/` is empty -- and a tagging
#: primitive with no caller is exactly the kind of grant that gets used by the next thing
#: that wants it. Teardown DELETES resources, it does not un-tag them. Add it in the same
#: commit as its first caller or not at all.
AGENTCORE_OWNERSHIP_READ_TYPES: dict[str, tuple[str, ...]] = {
    # create-conflict / adoption: create_agent_runtime reads the owner of a runtime that
    # already exists before adopting it (runtime_deployer.py:865).
    "mcp_server": ("runtime",),
    "runtime_configure": ("runtime",),
    # _ensure_api_key/_oauth2_credential_provider (gateway_deployer.py:1145, :1282) plus the
    # abort path's cleanup_gateway_resources (:6174).
    "gateway": ("gateway", "oauth2credentialprovider", "apikeycredentialprovider"),
    # ensure_gateway_outbound_provider (:485) and create_harness's conflict branch (:606).
    "harness": ("harness", "oauth2credentialprovider"),
    "memory": ("memory",),
    "policy": ("policy-engine",),
    # Teardown. Reads every type it may delete and creates none of them.
    "status_update": (
        "gateway",
        "runtime",
        "memory",
        "policy-engine",
        "harness",
        "oauth2credentialprovider",
        "apikeycredentialprovider",
    ),
}


#: step name -> the services a step hands a role it minted to (iam:PassedToService), derived
#: from the call sites: gateway -> create_gateway(roleArn) [AgentCore] and create_function(Role=)
#: for its tool Lambdas [Lambda]; knowledge_base -> create_knowledge_base(roleArn) [Bedrock];
#: memory -> create_memory(memoryExecutionRoleArn) [AgentCore]. iam passes nothing (it hands the
#: ARN to the next step); mcp_server, evaluation and harness pass to AgentCore through their own
#: conditioned PassRole statements further down (runtime/eval/memory/MCP prefixes; AgentCoreHarness-*).
#: F-05: the shared CreateRole statement used to carry an UNCONDITIONED iam:PassRole for all seven.
STEP_PASS_ROLE_SERVICES: dict[str, tuple[str, ...]] = {
    "gateway": ("bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com"),
    "knowledge_base": ("bedrock.amazonaws.com",),
    "memory": ("bedrock-agentcore.amazonaws.com",),
}

#: Steps that mint or tear down AgentCore* roles and therefore carry the shared-role Deny.
ROLE_MINTING_STEPS = frozenset({"iam", "mcp_server", "gateway", "knowledge_base", "memory", "evaluation", "harness"})


def _create_step_role(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    step_name: str,
    *,
    tables: Tables,
    artifacts_bucket: s3.Bucket,
    role_boundary: iam.ManagedPolicy,
    shared_runtime_role: iam.Role,
    shared_mcp_runtime_role: iam.Role,
    gateway_auth_pool: cognito.IUserPool | None = None,
) -> iam.Role:
    """Create a dedicated IAM role for a step Lambda (1:1 relationship).

    Per-step least-privilege. Previously every step Lambda shared an
    identical kitchen-sink policy with iam:CreateRole + lambda:CreateFunction
    + secretsmanager:* on `*` — meaning RCE in any step Lambda became full
    account compromise. Verified live 2026-05-16; tasks/lessons.md Bug 36.
    """
    role = iam.Role(
        stack,
        f"Step{step_name.title().replace('_', '')}Role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )
    # ── Common: every step needs to update its DDB row + read SSM config ─
    tables.deployments.grant_read_write_data(role)
    tables.workflows.grant_read_data(role)
    # Phase 1 Gap 1A — every step's versioning hooks read the AgentVersions
    # table and status_update writes to both tables. Granting read to all
    # steps is acceptable: the data is owner-scoped via owner_sub anyway,
    # and these tables hold no secrets.
    tables.agent_versions.grant_read_write_data(role)
    tables.runtime_slots.grant_read_write_data(role)
    # Loom-study 2.2 — runtime_configure/harness steps READ approval policies
    # (tag-policy table) to inject LOOM_APPROVAL_POLICIES into the runtime.
    # Workstream A — the gateway step reads the SETTING#gateway_provider row from
    # the same generic settings store to resolve the platform-default gateway
    # provider. Read-only: without this the lookup silently degrades to
    # "agentcore" and an admin's chosen default would be ignored.
    if step_name in {"runtime_configure", "harness", "gateway"}:
        tables.tag_policy.grant_read_data(role)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/agentcore-workflow/{cfg.env}/*"],
        )
    )
    role.add_to_policy(iam.PolicyStatement(actions=["sts:GetCallerIdentity"], resources=["*"]))
    role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"]))
    # Phase 7 (opt-in) cross-account deploy: each step Lambda assumes the
    # target account's deployment role (services/step_clients). NAME-SCOPED
    # to the agreed role name — NOT a blanket AssumeRole. Feature is off by
    # default (no target → no assume-role call is ever made).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"],
        )
    )

    # ── Per-step grants ──────────────────────────────────────────────────
    # Every step that writes to S3 (codegen, gateway, knowledge_base,
    # mcp_server) gets bucket access. runtime_configure / runtime_launch
    # only need read because AgentCore's CreateAgentRuntime does a
    # pre-flight S3 check on the CALLING principal's identity — not just
    # the passed roleArn. Without s3:GetObject, the call fails with
    # `ValidationException: Access denied when trying to retrieve zip
    # file from S3` even though the shared runtime exec role can read it.
    # Verified live 2026-05-18: same role+bucket+key, direct boto3 from
    # an S3-permitted user succeeds; from the step Lambda fails. See
    # tasks/lessons.md Bug 66.
    s3_writers = {"codegen", "gateway", "knowledge_base", "mcp_server"}
    s3_readers = {"runtime_configure", "runtime_launch"}
    # The same access on the regional artifact buckets a non-home deploy stages into (F-41b).
    # Tagged uploads (runtime_deployer.upload_code_to_s3, gateway spec staging) need
    # PutObjectTagging; only the gateway also proves ownership before deleting a staged
    # spec (assert_s3_object_owned). knowledge_base does neither.
    s3_tag_writers = {"codegen", "gateway", "mcp_server"}
    s3_tag_readers = {"gateway"}
    if step_name in s3_writers:
        artifacts_bucket.grant_read_write(role)
        # ... except the dependency bundles the runtimes are built from (F-21, buckets.py):
        # codegen and mcp_server READ agentcore-deps/; only the CDK BucketDeployment writes it.
        deny_writes_to_agentcore_deps(role, artifacts_bucket)
        grant_regional_artifact_buckets(
            role,
            stack,
            "read_write",
            write_tags=step_name in s3_tag_writers,
            read_tags=step_name in s3_tag_readers,
        )
    elif step_name in s3_readers:
        artifacts_bucket.grant_read(role)
        grant_regional_artifact_buckets(role, stack, "read")

    # iam_step: creates and tags the runtime's execution role.
    # mcp_server / gateway / knowledge_base / memory also create paired
    # IAM roles for their own dynamically-created Lambdas / AgentCore
    # resources. (memory creates AgentCoreMemory-* role for the memory
    # resource — see tasks/lessons.md Bug 45.)
    # evaluation creates AgentCoreEval-* role for the AgentCore evaluation
    # engine — same drift-across-paths shape as Bug 45/71/77; see
    # tasks/lessons.md Bug 118 (Phase 1 Gap 1C).
    if step_name in ROLE_MINTING_STEPS:
        # Read / delete / tag verbs: unconditioned on the wide prefix, because teardown names
        # roles from manifests and legacy conventions and none of these verbs' request contexts
        # carries the keys the minting verbs below are conditioned on.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:GetRole",
                    "iam:DeleteRole",
                    "iam:DetachRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                    "iam:TagRole",
                ],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            )
        )
        # The MINTING verbs, in their own statement so they can carry iam:PermissionsBoundary
        # (F-06, role_boundary.py). All three accept that key per the Service Reference feed;
        # GetRole/DeleteRole/PassRole/List* do not, so a mixed statement would deny them when
        # enforcement is on. Until enforcement is switched on (context
        # `enforce_role_permissions_boundary`) this is the same grant as before, minus PassRole.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreateRole",
                    "iam:PutRolePolicy",
                    "iam:UpdateAssumeRolePolicy",
                ],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
                conditions=boundary_conditions(stack, role_boundary),
            )
        )
        # Retrofit path (F-06): every "role already exists -> adopt" branch in these steps puts
        # the platform boundary on the role it found, so enforcement can be switched on without
        # a flag day. Pinned to this boundary, never a Delete -- see grant_boundary_retrofit.
        grant_boundary_retrofit(
            role,
            stack,
            role_boundary,
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
        )
        # iam:PassRole by the service the step hands the role to (F-05). Steps absent from the
        # table pass nothing here -- see STEP_PASS_ROLE_SERVICES for where the others pass.
        if step_name in STEP_PASS_ROLE_SERVICES:
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
                    conditions={"StringEquals": {"iam:PassedToService": list(STEP_PASS_ROLE_SERVICES[step_name])}},
                )
            )
        # AttachRolePolicy, split out so it can carry the iam:PolicyARN condition.
        # See ATTACHABLE_MANAGED_POLICIES for why, and for why detach is not
        # conditioned. Under boundary enforcement the target role must also carry the
        # platform boundary (F-06).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:AttachRolePolicy"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
                conditions={
                    "ArnEquals": {"iam:PolicyARN": list(ATTACHABLE_MANAGED_POLICIES)},
                    **(boundary_conditions(stack, role_boundary) or {}),
                },
            )
        )
    if step_name in ROLE_MINTING_STEPS or step_name == "status_update":
        # F-05: role/AgentCore* matches the two shared runtime roles every tenant runs as.
        # Whatever the grants above (and status_update's teardown grant below) reach, those two
        # are never a step's to rewrite, retrust, retag or delete. See shared_role_guard.py.
        deny_mutating_the_shared_runtime_roles(
            role,
            stack,
            shared_runtime_role=shared_runtime_role,
            shared_mcp_runtime_role=shared_mcp_runtime_role,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/aws-service-role/*"],
            )
        )

    # runtime_configure / runtime_launch / mcp_server PASS the runtime's
    # IAM role to AgentCore via CreateAgentRuntime / CreateAgentRuntimeEndpoint.
    # iam:PassRole is required at the calling principal — see tasks/lessons.md
    # Bug 49. Resource includes the shared runtime role (Bug 60) and the
    # legacy per-deploy AgentCoreRuntime-* / AgentCoreMemory-* patterns
    # so cleanup of older deployments still works.
    if step_name in {"runtime_configure", "runtime_launch", "mcp_server", "evaluation"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{stack.account}:role/AgentCoreRuntime-{cfg.project}-{cfg.env}-shared",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreRuntime-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreEval-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreMemory-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreMCP-*",
                ],
                # Defence-in-depth: these roles may only be passed to AgentCore,
                # never to another service (matches the policy-step grant below).
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # AgentCore creates a runtime's CloudWatch group outside CloudFormation and
    # leaves it without retention.  The two runtime-creation steps create-or-adopt
    # only the DEFAULT group for the exact runtime id returned by AgentCore, then
    # apply the platform's 30-day retention.  The harness step does the same for the
    # runtime AgentCore creates to host a harness (its id is read from the READY
    # harness).  Do not grant logs:TagResource here: the runtime id is unknowable at
    # synth time, so that action would authorize relabelling every historical/foreign
    # group under this account-wide prefix.
    if step_name in {"runtime_configure", "mcp_server", "harness"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
                resources=[f"arn:aws:logs:*:{stack.account}:log-group:/aws/bedrock-agentcore/runtimes/*"],
            )
        )

    # A create's `tags` argument is authorized as bedrock-agentcore:TagResource on each
    # resource the create tags, NOT as part of the Create action itself. Which steps get
    # this, and which resource types each one may tag, is AGENTCORE_TAG_ON_CREATE_TYPES at
    # the top of this module -- read its comment before changing anything here; it carries
    # the live denial that produced the workload-identity entry and the reason the table is
    # derived from the call graph instead of from step names.
    #
    # ONE statement per role, resources unioned from the table. That is not cosmetic:
    # CloudFormation MERGES statements whose action sets are equal by combining their
    # resources, so emitting a second ["bedrock-agentcore:TagResource"] statement here with
    # a different resource list and different conditions would either silently fuse into
    # this one or, worse, land as a broader twin. Seen live on lambda:ListTags: a narrow
    # grant on function:AgentCore* was unioned into the ownership-read block's function:*
    # statement and contributed nothing, and no synth assertion could see it.
    if step_name in AGENTCORE_TAG_ON_CREATE_TYPES:
        # Every create on these paths sends owner_tags(region) unconditionally, so there is
        # no untagged fallback: an untagged resource would be unattributable to teardown, so
        # refusing to create one beats creating one quietly. That is what makes a missing
        # entry here an outage rather than a degradation -- observed live, not inferred:
        #   AccessDeniedException ... not authorized to perform:
        #   bedrock-agentcore:TagResource on resource: .../runtime/*
        # raised from CreateAgentRuntime in DeployMCPServer, with runtime_id null.
        #
        # Conditioned for the same reason as the lambda:TagResource grant below: none of
        # these ids can be known at synth time, so every resource must stay a wildcard,
        # and an unconditioned grant there would let this role relabel any runtime,
        # gateway, memory, harness or credential provider in the account -- including the
        # foreign ones this account demonstrably holds.
        # aws:RequestTag/${TagKey} and aws:TagKeys are both supported on
        # bedrock-agentcore:TagResource and CreateAgentRuntime (confirmed against the
        # AWS Service Reference feed for bedrock-agentcore, not the docs).
        #
        # BOTH ownership values are pinned, not just ManagedBy. Pinning ManagedBy and
        # the key list alone still authorizes TagResource against ANY existing resource,
        # because a request carrying ManagedBy=agentcore-flows plus an ARBITRARY
        # AgentCoreStack value satisfies it -- and AgentCoreStack is the value teardown
        # actually matches on. With both pinned, the only tag pair this role can write
        # is this deployment's own, so it cannot forge another deployment's ownership
        # nor strip one (a strip would have to send some other value).
        #
        # WHAT THIS DOES NOT PREVENT, stated rather than implied: the grant still
        # permits stamping OUR ownership onto a foreign resource of these types that this
        # platform did not create, which would make it a teardown candidate. The feed has
        # no create-only/called-from-create condition key for TagResource (its only
        # action condition keys are aws:RequestTag/${TagKey} and aws:TagKeys), so a
        # dependent tag-on-create is indistinguishable from a standalone retag in
        # policy. A `Null` condition on aws:ResourceTag/AgentCoreStack was considered
        # to block overwriting an already-tagged resource; it is NOT adopted because it
        # cannot protect an UNTAGGED foreign one (which is what this account
        # actually holds), and because whether aws:ResourceTag evaluates as absent
        # during a dependent tag-on-create is unproven -- getting that wrong fails
        # closed and is an outage. Reaching this at all requires code execution in the
        # step Lambda; the residual is bounded to "claim", not "read" or "invoke".
        #
        # The blast radius is per-step rather than uniform, which is the point of the
        # table: the memory step may claim a memory and nothing else, and the nine steps
        # absent from the table may claim nothing at all.
        #
        # Tripwire, deliberately: every create on these paths except Memory passes
        # owner_tags(region) with no `extra`, so exactly these two keys are sent. Memory
        # additionally binds the resource to the authenticated caller and the exact
        # deployment that created it; its handler sends OwnerSubHash + DeploymentId.
        # Those two keys are allowed only on StepMemoryRole below. Adding another key at
        # any create call site will be DENIED here rather than silently widening what
        # these roles may stamp.
        #
        # The AgentCoreStack value is DERIVED, not hardcoded, and its region component
        # is a policy variable rather than stack.region. owner_tags() builds the value
        # as stack_id() == f"{PROJECT_NAME}-{ENVIRONMENT}-{region}", where `region` is
        # the DEPLOY TARGET region: step_clients.session_for_event honours
        # event["target_region"] with no target_account_id by using this very role in
        # another region (an admin-gated, allowlisted feature). A literal stack.region
        # here would deny every such deploy at CreateAgentRuntime -- the same outage
        # this statement exists to fix. ${aws:RequestedRegion} binds the requested owner
        # tag to the region the call is actually made in, which is exact in every
        # region instead of merely exact at home, so the resource ARN leaves the region
        # open (the account stays pinned, and the tag value is what carries the safety).
        #
        # PROVEN LIVE, not assumed: a policy variable that fails to substitute makes
        # this condition unsatisfiable, which denies every create -- fail-closed into the
        # outage. Because this is the ONLY statement granting
        # bedrock-agentcore:TagResource to these roles, a successful live
        # CreateAgentRuntime is itself the proof that substitution works. Do not land a
        # change to this value without re-running that deploy.
        allowed_tag_keys = ["ManagedBy", "AgentCoreStack"]
        tag_value_shapes: dict[str, str] = {}
        if step_name == "memory":
            allowed_tag_keys.extend(["OwnerSubHash", "DeploymentId"])
            # IAM StringLike has no regex character classes. These patterns pin the
            # values' lengths (and the UUID separators), while the backend remains
            # responsible for producing lowercase hex / a canonical UUID. Keeping the
            # conditions on aws:RequestTag/<key> is load-bearing: aws:TagKeys contains
            # key NAMES only, so putting a value pattern there would enforce nothing.
            tag_value_shapes = {
                "aws:RequestTag/OwnerSubHash": "?" * 32,
                "aws:RequestTag/DeploymentId": ("????????-????-????-????-????????????"),
            }

        # ...with ONE exception to that tripwire, and it is a namespace rather than a key.
        #
        # P0-B's governance tags reach these creates now, and they broke every governed deploy
        # on acfe2e-p0920 the moment they did -- measured live, CreateAgentRuntime:
        #   AccessDeniedException ... not authorized to perform:
        #   bedrock-agentcore:TagResource on resource: .../runtime/*
        # with the action granted and the resource matching. The key list denied it. ARCC
        # cnt_SaTYaDCgBBJTcv predicts exactly this shape: a stack without permission to tag the
        # resources it manages starts failing "even though the customer has not made any
        # changes to their code/stack".
        #
        # The governance keys are admin-created at runtime (POST /api/settings/tags), so they
        # are unknowable here and an enumeration would mean a platform redeploy per tag policy.
        # Dropping the bound instead is the escalation this condition exists to prevent
        # (cnt_L4ZLZgjrCctfxl lists create/update tags among the powerful operations;
        # cnt_6gBImtb08AJqCB is why: tags carry ABAC decisions). GOVERNANCE_TAG_KEY_PREFIXES in
        # config.py is the middle: any key inside the namespace, nothing outside it, no redeploy
        # to add a policy. StringLike, because StringEquals cannot express a namespace -- the
        # exact keys above carry no wildcard character and so still match literally.
        #
        # The same value is handed to the backend as an env var below, which refuses an
        # out-of-namespace key at the API boundary. That is not a duplicate control: without it
        # the operator learns about this condition from a deployment that has already created
        # resources, which is precisely how it was found.
        tag_conditions: dict[str, dict[str, object]] = {
            "StringEquals": {
                "aws:RequestTag/ManagedBy": "agentcore-flows",
                "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"),
            },
            "ForAllValues:StringLike": {"aws:TagKeys": allowed_tag_keys + governance_tag_key_globs()},
        }
        if tag_value_shapes:
            tag_conditions["StringLike"] = tag_value_shapes

        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:TagResource"],
                # dict.fromkeys rather than set(): the gateway step is granted both
                # credential-provider types and they share the token-vault/default
                # CONTAINER ARN, so a plain comprehension emits it twice. A duplicate is
                # harmless to IAM but makes the policy diff unreadable and the
                # equality-based audit noisy. dict.fromkeys keeps insertion order so the
                # synthesized list is stable across runs -- a set is not, and an unstable
                # resource order shows up as a spurious CloudFormation diff on every synth.
                resources=list(
                    dict.fromkeys(
                        f"arn:aws:bedrock-agentcore:*:{stack.account}:{tail}"
                        for t in AGENTCORE_TAG_ON_CREATE_TYPES[step_name]
                        for tail in AGENTCORE_TYPE_ARN_TAIL[t]
                    )
                ),
                conditions=tag_conditions,
            )
        )

    # The read half of the same model. AGENTCORE_OWNERSHIP_READ_TYPES at the top of this
    # module carries the live denial that produced it and the reason its step set is
    # narrower than the tagging one -- read it before changing anything here.
    if step_name in AGENTCORE_OWNERSHIP_READ_TYPES:
        # ONE statement per role for the same reason as TagResource above: CloudFormation
        # merges statements with equal action sets by unioning their resources.
        #
        # UNCONDITIONED, and that is a considered choice rather than an omission. A
        # `StringEquals` on aws:ResourceTag/AgentCoreStack would be tighter and would
        # still produce the right OUTCOME -- a foreign or untagged resource would deny,
        # and the caller already converts a denial into ResourceDeletionRefused, which is
        # the correct refusal. It is rejected because it makes the refusal UNDIAGNOSABLE:
        # the operator gets "live ownership could not be read (AccessDeniedException)"
        # where they could have had "belongs to deployment <id>". This repo has already
        # paid for that once -- F-49, where a real remedy was reduced to a class name and
        # stranded in a log group. An error that names the owner is worth more here than a
        # condition that re-derives a refusal the code performs anyway.
        #
        # What the grant actually buys an attacker with code execution in these three step
        # Lambdas: the ability to read the TAGS of AgentCore resources of these types in
        # this account, including foreign ones. Not their contents, not their config, not
        # invoke. That is the cost, stated rather than implied.
        #
        # The resource list stays type-scoped rather than "*" -- the memory step may read a
        # memory's tags and nothing else -- and the ids are wildcards for the same reason
        # as above: AgentCore mints them at create time.
        #
        # ONE statement PER TYPE rather than one for the whole step, which is a change
        # from the single ListTagsForResource statement this replaced. The getter differs
        # per type, so a single merged statement would have to union the getters across
        # types and grant every one of them on every type's ARN -- i.e. GetMemory on a
        # gateway. Per-type statements keep each getter on exactly its own resource, which
        # is also what makes the invariant test able to assert action AND resource
        # together instead of just "the string appears somewhere in the document".
        for resource_type in AGENTCORE_OWNERSHIP_READ_TYPES[step_name]:
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        # Both halves of the ownership read, together. The getter resolves
                        # the ARN; ListTagsForResource then reads the owner tag off it.
                        # Granting either alone leaves the read unable to complete -- see
                        # AGENTCORE_OWNERSHIP_GETTER for the live denial that proved it.
                        AGENTCORE_OWNERSHIP_GETTER[resource_type],
                        "bedrock-agentcore:ListTagsForResource",
                    ],
                    resources=[
                        f"arn:aws:bedrock-agentcore:*:{stack.account}:{tail}"
                        for tail in AGENTCORE_TYPE_ARN_TAIL[resource_type]
                    ],
                )
            )

    # Harness turns a gateway's deployment-bound client-secret reference into an
    # AgentCore outbound credential provider, so it alone reads connector secrets.
    # runtime_configure only passes references to the Runtime and needs no secret
    # read. Provider/OTEL source namespaces are read by DeploymentLambda before SFN
    # starts and are never exposed to any step role.
    if step_name == "harness":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
                ],
            )
        )
    if step_name in {"runtime_configure", "harness"}:
        # VPC egress (Loom-study 0.1): a VPC-mode runtime makes AWS lazily
        # create the AWSServiceRoleForBedrockAgentCoreNetwork service-linked
        # role on first use. Without CreateServiceLinkedRole (scoped to that
        # SLR) the FIRST VPC-mode deploy in an account fails. Scoped by the
        # iam:AWSServiceName condition so it can only create THIS SLR.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    f"arn:aws:iam::{stack.account}:role/aws-service-role/"
                    "network.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreNetwork*"
                ],
                conditions={"StringEquals": {"iam:AWSServiceName": "network.bedrock-agentcore.amazonaws.com"}},
            )
        )

    # policy step calls update_gateway(roleArn=...) when binding the
    # PolicyEngine — re-passing the gateway's existing role triggers
    # iam:PassRole on the calling principal. See tasks/lessons.md Bug 76.
    if step_name == "policy":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCoreGateway-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # Phase B — the harness step PASSES the harness exec role to AgentCore
    # via CreateHarness (mirrors runtime_configure's CreateAgentRuntime
    # PassRole, Bug 49). Per-harness roles follow the AgentCoreHarness-*
    # convention (get_shared_or_new_harness_role); the optional shared
    # harness role, if added, also matches this prefix. Defence-in-depth:
    # the role may only ever be passed to AgentCore.
    if step_name == "harness":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCoreHarness-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # knowledge_base gets NO lambda: statement here. It holds exactly one lambda-client
    # call -- list_tags(Resource=transform_lambda) in _authorize_create_new_resources --
    # and that is already covered by the ownership-read block further down, which grants
    # lambda:ListTags on function:* (alongside s3:GetBucketTagging, rds and kms tag reads)
    # because transform_lambda comes straight out of kb_config["transformationLambdaArn"]
    # and may name ANY function in the account.
    #
    # A narrow `lambda:ListTags` on function:AgentCore* stood here for part of 2026-09-22
    # and was DEAD. CDK unions the resources of statements whose action sets are equal, so
    # it merged into the function:* statement and the deployed policy read
    # `["...function:*", "...function:AgentCore*"]` -- the narrower ARN adding nothing.
    # Synth-time action-set assertions cannot see that (the action is granted either way);
    # only reading the live policy's Resource showed it. Do not re-add it: if the KB step
    # ever needs a genuinely narrower Lambda read it has to be a DIFFERENT action set, or
    # the broad statement has to shrink.
    #
    # And the broad one cannot be tag-conditioned the way the gateway's AddPermission
    # statement is: this read IS the ownership decision. Gating it on
    # aws:ResourceTag/...=allow would make "not ours" arrive as an AccessDenied instead of
    # a clean rejection, i.e. condition the lookup on its own answer.

    # Only the GATEWAY step creates and manages user Lambdas (custom tools, the shared
    # singleton tool Lambda, KB transformer Lambdas).
    #
    # This condition read `step_name in {"gateway", "mcp_server", "codegen",
    # "knowledge_base"}` until 2026-09-22, which handed the whole eleven-action Lambda
    # mutation suite to three roles that never call a single one of them. Measured, not
    # assumed: a transitive AST call graph from each step handler's `handler` entrypoint
    # across all of backend/src, following module-level AND in-function imports and
    # matching boto3 method names (which OVER-reports, the safe direction for a removal),
    # reaches every one of the eleven from gateway_step -- and from mcp_server_step and
    # codegen_step it reaches NONE. The only lambda-client calls anywhere in the backend
    # live in gateway_deployer (gateway step), tool_tester and deployment_handler (the
    # deployment role, not a step role), knowledge_base_step (list_tags, above) and
    # status_update_step (the cleanup arm further down, which has its own statement).
    # mcp_server_step's sole gateway_deployer import is three secretsmanager helpers.
    #
    # Why this is a real reduction and not tidying: ARCC cnt_L4ZLZgjrCctfxl names "IAM
    # principal accesses role by updating Lambda function code" as an escalation path --
    # a principal holding lambda:UpdateFunctionCode on a function makes that function's
    # EXECUTION role run its code. So each of those three roles could rewrite any
    # function:AgentCore* in the account and inherit every tool Lambda's execution role,
    # for a capability none of them uses. cnt_pXauQr9E6bKwke says the same of
    # CreateFunction. cnt_oikES5IaGqdpqw goes further and is worth quoting on the codegen
    # step specifically: "Lambda is a form of a terminal/compiler/interpreter ... access
    # to lambda creation or edit should not be allowed" on a path fed by model output.
    # codegen_step is exactly that path. cnt_BBrFTwAEgWxA30 is the framing: build up from
    # zero, which is what the per-step split below does.
    #
    # lambda:InvokeFunction is absent from the list deliberately, and it was in it: the
    # same call graph reaches no lambda invoke from ANY step handler, gateway included.
    # Tool Lambdas are invoked by the AgentCore GATEWAY's own role
    # (AgentCoreGateway-<name>, which is why AddPermission is granted) and the deployment
    # role's self-invokes have their own exact-ARN statement -- never by a step role.
    #
    # The risk direction of getting this wrong is an outage, not a weakening: every one
    # of these calls is on a path with no untagged/unauthorized fallback, so a missing
    # grant fails the step outright. That is why the oracle is a call graph over the real
    # handler entrypoints rather than a reading of what each step "is for", and why
    # test_no_step_role_holds_a_lambda_action_its_handler_cannot_call enforces it from the
    # same direction on every future step.
    if step_name == "gateway":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:CreateFunction",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    "lambda:AddPermission",
                    "lambda:RemovePermission",
                    # GetPolicy is REQUIRED by _prune_orphaned_lambda_permissions:
                    # without it the prune's GetPolicy call is implicitly denied,
                    # the bare except swallows it, and no dangling gateway-role
                    # principal is ever removed — so a shared tool Lambda reused
                    # by a new gateway fails AddPermission with "invalid principal"
                    # forever (matrix-run Defect A; the root cause of the
                    # multi-gateway / multi-target deploy failures).
                    "lambda:GetPolicy",
                    # The ownership READ that _authorize_tool_function_replacement (F-7)
                    # does before it replaces any function's code. Deliberately a separate
                    # ListTags call rather than GetFunction's Tags field, so a missing
                    # grant surfaces as an AccessDenied the authorizer refuses on, instead
                    # of an empty tag map that would make every function look unowned --
                    # that would make the "belongs to another deployment" refusal
                    # unreachable while every deploy still went green.
                    "lambda:ListTags",
                ],
                # F-7d: exactly this stack's own function prefix, one pattern per supported
                # target region (the token carries the TARGET region, F-41), instead of
                # function:AgentCore*. lambda:CreateFunction / UpdateFunctionCode are
                # privilege-escalation primitives over whatever execution role the function
                # holds, so a prefix that also matched the foreign AgentCore* functions in this
                # account was authority the platform must not hold. Legacy unscoped names are
                # never created or updated by this role any more; the teardown roles keep a
                # read/delete grant on AgentCore* for legacy manifest rows, ownership-gated in code.
                resources=tool_function_arn_patterns(stack, cfg),
            )
        )

        # The ownership WRITE, and it is conditioned rather than folded into the
        # statement above, for a reason that took a second look to see.
        #
        # Why it is needed at all: create_function's Tags= argument is authorized as
        # lambda:TagResource on the function being created, NOT as part of
        # lambda:CreateFunction (third time this repo has paid for that assumption -- the
        # sandbox function, then CreateLogGroup's tags, now these). Unlike the log-group
        # path there is no retry-untagged fallback in gateway_deployer, so without this
        # the gateway step fails outright. That is the intended trade: an untagged tool
        # Lambda IS the F-7 defect, so refusing to create one beats creating one quietly.
        # It also authorizes the one-time backfill onto an adopted untagged function.
        #
        # Why it is conditioned, part 1 -- the key allowlist. The sibling statement
        # further down lets this role call lambda:AddPermission on ANY function tagged
        # AgentCoreGatewayTarget=allow. An unconditioned lambda:TagResource on
        # function:AgentCore* would let the platform WRITE that opt-in tag onto any
        # foreign function whose name merely starts with AgentCore, and thereby
        # self-grant the account-wide AddPermission capability the opt-in tag was
        # invented to gate. The tag-based grant is only as strong as the platform's
        # inability to write the tag it reads. ForAllValues:StringEquals on aws:TagKeys
        # denies the WHOLE request if any key is outside the list, so that specific
        # chain is closed by the key allowlist alone.
        #
        # Why it is conditioned, part 2 -- the ownership VALUE, and this half was
        # missing until 2026-09-22. Pinning only ManagedBy left AgentCoreStack
        # CALLER-CHOSEN, which is a different gap from the AddPermission chain and is not
        # closed by the key list: with an arbitrary value this role could stamp ANOTHER
        # deployment's stack id onto a function, and AgentCoreStack is exactly what
        # teardown and assert_this_deployment_may_mutate match on. That is a
        # cross-deployment integrity hole -- forge ownership to make a sibling
        # deployment's teardown delete a function, or overwrite ownership to steal one.
        # Found by the same review that found the bedrock-agentcore:TagResource outage
        # above; the two grants had the identical shape.
        #
        # The region component is the IAM policy variable ${aws:RequestedRegion}, not a
        # synth-time literal, so the value stays exact if this statement is ever widened
        # past the home region (see F-41). It is exact today either way, because the
        # resource ARN below pins stack.region and the variable therefore resolves to it.
        #
        # WHAT THIS DOES NOT PREVENT, stated because the test names must not imply it:
        # lambda:TagResource has NO create-only / called-from-create condition key (its
        # only keys are aws:RequestTag/${TagKey} and aws:TagKeys, confirmed against the
        # AWS Service Reference feed for lambda, not the docs -- both are also supported
        # on lambda:CreateFunction). So a dependent tag-on-create is indistinguishable in
        # policy from a standalone retag, and this role can still stamp OUR ownership
        # pair onto an untagged foreign function whose name starts with AgentCore. What
        # the conditions buy is bounded and worth stating exactly: no caller-chosen owner
        # value, no key outside the ownership pair, and the value bound to the region the
        # call is actually made in. A request tag bounds WHICH VALUE may be written, not
        # WHICH RESOURCE it lands on.
        #
        # Plain StringEquals, deliberately not StringEqualsIfExists: ARCC
        # cnt_SFJJhkOueCPRkd notes IfExists "evaluates the condition as true" when the
        # key is absent from the request, so IfExists here would authorize a request that
        # omits the ownership tags entirely -- the opposite of the intent.
        #
        # The tripwire this comment used to describe -- "adding a third tag key at either
        # call site will be DENIED here" -- fired as designed when P0-B governance tags
        # reached gateway_deployer's two create_function calls, and is now spent: the two
        # governance NAMESPACES are in the key list below and the match operator moved from
        # StringEquals to StringLike to admit them. The namespaces, not the keys, are the
        # unit of the grant because a tag policy is admin data: keys are created at runtime
        # through POST /api/settings/tags and cannot be enumerated at synth time (the same
        # reasoning as the bedrock-agentcore:TagResource statement above, spelled out in
        # config.governance_tag_key_globs). Widening the OPERATOR does not widen what is
        # authorized beyond those namespaces -- "platform:*" cannot match "ManagedBy" or
        # "AgentCoreGatewayTarget" -- so the AddPermission self-grant chain described in
        # part 1 stays closed: the opt-in tag key it would have to write is unnamespaced.
        #
        # The StringEquals block below is untouched on purpose. ManagedBy and AgentCoreStack
        # still have pinned VALUES, so the cross-deployment ownership forgery in part 2
        # remains closed independently of how many other keys ride along in the request.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:TagResource"],
                # F-7d: the tag write reaches exactly as far as the create grant above -- this
                # stack's own token prefix per region -- and no further, so this role cannot
                # stamp OUR ownership pair onto an untagged foreign AgentCore* function either
                # (the residual the old comment below had to state; it is closed by the prefix).
                resources=tool_function_arn_patterns(stack, cfg),
                conditions={
                    "StringEquals": {
                        "aws:RequestTag/ManagedBy": "agentcore-flows",
                        "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"),
                    },
                    # DeploymentId joined the pair for the per-deployment KB tool function
                    # (F-7d, peer 82): its exact value is what makes two deployments of one
                    # stack distinguishable on a function they would otherwise share. Its
                    # value is caller-chosen by construction (it IS the deployment id), and
                    # the resource prefix above bounds where it can land.
                    # ToolScope is the custom-tool equivalent (owner+gateway scope digest, peer 5a).
                    "ForAllValues:StringLike": {
                        "aws:TagKeys": [
                            "ManagedBy",
                            "AgentCoreStack",
                            "DeploymentId",
                            "ToolScope",
                            *governance_tag_key_globs(),
                        ]
                    },
                },
            )
        )

        # The tool functions' own log groups. gateway_deployer.govern_tool_function_log_group
        # creates or adopts each one with the platform's retention before the function's
        # gateway target exists; Lambda would otherwise create /aws/lambda/<function> on the
        # first invocation with no retention, and nothing ever bounded it (measured on the
        # matrix account: the shared tool groups outlived their functions, retention absent).
        # Teardown leaves them to expire, like the runtime's DEFAULT group, so this grants
        # neither logs:DeleteLogGroup nor logs:TagResource (the manifest is the ownership
        # authority). The reach is exactly the function grant's, under /aws/lambda/: one
        # region-exact prefix per supported region. AWSLambdaBasicExecutionRole already
        # allows CreateLogGroup on *; it is named here so the grant does not depend on that.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
                resources=tool_function_log_group_arn_patterns(stack, cfg),
            )
        )

    # A gateway `lambda` target may name a function the PLATFORM DID NOT CREATE (the
    # multi-target feature: the user pastes their own function ARN), and
    # _grant_gateway_invoke_on_lambda must write an invoke grant into that function's
    # resource policy. The AgentCore* statement above cannot cover it, so before this
    # statement existed every bring-your-own-Lambda target failed the whole deploy
    # with a bare "not authorized to perform: lambda:AddPermission" — measured live on
    # 2026-09-21 against function acfe2e-llstub-22add474.
    #
    # Widening the statement above to `function:*` is NOT the fix. lambda:AddPermission
    # is permission management: it would let ANY tenant's canvas make this platform
    # rewrite the resource policy of ANY function in the account, and naming a function
    # is not authority over it (F-7). ARCC cnt_BBrFTwAEgWxA30 (scope to the exact
    # resources needed) and cnt_dIF0SRA5SUuWSk (enumerate the actions, avoid wildcards).
    #
    # Instead the function's OWNER opts in with a tag, and IAM — not application code —
    # enforces it. The `function` resource type supports aws:ResourceTag/${TagKey} and
    # AddPermission supports lambda:Principal (both confirmed against the AWS Service
    # Reference feed for lambda, not the docs), so the grant is doubly bounded:
    #   * only functions tagged AgentCoreGatewayTarget=allow, and
    #   * the only principal that may be granted anything is an AgentCore gateway role.
    # The tag name is mirrored in gateway_deployer.GATEWAY_TARGET_OPT_IN_TAG, whose
    # AccessDenied handler turns a denial here into the actionable "tag your function"
    # error. Keep the two in step.
    #
    # The StringLike wildcard is forced: gateway roles are named per gateway
    # (AgentCoreGateway-<gateway_name>), so no StringEquals form exists. It is the one
    # wildcard here and it still pins account, path and prefix.
    if step_name == "gateway":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:AddPermission"],
                resources=[f"arn:aws:lambda:*:{stack.account}:function:*"],
                conditions={
                    "StringEquals": {"aws:ResourceTag/AgentCoreGatewayTarget": "allow"},
                    "StringLike": {"lambda:Principal": f"arn:aws:iam::{stack.account}:role/AgentCoreGateway-*"},
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                # Separate statement, tag condition only: GetPolicy has NO action
                # condition keys at all (Service Reference feed), so a lambda:Principal
                # condition would leave the key absent from its request context and
                # deny it outright — which is how the prune goes silently inert.
                # RemovePermission takes a StatementId rather than a principal, so the
                # same hazard applies to it.
                actions=["lambda:GetPolicy", "lambda:RemovePermission"],
                resources=[f"arn:aws:lambda:*:{stack.account}:function:*"],
                conditions={"StringEquals": {"aws:ResourceTag/AgentCoreGatewayTarget": "allow"}},
            )
        )

    # gateway AND mcp_server both create a Cognito user pool — gateway
    # for OAuth2 client_credentials between caller and gateway,
    # mcp_server for the gateway-to-MCP-server-runtime auth bridge.
    # See tasks/lessons.md Bug 77.
    if step_name in {"gateway", "mcp_server"}:
        # Neither action supports resource-level permissions (service reference
        # servicereference.us-east-1.amazonaws.com/v1/cognito-idp lists no resource
        # type for CreateUserPool or DescribeUserPoolDomain) and no condition key
        # applies, so "*" is the only shape. DescribeUserPoolDomain: the MCP step waits
        # for the auth domain it just created to be ACTIVE before the gateway target's
        # OAuth provider is pointed at it (live 2026-09-28: AgentCore could not resolve
        # a seconds-old domain); without this grant every describe was AccessDenied
        # and the deployment failed after nine minutes of retrying a denial.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:CreateUserPool", "cognito-idp:DescribeUserPoolDomain"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool",
                    "cognito-idp:CreateUserPoolClient",
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:CreateResourceServer",
                    "cognito-idp:CreateUserPoolDomain",
                    "cognito-idp:DeleteUserPoolClient",
                    "cognito-idp:DeleteUserPoolDomain",
                    # When the platform supplies the shared gateway-auth pool, a
                    # gateway's teardown deletes only ITS OWN app client and
                    # resource server inside that pool (never the pool or its warm
                    # domain) — so DeleteResourceServer is the counterpart to the
                    # CreateResourceServer above. Without it the resource server
                    # leaks on every delete and the per-gateway scope it defines
                    # accumulates in the shared pool forever.
                    "cognito-idp:DeleteResourceServer",
                    # REQUIRED by CreateUserPool's UserPoolTags argument, which
                    # create_gateway_cognito_auth passes so teardown can tell this
                    # pool from another deployment's (the pool NAME comes from the
                    # user's gateway name and carries no deployment identity).
                    # Cognito authorizes the tagging half of that one call as a
                    # separate TagResource check against the not-yet-created pool,
                    # so omitting it fails the whole CreateUserPool with
                    # AccessDeniedException — the tags are not silently skipped.
                    "cognito-idp:TagResource",
                ],
                resources=[f"arn:aws:cognito-idp:*:{stack.account}:userpool/*"],
            )
        )
    if step_name == "gateway":
        # The gateway-name claim (services/gateway_name_claim): acquire, promote,
        # abandon and release are each one conditional UpdateItem. No read, no delete --
        # erasing a claim is teardown's job, below and in the deployment Lambda.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[tables.gateway_name_claims.table_arn],
            )
        )
    if step_name == "status_update":
        # The failure-path teardown's name hold (F-66f): acquire, abandon and release
        # are conditional UpdateItems; the erase of a confirmed-deleted gateway's claim
        # is a conditional DeleteItem. No read, no put.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                resources=[tables.gateway_name_claims.table_arn],
            )
        )
        # ...and finds a gateway recorded only on its claim: GetItems of its recovery
        # pointer and the claims it lists, projected, never the fence token.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[tables.gateway_name_claims.table_arn],
                conditions={
                    "ForAllValues:StringEquals": {"dynamodb:Attributes": list(RECOVERY_READ_ATTRIBUTES)},
                    "StringEquals": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
                },
            )
        )
    if step_name in ("gateway", "policy", "status_update"):
        # F-66e: every write to an existing gateway (the adoption repoint, the policy
        # attach, the failure cleanup's delete) holds the gateway write lock
        # (services/gateway_mutation_lock). Lock rows only, so this cannot put or erase
        # a name claim.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:DeleteItem"],
                resources=[tables.gateway_name_claims.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["gwlock#*"]}},
            )
        )
    if step_name == "gateway":
        # Two reads only the gateway step makes (mcp_server reaches neither; see
        # test_teardown_reads_are_granted_where_they_are_called). Both fail
        # quietly without the grant:
        # - DescribeResourceServer is the shared-pool reuse proof: when
        #   CreateResourceServer says AlreadyExists, _resource_server_has_scope
        #   reads the existing one. A denial there answers "no scope", so every
        #   redeploy of a gateway name re-raises the AlreadyExists and nothing
        #   in the error mentions IAM.
        # - ListUserPoolClients is the co-residency check a failed deploy's own
        #   rollback runs before deleting the resource server it created. It
        #   fails closed, so a denial leaks the resource server.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DescribeResourceServer",
                    "cognito-idp:ListUserPoolClients",
                ],
                resources=[f"arn:aws:cognito-idp:*:{stack.account}:userpool/*"],
            )
        )

    # Re-reading an app client's SECRET is a separate capability from creating and
    # managing a pool, so it is granted separately to exactly the steps that do it.
    #
    # DescribeUserPoolClient is a DIFFERENT IAM action from DescribeUserPool above, and
    # for a long time only the pool one was granted. Both `gateway` and `harness` call
    # gateway_deployer.resolve_client_secret, which re-reads the secret with this action
    # at the moment of use -- the secret is deliberately never carried in client_info,
    # because that dict reaches the Step Functions execution history, the DynamoDB item
    # and GET /api/deploy/{id}. Without the action that read is denied, so
    # get_cognito_token cannot mint a token.
    #
    #   gateway  -> handler -> the deploy-time tools/list probe. Measured live,
    #     deployment ad93d2d4 on acfe2e-p0920: the gateway deployed successfully with 9
    #     tools synced onto a READY target, and every probe still logged "tools/list
    #     probe failed ...: AccessDeniedException", so the result carried
    #     tool_plane_verified=False. A GREEN deploy whose tool plane was never verified.
    #     This was masked for a long time by a cold Cognito hosted domain, which produced
    #     the same never-answered-tools/list symptom for an unrelated reason; once the
    #     shared warm pool removed that cause, the missing grant was the only one left.
    #   harness  -> handler -> harness_deployer.ensure_gateway_outbound_provider, the
    #     harness->gateway OAuth2 bridge. Found by the call-graph test below, not by a
    #     live run: the harness role never had ANY cognito grant, so this path could not
    #     have worked either.
    #
    # NOT granted to mcp_server, which also creates a pool and client: it takes the
    # secret straight off its own create_user_pool_client response
    # (mcp_server_step.py:256) and never re-reads it. It previously inherited this action
    # from the shared statement above, which let it read the app-client secret of any
    # pool in the account -- including the shared gateway-auth pool that authenticates
    # every deployed agent. Removed per ARCC cnt_AGx9pUNpmdOVZB (scope to the identity's
    # business function) and cnt_1ZPqVzeASDHlO7 / cnt_dwzZ05hLnqhYXQ (tight resource
    # scoping).
    #
    # SCOPING. The first version of this statement was a bare userpool/* with no
    # condition, which let either role read the app-client secret of every Cognito pool
    # in the account -- including pools belonging to other products. It is now two
    # narrow statements from a helper shared with the runtime role, which had the
    # MIRROR-IMAGE bug (an owner-tag condition the shared pool cannot satisfy). One
    # function, three roles, one synth test over every statement in the template: see
    # cognito_client_secret_grant.py.
    #
    # WHICH steps is pinned by
    # infra/tests/test_every_step_that_reads_the_client_secret_is_allowed_to.py, which
    # derives this step set from a call graph over backend/src/app rather than
    # restating it -- a hand-written list would have been written from the same wrong
    # belief that caused the bug.
    if step_name in {"gateway", "harness"}:
        grant_client_secret_read(role, stack, cfg, gateway_auth_pool=gateway_auth_pool)

    if step_name in {"gateway", "mcp_server"}:
        # gateway also stores the OAuth2 client_secret in Secrets Manager.
        # Phase A SaaS connectors: the gateway step also mints/reads/deletes
        # connector credential secrets under the agentcore-connector/ prefix
        # (raw API keys + OAuth2 client_secrets that back the credential
        # providers). DescribeSecret is used by the cleanup path to confirm
        # existence before delete. Secrets live ONLY here — never in canvas
        # JSON, DDB, or logs.
        # TagResource is NOT in this statement. It is granted separately below, with
        # conditions, and the split is load-bearing rather than tidiness: the conditions
        # that bound tagging are written on aws:RequestTag/aws:TagKeys, which are ABSENT
        # on GetSecretValue and DescribeSecret. A StringEquals on an absent request tag
        # does not match, so conditioning this combined statement would deny every secret
        # READ these steps make -- fail-closed into an outage, the exact trap the
        # bedrock-agentcore comment above warns about.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:AgentCore*",
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-*",
                    # Phase A SaaS connector credential secrets.
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
                    # CreateOauth2CredentialProvider writes its client_secret
                    # under the bedrock-agentcore-identity!default/oauth2/<n>
                    # Secrets Manager namespace, not the platform's prefix.
                    # See tasks/lessons.md Bug 83.
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )
        # secretsmanager:TagResource, bounded the same way the bedrock-agentcore, lambda
        # and iam tag grants above are bounded. It was unconditioned, and that was the one
        # remaining hole of that class -- worth spelling out because the consequence here is
        # the most severe of the set.
        #
        # ``agentcore-connector/`` names the PRODUCT, not a deployment, so every
        # deployment in the account mints its connector credentials under one shared
        # prefix. Teardown finds them by TAG (discover_deployment_bound_secrets filters on
        # name ``agentcore-connector/`` plus matching AgentCoreStack and DeploymentId
        # values) precisely because a prefix sweep would be unsafe. With TagResource
        # unconditioned, a gateway step could stamp its OWN AgentCoreStack and DeploymentId
        # onto another live deployment's connector secret, and that deployment's next
        # teardown would delete it -- which is verbatim the worst case
        # ``_put_connector_secret``'s own comment says the owner tag exists to prevent:
        # "one customer teardown destroying another live deployment's raw customer API
        # keys". These are raw customer API keys, so the loss is not recoverable by
        # redeploying.
        #
        # BOTH ownership values are pinned, for the reason given at the bedrock-agentcore
        # grant: pinning ManagedBy alone still admits an arbitrary AgentCoreStack, and
        # AgentCoreStack is the value teardown matches on. ${aws:RequestedRegion} rather
        # than stack.region, because session_for_event honours event["target_region"] using
        # this same role in another region and a literal would deny every such deploy.
        #
        # The key allowlist is exactly what _put_connector_secret sends:
        # governed_tag_list(region, resource_tags, extra={"Purpose": ...}) plus
        # secret_binding_tags(owner_sub, deployment_id) -> OwnerSubHash + DeploymentId.
        # Adding a key at that call site will be DENIED here rather than silently widening
        # what these roles may stamp on a credential secret.
        #
        # bedrock-agentcore-* is deliberately NOT in this statement's resources, and it
        # keeps an UNCONDITIONED grant of its own below rather than losing the action. The
        # distinction matters: splitting TagResource out of the combined statement without
        # re-granting that prefix would have silently narrowed a grant the finding did not
        # justify, and gateway_deployer calls both create_api_key_credential_provider and
        # create_oauth2_credential_provider on this very role -- so the narrowing would
        # have surfaced as a gateway deploy failing mid-way, not as a tightening.
        #
        # ``agentcore-*`` is NOT in this statement's resources either, and leaving it here
        # (it is in the combined statement above, from which this one was split) defeated
        # the whole point of the split. That glob SPANS five prefixes with five different
        # writers: agentcore-otel/, agentcore-provider/, agentcore-git/, agentcore-trigger/
        # and agentcore-registry/. Each of those is bounded on its own role to exactly the
        # keys its writer sends -- and none of those key sets is this one. So a conditioned
        # grant naming ``agentcore-*`` here hands the gateway and mcp_server steps standing
        # authority to stamp DeploymentId, OwnerSubHash and the governance namespaces onto
        # another writer's git token or provider credential, which is exactly the
        # cross-prefix tag write the per-prefix allowlists exist to refuse. The narrow
        # allowlist on WorkflowLambdaRole is worthless if a second role can write the same
        # tag on the same secret. Tags carry ABAC decisions, so this is a tag-integrity
        # boundary and not a cost-reporting nicety.
        #
        # Nothing needs it. The only secret these steps CREATE outside
        # ``agentcore-connector/`` is ``agentcore-registry/litellm/<hex>`` (the LiteLLM
        # virtual key, _put_registry_secret), and that writer passes no ``Tags=`` at all --
        # so it is authorized entirely by the unconditioned CreateSecret above and needs no
        # tag action. If it ever starts sending tags it fails closed here, loudly, which is
        # the same deliberate choice made for agentcore-registry/* on the deployment role.
        # ``secret:AgentCore*`` is gone from here too, for the reasons spelled out at the
        # matching statement in lambdas.py: no writer in the AST census, no such secret name
        # constructed anywhere in the repo, and zero instances in a live account that does
        # hold real secrets under the neighbouring prefixes. The two calls must not disagree.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:TagResource"],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
                ],
                conditions={
                    "StringEquals": {
                        "aws:RequestTag/ManagedBy": "agentcore-flows",
                        "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"),
                        # F-01 (b): value closed to what the backend sends; see the matching
                        # statement in lambdas.py for why this is safe to require.
                        "aws:RequestTag/IdentityMode": ["shared", "per_agent"],
                    },
                    "ForAllValues:StringLike": {
                        "aws:TagKeys": [
                            "ManagedBy",
                            "AgentCoreStack",
                            "Purpose",
                            "IdentityMode",
                            "OwnerSubHash",
                            "DeploymentId",
                        ]
                        + governance_tag_key_globs(),
                    },
                },
            )
        )
        # The identity namespace, unconditioned. ``CreateOauth2CredentialProvider`` and
        # ``CreateApiKeyCredentialProvider`` (gateway_deployer) have the SERVICE write the
        # backing secret under ``bedrock-agentcore-identity!default/...`` through a forward
        # access session. This platform sends no tags of its own there, so there is nothing
        # for an aws:RequestTag condition to match on, and whether a FAS tag write during
        # provider creation is evaluated against this role's conditions at all is unproven.
        # Getting that wrong fails closed on gateway creation, and it is not where the
        # escalation lives -- teardown's tag-based discovery only scans
        # ``agentcore-connector/``. Same call as the deployment Lambda role in lambdas.py;
        # the two must not disagree.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:TagResource"],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )
        # No ListSecrets here. These steps never enumerate: the only ListSecrets
        # caller is the teardown's tag discovery (discover_deployment_bound_secrets),
        # which runs in status_update and the deployment Lambda. A `*` grant on a role
        # that makes no such call is standing access with no use.

    # Bug 150 — the harness step registers an OAuth2 credential provider for a
    # connected gateway; CreateOauth2CredentialProvider writes its client_secret
    # under the bedrock-agentcore-identity! Secrets Manager namespace, so the
    # harness step role needs to write/read/delete there (mirrors the gateway
    # step's secret perms, minus the Cognito + connector-secret scope it doesn't use).
    if step_name == "harness":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )

    # codegen reads bedrock:Converse to render system prompts that
    # describe a tool's purpose (used by the customer-support template).
    if step_name == "codegen":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                # Resource "*" required: cross-region inference profiles route
                # to foundation-model ARNs in OTHER regions at runtime, and the
                # model is user-selectable per flow.
                resources=["*"],
            )
        )

    # knowledge_base creates KBs / data sources / ingestion jobs.
    if step_name == "knowledge_base":
        # KB / data-source lifecycle verbs support resource-level scoping on
        # the knowledge-base ARN. KB ids are SERVICE-GENERATED (the ARN carries
        # the id, not the user-supplied name), so the tightest pre-creation
        # pattern is knowledge-base/* in this account+region.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:CreateKnowledgeBase",
                    "bedrock:GetKnowledgeBase",
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:CreateDataSource",
                    "bedrock:GetDataSource",
                    # ListDataSources powers the idempotent create_data_source
                    # conflict recovery (matrix-run finding, P-KB-008).
                    "bedrock:ListDataSources",
                    "bedrock:DeleteDataSource",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                resources=[f"arn:aws:bedrock:*:{stack.account}:knowledge-base/*"],
            )
        )
        # Account-level list verbs — no resource-level scoping supported.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListKnowledgeBases", "bedrock:ListFoundationModels"],
                resources=["*"],
            )
        )
        # Customer-supplied KB resources are authorized from their LIVE tags
        # before the step creates or rewrites the Bedrock service role.  These
        # are read-only discovery grants; the role receives no new mutation
        # capability over the customer resources themselves.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListTagsForResource"],
                resources=[
                    f"arn:aws:bedrock:*:{stack.account}:knowledge-base/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetBucketTagging"],
                resources=["arn:aws:s3:::*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["rds:ListTagsForResource"],
                resources=[
                    f"arn:aws:rds:*:{stack.account}:cluster:*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:ListTags"],
                resources=[
                    f"arn:aws:lambda:*:{stack.account}:function:*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:ListResourceTags"],
                resources=[
                    f"arn:aws:kms:*:{stack.account}:key/*",
                ],
            )
        )
        # Credential source values have already been copied at the API boundary.
        # The KB step can only describe the deployment-bound target copies to
        # re-prove exact stack/deployment/caller ownership before granting the
        # Bedrock KB role GetSecretValue.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
                ],
            )
        )
        # Customer-supplied S3 Vectors buckets are read/validated only: the
        # owner must pre-create a compatible index. The step creates a bucket
        # and index only for its deterministic agentcore-kbvec-* lifecycle
        # path. Read permissions therefore still cover user-supplied bucket
        # ARNs, while application-level authorization checks the live opt-in
        # tags before any IAM role is created or rewritten.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:ListIndexes",
                    "s3vectors:CreateIndex",
                    "s3vectors:GetIndex",
                    "s3vectors:CreateVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListTagsForResource",
                ],
                resources=[f"arn:aws:s3vectors:*:{stack.account}:bucket/*"],
            )
        )
        # ListVectorBuckets is account-level (no resource ARN form).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:ListVectorBuckets"],
                resources=["*"],
            )
        )
        # OpenSearch Serverless: KB step auto-provisions a collection +
        # security/access policies + vector index when the caller supplies no
        # opensearchCollectionArn (Bedrock requires a pre-existing collection).
        # Collection verbs scope to collection ARNs (ids are service-generated,
        # so collection/* is the tightest pre-creation pattern).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "aoss:CreateCollection",
                    "aoss:DeleteCollection",
                    "aoss:CreateIndex",
                    "aoss:DescribeIndex",
                    "aoss:DeleteIndex",
                    "aoss:APIAccessAll",
                    "aoss:ListTagsForResource",
                ],
                resources=[f"arn:aws:aoss:*:{stack.account}:collection/*"],
            )
        )
        # aoss security/data-access policies AND the Batch/List read APIs do
        # NOT support resource-level permissions (account-level APIs) —
        # Resource must stay "*"; least privilege is enforced by the exact
        # action list. (BatchGetCollection on collection/* fails AccessDenied
        # — live-verified by the matrix run.)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "aoss:BatchGetCollection",
                    "aoss:ListCollections",
                    "aoss:CreateSecurityPolicy",
                    "aoss:GetSecurityPolicy",
                    "aoss:DeleteSecurityPolicy",
                    "aoss:CreateAccessPolicy",
                    "aoss:GetAccessPolicy",
                    "aoss:DeleteAccessPolicy",
                ],
                resources=["*"],
            )
        )
        # Tag-on-create for the four resources this step creates with a `tags` argument:
        # the Bedrock knowledge base, the OpenSearch Serverless collection, and the
        # S3 Vectors bucket + index. Each service authorizes the create's `tags` as its
        # OWN <service>:TagResource action, separately from the create verb -- the same
        # shape that took every governed deploy on acfe2e-p0920 down at CreateAgentRuntime
        # with the create action granted and the resource matching (see the
        # bedrock-agentcore:TagResource statement above). All three actions and all three
        # creates were confirmed to accept aws:TagKeys against the AWS Service Reference
        # feed rather than the documentation -- with one exception worth recording:
        # aoss:CreateIndex accepts NO tag condition keys at all, which matches the handler
        # creating the OSS index untagged. Only the collection carries tags there, so
        # nothing is granted for an index tag that cannot be sent.
        #
        # ARCC cnt_SaTYaDCgBBJTcv is the reason these are granted at all: a stack without
        # permission to tag the resources it manages starts failing with no code change.
        # ARCC cnt_L4ZLZgjrCctfxl and cnt_6gBImtb08AJqCB are the reason they are bounded:
        # tags carry ABAC decisions, so an unconditioned grant here would let this role
        # relabel any knowledge base, collection or vector bucket in the account --
        # including the eight foreign S3 Vectors buckets this account demonstrably holds.
        # The key list is the four this handler sends plus the governance NAMESPACES, for
        # the reason spelled out on the AgentCore statement: admin-created keys cannot be
        # enumerated at synth time.
        kb_tag_conditions: dict[str, dict[str, object]] = {
            "ForAllValues:StringLike": {
                "aws:TagKeys": [
                    "ManagedBy",
                    "AgentCoreStack",
                    "DeploymentId",
                    "OwnerSubHash",
                    *governance_tag_key_globs(),
                ]
            }
        }
        for tag_action, tag_resources in (
            ("bedrock:TagResource", [f"arn:aws:bedrock:*:{stack.account}:knowledge-base/*"]),
            ("aoss:TagResource", [f"arn:aws:aoss:*:{stack.account}:collection/*"]),
            ("s3vectors:TagResource", [f"arn:aws:s3vectors:*:{stack.account}:bucket/*"]),
        ):
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=[tag_action],
                    resources=tag_resources,
                    conditions=dict(kb_tag_conditions),
                )
            )

    # guardrails creates Bedrock Guardrails. Guardrail ids are service-
    # generated (ARN carries the id, not the name), so guardrail/* in this
    # account+region is the tightest pre-creation pattern.
    if step_name == "guardrails":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:CreateGuardrail",
                    "bedrock:GetGuardrail",
                    "bedrock:UpdateGuardrail",
                    "bedrock:DeleteGuardrail",
                    "bedrock:CreateGuardrailVersion",
                ],
                resources=[f"arn:aws:bedrock:*:{stack.account}:guardrail/*"],
            )
        )
        # ListGuardrails (account-level listing) — no resource-level scoping.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListGuardrails"],
                resources=["*"],
            )
        )

    # AgentCore control-plane verbs are split across many steps.
    agentcore_steps = {
        "mcp_server": [
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            # Bug 171: the step pre-warms the MCP runtime (sends an MCP
            # initialize) so the Gateway's 30s tool-discovery probe hits a
            # warm container instead of timing out on cold start. Needs the
            # data-plane invoke verb on its own runtime.
            "bedrock-agentcore:InvokeAgentRuntime",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
        ],
        "gateway": [
            "bedrock-agentcore:CreateGateway",
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:UpdateGateway",
            "bedrock-agentcore:ListGateways",
            "bedrock-agentcore:CreateGatewayTarget",
            "bedrock-agentcore:DeleteGatewayTarget",
            "bedrock-agentcore:ListGatewayTargets",
            # A role that can CreateGateway must be able to delete the gateway it
            # just created, or every failure path leaks one. Two code paths need
            # it and BOTH were silently dead without it (measured live, deploy
            # 3ef480e2, run df698a37):
            #   * Bug 134's empty-tool-plane retry, which deletes the gateway and
            #     recreates it from scratch -- described in gateway_deployer as
            #     "the ONLY deterministic cure". DeleteGateway 403'd, the except
            #     logged it "(non-fatal)", and deploy_gateway then re-adopted the
            #     SAME gateway id and re-synced the same 9 tools. Three identical
            #     "recreations", 368s of billed Lambda, same result, and the
            #     failure still blamed an "AgentCore provisioning flake".
            #   * deploy_gateway's own abort cleanup, which could not remove the
            #     gateway it had created.
            # Constrained the same way as every other verb in this list: by the
            # action itself, against resources=["*"] (see the statement below --
            # gateway ARNs are unknowable at synth time and AgentCore ignores
            # service wildcards at authorization time, Bug 47). Per ARCC
            # cnt_dwzZ05hLnqhYXQ / cnt_AGx9pUNpmdOVZB this is the narrowest form
            # available here, and it grants no reach the step does not already
            # have: it can already create these gateways and delete their targets.
            "bedrock-agentcore:DeleteGateway",
            # Bug 134: the gateway step now reads each target's MCP tool
            # manifest (GetGatewayTarget) and triggers a target sync
            # (SynchronizeGatewayTargets) so the policy step gets the real,
            # synced tool action names for schema-valid Cedar.
            "bedrock-agentcore:GetGatewayTarget",
            "bedrock-agentcore:SynchronizeGatewayTargets",
            # Bug 171: when an MCP target lands FAILED (cold-start probe), the
            # gateway step RETRIES it via UpdateGatewayTarget. Without this the
            # retry path itself 403s and the target can never recover.
            "bedrock-agentcore:UpdateGatewayTarget",
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:DeleteOauth2CredentialProvider",
            "bedrock-agentcore:ListOauth2CredentialProviders",
            # Phase A SaaS connectors: the gateway step now mints API-key
            # credential providers (Jira/Asana/GitHub/Slack/Salesforce/
            # generic OpenAPI) in addition to OAuth2 ones, and the cleanup
            # path deletes them. Same provisioning shape as the OAuth2 verbs
            # above (token-vault + workload-identity creation still apply).
            "bedrock-agentcore:CreateApiKeyCredentialProvider",
            "bedrock-agentcore:GetApiKeyCredentialProvider",
            "bedrock-agentcore:DeleteApiKeyCredentialProvider",
            "bedrock-agentcore:ListApiKeyCredentialProviders",
            # Update*: a provider reused by name must be REPOINTED at the current
            # secret. Without these the reuse path 403s and, worse, would silently
            # keep sending the key the provider was first created with — a
            # rotation that never takes effect. See
            # gateway_deployer._ensure_api_key_credential_provider.
            "bedrock-agentcore:UpdateApiKeyCredentialProvider",
            "bedrock-agentcore:UpdateOauth2CredentialProvider",
            # CreateOauth2CredentialProvider transparently provisions a
            # token vault under the account's identity directory if one
            # doesn't exist. Without these, mcp-server-gateway-target
            # deploys fail with "not authorized to perform:
            # bedrock-agentcore:CreateTokenVault on resource:
            # token-vault/default". See tasks/lessons.md Bug 79.
            "bedrock-agentcore:CreateTokenVault",
            "bedrock-agentcore:GetTokenVault",
            "bedrock-agentcore:ListTokenVaults",
            # CreateGateway transparently creates a workload-identity
            # record under the gateway's identity directory. Without this
            # action, the gateway lands in FAILED status with
            # "Failed to create gateway dependencies: ... not authorized
            # to perform: bedrock-agentcore:CreateWorkloadIdentity ..."
            # Verified live 2026-05-18 — see tasks/lessons.md Bug 65.
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
            # Bug 169 (caught live 2026-06-25): an MCP-server-as-gateway-target
            # with an OAUTH credential provider makes the gateway service mint
            # a workload access token to call the MCP runtime. Setting up that
            # target validates the caller can GetWorkloadAccessToken — without
            # it the TARGET lands in FAILED ("not authorized to perform
            # bedrock-agentcore:GetWorkloadAccessToken on ... workload-identity/
            # <gw>"), the gateway serves 0 tools, and the agent 500s at init.
            # (Mis-attributed to a wiring/endpoint bug; it is purely IAM.)
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            # Bug 169 (cont.): wiring the MCP target's OAUTH credential
            # provider also reads the oauth2 token (and, for api-key targets,
            # the api key) from the token vault during setup/validation. Both
            # surface as the same "OAuth setup ... not authorized to perform
            # bedrock-agentcore:GetResourceOauth2Token on .../token-vault/
            # default/oauth2credentialprovider/<name>" 403 → target FAILED.
            "bedrock-agentcore:GetResourceOauth2Token",
            "bedrock-agentcore:GetResourceApiKey",
        ],
        "memory": [
            "bedrock-agentcore:CreateMemory",
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:DeleteMemory",
            "bedrock-agentcore:ListMemories",
        ],
        "policy": [
            "bedrock-agentcore:CreatePolicyEngine",
            "bedrock-agentcore:GetPolicyEngine",
            "bedrock-agentcore:DeletePolicyEngine",
            "bedrock-agentcore:ListPolicyEngines",
            "bedrock-agentcore:CreatePolicy",
            "bedrock-agentcore:DeletePolicy",
            "bedrock-agentcore:ListPolicies",
            "bedrock-agentcore:UpdatePolicy",
            "bedrock-agentcore:GetPolicy",
            # CreatePolicy implicitly requires ManageAdminPolicy
            # (undocumented). See tasks/lessons.md Bug 93.
            "bedrock-agentcore:ManageAdminPolicy",
            # Bug 134 (real root cause): creating a policy SCOPED TO A GATEWAY
            # is authorized as bedrock-agentcore:ManageResourceScopedPolicy on
            # the *gateway* ARN — NOT CreatePolicy. Without it, create_policy
            # silently AccessDenied'd, the engine attached with ZERO policies,
            # and ENFORCE default-deny returned 0 tools (looked like a Cedar
            # bug; it was a missing IAM grant).
            #
            # Only the Manage verb. The Get/List "resource scoped policy" verbs
            # that used to sit on the next two lines are not AgentCore IAM actions
            # at all — same class of mistake as GetLastKTurns/RetrieveMemories in
            # build_shared_runtime_role: IAM accepts a nonexistent action without
            # complaint and authorizes nothing, so they read as capability the role
            # does not have.
            #
            # HOW TO DECIDE THIS. The oracles, in decreasing authority — and no
            # single one of them is sufficient:
            #   1. A live AccessDenied naming the action proves it exists AND is
            #      enforced. Conclusive; nothing overrides it.
            #   2. AWS's machine-readable Service Reference feed: the index at
            #      https://servicereference.us-east-1.amazonaws.com/ then
            #      /v1/<service>/<service>.json. Authoritative but it LAGS —
            #      bedrock-agentcore lists 255 actions and omits CreateTokenVault,
            #      which oracle 1 proves is real and enforced.
            #   3. `aws accessanalyzer validate-policy --policy-type
            #      IDENTITY_POLICY` -> INVALID_ACTION "does not exist". Agreed with
            #      (2) on every action checked here, because it reads the SAME
            #      dataset — so it inherits the same lag and is NOT an independent
            #      second opinion.
            #   4. botocore's service model: NOT an oracle at all.
            #      ManageResourceScopedPolicy, ManageAdminPolicy, InvokeGateway and
            #      CreateTokenVault are all real IAM actions with no SDK operation
            #      behind them, so the model is silent on the real and the fake
            #      alike. (It does carry Get/Put/DeleteResourcePolicy — resource-
            #      BASED policies, an unrelated feature.)
            #
            # So absence from (2)/(3) is NOT grounds to delete a grant on its own.
            # Removal is safe only when the action is absent from the reference AND
            # nothing in the repo calls it AND no service-side implicit
            # authorization needs it. The two Get/List verbs retired here clear all
            # three. CreateTokenVault clears none of them: it is absent from the
            # reference and from Access Analyzer, yet a fresh-account deploy fails
            # with AccessDenied on it — which is exactly how the export path lost
            # that grant once, by pruning on absence alone.
            "bedrock-agentcore:ManageResourceScopedPolicy",
            # The policy step reads the gateway it's about to attach the
            # engine to (and updates it). Without GetGateway, the bind
            # call fails with AccessDenied. See tasks/lessons.md Bug 70.
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:UpdateGateway",
            # Bug 134: the policy step reads each target's MCP tool manifest
            # (list + get gateway targets) to generate Cedar that references
            # only REAL tool actions — referencing a non-existent tool fails
            # schema validation (CREATE_FAILED).
            "bedrock-agentcore:ListGatewayTargets",
            "bedrock-agentcore:GetGatewayTarget",
        ],
        "evaluation": [
            "bedrock-agentcore:Evaluate",
            "bedrock-agentcore:CreateOnlineEvaluationConfig",
            "bedrock-agentcore:GetOnlineEvaluationConfig",
            "bedrock-agentcore:ListOnlineEvaluationConfigs",
            "bedrock-agentcore:UpdateOnlineEvaluationConfig",
            "bedrock-agentcore:DeleteOnlineEvaluationConfig",
            "logs:StartQuery",
            "logs:GetQueryResults",
            # AgentCore eval reads the aws/spans CloudWatch Logs index
            # policy (X-Ray-backed traces). The error message
            # "Access denied when accessing index policy for aws/spans"
            # really means logs:DescribeIndexPolicies / PutIndexPolicy
            # on the aws/spans log group. See lessons.md Bug 119.
            "logs:DescribeIndexPolicies",
            "logs:DescribeFieldIndexes",
            "logs:DescribeLogGroups",
            "logs:PutIndexPolicy",
            # AgentCore's CreateOnlineEvaluationConfig validates that the
            # calling principal can read X-Ray's per-account index policy
            # (where AgentCore stores the runtime's span index). Without
            # these the API returns AccessDeniedException with the
            # confusing message "Access denied when accessing index
            # policy for aws/spans" — it's the caller, not the eval
            # execution role, that needs the grant. See lessons.md Bug 119.
            "xray:GetIndexingRules",
            "xray:UpdateIndexingRule",
            "xray:GetGroup",
            "xray:GetGroups",
            "xray:CreateGroup",
            "xray:UpdateGroup",
            "xray:GetTraceSummaries",
            "xray:BatchGetTraces",
            "application-signals:Get*",
            "application-signals:List*",
            "application-signals:BatchGet*",
        ],
        "runtime_configure": [
            # CreateAgentRuntime auto-creates a default endpoint AND a
            # workload-identity record; the caller IAM principal must
            # hold all three action sets — see tasks/lessons.md Bug 46/53.
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:DeleteAgentRuntime",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
            "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
        ],
        "runtime_launch": [
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
            "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
        ],
        # Phase B — AgentCore Harness lifecycle. The harness step creates
        # the harness and polls it to READY; InvokeHarness is included so
        # Bug-9 parity holds if the step ever smoke-tests. NO harness-
        # endpoint verbs exist. InvokeHarness is
        # served by the SAME bedrock-agentcore: action prefix even though it
        # is on the DATA plane (mirrors InvokeAgentRuntime, Bug 43).
        "harness": [
            "bedrock-agentcore:CreateHarness",
            "bedrock-agentcore:GetHarness",
            "bedrock-agentcore:ListHarnesses",
            "bedrock-agentcore:UpdateHarness",
            "bedrock-agentcore:DeleteHarness",
            "bedrock-agentcore:InvokeHarness",
            # Bug 151 (caught live): a Harness is implemented ON TOP OF an
            # AgentCore Runtime — CreateHarness internally calls
            # CreateAgentRuntime (and get/update/delete mirror it), so the
            # harness step role MUST also hold the AgentRuntime lifecycle
            # verbs or CreateHarness fails with AccessDenied on
            # bedrock-agentcore:CreateAgentRuntime (resource runtime/*).
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:DeleteAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
            # CreateHarness transparently provisions a workload-identity
            # record under the harness's identity directory (same shape as
            # CreateAgentRuntime / CreateGateway, Bug 53/65).
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
            # Bug 150 — when a gateway is connected, the harness step registers
            # an OAuth2 credential provider (ensure_gateway_outbound_provider)
            # so the harness can authenticate outbound to the CUSTOM_JWT
            # gateway. Without these the harness step gets AccessDenied
            # creating that provider on the SFN path.
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:DeleteOauth2CredentialProvider",
            "bedrock-agentcore:ListOauth2CredentialProviders",
            # Repoint an existing provider rather than reuse a stale client
            # secret (see the gateway step's policy for the full reasoning).
            "bedrock-agentcore:UpdateOauth2CredentialProvider",
            # Bug 153 (caught live): the FIRST CreateOauth2CredentialProvider in
            # an account/region implicitly provisions the default token-vault,
            # so the caller needs CreateTokenVault/GetTokenVault or it fails with
            # AccessDenied on bedrock-agentcore:CreateTokenVault
            # (token-vault/default). Idempotent once the vault exists.
            "bedrock-agentcore:CreateTokenVault",
            "bedrock-agentcore:GetTokenVault",
            # Bug 152 (caught live): CreateHarness ALWAYS auto-provisions a
            # default AgentCore Memory for the harness session (even a "bare"
            # harness with no memory configured). The CALLER (this step role)
            # must hold the Memory lifecycle verbs or the harness lands in
            # CREATE_FAILED with "Memory operation failed: not authorized to
            # perform: bedrock-agentcore:CreateMemory". Delete cascades on
            # DeleteHarness, but grant Delete/Get too for completeness.
            "bedrock-agentcore:CreateMemory",
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:ListMemories",
            "bedrock-agentcore:UpdateMemory",
            "bedrock-agentcore:DeleteMemory",
        ],
    }
    if step_name in agentcore_steps:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=agentcore_steps[step_name],
                # Least privilege: AgentCore control-plane resources (runtimes,
                # gateways, memories, harnesses, token vaults, workload
                # identities, ...) are created dynamically per user deploy —
                # their ARNs are unknowable when this stack is synthesized, and
                # several verbs (Create*, List*) have no resource-ARN form.
                # Scoped by the exact per-step action lists above instead of a
                # service wildcard (AgentCore also ignores `bedrock-agentcore:*`
                # wildcards at authorization time — Bug 47).
                resources=["*"],
            )
        )
    if step_name == "status_update":
        # Teardown of an `online_evaluation_config` manifest row (_cleanup_resource, mirrored in
        # deployment_handler): DeleteOnlineEvaluationConfig by exact id, GetOnlineEvaluationConfig
        # to confirm absence, then the per-config eval-results log group. Found by
        # test_the_teardown_role_is_granted_every_call_the_dispatcher_makes once the `logs` service
        # was mapped (2026-09-25): every call site sits in a best-effort except, so the missing
        # grants were a silent permanent leak of the config and its results log group, not an error.
        #
        # Blast radius, stated for review (ARCC cnt_jZ8jrb3g6aHuyL, destructive-capability grants):
        # this role can delete ANY online evaluation config in the account -- the id is minted by
        # AgentCore at create time, so the resource can only be scoped to the type, and the service
        # reference lists NO condition keys for Get/Delete (no aws:ResourceTag gate is possible).
        # The dispatcher deletes only ids recorded in the deployment's own manifest, and the
        # log-group grant is scoped to the eval-results prefix alone: it cannot delete a runtime's
        # or a Lambda's log group. Not added to the resources=["*"] list above on purpose.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetOnlineEvaluationConfig",
                    "bedrock-agentcore:DeleteOnlineEvaluationConfig",
                ],
                resources=[f"arn:aws:bedrock-agentcore:*:{stack.account}:online-evaluation-config/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DeleteLogGroup"],
                resources=[f"arn:aws:logs:*:{stack.account}:log-group:/aws/bedrock-agentcore/evaluations/results/*"],
            )
        )

    # The policy step cannot create a gateway-scoped Cedar policy without being
    # able to CALL the gateway. AgentCore resolves the gateway named in a
    # statement AS THE CALLER, so `bedrock-agentcore:InvokeGateway` has to be on
    # this role and not just on the engine or the agent runtime. Without it,
    # create_policy fails with "Insufficient permissions to call gateway with
    # ID <id>" in BOTH validation modes.
    #
    # Proven live on the customer-export path, same API, same account, same
    # statement shape: gateway READY for many minutes, this action as the only
    # variable — CREATE_FAILED without it, ACTIVE with it, under each mode. The
    # export's equivalent grant is AgentCorePolicyGatewayResolution in
    # backend/src/app/services/cfn_template_generator.py.
    #
    # The WILDCARD-ID resource below was then proven on its own, because the
    # export's proof used one exact gateway ARN and AgentCore is known to ignore
    # wildcards elsewhere (it ignores `bedrock-agentcore:*` ACTION wildcards —
    # Bug 47), which would have made this statement a no-op. Two throwaway roles,
    # identical but for this one statement, both creating the same
    # `resource == AgentCore::Gateway::"<arn>"` permit on the same live engine:
    # with `gateway/*` the policy reached ACTIVE with no statusReasons; without
    # it, CREATE_FAILED "Insufficient permissions to call gateway with ID <id>".
    # So the resource wildcard IS honored here and the id does not have to be
    # known at synth time.
    #
    # What decides whether a deploy hits it is the SHAPE of the Cedar statement,
    # not the mode: an unconstrained `resource is AgentCore::Gateway` resolves
    # nothing and goes ACTIVE regardless. policy_step._cedar_* emits
    # `resource == AgentCore::Gateway::"<arn>"` whenever a gateway ARN is known,
    # which is the form that needs this. So a platform deploy could have gone
    # green on the unconstrained branch and still never have created a
    # gateway-scoped policy.
    #
    # Its own statement, ARN-scoped, rather than another entry in the
    # `resources=["*"]` list above. ARCC cnt_BBrFTwAEgWxA30 (start from zero and
    # add the minimum) and cnt_AGx9pUNpmdOVZB (specific actions on specific
    # resources): unlike the Create*/List* verbs up there, InvokeGateway DOES
    # have a resource form and the gateway ARN prefix IS knowable at synth time,
    # so there is no excuse for `*`. Keeping a data-plane invoke verb out of the
    # kitchen-sink control-plane statement also means this one can be deleted
    # whole if AgentCore ever stops resolving the gateway as the caller.
    if step_name == "policy":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                # Gateways are created per user deploy, so the id stays
                # wildcarded — but the account, region and resource type do not.
                # An IAM `*` spans `/`, so this also covers a gateway's targets.
                resources=[f"arn:aws:bedrock-agentcore:*:{stack.account}:gateway/*"],
            )
        )

    # Phase 1 Gap 1D — runtime_launch creates a CloudWatch dashboard
    # for the deployed runtime. cloudwatch:PutDashboard / GetDashboard
    # are global (no resource ARN format for dashboards in IAM).
    if step_name == "runtime_launch":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutDashboard",
                    "cloudwatch:GetDashboard",
                    "cloudwatch:DeleteDashboards",
                ],
                resources=["*"],
            )
        )

    # Phase 6 (Loom) — AWS Agent Registry federation (opt-in). The status_update
    # step auto-registers a just-deployed agent as a DRAFT record
    # (_auto_register_in_aws_registry), so THIS role — not the deployment
    # Lambda's — is the principal on CreateRegistryRecord.
    #
    # This grant was missing entirely, which the best-effort wrapper around the
    # auto-register hid: every deploy logged "auto-register skipped" and the
    # federation feature silently never produced a record.
    #
    # Action prefix is `agent-registry:`, NOT `bedrock-agentcore:` — at GA the
    # Registry became its own AWS service (boto3 `agent-registry-control` /
    # `agent-registry`), and both planes authorize under the control model's
    # signingName, which is `agent-registry`. Hence a separate statement rather
    # than an entry in the agentcore_steps map above.
    if step_name == "status_update":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "agent-registry:GetRegistry",
                    "agent-registry:CreateRegistryRecord",
                    "agent-registry:GetRegistryRecord",
                    "agent-registry:ListRegistryRecords",
                    # Redeploying an agent hits the name+recordVersion uniqueness
                    # key, so register() falls back to updating the existing record
                    # in place (see AwsAgentRegistry.register). Without this action
                    # that fallback AccessDenies and the best-effort wrapper hides
                    # it, leaving the record pinned to the FIRST deployment's
                    # runtime ARN — stale, and silently so.
                    "agent-registry:UpdateRegistryRecord",
                    "agent-registry:DeleteRegistryRecord",
                    "agent-registry:TagResource",
                ],
                # The registryId is configured by an admin at runtime (opt-in),
                # so registry/record ARNs are unknowable at synth time.
                resources=["*"],
            )
        )

    # Bug 196 — auto-cleanup on failure. When a deployment fails, the
    # status_update step iterates created_resources and deletes them to
    # prevent orphans (KB, Cognito pools, gateways, IAM roles, Lambdas,
    # vector buckets). Needs DELETE verbs across every resource type.
    if step_name == "status_update":
        # KB ids are service-generated — knowledge-base/* in this
        # account+region is the tightest pattern for cleanup-by-manifest.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:ListDataSources",
                    "bedrock:DeleteDataSource",
                    "bedrock:GetKnowledgeBase",
                ],
                resources=[f"arn:aws:bedrock:*:{stack.account}:knowledge-base/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:DeleteVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListIndexes",
                    "s3vectors:DeleteIndex",
                ],
                resources=[f"arn:aws:s3vectors:*:{stack.account}:bucket/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:ListAttachedRolePolicies",
                    "iam:DetachRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:DeleteRolePolicy",
                ],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool",
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:DeleteUserPoolDomain",
                    # A gateway on the SHARED platform pool records no deletable
                    # pool (correct — it holds every other gateway's client), so
                    # the one credential a failed shared-pool deploy leaves behind
                    # is its own app client. Measured live on this role: without
                    # this action the new cognito_app_client arm below fails with
                    # AccessDenied and the client stays, secret still mintable.
                    # DescribeUserPool above is what classify_user_pool reads, so
                    # the ownership check needs no extra grant.
                    "cognito-idp:DeleteUserPoolClient",
                    # The gateway's per-name scope definition, deleted after its
                    # client. ListUserPoolClients is the co-residency check
                    # (gateway_deployer.resource_server_is_unused): two deploys that
                    # picked the same gateway name SHARE one resource server, so
                    # deleting it unchecked revokes the co-resident gateway's scope.
                    # It returns ids and NAMES ONLY — never a client secret, which is
                    # exactly why the check uses it instead of DescribeUserPoolClient
                    # (that action authorizes on the POOL, so the only workable grant
                    # reads every gateway's secret — see cognito_client_secret_grant).
                    "cognito-idp:DeleteResourceServer",
                    "cognito-idp:ListUserPoolClients",
                ],
                resources=[f"arn:aws:cognito-idp:*:{stack.account}:userpool/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:DeleteGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:GetHarness",
                    # Everything below was called by _cleanup_resource and granted to
                    # nowhere. The role held 6 of the 16 delete verbs its own code
                    # issues, and each missing one is a resource a failed deploy keeps:
                    #
                    # ListGatewayTargets/DeleteGatewayTarget — DeleteGateway REJECTS a
                    #   gateway that still has targets, which is exactly why the gateway
                    #   arm deletes them first. Without these, any deploy that failed
                    #   after target creation leaked the gateway permanently.
                    # DeleteMemory — AgentCore Memory holds conversation data.
                    # DeleteOauth2CredentialProvider / DeleteApiKeyCredentialProvider —
                    #   these hold the customer's OAuth client secret / raw API key.
                    # ListPolicies/DeletePolicy/DeletePolicyEngine — the engine delete
                    #   rejects an engine that still has policies, same shape as the
                    #   gateway.
                    # DeleteHarness — a harness-mode deploy's primary resource.
                    #
                    # Every name verified against the AWS Service Reference feed
                    # (servicereference.us-east-1.amazonaws.com/v1/bedrock-agentcore),
                    # not the docs.
                    "bedrock-agentcore:ListGatewayTargets",
                    "bedrock-agentcore:DeleteGatewayTarget",
                    "bedrock-agentcore:DeleteMemory",
                    "bedrock-agentcore:DeleteHarness",
                    # F-80, MEASURED live 2026-09-24: DeleteHarness needs this, and the
                    # note further down predicting the harness would "match the runtime"
                    # was wrong in the one way that mattered — it named the wrong action.
                    # A harness IS a runtime (Bug 151), so DeleteHarness tears down the
                    # backing runtime's ENDPOINT in a forward-access session under the
                    # caller's credentials, and that is a different verb from
                    # DeleteAgentRuntime. Without it the harness parks in DELETE_FAILED:
                    #   "assumed-role/...-step-status-update is not authorized to perform:
                    #    bedrock-agentcore:DeleteAgentRuntimeEndpoint on resource:
                    #    runtime/harness_p0bharn1790231300_302f262f-d68u4KDZ1M"
                    # (harness p0bharn1790231300_302f262f-uh37DKSpQX, read off its own
                    # failureReason). The harness, its backing runtime, that runtime's
                    # workload identity and the auto-provisioned Memory holding the
                    # conversation then leak PERMANENTLY, because a delete is never
                    # retried. The differential is what makes this unarguable: of the
                    # three roles that delete a harness, DeploymentLambdaRole (serving
                    # DELETE /api/runtime) and StepHarnessRole both already held this
                    # verb and only the cleanup role did not — so the product's own
                    # delete cascaded harness+runtime+identity+memory cleanly in the
                    # same account minutes earlier, and ONLY the failed-deploy path
                    # leaked. Same asymmetry as the DeleteWorkloadIdentity arm below.
                    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                    "bedrock-agentcore:ListPolicies",
                    "bedrock-agentcore:DeletePolicy",
                    "bedrock-agentcore:DeletePolicyEngine",
                    "bedrock-agentcore:DeleteOauth2CredentialProvider",
                    "bedrock-agentcore:DeleteApiKeyCredentialProvider",
                ],
                # AgentCore gateway/runtime ids are minted per-deploy — ARNs
                # unknowable at synth time; scoped by exact delete/get verbs.
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                # Measured live 2026-09-21: WITHOUT this action, cleaning up a failed
                # deploy's gateway looked like it worked and did nothing. DeleteGateway
                # returned success to the caller, and the control plane then made a
                # forward-access-session call back out UNDER THE CALLER'S CREDENTIALS to
                # remove the gateway's workload identity. That is what 403'd — after the
                # API had already answered. The gateway parked in status FAILED with
                #   "Failed to delete gateway: ... not authorized to perform:
                #    bedrock-agentcore:DeleteWorkloadIdentity on resource:
                #    ...workload-identity-directory/default (Service:
                #    AgentCredentialProvider, Status Code: 403)"
                # and _cleanup_resource, having seen a clean return, counted it cleaned.
                #
                # `simulate-principal-policy` is no use for finding this, and saying so
                # is the point: on the real role it answers DeleteGateway `allowed` (it
                # is) and DeleteWorkloadIdentity `implicitDeny`, while the delete does
                # not work. It cannot model a FAS hop, and the missing grant is on a
                # different resource type than the action you would think to simulate.
                # Only a live 403 finds this. Do not cite the simulator as evidence here.
                #
                # BOTH resource ARNs are required, and that is measured, not caution.
                # AgentCore performs TWO separate authorizations for one logical delete —
                # once against the directory ARN, once against the per-identity child ARN
                # — and the 403 names whichever check it reached first. Five throwaway
                # gateways under five roles differing in one statement:
                #   directory ARN only      -> FAILED, 403 naming .../workload-identity/<id>
                #   child ARN only          -> FAILED, 403 naming .../default
                #   both ARNs               -> gone (ResourceNotFoundException)
                # So the original 403, which named only the directory, understated the
                # requirement: granting exactly what it asked for still leaks. The child
                # wildcard is unavoidable — the identity name IS the service-generated
                # gateway/runtime id, unknowable at synth time — but the directory ARN is
                # fully determined, so neither is `Resource: "*"`.
                #
                # It applies to three of the verbs in the statement above, not one:
                # CreateGateway, CreateAgentRuntime and CreateHarness each provision a
                # workload-identity record (see this file's gateway/runtime_configure/
                # harness entries, where every CREATING role already pairs
                # Create/DeleteWorkloadIdentity with its lifecycle verbs for exactly this
                # reason — Bug 53/65). The cleanup role deletes all three types and held
                # the pairing for none of them. DeploymentLambdaRole, which serves
                # DELETE /api/runtime/{id}, already has it (lambdas.py) — so only the
                # FAILED-deploy path leaked, which is also the path nobody watches.
                #
                # The three types FAIL DIFFERENTLY, which matters for anyone reading a
                # cleanup log rather than this comment:
                #   gateway — async and SILENT. 200 + {"status":"DELETING"}, then the
                #     resource parks in FAILED with the 403 in `statusReasons`. Nothing
                #     reaches the caller's `except`, so it is counted cleaned.
                #   runtime — synchronous and LOUD. DeleteAgentRuntime raises
                #     AccessDeniedException naming DeleteWorkloadIdentity, and the runtime
                #     stays READY (it does not park in FAILED). The exception DOES reach
                #     _cleanup_resource's except — which logs it and counts the resource
                #     handled anyway, so it still leaks, just not invisibly.
                #   harness — MEASURED 2026-09-24 (F-80), and it matched NEITHER. It is
                #     async and parks in DELETE_FAILED like the gateway, not synchronous
                #     like the runtime — but the 403 named a THIRD action this statement
                #     did not grant, DeleteAgentRuntimeEndpoint, because a harness's
                #     backing runtime has an endpoint a bare runtime delete never
                #     reaches. The prediction "expected to match the runtime" was right
                #     that a FAS hop exists and wrong about which grant closes it, which
                #     is exactly why the statement did NOT cover it after all.
                # Do not generalize one shape to the others: it was the assumption that
                # all three behaved like the gateway that this comment originally made,
                # and the harness then disproved the replacement assumption too. Each
                # type's shape and its missing verb are separate measurements.
                actions=["bedrock-agentcore:DeleteWorkloadIdentity"],
                resources=[
                    f"arn:aws:bedrock-agentcore:*:{stack.account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:*:{stack.account}"
                    ":workload-identity-directory/default/workload-identity/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                # The guardrail arm. Guardrail ids are service-generated (the ARN
                # carries the id, not the name), so guardrail/* in this account+region
                # is the tightest pattern — same scoping as the guardrails step that
                # creates them.
                actions=["bedrock:DeleteGuardrail"],
                resources=[f"arn:aws:bedrock:*:{stack.account}:guardrail/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    # Required by _release_shared_tool_lambda (Defect C): the
                    # failure-path auto-cleanup ref-counts SHARED tool Lambdas —
                    # it reads the resource policy and drops this gateway's
                    # invoke grant. Without these it fail-safes to "kept" and
                    # the shared Lambda leaks with dangling grants.
                    "lambda:GetPolicy",
                    "lambda:RemovePermission",
                    # The ownership READ the failure-path teardown now does BEFORE
                    # lambda:DeleteFunction (F-7c). This grant is not optional and the
                    # direction of the failure is why: the authorizer treats an
                    # AccessDenied on the tag read as "cannot prove ownership" and
                    # therefore KEEPS the function, so shipping the code without this
                    # statement would turn every failure-path cleanup into a leak --
                    # silently, because the refusal is a log line and the step still
                    # succeeds. The F-7b deploy taught this in the other direction
                    # (grant without code is inert); both halves have to land together.
                    "lambda:ListTags",
                ],
                resources=[f"arn:aws:lambda:*:{stack.account}:function:AgentCore*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                # Same opt-in tag as the gateway step's AddPermission statement: a
                # failed deploy's auto-cleanup must be able to take its own
                # AllowAgentCoreInvoke-<role> statement back off a customer's
                # bring-your-own-Lambda target. Only the removal verbs, never
                # AddPermission — teardown has no reason to grant anything.
                actions=["lambda:GetPolicy", "lambda:RemovePermission"],
                resources=[f"arn:aws:lambda:*:{stack.account}:function:*"],
                conditions={"StringEquals": {"aws:ResourceTag/AgentCoreGatewayTarget": "allow"}},
            )
        )
        # The "secret" arm of _cleanup_resource has existed since Bug 196 with NO
        # grant behind it. Measured live on deployment 959b2c60 (2026-09-21): a deploy
        # that minted a connector credential secret and then failed logged
        # `AccessDeniedException ... DeleteSecret` and left the RAW CUSTOMER CREDENTIAL
        # in Secrets Manager, tagged DeploymentId=<the failed deploy>. Six such secrets
        # were sitting in the test account.
        #
        # Delete and describe ONLY. Teardown never needs to read a credential, so
        # GetSecretValue is deliberately absent — ARCC cnt_LuG2TKuO0errRp requires
        # least-privilege access to secrets, and a teardown role that can read every
        # connector credential is a far worse outcome than the leak it fixes.
        # Same resource list as the gateway/mcp_server minting statement, so a secret
        # this platform can create is exactly a secret this platform can remove.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:AgentCore*",
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-*",
                    f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )
        # Tag discovery for a secret no manifest row names (a lost CreateSecret
        # response, a step killed before its row): unrecorded_deployment_secret_rows.
        # Without it the discovery 403s, and the cleanup correctly reports
        # delete_retained for every failed deploy. ListSecrets has no resource-level
        # scoping, so `*`; it returns metadata only, never a SecretString.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:ListSecrets"],
                resources=["*"],
            )
        )
        # The oss_collection arm. An OpenSearch Serverless collection is a STANDING
        # billable resource (~$350/mo at the 2-OCU minimum), so a KB deploy that
        # provisioned one and then failed in a later state is the most expensive orphan
        # this platform can create. Delete verbs only; the collection/index create verbs
        # stay with the knowledge_base step.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:DeleteCollection", "aoss:DeleteIndex"],
                resources=[f"arn:aws:aoss:*:{stack.account}:collection/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                # BatchGetCollection resolves the recorded NAME to the id DeleteCollection
                # needs, and the security/data-access policy APIs are account-level with no
                # resource ARN form (live-verified on the knowledge_base role: the same
                # actions on collection/* fail AccessDenied). Least privilege is the exact
                # action list.
                actions=[
                    "aoss:BatchGetCollection",
                    # Ownership and post-delete confirmation both read the
                    # untaggable AOSS policies before/after mutation.
                    "aoss:GetSecurityPolicy",
                    "aoss:GetAccessPolicy",
                    "aoss:DeleteSecurityPolicy",
                    "aoss:DeleteAccessPolicy",
                ],
                resources=["*"],
            )
        )
        # The s3_object arm: staged connector OpenAPI specs, which gateway_step wrote
        # into this same bucket. Delete only — the failure path has no reason to read
        # or write an artifact.
        artifacts_bucket.grant_delete(role)
        # Teardown deletes staged artifacts, never the dependency bundles (F-21, buckets.py).
        deny_writes_to_agentcore_deps(role, artifacts_bucket)
        # delete_owned_s3_object lists the key's versions and reads each version's tags
        # before deleting it (F-60). grant_delete has no read or list, so without these
        # the proof fails AccessDenied and the staged object is retained as unprovable
        # -- in the home region too.
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=READ_TAG_ACTIONS,
                resources=[artifacts_bucket.arn_for_objects("*")],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=LIST_VERSION_ACTIONS,
                resources=[artifacts_bucket.bucket_arn],
            )
        )
        grant_regional_artifact_buckets(role, stack, "delete", read_tags=True)

    return role


def build_step_lambdas(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    backend_code: _lambda.Code,
    tables: Tables,
    artifacts_bucket: s3.Bucket,
    shared_runtime_role: iam.Role,
    shared_mcp_runtime_role: iam.Role,
    role_boundary: iam.ManagedPolicy,
    gateway_auth_pool: cognito.IUserPool | None = None,
    gateway_auth_domain: str = "",
) -> dict[str, _lambda.Function]:
    """Create individual Lambda functions for each Step Functions step.

    Requirements: 1.3, 6.3
    """
    step_configs = {
        "validate": {
            "handler": "src/app/step_handlers/validate_step.handler",
            "memory": 256,
            "timeout": 30,
        },
        "codegen": {
            "handler": "src/app/step_handlers/codegen_step.handler",
            "memory": 1024,
            "timeout": 90,
        },
        "iam": {
            "handler": "src/app/step_handlers/iam_step.handler",
            "memory": 256,
            "timeout": 60,
        },
        "mcp_server": {
            "handler": "src/app/step_handlers/mcp_server_step.handler",
            "memory": 1024,
            "timeout": 600,
        },
        "gateway": {
            "handler": "src/app/step_handlers/gateway_step.handler",
            "memory": 512,
            # Bug 134: lockstep with the DeployGateway SFN task timeout (720s).
            # The step probes the gateway MCP tool plane and may recreate the
            # gateway up to 3x to beat the empty-tool-plane provisioning flake.
            "timeout": 720,
        },
        "runtime_configure": {
            "handler": "src/app/step_handlers/runtime_configure_step.handler",
            "memory": 512,
            # 240s budget: Bug 52's IAM-race retry can spend up to 75s
            # waiting for AgentCore's IAM cache to populate after
            # put_role_policy. Plus the create call itself can be slow.
            # 60s caused 100% deploy regression. See lessons Bug 54.
            "timeout": 240,
        },
        "runtime_launch": {
            "handler": "src/app/step_handlers/runtime_launch_step.handler",
            "memory": 512,
            "timeout": 600,
        },
        "auth": {
            "handler": "src/app/step_handlers/auth_step.handler",
            "memory": 256,
            "timeout": 60,
        },
        "status_update": {
            "handler": "src/app/step_handlers/status_update_step.handler",
            "memory": 256,
            # 15s until 2026-09-21, which was the SHORTEST of every step Lambda here
            # (the rest run 30-720s) and belonged to a handler that only wrote a
            # DynamoDB row. It now also runs _auto_cleanup_on_failure, a full
            # destructive teardown of an arbitrary deployment manifest: gateway targets,
            # gateway, runtime, memory, guardrail, knowledge base, OpenSearch
            # collection, S3 vectors bucket, Cognito client + resource server, secret
            # and IAM role — sequentially, one or more API calls each.
            #
            # Measured on the live stack: 30 invocations, max 6815 ms, p90 4974 ms. So
            # nothing has timed out yet (0 "Task timed out" in the log group), but that
            # is against the small manifests this estate produces, and the headroom on a
            # fully-featured one is not there. A timeout mid-pass leaves every
            # not-yet-reached resource in the customer's account permanently AND skips
            # the completion line, so there is no record of how far it got.
            #
            # Not a status-loss risk, which is why this is sized generously rather than
            # urgently: the FAILED status is written BEFORE the cleanup runs, so a
            # timeout costs orphans and observability, not the deployment's state.
            #
            # 120s also pays for the bounded delete-confirmation wait added alongside
            # this (see _confirm_gateway_deleted). Lambda bills for time used, so a
            # ceiling that is never reached costs nothing.
            "timeout": 120,
        },
        "memory": {
            "handler": "src/app/step_handlers/memory_step.handler",
            "memory": 512,
            # 120s until 2026-09-22, which was exactly equal to the budget of the
            # _wait_for_memory_ready poll running INSIDE it. That made a fresh memory a
            # guaranteed timeout rather than an edge case: the poll's wall clock is its
            # own budget of sleep plus one get_memory per iteration plus a 10s data-plane
            # settle, so it could not finish inside the Lambda even when the memory went
            # ACTIVE, and the poll's own "did not become ACTIVE in Ns" error was
            # unreachable. Measured live on acfe2e-p0920: Status timeout at 120,000 ms,
            # memory still CREATING, and the SFN retry then failed DIFFERENTLY -- it found
            # the memory its own first attempt had created and took the adoption branch.
            #
            # 420s = the 300s poll + the 10s settle + headroom for the pre-work
            # (_find_memory_by_name's retries, the memory IAM role create, create_memory)
            # and the manifest write after. Sized the way the policy step already is for
            # the identical class of problem (Bug 177: a freshly-created AgentCore resource
            # takes minutes to converge) -- policy got 600s and memory never did.
            # A ceiling that is never reached costs nothing; Lambda bills for time used.
            "timeout": 420,
        },
        "evaluation": {
            "handler": "src/app/step_handlers/evaluation_step.handler",
            "memory": 512,
            "timeout": 120,
        },
        "policy": {
            "handler": "src/app/step_handlers/policy_step.handler",
            "memory": 512,
            # Bug 177: a freshly-created policy engine takes minutes to become
            # truly ACTIVE for policy creation (create_policy 409s "engine is
            # CREATING" / validates "Insufficient permissions to call gateway"
            # until it converges). The step waits for the engine + retries the
            # policy create across that window, so it needs a generous budget.
            "timeout": 600,
        },
        "knowledge_base": {
            "handler": "src/app/step_handlers/knowledge_base_step.handler",
            "memory": 1024,
            "timeout": 600,
        },
        "guardrails": {
            "handler": "src/app/step_handlers/guardrails_step.handler",
            "memory": 512,
            "timeout": 120,
        },
        # Phase B — AgentCore Harness (parallel authoring/deploy path).
        # Builds the harness exec role, calls CreateHarness, then polls
        # wait_for_harness_ready (up to 600s service-side); the 300s Lambda
        # budget covers role creation + create + a partial ready poll, with
        # the SFN task timeout matched below.
        "harness": {
            "handler": "src/app/step_handlers/harness_step.handler",
            "memory": 512,
            "timeout": 300,
        },
    }

    lambdas: dict[str, _lambda.Function] = {}
    for step_name, config in step_configs.items():
        step_role = _create_step_role(
            stack,
            cfg,
            step_name,
            tables=tables,
            artifacts_bucket=artifacts_bucket,
            role_boundary=role_boundary,
            shared_runtime_role=shared_runtime_role,
            shared_mcp_runtime_role=shared_mcp_runtime_role,
            gateway_auth_pool=gateway_auth_pool,
        )
        fn = _lambda.Function(
            stack,
            f"Step{step_name.title().replace('_', '')}Lambda",
            function_name=f"{cfg.project}-{cfg.env}-step-{step_name.replace('_', '-')}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler=config["handler"],
            code=backend_code,
            memory_size=config["memory"],
            timeout=Duration.seconds(config["timeout"]),
            role=step_role,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DEPLOYMENTS_TABLE_NAME": tables.deployments.table_name,
                "DEPLOYMENT_TABLE_NAME": tables.deployments.table_name,
                "GATEWAY_NAME_CLAIMS_TABLE_NAME": tables.gateway_name_claims.table_name,
                "WORKFLOWS_TABLE_NAME": tables.workflows.table_name,
                # Phase 1 Gap 1A — versioning tables; status_update_step
                # writes the AgentVersion + RuntimeSlots rows on success.
                "AGENT_VERSIONS_TABLE_NAME": tables.agent_versions.table_name,
                "RUNTIME_SLOTS_TABLE_NAME": tables.runtime_slots.table_name,
                # Phase 2 Gap 2D — HITL table name so runtime_configure_step
                # injects it into the runtime's environmentVariables.
                "HITL_REQUESTS_TABLE_NAME": tables.hitl_requests.table_name,
                # Loom-study 2.2 — approval policies live in the tag-policy
                # table; runtime_configure_step reads them to inject
                # LOOM_APPROVAL_POLICIES into the runtime (guaranteed HITL hook).
                "TAG_POLICY_TABLE_NAME": tables.tag_policy.table_name,
                "ARTIFACTS_BUCKET_NAME": artifacts_bucket.bucket_name,
                "ENVIRONMENT": cfg.env,
                # These three together form the resource-owner tag
                # ({project}-{env}-{region}) that services/resource_ownership.py
                # stamps on account-global resources — Cognito pools, connector and
                # OTEL secrets, AgentCoreMemory-* roles — and that cleanup.sh gates
                # deletion on. Without PROJECT_NAME the handler falls back to the
                # default project name and tags resources for the wrong stack, which
                # is exactly the cross-deployment deletion this prevents.
                "PROJECT_NAME": cfg.project,
                # The same value that builds the aws:TagKeys allowlist on this role's
                # tag-on-create grants. services/resource_tagging.py refuses a governance tag
                # key outside these namespaces, so the deploy is refused before it creates
                # anything instead of hitting the AccessDenied that condition produces
                # mid-deployment. The two halves are asserted to agree by
                # infra/tests/test_governance_tag_namespace_grant.py.
                GOVERNANCE_TAG_KEY_PREFIXES_ENV: governance_tag_key_prefixes_env_value(),
                "APP_AWS_REGION": stack.region,
                "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
                # Shared runtime execution role — pre-created at stack
                # init to avoid the per-deploy IAM-propagation race.
                "SHARED_RUNTIME_ROLE_ARN": shared_runtime_role.role_arn,
                # Model-free variant selected by a standalone FastMCP runtime
                # (runtime_artifact_kind == "mcp"). Every step that selects,
                # records, or tears down a role must see the same explicit
                # authority contract, so it is injected into all step Lambdas.
                "SHARED_MCP_RUNTIME_ROLE_ARN": shared_mcp_runtime_role.role_arn,
                # The permissions boundary every role a step creates must carry (F-06,
                # role_boundary.py). Unconditional so the backend can start passing it as
                # PermissionsBoundary= before the iam:PermissionsBoundary conditions are on.
                BOUNDARY_ARN_ENV: role_boundary.managed_policy_arn,
                # Shared gateway-auth Cognito pool + its warm hosted domain. When
                # both are set, gateway_deployer._create_cognito_oauth creates only
                # a per-gateway resource server + app client inside this pool
                # instead of a whole new pool and a COLD domain. A fresh hosted
                # domain is unreachable for >381s (measured) while the deploy-time
                # MCP probe window is 90s, so a per-deploy domain made the tool
                # plane unverifiable and drove a destructive retry loop. Empty
                # strings preserve the old per-gateway-pool behaviour, so a step
                # Lambda from an older stack keeps working.
                "GATEWAY_SHARED_USER_POOL_ID": (
                    gateway_auth_pool.user_pool_id if gateway_auth_pool is not None else ""
                ),
                "GATEWAY_SHARED_USER_POOL_DOMAIN": gateway_auth_domain,
            },
            log_group=logs.LogGroup(
                stack,
                f"Step{step_name.title().replace('_', '')}LogGroup",
                log_group_name=f"/aws/lambda/{cfg.project}-{cfg.env}-step-{step_name.replace('_', '-')}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        otel.apply(fn, f"step-{step_name.replace('_', '-')}")
        lambdas[step_name] = fn

    return lambdas
