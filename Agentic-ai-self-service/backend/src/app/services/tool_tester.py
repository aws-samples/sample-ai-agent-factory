"""Tool Tester service — deploys a temporary Lambda, invokes with test cases, validates, cleans up.

Tests generated Lambda code by deploying it to a real AWS Lambda environment,
running each test case, and validating the responses. This catches real runtime
issues (import errors, missing modules, timeout) that local sandbox testing misses.

Reuses Lambda/IAM helper functions from gateway_deployer to avoid duplication.
"""

import ast
import hashlib
import json
import logging
import os
import time
import uuid

import boto3

from app.services.aws_pagination import list_all
from app.services.gateway_deployer import (
    _create_iam_client,
    _create_lambda_client,
    _create_lambda_zip,
)
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.resource_ownership import assert_this_deployment_may_mutate, owner_tag_list, stack_id

logger = logging.getLogger(__name__)

# Kept for compatibility with anything that imported these names. The shared
# account-global role is NO LONGER used by this module -- see
# _ensure_sandbox_role for why a deployment-scoped role replaced it.
TOOL_TEST_ROLE_NAME = "AgentCoreToolTestRole"
TOOL_TEST_ROLE_DESC = "Shared IAM role for AI Tool Generator test Lambdas"
TOOL_TEST_FN_PREFIX = "AgentCore-ToolTest-"

SANDBOX_ROLE_PREFIX = "AgentCore-ToolSandbox-"
BASIC_EXECUTION_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
VPC_ACCESS_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

# Cap on how much of the account's concurrency untrusted code may consume. A
# generated tool that busy-loops or fans out otherwise competes with the
# platform's own Lambdas for the account pool, which is a cross-tenant
# availability problem, not just a cost one.
DEFAULT_SANDBOX_CONCURRENCY = 2

#: Retention for the sandbox function's log group, in days.
#:
#: Lambda creates ``/aws/lambda/<function>`` implicitly on the first invocation,
#: and a log group created that way has NO retention policy -- CloudWatch's
#: default is to keep it forever. Because the sandbox function is named with a
#: fresh uuid4 per tool test, that is one immortal log group per test, and
#: deleting the function does not delete the group.
#:
#: MEASURED on acfe2e-p0920 on 2026-09-21: **53** ``/aws/lambda/AgentCore-ToolTest-*``
#: groups, ``retentionInDays: null`` on every one of the 53. So this is not a
#: hypothetical -- it is the observed steady state of the feature.
#:
#: ARCC guidance ``cnt_qf7wYkuSRSM5fl`` (retention is a property of the log group and
#: the CloudWatch default is to retain indefinitely) and ``cnt_0qPEBTUot0pRyh`` ("you
#: can not accidentally store data forever"; enforce retention by an automated
#: mechanism rather than a manual sweep). A sandbox log group holds whatever the
#: tool under test printed, which can include data the tool was handed, so an
#: unbounded group is a retention violation and not merely untidy.
#:
#: The fix has to be pre-creation, not a sweep. Deleting the group while the
#: function still exists does not work: Lambda recreates it, with the whole
#: stream in it (see the lesson recorded for the 15/15 stack teardowns). Creating
#: it first, with retention, is the only ordering where Lambda never gets to make
#: an unmanaged one.
#:
#: Seven days rather than one: a tool test that failed is worth being able to look
#: at tomorrow, and 35 KB across 53 groups says the volume is irrelevant either way.
#: 7 is one of CloudWatch's accepted values; an arbitrary integer is rejected.
SANDBOX_LOG_RETENTION_DAYS = 7

#: How long to wait for the temporary function to reach Active, in seconds.
#:
#: These two numbers are measured, not chosen. A function with no VpcConfig is
#: Active in a couple of seconds. A function *with* one waits on Lambda creating
#: the Hyperplane ENI for its (subnet, security-group) pair, and three consecutive
#: live measurements in the acfe2e-p0920 sandbox VPC gave **223.3s, 223.9s and
#: 6.1s** -- the third being the one that found the mapping already warm from the
#: second. So the cold cost is real, recurs whenever the mapping goes idle, and is
#: nearly four times the 60s this code used to allow.
#:
#: That 60s was not a near miss. Nothing in the chain was budgeted for a VPC
#: function: the host Lambda's own timeout was 120s and the browser gave up polling
#: at 120s, so an isolated sandbox could not have succeeded on a cold mapping at
#: any layer. All three were raised together; see infra/stacks/platform/lambdas.py
#: (DeploymentLambda timeout) and frontend/src/services/api/tools.ts (maxAttempts).
ACTIVE_WAIT_SECONDS = 60
ACTIVE_WAIT_SECONDS_VPC = 300

#: Markers for the one CreateFunction rejection that means "IAM has not caught up
#: yet" rather than "this grant is missing". Matched case-insensitively against the
#: error message.
#:
#: No entry may contain another: a longer form of a marker already here would never
#: be consulted. The first covers the ENI case seen live
#: ("...does not have permissions to call CreateNetworkInterface on EC2"); the
#: second covers a role whose *trust* policy has not propagated, which is the same
#: class of failure one step earlier.
_ROLE_PROPAGATION_MARKERS = (
    "the provided execution role does not have permissions",
    "cannot be assumed by lambda",
)

#: Bounded retry budget for that rejection. 6 attempts, 5s apart, so ~25s of
#: patience -- comfortably more than IAM has ever needed here, and it fails with
#: the original error rather than a synthesized one when it is genuinely a
#: missing grant.
_ROLE_RETRY_ATTEMPTS = 6
_ROLE_RETRY_SLEEP_SECONDS = 5


