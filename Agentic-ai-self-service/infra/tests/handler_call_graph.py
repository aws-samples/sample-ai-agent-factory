"""What can a step Lambda's handler actually call? A transitive AST call graph.

Not a test module. It exists because "what is this step for?" is not an oracle and reading
it as one cost real authority: the Lambda mutation grant in ``_create_step_role`` was
handed to ``gateway``, ``mcp_server``, ``codegen`` and ``knowledge_base`` because all four
sounded like steps that deploy code, and three of them never made a single Lambda API call
(fixed 2026-09-22). ARCC ``cnt_L4ZLZgjrCctfxl`` is why that mattered rather than being
untidy: ``lambda:UpdateFunctionCode`` on a function makes that function's EXECUTION role
run the caller's code, so each of those roles silently held every tool Lambda's identity.

**Direction of use, and this is the whole design constraint.** Matching is by boto3 METHOD
NAME with no knowledge of what the receiver is, so ``policy_step``'s
``bedrock-agentcore:GetPolicy`` counts as ``lambda:GetPolicy`` and ``deployment_handler``'s
``lambda_client.invoke`` is indistinguishable from any other ``.invoke``. The reachable set
is therefore an OVER-estimate, which makes it sound for exactly one question:

* **Sound:** "is this granted action beyond anything the handler could call?" An
  over-estimate only ever makes that test more permissive -- it cannot manufacture a
  false red.
* **Not sound:** "is every action the handler needs granted?" An over-estimate would
  demand grants for calls on other services' clients. The outage direction stays with the
  per-capability tests that name their call site (``test_tool_sandbox_grant``,
  ``test_the_tool_lambda_ownership_grant``) and with the live deploy.

One dependent authorization is derived rather than called:
``create_function(Tags=...)`` requires ``lambda:TagResource`` on the new function. The
walker records that action only when it sees the explicit ``Tags`` keyword, so the
least-privilege audit does not misclassify the required tag-on-create grant as dead.

Edges followed: a plain ``Name`` call resolved through an ``app.*`` ``ImportFrom`` visible
at module level or inside the calling function body (deferred imports are common here), or
through a sibling def in the same module. Edges NOT followed: a call on an imported module
object (``import app.services.x`` then ``x.f()``), a call through a variable, a callback
passed to a helper, and any method on ``self``. So the graph can UNDER-report reachability,
which in the sound direction above surfaces as a red test demanding a grant be removed --
fail-toward-investigation, not fail-toward-outage. If one fires, prove the call site is
unreachable before deleting anything; that is what was done for the three roles above.
"""

from __future__ import annotations

import ast
import pathlib

#: Repo-relative root of the backend package tree, resolved from this file so the module
#: does not care where pytest was invoked from.
BACKEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src"

#: boto3 lambda-client method name -> IAM action. Only the actions any step role is
#: granted today, plus the ones a future step would plausibly reach for; an action absent
#: from this map is simply not checked, so extending a grant means extending this map.
LAMBDA_METHOD_TO_ACTION = {
    "create_function": "lambda:CreateFunction",
    "update_function_code": "lambda:UpdateFunctionCode",
    "update_function_configuration": "lambda:UpdateFunctionConfiguration",
    "delete_function": "lambda:DeleteFunction",
    "get_function": "lambda:GetFunction",
    "get_function_configuration": "lambda:GetFunctionConfiguration",
    "invoke": "lambda:InvokeFunction",
    "add_permission": "lambda:AddPermission",
    "remove_permission": "lambda:RemovePermission",
    "get_policy": "lambda:GetPolicy",
    "list_tags": "lambda:ListTags",
    "tag_resource": "lambda:TagResource",
    "untag_resource": "lambda:UntagResource",
    "put_function_concurrency": "lambda:PutFunctionConcurrency",
    "delete_function_url_config": "lambda:DeleteFunctionUrlConfig",
    "create_function_url_config": "lambda:CreateFunctionUrlConfig",
    "publish_version": "lambda:PublishVersion",
    "list_functions": "lambda:ListFunctions",
}

#: Boto3 method -> explicit keyword -> dependent IAM action. These are authorizations
#: AWS performs for the same request, not separate boto3 calls. Keep this keyword-
#: sensitive: treating every CreateFunction as tagged would make the over-reach audit
#: more permissive than the source warrants.
LAMBDA_KEYWORD_DEPENDENT_ACTIONS = {
    "create_function": {"Tags": "lambda:TagResource"},
}

