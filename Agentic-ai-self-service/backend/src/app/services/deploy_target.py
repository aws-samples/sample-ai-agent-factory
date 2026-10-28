"""Multi-region / multi-account deployment targets (Phase 7 — opt-in).

DISABLED BY DEFAULT. Unless an admin explicitly enables deployment targets
(a Settings row) AND registers a target, every deploy goes to the platform's
home account + region exactly as before — zero behavior change.

Two dimensions:
  * **region** — deploy to a different AWS region (already plumbed through
    services/deployment.py; this adds an admin allowlist + validation).
  * **account** — deploy to a DIFFERENT AWS account by assuming a cross-account
    ``deployment role`` that the target account's owner has created to trust the
    platform account. We sts:AssumeRole into it and hand step handlers a scoped
    boto3 Session.

Safety rails:
  * Feature gate: ``targets_enabled`` Settings flag (default false).
  * Region allowlist: only admin-approved regions are accepted.
  * Cross-account: the role must be assumable AND a dry-run GetCallerIdentity
    must confirm we landed in the expected account — else the deploy is refused.
  * Least privilege: the assumed role is the target owner's responsibility; we
    document the required trust policy + verb set (mirrors our step roles).

Config is stored in the tag-policy table (generic Settings store) to avoid a
new table:
  SK ``SETTING#deploy_targets_enabled`` → {"value": "true"|"false"}
  SK ``TARGET#region#<region>``          → {"region", "account_id",
                                            "artifact_bucket"}
  SK ``TARGET#account#<account_id>``     → {"account_id", "role_arn",
                                            "runtime_role_arn",
                                            "harness_role_arn",
                                            "artifact_bucket", "region"}
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import unquote

import boto3

logger = logging.getLogger(__name__)

_ENABLED_SK = "SETTING#deploy_targets_enabled"
_REGION_PREFIX = "TARGET#region#"
_ACCOUNT_PREFIX = "TARGET#account#"

# The home region — deploys with no explicit target land here (unchanged path).
HOME_REGION_DEFAULT = "us-east-1"
DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME = "AgentCoreFlowsDeploymentRole"
DEFAULT_TARGET_RUNTIME_ROLE_NAME = "AgentCoreFlowsRuntimeRole"
# A distinct, model-free execution role for protocol-only FastMCP servers. The
# Runtime role above is intentionally model-capable; reusing it for an MCP server
# would let a tool-only runtime invoke arbitrary models, contradicting the
# model-free runtime contract enforced for home-account deployments.
DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME = "AgentCoreFlowsMCPRuntimeRole"
DEFAULT_TARGET_HARNESS_ROLE_NAME = "AgentCoreFlowsHarnessRole"
DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX = "agentcore-flows-artifacts"

_IAM_ROLE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):iam::"
    r"(?P<account_id>\d{12}):role/(?P<role_path>[\w+=,.@/-]{1,512})$"
)
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IP_ADDRESS_BUCKET_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_KMS_KEY_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):kms:"
    r"(?P<region>[a-z0-9-]+):(?P<account_id>\d{12}):key/(?P<key_id>[A-Za-z0-9-]{1,128})$"
)
_REQUIRED_EVALUATION_KEY_ACTIONS = frozenset({"kms:GenerateDataKey", "kms:Decrypt"})


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", HOME_REGION_DEFAULT))


def home_region() -> str:
    """The platform account's default deployment region."""
    return _region()


def _settings_table():
    name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    return boto3.resource("dynamodb", region_name=_region()).Table(name)


# ---------------------------------------------------------------------------
# Feature gate + config
# ---------------------------------------------------------------------------


