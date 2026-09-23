/*
 * D03WorkstreamRuntimeMemoryStack — opt-in, pipeline-owned native AgentCore
 * Runtime + Memory foundation for a workstream.
 *
 * This stack stands up the two native GA resources whose API contracts were
 * live-proven on exact commit `88d5381` (see
 * `evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md`):
 *
 *   - `AWS::BedrockAgentCore::Memory`  — short/long-term memory store, CMK-
 *     encrypted, event-expiry bounded.
 *   - `AWS::BedrockAgentCore::Runtime` — the ARM64 container the inert proven
 *     agent runs under, addressed by an exact `@sha256` image digest.
 *
 * It is deliberately a FOUNDATION, not the generated-agent integration: the
 * Runtime carries only `AGENTCORE_MEMORY_ID` and nothing that wires LLM
 * inference or MCP tools. Generated-agent `LiteLLMModel`/`MCPClient` remains
 * the NEXT gate and is intentionally left unwired here — faking it would report
 * success for behaviour never exercised.
 *
 * Network posture: `networkMode = PUBLIC`, matching the live commit. A VPC
 * (`VPC` network mode) network configuration remains a documented next gate;
 * see the `networkMode` note below.
 *
 * The Runtime execution role is NOT created here. It is a stable, prior-stage
 * resource emitted by `D03WorkstreamRegistryRolesStack` (avoids the
 * fresh-IAM-role → AgentCore control-plane propagation race). This stack
 * imports it by its deterministic ARN (or an explicit override).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  CfnDeletionPolicy,
  CfnOutput,
  CustomResource,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Tags,
} from "aws-cdk-lib";
import { CfnMemory, CfnRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import { DockerImageAsset } from "aws-cdk-lib/aws-ecr-assets";
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { Key } from "aws-cdk-lib/aws-kms";
import {
  Code,
  Function as LambdaFunction,
  Runtime as LambdaRuntime,
} from "aws-cdk-lib/aws-lambda";
import { Provider } from "aws-cdk-lib/custom-resources";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";
import { createHash } from "node:crypto";
import { join } from "node:path";

/** Environment-qualified allocation tags, applied to every emitted resource. */
export interface AllocationTagInputs {
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
}

export interface D03WorkstreamRuntimeMemoryStackProps
  extends StackProps, AllocationTagInputs {
  readonly envName: "nonprod" | "prod";
  /**
   * Days short-term Memory events are retained. AgentCore requires 3–365.
   * Default 30.
   */
  readonly eventExpiryDays?: number;
  /**
   * Deterministic ARN of the stable, prior-stage Runtime execution role from
   * `D03WorkstreamRegistryRolesStack`. When omitted the ARN is derived from the
   * exact `AgenticAI-D03-<env>-<tenant>-<agent>-runtime` name family in this
   * stack's own account.
   */
  readonly runtimeExecutionRoleArnOverride?: string;
  /**
   * Production always retains its CMK. Nonproduction defaults to `DESTROY`
   * with a seven-day pending-deletion window; set true only for an explicit
   * state-retention test.
   */
  readonly retainMemoryKey?: boolean;
  /**
   * Which agent container image the Runtime runs. Defaults to
   * `"compatibility"` — the intentionally-inert handshake handler proven live
   * at commit `442de00`. Set to `"generated-agent"` to build the real Strands
   * reference agent (`scripts/live-agentcore-generated-agent-spike/agent`) that
   * wires `LiteLLMModel` + `MCPClient` + Memory. The image is still built,
   * scanned to zero HIGH findings, and consumed by digest exactly as before;
   * only the source directory changes. This is the offline bridge for the
   * generated-agent live `InvokeAgentRuntime` gate (roadmap Round 2.B).
   */
  readonly agentImageVariant?: "compatibility" | "generated-agent";
  /**
   * Container environment for the `"generated-agent"` variant. REQUIRED when
   * `agentImageVariant === "generated-agent"` and ignored otherwise — the inert
   * compatibility handler reads only `AGENTCORE_MEMORY_ID`. These values wire
   * the real agent's `LiteLLMModel` + `MCPClient`: the workstream tool Gateway
   * MCP URL, the (cross-account) Platform inference Gateway URL, the allow-listed
   * model id, the mandatory Guardrail id, and the exact subscribed qualified
   * tool names. `tenantId`/`agentId`/`envName` come from the props above.
   */
  readonly generatedAgentRuntimeConfig?: GeneratedAgentRuntimeConfig;
}

/** Runtime container environment for the generated-agent image variant. */
export interface GeneratedAgentRuntimeConfig {
  /** Workstream tool Gateway MCP endpoint URL (ends with `/mcp`). */
  readonly mcpGatewayUrl: string;
  /** Platform inference Gateway URL (OpenAI-compatible base derived from it). */
  readonly inferenceGatewayUrl: string;
  /** Allow-listed provider-qualified model id for inference. */
  readonly modelId: string;
  /** Mandatory Bedrock Guardrail identifier applied on every inference call. */
  readonly guardrailId: string;
  /** Exact subscribed qualified tool names (`<TargetName>___<ToolName>`). */
  readonly subscribedTools: readonly string[];
  /** OAuth scope required by the inference Gateway JWT authorizer. */
  readonly inferenceScope: string;
  /**
   * Platform M2M secret ARN (Stage A) holding the inference Gateway's Cognito
   * client id + secret + issuer/token endpoints. Read once at deploy time by
   * the credential-provider custom resource to seed CognitoOauth2.
   */
  readonly m2mSecretArn: string;
}

/** Native AgentCore network modes modeled by CfnRuntime. */
const NETWORK_MODE_PUBLIC = "PUBLIC";

/**
 * Inline handler for the credential-provider custom resource. Reads the
 * Platform M2M secret, then creates (Create/Update) or deletes (Delete) the
 * WorkloadIdentity + CognitoOauth2 credential provider. The client secret is
 * read in-process only and never logged or returned.
 */
