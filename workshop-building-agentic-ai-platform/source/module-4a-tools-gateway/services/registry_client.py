# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HTTP client for the Module 3 MCP Gateway & Registry REST API.

Supports two authentication modes (checked in order):
  1. Static API token — ``api_token`` key from ``workshop-registry-api-token``
     (Module 3 data-stack auto-generates this). Works for reads AND writes.
  2. Cognito M2M — ``client_credentials`` grant via the Cognito token
     endpoint. Used when the secret holds ``client_id``/``client_secret``.

Uses only stdlib (urllib) so no Lambda layer is needed.
"""

import base64
import json
import logging
import os
import re
import ssl
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import boto3

logger = logging.getLogger(__name__)

_TOKEN_BUFFER_SECONDS = 60
_TIMEOUT = 30


def _http(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, dict]:
    """Minimal HTTP helper using urllib."""
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS URL: {url!r}")
    req = Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    # nosec B310 — scheme validated to https above; URL comes from workshop CFN exports.
    with urlopen(req, timeout=_TIMEOUT, context=ctx) as resp:  # nosec B310
        body = json.loads(resp.read())
        return resp.status, body


def _build_cognito_token_url(creds: dict, region: str) -> str:
    """Build the Cognito token endpoint URL from credentials.

    Handles multiple formats from Siva's CFN/Lambda:
    - ``token_url`` key with full URL (preferred)
    - ``cognito_domain`` key with full URL (https://xxx.auth.region.amazoncognito.com)
    - ``cognito_domain`` key with prefix only (workshop-mcp-registry)
    """
    # Prefer explicit token_url if present and valid
    token_url = creds.get("token_url", "")
    if token_url and ".amazoncognito.com" in token_url:
        return token_url

    cognito_domain = creds.get("cognito_domain", "")
    if not cognito_domain:
        raise ValueError("No token_url or cognito_domain in credentials")

    # Already a full URL
    if ".amazoncognito.com" in cognito_domain:
        if not cognito_domain.startswith("https://"):
            cognito_domain = f"https://{cognito_domain}"
        return f"{cognito_domain}/oauth2/token"

    # Prefix only — construct full URL
    prefix = cognito_domain.replace("https://", "").replace("http://", "")
    return f"https://{prefix}.auth.{region}.amazoncognito.com/oauth2/token"


class RegistryClient:
    """Authenticated client for the MCP Gateway & Registry API."""

    def __init__(self, registry_url: str, m2m_secret_name: str, region: str = "us-west-2"):
        self.registry_url = registry_url.rstrip("/")
        self._m2m_secret_name = m2m_secret_name
        self._region = region
        self._token: str | None = None
        self._token_expiry: float = 0
        self._creds: dict | None = None

    def _load_credentials(self) -> dict:
        """Load credentials from Secrets Manager (cached)."""
        if self._creds is None:
            sm = boto3.client("secretsmanager", region_name=self._region)
            secret = sm.get_secret_value(SecretId=self._m2m_secret_name)
            self._creds = json.loads(secret["SecretString"])
        return self._creds

    def _get_token(self) -> str:
        """Get a valid auth token, refreshing if needed."""
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token

        creds = self._load_credentials()

        # Static API token (from Module 3 RegistryApiTokenSecret)
        if creds.get("api_token"):
            self._token = creds["api_token"]
            self._token_expiry = now + 86400
            return self._token

        # Cognito M2M client_credentials flow
        token_url = _build_cognito_token_url(creds, self._region)

        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
        scopes = creds.get("scopes", "")

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = f"grant_type=client_credentials&scope={quote(scopes)}".encode()

        req = Request(token_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Authorization", f"Basic {basic}")

        if not token_url.startswith("https://"):
            raise ValueError(f"Refusing non-HTTPS token URL: {token_url!r}")
        ctx = ssl.create_default_context()
        # nosec B310 — scheme validated to https above; token_url is the Cognito endpoint.
        with urlopen(req, timeout=10, context=ctx) as resp:  # nosec B310
            data = json.loads(resp.read())

        self._token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expiry = now + expires_in - _TOKEN_BUFFER_SECONDS
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def list_servers(self) -> list[dict]:
        """List all registered MCP servers from the Registry."""
        _, data = _http("GET", f"{self.registry_url}/api/servers", self._headers())
        if isinstance(data, list):
            return data
        return data.get("servers", data.get("items", []))

    def register_server(self, server_data: dict) -> dict:
        """Register a new MCP server/tool in the Registry."""
        headers = {**self._headers(), "Content-Type": "application/json"}
        _, data = _http(
            "POST",
            f"{self.registry_url}/api/register",
            headers,
            json.dumps(server_data).encode(),
        )
        return data

    def search_servers(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search for servers in the Registry."""
        headers = {**self._headers(), "Content-Type": "application/json"}
        body = json.dumps({"query": query, "limit": limit}).encode()
        _, data = _http("POST", f"{self.registry_url}/api/search/semantic", headers, body)
        if isinstance(data, list):
            return data
        return data.get("servers", data.get("results", []))

    def get_server(self, server_name: str) -> dict | None:
        """Get a specific server by name."""
        try:
            _, data = _http(
                "GET",
                f"{self.registry_url}/api/server_details/{server_name}",
                self._headers(),
            )
            return data
        except Exception:
            return None
