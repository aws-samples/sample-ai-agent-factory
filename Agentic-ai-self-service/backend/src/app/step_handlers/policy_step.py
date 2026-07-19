"""Step handler: Create AgentCore Policy Engine and attach to Gateway.

Creates a Cedar policy engine and attaches it to the gateway in ENFORCE mode.
Runs AFTER gateway creation since it needs the gateway ID.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/08-AgentCore-policy
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/policy/client.py)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os
import re
import time

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.aws_errors import is_error
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _wait_for_policy_engine(client, engine_id: str, timeout: int = 180) -> dict:
    """Poll until policy engine is ACTIVE/READY or timeout.

    Bug 177: a freshly-created policy engine reports a status of CREATING for up
    to a couple of minutes, and create_policy against it 409s ("Policy engine is
    CREATING, please wait till it is ACTIVE") or yields a misleading CREATE_FAILED
    "Insufficient permissions to call gateway" until it truly converges. The old
    60s budget was too short, so the wait fell through and every policy create
    failed → ENFORCE always degraded to LOG_ONLY. Wait up to 180s for genuine
    ACTIVE. The caller additionally retries the create across the residual window.
    """
    last = ""
    for _ in range(timeout // 5):
        resp = client.get_policy_engine(policyEngineId=engine_id)
        status = resp.get("status", "")
        last = status
        if status in ("ACTIVE", "READY"):
            return resp
        if "FAILED" in status:
            raise RuntimeError(f"Policy engine entered {status}")
        time.sleep(5)
    raise RuntimeError(f"Policy engine {engine_id} did not become ACTIVE in {timeout}s (last: {last})")


def _create_policy_when_engine_ready(client, engine_id, name, description, statement, attempts=6, backoff=10):
    """create_policy with retry on the engine-still-CREATING window (Bug 177).

    Even after get_policy_engine reports ACTIVE, the FIRST create_policy can 409
    "Policy engine is CREATING, please wait till it is ACTIVE" for a residual
    window. Retry the CREATE (not just poll the result) on that ConflictException
    so the engine fully converges. Returns the create_policy response.
    """
    last = None
    for i in range(attempts):
        try:
            # validationMode=IGNORE_ALL_FINDINGS: the policy VALIDATION step calls
            # the gateway to resolve action schemas, and on a freshly-created
            # gateway that call fails "Insufficient permissions to call gateway"
            # (the engine<->gateway authorization hasn't converged) — leaving the
            # policy CREATE_FAILED and the tool plane deny-all indefinitely. Proven
            # live: FAIL_ON_ANY_FINDINGS stays CREATE_FAILED for 8+ min; the SAME
            # statement with IGNORE_ALL_FINDINGS reaches ACTIVE immediately.
            # This skips the analysis FINDINGS (overly-permissive/restrictive
            # warnings), NOT enforcement: AgentCore is default-deny, so a permit
            # over the allowed tools still denies everything else by omission.
            return client.create_policy(
                policyEngineId=engine_id,
                name=name,
                description=description,
                definition={"cedar": {"statement": statement}},
                validationMode="IGNORE_ALL_FINDINGS",
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            # "is CREATING" message check kept: the engine-not-ready signal is only
            # carried in the message text, whatever the outer exception code is.
            if (is_error(e, "ConflictException") and "CREATING" in msg) or "is CREATING" in msg:
                last = e
                logger.warning(
                    "policy engine still CREATING on create (attempt %d/%d) — waiting",
                    i + 1,
                    attempts,
                )
                time.sleep(backoff * (i + 1))
                continue
            raise
    if last:
        raise last


def _read_gateway_tool_actions(agentcore_ctrl, gateway_id: str) -> list:
    """Return the fully-qualified tool names ("{TargetName}___{tool}") the gateway
    actually exposes, by reading each target's MCP tool manifest. Cedar actions
    must reference these exact names or validation fails (Bug 134).
    """
    out = []
    try:
        targets = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=50)
        items = targets.get("items", targets.get("gatewayTargetSummaries", []))
        for t in items:
            tname = t.get("name", "")
            tid = t.get("targetId") or t.get("gatewayTargetId")
            if not tname or not tid:
                continue
            try:
                detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            except Exception:  # noqa: BLE001
                continue
            tc = detail.get("targetConfiguration", {}) or {}
            schema = (
                tc.get("mcp", {}).get("lambda", {}).get("toolSchema", {})
                or tc.get("mcp", {}).get("openApiSchema", {})
                or {}
            )
            for tool in schema.get("inlinePayload", []) or []:
                tool_name = tool.get("name")
                if tool_name:
                    out.append(f"{tname}___{tool_name}")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read gateway tool manifest for %s: %s", gateway_id, e)
    return out


def _cedar_action_ref(action: str, target_names: list) -> str:
    """Return a Cedar action head for a tool.

    AgentCore Cedar actions are TARGET-PREFIXED: AgentCore::Action::"{Target}___{tool}"
    (per https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies.html).
    Accepts: a full Cedar ref (passed through), an already-prefixed name
    ("Target___tool"), or a bare tool name (prefixed with the first known target).
    """
    # Bug 176: ALWAYS use the `action in [...]` list form, never singleton
    # `action == "X"` — AgentCore's policy analysis rejects a singleton action
    # permit on a gateway resource as "Overly Permissive" (CREATE_FAILED), but
    # accepts the list form even for one action. Verified live against the engine.
    if not action or action == "*":
        return "action"
    if "::" in action:
        return f"action in [{action}]"
    if "___" in action:  # already target-prefixed
        return f'action in [AgentCore::Action::"{action}"]'
    # bare tool name -> prefix with each known target (Cedar `in [...]` over variants)
    if target_names:
        refs = ", ".join(f'AgentCore::Action::"{t}___{action}"' for t in target_names)
        return f"action in [{refs}]"
    return f'action in [AgentCore::Action::"{action}"]'


def _rules_to_cedar_policies(rules: list, gateway_arn: str, principal_type: str, target_names: list) -> list:
    """Translate the typed PolicyRule shape ({rule_id, effect, principal, action,
    resource}) into schema-correct Cedar {name, statement} policy dicts (Bug 134).

    Cedar schema facts (AWS docs):
      * principal entity type is AgentCore::OAuthUser (Cognito/OAuth gateways) or
        AgentCore::IamEntity (AWS_IAM gateways) — NOT a bare `principal`.
      * action ids are AgentCore::Action::"{TargetName}___{toolName}".
      * resource is AgentCore::Gateway::"<gateway-arn>".
    An unconditioned permit is emitted WITHOUT a `when` clause (matches the AWS
    example policies); a body-condition rule keeps its `when { ... }`.
    """
    p_is = f"principal is {principal_type}"
    res = f'resource == AgentCore::Gateway::"{gateway_arn}"' if gateway_arn else "resource is AgentCore::Gateway"
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        effect = "forbid" if (r.get("effect") or "permit").lower() == "forbid" else "permit"
        rule_id = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            str(r.get("rule_id") or r.get("ruleId") or f"rule_{len(out)}"),
        )
        # principal: explicit entity ref overrides the gateway-derived type
        principal = r.get("principal")
        p_head = f"principal == {principal}" if principal and principal != "*" and "::" in str(principal) else p_is
        a_head = _cedar_action_ref(r.get("action"), target_names)
        stmt = f"{effect}({p_head}, {a_head}, {res});"
        out.append(
            {
                "name": rule_id,
                "description": r.get("description", f"{effect} rule {rule_id}"),
                "statement": stmt,
            }
        )
    return out


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.POLICY, DeploymentStatusEnum.IN_PROGRESS)

        policy_config = event.get("policy_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        gateway_result = event.get("gateway_result") or {}

        if not policy_config.get("enabled", True):
            return {
                **event,
                "policy_result": {
                    "success": True,
                    "message": "Policy disabled, skipping",
                },
            }

        gateway_id = gateway_result.get("gateway_id", "")
        gateway_arn = gateway_result.get("gateway_arn", "")

        if not gateway_id:
            return {
                **event,
                "policy_result": {
                    "success": False,
                    "message": "No gateway_id available for policy attachment",
                },
            }

        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")

        # Bug 134: deploy_gateway does not return the gateway ARN, but Cedar
        # `resource ==` needs it. Construct it (and verify via get_gateway).
        if not gateway_arn:
            try:
                account_id = step_clients.account_id_for_event(event)
                gateway_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gateway_id}"
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not construct gateway ARN: %s", e)

        # Principal entity type is derived from the gateway's inbound auth:
        # Cognito/OAuth customJWTAuthorizer -> AgentCore::OAuthUser; AWS_IAM ->
        # AgentCore::IamEntity (AWS docs: policy-core-concepts "Principal types").
        # Our gateways use Cognito customJWTAuthorizer, so OAuthUser is the default.
        auth_type = (
            policy_config.get("principal_type")
            or gateway_result.get("principal_type")
            or ("AgentCore::IamEntity" if gateway_result.get("auth") == "IAM" else "AgentCore::OAuthUser")
        )
        principal_type = auth_type if "::" in str(auth_type) else "AgentCore::OAuthUser"

        # Bug 134: Cedar policies validate against the gateway's auto-generated
        # schema — each action is AgentCore::Action::"{Target}___{tool}" for tools
        # that ACTUALLY EXIST in the target's (synced) MCP manifest. The gateway
        # step now resolves these at deploy time (after target creation, polling
        # to readiness) and passes them through — authoritative + already-synced,
        # which avoids the "read too early -> 0 tools -> deny-all" race. Fall back
        # to reading them here only if the gateway step didn't provide them.
        qualified_tools = list(gateway_result.get("qualified_tools") or [])
        expected_tool_count = int(gateway_result.get("expected_tool_count") or 0)
        # FAIL-CLOSED: only fall back to the unguarded configured-schema reader
        # when the gateway step gave us NO sync signal at all (older in-flight
        # event with no expected_tool_count). When expected_tool_count > 0 the
        # gateway step already polled lastSynchronizedAt; an empty qualified_tools
        # there means the tool plane genuinely did NOT sync, and we must let the
        # empty/partial guard fire — NOT re-inflate from inlinePayload (the
        # always-present configured schema), which would ship a permit over a
        # 0-synced plane the agent can't yet serve.
        if not qualified_tools and not expected_tool_count:
            qualified_tools = _read_gateway_tool_actions(agentcore_ctrl, gateway_id)
        logger.info(
            "Gateway %s exposes %d/%d tools for policy scope",
            gateway_id,
            len(qualified_tools),
            expected_tool_count,
        )

        # ENFORCE with an empty OR partial manifest would deny part/all of the
        # tool plane — fail loudly instead of shipping a broken engine.
        if policy_config.get("mode", "ENFORCE") == "ENFORCE":
            if not qualified_tools:
                raise RuntimeError(
                    "Cannot build ENFORCE Cedar policies: the gateway exposes no "
                    "readable tools yet (manifest not synced). Aborting deploy rather "
                    "than attach a deny-all policy engine."
                )
            if expected_tool_count and len(qualified_tools) < expected_tool_count:
                raise RuntimeError(
                    "Cannot build ENFORCE Cedar policies: gateway tool plane is "
                    f"partial ({len(qualified_tools)}/{expected_tool_count} tools "
                    "synced). Aborting rather than attach a policy that silently "
                    "denies the unsynced tools."
                )

        raw_engine_name = policy_config.get("name", f"PolicyEngine_{gateway_id[:16]}")
        # Engine names must match [A-Za-z][A-Za-z0-9_]* — no hyphens
        engine_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_engine_name)

        # Create or reuse policy engine
        engine_id = None
        engine_arn = None

        try:
            existing = agentcore_ctrl.list_policy_engines(maxResults=100)
            for pe in existing.get("policyEngines", existing.get("items", [])):
                if pe.get("name") == engine_name:
                    engine_id = pe.get("policyEngineId")
                    engine_arn = pe.get("policyEngineArn")
                    logger.info("Reusing existing policy engine: %s", engine_id)
                    break
        except Exception as e:
            logger.warning("Could not list policy engines: %s", e)

        if not engine_id:
            resp = agentcore_ctrl.create_policy_engine(
                name=engine_name,
                description=f"Policy engine for gateway {gateway_id}",
            )
            engine_id = resp.get("policyEngineId", "")
            engine_arn = resp.get("policyEngineArn", "")
            logger.info("Created policy engine: %s", engine_id)

            # Manifest: record immediately after create (before the ACTIVE poll,
            # which can be killed mid-flight) so teardown never orphans the engine.
            if engine_id:
                store.record_resource(
                    deployment_id,
                    {"type": "policy_engine", "id": engine_id, "region": region},
                )

            # Wait for it to be ready
            _wait_for_policy_engine(agentcore_ctrl, engine_id)

        # Build the Cedar policy set (Bug 134 — proper ENFORCE fix), validated
        # empirically against the live engine. Rules learned from AWS docs + the
        # engine's own CREATE_FAILED reasons:
        #   * Actions MUST reference tools that EXIST in the gateway manifest
        #     (AgentCore::Action::"{Target}___{tool}") — a fake tool fails with
        #     "unable to find an applicable action".
        #   * An UNCONSTRAINED action permit fails as "Overly Permissive".
        #   * A lone forbid (no matching permit) fails as "Overly Restrictive".
        #   * principal is AgentCore::OAuthUser (Cognito) / AgentCore::IamEntity (IAM);
        #     resource == AgentCore::Gateway::"<arn>".
        # So we emit ONE permit over the real allowed tools (this makes discovery
        # work AND validates), then translate user forbid rules (each carved out
        # of that permit). forbid-wins gives real enforcement.
        res = f'resource == AgentCore::Gateway::"{gateway_arn}"' if gateway_arn else "resource is AgentCore::Gateway"
        rules = policy_config.get("rules", []) or []
        explicit = list(policy_config.get("policies", []) or [])

        # Which real tools should be permitted? Everything the gateway exposes,
        # minus any the user explicitly forbids (those become forbid statements).
        forbidden = set()
        for r in rules:
            if isinstance(r, dict) and (r.get("effect") or "").lower() == "forbid":
                a = r.get("action") or ""
                # match a forbid rule's bare/prefixed tool name against real tools
                for q in qualified_tools:
                    if a and (a == q or q.endswith(f"___{a}")):
                        forbidden.add(q)

        policies = list(explicit)
        if qualified_tools:
            # ENFORCEMENT MODEL (validated against the live engine's analysis):
            # AgentCore is DEFAULT-DENY, so a tool is denied simply by NOT being
            # in any permit. We therefore emit ONE permit over the ALLOWED tools
            # (everything the gateway exposes minus the forbidden set) and DO NOT
            # emit standalone forbid statements — a forbid for a tool that no
            # permit grants is rejected by Cedar analysis as "Overly Restrictive"
            # (it denies a request nothing would allow). Excluding the tool from
            # the permit is the correct, validating way to deny it.
            permitted = [q for q in qualified_tools if q not in forbidden]
            if permitted:
                action_list = ", ".join(f'AgentCore::Action::"{q}"' for q in permitted)
                # Bug 176 (root cause of the Cedar ENFORCE failure — was wrongly
                # degraded to LOG_ONLY by Bug 170): AgentCore's policy analysis
                # rejects a SINGLETON `action == AgentCore::Action::"X"` permit on
                # a gateway resource as "Overly Permissive: will allow every
                # request" (CREATE_FAILED), but ACCEPTS the list form
                # `action in [AgentCore::Action::"X"]` — even for ONE tool. Proven
                # live against the engine (== → CREATE_FAILED; in [..] → ACTIVE),
                # and the service's own StartPolicyGeneration emits the == form yet
                # flags it ALLOW_ALL. So ALWAYS use the `action in [...]` list form;
                # ENFORCE then validates and actually enforces (forbidden tools are
                # denied by omission under AgentCore default-deny).
                action_head = f"action in [{action_list}]"
                desc = "Bug 134: permit the principal to discover + call the allowed tools"
                if forbidden:
                    desc += f" (denied by omission, default-deny: {sorted(forbidden)})"
                policies.insert(
                    0,
                    {
                        "name": "allow_permitted_tools",
                        "description": desc + ".",
                        "statement": f"permit(principal is {principal_type}, {action_head}, {res});",
                    },
                )
            elif forbidden:
                # Everything is forbidden -> there is nothing to permit. In
                # ENFORCE that means an empty tool plane; fail loudly rather than
                # ship an "Overly Restrictive" engine.
                raise RuntimeError(
                    "Cedar ENFORCE would forbid EVERY tool on this gateway "
                    f"(forbidden={sorted(forbidden)}, exposed={qualified_tools}). "
                    "Refusing to attach a deny-all policy engine."
                )
        else:
            # No manifest readable — fall back to the rules translator (best effort).
            policies = explicit + _rules_to_cedar_policies(rules, gateway_arn, principal_type, [])

        created_policy_ids = []
        created_count = 0
        for pol in policies:
            base_name = re.sub(r"[^A-Za-z0-9_]", "_", pol.get("name", "default_policy"))
            # Bug 137: AgentCore policy names are ACCOUNT-GLOBAL, not engine-scoped
            # (proven against the live API: creating a policy whose name already
            # exists in ANOTHER engine raises ConflictException). Two deploys that
            # both emit "allow_permitted_tools" collided — the 2nd's ConflictException
            # was swallowed as "already exists", the engine shipped EMPTY in ENFORCE,
            # and the runtime saw 0 tools (default-deny). Prefix every policy name
            # with the gateway-unique engine name so names never collide across
            # gateways, while staying stable within a single engine's idempotent retry.
            # CreatePolicy /name is constrained to a short length (verified live:
            # a 51-char name was rejected; the limit is well under our old [:128]
            # cap). Build a UNIQUE-but-SHORT name: keep the full base_name (the
            # semantic part, e.g. allow_permitted_tools) and prefix with a bounded
            # slice of the engine name for cross-gateway uniqueness, capping the
            # whole thing at 48 chars to stay within the service limit.
            _eng_prefix = engine_name[: max(0, 48 - len(base_name) - 1)]
            pol_name = (f"{_eng_prefix}_{base_name}" if _eng_prefix else base_name)[:48]
            # A statement-less explicit policy entry (e.g. {"effect":"permit"} with
            # no Cedar text) must NOT fall back to `permit(principal, action,
            # resource)` — that unconstrained wildcard is BOTH rejected by AgentCore
            # analysis ("wildcard resource detected") AND a security hole (it would
            # allow every principal→action→resource, defeating the constrained
            # per-tool permit this step auto-builds). Skip it: the auto-built
            # `allow_permitted_tools` permit (inserted above from the gateway
            # manifest) is the correct constrained grant. Observed in the live
            # matrix run (P-PLAT-027).
            _stmt = pol.get("statement")
            if not _stmt or not _stmt.strip():
                logger.warning(
                    "Skipping statement-less policy '%s' — would emit a wildcard "
                    "permit (rejected + allow-all). The auto-built constrained "
                    "permit over the gateway's allowed tools governs access instead.",
                    pol_name,
                )
                continue
            try:
                cp = _create_policy_when_engine_ready(
                    agentcore_ctrl,
                    engine_id,
                    pol_name,
                    pol.get("description", ""),
                    _stmt,
                )
                # Bug 177 diagnostics: log the EXACT Cedar statement so a
                # CREATE_FAILED reason can be matched to what we emitted (the
                # statement is config-derived, not a secret).
                logger.warning("Created policy: %s | stmt=%s", pol_name, pol.get("statement", "")[:300])
                created_count += 1
                pid = cp.get("policyId")
                if pid:
                    created_policy_ids.append((pol_name, pid))
            except Exception as e:
                # "already exists" fallback kept: conflicts can surface as a
                # ValidationException whose message says "already exists".
                if is_error(e, "ConflictException") or "already exists" in str(e):
                    # With the engine-name prefix, a conflict means an idempotent
                    # retry reusing the SAME engine. Recover the existing policy's id
                    # FROM THIS ENGINE and validate it like a fresh one. If it is NOT
                    # in this engine, the name collided with a FOREIGN engine — that
                    # is the exact Bug 137 trap, so fail closed instead of shipping an
                    # empty deny-all engine.
                    existing_pid = None
                    try:
                        lp = agentcore_ctrl.list_policies(policyEngineId=engine_id, maxResults=100)
                        for ep in lp.get("policies", lp.get("items", [])):
                            if ep.get("name") == pol_name:
                                existing_pid = ep.get("policyId")
                                break
                    except Exception as le:  # noqa: BLE001
                        logger.warning("Could not list policies on conflict: %s", le)
                    if existing_pid:
                        logger.info("Policy '%s' already in engine %s; will validate", pol_name, engine_id)
                        created_count += 1
                        created_policy_ids.append((pol_name, existing_pid))
                    else:
                        raise RuntimeError(
                            f"Policy name '{pol_name}' conflicts with a policy NOT in "
                            f"engine {engine_id} (account-global name collision, Bug 137) "
                            "— refusing to ship an empty ENFORCE engine."
                        ) from e
                else:
                    logger.error("Failed to create policy '%s': %s", pol_name, e)
                    raise RuntimeError(f"Policy creation failed for '{pol_name}': {e}. Aborting.") from e

        # Bug 134: create_policy is ASYNC — it returns CREATING then validates
        # against the gateway schema. A policy that references a non-existent tool,
        # is overly permissive (unconstrained action), or overly restrictive (lone
        # forbid) ends CREATE_FAILED. Poll every created policy to a terminal state
        # and FAIL the deploy if any did not reach ACTIVE — otherwise we'd attach an
        # engine in ENFORCE that silently denies the tool plane (the original bug).
        is_enforce = policy_config.get("mode", "ENFORCE") == "ENFORCE"
        # P-PLAT-027 (supersedes Bug 170): when an ENFORCE policy fails Cedar
        # validation (e.g. "Insufficient permissions to call gateway" while the
        # gateway's policy-authorization plane converges), we must FAIL CLOSED.
        # The old behavior downgraded to LOG_ONLY so the tool plane stayed
        # functional — but that silently serves a tool the user explicitly
        # forbade (restricted-tool value leaked in live verification). AgentCore
        # engines are default-deny, so attaching in ENFORCE with the policies
        # still pending denies ALL tools until the permit validates — tools are
        # temporarily unavailable instead of unprotected. The lazy promoter
        # (Bug 178) then creates the pending policies once the gateway converges,
        # which un-bricks the tool plane under real enforcement.
        # Users who prefer availability over enforcement can opt out explicitly
        # with policyConfig.on_enforce_failure = "log_only".
        fail_open_requested = str(policy_config.get("on_enforce_failure", "fail_closed")).lower() == "log_only"
        enforce_validation_pending = False
        downgrade_to_log_only = False
        downgrade_reason = ""
        if is_enforce and created_policy_ids:
            import time as _t

            def _await_policy(pid):
                """Poll a policy to a terminal state; return (status, reason)."""
                for _ in range(20):
                    try:
                        d = agentcore_ctrl.get_policy(policyEngineId=engine_id, policyId=pid)
                        s = d.get("status", "")
                        if s in ("ACTIVE", "CREATE_FAILED", "FAILED"):
                            return s, str(d.get("statusReasons") or d.get("statusReason") or "")[:200]
                    except Exception:  # noqa: BLE001 — transient get_policy error; keep polling to timeout
                        logger.debug("get_policy %s poll failed", pid, exc_info=True)
                    _t.sleep(3)
                return "CREATING", "timeout"

            # Bug 177: a brand-new policy engine's FIRST policy create can end
            # CREATE_FAILED "Insufficient permissions to call gateway with ID ..."
            # purely from engine<->gateway eventual consistency — the IDENTICAL
            # statement validates ACTIVE seconds later (proven live: same engine,
            # same `action in [...]` permit, CREATE_FAILED then ACTIVE on retry).
            # So before degrading to LOG_ONLY, RETRY each transiently-failed policy
            # (delete + recreate with backoff). Only a persistent failure degrades.
            _TRANSIENT = (
                "insufficient permissions to call gateway",
                "is creating",
                "please wait till it is active",
            )
            name_to_stmt = {}
            for pol in policies:
                _bn = re.sub(r"[^A-Za-z0-9_]", "_", pol.get("name", "default_policy"))
                _ep = engine_name[: max(0, 48 - len(_bn) - 1)]
                name_to_stmt[(f"{_ep}_{_bn}" if _ep else _bn)[:48]] = pol
            failed = []
            for pol_name, pid in list(created_policy_ids):
                status, reason = _await_policy(pid)
                # Retry transient engine/gateway-consistency failures with a SHORT
                # budget only. The engine<->gateway authorization plane can take
                # 5-15 MINUTES to converge on a freshly-created gateway (proven
                # live: same engine + identical permit is CREATE_FAILED at gateway
                # age ~12min, ACTIVE at ~17min). Blocking the deploy step that long
                # is wrong — it makes deploys 12+ min AND still races the tail.
                # So: try a couple of quick recreates here to catch the FAST-
                # converging cases, then attach ENFORCE fail-closed with the permit
                # still pending. The lazy promoter (policy_promoter) recreates the
                # policy on later status/invoke touchpoints, once the gateway has
                # aged into convergence — that is the real hands-off converger.
                attempt = 0
                while status != "ACTIVE" and any(t in reason.lower() for t in _TRANSIENT) and attempt < 6:
                    attempt += 1
                    logger.warning(
                        "Policy %s CREATE_FAILED (transient, attempt %d/6): %s — recreating",
                        pol_name,
                        attempt,
                        reason,
                    )
                    try:
                        agentcore_ctrl.delete_policy(policyEngineId=engine_id, policyId=pid)
                    except Exception:  # noqa: BLE001 — recreate below conflicts loudly if the delete failed
                        logger.debug("delete_policy %s before recreate failed", pid, exc_info=True)
                    _t.sleep(20)  # short waits; promoter finishes convergence post-deploy
                    pol = name_to_stmt.get(pol_name, {})
                    try:
                        cp2 = _create_policy_when_engine_ready(
                            agentcore_ctrl,
                            engine_id,
                            pol_name,
                            pol.get("description", ""),
                            pol.get("statement", ""),
                        )
                        pid = cp2.get("policyId") or pid
                        status, reason = _await_policy(pid)
                    except Exception as ce:  # noqa: BLE001
                        reason = str(ce)[:200]
                if status != "ACTIVE":
                    failed.append((pol_name, reason))
            if failed:
                failure_detail = "; ".join(f"{n}: {r}" for n, r in failed)
                if fail_open_requested:
                    downgrade_to_log_only = True
                    downgrade_reason = (
                        f"Cedar ENFORCE validation failed ({failure_detail}) — "
                        "attaching engine in LOG_ONLY per explicit "
                        "on_enforce_failure=log_only opt-in; policies are still "
                        "evaluated + logged but NOT enforced."
                    )
                else:
                    # FAIL CLOSED (P-PLAT-027): attach in ENFORCE with the permit
                    # still pending. Default-deny blocks every tool (including the
                    # forbidden one) until the promoter validates the permit.
                    enforce_validation_pending = True
                    downgrade_reason = (
                        f"Cedar ENFORCE validation failed ({failure_detail}) — "
                        "attaching engine in ENFORCE anyway (fail-closed): ALL "
                        "tools are denied until the permit policy validates. The "
                        "policy promoter retries on status/test touchpoints."
                    )
                logger.warning(downgrade_reason)
                # Drop the CREATE_FAILED policies so the engine holds only ACTIVE
                # ones; the pending payload below recreates them once the gateway
                # converges.
                for _pol_name, pid in created_policy_ids:
                    try:
                        d = agentcore_ctrl.get_policy(policyEngineId=engine_id, policyId=pid)
                        if d.get("status") != "ACTIVE":
                            agentcore_ctrl.delete_policy(policyEngineId=engine_id, policyId=pid)
                    except Exception:  # noqa: BLE001 — best-effort drop; promoter recreates from the pending payload
                        logger.debug("Drop of non-ACTIVE policy %s failed", pid, exc_info=True)

        if is_enforce and created_count == 0 and not enforce_validation_pending:
            # Nothing valid to enforce. Same fail-closed rule as above.
            if fail_open_requested:
                downgrade_to_log_only = True
                downgrade_reason = downgrade_reason or (
                    "No ENFORCE Cedar policies could be created for this gateway — "
                    "attaching engine in LOG_ONLY (audit-only) per explicit "
                    "on_enforce_failure=log_only opt-in."
                )
            else:
                enforce_validation_pending = True
                downgrade_reason = downgrade_reason or (
                    "No ENFORCE Cedar policies could be created for this gateway — "
                    "attaching engine in ENFORCE (fail-closed, default-deny): all "
                    "tools are denied until the policies validate via the promoter."
                )
            logger.warning(downgrade_reason)

        # Bug 137 backstop (authoritative): before attaching in ENFORCE, confirm the
        # engine ACTUALLY HOLDS >=1 ACTIVE policy by reading it back from the service.
        # created_count is a client-side intent counter; this checks ground truth and
        # catches ANY path (name collision, async drop, eventual-consistency) that
        # would otherwise ship an empty deny-all engine that serves 0 tools at runtime.
        if is_enforce and not downgrade_to_log_only and not enforce_validation_pending:
            active_on_engine = 0
            for _attempt in range(10):
                try:
                    lp = agentcore_ctrl.list_policies(policyEngineId=engine_id, maxResults=100)
                    pols = lp.get("policies", lp.get("items", []))
                    active_on_engine = sum(1 for p in pols if p.get("status") == "ACTIVE")
                    if active_on_engine > 0:
                        break
                except Exception as le:  # noqa: BLE001
                    logger.warning("list_policies backstop attempt failed: %s", le)
                time.sleep(3)
            if active_on_engine == 0:
                # Empty ENFORCE engine denies ALL tools (default-deny). Fail
                # closed (P-PLAT-027): keep ENFORCE — a temporarily-denied tool
                # plane is safer than serving a tool the user forbade. LOG_ONLY
                # only with the explicit opt-in.
                if fail_open_requested:
                    downgrade_to_log_only = True
                    downgrade_reason = downgrade_reason or (
                        f"ENFORCE engine {engine_id} holds 0 ACTIVE policies — "
                        "attaching in LOG_ONLY per on_enforce_failure=log_only."
                    )
                else:
                    enforce_validation_pending = True
                    downgrade_reason = downgrade_reason or (
                        f"ENFORCE engine {engine_id} holds 0 ACTIVE policies — "
                        "staying in ENFORCE (fail-closed, all tools denied) until "
                        "the promoter validates the policies."
                    )
                logger.warning(downgrade_reason)
            else:
                logger.info(
                    "Engine %s holds %d ACTIVE policies — safe to attach in ENFORCE", engine_id, active_on_engine
                )

        # Attach policy engine to gateway.
        # ENFORCE is the requested default (real enforcement). When the auto-built
        # ENFORCE policy can't validate yet we STAY in ENFORCE (fail-closed,
        # default-deny — P-PLAT-027) unless the user explicitly opted into
        # on_enforce_failure=log_only.
        mode = policy_config.get("mode", "ENFORCE")
        if mode not in ("LOG_ONLY", "ENFORCE"):
            mode = "ENFORCE"
        if downgrade_to_log_only:
            mode = "LOG_ONLY"
        # Get current gateway config to preserve existing fields
        gw_detail = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id)

        update_params = {
            "gatewayIdentifier": gateway_id,
            "name": gw_detail.get("name", ""),
            "roleArn": gw_detail.get("roleArn", ""),
            "protocolType": gw_detail.get("protocolType", "MCP"),
            "policyEngineConfiguration": {"arn": engine_arn, "mode": mode},
        }
        # Preserve optional fields if present
        for optional_field in (
            "description",
            "authorizerType",
            "authorizerConfiguration",
            "protocolConfiguration",
            "kmsKeyArn",
        ):
            if gw_detail.get(optional_field):
                update_params[optional_field] = gw_detail[optional_field]

        agentcore_ctrl.update_gateway(**update_params)
        logger.info(
            "Attached policy engine %s to gateway %s in %s mode",
            engine_id,
            gateway_id,
            mode,
        )

        # Wait for gateway to be ready again
        for _ in range(24):
            gw = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id)
            if gw.get("status") == "READY":
                break
            time.sleep(5)

        # Bug 178 (lazy ENFORCE promotion): when ENFORCE was requested but we
        # attached LOG_ONLY because the gateway's policy-authorization plane had
        # not yet converged (~3-5 min after tool-sync — confirmed live + matches
        # the AWS policy workshop's separate-lifecycle model), record everything
        # needed to PROMOTE to ENFORCE later. The test/status endpoints call
        # services.policy_promoter.try_promote_to_enforce() when the agent is first
        # used (minutes later), which idempotently re-creates the now-valid policy
        # and flips the engine to ENFORCE. Carries the intended Cedar statements so
        # the promoter doesn't have to recompute them.
        # The pending payload now serves BOTH paths: the fail-closed ENFORCE
        # attach (promoter creates the pending policies to UN-BRICK the denied
        # tool plane) and the explicit log_only opt-in (promoter flips the mode
        # to ENFORCE once policies validate).
        enforce_pending = None
        if (downgrade_to_log_only or enforce_validation_pending) and policy_config.get("mode", "ENFORCE") == "ENFORCE":
            _plist = []
            for pol in policies:
                _bn = re.sub(r"[^A-Za-z0-9_]", "_", pol.get("name", "default_policy"))
                _ep = engine_name[: max(0, 48 - len(_bn) - 1)]
                _pn = (f"{_ep}_{_bn}" if _ep else _bn)[:48]
                _plist.append(
                    {"name": _pn, "statement": pol.get("statement", ""), "description": pol.get("description", "")}
                )
            enforce_pending = {
                "engine_id": engine_id,
                "gateway_id": gateway_id,
                "gateway_arn": gateway_arn,
                "policies": _plist,
            }

        return {
            **event,
            "policy_result": {
                "success": True,
                "engine_id": engine_id,
                "engine_arn": engine_arn,
                "engine_name": engine_name,
                "mode": mode,
                # Bug 170: when ENFORCE was requested but couldn't validate, the
                # engine is attached in LOG_ONLY and we report the downgrade so the
                # UI/caller can show "policy auditing only" instead of silently
                # implying full enforcement.
                "requested_mode": policy_config.get("mode", "ENFORCE"),
                "downgraded_to_log_only": downgrade_to_log_only,
                # P-PLAT-027: True when the engine attached in ENFORCE with the
                # permit still pending — all tools are denied (fail-closed)
                # until the promoter validates the policies.
                "enforce_validation_pending": enforce_validation_pending,
                "downgrade_reason": downgrade_reason or None,
                # Bug 178: lazy-promotion context (None unless a downgrade happened).
                "enforce_pending": enforce_pending,
            },
        }

    except Exception:
        logger.exception("Policy step failed for deployment %s", deployment_id)
        raise
