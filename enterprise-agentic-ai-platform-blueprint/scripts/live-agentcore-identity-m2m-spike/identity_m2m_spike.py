#!/usr/bin/env python3
"""Cleanup-first AgentCore Identity M2M compatibility spike.

A narrow, ephemeral probe of AgentCore Identity's machine-to-machine (M2M) path.
It creates exactly two resources it owns -- one AgentCore **workload identity**
and one **OAuth2 credential provider** (vendor ``CognitoOauth2``) wired to an
EXISTING Cognito app client the caller already provisioned -- proves the provider
reaches ``READY``, obtains a workload access token, exchanges it for an M2M
resource token for an exact scope, proves that bearer token discovers the
target-qualified model on the existing inference Gateway and drives a real
Strands ``LiteLLMModel`` (non-streaming + streaming), and then deletes
everything it created -- discovering partial creates so nothing leaks.

Strict boundary: the caller owns the Cognito user pool, its app client, the
resource server + scope, and the inference Gateway. This probe owns ONLY the
workload identity and the credential provider.

Commands: ``preflight``, ``deploy``, ``verify``, ``cleanup``, ``run-all``.

Secret discipline (non-negotiable):

* The existing Cognito app-client secret is read in-process via
  ``DescribeUserPoolClient`` and passed straight into
  ``CreateOauth2CredentialProvider``. It is NEVER written to state, evidence,
  logs, or stdout.
* Workload/resource tokens live only in local variables while a token exchange
  or inference call is in flight. Evidence records booleans, safe lengths, and
  one-way fingerprints of NON-secret ids only -- never a token, token prefix,
  ARN, account id, or client secret.
* Every evidence write is scanned recursively against credential/JWT patterns
  and refused if anything credential-shaped slips through.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# Standalone path bootstrap for the sibling gateway-spike helpers.
# --------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_GATEWAY_SPIKE_DIR = _THIS_DIR.parent / "live-agentcore-gateway-spike"
for _p in (_THIS_DIR, _GATEWAY_SPIKE_DIR):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import boto3  # noqa: E402
import botocore  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import identity_m2m_model as model  # noqa: E402
from gateway_spike import (  # noqa: E402
    Evidence,
    JsonStore,
    SpikeError,
    aws_error_code,
    utc_now,
)

# --------------------------------------------------------------------------
# Mutation / side-effect registry -- the contract the reachability test enforces
# --------------------------------------------------------------------------

#: Lifecycle mutations. A new mutating call must be registered here.
MUTATING_API_CALLS = frozenset(
    {
        "create_workload_identity",
        "delete_workload_identity",
        "create_oauth2_credential_provider",
        "delete_oauth2_credential_provider",
    }
)
#: Token-minting calls are not lifecycle writes, but they ARE side-effecting
#: (they mint credentials) and are billable/rate-limited, so ``verify`` must opt
#: into them explicitly and no other phase may reach them.
SIDE_EFFECTING_API_CALLS = frozenset(
    {"get_workload_access_token", "get_resource_oauth2_token"}
)
SCOPED_API_CALLS = MUTATING_API_CALLS | SIDE_EFFECTING_API_CALLS

#: Only this error code is tolerated during cleanup ("already gone").
NOT_FOUND_CODES = frozenset({"ResourceNotFoundException"})

# Bounded polling budgets.
PROVIDER_READY_TIMEOUT_SECONDS = 300
ABSENT_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5

# Pagination guardrails.
MAX_LIST_PAGES = 100
LIST_PAGE_SIZE = 50

# Bounded inference probe.
INFERENCE_MAX_TOKENS = 256

SUCCESSFUL_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
REQUIRED_INFERENCE_OUTPUTS = frozenset(
    {
        "CognitoUserPoolId",
        "CognitoClientId",
        "TokenEndpoint",
        "GatewayUrl",
        "OAuthScope",
        "InferenceTargetName",
    }
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpikeConfig:
    account_id: str
    region: str
    prefix: str
    source_revision: str
    stack_name: str
    user_pool_id: str
    client_id: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    resource_scope: str
    gateway_url: str
    model_id: str
    state_path: Path
    evidence_path: Path


def _scratch_file(scratch_path: Path, raw: str | None, default_name: str, label: str) -> Path:
    candidate = Path(raw).expanduser() if raw else scratch_path / default_name
    resolved = candidate.resolve()
    try:
        resolved.relative_to(scratch_path)
    except ValueError as error:
        raise SpikeError(f"{label} must remain under KIROCREW_SCRATCH") from error
    if resolved == scratch_path:
        raise SpikeError(f"{label} must name a file, not the scratch directory")
    return resolved


def build_config(args: argparse.Namespace) -> SpikeConfig:
    if not model.ACCOUNT_PATTERN.fullmatch(str(args.account_id)):
        raise SpikeError("--account-id must contain exactly 12 digits")
    if not model.region_is_supported(str(args.region)):
        raise SpikeError(
            f"Region {args.region!r} is not in the documented AgentCore Identity "
            f"region list {sorted(model.SUPPORTED_REGIONS)}"
        )
    if not model.PREFIX_PATTERN.fullmatch(str(args.prefix)):
        raise SpikeError("--prefix is not a valid lowercase resource prefix")
    try:
        source_revision = model.validate_source_revision(str(args.source_revision))
        stack_name = model.validate_stack_name(str(args.stack_name))
        user_pool_id = model.validate_user_pool_id(
            str(args.user_pool_id), region=str(args.region)
        )
        client_id = model.validate_client_id(str(args.client_id))
        issuer, authorization_endpoint, token_endpoint = (
            model.validate_cognito_endpoint_bundle(
                region=str(args.region),
                user_pool_id=user_pool_id,
                issuer=str(args.issuer),
                authorization_endpoint=str(args.authorization_endpoint),
                token_endpoint=str(args.token_endpoint),
            )
        )
        resource_scope = model.validate_scope(str(args.resource_scope))
        gateway_url = model.validate_gateway_url(
            str(args.gateway_url), region=str(args.region)
        )
        model_id = model.validate_target_qualified_model_id(str(args.model_id))
    except model.ModelError as error:
        raise SpikeError(str(error)) from error

    scratch = os.environ.get("KIROCREW_SCRATCH")
    if not scratch:
        raise SpikeError("KIROCREW_SCRATCH must be set; refusing shared /tmp state")
    scratch_path = Path(scratch).resolve()
    state_path = _scratch_file(
        scratch_path, args.state_file, f"{args.prefix}-identity-m2m-state.json",
        "--state-file",
    )
    evidence_path = _scratch_file(
        scratch_path, args.evidence_file, f"{args.prefix}-identity-m2m-evidence.json",
        "--evidence-file",
    )
    if state_path == evidence_path:
        raise SpikeError("--state-file and --evidence-file must be different files")
    return SpikeConfig(
        account_id=str(args.account_id),
        region=str(args.region),
        prefix=str(args.prefix),
        source_revision=source_revision,
        stack_name=stack_name,
        user_pool_id=user_pool_id,
        client_id=client_id,
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        resource_scope=resource_scope,
        gateway_url=gateway_url,
        model_id=model_id,
        state_path=state_path,
        evidence_path=evidence_path,
    )


# --------------------------------------------------------------------------
# Evidence with recursive value scanning + request-id fingerprinting
# --------------------------------------------------------------------------


class SpikeEvidence(Evidence):
    SCHEMA_VERSION = 1

    def __init__(self, path: Path, config: SpikeConfig, run_marker: str) -> None:  # noqa: D107
        self.store = JsonStore(path)
        expected_run = {
            "prefix": config.prefix,
            "region": config.region,
            "accountSuffix": model.account_suffix(config.account_id),
            "runFingerprint": model.fingerprint(run_marker),
            "sourceRevisionShort": config.source_revision[:12],
            "sourceRevisionFingerprint": model.fingerprint(config.source_revision),
            "stackNameFingerprint": model.fingerprint(config.stack_name),
            "clientIdFingerprint": model.fingerprint(config.client_id),
            "boto3Version": boto3.__version__,
            "botocoreVersion": botocore.__version__,
        }
        current = self.store.read()
        if current:
            try:
                model.assert_no_secret_values(current)
            except model.ModelError as error:
                raise SpikeError(
                    "Existing evidence contains a forbidden identifier"
                ) from error
            persisted_run = current.get("run")
            if (
                current.get("schemaVersion") != self.SCHEMA_VERSION
                or current.get("spike") != "agentcore-identity-m2m"
                or not isinstance(persisted_run, Mapping)
                or any(persisted_run.get(k) != v for k, v in expected_run.items())
                or not isinstance(current.get("events"), list)
                or not isinstance(current.get("unknowns"), list)
            ):
                raise SpikeError("Existing evidence does not belong to this exact run")
            self.document = current
            return
        self.document: dict[str, Any] = {
            "schemaVersion": self.SCHEMA_VERSION,
            "spike": "agentcore-identity-m2m",
            "run": {**expected_run, "startedAt": utc_now()},
            "unknowns": [],
            "events": [],
        }

    def add(self, event: str, **details: Any) -> None:
        # Do NOT delegate to the parent's coarser forbidden-key check: it would
        # reject safe derived-metadata keys like ``tokenLength``. This spike's
        # model applies the authoritative recursive secret scan (keys + values),
        # which allows length/fingerprint/count/suffix metadata while still
        # refusing any credential-shaped key or value.
        model.assert_no_secret_values(details, path=event)
        records = self.document.setdefault("events", [])
        if not isinstance(records, list):
            raise SpikeError("Evidence events field is not an array")
        records.append({"at": utc_now(), "event": event, **details})
        self.store.write(self.document)

    def add_unknown(self, code: str, detail: str) -> None:
        record = model.unknown(code, model.sanitize_error(detail, prefix=code))
        model.assert_no_secret_values(record, path="unknown")
        record["at"] = utc_now()
        unknowns = self.document.setdefault("unknowns", [])
        if not isinstance(unknowns, list):
            raise SpikeError("Evidence unknowns field is not an array")
        unknowns.append(record)
        self.store.write(self.document)

    def finish(self, status: str) -> None:
        current = self.document.get("status")
        if status == "cleanup-passed" and current in {"passed", "failed"}:
            self.document["lastCleanupAt"] = utc_now()
            self.store.write(self.document)
            return
        super().finish(status)


def request_fingerprint(response: Mapping[str, Any]) -> str:
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, Mapping) else {}
    value = metadata.get("RequestId") if isinstance(metadata, Mapping) else None
    return model.fingerprint(str(value)) if value else "n/a"


def client_error_http_status(error: ClientError) -> int | None:
    metadata = error.response.get("ResponseMetadata", {})
    value = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, int) else None


# --------------------------------------------------------------------------
# Secret / token providers -- injectable so unit tests use sentinels
# --------------------------------------------------------------------------


class CognitoSecretReader:
    """Reads the EXISTING app-client secret in-process via DescribeUserPoolClient.

    The secret is returned to the caller and used exactly once (building the
    provider config). It is never stored on ``self`` beyond the call return.
    Injectable: unit tests pass a fake that returns a sentinel and assert the
    sentinel never reaches disk/output.
    """

    def __init__(self, session: "boto3.Session") -> None:
        self._cognito = session.client("cognito-idp")

    def verify_pool_domain(
        self, *, user_pool_id: str, region: str, token_endpoint: str
    ) -> None:
        response = self._cognito.describe_user_pool(UserPoolId=user_pool_id)
        pool = response.get("UserPool", {}) if isinstance(response, Mapping) else {}
        domain = pool.get("Domain")
        if not isinstance(domain, str) or not domain:
            raise SpikeError("Cognito user pool has no managed domain")
        expected = f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token"
        if token_endpoint != expected:
            raise SpikeError("Cognito token endpoint does not belong to the user pool")

    def read_client_secret(
        self, *, user_pool_id: str, client_id: str, resource_scope: str
    ) -> str:
        response = self._cognito.describe_user_pool_client(
            UserPoolId=user_pool_id, ClientId=client_id
        )
        client = response.get("UserPoolClient", {}) if isinstance(response, Mapping) else {}
        if "client_credentials" not in client.get("AllowedOAuthFlows", []):
            raise SpikeError("Cognito app client does not allow client_credentials")
        if client.get("AllowedOAuthFlowsUserPoolClient") is not True:
            raise SpikeError("Cognito app client OAuth flows are not enabled")
        if resource_scope not in client.get("AllowedOAuthScopes", []):
            raise SpikeError("Cognito app client does not allow the exact Gateway scope")
        secret = client.get("ClientSecret")
        if not secret:
            raise SpikeError("Cognito app client has no client secret")
        return str(secret)


# --------------------------------------------------------------------------
# API wrappers -- one named function per AWS operation, scope-guarded
# --------------------------------------------------------------------------


class IdentityM2mApi:
    def __init__(self, session: "boto3.Session", config: SpikeConfig) -> None:
        self.config = config
        self.sts = session.client("sts")
        self.cloudformation = session.client("cloudformation")
        self.control = session.client(model.CONTROL_SERVICE)
        self.data = session.client(model.DATA_SERVICE)
        self.mutation_scope: frozenset[str] = frozenset()

    def _require_scope(self, operation: str) -> None:
        if operation not in SCOPED_API_CALLS:
            raise SpikeError(f"Operation {operation!r} is not in the scope registry")
        if operation not in self.mutation_scope:
            raise SpikeError(
                f"Refusing {operation!r}: the current command's scope is "
                f"{sorted(self.mutation_scope) or 'read-only'}"
            )

    def capability(self, service: str, operation_name: str, member: str) -> bool:
        client = self.control if service == model.CONTROL_SERVICE else self.data
        try:
            shape = client.meta.service_model.operation_model(operation_name).input_shape
        except Exception:  # pragma: no cover - defensive
            return False
        return bool(shape is not None and member in shape.members)

    # -- identity -------------------------------------------------------
    def get_caller_identity(self) -> Mapping[str, Any]:
        return self.sts.get_caller_identity()

    def describe_inference_stack(self, stack_name: str) -> tuple[str, dict[str, str]]:
        response = self.cloudformation.describe_stacks(StackName=stack_name)
        stacks = response.get("Stacks", []) if isinstance(response, Mapping) else []
        if len(stacks) != 1:
            raise SpikeError("Expected exactly one inference stack")
        stack = stacks[0]
        status = str(stack.get("StackStatus", ""))
        outputs = {
            str(item["OutputKey"]): str(item["OutputValue"])
            for item in stack.get("Outputs", [])
            if item.get("OutputKey") and item.get("OutputValue")
        }
        return status, outputs

    # -- workload identity lifecycle -----------------------------------
    def create_workload_identity(
        self, name: str, tags: Mapping[str, str]
    ) -> tuple[Mapping[str, Any], str]:
        self._require_scope("create_workload_identity")
        response = self.control.create_workload_identity(name=name, tags=dict(tags))
        return response, request_fingerprint(response)

    def get_workload_identity(self, name: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_workload_identity(name=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def list_workload_identities(self) -> Iterator[Mapping[str, Any]]:
        yield from _paginate(
            lambda token: self.control.list_workload_identities(**_page_kwargs(token)),
            *model.LIST_OUTPUT_FIELDS["ListWorkloadIdentities"],
        )

    def delete_workload_identity(self, name: str) -> None:
        self._require_scope("delete_workload_identity")
        try:
            self.control.delete_workload_identity(name=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return
            raise

    # -- credential-provider lifecycle ---------------------------------
    def create_oauth2_credential_provider(
        self, name: str, provider_config: Mapping[str, Any], tags: Mapping[str, str]
    ) -> tuple[Mapping[str, Any], str]:
        """Create the provider.

        ``provider_config`` already contains the caller's client secret in the
        ``includedOauth2ProviderConfig`` union member. It flows straight into the
        request body and this method returns only a fingerprint of the request
        id -- never the response's ``clientSecretArn``/``credentialProviderArn``.
        """
        self._require_scope("create_oauth2_credential_provider")
        response = self.control.create_oauth2_credential_provider(
            name=name,
            credentialProviderVendor=model.CREDENTIAL_PROVIDER_VENDOR,
            oauth2ProviderConfigInput=dict(provider_config),
            tags=dict(tags),
        )
        return response, request_fingerprint(response)

    def get_oauth2_credential_provider(self, name: str) -> Mapping[str, Any] | None:
        try:
            return self.control.get_oauth2_credential_provider(name=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return None
            raise

    def list_oauth2_credential_providers(self) -> Iterator[Mapping[str, Any]]:
        yield from _paginate(
            lambda token: self.control.list_oauth2_credential_providers(
                **_page_kwargs(token)
            ),
            *model.LIST_OUTPUT_FIELDS["ListOauth2CredentialProviders"],
        )

    def delete_oauth2_credential_provider(self, name: str) -> None:
        self._require_scope("delete_oauth2_credential_provider")
        try:
            self.control.delete_oauth2_credential_provider(name=name)
        except ClientError as error:
            if aws_error_code(error) in NOT_FOUND_CODES:
                return
            raise

    def list_resource_tags(self, resource_arn: str) -> Mapping[str, str]:
        response = self.control.list_tags_for_resource(resourceArn=resource_arn)
        tags = response.get("tags", {}) if isinstance(response, Mapping) else {}
        if not isinstance(tags, Mapping):
            raise SpikeError("ListTagsForResource returned a non-object tags field")
        return {str(key): str(value) for key, value in tags.items()}

    # -- token data plane ----------------------------------------------
    def get_workload_access_token(self, workload_name: str) -> str:
        """Mint a workload access token. Returns the raw token to the caller ONLY.

        The token is never persisted; the caller uses it for the immediate
        resource-token exchange and drops it.
        """
        self._require_scope("get_workload_access_token")
        response = self.data.get_workload_access_token(workloadName=workload_name)
        token = response.get("workloadAccessToken") if isinstance(response, Mapping) else None
        if not token:
            raise SpikeError("GetWorkloadAccessToken returned no workloadAccessToken")
        return str(token)

    def get_resource_oauth2_token(
        self, workload_identity_token: str, provider_name: str, scopes: Sequence[str]
    ) -> str:
        """Exchange the workload token for an M2M resource token. Returns raw token."""
        self._require_scope("get_resource_oauth2_token")
        response = self.data.get_resource_oauth2_token(
            workloadIdentityToken=workload_identity_token,
            resourceCredentialProviderName=provider_name,
            scopes=list(scopes),
            oauth2Flow=model.OAUTH2_FLOW_M2M,
        )
        token = response.get("accessToken") if isinstance(response, Mapping) else None
        if not token:
            raise SpikeError("GetResourceOauth2Token returned no accessToken")
        return str(token)


def _page_kwargs(token: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"maxResults": LIST_PAGE_SIZE}
    if token:
        kwargs["nextToken"] = token
    return kwargs


def _paginate(
    call: Callable[[str | None], Mapping[str, Any]], items_key: str, token_key: str
) -> Iterator[Mapping[str, Any]]:
    """Bounded pagination with repeated-token/runaway protection."""
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_LIST_PAGES):
        response = call(token)
        for item in response.get(items_key, []) or []:
            if isinstance(item, Mapping):
                yield item
        token = response.get(token_key)
        if not token:
            return
        if token in seen_tokens:
            raise SpikeError("Pagination returned a repeated nextToken; aborting")
        seen_tokens.add(str(token))
    raise SpikeError(f"Pagination exceeded {MAX_LIST_PAGES} pages; aborting")


# --------------------------------------------------------------------------
# Inference probe -- proves the M2M token works against the existing Gateway
# --------------------------------------------------------------------------


class InferenceProbe:
    """Proves a bearer token discovers the target-qualified model and drives
    a real Strands ``LiteLLMModel`` (non-streaming + streaming).

    Kept behind a wrapper so unit tests substitute a fake that never imports
    ``strands``/``litellm`` and asserts the injected token is used but never
    logged. The token is passed as an argument on each call, never stored.
    """

    def __init__(self, config: SpikeConfig, http_get: Callable[..., Any] | None = None) -> None:
        self.config = config
        self._http_get = http_get

    def discover_models(self, bearer_token: str) -> list[str]:
        import urllib.request  # local import: only needed on the live path

        if self._http_get is not None:
            return list(self._http_get(bearer_token))
        url = f"{self.config.gateway_url}/inference/v1/models"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status != 200:
                raise SpikeError(f"Model discovery returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        return [str(item["id"]) for item in payload.get("data", []) if item.get("id")]

    def run_litellm(self, bearer_token: str, *, stream: bool) -> int:
        """Instantiate a real ``LiteLLMModel`` and prove one bounded reply.

        Returns the number of returned content blocks. Imports ``strands`` lazily
        so the offline test suite never needs it.
        """
        from strands import Agent  # noqa: E402
        from strands.models.litellm import LiteLLMModel  # noqa: E402

        llm = LiteLLMModel(
            model_id=f"openai/{self.config.model_id}",
            client_args={
                "api_base": f"{self.config.gateway_url}/inference/v1",
                "api_key": bearer_token,
            },
            params={"max_tokens": INFERENCE_MAX_TOKENS, "temperature": 0, "stream": stream},
        )
        agent = Agent(model=llm, callback_handler=None)
        result = agent("Reply with exactly the word verified.")
        message = result.message
        if not message or not message.get("content"):
            raise SpikeError("LiteLLMModel returned no Strands message content")
        return len(message["content"])


# --------------------------------------------------------------------------
# Spike orchestration
# --------------------------------------------------------------------------


class IdentityM2mSpike:
    def __init__(
        self,
        config: SpikeConfig,
        *,
        session: "boto3.Session | None" = None,
        secret_reader: "CognitoSecretReader | None" = None,
        inference_probe: "InferenceProbe | None" = None,
    ) -> None:
        if (
            boto3.__version__ != model.REQUIRED_BOTO3_VERSION
            or botocore.__version__ != model.REQUIRED_BOTOCORE_VERSION
        ):
            raise SpikeError(
                "Pinned SDK mismatch: required "
                f"boto3 {model.REQUIRED_BOTO3_VERSION} / "
                f"botocore {model.REQUIRED_BOTOCORE_VERSION}; found "
                f"boto3 {boto3.__version__} / botocore {botocore.__version__}"
            )
        self.config = config
        self.session = session or boto3.Session(region_name=config.region)
        self.api = IdentityM2mApi(self.session, config)
        self.secret_reader = secret_reader or CognitoSecretReader(self.session)
        self.inference = inference_probe or InferenceProbe(config)
        self.state_store = JsonStore(config.state_path)
        self.state = self.state_store.read()
        self.run_marker = self._load_or_init_run_marker()
        self.names = model.SpikeNames(prefix=config.prefix, run_marker=self.run_marker)
        self.evidence = SpikeEvidence(config.evidence_path, config, self.run_marker)

    # -- run marker + provenance ---------------------------------------
    def _load_or_init_run_marker(self) -> str:
        if self.state:
            return model.assert_state_provenance(
                self.state,
                account_id=self.config.account_id,
                region=self.config.region,
                prefix=self.config.prefix,
                source_revision=self.config.source_revision,
            )
        marker = model.new_run_marker()
        self.state = model.build_state_header(
            run_marker=marker,
            account_id=self.config.account_id,
            region=self.config.region,
            prefix=self.config.prefix,
            source_revision=self.config.source_revision,
        )
        self.state_store.write(self.state)
        return marker

    def save_state(self, **updates: Any) -> None:
        allowed = {"workloadName", "providerName"}
        unknown = set(updates) - allowed
        if unknown:
            raise SpikeError(f"Refusing unknown state fields: {sorted(unknown)}")
        for key, value in updates.items():
            if key == "workloadName" and value != self.names.workload_name:
                raise SpikeError("Refusing a non-owned workload name in state")
            if key == "providerName" and value != self.names.provider_name:
                raise SpikeError("Refusing a non-owned provider name in state")
        self.state.update(updates)
        self.state_store.write(self.state)

    def require_state(self, key: str) -> str:
        value = self.state.get(key)
        if not value:
            raise SpikeError(f"State {key!r} is missing; run deploy first")
        return str(value)

    # -- scope ----------------------------------------------------------
    def set_scope(self, operations: Sequence[str]) -> None:
        unknown = set(operations) - SCOPED_API_CALLS
        if unknown:
            raise SpikeError(f"Unknown scope entries: {sorted(unknown)}")
        self.api.mutation_scope = frozenset(operations)

    # -- identity gate --------------------------------------------------
    def verify_identity(self) -> None:
        identity = self.api.get_caller_identity()
        actual = str(identity.get("Account", ""))
        if actual != self.config.account_id:
            raise SpikeError("STS account does not match --account-id")
        self.evidence.add("identity-verified", accountSuffix=model.account_suffix(actual))

    def verify_inference_stack(self) -> None:
        status, outputs = self.api.describe_inference_stack(self.config.stack_name)
        if status not in SUCCESSFUL_STACK_STATUSES:
            raise SpikeError("Inference stack is not in a successful terminal state")
        missing = sorted(REQUIRED_INFERENCE_OUTPUTS - outputs.keys())
        if missing:
            raise SpikeError(f"Inference stack is missing required outputs: {missing}")
        expected = {
            "CognitoUserPoolId": self.config.user_pool_id,
            "CognitoClientId": self.config.client_id,
            "TokenEndpoint": self.config.token_endpoint,
            "GatewayUrl": self.config.gateway_url,
            "OAuthScope": self.config.resource_scope,
            "InferenceTargetName": self.config.model_id.split("/", 1)[0],
        }
        if any(outputs.get(key) != value for key, value in expected.items()):
            raise SpikeError("Caller inputs do not match the inference stack outputs")
        self.evidence.add(
            "inference-stack-verified",
            stackStatus=status,
            requiredOutputCount=len(REQUIRED_INFERENCE_OUTPUTS),
            targetNameFingerprint=model.fingerprint(expected["InferenceTargetName"]),
        )

    # -- ownership proofs ----------------------------------------------
    def _prove_workload_owned(self, record: Mapping[str, Any]) -> None:
        arn = str(record.get("workloadIdentityArn", ""))
        tags = self.api.list_resource_tags(arn) if arn else {}
        model.assert_workload_owned(
            record,
            tags=tags,
            names=self.names,
            account_id=self.config.account_id,
            region=self.config.region,
        )

    def _prove_provider_owned(self, record: Mapping[str, Any]) -> None:
        arn = str(record.get("credentialProviderArn", ""))
        tags = self.api.list_resource_tags(arn) if arn else {}
        model.assert_provider_owned(
            record,
            tags=tags,
            names=self.names,
            account_id=self.config.account_id,
            region=self.config.region,
        )

    # -- SDK capability preflight --------------------------------------
    def preflight(self) -> None:
        """Prove the pinned SDK models every operation/member this spike needs.

        Read-only: reaches ``sts:GetCallerIdentity`` and
        ``cloudformation:DescribeStacks`` only. Fails closed on a stack-output
        mismatch or any missing SDK operation/member.
        """
        self.set_scope([])
        self.verify_identity()
        self.verify_inference_stack()
        missing: list[str] = []
        for service, ops in (
            (model.CONTROL_SERVICE, model.CONTROL_OPERATIONS),
            (model.DATA_SERVICE, model.DATA_OPERATIONS),
        ):
            for op, required in ops.items():
                for member in required:
                    if not self.api.capability(service, op, member):
                        missing.append(f"{service}:{op}.{member}")
        optional = (
            (model.CONTROL_SERVICE, model.CONTROL_OPTIONAL_MEMBERS),
            (model.DATA_SERVICE, model.DATA_OPTIONAL_MEMBERS),
        )
        for service, table in optional:
            for op, members in table.items():
                for member in members:
                    if not self.api.capability(service, op, member):
                        missing.append(f"{service}:{op}.{member}")
        self.evidence.add(
            "preflight",
            missingCount=len(missing),
            missing=sorted(missing)[:20],
            boto3Version=boto3.__version__,
        )
        if missing:
            raise SpikeError(f"Pinned SDK is missing required members: {sorted(missing)[:5]}")

    # -- bounded polling ------------------------------------------------
    def wait_provider_ready(
        self, name: str, timeout: int = PROVIDER_READY_TIMEOUT_SECONDS
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.api.get_oauth2_credential_provider(name)
            if record is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            status = str(record.get("status", ""))
            model.assert_not_terminal_provider(status)
            if model.classify_provider_status(status) == "ready":
                self.evidence.add("provider-ready", status=status)
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise SpikeError(f"Credential provider did not reach READY within {timeout}s")

    def wait_absent(
        self,
        label: str,
        getter: Callable[[], Mapping[str, Any] | None],
        timeout: int = ABSENT_TIMEOUT_SECONDS,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getter() is None:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise SpikeError(f"{label} was not deleted within {timeout}s")

    # -- discovery / recovery ------------------------------------------
    def discover_owned_workload(self) -> str | None:
        expected = self.names.workload_name
        record = self.api.get_workload_identity(expected)
        if record is not None:
            try:
                self._prove_workload_owned(record)
            except model.OwnershipError as error:
                self.evidence.add(
                    "collision-refused", resource="workload", ownedByRun=False
                )
                raise SpikeError(
                    "An exact-name workload identity exists but is NOT owned by this run"
                ) from error
            return expected
        if any(
            str(summary.get("name", "")) == expected
            for summary in self.api.list_workload_identities()
        ):
            raise SpikeError("Workload list/get inventory is inconsistent")
        return None

    def discover_owned_provider(self) -> str | None:
        expected = self.names.provider_name
        record = self.api.get_oauth2_credential_provider(expected)
        if record is not None:
            try:
                self._prove_provider_owned(record)
            except model.OwnershipError as error:
                self.evidence.add(
                    "collision-refused", resource="provider", ownedByRun=False
                )
                raise SpikeError(
                    "An exact-name credential provider exists but is NOT owned by this run"
                ) from error
            return expected
        if any(
            str(summary.get("name", "")) == expected
            for summary in self.api.list_oauth2_credential_providers()
        ):
            raise SpikeError("Credential provider list/get inventory is inconsistent")
        return None

    # -- deploy ---------------------------------------------------------
    def _ensure_workload(self) -> str:
        existing = self.discover_owned_workload()
        if existing:
            self.evidence.add(
                "workload-recovered", nameFingerprint=model.fingerprint(existing)
            )
        else:
            response, request_fp = self.api.create_workload_identity(
                self.names.workload_name, self.names.allocation_tags()
            )
            created = str(response.get("name", ""))
            if created != self.names.workload_name:
                raise SpikeError("CreateWorkloadIdentity returned an unexpected name")
            self.evidence.add(
                "workload-created",
                requestFingerprint=request_fp,
                nameFingerprint=model.fingerprint(created),
                allocationTagCount=len(self.names.allocation_tags()),
            )
        self.save_state(workloadName=self.names.workload_name)
        record = self.api.get_workload_identity(self.names.workload_name)
        if record is None:
            raise SpikeError("Workload identity disappeared after create")
        self._prove_workload_owned(record)
        return self.names.workload_name

    def _ensure_provider(self) -> str:
        existing = self.discover_owned_provider()
        if existing:
            self.evidence.add(
                "provider-recovered", nameFingerprint=model.fingerprint(existing)
            )
        else:
            # Read the EXISTING app-client secret in-process, build the config,
            # create the provider, and drop the secret reference immediately.
            self.secret_reader.verify_pool_domain(
                user_pool_id=self.config.user_pool_id,
                region=self.config.region,
                token_endpoint=self.config.token_endpoint,
            )
            client_secret = self.secret_reader.read_client_secret(
                user_pool_id=self.config.user_pool_id,
                client_id=self.config.client_id,
                resource_scope=self.config.resource_scope,
            )
            self.evidence.add(
                "cognito-client-verified",
                m2mFlowEnabled=True,
                scopeFingerprint=model.fingerprint(self.config.resource_scope),
            )
            provider_config = model.build_included_provider_config(
                client_id=self.config.client_id,
                client_secret=client_secret,
                issuer=self.config.issuer,
                authorization_endpoint=self.config.authorization_endpoint,
                token_endpoint=self.config.token_endpoint,
            )
            del client_secret
            _, request_fp = self.api.create_oauth2_credential_provider(
                self.names.provider_name, provider_config, self.names.allocation_tags()
            )
            del provider_config
            self.evidence.add(
                "provider-created",
                requestFingerprint=request_fp,
                nameFingerprint=model.fingerprint(self.names.provider_name),
                allocationTagCount=len(self.names.allocation_tags()),
            )
        self.save_state(providerName=self.names.provider_name)
        self.wait_provider_ready(self.names.provider_name)
        record = self.api.get_oauth2_credential_provider(self.names.provider_name)
        if record is None:
            raise SpikeError("Credential provider disappeared after reaching READY")
        self._prove_provider_owned(record)
        return self.names.provider_name

    def deploy(self) -> None:
        if self.state.get("completed") is True:
            raise SpikeError(
                "This run already completed cleanup; use fresh state/evidence files "
                "so AgentCore idempotency is never reused after deletion"
            )
        # Only lifecycle create calls are in scope for deploy.
        self.set_scope(["create_workload_identity", "create_oauth2_credential_provider"])
        self.verify_identity()
        self.verify_inference_stack()
        self._ensure_workload()
        self._ensure_provider()

    # -- verify ---------------------------------------------------------
    def _require_resource_token_denial(
        self,
        *,
        vector: str,
        workload_identity_token: str,
        provider_name: str,
        scopes: Sequence[str],
    ) -> None:
        try:
            unexpected_token = self.api.get_resource_oauth2_token(
                workload_identity_token, provider_name, scopes
            )
        except ClientError as error:
            code = aws_error_code(error)
            status = client_error_http_status(error)
            if not code or status is None or not 400 <= status < 500 or status in {408, 429}:
                raise SpikeError(
                    f"Adversarial {vector} returned a non-denial service failure"
                ) from error
            self.evidence.add(
                "resource-token-denied",
                vector=vector,
                errorCode=code,
                httpStatus=status,
            )
            return
        del unexpected_token
        raise SpikeError(f"Adversarial {vector} unexpectedly returned a resource token")

    def _verify_adversarial_token_denials(
        self, workload_token: str, provider_name: str
    ) -> None:
        invalid_scope = f"aiaf-invalid/{model.fingerprint(self.config.resource_scope)[:12]}"
        self._require_resource_token_denial(
            vector="wrong-scope",
            workload_identity_token=workload_token,
            provider_name=provider_name,
            scopes=[invalid_scope],
        )
        self._require_resource_token_denial(
            vector="wrong-provider",
            workload_identity_token=workload_token,
            provider_name=f"{provider_name}_absent",
            scopes=[self.config.resource_scope],
        )
        self._require_resource_token_denial(
            vector="wrong-workload-token",
            workload_identity_token="invalid-workload-identity-token",
            provider_name=provider_name,
            scopes=[self.config.resource_scope],
        )

    def verify(self) -> None:
        """Mint a workload token, exchange for an M2M resource token, and prove
        the bearer token discovers the model and drives LiteLLMModel.

        Scoped to exactly the two token-minting calls -- no lifecycle write is
        reachable. STS account validation runs first, then ownership proofs.
        """
        self.set_scope(["get_workload_access_token", "get_resource_oauth2_token"])
        self.verify_identity()
        self.verify_inference_stack()

        workload_name = self.require_state("workloadName")
        provider_name = self.require_state("providerName")

        workload = self.api.get_workload_identity(workload_name)
        if workload is None:
            raise SpikeError("Workload identity is absent at verify")
        self._prove_workload_owned(workload)

        provider = self.api.get_oauth2_credential_provider(provider_name)
        if (
            provider is None
            or model.classify_provider_status(str(provider.get("status", ""))) != "ready"
        ):
            raise SpikeError("Credential provider is not READY at verify")
        self._prove_provider_owned(provider)

        # Token values live only in these locals; never persisted/logged.
        workload_token = self.api.get_workload_access_token(workload_name)
        self.evidence.add(
            "workload-token-obtained",
            obtained=model.workload_access_token_ok(workload_token),
            tokenLength=model.safe_token_length(workload_token),
        )
        if not model.workload_access_token_ok(workload_token):
            raise SpikeError("Workload access token is empty or too short")

        try:
            resource_token = self.api.get_resource_oauth2_token(
                workload_token, provider_name, [self.config.resource_scope]
            )
            self.evidence.add(
                "resource-token-obtained",
                obtained=model.resource_token_ok(resource_token),
                tokenLength=model.safe_token_length(resource_token),
                scopeFingerprint=model.fingerprint(self.config.resource_scope),
            )
            if not model.resource_token_ok(resource_token):
                raise SpikeError("M2M resource token is empty or too short")
            self._verify_adversarial_token_denials(workload_token, provider_name)
        finally:
            del workload_token

        try:
            self._verify_inference(resource_token)
        finally:
            del resource_token

    def _verify_inference(self, bearer_token: str) -> None:
        model_ids = self.inference.discover_models(bearer_token)
        found = self.config.model_id in model_ids
        self.evidence.add(
            "model-discovery",
            found=found,
            modelCount=len(model_ids),
            modelFingerprint=model.fingerprint(self.config.model_id),
        )
        if not found:
            raise SpikeError(
                f"Target-qualified model {self.config.model_id!r} absent from discovery"
            )
        for stream in (False, True):
            blocks = self.inference.run_litellm(bearer_token, stream=stream)
            self.evidence.add(
                "litellm-inference",
                stream=stream,
                contentBlocks=blocks,
                passed=blocks > 0,
            )
            if blocks <= 0:
                raise SpikeError(
                    f"LiteLLMModel returned no content (stream={stream})"
                )

    # -- cleanup --------------------------------------------------------
    def _cleanup_provider(self) -> None:
        name = self.discover_owned_provider() or str(self.state.get("providerName") or "")
        if not name:
            return
        record = self.api.get_oauth2_credential_provider(name)
        if record is None:
            return
        self._prove_provider_owned(record)
        self.api.delete_oauth2_credential_provider(name)
        self.wait_absent(
            "provider", lambda: self.api.get_oauth2_credential_provider(name)
        )
        self.evidence.add("provider-deleted", deleted=True)

    def _cleanup_workload(self) -> None:
        name = self.discover_owned_workload() or str(self.state.get("workloadName") or "")
        if not name:
            return
        record = self.api.get_workload_identity(name)
        if record is None:
            return
        self._prove_workload_owned(record)
        self.api.delete_workload_identity(name)
        self.wait_absent("workload", lambda: self.api.get_workload_identity(name))
        self.evidence.add("workload-deleted", deleted=True)

    def cleanup(self) -> None:
        """Sweep provider THEN workload identity, even if one step fails.

        Ordering rationale: the credential provider is the resource that depends
        on / is exercised through the workload identity's token exchange, so it
        is deleted first; the workload identity is deleted second. Each step is
        independent and continues after the other fails. Every delete still
        requires a live ownership proof, so a foreign collision, API error, or
        uncertain inventory is retained as failure evidence and can never be
        converted into a false clean pass.
        """
        self.set_scope(
            ["delete_oauth2_credential_provider", "delete_workload_identity"]
        )
        self.verify_identity()

        failed_steps: list[str] = []
        for label, step in (
            ("provider", self._cleanup_provider),
            ("workload", self._cleanup_workload),
        ):
            try:
                step()
            except Exception as error:  # noqa: BLE001 - continue the owned sweep
                failed_steps.append(f"{label}:{type(error).__name__}")
                self.evidence.add(
                    "cleanup-step-failed",
                    resource=label,
                    errorType=type(error).__name__,
                    errorCode=(
                        aws_error_code(error) if isinstance(error, ClientError) else None
                    ),
                )

        residue = self.residual_inventory()
        self.evidence.add("residual-inventory", **residue)
        if failed_steps or any(residue.values()):
            failed = ",".join(failed_steps) if failed_steps else "none"
            raise SpikeError(
                f"Cleanup incomplete; failed steps={failed}; residual={residue}"
            )

        self.state = {
            **model.build_state_header(
                run_marker=self.run_marker,
                account_id=self.config.account_id,
                region=self.config.region,
                prefix=self.config.prefix,
                source_revision=self.config.source_revision,
            ),
            "completed": True,
        }
        self.state_store.write(self.state)

    def residual_inventory(self) -> dict[str, bool]:
        """Discovery-based residue sweep; any uncertainty counts as present."""
        result: dict[str, bool] = {}
        for label, discover in (
            ("provider", self.discover_owned_provider),
            ("workload", self.discover_owned_workload),
        ):
            try:
                result[label] = discover() is not None
            except Exception:  # noqa: BLE001 - never convert uncertainty to zero
                result[label] = True
        return result

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

COMMANDS = ("preflight", "deploy", "verify", "cleanup", "run-all")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--prefix", default="aiaf-idm2m-spike")
    parser.add_argument(
        "--source-revision",
        required=True,
        help="Exact 40-character Git revision whose spike code is running",
    )
    parser.add_argument(
        "--stack-name",
        default="Prod-InferenceGateway",
        help="Existing inference Gateway stack used as the output source of truth",
    )
    parser.add_argument("--user-pool-id", required=True,
                        help="EXISTING Cognito user pool id holding the M2M app client")
    parser.add_argument("--client-id", required=True,
                        help="EXISTING Cognito app-client id (M2M, has a client secret)")
    parser.add_argument("--issuer", required=True, help="OAuth2 issuer URL")
    parser.add_argument("--authorization-endpoint", required=True)
    parser.add_argument("--token-endpoint", required=True)
    parser.add_argument("--resource-scope", required=True,
                        help="Exact M2M scope to request, e.g. 'resource-server/scope'")
    parser.add_argument("--gateway-url", required=True,
                        help="EXISTING inference Gateway base URL (no trailing /inference/v1)")
    parser.add_argument("--model-id", required=True,
                        help="Target-qualified model id expected in discovery")
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    return parser.parse_args(argv)


def run_command(spike: IdentityM2mSpike, command: str) -> str:
    if command == "preflight":
        spike.preflight()
        return "preflight-passed"
    if command == "deploy":
        spike.deploy()
        return "deploy-passed"
    if command == "verify":
        spike.verify()
        return "verify-passed"
    if command == "cleanup":
        spike.cleanup()
        return "cleanup-passed"
    # run-all: cleanup always runs in finally so a failure still tears down.
    primary: Exception | None = None
    try:
        spike.preflight()
        spike.deploy()
        spike.verify()
    except Exception as error:  # noqa: BLE001 - cleanup must still run
        primary = error
    finally:
        try:
            spike.cleanup()
        except Exception as cleanup_error:  # noqa: BLE001
            if primary is not None:
                raise SpikeError(
                    f"Run failed: {primary}; cleanup also failed: {cleanup_error}"
                ) from cleanup_error
            raise
    if primary is not None:
        raise primary
    return "passed"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except Exception as error:  # noqa: BLE001 - no evidence file exists yet
        message = model.sanitize_error(str(error), prefix=args.command)
        print(f"FAIL: {message}", file=sys.stderr)
        return 1

    spike: IdentityM2mSpike | None = None
    try:
        spike = IdentityM2mSpike(config)
        status = run_command(spike, args.command)
        spike.evidence.finish(status)
        print(f"Evidence: {config.evidence_path}")
        return 0
    except Exception as error:  # noqa: BLE001 - single fail-closed exit path
        message = model.sanitize_error(str(error), prefix=args.command)
        if spike is not None:
            spike.evidence.add(
                "failure",
                errorType=type(error).__name__,
                errorCode=(
                    aws_error_code(error) if isinstance(error, ClientError) else None
                ),
                message=message,
            )
            spike.evidence.finish("failed")
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    finally:
        if spike is not None:
            spike.close()


if __name__ == "__main__":
    raise SystemExit(main())