const CREDENTIAL_PROVIDER_HANDLER = `
import json
import time
import boto3
from botocore.exceptions import ClientError

_NOT_FOUND = ("ResourceNotFoundException", "ResourceNotFound")
_TERMINAL = ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED")
_PENDING = ("CREATING", "UPDATING", "DELETING")


def _error_code(error):
    return error.response["Error"]["Code"]


def _is_not_found(error):
    return _error_code(error) in _NOT_FOUND


def _is_already_exists(error):
    code = _error_code(error)
    message = error.response["Error"].get("Message", "")
    return (
        "AlreadyExists" in code
        or "Conflict" in code
        or (code == "ValidationException" and "already exists" in message)
    )


def _assert_tags(client, resource_arn, expected_tags, allow_untagged=False):
    live = client.list_tags_for_resource(resourceArn=resource_arn).get("tags", {})
    if live == expected_tags:
        return
    if allow_untagged and live == {}:
        client.tag_resource(resourceArn=resource_arn, tags=expected_tags)
        live = client.list_tags_for_resource(resourceArn=resource_arn).get("tags", {})
        if live == expected_tags:
            return
    raise RuntimeError("Refusing resource with missing or foreign ownership tags")


def _assert_workload_owned(client, name, expected_arn, tags, allow_untagged=False):
    response = client.get_workload_identity(name=name)
    if response.get("name") != name or response.get("workloadIdentityArn") != expected_arn:
        raise RuntimeError("Workload identity response does not match the exact expected identity")
    _assert_tags(client, expected_arn, tags, allow_untagged)


def _assert_provider_owned(client, name, expected_arn, tags):
    response = client.get_oauth2_credential_provider(name=name)
    if response.get("name") != name or response.get("credentialProviderArn") != expected_arn:
        raise RuntimeError("Credential provider response does not match the exact expected provider")
    _assert_tags(client, expected_arn, tags)


def _wait_provider(client, provider_name):
    for _ in range(50):
        try:
            response = client.get_oauth2_credential_provider(name=provider_name)
        except ClientError as e:
            if _is_not_found(e):
                time.sleep(5)
                continue
            raise
        status = response.get("status")
        if status == "READY":
            return
        if status in _TERMINAL:
            raise RuntimeError("Credential provider reached terminal status " + str(status))
        if status not in _PENDING:
            raise RuntimeError("Credential provider returned unknown status " + repr(status))
        time.sleep(5)
    raise TimeoutError("Credential provider did not reach READY within 250 seconds")


def on_event(event, context):
    rt = event["RequestType"]
    props = event["ResourceProperties"]
    region = props["Region"]
    provider_name = props["ProviderName"]
    provider_arn = props["ProviderArn"]
    workload_name = props["WorkloadName"]
    workload_arn = props["WorkloadArn"]
    tags = props["Tags"]
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    if rt == "Delete":
        try:
            _assert_provider_owned(client, provider_name, provider_arn, tags)
            client.delete_oauth2_credential_provider(name=provider_name)
        except ClientError as e:
            if not _is_not_found(e):
                raise
        try:
            _assert_workload_owned(client, workload_name, workload_arn, tags)
            client.delete_workload_identity(name=workload_name)
        except ClientError as e:
            if not _is_not_found(e):
                raise
        return {"PhysicalResourceId": provider_name}

    # Create/Update: read the secret in-process only and bind it to the synth input.
    secrets = boto3.client("secretsmanager", region_name=region)
    raw = secrets.get_secret_value(SecretId=props["SecretArn"])["SecretString"]
    data = json.loads(raw)
    if data.get("scope") != props["Scope"]:
        raise RuntimeError("Platform M2M secret scope does not match the synth input")
    client_id = data["clientId"]
    client_secret = data["clientSecret"]

    # AgentCore returns ValidationException (not ConflictException) for an
    # existing WorkloadIdentity. Adopt only an exact identity with either all
    # expected tags or no tags from the known pre-tagging partial-create path.
    try:
        client.create_workload_identity(name=workload_name, tags=tags)
    except ClientError as e:
        if not _is_already_exists(e):
            raise
        _assert_workload_owned(
            client, workload_name, workload_arn, tags, allow_untagged=True
        )
    else:
        _assert_workload_owned(client, workload_name, workload_arn, tags)

    provider_config = {
        "includedOauth2ProviderConfig": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "issuer": data["issuer"],
            "authorizationEndpoint": data["authorizationEndpoint"],
            "tokenEndpoint": data["tokenEndpoint"],
        }
    }
    try:
        client.create_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor="CognitoOauth2",
            oauth2ProviderConfigInput=provider_config,
            tags=tags,
        )
    except ClientError as e:
        if not _is_already_exists(e):
            raise
        _assert_provider_owned(client, provider_name, provider_arn, tags)
        client.update_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor="CognitoOauth2",
            oauth2ProviderConfigInput=provider_config,
        )
    _wait_provider(client, provider_name)
    _assert_provider_owned(client, provider_name, provider_arn, tags)
    del client_secret, provider_config, data, raw
    return {"PhysicalResourceId": provider_name}
`;

/*
 * ---------------------------------------------------------------------------
 * Live-preflight image scan gate
 * ---------------------------------------------------------------------------
 * LANDMINE (live-observed 2026-09-21, Workstream account/us-west-2): the CDK
 * bootstrap container-assets repository had `scanOnPush=false`, no registry
 * scanning configuration, and zero scanned images. A `DescribeImages`-only
 * digest lookup therefore resolved a digest for an image whose vulnerability
 * posture had never been assessed, and handed it straight to `CreateRuntime`.
 *
 * This gate closes that hole at deploy time. It never mutates or deletes the
 * shared bootstrap repository: it reads the exact immutable asset tag, starts
 * at most one ECR basic scan for exactly that digest when no scan exists,
 * polls only that digest, and refuses to yield a container URI unless the scan
 * reached a usable status with zero CRITICAL and zero HIGH findings.
 *
 * Every status string below is taken verbatim from the pinned ECR service
 * model (`botocore` `ecr/2015-09-21`, shape `ScanStatus`): IN_PROGRESS,
 * COMPLETE, FAILED, UNSUPPORTED_IMAGE, ACTIVE, PENDING,
 * SCAN_ELIGIBILITY_EXPIRED, FINDINGS_UNAVAILABLE, LIMIT_EXCEEDED, and
 * IMAGE_ARCHIVED. Severity names come from shape `FindingSeverity`.
 */