class SandboxPolicyError(Exception):
    """The sandbox cannot be brought up to the posture this deployment requires.

    Separate from every other failure because its message is deliberately
    published to the caller: it tells an operator which control could not be
    applied and what to set. Every other exception on this path is genericized
    before it leaves the service (ARCC cnt_94E30Xo4RZHtSJ), because a boto3 error
    here names account ids, role ARNs and the platform's own internals.
    """


def _isolation_required() -> bool:
    """True when this deployment refuses to run tool code without a VPC.

    **Default ON.** The first version of this defaulted off, on the argument that a
    sandbox with no egress cannot test a tool that calls an HTTP API (and
    tool_generator's contract is "stdlib + urllib + boto3" precisely so tools can
    call APIs), so enforcing isolation without provisioning endpoints turns
    "untested" into "always fails". That argument is about convenience and it loses:
    ARCC cnt_MSVB0Kk8WMwmmW requires customer-provided code to run "from a private
    network ... with no public internet access", full stop, and a control that is off
    unless an operator finds the switch is not a control. The platform stack now
    provisions the isolated VPC itself (infra/stacks/platform/tool_sandbox_net.py),
    so the default costs nothing on a stack deployed from this repo.

    What the HTTP trade-off actually became: a tool that calls a remote API fails
    *during testing* with a connection error, and works in production, because the
    deployed agent runtime has normal egress. Reporting that honestly beats executing
    model-written code with an open path to the internet.

    Set ``TOOL_SANDBOX_REQUIRE_ISOLATION`` to a falsy spelling (``0``/``false``/
    ``no``/``off``) to opt out -- for a deployment whose Lambda cannot be placed in a
    VPC at all. An unset variable is *not* an opt-out; only an explicit one is, so a
    deployment that simply never heard of this setting still fails closed.
    """
    raw = (os.environ.get("TOOL_SANDBOX_REQUIRE_ISOLATION") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _sandbox_vpc_config() -> dict | None:
    """VpcConfig for the sandbox, or None when this deployment has no VPC.

    The real boundary for untrusted code is the network, not the AST check:
    ARCC cnt_MSVB0Kk8WMwmmW requires it to run "from a private network ...
    with no public internet access". These are read from the environment so the
    platform stack can provision subnets with no NAT and hand them over --
    ``build_tool_sandbox_network`` in infra/stacks/platform/tool_sandbox_net.py sets
    both variables, mirroring the LambdaSubnetIds/LambdaSecurityGroupIds shape the
    exported customer template already uses.

    Returning None is not "no isolation configured, carry on": with the default
    posture ``_isolation_required()`` turns it into a refusal at the caller.
    """
    subnets = [s.strip() for s in (os.environ.get("TOOL_SANDBOX_SUBNET_IDS") or "").split(",") if s.strip()]
    groups = [g.strip() for g in (os.environ.get("TOOL_SANDBOX_SECURITY_GROUP_IDS") or "").split(",") if g.strip()]
    if not subnets:
        return None
    if not groups:
        # Subnets without a security group would fall back to the VPC default
        # group, which commonly allows all egress -- the opposite of the point.
        raise SandboxPolicyError(
            "TOOL_SANDBOX_SUBNET_IDS is set but TOOL_SANDBOX_SECURITY_GROUP_IDS is empty; "
            "refusing to place untrusted tool code in a VPC under the default security group"
        )
    return {"SubnetIds": subnets, "SecurityGroupIds": groups}


def _sandbox_concurrency() -> int:
    """Reserved concurrency for a sandbox function, from the environment."""
    raw = os.environ.get("TOOL_SANDBOX_RESERVED_CONCURRENCY")
    try:
        value = int(raw) if raw else DEFAULT_SANDBOX_CONCURRENCY
    except ValueError:
        logger.warning("Ignoring non-numeric TOOL_SANDBOX_RESERVED_CONCURRENCY=%r", raw)
        return DEFAULT_SANDBOX_CONCURRENCY
    # 0 would make the function unable to run at all, which looks like a broken
    # feature rather than a policy; treat it as "use the default".
    return value if value > 0 else DEFAULT_SANDBOX_CONCURRENCY


def _sandbox_role_name(region: str | None = None) -> str:
    """Deployment-scoped role name, <=64 chars.

    Scoped rather than account-global because this role backs *untrusted code*.
    Two deployments sharing one role means tenant B's tool runs with whatever
    tenant A's deployment granted, and an identically-named pre-existing role
    means it runs with grants nobody in this system chose.
    """
    name = f"{SANDBOX_ROLE_PREFIX}{stack_id(region)}"
    if len(name) <= 64:
        return name
    digest = hashlib.sha256(stack_id(region).encode()).hexdigest()[:8]
    return f"{name[: 64 - 9]}-{digest}"


# ---------------------------------------------------------------------------
# What this AST check is, and what it is NOT.
#
# It is defence in depth. It is NOT the security boundary, and no comment in
# this repo may claim that it is. ARCC cnt_MSVB0Kk8WMwmmW ("Isolation
# requirements for executing customer-provided code in multi-tenant
# environments") is explicit: "Language restrictions and DSLs ... may not be
# substituted for the above", where "the above" is process/network isolation.
#
# It said so because a name-based denylist does not hold. The original one
# here accepted all five of these, every one of which reaches os or the
# network, because none of them mentions a blocked name:
#
#   getattr(builtins, '__im' + 'port__')('os').environ      # rebuild __import__
#   getattr(tool, '__glo' + 'bals__')['__bui' + 'ltins__']  # no import at all
#   getattr(m, 'sys' + 'tem')                               # rebuild os.system
#   urllib.request.urlopen(...)                             # egress, nothing blocked
#
# Adding "getattr" to the blocked list would not have fixed it either -- the
# next rung is [].__class__.__mro__[1].__subclasses__(). So the checks below
# close the *class* rather than named instances: no dunder attribute may be
# read, no dunder key may be subscripted, and the builtins that turn a string
# into an attribute lookup are refused outright. The real boundary is the
# network and IAM isolation applied in _deploy_temp_lambda.
# ---------------------------------------------------------------------------

# Imports that could allow arbitrary system access or network abuse
BLOCKED_IMPORTS = frozenset(
    {
        "subprocess",
        "shutil",
        "ctypes",
        "multiprocessing",
        "socket",
        "http.server",
        "xmlrpc",
        "ftplib",
        "telnetlib",
        "importlib",
        "code",
        "codeop",
        "pty",
        "pipes",
        "commands",
        "os",
        "sys",
        "pickle",
        "marshal",
        "pathlib",
        "tempfile",
        "threading",
        "asyncio",
        # Reach the interpreter's own object graph, which is how a denylist gets
        # walked around: inspect.getmodule, types.FunctionType, gc.get_objects.
        "builtins",
        "gc",
        "inspect",
        "types",
        "runpy",
        "site",
        "sysconfig",
        "zipimport",
        "signal",
        "fcntl",
        "mmap",
        "resource",
        "platform",
        "webbrowser",
    }
)

# Built-in functions that enable dynamic code execution
BLOCKED_CALLS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "breakpoint",
        # Turn a runtime string into an attribute lookup, which defeats any
        # name-based check above: getattr(m, 'sys' + 'tem').
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "dir",
        # Filesystem and interpreter handles a generated tool has no use for.
        # The contract in tool_generator.GENERATION_PROMPT is "stdlib + urllib
        # + boto3" for calling APIs, not for reading /proc/self/environ.
        "open",
        "input",
        "memoryview",
        "exit",
        "quit",
    }
)