#: boto3 ``bedrock-agentcore-control`` create-method name -> the AgentCore resource TYPES
#: whose tags that one call writes. Passing ``tags=``/``Tags=`` to a create is authorized as
#: a separate ``bedrock-agentcore:TagResource`` on every resource the create tags, which
#: includes resources the caller never names: ``CreateAgentRuntime`` mints a workload
#: identity for the runtime and tags that too, so ``runtime`` alone is an outage. Types are
#: the names used by the AWS Service Reference feed for bedrock-agentcore.
AGENTCORE_CREATE_TO_TAGGED_TYPES = {
    "create_agent_runtime": ("runtime", "workload-identity"),
    "create_gateway": ("gateway", "workload-identity"),
    "create_memory": ("memory",),
    "create_policy_engine": ("policy-engine",),
    # CreateHarness tags THREE types, not one, and the two extra ones are invisible to
    # every oracle except a live delete. CreateHarness is implemented on top of
    # CreateAgentRuntime (it sets agentCoreRuntimeEnvironment.agentRuntimeName), and it
    # creates and tags that backing runtime ASYNCHRONOUSLY, under the CALLER's
    # credentials, AFTER the synchronous create has already returned 200.
    #
    # Measured live 2026-09-22 on acfe2e-p0920, StepHarnessRole:
    #   User: .../acfe2e-p0920-step-harness is not authorized to perform:
    #   bedrock-agentcore:TagResource on resource:
    #   arn:aws:bedrock-agentcore:123456789012:runtime/*
    #   (Service: BedrockAgentcoreRuntimeControl, Status Code: 403)
    # Note the Service is BedrockAgentcoreRuntimeControl -- a DIFFERENT service from the
    # API that was called. The harness merely went CREATE_FAILED and the step raised
    # "Harness failed to become ready: Harness entered CREATE_FAILED" with no code, no
    # ARN and no action. A full CloudWatch sweep of every step log group found NOTHING:
    # the denial existed only in the delete_harness response's own failureReason field.
    # So for any CREATING/CREATE_FAILED lifecycle, "no denial in the logs" is unproven,
    # not clean -- had teardown used a blind delete this would read as a non-IAM failure.
    #
    # `workload-identity` is DERIVED, not measured, and is included deliberately:
    # create_agent_runtime above mints and tags one, so a backing runtime mints one too.
    # It could not have been measured yet -- the runtime/* denial above is reached first
    # and masks it, so granting only what was observed would just buy the next
    # invisible async denial. It fails closed, which is the side to err on here.
    #
    # `memory` is MEASURED, and it is the entry that shows the round above stopped one
    # resource short. "A backing runtime mints a workload identity too" was the right
    # inference; the same call also auto-provisions a DEFAULT memory, which
    # harness_deployer.py:276 already documents in prose -- for the exec role that has to
    # read it later. The denial was invisible for the same reason as the runtime one: it
    # arrives asynchronously, after CreateHarness has already returned 200.
    # Measured live 2026-09-24 on acfe2e-p0920, deployment d22088ec:
    #   User: arn:aws:sts::123456789012:assumed-role/acfe2e-p0920-StepHarnessRole.../
    #   acfe2e-p0920-step-harness is not authorized to perform:
    #   bedrock-agentcore:TagResource on resource:
    #   arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/p0bharn1790231300_302f262f-*
    #   because no identity-based policy allows the bedrock-agentcore:TagResource action
    #   (Service: GenesisMemoryControlPlane, Status Code: 403)
    # Unlike the runtime one this DID reach a log group (/aws/lambda/acfe2e-p0920-step-harness,
    # via the step's own re-raise) -- but only because the harness step surfaces
    # wait_for_harness_ready's error text. The deployment row's copy is redacted, so the
    # principal is recoverable from the log and nowhere else.
    "create_harness": ("harness", "runtime", "workload-identity", "memory"),
    "create_oauth2_credential_provider": ("oauth2credentialprovider",),
    "create_api_key_credential_provider": ("apikeycredentialprovider",),
}

#: The ARN tail for each of these types is deliberately NOT defined here. It lives in
#: ``stacks/platform/step_lambdas.AGENTCORE_TYPE_ARN_TAIL``, because the grant is the
#: product and this module is the audit: a test that carried its own copy of the ARN shapes
#: would pass while the deployed policy named something else.


def _module_path(dotted: str) -> pathlib.Path | None:
    p = BACKEND_SRC / (dotted.replace(".", "/") + ".py")
    if p.is_file():
        return p
    p = BACKEND_SRC / dotted.replace(".", "/") / "__init__.py"
    return p if p.is_file() else None


