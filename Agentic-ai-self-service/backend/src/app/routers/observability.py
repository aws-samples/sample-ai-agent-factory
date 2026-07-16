"""Observability credential storage endpoint.

Stores OTLP backend auth headers in AWS Secrets Manager so they never travel
in plaintext as runtime environment variables. The agent codegen prologue
resolves the ARN at module load via boto3 and injects the value into
``OTEL_EXPORTER_OTLP_HEADERS`` before Strands' OTLPSpanExporter is built.

Provider presets:
- langfuse: takes public_key + secret_key, computes ``Authorization=Basic <b64>``
- custom: takes raw header_value (already in ``HeaderName=Value`` form)
"""

import base64
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)


# Critic Finding 1: the secret name itself encodes the owning tenant so the
# returned ARN is self-describing. Keep this restrictive so the suffix can't
# collide with the agentcore-otel/ regex namespace check in
# services.observability._validate_user_otel_secret_arn.
_OWNER_SUB_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_owner_sub(owner_sub: str) -> str:
    """Sanitize a Cognito sub for inclusion in a Secrets Manager name.

    Secrets Manager names allow ``[A-Za-z0-9/_+=.@-]`` but our namespace
    convention reserves ``/`` as the level separator and ``+=.@`` are
    awkward in IAM resource ARNs, so we collapse anything outside
    ``[A-Za-z0-9_-]`` to ``-``. Cognito subs are UUIDs in practice so this
    is a no-op for the production case; the local-dev sub ``local-dev``
    survives unchanged.
    """
    return _OWNER_SUB_SAFE_RE.sub("-", owner_sub)[:64] or "anon"

router = APIRouter()


class StoreCredentialsRequest(BaseModel):
    """Request body for POST /api/observability/credentials."""

    provider: str = Field(min_length=1, max_length=64)
    public_key: Optional[str] = Field(default=None, max_length=512)
    secret_key: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=512)
    header_value: Optional[str] = Field(default=None, max_length=4096)


class StoreCredentialsResponse(BaseModel):
    """Response: ARN for the agent runtime to consume."""

    secret_arn: str


def _build_header(req: StoreCredentialsRequest) -> str:
    """Translate provider-specific keys into an OTLP-compatible header string.

    Output format: ``Header-Name=Value`` (comma-separated for multiple).
    """
    if req.provider == "langfuse":
        if not req.public_key or not req.secret_key:
            raise HTTPException(
                status_code=400,
                detail="Langfuse requires public_key and secret_key.",
            )
        token = base64.b64encode(f"{req.public_key}:{req.secret_key}".encode()).decode()
        return f"Authorization=Basic {token}"
    if req.provider == "custom":
        if not req.header_value:
            raise HTTPException(
                status_code=400, detail="Custom provider requires header_value."
            )
        return req.header_value
    raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")


class PlatformDefaultsResponse(BaseModel):
    """Public-safe view of platform OTEL defaults.

    SECURITY: Never include the secret ARN here. The frontend only needs to know
    whether the feature is on, the endpoint URL (for display in the modal),
    and the sample rate so the slider can show a read-only value.
    """

    enabled: bool = False
    endpoint: Optional[str] = None
    sample_rate: Optional[float] = None
    service_name_prefix: Optional[str] = None


@router.get("/observability/platform-defaults", response_model=PlatformDefaultsResponse, dependencies=[Depends(require_scopes("observability:read"))])
def get_platform_defaults() -> PlatformDefaultsResponse:
    """Return platform-level OTEL defaults so the UI can show them as locked.

    When the platform admin has configured OTEL via deploy.sh, every agent
    inherits those values and per-canvas overrides for endpoint/secret/sample
    are dropped at deploy time. This endpoint lets the modal render the
    relevant fields as read-only.

    Never returns the auth secret ARN (privileged).
    """
    from app.services.observability import get_platform_observability_defaults

    defaults = get_platform_observability_defaults()
    if not defaults:
        return PlatformDefaultsResponse(enabled=False)
    return PlatformDefaultsResponse(
        enabled=True,
        endpoint=defaults.get("otlp_endpoint"),
        sample_rate=defaults.get("sample_rate"),
        service_name_prefix=defaults.get("service_name_prefix"),
    )


@router.post("/observability/credentials", response_model=StoreCredentialsResponse, dependencies=[Depends(require_scopes("observability:write"))])
def store_credentials(
    request: StoreCredentialsRequest,
    raw_request: Request,
) -> StoreCredentialsResponse:
    """Store OTLP auth header in Secrets Manager and return the ARN.

    The secret value is the raw ``Header=Value`` string that the agent runtime
    bootstrap will set as ``OTEL_EXPORTER_OTLP_HEADERS``.

    Critic Finding 1: the secret name encodes both the agentcore-otel/
    namespace AND the owning Cognito sub so the returned ARN is
    self-describing. The runtime IAM policy only grants ARNs whose pattern
    matches ``agentcore-otel/*``, and the per-canvas observability path
    additionally validates the prefix in
    ``services.observability._validate_user_otel_secret_arn``. Tags
    (``owner_sub``, ``created_at``, ``Purpose``) make the secret auditable
    and let the cleanup sweepers correlate ownership.
    """
    header_value = _build_header(request)
    region = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    owner_sub = get_caller_sub(raw_request)
    safe_owner = _safe_owner_sub(owner_sub)
    secret_name = (
        f"agentcore-otel/{request.provider}/{safe_owner}-{uuid.uuid4().hex[:12]}"
    )
    created_at_iso = datetime.now(timezone.utc).isoformat()

    sm = boto3.client("secretsmanager", region_name=region)
    try:
        resp = sm.create_secret(
            Name=secret_name,
            SecretString=header_value,
            Description=f"OTLP auth header for {request.provider} (agentcore-flows)",
            Tags=[
                {"Key": "ManagedBy", "Value": "agentcore-flows"},
                {"Key": "Purpose", "Value": "user-otel-auth"},
                {"Key": "Provider", "Value": request.provider},
                {"Key": "owner_sub", "Value": owner_sub},
                {"Key": "created_at", "Value": created_at_iso},
            ],
        )
    except ClientError as e:
        logger.exception("Failed to store OTEL credentials in Secrets Manager")
        raise HTTPException(
            status_code=500,
            detail=f"Could not store credentials: {e.response.get('Error', {}).get('Message', str(e))}",
        ) from e

    return StoreCredentialsResponse(secret_arn=resp["ARN"])