# Dunder attributes a generated tool may legitimately touch. Everything else
# named __x__ is refused: __globals__, __builtins__, __class__, __bases__,
# __mro__, __subclasses__, __code__, __closure__, __dict__ and __import__ are
# each a step on a known escape path, and enumerating the bad ones invites the
# next rung. __name__ and __doc__ are inert and do appear in real code.
ALLOWED_DUNDER_ATTRS = frozenset({"__name__", "__doc__"})


MAX_CODE_SIZE = 50_000  # 50KB max for generated tool code


def _ensure_sandbox_role(iam_client, region: str | None = None, need_vpc: bool = False) -> str:
    """Create or reuse the deployment's tool-sandbox role. Returns its ARN.

    Deliberately NOT _ensure_lambda_role, and the difference is the point.
    _ensure_lambda_role adopts an identically-named pre-existing role on
    purpose, because its callers manage account-global singletons that may
    predate this stack (the live account holds an AgentCoreDynamicToolsLambdaRole
    created months earlier by something else). Adoption is the right call there.

    It is the wrong call here. This role is assumed by code the platform did not
    write and cannot vet, so adopting a role whose grants this deployment did
    not choose hands untrusted code an unknown set of permissions. Per ARCC
    cnt_MSVB0Kk8WMwmmW's least-privilege requirement this fails closed instead,
    which costs nothing in practice because the name is deployment-scoped and so
    a foreign collision means genuine ambiguity.
    """
    role_name = _sandbox_role_name(region)
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    created = False
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Least-privilege role for sandboxed AI-generated tool code",
            # DELIBERATELY owner_tag_list, not governed_tag_list, and the same holds for the
            # sandbox log group and the temp function below. This is the ONE resource family in
            # the product that P0-B governance tags do not reach, decided rather than missed.
            #
            # A tool test is not a deployment. There is no deployment request to carry a tag
            # policy, so honoring one here would mean reading the org's policy defaults inside
            # this function -- and a policy with a REQUIRED key and no default would then make
            # every tool test in that org fail closed, taking out a feature that creates nothing
            # a customer is billed for beyond a few seconds of Lambda. Fail-closed over an
            # incomplete table removes the feature; the cost-attribution benefit does not pay
            # for that. ARCC cnt_6gBImtb08AJqCB is about ABAC on durable resources, and these
            # three are deleted in the `finally` of the same request that made them.
            #
            # The consequence to state plainly: the two ownership tags are the only tags on a
            # sandbox resource, so a cost report filtered by a governance tag will not show
            # tool-test spend. That is visible in the tag list, not hidden. If this ever needs
            # to change, the policy read belongs in `handle_test_tool` (where a 400 is an
            # honest answer) and NOT in this module, and lambdas.py's lambda:TagResource and
            # logs:TagResource key allowlists have to be widened in the same commit -- they
            # are still ForAllValues:StringEquals on exactly ManagedBy + AgentCoreStack.
            Tags=owner_tag_list(region),
            **create_role_kwargs(),
        )
        role_arn = resp["Role"]["Arn"]
        created = True
        logger.info("Created tool-sandbox role: %s", role_arn)
    except iam_client.exceptions.EntityAlreadyExistsException:
        existing = iam_client.get_role(RoleName=role_name)["Role"]
        role_arn = existing["Arn"]
        # The shared helper, not a local tag comparison: this is an adopt-by-name
        # branch like every other one in the app, and it raises with the remedy
        # (rename, or adopt by tagging) instead of a dead end. Attaching managed
        # policies below is a mutation, so the mutation predicate is the right
        # one -- it also accepts the CDK Project/Environment pair, which is this
        # same deployment by another route rather than a wider grant.
        #
        # The region MUST be passed. stack_id() falls back to the ambient
        # AWS_REGION, so a deployment operating on a region other than its own
        # (this platform deploys the same project/env to two) would tag the role
        # with one identity and then check it against another -- rejecting the
        # role it had just created and disabling tool testing outright.
        assert_this_deployment_may_mutate(f"IAM role {role_name}", existing.get("Tags"), region)
        # F-06: retrofit the permissions boundary once ownership is proven, before the
        # managed policies below are attached.
        ensure_role_boundary(iam_client, role_name, role=existing)
        logger.info("Reusing tool-sandbox role: %s", role_arn)

    # Only the two managed policies below, and the VPC one only when a VPC is
    # actually in use: attaching it unconditionally would grant ENI management
    # to code that never needs it.
    wanted = [BASIC_EXECUTION_POLICY] + ([VPC_ACCESS_POLICY] if need_vpc else [])
    pagination_marker = "Marker"
    attached = {
        p["PolicyArn"]
        for p in list_all(
            iam_client,
            "list_attached_role_policies",
            item_keys=("AttachedPolicies",),
            request={"RoleName": role_name},
            request_token=pagination_marker,
            response_token=pagination_marker,
            continuation_flag="IsTruncated",
        )
    }
    newly_attached = False
    for policy_arn in wanted:
        if policy_arn not in attached:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            newly_attached = True
            logger.info("Attached %s to %s", policy_arn, role_name)

    if created or newly_attached:
        # NOT `created` alone, which is what this was, because the grant a reused
        # role is missing is exactly the one the next call depends on.
        #
        # Observed live on acfe2e-p0920: the role already existed from a run made
        # before isolation was enabled, so it had only the basic-execution policy.
        # This attached VPC_ACCESS_POLICY, `created` was False, nothing waited, and
        # CreateFunction failed 0.8s later with "The provided execution role does
        # not have permissions to call CreateNetworkInterface on EC2". Lambda
        # validates the role's EC2 permissions synchronously against an IAM view
        # that did not yet include the attachment.
        #
        # The sleep is the cheap half; _create_function_with_role_retry is the half
        # that does not depend on guessing how long propagation takes.
        time.sleep(10)
    return role_arn