class _Graph:
    """Walks one handler's transitive call graph, recording hits on *method_map*'s keys.

    *method_map* is keyed by boto3 method name; the value is opaque to the walker and is
    what ``hits`` is keyed by, so the same traversal serves both the Lambda-action question
    and the tag-on-create question without either one's map leaking into the other's.
    """

    def __init__(
        self,
        method_map: dict[str, object],
        keyword_dependent_actions: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._trees: dict[str, ast.Module | None] = {}
        self._method_map = method_map
        self._keyword_dependent_actions = keyword_dependent_actions or {}

    def tree(self, dotted: str) -> ast.Module | None:
        if dotted not in self._trees:
            p = _module_path(dotted)
            self._trees[dotted] = ast.parse(p.read_text()) if p else None
        return self._trees[dotted]

    @staticmethod
    def _defs(tree: ast.Module) -> dict[str, ast.AST]:
        return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    @staticmethod
    def _app_imports(node: ast.AST) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for n in ast.walk(node):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app."):
                for a in n.names:
                    out[a.asname or a.name] = (n.module, a.name)
        return out

    def _record_method(
        self,
        method_name: str,
        call: ast.Call,
        dotted: str,
        func: str,
        hits: dict[object, set[str]],
    ) -> None:
        site = f"{dotted}.{func}:{call.lineno}"
        key = self._method_map.get(method_name)
        if key is not None:
            hits.setdefault(key, set()).add(site)
        for keyword, action in self._keyword_dependent_actions.get(method_name, {}).items():
            if any(item.arg == keyword for item in call.keywords):
                hits.setdefault(action, set()).add(site)

    def walk(self, dotted: str, func: str, seen: set, hits: dict[object, set[str]]) -> None:
        if (dotted, func) in seen:
            return
        seen.add((dotted, func))
        tree = self.tree(dotted)
        if tree is None:
            return
        defs = self._defs(tree)
        if func not in defs:
            return
        node = defs[func]
        visible = self._app_imports(tree)
        visible.update(self._app_imports(node))
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Attribute):
                self._record_method(f.attr, n, dotted, func, hits)
            elif isinstance(f, ast.Name):
                # A bare-Name call can BE one of the mapped methods when the handler
                # imports our own wrapper of the same name (``from app.services.
                # runtime_deployer import create_agent_runtime``). Record the hit and keep
                # descending: the wrapper contains the boto3 call as well, and a set makes
                # the double count harmless.
                self._record_method(f.id, n, dotted, func, hits)
                if f.id in visible:
                    m, orig = visible[f.id]
                    self.walk(m, orig, seen, hits)
                elif f.id in defs:
                    self.walk(dotted, f.id, seen, hits)


def reachable_lambda_actions(handler_ref: str) -> tuple[dict[str, set[str]], int]:
    """``({action: {"module.func:line", ...}}, functions reached)`` for one handler.

    *handler_ref* is the CDK ``handler=`` string, e.g.
    ``"src/app/step_handlers/gateway_step.handler"``.

    The function count is returned so a caller can assert the graph actually traversed
    something: a typo'd handler ref would otherwise yield an empty action set, which reads
    as "grants nothing, all clear" -- the exact failure shape this module exists to stop.
    """
    hits, reached = _reachable(
        handler_ref,
        LAMBDA_METHOD_TO_ACTION,
        keyword_dependent_actions=LAMBDA_KEYWORD_DEPENDENT_ACTIONS,
    )
    return {str(k): v for k, v in hits.items()}, reached


def _reachable(
    handler_ref: str,
    method_map: dict[str, object],
    *,
    keyword_dependent_actions: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[object, set[str]], int]:
    path, _, func = handler_ref.rpartition(".")
    dotted = path.removeprefix("src/").replace("/", ".")
    hits: dict[object, set[str]] = {}
    seen: set = set()
    _Graph(method_map, keyword_dependent_actions).walk(dotted, func, seen, hits)
    return hits, len(seen)


def reachable_agentcore_tagged_types(handler_ref: str) -> tuple[set[str], int, dict[str, set[str]]]:
    """``(resource types this handler can tag on create, functions reached, call sites)``.

    Same over-estimating traversal as ``reachable_lambda_actions`` and the same soundness
    argument -- but used in the OPPOSITE direction, which is why that matters. Here an
    over-estimate demands a grant the handler may not need, and the failure mode of an
    unnecessary ``bedrock-agentcore:TagResource`` bounded by
    ``aws:RequestTag/AgentCoreStack`` is that the role may stamp THIS deployment's own
    ownership tags on a resource of that type. The failure mode of a missing one is a live
    outage of that deploy path with no fallback, because every create here sends
    ``owner_tags()`` unconditionally. So over-granting is the correct side to err on, and
    the under-reporting edges listed in the module docstring are the real risk: a create
    reached only through a module object or a variable will not appear here.
    """
    hits, reached = _reachable(handler_ref, AGENTCORE_CREATE_TO_TAGGED_TYPES)
    sites = {",".join(k): v for k, v in hits.items()}  # type: ignore[arg-type]
    types: set[str] = set()
    for key in hits:
        types.update(key)  # type: ignore[arg-type]
    return types, reached, sites


def step_handler_refs(step_lambdas_py: pathlib.Path) -> dict[str, str]:
    """``{step name: handler ref}`` parsed out of ``step_lambdas.py``'s own dict literal.

    Parsed rather than transcribed so a step added later is covered automatically instead
    of being invisible to the test that governs its role. Only keys whose value dict has a
    ``handler`` string are returned.
    """
    tree = ast.parse(step_lambdas_py.read_text())
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        # strict=True is safe and load-bearing: ast.Dict.keys and .values are parallel by
        # construction EXCEPT for `**spread`, which parses as a None key. A length mismatch
        # would mean the parse is not what this code assumes, and pairing the wrong key
        # with the wrong value would silently mis-map a step to another step's handler.
        for k, v in zip(node.keys, node.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Dict)):
                continue
            for k2, v2 in zip(v.keys, v.values, strict=True):
                if (
                    isinstance(k2, ast.Constant)
                    and k2.value == "handler"
                    and isinstance(v2, ast.Constant)
                    and isinstance(v2.value, str)
                ):
                    out[k.value] = v2.value
    return out
