"""Custom Resource Lambda for AgentCore CloudFormation stacks.

Handles two Custom Resource types:

1. Custom::AgentCodePackage
   Merges pre-generated agent code with a pre-built dependency bundle
   (strands-mcp.zip or base.zip) into a single code.zip and uploads to S3.

   Properties:
       ArtifactsBucket  — S3 bucket for all artifacts
       AgentCodeKey     — S3 key of the agent code zip (contains agent.py)
       DependencyBundleKey — S3 key of the dependency bundle
       OutputKey        — S3 key for the merged output code.zip
   Returns:
       CodeZipPrefix    — S3 key prefix of the assembled code.zip

2. Custom::OAuth2CredentialProvider
   Creates/deletes an OAuth2 credential provider via the bedrock-agentcore-control
   API. Required for MCP server gateway targets (GATEWAY_IAM_ROLE is not supported).

   Properties:
       ProviderName     — Name for the credential provider
       DiscoveryUrl     — OIDC discovery URL (Cognito)
       ClientId         — OAuth2 client ID
       ClientSecret     — OAuth2 client secret
   Returns:
       CredentialProviderArn — ARN of the created credential provider
"""

import io
import logging
import time
import zipfile
from urllib.parse import quote

import boto3
import cfn_response  # absolute import — this file is packaged as a flat Lambda zip, not a package
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _error_code(exc: BaseException) -> str:
    """AWS error code from a ClientError ('' for non-ClientError).

    Local copy of app.services.aws_errors.error_code — this module is packaged
    as a flat Lambda zip (handler.py + cfn_response.py only, see
    cfn_template_generator._package_cfn_provider) and cannot import app.*.
    """
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


# ---------------------------------------------------------------------------
# Custom::AgentCodePackage
# ---------------------------------------------------------------------------


def _merge_deps_into_zip(target_zf: zipfile.ZipFile, bundle_bytes: bytes) -> None:
    """Extract dependency bundle into target zip, excluding __pycache__/.pyc."""
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as bundle_zf:
        for item in bundle_zf.namelist():
            if "__pycache__" in item or item.endswith(".pyc"):
                continue
            target_zf.writestr(item, bundle_zf.read(item))


def _merge_code_and_deps(agent_zip_bytes: bytes, bundle_bytes: bytes) -> bytes:
    """Merge agent code zip and dependency bundle into a single zip.

    Starts from the pre-built bundle and appends agent code files on top.
    This preserves the bundle's original compression, avoiding a full
    re-compress with ZIP_DEFLATED that can push runtime init past the
    30-second timeout.
    """
    buf = io.BytesIO(bundle_bytes)
    with zipfile.ZipFile(buf, "a") as out_zf:
        with zipfile.ZipFile(io.BytesIO(agent_zip_bytes), "r") as code_zf:
            for item in code_zf.namelist():
                if "__pycache__" in item or item.endswith(".pyc"):
                    continue
                out_zf.writestr(item, code_zf.read(item))
    buf.seek(0)
    return buf.read()


def _handle_code_package_create_update(event: dict) -> tuple[dict, str]:
    """Handle CREATE/UPDATE for AgentCodePackage."""
    props = event["ResourceProperties"]
    bucket = props["ArtifactsBucket"]
    agent_code_key = props["AgentCodeKey"]
    bundle_key = props["DependencyBundleKey"]
    output_key = props["OutputKey"]

    s3 = boto3.client("s3")

    logger.info("Downloading agent code: s3://%s/%s", bucket, agent_code_key)
    agent_zip = s3.get_object(Bucket=bucket, Key=agent_code_key)["Body"].read()
    logger.info("Agent code zip: %d bytes", len(agent_zip))

    logger.info("Downloading dependency bundle: s3://%s/%s", bucket, bundle_key)
    bundle = s3.get_object(Bucket=bucket, Key=bundle_key)["Body"].read()
    logger.info("Dependency bundle: %d bytes", len(bundle))

    merged = _merge_code_and_deps(agent_zip, bundle)
    logger.info("Merged code.zip: %d bytes", len(merged))

    logger.info("Uploading to s3://%s/%s", bucket, output_key)
    s3.put_object(Bucket=bucket, Key=output_key, Body=merged)

    physical_id = f"{bucket}/{output_key}"
    return {"CodeZipPrefix": output_key}, physical_id


def _handle_code_package_delete(event: dict) -> tuple[dict, str]:
    """Handle DELETE for AgentCodePackage."""
    props = event["ResourceProperties"]
    bucket = props["ArtifactsBucket"]
    output_key = props["OutputKey"]

    s3 = boto3.client("s3")
    try:
        s3.delete_object(Bucket=bucket, Key=output_key)
        logger.info("Deleted s3://%s/%s", bucket, output_key)
    except Exception as e:
        logger.warning("Failed to delete s3://%s/%s: %s", bucket, output_key, e)

    return {}, event.get("PhysicalResourceId", event.get("LogicalResourceId", ""))


