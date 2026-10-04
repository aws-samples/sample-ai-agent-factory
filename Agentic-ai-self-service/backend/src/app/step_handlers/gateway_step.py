"""Step handler: Deploy MCP Gateway via boto3.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import copy
import logging
import os
import uuid

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import GATEWAY_GRAPH_FIELD, DeploymentStateStore
from app.services.failure_inventory import GatewayRefusedBeforeSideEffects, StepFailedWithUnrecordedRows
from app.services.gateway_deployer import (
    ConnectorSecretDeletionRefused,
    _pool_region,
    bind_connector_secret_for_deployment,
    connector_identity_mode,
    custom_tool_manifest_row,
    delete_deployment_bound_secret,
    deploy_gateway,
    gateway_aws_session,
    is_shared_tool_function,
    manifest_secret_journal,
    secret_intent_journal,
)
from app.services.gateway_name_claim import claim_account, claims_from_env
from app.services.litellm_gateway_deployer import deploy_litellm_gateway, resolve_gateway_provider

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _record_gateway_resources(
    store: DeploymentStateStore,
    deployment_id: str,
    region: str,
    gateway_result: dict,
    skip_secret_arns: set[str] | None = None,
) -> None:
    """Append every AWS sub-resource ``deploy_gateway`` created to the manifest.

    All best-effort (record_resource swallows its own errors). Recorded TYPE
    strings match the _delete_managed_resource dispatcher exactly:
    gateway / cognito_user_pool / cognito_app_client / cognito_resource_server /
    lambda / iam_role / secret / api_key_credential_provider /
    oauth2_credential_provider / litellm_gateway (informational; deletes nothing) /
    gateway_target (F-74b: provenance only for now, deletes nothing — see the note
    at its loop for why the rows land a release before the delete arm does).
    """

    def _rec(resource: dict, *, created_by_deployment: bool) -> None:
        # Most resources live with the target deployment. Shared Cognito pool
        # children are the exception: the warm pool belongs to the platform stack's
        # home region, and their producer supplies that region explicitly.
        resource.setdefault("region", region)
        resource["created_by_deployment"] = created_by_deployment
        if resource.get("type") != "gateway":
            # Teardown leaves this whole graph in place while its gateway stands.
            resource[GATEWAY_GRAPH_FIELD] = True
        store.record_resource(deployment_id, resource)

    gw_id = gateway_result.get("gateway_id")
    if gw_id:
        _rec(
            {
                "type": "gateway",
                "id": gw_id,
                # The key of the gateway-name claim, which teardown holds from
                # before it decides anything and erases once the graph is gone.
                "name": gateway_result.get("gateway_name") or "",
            },
            created_by_deployment=(gateway_result.get("gateway_created_by_deployment") is True),
        )

    gw_name = gateway_result.get("gateway_name")

    # A LiteLLM gateway is the CUSTOMER's own proxy, so record one informational
    # row naming what the deploy pointed at. It deletes nothing — see the
    # "litellm_gateway" arm in _delete_managed_resource. The rows further down are
    # already no-ops for LiteLLM (no pool_id, no tool Lambdas), except the
    # virtual-key secret, which is a normal "secret" row we do still want — hence no
    # early return.
    if gateway_result.get("gateway_provider") == "litellm":
        base_url = gateway_result.get("litellm_base_url")
        if base_url:
            entry = {"type": "litellm_gateway", "id": base_url}
            if gw_name:
                entry["name"] = gw_name
            _rec(entry, created_by_deployment=False)
    else:
        # The gateway's own execution role. Taken from the field deploy_gateway sets
        # only once IAM has confirmed the role, NOT derived from gw_name: a deploy
        # that fails before Step 1b has a gateway_name (it is built at the top of the
        # function) and no role, and deriving the row from the name wrote one for a
        # role that was never created — measured live on 2026-09-21, see the note at
        # gw_role_confirmed in gateway_deployer. That is also why the LiteLLM branch
        # above no longer needs to suppress this row: LiteLLM never reaches Step 1b,
        # so the field is absent and there is nothing to suppress.
        gw_role_name = gateway_result.get("gateway_role_name")
        if gw_role_name:
            _rec(
                {
                    "type": "iam_role",
                    "name": gw_role_name,
                },
                created_by_deployment=(gateway_result.get("gateway_role_created_by_deployment") is True),
            )

    # The Cognito pool fronting the gateway's CUSTOM_JWT auth.
    client_info = gateway_result.get("client_info") or {}
    pool_id = client_info.get("user_pool_id")
    if pool_id and not client_info.get("shared_pool"):
        # Only a pool THIS deployment created is recorded as deletable. When the
        # platform provides the shared gateway-auth pool, the pool and its hosted
        # domain are platform-owned (RETAIN in the CDK stack) and hold the app
        # client of every gateway in the account — recording it here would let a
        # single agent's DELETE revoke every other agent's gateway access and force
        # a >381s domain reprovision.
        _rec(
            {"type": "cognito_user_pool", "id": pool_id},
            created_by_deployment=True,
        )
    elif pool_id:
        pool_region = client_info.get("user_pool_region") or _pool_region(pool_id)
        # Shared pool: the pool survives, so this gateway's OWN app client is the
        # credential teardown has to revoke, and it needs its own manifest row.
        #
        # The comment that used to sit here said cleanup_gateway_resources handled
        # it. It does — and the caller gates that entire function on
        # `not manifest_used` (deployment_handler.py, "Step 1"), so on every deploy
        # that wrote a manifest it never ran. Confirmed live: a torn-down
        # shared-pool deployment left its app client and resource server behind in
        # the platform pool, with the client's secret still mintable. The app
        # client is per-deploy (create_user_pool_client always creates a new one),
        # so deleting it by id revokes exactly this gateway.
        #
        # Each row is gated on its own handle, not the resource server on the
        # client: a failed Cognito rollback can delete the client and leave the
        # resource server, and the rollback's leftover inventory then names the
        # scope alone.
        if client_info.get("client_id"):
            _rec(
                {
                    "type": "cognito_app_client",
                    "id": client_info["client_id"],
                    "pool_id": pool_id,
                    "region": pool_region,
                },
                created_by_deployment=True,
            )

        # The resource server (`agentcore-<gateway>`) gets its own row, deleted
        # AFTER the client. It holds no credential — it is a scope definition — but
        # it accumulates in the shared pool forever, and its identifier is derived
        # from the gateway NAME, which is not proof of ownership (F-7):
        # create_resource_server treats AlreadyExists as success, so two deployments
        # that picked the same gateway name SHARE one resource server and deleting it
        # would revoke the co-resident gateway's scope.
        #
        # The teardown arm therefore proves co-residency first, and it does so with
        # ListUserPoolClients, which returns ids and NAMES ONLY — never a client
        # secret. DescribeUserPoolClient would answer the question directly, but the
        # only roles holding it are the deploy-time ones, on the shared pool's whole
        # ARN (infra/stacks/platform/cognito_client_secret_grant.py); granting it to
        # a teardown role would hand teardown the ability to read every gateway's
        # client secret to decide a namespace delete. Names are enough because the
        # scope and the client name come from the same `gateway_name` in one function
        # (gateway_deployer ~:1585): resource server `agentcore-X`, scope
        # `agentcore-X/invoke`, client `X-client`.
        #
        # The identifier is taken from the SCOPE the client was actually granted
        # rather than re-derived from the gateway name, for the same reason
        # cleanup_gateway_resources does it that way: the scope cannot disagree with
        # what was created.
        _rs_id = str(client_info.get("scope") or "").split("/", 1)[0]
        if _rs_id:
            _rec(
                {
                    "type": "cognito_resource_server",
                    "id": _rs_id,
                    "pool_id": pool_id,
                    "region": pool_region,
                },
                created_by_deployment=True,
            )

    # Tool Lambdas + their exec roles (built-in dynamic-tools / customer-support,
    # KB query tool, and per-custom-tool lambdas/roles). SHARED singleton tool
    # Lambdas (AgentCoreDynamicTools / AgentCoreCustomerSupportTools) are reused
    # by every gateway, so their manifest entry also records WHICH gateway role
    # owns this deployment's invoke grant — the teardown dispatchers use it to
    # release the Lambda by reference count instead of hard-deleting it out from
    # under other live gateways (Defect C, manifest path).
    gateway_role_name = gateway_result.get("gateway_role_name")
    if not gateway_role_name and gateway_result.get("gateway_name"):
        # Compatibility with deployment results written before the producer
        # returned the exact role name. Those builds used the same unsuffixed
        # account-global role name in every region. New results always take the
        # exact acknowledged name above, including the regional discriminator.
        gateway_role_name = f"AgentCoreGateway-{gateway_result['gateway_name']}"
    for fn in [gateway_result.get("lambda_function_name"), gateway_result.get("kb_lambda_name")]:
        if fn:
            entry = {"type": "lambda", "name": fn}
            if is_shared_tool_function(fn) and gateway_role_name:
                entry["gateway_role"] = gateway_role_name
            _rec(entry, created_by_deployment=True)
    # F-7d: every custom-tool row carries the exact owner+gateway scope binding its function
    # and role were tagged with, and a role row names its paired function, so each teardown
    # dispatcher can REQUIRE the binding (and take the function's lock) before it mutates.
    bindings = gateway_result.get("custom_tool_bindings") or {}
    pairs = gateway_result.get("custom_tool_pairs") or {}
    for fn in gateway_result.get("custom_tool_lambdas") or []:
        if fn:
            _rec(custom_tool_manifest_row("lambda", fn, bindings, pairs), created_by_deployment=True)
    for role_name in gateway_result.get("custom_tool_roles") or []:
        if role_name:
            _rec(custom_tool_manifest_row("iam_role", role_name, bindings, pairs), created_by_deployment=True)
    # F-66: a redeploy onto the gateway reuses its custom-tool function and role. The
    # row is what keeps them: teardown of the deployment that created them sees this
    # live reference and retains them, and the last deployment on the gateway reclaims.
    #
    # F-74c: built by the SAME helper as the created rows, because the reclaim above is
    # exactly what an adopted row has to be able to perform. These were bare type+name, so
    # the reclaiming teardown held no ToolScope and refused to delete on the name alone —
    # every redeploy leaked its custom-tool Lambda and IAM role.
    for fn in gateway_result.get("custom_tool_lambdas_adopted") or []:
        if fn:
            _rec(custom_tool_manifest_row("lambda", fn, bindings, pairs), created_by_deployment=False)
    for role_name in gateway_result.get("custom_tool_roles_adopted") or []:
        if role_name:
            _rec(custom_tool_manifest_row("iam_role", role_name, bindings, pairs), created_by_deployment=False)

    # The gateway's own OAuth client secret, moved out of Cognito into a
    # per-deployment secret so the runtime needs no pool-wide DescribeUserPoolClient
    # grant (gateway_deployer._mint_client_secret_ref). Recorded here and not beside
    # the pool row above because it must be deleted on BOTH pool paths — in shared
    # mode the pool itself is deliberately not recorded, and the secret is the one
    # thing that deploy created in that case which teardown would otherwise leave.
    #
    # Read from `minted_client_secret_ref`, NOT `client_secret_ref`. Both hold the same
    # ARN on the Cognito paths, but on the external-IDP path `client_secret_ref` is the
    # CUSTOMER's OAuth client secret, copied out of their identity_config — the
    # platform created nothing there. The teardown arm for a `secret` row is
    # delete_secret(..., ForceDeleteWithoutRecovery=True) with no ownership check, so
    # this row used to mean: deleting one agent irrecoverably destroys the IDP
    # credential every other agent on that provider is using. Measured live on
    # 2026-09-21 — a probe deploy against a fake external IDP recorded
    # {"type": "secret", "id": "agentcore-gateway/f6pb2-does-not-exist"} for a
    # reference the platform had never created.
    #
    # Same principle as the gateway role above: the producer states what it created;
    # the manifest never infers ownership from a name.
    skip_secret_arns = skip_secret_arns or set()
    cs_ref = client_info.get("minted_client_secret_ref")
    if cs_ref and cs_ref not in skip_secret_arns:
        _rec(
            {"type": "secret", "id": cs_ref},
            created_by_deployment=True,
        )

    # Per-connector Secrets Manager secrets (hold the raw credential).
    for secret_arn in gateway_result.get("connector_secret_arns") or []:
        if secret_arn and secret_arn not in skip_secret_arns:
            _rec(
                {"type": "secret", "id": secret_arn},
                created_by_deployment=True,
            )

    # Per-connector credential providers. deploy_gateway records each as
    # "TYPE:name" (TYPE in {OAUTH, API_KEY}) so we route to the correct deleter.
    for entry in gateway_result.get("connector_credential_providers") or []:
        if not entry:
            continue
        kind, _, prov_name = str(entry).partition(":")
        if not prov_name:
            # Legacy bare name (no type prefix). The recorded type is a hint only:
            # teardown purges BOTH namespaces for either row type, because the two
            # deleters silently no-op on each other's providers.
            kind, prov_name = "OAUTH", str(entry)
        res_type = "api_key_credential_provider" if kind.upper() == "API_KEY" else "oauth2_credential_provider"
        _rec(
            {"type": res_type, "name": prov_name},
            created_by_deployment=True,
        )

    # Staged OpenAPI spec objects (large connector specs routed to S3, not inline).
    for uri in gateway_result.get("connector_spec_s3_uris") or []:
        if uri:
            _rec(
                {"type": "s3_object", "id": uri},
                created_by_deployment=True,
            )

    # F-74b: one row per gateway target this deploy created, reused or updated.
    #
    # The "gateway_target" arm in _delete_managed_resource (and its mirror in
    # status_update_step) deletes NOTHING: teardown behaves exactly as it does today, in
    # that the "gateway" arm deletes every target on the gateway before deleting the
    # gateway, and a gateway retained for a co-resident deployment keeps its targets.
    # That is a leak (this deployment's target outlives it on a shared gateway), not a
    # destruction, and it is the state we are already in. Writing the rows first is what
    # makes the fix possible: a refcounted per-target delete cannot be written before
    # there is durable evidence of WHICH deployment asked for WHICH target, and adding
    # the delete arm in the same change as the rows that feed it would mean the very
    # first teardown after the deploy ran against a population where almost every target
    # has no row -- and a delete that cannot see a co-resident's reference deletes it.
    #
    # The arm still has to EXIST, though, and an earlier version of this comment was wrong
    # about why: an unrecognized row type is not a silent no-op. The dispatcher logs an
    # ERROR and tells the operator the resource is "still in the account ... delete it by
    # hand", so rows with no arm would report a leak on every teardown of every
    # gateway-bearing deployment, for targets that were in fact just deleted.
    #
    # ``digest`` is over exactly the fields an UpdateGatewayTarget replaces, so two
    # deployments asking for the same target name can be told apart from two asking for
    # the same target. It is never computed from a control-plane read; see
    # gateway_deployer.target_replace_digest.
    for record in gateway_result.get("gateway_targets") or []:
        target_id = (record or {}).get("target_id")
        if not target_id:
            continue
        arm = record.get("arm") or "created"
        entry = {
            "type": "gateway_target",
            "id": target_id,
            "name": record.get("name") or "",
            "gateway_id": gateway_result.get("gateway_id") or "",
            "target_family": record.get("family") or "",
            "target_digest": record.get("digest") or "",
            # Provenance (Stage80 attribution): how this deployment came to hold the target and,
            # for MCP-runtime targets, exactly which runtime it fronts.
            "target_arm": arm,
            "source_runtime_arn": record.get("source_runtime_arn") or "",
            "source_runtime_id": record.get("source_runtime_id") or "",
        }
        # A target is unconditionally ours when we created it, or when it sits on a gateway we
        # created (an "already exists" there can only be our own earlier attempt). A target
        # adopted or updated on a REUSED gateway existed before us: recorded, never deleted by us.
        gateway_ours = gateway_result.get("gateway_created_by_deployment") is True
        _rec(entry, created_by_deployment=(arm == "created" or gateway_ours))


class _KeepingUnrecordedRows:
    """``record_resource`` semantics, keeping every row DynamoDB refused.

    The durability marker is still written (finalization must see the manifest is
    incomplete), but a marker that also fails does not raise here: the step is already
    failing, and the rows it carries are the only copy left.
    """

    def __init__(self, store: DeploymentStateStore):
        self._store = store
        self.unrecorded: list[dict] = []
        #: Rows DynamoDB acknowledged: the only inventory a later DELETE can read.
        self.acked: list[dict] = []

    def record_resource(self, deployment_id: str, resource: dict) -> None:
        try:
            self._store.record_resource_strict(deployment_id, resource)
        except (TypeError, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_resource failed for %s (carried out): err=%s", deployment_id, type(exc).__name__)
            self.unrecorded.append(dict(resource))
            try:
                self._store.mark_resource_manifest_error(deployment_id)
            except Exception as marker_exc:  # noqa: BLE001
                logger.error(
                    "Could not durably mark resource-manifest failure for %s: %s",
                    deployment_id,
                    type(marker_exc).__name__,
                )
        else:
            self.acked.append(dict(resource))


class _AckingRecorder:
    """``record_resource`` semantics, and which rows DynamoDB acknowledged."""

    def __init__(self, store: DeploymentStateStore):
        self._store = store
        self.acked: list[dict] = []
        #: Rows DynamoDB refused, even if the best-effort retry then landed them.
        self.unrecorded: list[dict] = []

    def record_resource(self, deployment_id: str, resource: dict) -> None:
        try:
            self._store.record_resource_strict(deployment_id, resource)
        except (TypeError, ValueError):
            raise
        except Exception:  # noqa: BLE001
            self.unrecorded.append(dict(resource))
            # The store's own best-effort append: a retry, and the durable marker.
            self._store.record_resource(deployment_id, resource)
        else:
            self.acked.append(dict(resource))


def _names_a_gateway_graph(rows: list[dict]) -> bool:
    """Whether *rows* name a gateway, its role or its resource server: the durable
    evidence that makes a gateway name's claim worth keeping (F-66f)."""
    for row in rows:
        rtype, rid = row.get("type"), str(row.get("name") or row.get("id") or "")
        if rtype == "gateway" and row.get("id"):
            return True
        if rtype == "iam_role" and rid.startswith("AgentCoreGateway-"):
            return True
        if rtype == "cognito_resource_server" and rid.startswith("agentcore-"):
            return True
    return False