/**
 * Statuses that carry trustworthy `findingSeverityCounts`. `COMPLETE` is the
 * ECR basic-scanning terminal success this gate drives towards; `ACTIVE` is the
 * equivalent Inspector enhanced-scanning status, accepted so that an account
 * which later enables enhanced scanning still converges instead of timing out.
 * Both are gated by the identical zero-CRITICAL/zero-HIGH check below.
 */
const USABLE_SCAN_STATUSES = ["COMPLETE", "ACTIVE"] as const;
/** Statuses that are still converging — keep polling, bounded by the waiter. */
const PENDING_SCAN_STATUSES = ["IN_PROGRESS", "PENDING"] as const;
/** Statuses that can never become usable — fail closed immediately. */
const TERMINAL_SCAN_STATUSES = [
  "FAILED",
  "UNSUPPORTED_IMAGE",
  "SCAN_ELIGIBILITY_EXPIRED",
  "FINDINGS_UNAVAILABLE",
  "LIMIT_EXCEEDED",
  "IMAGE_ARCHIVED",
] as const;
/** Any nonzero count in these severities blocks Runtime creation outright. */
const BLOCKING_SCAN_SEVERITIES = ["CRITICAL", "HIGH"] as const;
/** Bounded waiter: fixed 15 s polls inside a hard 30 min ceiling. */
const SCAN_POLL_INTERVAL = Duration.seconds(15);
const SCAN_TOTAL_TIMEOUT = Duration.minutes(30);
/**
 * Bounded per-call SDK budget. `total_max_attempts` includes the initial call,
 * unlike Config's `max_attempts`; two total attempts fit the Lambda timeout
 * even after the standard retry mode's maximum backoff.
 */
const SCAN_SDK_TOTAL_MAX_ATTEMPTS = 2;
const SCAN_SDK_CONNECT_TIMEOUT_SECONDS = 3;
const SCAN_SDK_READ_TIMEOUT_SECONDS = 10;

/**
 * Inline `onEvent`/`isComplete` handler for the image scan gate. Deliberately
 * composed of small single-purpose wrappers so a failure names the exact step
 * that refused. Every ECR call is read-only apart from one `StartImageScan`,
 * and `Delete` performs no API call at all — the bootstrap repository and its
 * images are shared infrastructure that outlive this stack.
 */