# ---------------------------------------------------------------------------
# Custom::OAuth2CredentialProvider
# ---------------------------------------------------------------------------


def _get_agentcore_ctrl():
    """Get bedrock-agentcore-control client."""
    return boto3.client("bedrock-agentcore-control")


def _handle_oauth2_cred_create(event: dict) -> tuple[dict, str]:
    """Create an OAuth2 credential provider via bedrock-agentcore-control API."""
    props = event["ResourceProperties"]
    name = props["ProviderName"]
    discovery_url = props["DiscoveryUrl"]
    client_id = props["ClientId"]
    client_secret = props["ClientSecret"]

    ctrl = _get_agentcore_ctrl()

    logger.info(
        "Creating OAuth2 credential provider: %s", name
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource name, not secret
    try:
        resp = ctrl.create_oauth2_credential_provider(
            name=name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": {
                        "discoveryUrl": discovery_url,
                    },
                    "clientId": client_id,
                    "clientSecret": client_secret,
                }
            },
        )
        cred_arn = resp.get("credentialProviderArn", "")
    except ctrl.exceptions.ValidationException as e:
        if "already exists" in str(e):
            logger.info(
                "Credential provider %s already exists, fetching ARN", name
            )  # nosemgrep: python-logger-credential-disclosure -- logs resource name, not secret
            resp = ctrl.get_oauth2_credential_provider(name=name)
            cred_arn = resp.get("credentialProviderArn", "")
        else:
            raise
    logger.info(
        "Created OAuth2 credential provider: %s", cred_arn
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret

    # Wait a few seconds for IAM propagation
    time.sleep(5)

    data = {"CredentialProviderArn": cred_arn}

    # If a RuntimeArn is provided, compute the URL-encoded MCP endpoint URL
    runtime_arn = props.get("RuntimeArn", "")
    if runtime_arn:
        region = runtime_arn.split(":")[3] if ":" in runtime_arn else "us-east-1"
        encoded_arn = quote(runtime_arn, safe="")
        endpoint_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )
        data["McpEndpointUrl"] = endpoint_url
        logger.info("MCP endpoint URL: %s", endpoint_url)

    return data, cred_arn


def _handle_oauth2_cred_update(event: dict) -> tuple[dict, str]:
    """Update: delete old, create new."""
    old_arn = event.get("PhysicalResourceId", "")
    if old_arn and old_arn.startswith("arn:"):
        _delete_oauth2_cred(old_arn)
    return _handle_oauth2_cred_create(event)


def _delete_oauth2_cred(cred_arn: str) -> None:
    """Delete an OAuth2 credential provider by ARN."""
    ctrl = _get_agentcore_ctrl()
    # Extract the name from ARN
    # ARN format: arn:aws:bedrock-agentcore:region:account:token-vault/default/oauth2credentialprovider/name
    cred_name = cred_arn.rsplit("/", 1)[-1] if "/" in cred_arn else cred_arn
    try:
        ctrl.delete_oauth2_credential_provider(name=cred_name)
        logger.info(
            "Deleted OAuth2 credential provider: %s", cred_arn
        )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret
    except Exception as e:
        logger.warning("Failed to delete credential provider %s: %s", cred_name, type(e).__name__)


def _handle_oauth2_cred_delete(event: dict) -> tuple[dict, str]:
    """Handle DELETE for OAuth2CredentialProvider."""
    cred_arn = event.get("PhysicalResourceId", "")
    if cred_arn and cred_arn.startswith("arn:"):
        _delete_oauth2_cred(cred_arn)
    physical_id = event.get("PhysicalResourceId", event.get("LogicalResourceId", ""))
    return {}, physical_id


# ---------------------------------------------------------------------------
# Custom::AgentCorePolicy — Cedar policy attached to a PolicyEngine
# Native AWS::BedrockAgentCore::Policy has a stabilization timeout that
# fires before the policy engine is ready in fresh accounts (Bug 72). This
# Custom Resource gives us a longer wait + retries on the bind step.
# ---------------------------------------------------------------------------


