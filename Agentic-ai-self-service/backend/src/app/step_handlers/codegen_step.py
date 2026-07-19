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
from app.services import step_clients
from app.services.code_generator import generate_agent_code, generate_requirements
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


# Strands-based agents need the full bundle (strands + mcp + boto3).
STRANDS_BUNDLE_KEY = "agentcore-deps/strands-mcp.zip"

# Agents using only boto3/stdlib need the lighter bundle (boto3 only).
BASE_BUNDLE_KEY = "agentcore-deps/base.zip"


def _needs_strands_bundle(agent_code: str) -> bool:
    """Check if generated code needs the strands-mcp dependency bundle.

    Scans the generated code for strands/mcp imports. Agents that use only
    boto3 + stdlib (gateway agents, web-search, MCP server runtime) get the
    lighter base.zip (18MB) to stay within the 30s runtime init window.
    The full strands-mcp.zip (43MB) is only needed when code imports strands.
    """
    return "from strands " in agent_code or "import strands" in agent_code


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
        from app.services.observability import get_platform_observability_defaults

        obs_cfg = event.get("observability_config") or {}
        observability_enabled = bool(
            get_platform_observability_defaults()
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
        # (agentcore-flows-artifacts-<acct>-<region>, created at account
        # registration), while the dependency BUNDLE is still read from the
        # platform bucket (cross-account read, granted to the Deployment role).
        # Same-account is unchanged: everything uses the platform bucket.
        _target_account = event.get("target_account_id")
        upload_bucket = f"agentcore-flows-artifacts-{_target_account}-{region}" if _target_account else platform_bucket

        # Select bundle based on generated code: strands-mcp.zip (43MB) only
        # when code imports strands, otherwise base.zip (18MB) to stay within
        # the 30s runtime initialization window.
        deps_bundle = None
        if upload_bucket:
            # The deploy session (target account when cross-account) uploads the
            # final zip. The dependency bundle lives ONLY in the platform bucket,
            # so it's read with the HOME/default session (never the target).
            upload_s3 = step_clients.client(event, "s3")
            import boto3 as _boto3

            deps_s3 = _boto3.client("s3", region_name=_get_env("APP_AWS_REGION", "us-east-1"))

            bundle_key = STRANDS_BUNDLE_KEY if _needs_strands_bundle(agent_code) else BASE_BUNDLE_KEY
            if platform_bucket:
                logger.info("Downloading dependency bundle: s3://%s/%s (platform)", platform_bucket, bundle_key)
                deps_bundle = _download_bundle(deps_s3, platform_bucket, bundle_key)
                if not deps_bundle:
                    logger.warning("Bundle download failed for %s", bundle_key)

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
            )
        else:
            logger.warning("No artifacts bucket resolved, code not uploaded to S3")

        return {
            **event,
            "s3_bucket": upload_bucket,
            "s3_key": s3_key,
            "entrypoint": entrypoint,
            "agent_code": agent_code,
            "requirements_txt": requirements_txt,
        }

    except Exception:
        logger.exception("Codegen step failed for deployment %s", deployment_id)
        raise