def _gateway_manifest_resources(
    region: str,
    gateway_result: dict,
    skip_secret_arns: set[str] | None = None,
) -> list[dict]:
    """Return the exact rows ``_record_gateway_resources`` would append.

    Failure finalization uses this pure view to recover from a best-effort
    DynamoDB append that was durably marked incomplete. Keeping one producer of
    the row shapes prevents compensation cleanup from drifting from deployment.
    """
    rows: list[dict] = []

    class _Collector:
        @staticmethod
        def record_resource(_deployment_id: str, resource: dict) -> None:
            rows.append(dict(resource))

    _record_gateway_resources(
        _Collector(),
        "manifest-preview",
        region,
        gateway_result,
        skip_secret_arns=skip_secret_arns,
    )
    return rows


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.GATEWAY, DeploymentStatusEnum.IN_PROGRESS)
        # Every secret this step may create is named in the manifest before it exists.
        _journal = manifest_secret_journal(
            store, deployment_id, event.get("target_account_id"), extra={GATEWAY_GRAPH_FIELD: True}
        )

        gateway_config = copy.deepcopy(event.get("gateway_config") or {})
        region = event.get("target_region") or _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        template_id = event.get("template_id")
        gateway_tools = event.get("gateway_tools") or []
        identity_config = event.get("identity_config") or {}
        custom_tools = event.get("custom_tools") or []
        connectors = copy.deepcopy(event.get("connectors") or [])
        external_mcp_servers = copy.deepcopy(event.get("external_mcp_servers") or [])
        owner_sub = event.get("owner_sub") or ""
        # P0-B governance tags, resolved once per deploy by `tag_policy_store.resolve_governance`
        # and carried in the Step Functions state. This step creates the widest set of billable
        # resources of any step -- gateway, Cognito pool, IAM roles, tool Lambdas, credential
        # providers, connector secrets -- so it is where cost attribution and ABAC (ARCC
        # cnt_6gBImtb08AJqCB) matter most. The `governed_*` helpers inside gateway_deployer
        # validate the set against the namespaces this step's role may stamp BEFORE the first
        # create call, so an unstampable key fails the step instead of stranding half a gateway.
        resource_tags = event.get("resource_tags")
        artifact_bucket = step_clients.artifacts_bucket_for_event(
            event,
            platform_bucket=_get_env("ARTIFACTS_BUCKET_NAME", ""),
        )
        artifact_bucket_owner = (
            str(event["target_account_id"])
            if event.get("target_account_id")
            else (_get_env("AWS_ACCOUNT_ID", "") or None)
        )
        target_session = step_clients.session_for_event(event)
        secrets_client = target_session.client("secretsmanager", region_name=region)
        recorded_secret_arns = list(event.get("recorded_secret_arns") or [])

        def _bind_and_record(
            *,
            payload_key: str,
            raw_value: str | None,
            secret_ref: str | None,
        ) -> str:
            with secret_intent_journal(_journal), connector_identity_mode(identity_config):
                arn, _created = bind_connector_secret_for_deployment(
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    payload_key=payload_key,
                    raw_value=raw_value,
                    secret_ref=secret_ref,
                    secrets_client=secrets_client,
                    resource_tags=resource_tags,
                )
            if arn not in recorded_secret_arns:
                row = {
                    "type": "secret",
                    "id": arn,
                    "region": region,
                    "created_by_deployment": True,
                    GATEWAY_GRAPH_FIELD: True,
                }
                if event.get("target_account_id"):
                    row["account"] = event["target_account_id"]
                try:
                    store.record_resource_strict(deployment_id, row)
                except Exception:
                    # Never advance SFN with a live credential whose teardown
                    # handle failed to persist.
                    try:
                        delete_deployment_bound_secret(
                            region=region,
                            deployment_id=deployment_id,
                            secret_ref=arn,
                            secrets_client=secrets_client,
                        )
                    except ConnectorSecretDeletionRefused:
                        logger.error("Credential rollback refused because exact ownership was not proven")
                    except Exception as cleanup_exc:  # noqa: BLE001
                        logger.error("Gateway rollback failed: %s", type(cleanup_exc).__name__)
                    raise
                recorded_secret_arns.append(arn)
            return arn

        # Legacy/direct callers may still arrive with plaintext or an older
        # reference. Bind every shape and unconditionally remove the raw fields
        # before this step re-emits the event into SFN history.
        for connector in connectors:
            raw = connector.pop("secret_value", None)
            raw_alias = connector.pop("secretValue", None)
            ref = connector.get("secret_arn") or connector.get("secretArn")
            if raw or raw_alias or ref:
                payload_key = (
                    "clientSecret"
                    if (connector.get("auth_method") or connector.get("authMethod")) == "oauth2_cc"
                    else "apiKey"
                )
                connector["secret_arn"] = _bind_and_record(
                    payload_key=payload_key,
                    raw_value=raw or raw_alias,
                    secret_ref=ref,
                )
                connector.pop("secretArn", None)

        for selection in external_mcp_servers:
            raw = selection.pop("secret_value", None)
            raw_alias = selection.pop("secretValue", None)
            ref = selection.get("secret_arn") or selection.get("secretArn")
            if raw or raw_alias or ref:
                selection["secret_arn"] = _bind_and_record(
                    payload_key="apiKey",
                    raw_value=raw or raw_alias,
                    secret_ref=ref,
                )
                selection.pop("secretArn", None)

            oauth = copy.deepcopy(selection.get("oauth") or {})
            oauth_raw = oauth.pop("client_secret", None)
            oauth_raw_alias = oauth.pop("clientSecret", None)
            oauth_ref = (
                oauth.get("client_secret_arn")
                or oauth.get("clientSecretArn")
                or oauth.get("client_secret_ref")
                or oauth.get("clientSecretRef")
            )
            if oauth_raw or oauth_raw_alias or oauth_ref:
                oauth["client_secret_arn"] = _bind_and_record(
                    payload_key="clientSecret",
                    raw_value=oauth_raw or oauth_raw_alias,
                    secret_ref=oauth_ref,
                )
                for alias in ("clientSecretArn", "client_secret_ref", "clientSecretRef"):
                    oauth.pop(alias, None)
                selection["oauth"] = oauth

        litellm_raw = gateway_config.pop("litellm_api_key", None)
        litellm_raw_alias = gateway_config.pop("litellmApiKey", None)
        litellm_ref = gateway_config.get("litellm_api_key_ref") or gateway_config.get("litellmApiKeyRef")
        if litellm_raw or litellm_raw_alias or litellm_ref:
            gateway_config["litellm_api_key_ref"] = _bind_and_record(
                payload_key="apiKey",
                raw_value=litellm_raw or litellm_raw_alias,
                secret_ref=litellm_ref,
            )
            gateway_config.pop("litellmApiKeyRef", None)

        mcp_server_runtime_arn = event.get("mcp_server_runtime_arn")
        mcp_oauth = copy.deepcopy(event.get("mcp_oauth") or {})
        if mcp_oauth:
            mcp_raw = mcp_oauth.pop("client_secret", None)
            mcp_raw_alias = mcp_oauth.pop("clientSecret", None)
            mcp_ref = (
                mcp_oauth.get("client_secret_ref")
                or mcp_oauth.get("clientSecretRef")
                or mcp_oauth.get("client_secret_arn")
                or mcp_oauth.get("clientSecretArn")
            )
            if mcp_raw or mcp_raw_alias or mcp_ref:
                mcp_oauth["client_secret_ref"] = _bind_and_record(
                    payload_key="clientSecret",
                    raw_value=mcp_raw or mcp_raw_alias,
                    secret_ref=mcp_ref,
                )
                for alias in ("clientSecretRef", "client_secret_arn", "clientSecretArn"):
                    mcp_oauth.pop(alias, None)

        knowledge_base_result = event.get("knowledge_base_result") or {}

        # Provider dispatch (Workstream A). AgentCore is the default and its call
        # below is untouched. A LiteLLM gateway is a customer-run proxy: nothing
        # is created in AWS beyond the virtual-key secret, and the return contract
        # is identical, which is why the state machine needs no new branch.
        gateway_provider = resolve_gateway_provider(gateway_config)
        if gateway_provider == "litellm":
            # The key was bound above, so this only reuses it; the journal is there so
            # a create added on this path later is named before it exists.
            with (
                gateway_aws_session(
                    target_session,
                    artifact_bucket=artifact_bucket,
                    expected_bucket_owner=artifact_bucket_owner,
                ),
                secret_intent_journal(_journal),
                connector_identity_mode(identity_config),
            ):
                litellm_result = deploy_litellm_gateway(
                    gateway_config=gateway_config,
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id if deployment_id else None,
                    secrets_client=secrets_client,
                    resource_tags=resource_tags,
                )
            if not litellm_result.get("success"):
                raise RuntimeError(f"Gateway deployment failed: {litellm_result.get('error', 'unknown error')}")

            # Persist only the ARN back onto the config so a redeploy reuses it.
            key_ref = (litellm_result.get("client_info") or {}).get("api_key_ref")
            if key_ref:
                gateway_config["litellm_api_key_ref"] = key_ref

            # Manifest: the same recorder the AgentCore branch runs below. This branch
            # returned before it, so the informational ``litellm_gateway`` row (and any
            # secret row the deployer minted) was never written; once the completeness
            # gate in status_update_step began demanding that row, every LiteLLM deploy
            # failed with "missing litellm_gateway teardown handle" (live, 2026-09-30,
            # the first LiteLLM deploy to reach the gate).
            acking = _AckingRecorder(store)
            _record_gateway_resources(
                acking,
                deployment_id,
                region,
                litellm_result,
                skip_secret_arns=set(recorded_secret_arns),
            )
            for row in acking.acked:
                if row.get("type") == "secret":
                    arn = row.get("id") or row.get("name")
                    if arn and arn not in recorded_secret_arns:
                        recorded_secret_arns.append(arn)

            return {
                **event,
                "gateway_config": gateway_config,
                "connectors": connectors,
                "external_mcp_servers": external_mcp_servers,
                "mcp_oauth": mcp_oauth or None,
                "recorded_secret_arns": recorded_secret_arns,
                "gateway_result": litellm_result,
            }

        def _gateway_consumers(gateway_id: str, pool_id: str) -> list[dict]:
            # The OTHER live deployments on an existing gateway, for the adoption
            # gate (F-63). A read of the platform table, so it uses the store, not
            # the target session.
            return _get_deployment_store().live_gateway_consumers(
                deployment_id,
                gateway_id,
                pool_id=pool_id,
                target_account_id=event.get("target_account_id"),
                target_region=region,
            )

        # The gateway-name claim (services/gateway_name_claim), in the PLATFORM table.
        # deploy_gateway calls this with the normalized name before its first Cognito
        # or IAM call, so anything raised here (a refusal, or no table configured)
        # fails the deploy with nothing created. The lease is released below, only
        # once the manifest names what this deploy created.
        _claimed: dict = {}
        # This invocation's fence on the name, the same across deploy_gateway's own
        # retries. Never the deployment id: a Step Functions retry or a duplicate
        # delivery of this step reuses that while this invocation may still run.
        claim_token = uuid.uuid4().hex

        def _claim_gateway_name(gateway_name: str) -> None:
            claims = claims_from_env()
            account = claim_account(event.get("target_account_id"), target_session.client("sts"))
            provisional = claims.acquire(
                account=account,
                region=region,
                name=gateway_name,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                token=claim_token,
            )
            _claimed.update(claims=claims, account=account, name=gateway_name, provisional=provisional)

        def _release_gateway_name(
            acked_rows: list[dict],
            unrecorded_rows: list[dict] = (),
            live_gateway_id: str | None = None,
            carried_rows: list[dict] = (),
        ) -> str | None:
            # A new claim is provisional: it is kept only once a row DynamoDB
            # acknowledged names a gateway graph on the name, the handle a later
            # DELETE needs to erase it. With none (a validation failure) it is
            # abandoned, and if that write is lost too it expires.
            #
            # A gateway this deploy created and left live (*live_gateway_id*) keeps the
            # claim even when its row never landed: abandoning it would free the name,
            # and with it the next deploy's adoption of a gateway nothing records. The
            # claim then records the gateway itself, the handle nothing else holds.
            #
            # Rows that name a graph but never landed on a FAILED deploy are carried
            # out to failure cleanup instead, and so is the lease, for the same reason:
            # the cleanup proves what still stands. Returns the token handed over.
            #
            # If the claim cannot record it either (its write failed, or the lease was
            # lost), the gateway is recorded nowhere: the step fails and carries it,
            # with *carried_rows*, to failure cleanup, the one pass that still knows it.
            #
            # Otherwise best-effort: an unreleased lease only holds this owner's next
            # deploy of the name until it expires, which is no reason to fail this one.
            if not _claimed:
                return None
            where = {"account": _claimed["account"], "region": region, "name": _claimed["name"]}
            if _claimed["provisional"]:
                keep = _names_a_gateway_graph(acked_rows)
                recovery = None
                if not keep and live_gateway_id:
                    keep = True
                    recovery = {"recovery_gateway_id": live_gateway_id, "recovery_deployment_id": deployment_id}
                    logger.error(
                        "Gateway %s is live and no manifest row for it landed; its name claim records it",
                        live_gateway_id,
                    )
                elif not keep and _names_a_gateway_graph(list(unrecorded_rows)):
                    return claim_token
                landed = _claimed["claims"].settle_provisional(
                    **where, owner_sub=owner_sub, token=claim_token, keep=keep, recovery=recovery
                )
                if recovery is not None and not landed:
                    carried = [dict(r) for r in carried_rows]
                    if not any(r.get("type") == "gateway" and r.get("id") == live_gateway_id for r in carried):
                        carried.append(
                            {"type": "gateway", "id": live_gateway_id, "name": _claimed["name"], "region": region}
                        )
                    raise StepFailedWithUnrecordedRows(
                        f"Gateway {live_gateway_id} is live and neither its manifest row nor its name claim "
                        "could record it; failing so cleanup removes it",
                        carried,
                        claim_token=claim_token,
                    )
                return None
            # A durable claim may carry an earlier deployment's recovery evidence; a
            # gateway this deploy's acknowledged rows record is no longer that one's.
            recorded = tuple(
                str(r["id"]) for r in acked_rows if isinstance(r, dict) and r.get("type") == "gateway" and r.get("id")
            )
            try:
                _claimed["claims"].release(**where, token=claim_token, recorded_gateways=recorded)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gateway name lease for %s not released: %s", _claimed["name"], type(exc).__name__)
            return None

        with (
            gateway_aws_session(
                target_session,
                artifact_bucket=artifact_bucket,
                expected_bucket_owner=artifact_bucket_owner,
            ),
            secret_intent_journal(_journal),
            connector_identity_mode(identity_config),
        ):
            gateway_result = deploy_gateway(
                gateway_config=gateway_config,
                region=region,
                template_id=template_id,
                gateway_tools=gateway_tools,
                identity_config=identity_config,
                custom_tools=custom_tools,
                connectors=connectors,
                external_mcp_servers=external_mcp_servers,
                owner_sub=owner_sub,
                mcp_server_runtime_arn=mcp_server_runtime_arn,
                mcp_oauth=mcp_oauth or None,
                knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
                deployment_id=deployment_id if deployment_id else None,
                secrets_prebound=True,
                gateway_consumers=_gateway_consumers,
                claim_gateway_name=_claim_gateway_name,
                resource_tags=resource_tags,
            )

        if not gateway_result.get("success"):
            # Record the partial inventory BEFORE raising. deploy_gateway tries its
            # own abort cleanup, but that is best-effort and returns per-resource
            # failures it used to discard; when it leaves something behind there is
            # no runtime, so nothing else ever names those resources and they are
            # orphaned permanently. Verified live: a deploy that failed at
            # CreateApiKeyCredentialProvider left an orphan gateway + Cognito pool
            # with created_resources still null. With rows written, the normal
            # manifest-driven teardown cleans them up on a later delete (which
            # accepts a deployment_id precisely for this partial-failure case).
            #
            # A row whose append fails would still be lost: the Catch keeps only this
            # step's input, so the exception carries it out (see failure_inventory).
            recorder = _KeepingUnrecordedRows(store)
            _record_gateway_resources(
                recorder,
                deployment_id,
                region,
                gateway_result,
                skip_secret_arns=set(recorded_secret_arns),
            )
            handed_token = _release_gateway_name(recorder.acked, recorder.unrecorded)
            if gateway_result.get("refused_before_side_effects") is True and not recorder.unrecorded:
                # F-67: tells failure cleanup this step created nothing, so an empty
                # manifest is a certainty and not an unproven absence.
                raise GatewayRefusedBeforeSideEffects(
                    f"Gateway deployment failed: {gateway_result.get('error', 'unknown error')}"
                )
            raise StepFailedWithUnrecordedRows(
                f"Gateway deployment failed: {gateway_result.get('error', 'unknown error')}",
                recorder.unrecorded,
                claim_token=handed_token,
            )

        # Manifest: record every AWS sub-resource deploy_gateway created so the
        # generic teardown path can destroy them even if a later step fails
        # before *_result lands. Best-effort: record_resource never raises into
        # the deploy. Types MUST match _delete_managed_resource's dispatcher.
        acking = _AckingRecorder(store)
        _record_gateway_resources(
            acking,
            deployment_id,
            region,
            gateway_result,
            skip_secret_arns=set(recorded_secret_arns),
        )
        _release_gateway_name(
            acking.acked,
            live_gateway_id=None
            if any(r.get("type") == "gateway" for r in acking.acked)
            else gateway_result.get("gateway_id"),
            carried_rows=acking.unrecorded,
        )

        # Persist connector cleanup handles (provider NAMES + secret ARNs) into
        # the gateway_result that gets written to the deployment record so
        # cleanup.sh can tear down credential providers and secrets later.
        gateway_result["connector_credential_providers"] = gateway_result.get("connector_credential_providers", [])
        gateway_result["connector_secret_arns"] = gateway_result.get("connector_secret_arns", [])
        gateway_result["connector_spec_s3_uris"] = gateway_result.get("connector_spec_s3_uris", [])

        return {
            **event,
            "gateway_config": gateway_config,
            "connectors": connectors,
            "external_mcp_servers": external_mcp_servers,
            "mcp_oauth": mcp_oauth or None,
            "recorded_secret_arns": recorded_secret_arns,
            "gateway_result": gateway_result,
        }

    except Exception:
        logger.exception("Gateway step failed for deployment %s", deployment_id)
        raise