def targets_enabled() -> bool:
    """True only when an admin has explicitly enabled deployment targets."""
    if os.environ.get("DEPLOY_TARGETS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        item = _settings_table().get_item(Key={"org_id": "default", "sk": _ENABLED_SK}).get("Item")
        return bool(item and str(item.get("value", "")).lower() == "true")
    except Exception as e:  # noqa: BLE001
        logger.info("targets_enabled check failed (default false): %s", e)
        return False


def set_targets_enabled(enabled: bool) -> None:
    _settings_table().put_item(Item={"org_id": "default", "sk": _ENABLED_SK, "value": "true" if enabled else "false"})


def add_region(
    region: str,
    *,
    account_id: str | None = None,
    artifact_bucket: str | None = None,
    evaluation_kms_key_arn: str | None = None,
) -> None:
    """Persist a region target and its validated artifacts/evaluation authority.

    The optional fields only preserve readability of legacy rows. Admin registration
    always supplies all three; live deployment resolution rejects an incomplete row.
    """
    _require_release_region(region, surface="a deployment target")
    if bool(account_id) != bool(artifact_bucket):
        raise TargetError("A region target must store both account_id and artifact_bucket")
    if evaluation_kms_key_arn and not account_id:
        raise TargetError("A region target evaluation key requires an account_id")
    item = {
        "org_id": "default",
        "sk": _REGION_PREFIX + region,
        "region": region,
    }
    if account_id and artifact_bucket:
        item["account_id"] = account_id
        item["artifact_bucket"] = target_artifact_bucket_name(
            account_id,
            region,
            artifact_bucket=artifact_bucket,
        )
    if evaluation_kms_key_arn:
        item["evaluation_kms_key_arn"] = evaluation_kms_key_arn
    _settings_table().put_item(Item=item)


def get_region_target(region: str) -> dict | None:
    item = _settings_table().get_item(Key={"org_id": "default", "sk": _REGION_PREFIX + region}).get("Item")
    return dict(item) if item else None


def list_region_targets() -> list[dict]:
    from boto3.dynamodb.conditions import Key

    try:
        resp = _settings_table().query(
            KeyConditionExpression=Key("org_id").eq("default") & Key("sk").begins_with(_REGION_PREFIX)
        )
        return [dict(item) for item in resp.get("Items", [])]
    except Exception:  # noqa: BLE001
        return []


def list_regions() -> list[str]:
    return [str(item["region"]) for item in list_region_targets() if item.get("region")]


def target_execution_role_arn(
    account_id: str,
    *,
    role_arn: str | None,
    default_role_name: str,
) -> str:
    """Return an exact target-account execution-role ARN.

    A target registration may override the conventional role name with a full
    ARN (including an IAM path). Regardless of source, the ARN must belong to
    the target account; accepting a role from a different account would make
    the registration appear valid while ``CreateAgentRuntime``/``CreateHarness``
    later fails at ``iam:PassRole``.
    """
    resolved = role_arn or f"arn:aws:iam::{account_id}:role/{default_role_name}"
    match = _IAM_ROLE_ARN_RE.fullmatch(resolved)
    if not match:
        raise TargetError(f"Invalid IAM role ARN for target account {account_id}: {resolved!r}")
    if match.group("account_id") != account_id:
        raise TargetError(
            f"IAM role {resolved!r} belongs to account {match.group('account_id')}, not target account {account_id}"
        )
    return resolved


def target_deployment_role_arn(account_id: str, role_arn: str | None) -> str:
    """Return the one deployment-role ARN platform Lambda IAM permits.

    Platform and step Lambda roles are intentionally allowed to assume only
    ``AgentCoreFlowsDeploymentRole`` in target accounts. Accepting an arbitrary
    registration ARN would persist a target that can never be assumed at
    runtime (or require a dangerous wildcard ``sts:AssumeRole`` grant).
    """
    resolved = target_execution_role_arn(
        account_id,
        role_arn=role_arn,
        default_role_name=DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME,
    )
    if _role_name_from_arn(resolved) != DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME:
        raise TargetError(
            "The target deployment role must be named exactly "
            f"{DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME!r}; platform Lambda IAM "
            "does not allow assuming arbitrary target roles."
        )
    match = _IAM_ROLE_ARN_RE.fullmatch(resolved)
    if not match or match.group("role_path") != DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME:
        raise TargetError(
            f"The target deployment role must not use an IAM path; expected role/{DEFAULT_TARGET_DEPLOYMENT_ROLE_NAME}."
        )
    return resolved


def default_runtime_role_arn(account_id: str) -> str:
    return target_execution_role_arn(
        account_id,
        role_arn=None,
        default_role_name=DEFAULT_TARGET_RUNTIME_ROLE_NAME,
    )


def default_mcp_runtime_role_arn(account_id: str) -> str:
    return target_execution_role_arn(
        account_id,
        role_arn=None,
        default_role_name=DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
    )


def default_harness_role_arn(account_id: str) -> str:
    return target_execution_role_arn(
        account_id,
        role_arn=None,
        default_role_name=DEFAULT_TARGET_HARNESS_ROLE_NAME,
    )


def target_artifact_bucket_name(
    account_id: str,
    region: str,
    *,
    artifact_bucket: str | None = None,
) -> str:
    """Return one exact, DNS-compatible target-account artifact bucket name."""
    resolved = artifact_bucket or f"{DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX}-{account_id}-{region}"
    if not _S3_BUCKET_RE.fullmatch(resolved) or ".." in resolved or _IP_ADDRESS_BUCKET_RE.fullmatch(resolved):
        raise TargetError(f"Invalid S3 artifact bucket name for target account {account_id}: {resolved!r}")
    return resolved


def default_artifact_bucket_name(account_id: str, region: str) -> str:
    return target_artifact_bucket_name(account_id, region)


def require_platform_bucket_namespace(account_id: str, region: str, artifact_bucket: str | None) -> str:
    """Refuse a same-account regional bucket the platform's roles cannot reach.

    The stack grants its roles ``{prefix}-{account}-*`` only, conditioned on
    ``aws:ResourceAccount`` (infra/stacks/platform/regional_artifact_bucket_grant.py).
    A bucket outside that namespace would pass nothing but this check's absence and
    then fail inside Step Functions, after the deploy returned 202.
    """
    resolved = target_artifact_bucket_name(account_id, region, artifact_bucket=artifact_bucket)
    prefix = f"{DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX}-{account_id}-"
    if not resolved.startswith(prefix):
        raise TargetError(
            f"A same-account regional target must use a bucket named {prefix}<suffix> "
            f"(default {prefix}{region}); the platform's roles are granted only that namespace"
        )
    return resolved


def add_account(
    account_id: str,
    role_arn: str,
    region: str,
    *,
    runtime_role_arn: str | None = None,
    mcp_runtime_role_arn: str | None = None,
    harness_role_arn: str | None = None,
    artifact_bucket: str | None = None,
    evaluation_kms_key_arn: str | None = None,
) -> None:
    role_arn = target_deployment_role_arn(account_id, role_arn)
    runtime_role_arn = target_execution_role_arn(
        account_id,
        role_arn=runtime_role_arn,
        default_role_name=DEFAULT_TARGET_RUNTIME_ROLE_NAME,
    )
    mcp_runtime_role_arn = target_execution_role_arn(
        account_id,
        role_arn=mcp_runtime_role_arn,
        default_role_name=DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
    )
    harness_role_arn = target_execution_role_arn(
        account_id,
        role_arn=harness_role_arn,
        default_role_name=DEFAULT_TARGET_HARNESS_ROLE_NAME,
    )
    artifact_bucket = target_artifact_bucket_name(
        account_id,
        region,
        artifact_bucket=artifact_bucket,
    )
    _settings_table().put_item(
        Item={
            "org_id": "default",
            "sk": _ACCOUNT_PREFIX + account_id,
            "account_id": account_id,
            "role_arn": role_arn,
            "runtime_role_arn": runtime_role_arn,
            "mcp_runtime_role_arn": mcp_runtime_role_arn,
            "harness_role_arn": harness_role_arn,
            "artifact_bucket": artifact_bucket,
            "evaluation_kms_key_arn": evaluation_kms_key_arn,
            "region": region,
        }
    )


def get_account(account_id: str) -> dict | None:
    item = _settings_table().get_item(Key={"org_id": "default", "sk": _ACCOUNT_PREFIX + account_id}).get("Item")
    return dict(item) if item else None


def list_accounts() -> list[dict]:
    from boto3.dynamodb.conditions import Key

    try:
        resp = _settings_table().query(
            KeyConditionExpression=Key("org_id").eq("default") & Key("sk").begins_with(_ACCOUNT_PREFIX)
        )
        return [dict(i) for i in resp.get("Items", [])]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Target resolution → boto3 Session
# ---------------------------------------------------------------------------


class TargetError(ValueError):
    """Raised when a requested deploy target is invalid / disabled / unreachable."""


def _require_release_region(region: str, *, surface: str) -> str:
    """The shared release-Region predicate, surfaced as this module's TargetError."""
    from app.services.region_capabilities import UnsupportedReleaseRegionError, require_release_region

    try:
        return require_release_region(region, surface=surface)
    except UnsupportedReleaseRegionError as exc:
        raise TargetError(str(exc)) from exc


def _role_name_from_arn(role_arn: str) -> str:
    match = _IAM_ROLE_ARN_RE.fullmatch(role_arn)
    if not match:
        raise TargetError(f"Invalid IAM role ARN: {role_arn!r}")
    return match.group("role_path").rsplit("/", 1)[-1]


def _trusts_agentcore(document) -> bool:
    """Whether an IAM trust policy allows the AgentCore service to assume it."""
    if isinstance(document, str):
        try:
            document = json.loads(unquote(document))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if not isinstance(document, dict):
        return False
    statements = document.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action") or []
        if isinstance(actions, str):
            actions = [actions]
        if "sts:AssumeRole" not in actions and "sts:*" not in actions and "*" not in actions:
            continue
        principal = statement.get("Principal") or {}
        services = principal.get("Service") if isinstance(principal, dict) else None
        if isinstance(services, str):
            services = [services]
        if services and ("bedrock-agentcore.amazonaws.com" in services or "*" in services):
            return True
    return False


def validate_execution_roles(
    session: boto3.Session,
    *,
    account_id: str,
    runtime_role_arn: str | None = None,
    mcp_runtime_role_arn: str | None = None,
    harness_role_arn: str | None = None,
) -> tuple[str, str, str]:
    """Prove all three stable AgentCore execution roles exist and trust the service.

    Cross-account Runtime, MCP-runtime and Harness creation deliberately use
    pre-provisioned roles to avoid AgentCore's long IAM propagation race. The MCP
    runtime role is distinct from — and intentionally weaker than — the
    model-capable Runtime role, so a protocol-only FastMCP server cannot invoke
    models. A target that lacks any of the three is not fully deployable, so
    registration fails before the configuration is persisted instead of accepting
    a target that fails after ``POST /api/deploy`` returns 202.
    """
    resolved_runtime = target_execution_role_arn(
        account_id,
        role_arn=runtime_role_arn,
        default_role_name=DEFAULT_TARGET_RUNTIME_ROLE_NAME,
    )
    resolved_mcp_runtime = target_execution_role_arn(
        account_id,
        role_arn=mcp_runtime_role_arn,
        default_role_name=DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
    )
    resolved_harness = target_execution_role_arn(
        account_id,
        role_arn=harness_role_arn,
        default_role_name=DEFAULT_TARGET_HARNESS_ROLE_NAME,
    )
    iam = session.client("iam")
    for kind, role_arn in (
        ("runtime", resolved_runtime),
        ("mcp runtime", resolved_mcp_runtime),
        ("harness", resolved_harness),
    ):
        role_name = _role_name_from_arn(role_arn)
        try:
            role = iam.get_role(RoleName=role_name)["Role"]
        except Exception as exc:  # noqa: BLE001
            raise TargetError(
                f"Cannot read target {kind} execution role {role_arn}. "
                "Create it and grant the deployment role iam:GetRole before registering the target."
            ) from exc
        actual_arn = role.get("Arn")
        if actual_arn != role_arn:
            raise TargetError(f"Target {kind} execution role resolved to {actual_arn!r}, expected {role_arn!r}")
        if not _trusts_agentcore(role.get("AssumeRolePolicyDocument")):
            raise TargetError(
                f"Target {kind} execution role {role_arn} does not trust "
                "bedrock-agentcore.amazonaws.com to call sts:AssumeRole"
            )
    return resolved_runtime, resolved_mcp_runtime, resolved_harness


def _normalise_bucket_region(location: str | None) -> str:
    """Map S3's legacy location values to normal region names."""
    if not location:
        return "us-east-1"
    if location == "EU":
        return "eu-west-1"
    return str(location)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _statement_allows(statement: dict, action: str) -> bool:
    actions = {str(value) for value in _as_list(statement.get("Action"))}
    return action in actions or "kms:*" in actions or "*" in actions


def _condition_contains(statement: dict, operator: str, key: str, value: str) -> bool:
    condition = statement.get("Condition")
    if not isinstance(condition, dict):
        return False
    values = condition.get(operator)
    if not isinstance(values, dict):
        return False
    return value in {str(item) for item in _as_list(values.get(key))}


def _principal_contains(statement: dict, kind: str, value: str) -> bool:
    principal = statement.get("Principal")
    if not isinstance(principal, dict):
        return False
    return value in {str(item) for item in _as_list(principal.get(kind))}


def _validate_evaluation_key_policy(
    policy: dict,
    *,
    key_arn: str,
    account_id: str,
    region: str,
    deployment_role_arn: str,
) -> None:
    statements = policy.get("Statement") if isinstance(policy, dict) else None
    statements = _as_list(statements)
    if not statements or not all(isinstance(statement, dict) for statement in statements):
        raise TargetError(f"Evaluation KMS key {key_arn} has no valid key-policy statements")

    match = _KMS_KEY_ARN_RE.fullmatch(key_arn)
    assert match is not None
    batch_arn = f"arn:{match.group('partition')}:bedrock-agentcore:{region}:{account_id}:batch-evaluate/*"
    service_allowed = any(
        statement.get("Effect") == "Allow"
        and _principal_contains(statement, "Service", "bedrock-agentcore.amazonaws.com")
        and all(_statement_allows(statement, action) for action in _REQUIRED_EVALUATION_KEY_ACTIONS)
        and _condition_contains(statement, "StringEquals", "aws:SourceAccount", account_id)
        and _condition_contains(statement, "ArnLike", "aws:SourceArn", batch_arn)
        for statement in statements
    )
    if not service_allowed:
        raise TargetError(
            f"Evaluation KMS key {key_arn} must allow bedrock-agentcore.amazonaws.com "
            "kms:GenerateDataKey and kms:Decrypt, restricted to the target account and "
            "target-Region batch-evaluate/* source ARN"
        )

    root_arn = f"arn:{match.group('partition')}:iam::{account_id}:root"
    caller_allowed = any(
        statement.get("Effect") == "Allow"
        and (
            _principal_contains(statement, "AWS", deployment_role_arn)
            or _principal_contains(statement, "AWS", root_arn)
        )
        and all(_statement_allows(statement, action) for action in _REQUIRED_EVALUATION_KEY_ACTIONS)
        for statement in statements
    )
    if not caller_allowed:
        raise TargetError(
            f"Evaluation KMS key {key_arn} must delegate kms:GenerateDataKey and "
            "kms:Decrypt to the target deployment role or target-account root"
        )


def validate_evaluation_kms_key(
    session: boto3.Session,
    *,
    account_id: str,
    region: str,
    key_arn: str,
    deployment_role_arn: str,
) -> str:
    """Validate one exact CMK using metadata and its policy, never key material."""

    match = _KMS_KEY_ARN_RE.fullmatch(key_arn or "")
    if match is None:
        raise TargetError("The evaluation KMS key must be a full key ARN, not an alias")
    if match.group("account_id") != account_id:
        raise TargetError(
            f"Evaluation KMS key belongs to account {match.group('account_id')}, not target account {account_id}"
        )
    if match.group("region") != region:
        raise TargetError(f"Evaluation KMS key is in region {match.group('region')}, not target region {region}")
    role_match = _IAM_ROLE_ARN_RE.fullmatch(deployment_role_arn or "")
    if role_match is None or role_match.group("partition") != match.group("partition"):
        raise TargetError("Evaluation KMS key and deployment role must belong to the same AWS partition")

    kms_client = session.client("kms", region_name=region)
    try:
        metadata = kms_client.describe_key(KeyId=key_arn).get("KeyMetadata") or {}
    except Exception as exc:  # noqa: BLE001
        raise TargetError(f"Cannot describe evaluation KMS key {key_arn}") from exc
    if metadata.get("Arn") != key_arn or metadata.get("AWSAccountId") != account_id:
        raise TargetError("Evaluation KMS key metadata does not match its registered ARN/account")
    if metadata.get("KeyManager") != "CUSTOMER":
        raise TargetError("Evaluation KMS key must be customer managed")
    if metadata.get("KeySpec") != "SYMMETRIC_DEFAULT" or metadata.get("KeyUsage") != "ENCRYPT_DECRYPT":
        raise TargetError("Evaluation KMS key must be a symmetric ENCRYPT_DECRYPT key")
    if metadata.get("KeyState") != "Enabled" or metadata.get("Enabled") is not True:
        raise TargetError("Evaluation KMS key must be enabled")

    try:
        policy_text = kms_client.get_key_policy(KeyId=key_arn, PolicyName="default").get("Policy")
        policy = json.loads(policy_text) if isinstance(policy_text, str) else policy_text
    except Exception as exc:  # noqa: BLE001
        raise TargetError(f"Cannot read the default policy for evaluation KMS key {key_arn}") from exc
    _validate_evaluation_key_policy(
        policy,
        key_arn=key_arn,
        account_id=account_id,
        region=region,
        deployment_role_arn=deployment_role_arn,
    )
    return key_arn


def validate_artifact_bucket(
    session: boto3.Session,
    *,
    account_id: str,
    region: str,
    artifact_bucket: str | None = None,
    same_account: bool = False,
) -> str:
    """Prove the target code bucket exists, belongs to the account, and is regional.

    AgentCore fetches runtime code from S3 after the deployment Lambda has
    returned. Accepting an unverified bucket therefore creates a delayed,
    hard-to-diagnose runtime failure. Both reads pin ``ExpectedBucketOwner`` so
    a globally unique bucket with the requested name cannot silently resolve to
    somebody else's account.
    """
    resolved = target_artifact_bucket_name(
        account_id,
        region,
        artifact_bucket=artifact_bucket,
    )
    s3 = session.client("s3", region_name=region)
    call = "HeadBucket"
    try:
        s3.head_bucket(Bucket=resolved, ExpectedBucketOwner=account_id)
        call = "GetBucketLocation"
        location = s3.get_bucket_location(
            Bucket=resolved,
            ExpectedBucketOwner=account_id,
        ).get("LocationConstraint")
    except Exception as exc:  # noqa: BLE001
        # Every cause below collapses into one caller-facing message, so without this
        # line a refusal is undiagnosable: a missing grant, a 404 and a client-side
        # parameter error all read the same. Type and error code only, never the
        # message, which can echo request parameters.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        logger.warning("artifact bucket validation failed at %s in %s: %s %s", call, region, type(exc).__name__, code)
        if same_account:
            # The stack already grants its own roles this namespace, so the remedy is
            # never "add a grant": the bucket is missing, or its policy denies them.
            remedy = f"Create it in {region}, and make sure its bucket policy does not deny the platform's roles."
        else:
            remedy = (
                f"Create it in {region} and grant the target deployment role "
                "s3:ListBucket, s3:GetBucketLocation, s3:GetObject, s3:PutObject, "
                "and s3:DeleteObject before registering the target."
            )
        raise TargetError(
            f"Cannot access target artifact bucket {resolved!r} in account {account_id}. {remedy}"
        ) from exc
    actual_region = _normalise_bucket_region(location)
    if actual_region != region:
        raise TargetError(
            f"Target artifact bucket {resolved!r} is in region "
            f"{actual_region!r}, not registered target region {region!r}"
        )
    return resolved


def resolve_region(requested: str | None) -> str:
    """Return the region to deploy to, enforcing the allowlist when targeting.

    No requested region → home region (unchanged). A requested region is only
    honored when targets are enabled AND it's on the admin allowlist. Either way
    the result must be a selected release Region: the admin allowlist is a
    narrowing of the envelope, never a widening of it, so a row registered before
    the envelope was enforced (or a platform stack stood up outside it) is refused
    here rather than deployed to.
    """
    home = _region()
    if not requested or requested == home:
        return _require_release_region(home, surface="the platform home Region")
    if not targets_enabled():
        raise TargetError("Deployment targets are disabled; cannot target another region")
    if requested not in list_regions():
        raise TargetError(f"Region '{requested}' is not on the deployment allowlist")
    return _require_release_region(requested, surface="this deployment target")


def session_for_target(
    account_id: str | None = None,
    region: str | None = None,
    *,
    require_gate: bool = True,
    role_arn: str | None = None,
) -> boto3.Session:
    """Return a boto3 Session for the deploy target.

    * No account_id (or the home account) → the DEFAULT session (unchanged path).
    * A registered target account → assume its cross-account deployment role and
      return a scoped session, after a dry-run GetCallerIdentity confirms we
      landed in the expected account.

    ``require_gate`` (default True) re-checks the opt-in feature flag + region
    allowlist. The SFN STEP path passes ``require_gate=False`` because the
    deployment Lambda (handle_deploy) is the single authoritative gate — it
    validated targets_enabled() + the allowlist BEFORE starting the state
    machine, and the step Lambdas don't carry the Settings-table env to re-read
    the flag. ``role_arn`` may be supplied to skip the Settings lookup (used when
    the caller already knows the target's role, e.g. teardown from a manifest).

    Raises TargetError when targeting is disabled (gated path only), the account
    is unregistered, the role can't be assumed, or the landed account mismatches.
    """
    resolved_region = (
        resolve_region(region)
        if require_gate
        else (region or os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", HOME_REGION_DEFAULT)))
    )
    if not account_id:
        return boto3.Session(region_name=resolved_region)

    if require_gate and not targets_enabled():
        raise TargetError("Deployment targets are disabled; cannot target another account")

    if role_arn is None:
        target = get_account(account_id)
        if target is None:
            raise TargetError(f"Account '{account_id}' is not a registered deployment target")
        role_arn = target["role_arn"]
    role_arn = target_deployment_role_arn(account_id, role_arn)
    sts = boto3.client("sts", region_name=resolved_region)
    try:
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="agentcore-flows-deploy")["Credentials"]
    except Exception as e:  # noqa: BLE001
        raise TargetError(f"Cannot assume deployment role in {account_id}: {str(e)[:160]}") from e

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=resolved_region,
    )
    # Dry-run: confirm we actually landed in the expected account.
    landed = session.client("sts").get_caller_identity()["Account"]
    if landed != account_id:
        raise TargetError(f"Assumed role landed in account {landed}, expected {account_id}")
    return session


