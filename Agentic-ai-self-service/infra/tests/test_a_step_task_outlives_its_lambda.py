"""A Step Functions task must never give up while its Lambda is still working.

Every step in the deployment state machine is a ``LambdaInvoke`` with two independent
timeouts: the Lambda's own function timeout, and the task's ``TimeoutSeconds``. They are
set in two different files (``step_lambdas.py`` and ``step_functions.py``) and nothing
connected them, so raising one without the other was a silent, single-line mistake.

What goes wrong when the task timeout is the shorter of the two:

  1. Step Functions abandons the task and raises ``States.Timeout`` while the Lambda is
     still running — and still making AWS calls.
  2. ``States.Timeout`` is in ``_retry_kwargs()``, so the task is retried up to 3 times.
  3. The handler therefore runs CONCURRENTLY with the invocation that was abandoned.

For most steps that is wasteful. For ``StatusUpdateFailure`` it is destructive: that task
runs the failure-path teardown, which walks the deployment manifest issuing deletes. Two
overlapping passes mean a second ``delete_*`` lands on a resource the first pass already
has in flight — which is not a "already gone" error and gets reported as a cleanup failure
on a teardown that was actually working.

The invariant is therefore universal: every task must outlive its Lambda by a real
positive margin. Equality still permits the race because the two clocks do not start at
the same instant — Step Functions starts counting when it submits the invocation, while
the Lambda clock begins when the function starts executing. A cold start or delayed
terminal response can therefore make an equal task deadline fire first. That is especially
dangerous for resource-creating steps: their Catch immediately starts
``StatusUpdateFailure``, whose cleanup can snapshot the manifest while the abandoned
creator is still making or journaling resources.

This reads both numbers off the SYNTHESIZED template rather than the construct objects, so
a refactor that stops wiring ``task_timeout`` through cannot make it pass.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"

# Thirty seconds is the smallest margin already used by the correctly ordered
# pairs. It covers invoke/cold-start/terminal-response skew without changing
# Lambda execution cost: TimeoutSeconds is only an outer ceiling.
MIN_TASK_MARGIN_SECONDS = 30
FINALIZER_LEASE_SECONDS = 300
FINALIZER_TASKS = {"StatusUpdateFailure", "UpdateStatusSuccess"}

EXPECTED_TASKS = {
    "StatusUpdateFailure",
    "ValidateWorkflow",
    "CreateGuardrails",
    "DeployMCPServer",
    "GenerateCode",
    "CreateIAMRole",
    "DeployGateway",
    "CreateKnowledgeBase",
    "CreateMemory",
    "CheckMemoryReady",
    "CreatePolicy",
    "ConfigureRuntime",
    "LaunchRuntime",
    "DeployHarness",
    "CreateEvaluation",
    "ConfigureJWTAuth",
    "UpdateStatusSuccess",
}


@pytest.fixture(scope="module")
def resources() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()["Resources"]


def _definition_and_tokens(resources: dict) -> tuple[dict, dict]:
    """The state machine definition as real JSON, plus a map of placeholder -> intrinsic.

    ``DefinitionString`` is an ``Fn::Join`` of literal JSON fragments interleaved with
    ``Fn::GetAtt`` intrinsics for the Lambda ARNs, so it is not parseable as-is. Each
    intrinsic is replaced by a unique placeholder string first; the result is valid JSON
    and every task's ``Resource`` becomes a placeholder that maps back to a logical id.
    """
    machines = [r for r in resources.values() if r["Type"] == "AWS::StepFunctions::StateMachine"]
    assert len(machines) == 1, f"expected exactly one state machine, found {len(machines)}"

    definition = machines[0]["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return json.loads(definition), {}

    separator, parts = definition["Fn::Join"]
    rendered: list[str] = []
    tokens: dict[str, dict] = {}
    for i, part in enumerate(parts):
        if isinstance(part, str):
            rendered.append(part)
        else:
            token = f"__TOKEN_{i}__"
            tokens[token] = part
            rendered.append(token)
    return json.loads(separator.join(rendered)), tokens


def _lambda_logical_id(intrinsic: dict) -> str | None:
    """The logical id a ``Fn::GetAtt``/``Ref`` intrinsic points at."""
    if "Fn::GetAtt" in intrinsic:
        target = intrinsic["Fn::GetAtt"]
        return target[0] if isinstance(target, list) and target else None
    if "Ref" in intrinsic:
        return intrinsic["Ref"]
    return None


@pytest.fixture(scope="module")
def task_timeouts(resources) -> dict[str, tuple[int, int]]:
    """``state name -> (task TimeoutSeconds, the invoked Lambda's function Timeout)``.

    Only states where BOTH numbers were resolvable are returned; the test below asserts the
    result is non-empty and covers the destructive task, so a parsing change that stops
    resolving anything fails loudly instead of vacuously passing.
    """
    definition, tokens = _definition_and_tokens(resources)
    pairs: dict[str, tuple[int, int]] = {}

    for name, state in (definition.get("States") or {}).items():
        if state.get("Type") != "Task":
            continue
        task_timeout = state.get("TimeoutSeconds")
        if not isinstance(task_timeout, int):
            continue

        # With payload_response_only the function ARN is the Resource itself; the
        # non-optimized form puts it in Parameters.FunctionName. Accept either.
        candidates = [state.get("Resource"), (state.get("Parameters") or {}).get("FunctionName")]
        logical_id = None
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            for token, intrinsic in tokens.items():
                if token in candidate:
                    logical_id = _lambda_logical_id(intrinsic)
                    break
            if logical_id:
                break

        if not logical_id:
            continue
        target = resources.get(logical_id) or {}
        if target.get("Type") != "AWS::Lambda::Function":
            continue
        fn_timeout = (target.get("Properties") or {}).get("Timeout")
        if isinstance(fn_timeout, int):
            pairs[name] = (task_timeout, fn_timeout)

    return pairs


def test_the_pairs_were_actually_resolved(task_timeouts):
    """A guard on the test itself.

    Every assertion below iterates ``task_timeouts``. If the definition parsing breaks --
    CDK changes how it renders the integration, or a refactor drops ``task_timeout`` -- the
    loops iterate nothing and every other test in this file passes while measuring nothing.
    """
    assert len(task_timeouts) >= 10, (
        f"resolved only {len(task_timeouts)} task/Lambda timeout pairs, expected one per step "
        f"({sorted(task_timeouts)}) — the definition parsing is broken, not the timeouts"
    )
    missing = EXPECTED_TASKS - set(task_timeouts)
    assert not missing, (
        f"{sorted(missing)} not resolved, so the deadline check below is inert for those tasks: {sorted(task_timeouts)}"
    )


def test_every_task_has_a_real_margin_beyond_its_lambda(task_timeouts):
    """No Catch or retry may begin while the invocation it replaces can still run."""
    insufficient = {
        name: (task, fn, task - fn) for name, (task, fn) in task_timeouts.items() if task - fn < MIN_TASK_MARGIN_SECONDS
    }
    assert not insufficient, (
        "these Step Functions tasks do not outlive their Lambda by the required "
        f"{MIN_TASK_MARGIN_SECONDS}s margin: {insufficient} "
        "(each entry is state -> (task TimeoutSeconds, Lambda Timeout, margin)). "
        "Without the margin, a Catch/retry can overlap a still-live invocation."
    )


def test_status_tasks_wait_out_a_busy_finalizer_lease(resources):
    """A replacement status invocation cannot overlap the owner it replaces."""
    definition, _ = _definition_and_tokens(resources)

    for name in FINALIZER_TASKS:
        retries = definition["States"][name].get("Retry") or []
        matching = [retry for retry in retries if retry.get("ErrorEquals") == ["FinalizerLeaseBusy"]]
        assert len(matching) == 1, f"{name} needs exactly one dedicated FinalizerLeaseBusy retrier; found {matching}"
        retry = matching[0]
        interval = retry.get("IntervalSeconds")
        attempts = retry.get("MaxAttempts")
        backoff = retry.get("BackoffRate")
        assert (interval, attempts, backoff) == (60, 3, 2), (
            f"{name} changed its finalizer serialization budget: {retry}"
        )

        cumulative_wait = sum(interval * (backoff**attempt) for attempt in range(attempts))
        assert cumulative_wait > FINALIZER_LEASE_SECONDS, (
            f"{name} waits only {cumulative_wait}s across its dedicated retries, "
            f"which cannot outlive the {FINALIZER_LEASE_SECONDS}s finalizer lease"
        )
