"""Tenant-bound model-provider API-key storage.

The browser sends a key once over the authenticated control plane. This router
stores it under ``agentcore-provider/`` and returns only its ARN. Deployment
later live-validates the source tags and copies the raw value into an exact
deployment-bound target-account secret; neither Step Functions history nor an
AgentCore runtime environment ever contains the plaintext key.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes
from app.services.resource_ownership import (
    owner_sub_hash,
    owner_tag_list,
)

logger = logging.getLogger(__name__)

CredentialedProvider = Literal[
    "openai",
    "anthropic",
    "gemini",
    "litellm",
    "mistral",
    "writer",
    "llamaapi",
    "deepseek",
    "groq",
    "together",
]

router = APIRouter(prefix="/api/provider-credentials", tags=["provider-credentials"])


class StoreProviderCredentialRequest(BaseModel):
    provider: CredentialedProvider
    api_key: SecretStr = Field(min_length=1, max_length=8192)

    @field_validator("api_key")
    @classmethod
    def _reject_blank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value


class StoreProviderCredentialResponse(BaseModel):
    secret_arn: str


@router.post(
    "",
    response_model=StoreProviderCredentialResponse,
    dependencies=[Depends(require_scopes("agent:write"))],
)
def store_provider_credential(
    body: StoreProviderCredentialRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> StoreProviderCredentialResponse:
    """Store one API key and return only its tenant-bound source ARN."""
    region = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    caller_hash = owner_sub_hash(caller_sub)
    secret_name = f"agentcore-provider/{body.provider}/{caller_hash}-{uuid.uuid4().hex[:12]}"

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        response = sm.create_secret(
            Name=secret_name,
            SecretString=body.api_key.get_secret_value(),
            Description=f"Model-provider API key for {body.provider} (agentcore-flows)",
            Tags=owner_tag_list(
                region,
                extra={
                    "Purpose": "model-provider-api-key",
                    "Provider": body.provider,
                    "OwnerSubHash": caller_hash,
                },
            ),
        )
    except (BotoCoreError, ClientError) as exc:
        # The request model uses SecretStr and this message is constant: neither
        # the key nor its generated secret name reaches logs or the response.
        logger.exception("Failed to store a model-provider credential")
        raise HTTPException(
            status_code=500,
            detail="Could not store the model-provider credential",
        ) from exc

    return StoreProviderCredentialResponse(secret_arn=response["ARN"])