def resolve_registered_account_target(
    account_id: str,
    requested_region: str | None = None,
) -> dict:
    """Resolve and revalidate one registered account target for a live deploy.

    The account registry is keyed by account id and carries one onboarding
    region because its artifact bucket is regional. Omitting ``requested_region``
    uses that registered region; supplying a different one is rejected instead
    of silently targeting a region whose bucket/onboarding was never validated.

    Role validation is intentionally repeated for every deployment. IAM roles
    can be deleted or have their trust changed after registration, and returning
    HTTP 202 before discovering that inside Step Functions leaves a needless
    failed deployment record.
    """
    if not targets_enabled():
        raise TargetError("Deployment targets are disabled; cannot target another account")
    target = get_account(account_id)
    if target is None:
        raise TargetError(f"Account '{account_id}' is not a registered deployment target")

    registered_region = str(target.get("region") or _region())
    if requested_region and requested_region != registered_region:
        raise TargetError(
            f"Account '{account_id}' is registered for region '{registered_region}', "
            f"not requested region '{requested_region}'"
        )
    resolved_region = resolve_region(registered_region)
    deployment_role_arn = target_deployment_role_arn(
        account_id,
        target.get("role_arn"),
    )
    session = session_for_target(
        account_id=account_id,
        region=resolved_region,
        role_arn=deployment_role_arn,
    )
    runtime_role_arn, mcp_runtime_role_arn, harness_role_arn = validate_execution_roles(
        session,
        account_id=account_id,
        runtime_role_arn=target.get("runtime_role_arn"),
        mcp_runtime_role_arn=target.get("mcp_runtime_role_arn"),
        harness_role_arn=target.get("harness_role_arn"),
    )
    artifact_bucket = validate_artifact_bucket(
        session,
        account_id=account_id,
        region=resolved_region,
        artifact_bucket=target.get("artifact_bucket"),
    )
    evaluation_kms_key_arn = str(target.get("evaluation_kms_key_arn") or "")
    if not evaluation_kms_key_arn:
        raise TargetError(
            f"Account '{account_id}' has no validated production-evaluation KMS key; re-register the target"
        )
    evaluation_kms_key_arn = validate_evaluation_kms_key(
        session,
        account_id=account_id,
        region=resolved_region,
        key_arn=evaluation_kms_key_arn,
        deployment_role_arn=deployment_role_arn,
    )
    return {
        "account_id": account_id,
        "region": resolved_region,
        "role_arn": deployment_role_arn,
        "runtime_role_arn": runtime_role_arn,
        "mcp_runtime_role_arn": mcp_runtime_role_arn,
        "harness_role_arn": harness_role_arn,
        "artifact_bucket": artifact_bucket,
        "evaluation_kms_key_arn": evaluation_kms_key_arn,
    }