def _is_dunder(name: str) -> bool:
    """True for __x__ style names. Bare "__" is not a dunder and not special."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _validate_code_safety(code: str) -> tuple[bool, str]:
    """AST-validate generated Lambda code before deployment.

    Returns (is_safe, error_message). Checks for syntax errors,
    blocked imports, dangerous function calls, and required entry point.
    """
    if len(code) > MAX_CODE_SIZE:
        return False, f"Code too large: {len(code)} bytes exceeds {MAX_CODE_SIZE} limit"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"

    # Dangerous attribute calls like os.system(), os.popen()
    blocked_attrs = frozenset(
        {
            "system",
            "popen",
            "exec",
            "execl",
            "execle",
            "execlp",
            "execv",
            "execve",
            "execvp",
            "spawn",
            "spawnl",
            "spawnle",
        }
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    return False, f"Blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    return False, f"Blocked import: {node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                return False, f"Blocked function call: {node.func.id}"
            # Check attribute calls like os.system(), os.popen()
            if isinstance(node.func, ast.Attribute) and node.func.attr in blocked_attrs:
                return False, f"Blocked function call: {node.func.attr}"
        elif isinstance(node, ast.Name):
            # A dunder does not have to be reached through an attribute: at
            # module scope __builtins__ and __loader__ are already in scope as
            # bare names, so __builtins__.open(...) and __loader__.load_module
            # sailed past the Attribute rule below (neither "open" nor
            # "load_module" is a dunder). Refusing the name closes all of it,
            # including the concatenated-key form __builtins__['__im'+'port__'],
            # which the Subscript rule cannot see because a BinOp is not a
            # Constant.
            if _is_dunder(node.id) and node.id not in ALLOWED_DUNDER_ATTRS:
                return False, f"Blocked attribute access: {node.id}"
        elif isinstance(node, ast.Attribute):
            # Reading ANY dunder is refused, not a list of known-bad ones.
            # tool.__globals__ is the whole bypass, and it needs no import.
            if _is_dunder(node.attr) and node.attr not in ALLOWED_DUNDER_ATTRS:
                return False, f"Blocked attribute access: {node.attr}"
        elif isinstance(node, ast.Subscript):
            # The dict form of the same move: __globals__['__builtins__'].
            # Only a constant index can be judged here; a computed one cannot
            # reach a dunder without first reading one, which is already refused.
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and _is_dunder(key.value)
                and key.value not in ALLOWED_DUNDER_ATTRS
            ):
                return False, f"Blocked attribute access: {key.value}"

    func_names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if "lambda_handler" not in func_names:
        return False, "Missing required function: lambda_handler"

    return True, ""


#: Substrings that mean "this failure was the network, not the logic". Matched
#: case-insensitively against a test case's error text. Kept short and specific on
#: purpose: a false positive tells the user their working tool has a network problem.
#:
#: Matched against BOTH ``errorMessage`` and ``errorType`` (see
#: ``_scannable_failure_text``), so the second group below is exception class names.
#: Those are the more durable half: ``ReadTimeoutError`` is the same string across
#: botocore releases, and the prose it renders is not.
#:
#: No entry may contain another entry: a longer form of a marker already here would
#: never be consulted, because the shorter one matches first and matches strictly
#: more. ``"connection timed out"`` was in this tuple until a surviving mutant showed
#: that deleting it changed nothing -- ``"timed out"`` already covered every string
#: it could ever match. ``test_no_marker_is_dead_weight`` now holds that line, which
#: is also why the broad ``"connectionerror"`` replaced the three class names that
#: end in it rather than joining them.
_NETWORK_FAILURE_MARKERS = (
    # Message text.
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname",
    "urlopen error",
    "connection refused",
    "connect timeout",
    "read timeout",
    "timed out",
    "network is unreachable",
    # EADDRNOTAVAIL. Never produced by a tool's own logic, and it is what a live run
    # in this sandbox returned *before* the sandbox gained a DNS firewall: the name
    # resolved, and the address it resolved to was unroutable from a subnet with no
    # egress.
    "cannot assign requested address",
    # EBUSY, which replaced EADDRNOTAVAIL above once the sandbox started filtering
    # DNS. Adding the firewall changed the errno this sandbox produces, and because
    # nothing here matched the new one, the note silently stopped being emitted for
    # any tool that called out without urllib wrapping the error. That regression was
    # found by a live probe, not by this suite.
    #
    # MEASURED on the deployed stack, one handler using ``http.client``, two hosts,
    # a firewall-allowed name against a blocked one:
    #
    #   logs.us-east-1.amazonaws.com  -> reached, HTTP 404, 716 ms
    #   api.open-meteo.com            -> OSError(16, 'Device or resource busy'), 297 ms
    #
    # So this is specifically what a blocked name resolution looks like from inside
    # the sandbox, and it is unambiguous here for the same reason EADDRNOTAVAIL is:
    # EBUSY comes from device and mount operations, and ``os``, ``socket``,
    # ``subprocess``, ``multiprocessing`` and ``ctypes`` are all blocked imports, so a
    # tool's own logic has no way to raise it.
    "device or resource busy",
    "could not connect to the endpoint",
    "max retries exceeded",
    # Exception class names, from errorType. Deliberately only unambiguous ones: a
    # marker that also fires on a tool's own bug is worse than a missing marker,
    # because it tells the user to go looking for a network problem that is not there.
    "gaierror",
    "connectionerror",
    "connecttimeouterror",
    "readtimeouterror",
    "maxretryerror",
    "urlerror",
)


def _scannable_failure_text(result: dict) -> str:
    """The text of one failed test case, lowercased, for marker matching.

    Every part of the result, because the network error can be in any of them:

    * ``error`` -- the harness's own one-line summary of the failure.
    * ``errorType`` -- the exception *class* name, present on an unhandled
      exception. The more stable signal of the two: ``EndpointConnectionError`` does
      not change between botocore releases, while the prose it renders has.
      ``_run_test_case`` keeps the whole payload in ``actualOutput`` and copies only
      ``errorMessage`` into ``error``, so a scan of ``error`` alone can never see a
      class name, which made every class-name marker here unreachable. A surviving
      mutant is how that was found: deleting ``"endpointconnectionerror"`` broke
      nothing.
    * **the payload the tool itself returned** -- the case a live browser run found
      and no unit test did. A *well-written* generated tool catches its own
      ``URLError`` and returns ``{"statusCode": 502, "body": ...}``. That is not a
      ``FunctionError``, so there is no ``errorType``, and ``error`` is the harness's
      ``"Expected statusCode 200, got 502"`` -- while the actual cause,
      ``"Network error: <urlopen error [Errno 99] Cannot assign requested
      address>"``, sat in the body and was never read. So no note fired; and because
      the frontend suppresses auto-fix *only* when a note is present, auto-fix then
      asked the model to repair correct code, whose only available repair is to
      delete the network call the user asked for. The better the generated code, the
      worse the outcome: only a tool that let the exception escape was ever
      classified, which is the one shape the tests happened to use.

    ``default=str`` because the body is whatever the tool chose to return, and a
    scan must never be the thing that raises.
    """
    parts = [str(result.get("error") or "")]
    output = result.get("actualOutput")
    if output is not None:
        try:
            parts.append(json.dumps(output, default=str))
        except (TypeError, ValueError):
            parts.append(str(output))
    return " ".join(parts).lower()


def _isolation_note(results: list[dict], *, isolated: bool) -> str | None:
    """Explain a network failure that the sandbox itself caused, or return None.

    The isolated sandbox has no route to the internet, so a tool that calls an HTTP
    API fails *during testing* and works in production, where the agent runtime has
    normal egress. Without this note that is an indistinguishable mystery: the user
    sees a timeout from code that is correct, concludes the tool is broken, and
    rewrites working code. ARCC ``cnt_MSVB0Kk8WMwmmW`` is why the isolation is not
    negotiable; saying so in the result is what keeps it honest rather than
    confusing.

    Returns None when the sandbox is not isolated, or when nothing failed in a way
    that looks like the network -- a note attached to every result would be noise,
    and noise is how a real explanation gets ignored.

    Only *failed* cases are scanned. That matters now that
    ``_scannable_failure_text`` reads the tool's returned payload: a tool that
    succeeds and legitimately returns the words "read timeout" as data would
    otherwise be told the sandbox broke it. Only a failure can be explained by the
    boundary.
    """
    if not isolated:
        return None
    failed = [r for r in results if not r.get("passed")]
    if not any(marker in _scannable_failure_text(r) for r in failed for marker in _NETWORK_FAILURE_MARKERS):
        return None
    return (
        "One or more test cases failed on a network call. The tool-test sandbox runs "
        "with no internet access by design, so outbound HTTP calls cannot succeed "
        "here even when the tool is correct. The deployed agent has normal network "
        "access, so a tool whose only failure is a connection error will work once "
        "deployed. Test the rest of the tool's logic with a case that does not call "
        "out. Any other case that failed in this run is listed with its own error, "
        "and may have failed for a reason unrelated to the network."
    )


def test_tool(lambda_code: str, test_cases: list[dict], region: str = "us-east-1") -> dict:
    """Deploy a temporary Lambda, run test cases, return results, clean up.

    Args:
        lambda_code: Python source code with lambda_handler(event, context).
        test_cases: List of dicts with name, input, expectedOutputKeys, description.
        region: AWS region.

    Returns:
        Dict with keys: success, results (list), allPassed (bool), error (str|None).
    """
    # Step 0: Validate code safety before any AWS calls
    is_safe, safety_error = _validate_code_safety(lambda_code)
    if not is_safe:
        return {
            "success": False,
            "results": [],
            "allPassed": False,
            "error": f"Code safety validation failed: {safety_error}",
        }

    function_name = f"{TOOL_TEST_FN_PREFIX}{uuid.uuid4().hex[:8]}"
    lambda_client = _create_lambda_client(region)
    results = []

    try:
        # Step 1: Ensure this deployment's own least-privilege sandbox role.
        iam_client = _create_iam_client()
        vpc_config = _sandbox_vpc_config()
        if vpc_config is None and _isolation_required():
            # Fail closed, before any AWS resource is created.
            raise SandboxPolicyError(
                "This deployment requires network-isolated tool testing but "
                "TOOL_SANDBOX_SUBNET_IDS is empty, so generated code would run with public "
                "egress. Tool testing is disabled until the sandbox subnets and security "
                "groups are configured. Set TOOL_SANDBOX_REQUIRE_ISOLATION=false to allow "
                "un-isolated execution instead."
            )
        role_arn = _ensure_sandbox_role(iam_client, region=region, need_vpc=vpc_config is not None)

        # Step 1.5: claim the function's log group BEFORE Lambda can create it
        # implicitly with no retention. Ordering is the whole point -- see
        # SANDBOX_LOG_RETENTION_DAYS.
        _ensure_sandbox_log_group(_create_logs_client(region), function_name, region)

        # Step 2: Deploy temporary Lambda
        zip_bytes = _create_lambda_zip(lambda_code)
        _deploy_temp_lambda(lambda_client, function_name, role_arn, zip_bytes, vpc_config=vpc_config, region=region)

        # Step 3: Run each test case
        for tc in test_cases:
            result = _run_test_case(lambda_client, function_name, tc)
            results.append(result)

        all_passed = all(r["passed"] for r in results)
        return {
            "success": True,
            "results": results,
            "allPassed": all_passed,
            "error": None,
            "sandboxIsolated": vpc_config is not None,
            "note": _isolation_note(results, isolated=vpc_config is not None),
        }

    except SandboxPolicyError as exc:
        # Published verbatim: it names the control that could not be applied and
        # the setting that fixes it, and nothing about the account's internals.
        logger.error("Tool testing refused: %s", exc)
        return {
            "success": False,
            "results": results,
            "allPassed": False,
            "error": str(exc),
        }

    except Exception as exc:
        # SECURITY (ARCC cnt_94E30Xo4RZHtSJ): this returned str(exc), and the
        # caller reads this field -- _handle_async_test stores it and the poll
        # route republishes it. A boto3 error here carries account ids and role
        # ARNs (an AccessDenied on iam:AttachRolePolicy names both), so the
        # detail goes to the log and a generic message goes to the caller. A
        # refusal of the submitted code is NOT this path: it returns above with
        # its own actionable "Code safety validation failed: ..." message.
        logger.exception("Tool testing failed: %s", exc)
        return {
            "success": False,
            "results": results,
            "allPassed": False,
            "error": "Tool testing failed unexpectedly. Check the platform logs for details.",
        }

    finally:
        # Step 4: Cleanup — delete temp Lambda (role persists for reuse)
        _cleanup_temp_lambda(lambda_client, function_name)


def _is_role_propagation_error(exc: Exception) -> bool:
    """True when CreateFunction was rejected because IAM has not caught up.

    Distinguishing this from a genuinely missing grant matters in both directions.
    Retrying a real missing grant would turn an instant, readable failure into a
    25-second one with the same outcome; *not* retrying propagation makes the first
    isolated tool test of a deployment fail for reasons the operator cannot act on.
    """
    return any(marker in str(exc).lower() for marker in _ROLE_PROPAGATION_MARKERS)


def _create_function_with_role_retry(lambda_client, kwargs: dict) -> None:
    """CreateFunction, retrying only the IAM-propagation rejection.

    Safe to retry because this rejection is a validation failure: Lambda checks the
    execution role's EC2 permissions before creating anything, so no half-made
    function is left behind. The live failure that motivated this confirmed it --
    the cleanup path's DeleteFunction came back ResourceNotFoundException, meaning
    nothing had been created.
    """
    for attempt in range(_ROLE_RETRY_ATTEMPTS):
        try:
            lambda_client.create_function(**kwargs)
            return
        except Exception as exc:  # noqa: BLE001 -- narrowed by the predicate below
            last = attempt == _ROLE_RETRY_ATTEMPTS - 1
            if last or not _is_role_propagation_error(exc):
                raise
            logger.info(
                "CreateFunction rejected on IAM propagation (attempt %d/%d), retrying in %ds",
                attempt + 1,
                _ROLE_RETRY_ATTEMPTS,
                _ROLE_RETRY_SLEEP_SECONDS,
            )
            time.sleep(_ROLE_RETRY_SLEEP_SECONDS)


def _create_logs_client(region: str | None):
    """A CloudWatch Logs client, factored out so tests can replace it.

    Deliberately NOT imported from gateway_deployer like the lambda and IAM
    clients: no other module needs one, and tool testing is the only path that
    creates a log group it does not own the lifecycle of.
    """
    return boto3.client("logs", region_name=region) if region else boto3.client("logs")


def _ensure_sandbox_log_group(logs_client, function_name: str, region: str | None = None) -> None:
    """Create the sandbox's log group WITH retention, before Lambda creates it without.

    Must be called before ``create_function``. See SANDBOX_LOG_RETENTION_DAYS for
    the measurement that motivated this and for why a sweep cannot work.

    Best-effort by construction. Every failure here is logged and swallowed,
    because the alternative is that a deployment missing one CloudWatch action
    loses tool testing entirely -- which is exactly the outage a missing
    ``lambda:TagResource`` caused live (see infra/tests/test_tool_sandbox_grant.py),
    and the trade is not close: an ungoverned log group costs pennies, a dead
    feature costs the product. The consequence of swallowing is visible in the
    log, and the grant is pinned by a test on the CDK side.

    Tagging is attempted through ``create_log_group``'s own ``tags`` argument and
    retried without it on an authorization failure. That retry is not defensive
    programming for a hypothetical -- it fired. Measured on ``acfe2e-p0920`` on
    2026-09-21, with ``logs:CreateLogGroup`` and ``logs:PutRetentionPolicy``
    granted and nothing else, CloudWatch refused the tagged create and named the
    missing action itself::

        AccessDeniedException ... is not authorized to perform CreateLogGroup with
        Tags. An additional permission "logs:TagResource" is required.

    So ``logs:TagResource`` is a separate authorization even though ``tags`` is an
    argument to ``CreateLogGroup``, the same way Lambda authorizes
    ``create_function``'s ``Tags`` separately. The grant now includes it (see
    ``build_deployment_lambda`` in ``infra/stacks/platform/lambdas.py``). The retry
    stays: without it that AccessDenied would have cost the whole tool test rather
    than a tag, and the observable consequence -- a governed but *unattributable*
    log group -- is only visible by reading the deployed group's tags after a real
    test, never from the test suite.
    """
    log_group = f"/aws/lambda/{function_name}"
    try:
        try:
            logs_client.create_log_group(
                logGroupName=log_group,
                tags={t["Key"]: t["Value"] for t in owner_tag_list(region)},
            )
        except Exception as tag_exc:  # noqa: BLE001
            # ResourceAlreadyExistsException is reachable: a uuid4 collision is not
            # the case, but a retried tool test with the same name is.
            if "ResourceAlreadyExists" in repr(tag_exc):
                raise
            logger.warning("Creating %s with owner tags failed (%s); retrying untagged", log_group, tag_exc)
            logs_client.create_log_group(logGroupName=log_group)
        logs_client.put_retention_policy(logGroupName=log_group, retentionInDays=SANDBOX_LOG_RETENTION_DAYS)
        logger.info("Sandbox log group %s created with %s-day retention", log_group, SANDBOX_LOG_RETENTION_DAYS)
    except Exception as exc:  # noqa: BLE001
        # Includes ResourceAlreadyExistsException, where the group already exists
        # and its retention is whatever it already was. Not worth a second call:
        # the only way to reach it is a name reuse, and the group Lambda would
        # have made is the one this is trying to prevent.
        logger.warning(
            "Could not govern the sandbox log group %s (%s). The tool test continues; the group "
            "will keep whatever retention it has, which for a group Lambda created implicitly is "
            "never expire.",
            log_group,
            exc,
        )


def _deploy_temp_lambda(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
    vpc_config: dict | None = None,
    region: str | None = None,
) -> None:
    """Create a temporary Lambda function and wait for it to become Active.

    This function IS the sandbox, so the isolation lives here rather than in the
    AST check (ARCC cnt_MSVB0Kk8WMwmmW: a language restriction "may not be
    substituted" for isolation):

    * a fresh uuid-named function per invocation, deleted in test_tool's
      finally -- no execution environment is ever reused between callers, which
      is strictly stronger than the per-customer function ARCC asks for;
    * VpcConfig when the deployment provides subnets, so egress is whatever the
      security group allows and nothing more;
    * reserved concurrency, so untrusted code cannot drain the account pool;
    * owner tags, so teardown can tell this function apart from a foreign one.
    """
    kwargs: dict = {
        "FunctionName": function_name,
        "Runtime": "python3.12",
        "Role": role_arn,
        "Handler": "lambda_function.lambda_handler",
        "Code": {"ZipFile": zip_bytes},
        "Description": "Temporary sandboxed test Lambda for AI Tool Generator",
        "Timeout": 10,
        "MemorySize": 128,
        # region, not the ambient default: a function created in eu-central-1 and
        # tagged us-east-1 is invisible to its own region's teardown.
        "Tags": {t["Key"]: t["Value"] for t in owner_tag_list(region)},
    }
    if vpc_config:
        kwargs["VpcConfig"] = vpc_config
    else:
        # Only reachable when an operator explicitly set
        # TOOL_SANDBOX_REQUIRE_ISOLATION to a falsy value -- test_tool refuses
        # before creating anything otherwise. Logged at WARNING so the opted-out
        # posture is visible in the log rather than inferred from its absence.
        logger.warning(
            "Tool sandbox %s is running WITHOUT network isolation: TOOL_SANDBOX_SUBNET_IDS is "
            "unset, so generated code has public internet egress. Set it to private subnets "
            "with no NAT to close this.",
            function_name,
        )
    _create_function_with_role_retry(lambda_client, kwargs)

    # Cap concurrency before the first invoke. Best-effort on purpose, and NOT tied
    # to _isolation_required(): this is a blast-radius limit, not the boundary.
    #
    # It briefly was tied to it. Once isolation became the default, that coupling
    # meant any deployment whose role lacked ONE action -- lambda:PutFunctionConcurrency
    # -- lost tool testing entirely, which is precisely the outage a missing
    # lambda:TagResource had just caused live (see infra/tests/test_tool_sandbox_grant.py).
    # The trade is not close: refusing here prevents a single 10-second, 128 MB
    # invocation of code that, under the default posture, has no network route off the
    # VPC anyway, and costs every under-granted deployment the whole feature.
    try:
        lambda_client.put_function_concurrency(
            FunctionName=function_name,
            ReservedConcurrentExecutions=_sandbox_concurrency(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not reserve concurrency for %s: %s", function_name, exc)

    # Wait for Active state. The budget depends on whether an ENI has to be built
    # first -- see ACTIVE_WAIT_SECONDS_VPC for the measurements.
    budget = ACTIVE_WAIT_SECONDS_VPC if vpc_config else ACTIVE_WAIT_SECONDS
    deadline = time.monotonic() + budget
    while True:
        fn = lambda_client.get_function(FunctionName=function_name)
        state = fn["Configuration"]["State"]
        if state == "Active":
            return
        if state == "Failed":
            # Terminal. Waiting out the remaining minutes changes nothing, and the
            # reason names the actual problem (a bad subnet, an exhausted ENI quota).
            raise SandboxPolicyError(
                "The isolated tool-test sandbox could not be created: "
                f"{fn['Configuration'].get('StateReason') or 'Lambda reported state Failed'}"
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    # Actionable, and deliberately a SandboxPolicyError: this message IS published
    # to the caller (see the class docstring), because a bare TimeoutError reached
    # the user as "Tool testing failed unexpectedly" -- which is what the live probe
    # saw, and it names neither the cause nor anything to do about it.
    raise SandboxPolicyError(
        f"The isolated tool-test sandbox did not become ready within {budget}s. "
        "The first test after a period of inactivity waits on AWS building a network "
        "interface for the sandbox VPC, which can take around four minutes; a retry "
        "usually completes in seconds. Set TOOL_SANDBOX_REQUIRE_ISOLATION=0 only if "
        "you accept running generated code with public internet egress."
    )


def _run_test_case(lambda_client, function_name: str, test_case: dict) -> dict:
    """Invoke the Lambda with a single test case and validate the response."""
    tc_name = test_case.get("name", "unnamed")
    tc_input = test_case.get("input", {})
    expected_keys = test_case.get("expectedOutputKeys", test_case.get("expected_output_keys", []))

    payload = json.dumps({"toolName": tc_name, "input": tc_input})

    start = time.time()
    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=payload.encode(),
        )
        duration_ms = int((time.time() - start) * 1000)

        # Check for Lambda-level errors (unhandled exceptions)
        if response.get("FunctionError"):
            error_payload = json.loads(response["Payload"].read().decode())
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": error_payload,
                "error": error_payload.get("errorMessage", str(error_payload)),
                "durationMs": duration_ms,
            }

        # Parse Lambda response
        raw = json.loads(response["Payload"].read().decode())
        status_code = raw.get("statusCode", 0)
        body = raw.get("body", "{}")

        # Parse body (may be a JSON string or already a dict)
        if isinstance(body, str):
            try:
                body_parsed = json.loads(body)
            except json.JSONDecodeError:
                body_parsed = {"raw": body}
        else:
            body_parsed = body

        # Validate status code
        if status_code != 200:
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": body_parsed,
                "error": f"Expected statusCode 200, got {status_code}",
                "durationMs": duration_ms,
            }

        # Validate expected output keys
        missing_keys = [k for k in expected_keys if k not in body_parsed]
        if missing_keys:
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": body_parsed,
                "error": f"Missing expected keys in response: {missing_keys}",
                "durationMs": duration_ms,
            }

        return {
            "testCaseName": tc_name,
            "passed": True,
            "actualOutput": body_parsed,
            "error": None,
            "durationMs": duration_ms,
        }

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "testCaseName": tc_name,
            "passed": False,
            "actualOutput": None,
            "error": str(exc),
            "durationMs": duration_ms,
        }


def _cleanup_temp_lambda(lambda_client, function_name: str) -> None:
    """Delete the temporary test Lambda. Role is shared and persists."""
    try:
        lambda_client.delete_function(FunctionName=function_name)
        logger.info("Cleaned up test Lambda: %s", function_name)
    except Exception as exc:
        logger.warning("Failed to clean up test Lambda %s: %s", function_name, exc)
