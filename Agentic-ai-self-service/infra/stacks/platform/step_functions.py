"""Step Functions state machine for deployment orchestration."""

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks

from .config import DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES, PlatformConfig
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
    # 150s, in lockstep with the status_update Lambda's own 120s timeout (see
    # step_lambdas.py, where the reasoning for raising it from 15s lives). This is the
    # task that runs the auto-cleanup teardown, so it is the one that most needs the
    # room. Deliberately LONGER than the Lambda's timeout rather than equal to it: at
    # equal values the Step Functions task can give up while the Lambda is still running
    # and still deleting, which surfaces as States.Timeout and — because this task has
    # add_retry — starts the whole destructive pass over on top of the one in flight.
    failure_handler = _create_step_task(
        stack,
        "StatusUpdateFailure",
        step_lambdas["status_update"],
        timeout_seconds=150,
        result_path="$.failure_result",
    )
    failure_handler.add_retry(**_finalizer_retry_kwargs())
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
        # The task clock starts when Step Functions submits the invocation,
        # before the Lambda execution clock. Equality is therefore not a margin.
        timeout_seconds=60,
        result_path="$",
    )
    validate.add_retry(**_retry_kwargs())
    # NOTE: validate's add_catch is deliberately NOT here with the others. It must route
    # through the no-resources marker, which is defined with the validation gate further
    # down. Wiring it to failure_handler directly -- as every other step correctly does --
    # would make a thrown validate error persist delete_retained, claiming an unproven
    # orphan for the one task that runs before anything can be created. See the gate below.

    guardrails = _create_step_task(
        stack,
        "CreateGuardrails",
        step_lambdas["guardrails"],
        timeout_seconds=150,
        result_path="$",
    )
    guardrails.add_retry(**_retry_kwargs())
    guardrails.add_catch(**_catch_kwargs(failure_handler))

    mcp_server = _create_step_task(
        stack,
        "DeployMCPServer",
        step_lambdas["mcp_server"],
        # The Lambda itself is capped at 600s. Keep a positive orchestration
        # margin so Step Functions cannot time out and retry while the first
        # resource-creating invocation is still alive.
        timeout_seconds=660,
        result_path="$",
    )
    mcp_server.add_retry(**_retry_kwargs())
    mcp_server.add_catch(**_catch_kwargs(failure_handler))

    codegen = _create_step_task(
        stack,
        "GenerateCode",
        step_lambdas["codegen"],
        timeout_seconds=120,
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
        # tool action names. Kept strictly above the Lambda's 720s (Bug 56), not
        # equal: the retry carries the same claim token, so an invocation Step
        # Functions abandoned mid-create would share the gateway-name lease with its
        # own retry and both could create (test_a_step_task_outlives_its_lambda).
        timeout_seconds=780,
        result_path="$",
    )
    gateway.add_retry(**_retry_kwargs())
    gateway.add_catch(**_catch_kwargs(failure_handler))

    knowledge_base = _create_step_task(
        stack,
        "CreateKnowledgeBase",
        step_lambdas["knowledge_base"],
        # Keep failure cleanup outside the resource-creating invocation.
        timeout_seconds=660,
        result_path="$",
    )
    knowledge_base.add_retry(**_retry_kwargs())
    knowledge_base.add_catch(**_catch_kwargs(failure_handler))

    memory_step = _create_step_task(
        stack,
        "CreateMemory",
        step_lambdas["memory"],
        # The same Lambda now performs one bounded create/journal operation and
        # returns without waiting for ACTIVE. Keep the task outside the function's
        # 420s cap so an IAM-propagation retry cannot leave an invocation running
        # after Step Functions has started another mutating attempt.
        timeout_seconds=450,
        result_path="$",
    )
    memory_step.add_retry(**_retry_kwargs())
    memory_step.add_catch(**_catch_kwargs(failure_handler))

    memory_readiness_check = _create_step_task(
        stack,
        "CheckMemoryReady",
        step_lambdas["memory"],
        # Read-only ownership + status observation. The function itself retains
        # the larger create budget, so preserve the same outer ordering while
        # keeping readiness retries safe and side-effect free.
        timeout_seconds=450,
        result_path="$",
    )
    memory_readiness_check.add_retry(**_retry_kwargs())
    memory_readiness_check.add_catch(**_catch_kwargs(failure_handler))

    policy_step = _create_step_task(
        stack,
        "CreatePolicy",
        step_lambdas["policy"],
        # Bug 177 + Cedar IGNORE_ALL_FINDINGS convergence: outside the 600s
        # Lambda budget — the engine CREATING->ACTIVE + up to 12 policy-create
        # retries (as the engine<->gateway authorization converges) can take
        # several minutes on a freshly-created gateway.
        timeout_seconds=660,
        result_path="$",
    )
    policy_step.add_retry(**_retry_kwargs())
    policy_step.add_catch(**_catch_kwargs(failure_handler))

    runtime_configure = _create_step_task(
        stack,
        "ConfigureRuntime",
        step_lambdas["runtime_configure"],
        # Outlive the underlying Lambda timeout (240s — bumped for Bug 54).
        # The IAM-propagation retry loop inside `create_agent_runtime` can
        # legitimately spend up to 75s waiting for AgentCore's IAM cache.
        # The outer margin prevents cleanup from overlapping an invocation
        # still creating or journaling the runtime.
        timeout_seconds=300,
        result_path="$",
    )
    runtime_configure.add_retry(**_retry_kwargs())
    runtime_configure.add_catch(**_catch_kwargs(failure_handler))

    runtime_launch = _create_step_task(
        stack,
        "LaunchRuntime",
        step_lambdas["runtime_launch"],
        timeout_seconds=660,
        result_path="$",
    )
    runtime_launch.add_retry(**_retry_kwargs())
    runtime_launch.add_catch(**_catch_kwargs(failure_handler))

    # Phase B — AgentCore Harness deploy task (parallel to the codegen →
    # iam → configure → launch Runtime path). The task outlives the 300s
    # Lambda budget so cleanup cannot overlap a live creator.
    harness_step = _create_step_task(
        stack,
        "DeployHarness",
        step_lambdas["harness"],
        timeout_seconds=360,
        result_path="$",
    )
    harness_step.add_retry(**_retry_kwargs())
    harness_step.add_catch(**_catch_kwargs(failure_handler))

    evaluation_step = _create_step_task(
        stack,
        "CreateEvaluation",
        step_lambdas["evaluation"],
        timeout_seconds=150,
        result_path="$",
    )
    evaluation_step.add_retry(**_retry_kwargs())
    evaluation_step.add_catch(**_catch_kwargs(failure_handler))

    auth = _create_step_task(
        stack,
        "ConfigureJWTAuth",
        step_lambdas["auth"],
        timeout_seconds=90,
        result_path="$",
    )
    auth.add_retry(**_retry_kwargs())
    auth.add_catch(**_catch_kwargs(failure_handler))

    # 150s to match StatusUpdateFailure above — same Lambda, same 120s function timeout,
    # so the task timeout has to clear it on both paths. The success path does not run the
    # cleanup, but a task timeout shorter than the function's would still abandon an
    # in-flight invocation and retry it.
    status_update = _create_step_task(
        stack,
        "UpdateStatusSuccess",
        step_lambdas["status_update"],
        timeout_seconds=150,
        result_path="$",
    )
    status_update.add_retry(**_finalizer_retry_kwargs())
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
    wait_for_memory = sfn.Wait(
        stack,
        "WaitForMemoryReady",
        time=sfn.WaitTime.duration(Duration.seconds(10)),
    )
    memory_is_ready = sfn.Condition.and_(
        sfn.Condition.is_present("$.memory_result.ready"),
        sfn.Condition.is_boolean("$.memory_result.ready"),
        sfn.Condition.boolean_equals("$.memory_result.ready", True),
    )
    memory_ready_choice = sfn.Choice(stack, "IsMemoryReady?")

    # validate → THE VALIDATION GATE → guardrails choice
    #
    # F-55. Until this Choice existed, ValidateWorkflow's verdict was computed and then
    # discarded: `grep -rn "is_valid" infra/stacks/` returned ZERO, and `validate.next(...)`
    # went straight to the guardrails choice. Proven live on acfe2e-p0920 by an execution
    # that SUCCEEDED while emitting
    #
    #   is_valid = False, errors = ["Workflow 'd47f6a7b85' not found"]  # pragma: allowlist secret
    #
    # and then ran DeployMCPServer, GenerateCode, CreateIAMRole, ConfigureRuntime,
    # LaunchRuntime and UpdateStatusSuccess anyway. A green deployment was not evidence the
    # input was valid; it was not even evidence the input existed.
    #
    # The `add_catch` on validate (see above) is not this gate and never could be: it catches
    # a thrown exception, and the handler converts every failure -- including its own
    # catch-all -- into a SUCCESSFUL Lambda return carrying is_valid=False. The Catch is
    # unreachable by design, which is exactly what made fail-open look safe on a skim.
    #
    # ORDERING NOTE, and it is part of the finding rather than an implementation detail:
    # this gate must not be added before the validator actually receives the deployment
    # payload. Closing it alone, while the handler still looked up a workflow id in a table
    # that never receives the canvas, would have turned a 100%-fail-open gate into a
    # 100%-outage gate. The handler change lands with this one.
    #
    # Fails closed on MISSING as well as on false. `boolean_equals` alone would raise
    # States.Runtime if an older Lambda version returned no `is_valid` at all, and a
    # States.Runtime here routes to the catch rather than to this branch -- so the three
    # conditions are ANDed to make absence and a wrong TYPE both take the invalid path
    # deliberately instead of by accident.
    workflow_is_valid = sfn.Condition.and_(
        sfn.Condition.is_present("$.is_valid"),
        sfn.Condition.is_boolean("$.is_valid"),
        sfn.Condition.boolean_equals("$.is_valid", True),
    )

    # The invalid branch MUST inject an error before StatusUpdateFailure. status_update_step
    # reads `event["error"]` and then falls back to `error_info.Cause`; it never reads
    # `is_valid` or `errors`. Routing straight to the failure handler would therefore mark
    # the deployment failed with an empty reason -- failed for the wrong reason, which is
    # worse than a clear refusal because it sends the operator looking at the wrong step.
    #
    # This Pass writes only the FALLBACK. The validate handler sets a specific `error`
    # summarizing the actual validation errors, and `error` wins the precedence above, so a
    # normal refusal carries its real reasons and this static Cause only surfaces when the
    # handler produced no `error` at all (the missing-or-wrong-type case above).
    invalid_input = sfn.Pass(
        stack,
        "DeploymentInputInvalid",
        parameters={
            "Error": "DeploymentInputInvalid",
            "Cause": (
                "ValidateWorkflow rejected the deployment payload, or returned no usable "
                "is_valid verdict. No resource-creating step ran. See the deployment "
                "record's error_details for the specific validation errors."
            ),
        },
        result_path="$.error_info",
    )

    # ...and it must ALSO say that nothing was created, or the fix trades fail-open for a
    # false orphan report. StatusUpdateFailure runs `_auto_cleanup_on_failure`, which on an
    # empty manifest records delete_status=delete_retained with "could not prove that the
    # empty manifest represented a deployment that created no resources"
    # (status_update_step.py:558-573). That message is correct when a deploy died at an
    # unknown point, and wrong here: ValidateWorkflow is the FIRST task in the machine, so on
    # this branch the empty manifest is not an unproven absence, it is a certainty. Sending
    # the operator to hunt for orphans that provably cannot exist is the same class of defect
    # as F-56's delete_failed on a resource that no longer existed.
    #
    # Written by the state machine rather than only by the handler because this branch is
    # also the one taken when `is_valid` is absent entirely -- in which case the handler that
    # would have set it is precisely the thing that did not run as expected.
    no_resources_created = sfn.Pass(
        stack,
        "NoResourcesCreatedOnInvalidInput",
        result_path="$.no_resources_created",
        parameters={"proven": True, "reason": "rejected at ValidateWorkflow, before any resource-creating task"},
    )
    invalid_input.next(no_resources_created)
    no_resources_created.next(failure_handler)

    # The THROWN path needs the same marker, for the same reason. A Lambda service error,
    # timeout or permission failure in ValidateWorkflow is not a verdict, but it is equally
    # proof that no resource was created -- validate is the first task in the machine. Sending
    # it straight to failure_handler (which is what every other step correctly does, because
    # for them the manifest genuinely is unproven) would persist delete_retained and claim an
    # orphan that cannot exist.
    #
    # `_catch_kwargs` supplies result_path="$.error_info", so the thrown error's Error/Cause is
    # preserved and lands in the same field status_update already reads. The marker Pass is
    # already chained to failure_handler above, so no second .next() is needed here -- and CDK
    # would reject one.
    validate.add_catch(**_catch_kwargs(no_resources_created))

    guardrails_choice = sfn.Choice(stack, "HasGuardrails?").when(has_guardrails, guardrails).otherwise(skip_guardrails)
    validate.next(
        sfn.Choice(stack, "IsDeploymentInputValid?").when(workflow_is_valid, guardrails_choice).otherwise(invalid_input)
    )
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
    # CreateMemory journals the id and returns promptly. Every enabled memory
    # then gets at least one 10-second settle interval; transitional resources
    # loop through read-only checks until two ACTIVE observations prove both
    # control-plane readiness and a data-plane settle margin. DELETING, FAILED,
    # unknown states, or an exhausted check budget raise into the normal failure
    # cleanup path rather than being adopted as ready.
    memory_step.next(memory_ready_choice)
    memory_ready_choice.when(memory_is_ready, skip_memory).otherwise(wait_for_memory)
    wait_for_memory.next(memory_readiness_check)
    memory_readiness_check.next(memory_ready_choice)

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
        # F-82: shared with the deploy API, which derives how long a "pending" AgentVersion row
        # may hold a friendly name from this exact ceiling. See config.py.
        timeout=Duration.minutes(DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES),
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


def _finalizer_retry_kwargs() -> dict:
    """Serialize finalizer attempts instead of overlapping their side effects.

    The status Lambda's exclusive lease lasts 300 seconds and outlives its
    120-second function timeout. A replacement invocation that sees the live
    lease raises ``FinalizerLeaseBusy``. With waits of 60s, 120s, and 240s, even
    an immediate duplicate reaches a retry after the bounded lease has expired;
    a retry following a Lambda timeout reaches that point sooner.
    """
    return {
        "errors": ["FinalizerLeaseBusy"],
        "interval": Duration.seconds(60),
        "max_attempts": 3,
        "backoff_rate": 2.0,
    }


def _catch_kwargs(handler: sfn_tasks.LambdaInvoke) -> dict:
    """Return catch configuration kwargs for add_catch()."""
    return {
        "handler": handler,
        "result_path": "$.error_info",
    }