def resolve_registered_region_target(requested_region: str | None) -> dict:
    """Resolve and revalidate a same-account regional deployment target.

    A bare region allowlist is insufficient for AgentCore code deployment:
    runtime code is uploaded through a regional S3 client and fetched after the
    deployment Lambda returns. Freeze the exact bucket and owning platform
    account at onboarding, then revalidate both for every live deploy.
    """
    resolved_region = resolve_region(requested_region)
    if resolved_region == _region():
        evaluation_kms_key_arn = os.environ.get("EVALUATION_EVIDENCE_KMS_KEY_ARN", "").strip()
        if not evaluation_kms_key_arn:
            raise TargetError("The home Region has no configured production-evaluation KMS key")
        return {
            "region": resolved_region,
            "artifact_bucket": os.environ.get("ARTIFACTS_BUCKET_NAME", ""),
            "evaluation_kms_key_arn": evaluation_kms_key_arn,
        }

    target = get_region_target(resolved_region)
    if target is None:
        raise TargetError(f"Region '{resolved_region}' is not a registered deployment target")

    account_id = str(target.get("account_id") or "")
    artifact_bucket = str(target.get("artifact_bucket") or "")
    if not account_id or not artifact_bucket:
        raise TargetError(
            f"Region '{resolved_region}' has no validated artifact bucket; "
            "re-register it in the admin deployment-target settings"
        )

    session = session_for_target(
        account_id=None,
        region=resolved_region,
        require_gate=False,
    )
    try:
        landed_account = str(session.client("sts").get_caller_identity()["Account"])
    except Exception as exc:  # noqa: BLE001
        raise TargetError(f"Cannot verify the platform account while resolving region '{resolved_region}'") from exc
    if landed_account != account_id:
        raise TargetError(
            f"Region '{resolved_region}' is registered for account {account_id}, "
            f"but the platform credentials currently resolve to {landed_account}"
        )

    # A row registered before the namespace was enforced fails here, not mid-deploy.
    require_platform_bucket_namespace(account_id, resolved_region, artifact_bucket)
    artifact_bucket = validate_artifact_bucket(
        session,
        account_id=account_id,
        region=resolved_region,
        artifact_bucket=artifact_bucket,
        same_account=True,
    )
    evaluation_kms_key_arn = str(target.get("evaluation_kms_key_arn") or "")
    if not evaluation_kms_key_arn:
        raise TargetError(
            f"Region '{resolved_region}' has no validated production-evaluation KMS key; re-register the target"
        )
    deployment_role_arn = os.environ.get("DEPLOYMENT_LAMBDA_ROLE_ARN", "").strip()
    if not deployment_role_arn:
        raise TargetError("The platform deployment role ARN is unavailable for evaluation-key validation")
    evaluation_kms_key_arn = validate_evaluation_kms_key(
        session,
        account_id=account_id,
        region=resolved_region,
        key_arn=evaluation_kms_key_arn,
        deployment_role_arn=deployment_role_arn,
    )
    return {
        "account_id": account_id,
        "region": resolved_region,
        "artifact_bucket": artifact_bucket,
        "evaluation_kms_key_arn": evaluation_kms_key_arn,
    }
