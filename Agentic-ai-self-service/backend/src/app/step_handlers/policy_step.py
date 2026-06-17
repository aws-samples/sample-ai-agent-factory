"""Step handler: Create AgentCore Policy Engine and attach to Gateway.

Creates a Cedar policy engine and attaches it to the gateway in ENFORCE mode.
Runs AFTER gateway creation since it needs the gateway ID.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/08-AgentCore-policy
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/policy/client.py)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
import re
import time

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _wait_for_policy_engine(client, engine_id: str, timeout: int = 60) -> dict:
    """Poll until policy engine is ACTIVE/READY or timeout."""
    for _ in range(timeout // 5):
        resp = client.get_policy_engine(policyEngineId=engine_id)
        status = resp.get("status", "")
        if status in ("ACTIVE", "READY"):
            return resp
        if "FAILED" in status:
            raise RuntimeError(f"Policy engine entered {status}")
        time.sleep(5)
    raise RuntimeError(f"Policy engine {engine_id} did not become ACTIVE in {timeout}s")


def _read_gateway_tool_actions(agentcore_ctrl, gateway_id: str) -> list:
    """Return the fully-qualified tool names ("{TargetName}___{tool}") the gateway
    actually exposes, by reading each target's MCP tool manifest. Cedar actions
    must reference these exact names or validation fails (Bug 134).
    """
    out = []
    try:
        targets = agentcore_ctrl.list_gateway_targets(
            gatewayIdentifier=gateway_id, maxResults=50
        )
        items = targets.get("items", targets.get("gatewayTargetSummaries", []))
        for t in items:
            tname = t.get("name", "")
            tid = t.get("targetId") or t.get("gatewayTargetId")
            if not tname or not tid:
                continue
            try:
                detail = agentcore_ctrl.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=tid
                )
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
    if not action or action == "*":
        return "action"
    if "::" in action:
        return f"action == {action}"
    if "___" in action:  # already target-prefixed
        return f'action == AgentCore::Action::"{action}"'
    # bare tool name -> prefix with each known target (Cedar `in [...]` over variants)
    if target_names:
        refs = ", ".join(f'AgentCore::Action::"{t}___{action}"' for t in target_names)
        return f"action in [{refs}]" if len(target_names) > 1 else f"action == {refs}"
    return f'action == AgentCore::Action::"{action}"'


def _rules_to_cedar_policies(
    rules: list, gateway_arn: str, principal_type: str, target_names: list
) -> list:
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
    res = (
        f'resource == AgentCore::Gateway::"{gateway_arn}"'
        if gateway_arn
        else f"resource is AgentCore::Gateway"
    )
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        effect = "forbid" if (r.get("effect") or "permit").lower() == "forbid" else "permit"
        rule_id = re.sub(
            r"[^A-Za-z0-9_]", "_",
            str(r.get("rule_id") or r.get("ruleId") or f"rule_{len(out)}"),
        )
        # principal: explicit entity ref overrides the gateway-derived type
        principal = r.get("principal")
        p_head = f"principal == {principal}" if principal and principal != "*" and "::" in str(principal) else p_is
        a_head = _cedar_action_ref(r.get("action"), target_names)
        stmt = f"{effect}({p_head}, {a_head}, {res});"
        out.append({
            "name": rule_id,
            "description": r.get("description", f"{effect} rule {rule_id}"),
            "statement": stmt,
        })
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

        agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)

        # Bug 134: deploy_gateway does not return the gateway ARN, but Cedar
        # `resource ==` needs it. Construct it (and verify via get_gateway).
        if not gateway_arn:
            try:
                account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
                gateway_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gateway_id}"
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not construct gateway ARN: %s", e)

        # Principal entity type is derived from the gateway's inbound auth:
        # Cognito/OAuth customJWTAuthorizer -> AgentCore::OAuthUser; AWS_IAM ->
        # AgentCore::IamEntity (AWS docs: policy-core-concepts "Principal types").
        # Our gateways use Cognito customJWTAuthorizer, so OAuthUser is the default.
        auth_type = (policy_config.get("principal_type")
                     or gateway_result.get("principal_type")
                     or ("AgentCore::IamEntity" if gateway_result.get("auth") == "IAM"
                         else "AgentCore::OAuthUser"))
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
            gateway_id, len(qualified_tools), expected_tool_count,
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
        res = (
            f'resource == AgentCore::Gateway::"{gateway_arn}"'
            if gateway_arn else "resource is AgentCore::Gateway"
        )
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
                action_head = (
                    f"action in [{action_list}]" if len(permitted) > 1
                    else f"action == {action_list}"
                )
                desc = "Bug 134: permit the principal to discover + call the allowed tools"
                if forbidden:
                    desc += f" (denied by omission, default-deny: {sorted(forbidden)})"
                policies.insert(0, {
                    "name": "allow_permitted_tools",
                    "description": desc + ".",
                    "statement": f"permit(principal is {principal_type}, {action_head}, {res});",
                })
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
            pol_name = f"{engine_name}_{base_name}"[:128]
            try:
                cp = agentcore_ctrl.create_policy(
                    policyEngineId=engine_id,
                    name=pol_name,
                    description=pol.get("description", ""),
                    definition={"cedar": {"statement": pol.get("statement", "permit(principal, action, resource);")}},
                )
                logger.info("Created policy: %s", pol_name)
                created_count += 1
                pid = cp.get("policyId")
                if pid:
                    created_policy_ids.append((pol_name, pid))
            except Exception as e:
                if "ConflictException" in str(e) or "already exists" in str(e):
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
                    raise RuntimeError(
                        f"Policy creation failed for '{pol_name}': {e}. Aborting."
                    ) from e

        # Bug 134: create_policy is ASYNC — it returns CREATING then validates
        # against the gateway schema. A policy that references a non-existent tool,
        # is overly permissive (unconstrained action), or overly restrictive (lone
        # forbid) ends CREATE_FAILED. Poll every created policy to a terminal state
        # and FAIL the deploy if any did not reach ACTIVE — otherwise we'd attach an
        # engine in ENFORCE that silently denies the tool plane (the original bug).
        is_enforce = policy_config.get("mode", "ENFORCE") == "ENFORCE"
        if is_enforce and created_policy_ids:
            import time as _t
            failed = []
            for pol_name, pid in created_policy_ids:
                status = "CREATING"
                for _ in range(20):
                    try:
                        d = agentcore_ctrl.get_policy(policyEngineId=engine_id, policyId=pid)
                        status = d.get("status", "")
                        if status in ("ACTIVE", "CREATE_FAILED", "FAILED"):
                            if status != "ACTIVE":
                                failed.append((pol_name, str(d.get("statusReasons") or d.get("statusReason") or "")[:200]))
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    _t.sleep(3)
            if failed:
                raise RuntimeError(
                    "Cedar policy validation failed in ENFORCE mode (would deny the "
                    "tool plane): " + "; ".join(f"{n}: {r}" for n, r in failed)
                )

        if is_enforce and created_count == 0:
            raise RuntimeError(
                "ENFORCE mode requested but no Cedar policies were created — "
                "the gateway would deny all tools. Aborting."
            )

        # Bug 137 backstop (authoritative): before attaching in ENFORCE, confirm the
        # engine ACTUALLY HOLDS >=1 ACTIVE policy by reading it back from the service.
        # created_count is a client-side intent counter; this checks ground truth and
        # catches ANY path (name collision, async drop, eventual-consistency) that
        # would otherwise ship an empty deny-all engine that serves 0 tools at runtime.
        if is_enforce:
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
                raise RuntimeError(
                    f"ENFORCE engine {engine_id} holds 0 ACTIVE policies after creation "
                    "— it would deny ALL tools (default-deny). Refusing to attach a "
                    "deny-all engine. This is the Bug 137 empty-engine trap."
                )
            logger.info("Engine %s holds %d ACTIVE policies — safe to attach in ENFORCE",
                        engine_id, active_on_engine)

        # Attach policy engine to gateway.
        # Bug 134 (proper fix): ENFORCE now works because the baseline permit +
        # schema-correct principal/action let the principal discover + call tools
        # while forbid rules still block. So ENFORCE is the default (real
        # enforcement). LOG_ONLY remains available for audit-only dry-runs.
        mode = policy_config.get("mode", "ENFORCE")
        if mode not in ("LOG_ONLY", "ENFORCE"):
            mode = "ENFORCE"
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

        return {
            **event,
            "policy_result": {
                "success": True,
                "engine_id": engine_id,
                "engine_arn": engine_arn,
                "engine_name": engine_name,
                "mode": mode,
            },
        }

    except Exception:
        logger.exception("Policy step failed for deployment %s", deployment_id)
        raise
