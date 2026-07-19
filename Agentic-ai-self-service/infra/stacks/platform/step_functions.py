"""Step Functions state machine for deployment orchestration."""

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks

from .config import PlatformConfig
from .tables import Tables


def build_state_machine(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    step_lambdas: dict[str, _lambda.Function],
    tables: Tables,
) -> sfn.StateMachine:
    """Create Step Functions state machine for deployment orchestration.

    Retry: 3 attempts with exponential backoff (2s, 4s, 8s)
    Catch: fallback to failure handler writing error to DynamoDB
    Per-step timeouts per design table
    Overall timeout: 30 minutes

    Requirements: 1.3, 1.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 7.1
    """
    # Failure handler — writes error to DynamoDB
    failure_handler = _create_step_task(
        stack,
        "StatusUpdateFailure",
        step_lambdas["status_update"],
        timeout_seconds=15,
        result_path="$.failure_result",
    )
    failure_handler.add_retry(**_retry_kwargs())
    fail_state = sfn.Fail(stack, "DeploymentFailed", cause="Deployment failed", error="DeploymentError")
    failure_handler.next(fail_state)

    # --- Define steps ---
    # Each step handler returns {**event, ...new_fields} so we use result_path="$"
    # to replace the entire state, allowing fields to accumulate across steps.
    validate = _create_step_task(
        stack,
        "ValidateWorkflow",
        step_lambdas["validate"],
        timeout_seconds=30,
        result_path="$",
    )
    validate.add_retry(**_retry_kwargs())
    validate.add_catch(**_catch_kwargs(failure_handler))

    guardrails = _create_step_task(
        stack,
        "CreateGuardrails",
        step_lambdas["guardrails"],
        timeout_seconds=120,
        result_path="$",
    )
    guardrails.add_retry(**_retry_kwargs())
    guardrails.add_catch(**_catch_kwargs(failure_handler))

    mcp_server = _create_step_task(
        stack,
        "DeployMCPServer",
        step_lambdas["mcp_server"],
        timeout_seconds=600,
        result_path="$",
    )
    mcp_server.add_retry(**_retry_kwargs())
    mcp_server.add_catch(**_catch_kwargs(failure_handler))

    codegen = _create_step_task(
        stack,
        "GenerateCode",
        step_lambdas["codegen"],
        timeout_seconds=90,
        result_path="$",
    )
    codegen.add_retry(**_retry_kwargs())
    codegen.add_catch(**_catch_kwargs(failure_handler))

    iam_step = _create_step_task(
        stack,
        "CreateIAMRole",
        step_lambdas["iam"],
        # 90s budget: create_runtime_iam_role does put_role_policy + 15s
        # IAM-propagation sleep + per-tool inline policy attachments. 60s
        # was tight on cold starts.
        timeout_seconds=90,
        result_path="$",
    )
    iam_step.add_retry(**_retry_kwargs())
    iam_step.add_catch(**_catch_kwargs(failure_handler))

    gateway = _create_step_task(
        stack,
        "DeployGateway",
        step_lambdas["gateway"],
        # Bug 134: the gateway step now also resolves + waits for the target
        # MCP tool manifest (up to ~90s) so the policy step gets authoritative
        # tool action names. Raise the cap in lockstep with the Lambda timeout
        # (Bug 56) so a slow manifest sync surfaces as a real failure, not a
        # retryable States.Timeout that could mask a broken policy.
        timeout_seconds=720,
        result_path="$",
    )
    gateway.add_retry(**_retry_kwargs())
    gateway.add_catch(**_catch_kwargs(failure_handler))

    knowledge_base = _create_step_task(
        stack,
        "CreateKnowledgeBase",
        step_lambdas["knowledge_base"],
        timeout_seconds=600,
        result_path="$",
    )
    knowledge_base.add_retry(**_retry_kwargs())
    knowledge_base.add_catch(**_catch_kwargs(failure_handler))

    memory_step = _create_step_task(
        stack,
        "CreateMemory",
        step_lambdas["memory"],
        timeout_seconds=120,
        result_path="$",
    )
    memory_step.add_retry(**_retry_kwargs())
    memory_step.add_catch(**_catch_kwargs(failure_handler))

    policy_step = _create_step_task(
        stack,
        "CreatePolicy",
        step_lambdas["policy"],
        # Bug 177 + Cedar IGNORE_ALL_FINDINGS convergence: matched to the 600s
        # Lambda budget — the engine CREATING->ACTIVE + up to 12 policy-create
        # retries (as the engine<->gateway authorization converges) can take
        # several minutes on a freshly-created gateway.
        timeout_seconds=600,
        result_path="$",
    )
    policy_step.add_retry(**_retry_kwargs())
    policy_step.add_catch(**_catch_kwargs(failure_handler))

    runtime_configure = _create_step_task(
        stack,
        "ConfigureRuntime",
        step_lambdas["runtime_configure"],
        # Match the underlying Lambda timeout (240s — bumped for Bug 54).
        # The IAM-propagation retry loop inside `create_agent_runtime` can
        # legitimately spend up to 75s waiting for AgentCore's IAM cache.
        # See tasks/lessons.md Bug 56 — SFN task TimeoutSeconds is the
        # outer cap and must match the Lambda's.
        timeout_seconds=240,
        result_path="$",
    )
    runtime_configure.add_retry(**_retry_kwargs())
    runtime_configure.add_catch(**_catch_kwargs(failure_handler))

    runtime_launch = _create_step_task(
        stack,
        "LaunchRuntime",
        step_lambdas["runtime_launch"],
        timeout_seconds=600,
        result_path="$",
    )
    runtime_launch.add_retry(**_retry_kwargs())
    runtime_launch.add_catch(**_catch_kwargs(failure_handler))

    # Phase B — AgentCore Harness deploy task (parallel to the codegen →
    # iam → configure → launch Runtime path). Matches the SFN task timeout
    # to the Lambda budget (300s). Shares the same retry/catch wrappers.
    harness_step = _create_step_task(
        stack,
        "DeployHarness",
        step_lambdas["harness"],
        timeout_seconds=300,
        result_path="$",
    )
    harness_step.add_retry(**_retry_kwargs())
    harness_step.add_catch(**_catch_kwargs(failure_handler))

    evaluation_step = _create_step_task(
        stack,
        "CreateEvaluation",
        step_lambdas["evaluation"],
        timeout_seconds=120,
        result_path="$",
    )
    evaluation_step.add_retry(**_retry_kwargs())
    evaluation_step.add_catch(**_catch_kwargs(failure_handler))

    auth = _create_step_task(
        stack,
        "ConfigureJWTAuth",
        step_lambdas["auth"],
        timeout_seconds=60,
        result_path="$",
    )
    auth.add_retry(**_retry_kwargs())
    auth.add_catch(**_catch_kwargs(failure_handler))

    status_update = _create_step_task(
        stack,
        "UpdateStatusSuccess",
        step_lambdas["status_update"],
        timeout_seconds=15,
        result_path="$",
    )
    status_update.add_retry(**_retry_kwargs())
    status_update.add_catch(**_catch_kwargs(failure_handler))

    succeed = sfn.Succeed(stack, "DeploymentSucceeded")

    # --- Build chain with conditionals ---
    # Flow: validate → [mcp_server?] → [knowledge_base?] → [gateway?] → [memory?] → [policy?]
    #       → codegen → iam → configure → launch → [evaluation?] → [auth?] → status
    #
    # KB runs BEFORE gateway because deploy_gateway() reads knowledge_base_result
    # from the event to create the KB Lambda target.
    #
    # Each optional step uses a Pass state as a skip target so that
    # each Lambda task's .next() is called exactly once (CDK requirement).
    has_guardrails = sfn.Condition.is_present("$.guardrails_config")
    has_mcp_server = sfn.Condition.is_present("$.mcp_server_config")
    has_gateway = sfn.Condition.is_present("$.gateway_config")
    has_knowledge_base = sfn.Condition.is_present("$.knowledge_base_config")
    has_memory = sfn.Condition.is_present("$.memory_config")
    has_policy = sfn.Condition.is_present("$.policy_config")
    has_evaluation = sfn.Condition.is_present("$.evaluation_config")

    skip_guardrails = sfn.Pass(stack, "SkipGuardrails")
    skip_mcp_server = sfn.Pass(stack, "SkipMCPServer")
    skip_knowledge_base = sfn.Pass(stack, "SkipKnowledgeBase")
    skip_gateway = sfn.Pass(stack, "SkipGateway")
    skip_memory = sfn.Pass(stack, "SkipMemory")
    skip_policy = sfn.Pass(stack, "SkipPolicy")
    skip_evaluation = sfn.Pass(stack, "SkipEvaluation")
    skip_auth = sfn.Pass(stack, "SkipAuth")

    # validate → guardrails choice
    validate.next(sfn.Choice(stack, "HasGuardrails?").when(has_guardrails, guardrails).otherwise(skip_guardrails))
    guardrails.next(skip_guardrails)

    # → mcp_server choice
    skip_guardrails.next(sfn.Choice(stack, "HasMCPServer?").when(has_mcp_server, mcp_server).otherwise(skip_mcp_server))
    mcp_server.next(skip_mcp_server)  # converge after mcp_server

    # → knowledge base choice (runs before gateway so result is available)
    skip_mcp_server.next(
        sfn.Choice(stack, "HasKnowledgeBase?").when(has_knowledge_base, knowledge_base).otherwise(skip_knowledge_base)
    )
    knowledge_base.next(skip_knowledge_base)

    # → gateway choice (reads knowledge_base_result to create KB Lambda target)
    skip_knowledge_base.next(sfn.Choice(stack, "HasGateway?").when(has_gateway, gateway).otherwise(skip_gateway))
    gateway.next(skip_gateway)  # converge after gateway

    # → memory choice
    skip_gateway.next(sfn.Choice(stack, "HasMemory?").when(has_memory, memory_step).otherwise(skip_memory))
    memory_step.next(skip_memory)

    # → policy choice (only meaningful when gateway exists, but handler handles gracefully)
    skip_memory.next(sfn.Choice(stack, "HasPolicy?").when(has_policy, policy_step).otherwise(skip_policy))
    policy_step.next(skip_policy)

    # → harness vs. runtime deploy-mode choice.
    # Phase B: deployment_mode=="harness" diverts to the AgentCore Harness
    # task (no codegen / no per-runtime IAM / no runtime configure+launch),
    # then rejoins the shared tail at the evaluation choice — so the SAME
    # status_update (and optional auth) steps still run, keeping connectors+
    # memory parity in BOTH modes. The default (Visual Canvas) Runtime path
    # is unchanged: absent/any-other deployment_mode falls through to codegen.
    # Both branches converge on `post_deploy_choice` so each task's .next()
    # is wired exactly once (CDK requirement).
    post_deploy_choice = (
        sfn.Choice(stack, "HasEvaluation?").when(has_evaluation, evaluation_step).otherwise(skip_evaluation)
    )

    is_harness_mode = sfn.Condition.string_equals("$.deployment_mode", "harness")
    skip_policy.next(sfn.Choice(stack, "IsHarnessMode?").when(is_harness_mode, harness_step).otherwise(codegen))

    # Default Runtime path (UNCHANGED): codegen → iam → configure → launch
    codegen.next(iam_step)
    iam_step.next(runtime_configure)
    runtime_configure.next(runtime_launch)
    runtime_launch.next(post_deploy_choice)

    # Harness path rejoins the shared tail at the evaluation choice, exactly
    # where runtime_launch would continue — so status_update still runs.
    harness_step.next(post_deploy_choice)

    # → evaluation choice (shared tail)
    evaluation_step.next(skip_evaluation)

    # → auth choice (only when gateway was deployed)
    skip_evaluation.next(sfn.Choice(stack, "HasGatewayForAuth?").when(has_gateway, auth).otherwise(skip_auth))
    auth.next(skip_auth)

    # → status update → succeed
    skip_auth.next(status_update)
    status_update.next(succeed)

    # State machine role
    sm_role = iam.Role(
        stack,
        "StateMachineRole",
        assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
    )
    # Grant invoke on all step lambdas
    for fn in step_lambdas.values():
        fn.grant_invoke(sm_role)
    # DynamoDB access for deployment state
    tables.deployments.grant_read_write_data(sm_role)
    # Phase 1 Gap 1A — state machine writes versions/slots via status_update.
    tables.agent_versions.grant_read_write_data(sm_role)
    tables.runtime_slots.grant_read_write_data(sm_role)

    return sfn.StateMachine(
        stack,
        "DeploymentStateMachine",
        state_machine_name=f"{cfg.project}-{cfg.env}-deployment",
        definition_body=sfn.DefinitionBody.from_chainable(validate),
        role=sm_role,
        timeout=Duration.minutes(30),
        tracing_enabled=True,
        logs=sfn.LogOptions(
            destination=logs.LogGroup(
                stack,
                "StateMachineLogGroup",
                log_group_name=f"/stepfunctions/{cfg.project}-{cfg.env}/deployment",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            level=sfn.LogLevel.ERROR,
        ),
    )


def _create_step_task(
    stack: cdk.Stack,
    id: str,
    fn: _lambda.Function,
    *,
    timeout_seconds: int,
    result_path: str,
) -> sfn_tasks.LambdaInvoke:
    """Create a Step Functions LambdaInvoke task with payload passthrough."""
    return sfn_tasks.LambdaInvoke(
        stack,
        id,
        lambda_function=fn,
        payload_response_only=True,
        result_path=result_path,
        task_timeout=sfn.Timeout.duration(Duration.seconds(timeout_seconds)),
    )


def _retry_kwargs() -> dict:
    """Return retry configuration kwargs for add_retry().

    Bug 134 (root cause): previously this retried ``States.TaskFailed`` —
    a WILDCARD that matches ANY application error (incl. a deterministic
    Cedar-validation RuntimeError from the policy step). When a step raised
    on attempt 1 but a later attempt happened to succeed (e.g. the gateway
    tool manifest finished syncing between attempts), Step Functions took the
    SUCCESS path and the Catch (which only fires after retries are exhausted)
    never ran — so a broken Cedar policy shipped as "succeeded". We now retry
    ONLY genuinely-transient infra errors (Lambda service/throttle/timeout),
    NOT the catch-all TaskFailed. A deterministic handler error now goes
    straight to Catch(States.ALL) -> StatusUpdateFailure -> DeploymentFailed.
    """
    return {
        "errors": [
            "States.Timeout",
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.ClientExecutionTimeoutException",
            "Lambda.TooManyRequestsException",
        ],
        "interval": Duration.seconds(2),
        "max_attempts": 3,
        "backoff_rate": 2.0,
    }


def _catch_kwargs(handler: sfn_tasks.LambdaInvoke) -> dict:
    """Return catch configuration kwargs for add_catch()."""
    return {
        "handler": handler,
        "result_path": "$.error_info",
    }
