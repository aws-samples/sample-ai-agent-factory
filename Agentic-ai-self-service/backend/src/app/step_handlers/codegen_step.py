"""Step handler: Generate agent code and upload to S3.

Generates agent code, downloads pre-built dependency bundle from S3,
and merges both into a code.zip. The AgentCore Runtime does NOT install
from requirements.txt — ALL dependencies must be pre-bundled in code.zip.

Requirements: 3.3
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import (
    DeploymentStatusEnum,
    DeploymentStepName,
    RuntimeConfig,
)
from app.services import runtime_artifact, step_clients
from app.services.code_generator import (
    generate_agent_code,
    generate_requirements,
    provider_bundle_keys_for,
)
from app.services.deployment_state_store import DeploymentStateStore
from app.services.runtime_deployer import canvas_model_providers

logger = logging.getLogger(__name__)

# Backward-compatible module exports used by tests and external helpers.
BASE_BUNDLE_KEY = runtime_artifact.BASE_BUNDLE_KEY
STRANDS_BUNDLE_KEY = runtime_artifact.STRANDS_BUNDLE_KEY
MCP_LEAN_BUNDLE_KEY = runtime_artifact.MCP_LEAN_BUNDLE_KEY
classify_runtime_artifact = runtime_artifact.classify_runtime_artifact


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _needs_strands_bundle(agent_code: str) -> bool:
    """Backward-compatible predicate over the shared source classifier."""

    return classify_runtime_artifact(agent_code).kind == "strands"


def _download_bundle(s3_client, bucket: str, bundle_key: str) -> bytes | None:
    """Download pre-built dependency bundle from S3."""
    try:
        logger.info("Downloading dependency bundle s3://%s/%s", bucket, bundle_key)
        resp = s3_client.get_object(Bucket=bucket, Key=bundle_key)
        data = resp["Body"].read()
        logger.info("Downloaded bundle: %d bytes", len(data))
        return data
    except Exception as e:
        logger.warning("Failed to download bundle %s: %s", bundle_key, e)
        return None


def _provider_bundles(deps_s3, platform_bucket: str, config) -> list[bytes]:
    """Every model-provider SDK bundle this canvas needs, in canvas order.

    FAILS THE STEP if one is missing, and that is the whole point. The defect this
    exists to close was a green deploy over a container that could not import — the
    least actionable failure the platform is capable of, because AgentCore reports it
    as a 30-second initialization timeout and says nothing about a module. A codegen
    step that stops and names the absent bundle key trades that for a deploy which
    fails where the cause is.

    Not raising would be the worse of the two available bugs, not the safer one.
    """
    keys = provider_bundle_keys_for(canvas_model_providers(config))
    if not keys:
        return []
    if not platform_bucket:
        raise RuntimeError(
            f"This agent needs the model-provider SDK bundles {keys}, but no platform "
            "artifacts bucket is configured to read them from. Without them the runtime "
            "deploys successfully and the container fails at import."
        )
    bundles: list[bytes] = []
    for key in keys:
        data = _download_bundle(deps_s3, platform_bucket, key)
        if not data:
            raise RuntimeError(
                f"Model-provider dependency bundle s3://{platform_bucket}/{key} is missing "
                "or unreadable. The agent imports a non-Bedrock Strands model provider, "
                "whose SDK is in neither base.zip nor strands-mcp.zip, so deploying "
                "without it produces a runtime that reports SUCCESS and then dies at "
                "container import with ModuleNotFoundError — surfaced only as 'Runtime "
                "initialization time exceeded … 30s'. Build and upload the bundles with "
                "scripts/install-agentcore-deps.sh."
            )
        bundles.append(data)
    return bundles


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.CODEGEN, DeploymentStatusEnum.IN_PROGRESS)

        config_dict = event.get("config", {})
        config = RuntimeConfig.model_validate(config_dict)
        template_id = event.get("template_id")
        connected_tools = event.get("connected_tools") or []
        gateway_config = event.get("gateway_config")
        gateway_tools = event.get("gateway_tools") or []
        custom_tools = event.get("custom_tools") or []
        a2a_config = event.get("a2a_config") or {}
        kb_config = event.get("knowledge_base_config") or {}

        # Merge gateway_result (from gateway step) into gateway_config
        # so code generator gets the real Cognito credentials + gateway URL
        gateway_result = event.get("gateway_result")
        if gateway_result and isinstance(gateway_result, dict):
            if gateway_config is None:
                gateway_config = {}
            if gateway_result.get("gateway_url"):
                gateway_config["gateway_url"] = gateway_result["gateway_url"]
            if gateway_result.get("client_info"):
                gateway_config["client_info"] = gateway_result["client_info"]

        # OTEL is enabled when ANY of:
        #   - platform-level OTEL defaults are configured (Reading A — every
        #     agent inherits the admin-configured backend, even without an
        #     Observability node on the canvas)
        #   - per-canvas Observability node is wired
        #   - observability_config supplied directly
        #   - legacy enable_otel flag set
        from app.services.observability import (
            get_platform_observability_defaults_lenient as get_platform_observability_defaults,
        )

        obs_cfg = event.get("observability_config") or {}
        platform_observability_defaults = (
            event.get("platform_observability_defaults")
            if "platform_observability_defaults" in event
            else get_platform_observability_defaults()
        )
        observability_enabled = bool(
            platform_observability_defaults
            or (isinstance(obs_cfg, dict) and obs_cfg.get("enabled", True) and obs_cfg.get("provider"))
            or "observability" in connected_tools
            or getattr(config, "enable_otel", False)
        )

        agent_code = generate_agent_code(
            config=config,
            tools=connected_tools,
            gateway_config=gateway_config,
            template_id=template_id,
            gateway_tools=gateway_tools,
            custom_tools=custom_tools,
            observability_enabled=observability_enabled,
            a2a_config=a2a_config,
            kb_config=kb_config,
        )
        requirements_txt = generate_requirements(
            config=config,
            tools=connected_tools,
            template_id=template_id,
            gateway_tools=gateway_tools,
        )

        # Upload to S3 using a per-version prefix. Bug 61 originally used a
        # stable prefix keyed on the friendly runtime name to ride out the
        # AgentCore IAM cache, but Bug 63 isolated the real cause to an S3
        # region cache 301 transient that the runtime_deployer retries on
        # _create_with_transient_retry. With versioning (Phase 1 Gap 1A) we
        # keep code.zip per-version so rollback can re-point at a previous
        # version's code without redeploy. The retry budget covers the cache
        # miss on the first deploy of each new version_id.
        from app.services.runtime_deployer import sanitize_runtime_name

        friendly_runtime_name = event.get("friendly_runtime_name") or sanitize_runtime_name(
            config.name or f"agent-{deployment_id[:8]}"
        )
        version_id = event.get("version_id") or ""
        platform_bucket = _get_env("ARTIFACTS_BUCKET_NAME", "")
        region = event.get("target_region") or _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        entrypoint = config.entrypoint or "agent.py"
        if version_id:
            s3_key = f"deployments/by-name/{friendly_runtime_name}/v/{version_id}/code.zip"
        else:
            # Back-compat for any caller that bypasses the deployment handler
            # (e.g. legacy direct deploys). Falls back to the pre-versioning prefix.
            s3_key = f"deployments/by-name/{friendly_runtime_name}/code.zip"

        # Phase 7 (opt-in) cross-account: AgentCore's runtime code-fetch does NOT
        # honor cross-account S3 grants — the runtime must read its code zip from
        # a bucket IN ITS OWN account. So a cross-account deploy uploads the final
        # code.zip to a PRE-PROVISIONED bucket in the TARGET account
        # (validated at account registration), while the dependency BUNDLE is
        # still read from the platform bucket with the HOME session.
        # Same-account is unchanged: everything uses the platform bucket.
        _target_account = event.get("target_account_id")
        upload_bucket = step_clients.artifacts_bucket_for_event(
            event,
            platform_bucket=platform_bucket,
        )

        # Select from the generated imports, not the caller-controlled template
        # id. A standalone FastMCP module needs mcp-lean.zip; base.zip does not
        # contain mcp and would fail only after AgentCore reported deployment
        # success.
        runtime_artifact = classify_runtime_artifact(agent_code)
        deps_bundle = None
        if runtime_artifact.kind == "mcp" and (not upload_bucket or not platform_bucket):
            raise RuntimeError(
                "The standalone MCP runtime requires the platform mcp-lean dependency "
                "bundle, but no artifacts bucket is configured."
            )
        if upload_bucket:
            # The deploy session (target account when cross-account) uploads the
            # final zip. The dependency bundle lives ONLY in the platform bucket,
            # so it's read with the HOME/default session (never the target).
            upload_s3 = step_clients.client(event, "s3")
            import boto3 as _boto3

            deps_s3 = _boto3.client("s3", region_name=_get_env("APP_AWS_REGION", "us-east-1"))

            bundle_key = runtime_artifact.bundle_key
            if platform_bucket:
                logger.info("Downloading dependency bundle: s3://%s/%s (platform)", platform_bucket, bundle_key)
                deps_bundle = _download_bundle(deps_s3, platform_bucket, bundle_key)
                if not deps_bundle:
                    if runtime_artifact.kind == "mcp":
                        raise RuntimeError(
                            f"Standalone MCP dependency bundle s3://{platform_bucket}/"
                            f"{MCP_LEAN_BUNDLE_KEY} is missing or unreadable. Refusing "
                            "to upload a FastMCP module that cannot import."
                        )
                    logger.warning("Bundle download failed for %s", bundle_key)

            # The model-provider SDK. Neither base.zip nor strands-mcp.zip carries one,
            # so before this every non-Bedrock agent deployed `succeeded` and then died
            # at `from strands.models.openai import OpenAIModel` with ModuleNotFoundError
            # — which AgentCore surfaces only as "Runtime initialization time exceeded …
            # 30s". Measured on a live deploy; see PROVIDER_STRANDS_EXTRA.
            #
            # Merged as EXTRA bundles rather than folded into strands-mcp.zip because
            # every byte here is paid on every cold start against that same 30s budget,
            # and only the providers this canvas actually uses should cost anything.
            extra_bundles = (
                _provider_bundles(deps_s3, platform_bucket, config)
                if runtime_artifact.model_provider_applicable
                else []
            )

            from app.services.runtime_deployer import upload_code_to_s3

            logger.info("Uploading code.zip to s3://%s/%s", upload_bucket, s3_key)
            upload_code_to_s3(
                upload_s3,
                upload_bucket,
                s3_key,
                agent_code,
                "",
                entrypoint,
                deps_bundle=deps_bundle,
                extra_bundles=extra_bundles,
                expected_bucket_owner=str(_target_account) if _target_account else None,
                region=region,
                deployment_id=deployment_id,
            )
            # Record the bundle in the manifest, AFTER the upload, using the very
            # `s3_key` the upload used.
            #
            # Until this existed nothing in the codebase recorded a code bundle, so a
            # deploy that uploaded 18-43MB and then failed in any later state
            # (CreateIAMRole, ConfigureRuntime, LaunchRuntime, CreateEvaluation,
            # ConfigureJWTAuth) left the object behind forever. Ten such orphans
            # (~420MB) were found in the live artifacts bucket. The platform bucket has
            # a 90-day expiration on `deployments/`, but a cross-account TARGET bucket
            # (the registered ``target_artifact_bucket``) is customer-provisioned and
            # has no lifecycle rule we control, so expiry is not a substitute.
            #
            # Recorded here rather than reconstructed at delete time on purpose: the
            # only thing teardown otherwise has is `agentcore_runtime_name`, which
            # carries a `_<suffix>` and a [:39] truncation this key does not, and
            # `delete_object` on a WRONG key returns HTTP 200. A reconstructed key would
            # have produced a teardown that reports success and deletes nothing.
            try:
                object_row = {
                    "type": "s3_object",
                    "id": f"s3://{upload_bucket}/{s3_key}",
                    "region": region,
                    "created_by_deployment": True,
                }
                if _target_account:
                    object_row["account"] = str(_target_account)
                store.record_resource(deployment_id, object_row)
            except Exception:  # noqa: BLE001 - record_resource already swallows; belt and braces
                logger.warning("Could not record the code bundle for %s (non-fatal)", deployment_id)
        else:
            logger.warning("No artifacts bucket resolved, code not uploaded to S3")

        return {
            **event,
            "s3_bucket": upload_bucket,
            "s3_key": s3_key,
            "entrypoint": entrypoint,
            "agent_code": agent_code,
            "requirements_txt": requirements_txt,
            "runtime_artifact_kind": runtime_artifact.kind,
            "dependency_bundle_key": runtime_artifact.bundle_key,
        }

    except Exception:
        logger.exception("Codegen step failed for deployment %s", deployment_id)
        raise