const AGENT_IMAGE_SCAN_GATE_HANDLER = `
import json
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{12}$")
CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _bounded_client():
    """One ECR client with an explicitly bounded retry/timeout budget."""
    return boto3.client(
        "ecr",
        config=Config(
            retries={
                "total_max_attempts": int(os.environ["SDK_TOTAL_MAX_ATTEMPTS"]),
                "mode": "standard",
            },
            connect_timeout=int(os.environ["SDK_CONNECT_TIMEOUT_SECONDS"]),
            read_timeout=int(os.environ["SDK_READ_TIMEOUT_SECONDS"]),
        ),
    )


def _status_set(name):
    """Parse one comma-separated status/severity allow-list from the template."""
    return {item.strip() for item in (os.environ.get(name) or "").split(",") if item.strip()}


def _deny(message):
    raise RuntimeError("AgentImageScanGate: " + message)


def _required_property(properties, key):
    value = properties.get(key)
    if not isinstance(value, str) or not value:
        _deny("missing required resource property '" + key + "'.")
    return value


def _checked_registry_id(registry_id):
    if not isinstance(registry_id, str) or not ACCOUNT_ID_PATTERN.match(registry_id):
        _deny("refusing a malformed registry id.")
    return registry_id


def _checked_content_hash(value, label):
    if not isinstance(value, str) or not CONTENT_HASH_PATTERN.match(value):
        _deny("refusing a malformed " + label + ".")
    return value


def _checked_digest(digest):
    """Refuse anything that is not an exact sha256 content address."""
    if not isinstance(digest, str) or not DIGEST_PATTERN.match(digest):
        _deny("refusing a malformed image digest.")
    return digest


def _scan_status(payload):
    return str(((payload.get("imageScanStatus") or {}).get("status") or "")).upper()


def _classify(status):
    """USABLE -> evaluate findings, PENDING -> poll, else fail closed."""
    if not status:
        return "ABSENT"
    if status in _status_set("USABLE_SCAN_STATUSES"):
        return "USABLE"
    if status in _status_set("PENDING_SCAN_STATUSES"):
        return "PENDING"
    if status in _status_set("TERMINAL_SCAN_STATUSES"):
        return "TERMINAL"
    return "UNKNOWN"


def _assert_image_identity(detail, registry_id, repository_name, image_tag):
    """Fail closed on any drift between what was asked for and what returned."""
    if detail.get("registryId") != registry_id:
        _deny("registry identity drift in DescribeImages.")
    if detail.get("repositoryName") != repository_name:
        _deny("repository identity drift in DescribeImages.")
    if image_tag not in (detail.get("imageTags") or []):
        _deny("image tag identity drift in DescribeImages.")
    return _checked_digest(detail.get("imageDigest"))


def resolve_tag_to_digest(client, registry_id, repository_name, image_tag):
    """Resolve the exact immutable asset tag to exactly one image detail."""
    response = client.describe_images(
        registryId=registry_id,
        repositoryName=repository_name,
        imageIds=[{"imageTag": image_tag}],
    )
    details = response.get("imageDetails") or []
    if len(details) != 1:
        _deny("expected exactly one image for the asset tag, observed " + str(len(details)) + ".")
    detail = details[0]
    return _assert_image_identity(detail, registry_id, repository_name, image_tag), detail


def start_basic_scan(client, registry_id, repository_name, digest):
    """Start at most one basic scan for exactly this digest."""
    try:
        response = client.start_image_scan(
            registryId=registry_id,
            repositoryName=repository_name,
            imageId={"imageDigest": digest},
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "LimitExceededException":
            # ECR permits one basic scan per image per 24 h. If this limit is
            # account-wide instead, the bounded findings poll still fails
            # closed because no scan can materialize for this exact digest.
            return "IN_PROGRESS"
        raise
    if response.get("registryId") != registry_id:
        _deny("registry identity drift in StartImageScan.")
    if response.get("repositoryName") != repository_name:
        _deny("repository identity drift in StartImageScan.")
    if ((response.get("imageId") or {}).get("imageDigest")) != digest:
        _deny("digest identity drift in StartImageScan.")
    return _scan_status(response) or "IN_PROGRESS"


def describe_digest_findings(client, registry_id, repository_name, digest):
    """Read scan state for ONLY this digest — never by mutable tag."""
    response = client.describe_image_scan_findings(
        registryId=registry_id,
        repositoryName=repository_name,
        imageId={"imageDigest": digest},
        maxResults=1,
    )
    if response.get("registryId") != registry_id:
        _deny("registry identity drift in DescribeImageScanFindings.")
    if response.get("repositoryName") != repository_name:
        _deny("repository identity drift in DescribeImageScanFindings.")
    if ((response.get("imageId") or {}).get("imageDigest")) != digest:
        _deny("digest identity drift in DescribeImageScanFindings.")
    return response


def blocking_findings(response):
    """Nonzero counts in the blocking severities, as an ordered dict."""
    counts = (response.get("imageScanFindings") or {}).get("findingSeverityCounts") or {}
    blocking = {}
    for severity in sorted(_status_set("BLOCKING_SCAN_SEVERITIES")):
        observed = int(counts.get(severity) or 0)
        if observed > 0:
            blocking[severity] = observed
    return blocking


def on_event(event, _context):
    """Resolve the digest and ensure a scan is running. No Delete-time calls."""
    request_type = event.get("RequestType")
    properties = event.get("ResourceProperties") or {}
    if request_type == "Delete":
        # Shared bootstrap repository and images are NEVER deleted here.
        return {"PhysicalResourceId": event.get("PhysicalResourceId")}
    if request_type not in ("Create", "Update"):
        _deny("unsupported request type '" + str(request_type) + "'.")
    registry_id = _checked_registry_id(_required_property(properties, "RegistryId"))
    repository_name = _required_property(properties, "RepositoryName")
    image_tag = _checked_content_hash(_required_property(properties, "ImageTag"), "image tag")
    asset_hash = _checked_content_hash(_required_property(properties, "AssetHash"), "asset hash")
    contract_hash = _checked_content_hash(
        _required_property(properties, "GateContractSha256"), "gate contract hash"
    )
    if image_tag != asset_hash:
        _deny("image tag does not equal the content-addressed asset hash.")
    client = _bounded_client()
    digest, detail = resolve_tag_to_digest(client, registry_id, repository_name, image_tag)
    status = _scan_status(detail)
    state = _classify(status)
    if state == "TERMINAL":
        _deny("refusing image with terminal scan status '" + status + "'.")
    if state == "UNKNOWN":
        _deny("refusing image with unknown scan status '" + status + "'.")
    if state == "ABSENT":
        status = start_basic_scan(client, registry_id, repository_name, digest)
        started_state = _classify(status)
        if started_state in ("ABSENT", "TERMINAL", "UNKNOWN"):
            _deny("refusing non-converging StartImageScan status '" + status + "'.")
    return {
        "PhysicalResourceId": "agent-image-scan-" + asset_hash + "-" + contract_hash[:12],
        "Data": {
            "ImageDigest": digest,
            "RegistryId": registry_id,
            "RepositoryName": repository_name,
            "ScanStatus": status,
        },
    }


def is_complete(event, _context):
    """Poll only the resolved digest; refuse on findings, terminal or unknown."""
    if event.get("RequestType") == "Delete":
        return {"IsComplete": True}
    properties = event.get("ResourceProperties") or {}
    registry_id = _checked_registry_id(_required_property(properties, "RegistryId"))
    repository_name = _required_property(properties, "RepositoryName")
    data = event.get("Data") or {}
    if data.get("RegistryId") != registry_id:
        _deny("registry identity drift between waiter phases.")
    if data.get("RepositoryName") != repository_name:
        _deny("repository identity drift between waiter phases.")
    digest = _checked_digest(data.get("ImageDigest"))
    try:
        response = describe_digest_findings(
            _bounded_client(), registry_id, repository_name, digest
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "ScanNotFoundException":
            # The started scan is not yet registered. Bounded by the waiter.
            return {"IsComplete": False}
        raise
    status = _scan_status(response)
    state = _classify(status)
    if state == "PENDING":
        return {"IsComplete": False}
    if state != "USABLE":
        _deny("refusing non-usable scan status '" + status + "'.")
    blocking = blocking_findings(response)
    if blocking:
        _deny(
            "refusing Runtime creation for digest "
            + digest
            + " with blocking findings "
            + json.dumps(blocking, sort_keys=True)
            + "."
        )
    return {
        "IsComplete": True,
        "Data": {
            "ImageDigest": digest,
            "RegistryId": registry_id,
            "RepositoryName": repository_name,
            "ScanStatus": status,
        },
    }
`;

/** Runtime is usable at this status; Memory at ACTIVE. */
export class D03WorkstreamRuntimeMemoryStack extends Stack {
  readonly memory: CfnMemory;
  readonly runtime: CfnRuntime;
  readonly memoryKey: Key;
  /**
   * Deploy-time preflight gate. Completes only when the exact asset digest has
   * a usable ECR scan with zero CRITICAL and zero HIGH findings.
   */
  readonly imageScanGate: CustomResource;
  /** Exact digest-pinned image URI (`<repositoryUri>@sha256:<digest>`). */
  readonly containerDigestUri: string;