def _handle_policy_create_update(event: dict) -> tuple[dict, str]:
    props = event["ResourceProperties"]
    name = props["Name"]
    statement = props["Statement"]
    engine_id = props["PolicyEngineId"]
    description = props.get("Description", "")

    ctrl = boto3.client("bedrock-agentcore-control")

    # Wait for the policy engine to actually be ready before attaching a
    # policy. Up to 5 minutes in fresh accounts.
    # Use list_policy_engines instead of get_policy_engine because Lambda's
    # bundled boto3 may not include the per-engine getter on older runtimes.
    # See tasks/lessons.md Bug 92.
    for attempt in range(30):
        found_active = False
        try:
            next_token = None
            for _ in range(20):
                kw = {"nextToken": next_token} if next_token else {}
                resp = ctrl.list_policy_engines(**kw)
                items = resp.get("policyEngineSummaries", resp.get("policyEngines", resp.get("items", [])))
                for item in items:
                    pid = item.get("policyEngineId") or item.get("id")
                    if pid == engine_id:
                        status = item.get("status", "")
                        if status in ("ACTIVE", "READY"):
                            found_active = True
                            break
                        if "FAILED" in status:
                            raise RuntimeError(f"PolicyEngine entered {status}")
                if found_active:
                    break
                next_token = resp.get("nextToken")
                if not next_token:
                    break
        except Exception as e:
            err_str = str(e)
            if "RuntimeError" in err_str and "FAILED" in err_str:
                raise
            logger.info("list_policy_engines attempt %d: %s", attempt + 1, err_str[:200])
        if found_active:
            break
        time.sleep(10)

    # Idempotent: look up existing policy by name
    policy_id = None
    try:
        next_token = None
        for _ in range(10):
            kw = {"policyEngineId": engine_id}
            if next_token:
                kw["nextToken"] = next_token
            resp = ctrl.list_policies(**kw)
            for p in resp.get("policySummaries", resp.get("policies", [])):
                if p.get("name") == name:
                    policy_id = p.get("policyId") or p.get("id")
                    break
            if policy_id or not resp.get("nextToken"):
                break
            next_token = resp.get("nextToken")
    except Exception as e:
        logger.info("list_policies (idempotency check) failed (will create): %s", e)

    if policy_id:
        logger.info("Policy %s already exists (id=%s), reusing", name, policy_id)
    else:
        for attempt in range(10):
            try:
                resp = ctrl.create_policy(
                    policyEngineId=engine_id,
                    name=name,
                    description=description,
                    definition={"cedar": {"statement": statement}},
                )
                policy_id = resp.get("policyId") or resp.get("id") or resp.get("policy", {}).get("policyId", "")
                break
            except Exception as e:
                err = str(e)
                # Message fallback kept: the propagation race can also surface as a
                # ValidationException saying "PolicyEngine ... not found".
                if _error_code(e) == "ResourceNotFoundException" or (
                    "PolicyEngine" in err and "not found" in err.lower()
                ):
                    logger.info("PolicyEngine still propagating (attempt %d): %s", attempt + 1, err[:200])
                    time.sleep(10)
                    continue
                raise

    physical_id = f"{engine_id}/policies/{policy_id or name}"
    return {"PolicyId": policy_id or "", "PolicyEngineId": engine_id}, physical_id


def _handle_policy_delete(event: dict) -> tuple[dict, str]:
    physical_id = event.get("PhysicalResourceId", "")
    props = event.get("ResourceProperties", {})
    engine_id = props.get("PolicyEngineId", "")
    # Parse policy_id out of physical_id format "engine_id/policies/policy_id"
    policy_id = ""
    if "/policies/" in physical_id:
        policy_id = physical_id.rsplit("/", 1)[-1]
    if engine_id and policy_id:
        try:
            ctrl = boto3.client("bedrock-agentcore-control")
            ctrl.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        except Exception as e:
            logger.warning("delete_policy failed (treating as benign): %s", e)
    return {}, physical_id or event.get("LogicalResourceId", "")


# ---------------------------------------------------------------------------
# Router — dispatches by resource type
# ---------------------------------------------------------------------------


def _get_resource_type(event: dict) -> str:
    """Determine the custom resource type from the event."""
    return event.get("ResourceType", event.get("ResourceProperties", {}).get("ServiceToken", ""))


def handler(event: dict, context) -> None:
    """CloudFormation Custom Resource entry point."""
    request_type = event.get("RequestType", "")
    logical_id = event.get("LogicalResourceId", "")
    resource_type = _get_resource_type(event)
    logger.info("CFN %s for %s (type: %s)", request_type, logical_id, resource_type)

    try:
        if resource_type == "Custom::OAuth2CredentialProvider":
            if request_type == "Create":
                data, physical_id = _handle_oauth2_cred_create(event)
            elif request_type == "Update":
                data, physical_id = _handle_oauth2_cred_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_oauth2_cred_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")
        elif resource_type == "Custom::AgentCorePolicy":
            if request_type in ("Create", "Update"):
                data, physical_id = _handle_policy_create_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_policy_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")
        else:
            # Default: AgentCodePackage
            if request_type in ("Create", "Update"):
                data, physical_id = _handle_code_package_create_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_code_package_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")

        cfn_response.send(
            event,
            context,
            cfn_response.SUCCESS,
            data=data,
            physical_resource_id=physical_id,
        )

    except Exception as e:
        logger.exception("Custom resource handler failed")
        cfn_response.send(
            event,
            context,
            cfn_response.FAILED,
            reason=str(e),
            physical_resource_id=event.get("PhysicalResourceId", logical_id),
        )