  constructor(
    scope: Construct,
    id: string,
    props: D03WorkstreamRuntimeMemoryStackProps,
  ) {
    super(scope, id, props);

    // The scan gate adds Lambda, IAM, and Step Functions support resources.
    // Apply the mandatory five-tag contract at stack scope so every taggable
    // descendant receives the same allocation identity as Runtime and Memory.
    for (const [key, value] of Object.entries(
      this.allocationTagRecord(props),
    )) {
      Tags.of(this).add(key, value);
    }

    const eventExpiryDays = props.eventExpiryDays ?? 30;
    if (
      !Number.isSafeInteger(eventExpiryDays) ||
      eventExpiryDays < 3 ||
      eventExpiryDays > 365
    ) {
      throw new Error(
        "D03WorkstreamRuntimeMemoryStack: eventExpiryDays must be an integer from 3 through 365.",
      );
    }

    const base = `${props.envName}-${props.tenantId}-${props.agentId}`;
    // AgentCore Runtime/Memory names use the [A-Za-z][A-Za-z0-9_]* charset —
    // hyphens are invalid — so the hyphenated base is translated to underscores.
    const underscoreBase = base.replace(/-/g, "_");
    const runtimeName = `AgenticAI_D03_${underscoreBase}_runtime`;
    const memoryName = `AgenticAI_D03_${underscoreBase}_memory`;
    for (const [kind, name] of Object.entries({
      runtime: runtimeName,
      memory: memoryName,
    })) {
      if (name.length > 48 || !/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
        throw new Error(
          `D03WorkstreamRuntimeMemoryStack: ${kind} name '${name}' is invalid for AgentCore.`,
        );
      }
    }

    // Production is never reversible; nonprod may opt into DESTROY + 7d.
    const retainKey =
      props.envName === "prod" ? true : (props.retainMemoryKey ?? false);
    const keyRemovalPolicy = retainKey
      ? RemovalPolicy.RETAIN
      : RemovalPolicy.DESTROY;
    const keyPendingWindow = retainKey ? Duration.days(30) : Duration.days(7);

    // ---- Memory CMK (rotating, service-scoped) ----
    this.memoryKey = this.buildMemoryKey(
      base,
      memoryName,
      keyRemovalPolicy,
      keyPendingWindow,
    );
    for (const [key, value] of Object.entries(
      this.allocationTagRecord(props),
    )) {
      Tags.of(this.memoryKey).add(key, value);
    }

    // ---- Memory ----
    this.memory = new CfnMemory(this, "Memory", {
      name: memoryName,
      description: `AgentCore Memory for ${props.tenantId}/${props.agentId} (${props.envName}).`,
      eventExpiryDuration: eventExpiryDays,
      encryptionKeyArn: this.memoryKey.keyArn,
      tags: this.allocationTagRecord(props),
    });
    this.memory.applyRemovalPolicy(RemovalPolicy.DESTROY);
    if (props.envName === "prod") {
      this.memory.cfnOptions.updateReplacePolicy = CfnDeletionPolicy.RETAIN;
    }

    // ---- Container image (ARM64) resolved to an exact SCANNED digest ----
    const scanned = this.resolveScannedContainerDigestUri(props);
    this.imageScanGate = scanned.gate;
    this.containerDigestUri = scanned.uri;

    // ---- AgentCore Identity (generated-agent only) ----
    // WorkloadIdentity + CognitoOauth2 credential provider seeded from the
    // Platform M2M secret, so the Runtime can exchange its workload-identity
    // token for an inference-Gateway bearer via GetResourceOauth2Token.
    const identityProvider =
      props.agentImageVariant === "generated-agent"
        ? this.buildInferenceCredentialProvider(props)
        : undefined;

    // ---- Runtime ----
    const runtimeRoleArn = this.runtimeExecutionRoleArn(props);
    this.runtime = new CfnRuntime(this, "Runtime", {
      agentRuntimeName: runtimeName,
      description: `AgentCore Runtime for ${props.tenantId}/${props.agentId} (${props.envName}). ${
        props.agentImageVariant === "generated-agent"
          ? "Generated Strands agent; LiteLLMModel + MCPClient + Memory wired."
          : "Inert compatibility handler; LLM/MCP unwired."
      }`,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: this.containerDigestUri,
        },
      },
      networkConfiguration: {
        // PUBLIC matches live commit 88d5381. VPC network mode is a documented
        // next gate: it requires AgentCore-compatible subnets/SG plumbed
        // through networkModeConfig and independent live proof.
        networkMode: NETWORK_MODE_PUBLIC,
      },
      roleArn: runtimeRoleArn,
      // Compatibility variant reads ONLY AGENTCORE_MEMORY_ID. The generated
      // agent additionally needs its LLM/MCP/tenant wiring (validated below).
      environmentVariables: this.buildRuntimeEnvironment(props),
      tags: this.allocationTagRecord(props),
    });
    this.runtime.applyRemovalPolicy(RemovalPolicy.DESTROY);
    // Runtime depends on Memory: the memory id is injected into its env.
    this.runtime.addDependency(this.memory);
    // Runtime depends on the COMPLETED scan gate. The container URI already
    // references the gate's digest attribute, but the dependency is made
    // explicit so the ordering survives any future URI refactor.
    this.runtime.node.addDependency(this.imageScanGate);
    // Runtime depends on the credential provider so the inference bearer is
    // available on first invocation.
    if (identityProvider) {
      this.runtime.node.addDependency(identityProvider);
    }

    this.emitOutputs(runtimeRoleArn);
  }

  /**
   * Runtime container environment, variant-aware. The compatibility handler
   * reads only `AGENTCORE_MEMORY_ID`. The generated agent also needs its
   * tenant/agent/env identity, the mandatory Guardrail id, the allow-listed
   * model id, the workstream MCP tool Gateway URL, the Platform inference
   * Gateway URL, and the exact subscribed qualified tool names. Missing config
   * for the generated-agent variant fails closed at synth.
   */
  private buildRuntimeEnvironment(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): Record<string, string> {
    const env: Record<string, string> = {
      AGENTCORE_MEMORY_ID: this.memory.attrMemoryId,
    };
    if (props.agentImageVariant !== "generated-agent") {
      return env;
    }
    const cfg = props.generatedAgentRuntimeConfig;
    if (!cfg) {
      throw new Error(
        "D03WorkstreamRuntimeMemoryStack: generatedAgentRuntimeConfig is required when agentImageVariant is 'generated-agent'.",
      );
    }
    const missing = (
      [
        ["mcpGatewayUrl", cfg.mcpGatewayUrl],
        ["inferenceGatewayUrl", cfg.inferenceGatewayUrl],
        ["modelId", cfg.modelId],
        ["guardrailId", cfg.guardrailId],
        ["inferenceScope", cfg.inferenceScope],
        ["m2mSecretArn", cfg.m2mSecretArn],
      ] as const
    )
      .filter(([, v]) => !v || v.trim().length === 0)
      .map(([k]) => k);
    if (missing.length > 0) {
      throw new Error(
        `D03WorkstreamRuntimeMemoryStack: generatedAgentRuntimeConfig is missing required value(s): ${missing.join(", ")}.`,
      );
    }
    if (cfg.subscribedTools.length === 0) {
      throw new Error(
        "D03WorkstreamRuntimeMemoryStack: generatedAgentRuntimeConfig.subscribedTools must list at least one qualified tool name.",
      );
    }
    return {
      ...env,
      AGENTCORE_TENANT_ID: props.tenantId,
      AGENTCORE_AGENT_ID: props.agentId,
      AGENTCORE_ENV_NAME: props.envName,
      AGENTCORE_GUARDRAIL_ID: cfg.guardrailId,
      AGENTCORE_MODEL_ID: cfg.modelId,
      AGENTCORE_GATEWAY_URL: cfg.mcpGatewayUrl,
      AGENTCORE_INFERENCE_GATEWAY_URL: cfg.inferenceGatewayUrl,
      AGENTCORE_SUBSCRIBED_TOOLS: cfg.subscribedTools.join(","),
      AGENTCORE_INFERENCE_SCOPE: cfg.inferenceScope,
      AGENTCORE_INFERENCE_CREDENTIAL_PROVIDER:
        this.credentialProviderName(props),
      AGENTCORE_WORKLOAD_IDENTITY_NAME: this.workloadIdentityName(props),
    };
  }

  /**
   * WorkloadIdentity + CognitoOauth2 credential provider, created by a
   * Lambda-backed custom resource. On create/update it reads the Platform M2M
   * secret (cross-account, scoped) and calls CreateWorkloadIdentity +
   * CreateOauth2CredentialProvider (vendor CognitoOauth2). On delete it removes
   * both. The client secret is read in-process only and never logged or output.
   */
  private buildInferenceCredentialProvider(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): CustomResource {
    if (!props.generatedAgentRuntimeConfig) {
      throw new Error(
        "D03WorkstreamRuntimeMemoryStack: generatedAgentRuntimeConfig is required when agentImageVariant is 'generated-agent'.",
      );
    }
    const cfg = props.generatedAgentRuntimeConfig!;
    const providerName = this.credentialProviderName(props);
    const workloadName = this.workloadIdentityName(props);
    const providerArn = `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/oauth2credentialprovider/${providerName}`;
    const workloadArn = `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/${workloadName}`;

    const roleName = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-idprov`;
    if (roleName.length > 64) {
      throw new Error(
        `D03WorkstreamRuntimeMemoryStack: credential-provider role name '${roleName}' exceeds 64 characters.`,
      );
    }
    const role = new Role(this, "InferenceCredProviderRole", {
      roleName,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Pipeline-owned Workstream role that seeds the CognitoOauth2 credential provider from the Platform M2M secret.",
      inlinePolicies: {
        SeedCredentialProvider: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: "ReadPlatformM2mSecret",
              effect: Effect.ALLOW,
              actions: [
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
              ],
              resources: [cfg.m2mSecretArn],
            }),
            new PolicyStatement({
              sid: "DecryptPlatformM2mSecret",
              effect: Effect.ALLOW,
              actions: ["kms:Decrypt"],
              resources: ["*"],
              conditions: {
                StringEquals: {
                  "kms:ViaService": `secretsmanager.${this.region}.amazonaws.com`,
                  "kms:EncryptionContext:SecretARN": cfg.m2mSecretArn,
                },
              },
            }),
            new PolicyStatement({
              sid: "InitializeDefaultTokenVault",
              effect: Effect.ALLOW,
              actions: ["bedrock-agentcore:CreateTokenVault"],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
              ],
            }),
            new PolicyStatement({
              sid: "ManageIdentityAndProvider",
              effect: Effect.ALLOW,
              actions: [
                "bedrock-agentcore:CreateWorkloadIdentity",
                "bedrock-agentcore:GetWorkloadIdentity",
                "bedrock-agentcore:DeleteWorkloadIdentity",
                "bedrock-agentcore:CreateOauth2CredentialProvider",
                "bedrock-agentcore:GetOauth2CredentialProvider",
                "bedrock-agentcore:UpdateOauth2CredentialProvider",
                "bedrock-agentcore:DeleteOauth2CredentialProvider",
              ],
              // These control-plane actions take no resource-level ARN in the
              // current service model (SEC-011 family); scoped by account trust.
              resources: ["*"],
            }),
            new PolicyStatement({
              sid: "VerifyIdentityOwnershipTags",
              effect: Effect.ALLOW,
              actions: [
                "bedrock-agentcore:ListTagsForResource",
                "bedrock-agentcore:TagResource",
              ],
              resources: [providerArn, workloadArn],
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    const handler = new LambdaFunction(this, "InferenceCredProviderFn", {
      runtime: LambdaRuntime.PYTHON_3_13,
      handler: "index.on_event",
      timeout: Duration.minutes(5),
      role,
      code: Code.fromInline(CREDENTIAL_PROVIDER_HANDLER),
    });
    const provider = new Provider(this, "InferenceCredProviderProvider", {
      onEventHandler: handler,
    });
    const resource = new CustomResource(this, "InferenceCredProvider", {
      serviceToken: provider.serviceToken,
      properties: {
        // A change to any of these re-runs the custom resource.
        Region: this.region,
        SecretArn: cfg.m2mSecretArn,
        ProviderName: providerName,
        ProviderArn: providerArn,
        WorkloadName: workloadName,
        WorkloadArn: workloadArn,
        Scope: cfg.inferenceScope,
        Tags: this.allocationTagRecord(props),
      },
    });
    NagSuppressions.addResourceSuppressions(
      role,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-011: bedrock-agentcore WorkloadIdentity/Oauth2CredentialProvider control-plane actions take no resource-level ARN in the current service model; kms:Decrypt is constrained by ViaService + the exact secret's encryption context.",
        },
        {
          id: "AwsSolutions-IAM4",
          reason:
            "SEC-005: the AWS-managed AWSLambdaBasicExecutionRole is the standard least-privilege log-write policy for a custom-resource Lambda.",
        },
      ],
      true,
    );
    return resource;
  }

  /** Deterministic CognitoOauth2 credential-provider name for this workstream. */
  private credentialProviderName(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): string {
    // Underscore charset + bounded length, matching AgentCore name rules.
    return `AgenticAI_D03_${props.envName}_${props.tenantId}_${props.agentId}_inference`.replace(
      /-/g,
      "_",
    );
  }

  /** Deterministic workload-identity name for this workstream. */
  private workloadIdentityName(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): string {
    return `AgenticAI_D03_${props.envName}_${props.tenantId}_${props.agentId}`.replace(
      /-/g,
      "_",
    );
  }

  /** The five allocation tags, as a plain record for the native tags prop. */
  private allocationTagRecord(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): Record<string, string> {
    return {
      "application-id": props.applicationId,
      "agent-id": props.agentId,
      "tenant-id": props.tenantId,
      "cost-centre": props.costCentre,
      environment: props.envName,
    };
  }

  private buildMemoryKey(
    base: string,
    memoryName: string,
    removalPolicy: RemovalPolicy,
    pendingWindow: Duration,
  ): Key {
    const key = new Key(this, "MemoryKey", {
      alias: `alias/agenticai/d03-runtime-memory-${base}`,
      description: `Workstream-local AgentCore Memory CMK for ${base}.`,
      enableKeyRotation: true,
      pendingWindow,
      removalPolicy,
    });
    // Exact Memory name family + SourceAccount close the confused-deputy gap.
    const memoryArnLike = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${memoryName}-*`;
    key.addToResourcePolicy(
      new PolicyStatement({
        sid: "AllowAgentCoreMemoryCrypto",
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal("bedrock-agentcore.amazonaws.com")],
        actions: [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": memoryArnLike },
        },
      }),
    );
    key.addToResourcePolicy(
      new PolicyStatement({
        sid: "AllowAgentCoreMemoryCreateGrant",
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal("bedrock-agentcore.amazonaws.com")],
        actions: ["kms:CreateGrant"],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": memoryArnLike },
          Bool: { "kms:GrantIsForAWSResource": "true" },
          "ForAllValues:StringEquals": {
            "kms:GrantOperations": [
              "CreateGrant",
              "Decrypt",
              "DescribeKey",
              "GenerateDataKey",
              "GenerateDataKeyWithoutPlaintext",
              "ReEncryptFrom",
              "ReEncryptTo",
            ],
          },
        },
      }),
    );
    return key;
  }

  /**
   * Build the ARM64 image from the spike agent, resolve its content-addressed
   * ECR tag to an exact `@sha256` digest, and refuse to emit that digest unless
   * a scan for that exact digest reached a usable status with zero CRITICAL and
   * zero HIGH findings. Only `<repositoryUri>@sha256:<digest>` is passed into
   * the Runtime — never a mutable tag. Asset publishing is pipeline-owned.
   */
  private resolveScannedContainerDigestUri(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): { readonly uri: string; readonly gate: CustomResource } {
    const imageSpikeDir =
      props.agentImageVariant === "generated-agent"
        ? "live-agentcore-generated-agent-spike"
        : "live-agentcore-runtime-memory-spike";
    const asset = new DockerImageAsset(this, "AgentImage", {
      directory: join(
        __dirname,
        "..",
        "..",
        "..",
        "scripts",
        imageSpikeDir,
        "agent",
      ),
      platform: Platform.LINUX_ARM64,
    });

    const role = this.buildImageScanGateRole(props, asset);
    const gate = this.buildImageScanGate(props, asset, role);
    // repositoryUri has no tag/digest; append the gated digest by reference.
    return {
      uri: `${asset.repository.repositoryUri}@${gate.getAttString("ImageDigest")}`,
      gate,
    };
  }

  /**
   * Least-privilege role for the scan gate: the three exact ECR actions, scoped
   * to the exact bootstrap container-assets repository ARN. No image, tag, or
   * repository deletion action is granted — the shared bootstrap assets must
   * survive this stack's deletion.
   */
  private buildImageScanGateRole(
    props: D03WorkstreamRuntimeMemoryStackProps,
    asset: DockerImageAsset,
  ): Role {
    const roleName = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-imgscan`;
    if (roleName.length > 64) {
      throw new Error(
        `D03WorkstreamRuntimeMemoryStack: generated image scan gate role name '${roleName}' exceeds 64 characters.`,
      );
    }
    const role = new Role(this, "AgentImageScanGateRole", {
      roleName,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Pipeline-owned Workstream role that preflights the agent image scan before Runtime creation.",
      inlinePolicies: {
        ReadBootstrapImageScan: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: "PreflightAgentImageScan",
              effect: Effect.ALLOW,
              actions: [
                "ecr:DescribeImageScanFindings",
                "ecr:DescribeImages",
                "ecr:StartImageScan",
              ],
              resources: [asset.repository.repositoryArn],
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });
    NagSuppressions.addResourceSuppressions(
      role,
      [
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the pipeline-owned image scan gate Lambda.",
        },
      ],
      true,
    );
    return role;
  }

  /** The bounded `onEvent` + `isComplete` waiter that gates Runtime creation. */
  private buildImageScanGate(
    props: D03WorkstreamRuntimeMemoryStackProps,
    asset: DockerImageAsset,
    role: Role,
  ): CustomResource {
    const fullNamePrefix = `agenticai-d03-${props.envName}-${props.tenantId}-${props.agentId}-imgscan`;
    const namePrefix =
      fullNamePrefix.length <= 61
        ? fullNamePrefix
        : `${fullNamePrefix.slice(0, 52)}-${createHash("sha256")
            .update(fullNamePrefix)
            .digest("hex")
            .slice(0, 8)}`;
    const environment: Record<string, string> = {
      USABLE_SCAN_STATUSES: USABLE_SCAN_STATUSES.join(","),
      PENDING_SCAN_STATUSES: PENDING_SCAN_STATUSES.join(","),
      TERMINAL_SCAN_STATUSES: TERMINAL_SCAN_STATUSES.join(","),
      BLOCKING_SCAN_SEVERITIES: BLOCKING_SCAN_SEVERITIES.join(","),
      SDK_TOTAL_MAX_ATTEMPTS: String(SCAN_SDK_TOTAL_MAX_ATTEMPTS),
      SDK_CONNECT_TIMEOUT_SECONDS: String(SCAN_SDK_CONNECT_TIMEOUT_SECONDS),
      SDK_READ_TIMEOUT_SECONDS: String(SCAN_SDK_READ_TIMEOUT_SECONDS),
    };
    // ServiceToken does not change when inline handler code or environment
    // changes. Carry their digest as a resource property so every scan-policy
    // revision forces CloudFormation to invoke and re-evaluate this gate.
    const gateContractSha256 = createHash("sha256")
      .update(AGENT_IMAGE_SCAN_GATE_HANDLER)
      .update("\0")
      .update(JSON.stringify(environment))
      .digest("hex");
    const onEvent = new LambdaFunction(this, "AgentImageScanGateOnEvent", {
      functionName: `${namePrefix}-oe`,
      runtime: LambdaRuntime.PYTHON_3_13,
      handler: "index.on_event",
      timeout: Duration.minutes(2),
      memorySize: 256,
      description:
        "Resolves the exact agent image digest and ensures an ECR scan exists — onEvent.",
      code: Code.fromInline(AGENT_IMAGE_SCAN_GATE_HANDLER),
      environment,
      role,
    });
    const isComplete = new LambdaFunction(
      this,
      "AgentImageScanGateIsComplete",
      {
        functionName: `${namePrefix}-ic`,
        runtime: LambdaRuntime.PYTHON_3_13,
        handler: "index.is_complete",
        timeout: Duration.minutes(1),
        memorySize: 256,
        description:
          "Polls only the resolved digest and refuses CRITICAL/HIGH findings — isComplete.",
        code: Code.fromInline(AGENT_IMAGE_SCAN_GATE_HANDLER),
        environment,
        role,
      },
    );
    const provider = new Provider(this, "AgentImageScanGateProvider", {
      onEventHandler: onEvent,
      isCompleteHandler: isComplete,
      queryInterval: SCAN_POLL_INTERVAL,
      totalTimeout: SCAN_TOTAL_TIMEOUT,
    });
    const gate = new CustomResource(this, "AgentImageScanGate", {
      resourceType: "Custom::AgenticAIAgentImageScanGate",
      serviceToken: provider.serviceToken,
      properties: {
        RegistryId: this.account,
        RepositoryName: asset.repository.repositoryName,
        ImageTag: asset.imageTag,
        AssetHash: asset.assetHash,
        GateContractSha256: gateContractSha256,
      },
    });
    this.suppressImageScanGateNag(onEvent, isComplete, provider);
    return gate;
  }

  private suppressImageScanGateNag(
    onEvent: LambdaFunction,
    isComplete: LambdaFunction,
    provider: Provider,
  ): void {
    for (const fn of [onEvent, isComplete]) {
      NagSuppressions.addResourceSuppressions(
        fn,
        [
          {
            id: "AwsSolutions-L1",
            reason:
              "SEC-006: pinned to the latest Lambda Python runtime carrying the bundled boto3 whose ECR scan contracts this handler targets.",
          },
          {
            id: "NIST.800.53.R5-LambdaConcurrency",
            reason: "SEC-007: CloudFormation-only invocation.",
          },
          {
            id: "NIST.800.53.R5-LambdaDLQ",
            reason: "SEC-008: CFN surfaces failures via stack events.",
          },
          {
            id: "NIST.800.53.R5-LambdaInsideVPC",
            reason: "SEC-009: ECR control-plane public IAM-auth endpoint.",
          },
        ],
        true,
      );
    }
    NagSuppressions.addResourceSuppressions(
      provider,
      [
        {
          id: "AwsSolutions-IAM4",
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for CDK provider framework Lambdas.",
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-029: the Provider framework invokes versions/aliases of the two scan gate Lambdas created in this stack.",
        },
        {
          id: "AwsSolutions-L1",
          reason:
            "SEC-006: Provider framework Lambda runtime is managed by aws-cdk-lib.",
        },
        {
          id: "AwsSolutions-SF1",
          reason:
            "SEC-029: CDK Provider framework waiter Step Function; logging config is framework-owned.",
        },
        {
          id: "AwsSolutions-SF2",
          reason:
            "SEC-029: CDK Provider framework waiter Step Function; X-Ray config is framework-owned.",
        },
        {
          id: "NIST.800.53.R5-LambdaConcurrency",
          reason: "SEC-007: provisioning-time only.",
        },
        {
          id: "NIST.800.53.R5-LambdaDLQ",
          reason: "SEC-008: CloudFormation surfaces failures.",
        },
        {
          id: "NIST.800.53.R5-LambdaInsideVPC",
          reason: "SEC-009: control-plane only.",
        },
      ],
      true,
    );
  }

  /**
   * The stable, prior-stage Runtime execution role ARN. Derived from the exact
   * name family unless an override is supplied.
   */
  private runtimeExecutionRoleArn(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): string {
    const roleName = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-runtime`;
    if (props.runtimeExecutionRoleArnOverride) {
      const match =
        /^arn:(?:aws|aws-cn|aws-us-gov):iam::(\d{12}):role\/(.+)$/.exec(
          props.runtimeExecutionRoleArnOverride,
        );
      if (!match || match[1] !== this.account || match[2] !== roleName) {
        throw new Error(
          "D03WorkstreamRuntimeMemoryStack: Runtime execution role override must match the exact prior-stage role ARN.",
        );
      }
      return props.runtimeExecutionRoleArnOverride;
    }
    return `arn:${this.partition}:iam::${this.account}:role/${roleName}`;
  }

  private emitOutputs(runtimeRoleArn: string): void {
    // Non-secret identity/status outputs only — never a secret.
    new CfnOutput(this, "RuntimeArn", {
      description: "AgentCore Runtime ARN (non-secret).",
      value: this.runtime.attrAgentRuntimeArn,
    });
    new CfnOutput(this, "RuntimeId", {
      description: "AgentCore Runtime id (non-secret).",
      value: this.runtime.attrAgentRuntimeId,
    });
    new CfnOutput(this, "RuntimeStatus", {
      description: "AgentCore Runtime status (non-secret).",
      value: this.runtime.attrStatus,
    });
    new CfnOutput(this, "MemoryId", {
      description: "AgentCore Memory id (non-secret).",
      value: this.memory.attrMemoryId,
    });
    new CfnOutput(this, "MemoryStatus", {
      description: "AgentCore Memory status (non-secret).",
      value: this.memory.attrStatus,
    });
    new CfnOutput(this, "ContainerDigestUri", {
      description: "Exact digest-pinned Runtime container image URI.",
      value: this.containerDigestUri,
    });
    new CfnOutput(this, "RuntimeExecutionRoleArn", {
      description: "Imported stable Runtime execution role ARN.",
      value: runtimeRoleArn,
    });
  }
}
