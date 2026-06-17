"""GitOps service for Gap 3D — git-backed agent (workflow) definitions.

Pulls a workflow JSON spec from a GitHub/GitLab repo and validates it against
``WorkflowDefinition`` so a CI/CD pipeline (or the /api/workflows/{id}/git-sync
endpoint) can keep a canvas in sync with a versioned definition in source
control. The git PAT is resolved from an owner-scoped Secrets Manager secret —
never from env vars or DDB plaintext (NON-NEGOTIABLE RULE 5).

SSRF posture (NON-NEGOTIABLE RULE 4): every operator-/user-supplied URL fetched
server-side is validated by :func:`validate_git_source`, which MIRRORS
``gateway_deployer._validate_discovery_url``:

  * require the ``https`` scheme,
  * resolve the host via ``socket.getaddrinfo`` under a strict 5s timeout,
  * reject any resolved IP in the shared ``_DISALLOWED_NETWORKS`` denylist
    (IMDS/RFC1918/loopback/link-local/CGNAT/multicast/reserved + IPv6
    ULA/link-local), and
  * enforce a git-host allowlist (default github.com / api.github.com /
    raw.githubusercontent.com / gitlab.com, overridable via the
    ``GIT_SYNC_HOST_ALLOWLIST`` env var).

Residual DNS-rebinding race: ``validate_git_source`` resolves + checks the host,
but ``urllib.request.urlopen`` re-resolves later, so a private IP could slip in
between (TOCTOU). We mitigate exactly as ``gateway_deployer`` does — a strict
``urlopen`` timeout plus a ``nosemgrep`` justification — and accept the residual
race (pinning the resolved IP with SNI/Host overrides is out of scope, matching
the existing pattern).

No AWS calls at import time; the region is read lazily from
``APP_AWS_REGION``/``AWS_REGION``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3

# Reuse the EXACT denylist + DNS-resolution strategy from the gateway deployer
# SSRF guard so the two guards can never drift (NON-NEGOTIABLE RULE 4).
from app.services.gateway_deployer import _DISALLOWED_NETWORKS

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to auto-follow 3xx. A redirect target host would NOT be re-run
    through the SSRF guard, so following one (with the bearer token attached)
    could pivot the request to a private/unvalidated host. Treat any redirect
    as a hard failure instead (SSRF defence-in-depth)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _GitSourceBlocked(
            f"git host returned an unexpected redirect ({code}) to '{newurl}'"
        )


# Built once: a urllib opener that never follows redirects. Used for the
# server-side git spec fetch.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


# ---------------------------------------------------------------------------
# SSRF guard exception classes — same shape as the gateway deployer's
# _DiscoveryUrlInvalid / _DiscoveryUrlBlocked so callers can keep a broad
# ``except ValueError`` while discriminating structural vs. network failures.
# ---------------------------------------------------------------------------


class _GitSourceInvalid(ValueError):
    """The supplied git_source is structurally invalid (bad scheme/path/branch)."""


class _GitSourceBlocked(ValueError):
    """The supplied repo URL resolves to a disallowed network or non-allowlisted host."""


# Default git hosts we are willing to fetch from. Overridable via env so an
# operator can add a self-hosted GitLab. github.com appears as both the web host
# (in the repo URL the user pastes) and api.github.com / raw.githubusercontent.com
# (the hosts we actually fetch from).
_DEFAULT_GIT_HOSTS = (
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
)

# Branch refs: git allows a fairly wide character set, but we keep this tight to
# avoid any chance of URL/query injection into the contents-API ?ref= param. No
# '..' (path traversal in the ref), no leading dash (option injection).
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

# GitHub owner/repo segments.
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Token-ref ARNs MUST live in the owner-scoped agentcore-git/ namespace — mirrors
# observability._validate_user_otel_secret_arn (agentcore-otel/).
_GIT_SECRET_ARN_RE = re.compile(
    r"^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:agentcore-git/[A-Za-z0-9_/-]+"
)

# Cap the fetched body so a malicious/compromised repo can't OOM the Lambda.
_MAX_SPEC_BYTES = 1 * 1024 * 1024  # 1 MiB

# Restrict a Cognito sub to a safe Secrets Manager name fragment (mirror
# routers/observability._safe_owner_sub).
_OWNER_SUB_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _region() -> str:
    return os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def _safe_owner_sub(owner_sub: str) -> str:
    """Sanitize a Cognito sub for inclusion in a Secrets Manager name."""
    return _OWNER_SUB_SAFE_RE.sub("-", owner_sub)[:64] or "anon"


def _load_git_host_allowlist() -> tuple[str, ...]:
    """Return the git-host allowlist (lowercased), env override or the default set.

    Env var: ``GIT_SYNC_HOST_ALLOWLIST=github.com,api.github.com,git.acme.io``.
    Unlike the OIDC allowlist (which is opt-in and may be unset), git ALWAYS has
    an allowlist — a blank/absent env var falls back to the safe default hosts.
    """
    raw = os.environ.get("GIT_SYNC_HOST_ALLOWLIST", "").strip()
    if not raw:
        return _DEFAULT_GIT_HOSTS
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or _DEFAULT_GIT_HOSTS


def _assert_host_resolves_to_public_ip(host: str) -> None:
    """Resolve ``host`` and raise ``_GitSourceBlocked`` if any IP is disallowed.

    MIRRORS gateway_deployer._validate_discovery_url's resolution loop exactly:
    strict 5s timeout, reject on resolution failure (never silently fall
    through), and check every returned A/AAAA record against the shared denylist.
    """
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (socket.gaierror, socket.timeout, OSError) as e:
            raise _GitSourceBlocked(
                f"git host '{host}' could not be resolved: {e}"
            ) from e
    finally:
        socket.setdefaulttimeout(prev_timeout)

    if not infos:
        raise _GitSourceBlocked(f"git host '{host}' returned no DNS records")

    import ipaddress

    for info in infos:
        ip_str = info[4][0]
        # IPv6 sockaddr can carry a scope id like "fe80::1%eth0" — strip it.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise _GitSourceBlocked(
                f"git host resolved to unparseable IP '{ip_str}': {e}"
            ) from e
        for net in _DISALLOWED_NETWORKS:
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                raise _GitSourceBlocked(
                    f"git host resolves to disallowed IP ({ip_str} in {net})"
                )


def _validate_fetch_url(url: str) -> str:
    """Validate a URL we are about to ``urlopen`` server-side (SSRF guard).

    Require https, host on the allowlist, and every resolved IP outside the
    denylist. Returns the URL unchanged on success. Used for BOTH the
    user-supplied repo URL and the derived contents-API URL — re-running on the
    API host (api.github.com) ensures the host we actually fetch from is also
    guarded, not just the one the user pasted.
    """
    if not url or not isinstance(url, str):
        raise _GitSourceInvalid("repo_url is empty")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise _GitSourceInvalid(
            f"git repo_url must use https scheme (got '{parsed.scheme}')"
        )
    host = parsed.hostname
    if not host:
        raise _GitSourceInvalid("git repo_url has no host component")

    allowlist = _load_git_host_allowlist()
    if host.lower() not in allowlist:
        raise _GitSourceBlocked(
            f"git host '{host}' is not on the git-host allowlist "
            f"(GIT_SYNC_HOST_ALLOWLIST); allowed: {', '.join(allowlist)}"
        )

    _assert_host_resolves_to_public_ip(host)
    return url


def _validate_branch(branch: str) -> str:
    if not branch or not isinstance(branch, str):
        raise _GitSourceInvalid("git branch is empty")
    if branch.startswith("-"):
        raise _GitSourceInvalid("git branch must not start with '-'")
    if ".." in branch:
        raise _GitSourceInvalid("git branch must not contain '..'")
    if not _BRANCH_RE.match(branch):
        raise _GitSourceInvalid(f"git branch '{branch}' has invalid characters")
    return branch


def _validate_path(path: str) -> str:
    if not path or not isinstance(path, str):
        raise _GitSourceInvalid("git path is empty")
    if len(path) > 512:
        raise _GitSourceInvalid("git path exceeds 512 characters")
    if path.startswith("/"):
        raise _GitSourceInvalid("git path must be repo-relative (no leading '/')")
    if ".." in path:
        raise _GitSourceInvalid("git path must not contain '..'")
    if not path.lower().endswith(".json"):
        raise _GitSourceInvalid("git path must point at a .json file")
    return path


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a github.com repo URL.

    Accepts ``https://github.com/owner/repo`` and ``.../owner/repo.git``. Raises
    ``_GitSourceInvalid`` for anything that doesn't look like an owner/repo pair.
    """
    parsed = urllib.parse.urlparse(repo_url)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        raise _GitSourceInvalid(
            "git repo_url must be of the form https://github.com/<owner>/<repo>"
        )
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_SEGMENT_RE.match(owner) or not _REPO_SEGMENT_RE.match(repo):
        raise _GitSourceInvalid("git repo_url owner/repo segments are invalid")
    return owner, repo


def validate_git_source(repo_url: str, branch: str, path: str) -> dict:
    """Validate + normalize a git_source. Raises a ``ValueError`` subclass on failure.

    Returns a normalized dict ``{repo_url, branch, path}`` (token_ref is added
    separately by the router). SSRF-guards ``repo_url`` (scheme/host/DNS/IP) and
    structurally validates ``branch`` and ``path`` so a malformed ``git_source``
    persisted directly via PUT can never reach a fetch unvalidated.
    """
    repo_url = _validate_fetch_url(repo_url)
    branch = _validate_branch(branch)
    path = _validate_path(path)
    # Parse early so an unparseable repo URL fails here rather than at fetch.
    _parse_github_repo(repo_url)
    return {"repo_url": repo_url, "branch": branch, "path": path}


def store_git_token(owner_sub: str, token: str) -> str:
    """Store a git PAT in Secrets Manager under the owner-scoped agentcore-git/ namespace.

    Mirrors ``routers/observability.store_credentials``: the secret name encodes
    the owning Cognito sub so the returned ARN is self-describing, the runtime
    IAM policy only grants ``agentcore-git/*`` ARNs, and ``_resolve_token``
    re-validates the prefix before any GetSecretValue. Returns the secret ARN.
    """
    if not token or not isinstance(token, str):
        raise _GitSourceInvalid("git token is empty")
    safe_owner = _safe_owner_sub(owner_sub)
    secret_name = f"agentcore-git/{safe_owner}-{uuid.uuid4().hex[:12]}"
    created_at_iso = datetime.now(timezone.utc).isoformat()

    sm = boto3.client("secretsmanager", region_name=_region())
    resp = sm.create_secret(
        Name=secret_name,
        SecretString=token,
        Description="Git PAT for GitOps workflow sync (agentcore-flows)",
        Tags=[
            {"Key": "ManagedBy", "Value": "agentcore-flows"},
            {"Key": "Purpose", "Value": "git-sync-token"},
            {"Key": "owner_sub", "Value": owner_sub},
            {"Key": "created_at", "Value": created_at_iso},
        ],
    )
    return resp["ARN"]


def _validate_token_ref(token_ref: str) -> str:
    """Reject a token ARN outside the owner-scoped agentcore-git/ namespace.

    MIRRORS observability._validate_user_otel_secret_arn — a user-supplied ARN
    must match the agentcore-git/ prefix or we never call GetSecretValue (so a
    malicious git_source can't exfiltrate an unrelated secret).
    """
    if not isinstance(token_ref, str) or not _GIT_SECRET_ARN_RE.match(token_ref):
        raise _GitSourceInvalid(
            "git token_ref must be a Secrets Manager ARN in the agentcore-git/ namespace"
        )
    return token_ref


def _resolve_token(token_ref: Optional[str]) -> Optional[str]:
    """Resolve a git PAT from its Secrets Manager ARN. Returns None when unset.

    Validates the ARN namespace BEFORE any GetSecretValue call so we never read
    a secret outside agentcore-git/.
    """
    if not token_ref:
        return None
    _validate_token_ref(token_ref)
    sm = boto3.client("secretsmanager", region_name=_region())
    resp = sm.get_secret_value(SecretId=token_ref)
    return resp.get("SecretString")


def _read_capped(response, limit: int = _MAX_SPEC_BYTES) -> bytes:
    """Read at most ``limit`` bytes; raise if the body exceeds the cap.

    We read ``limit + 1`` and reject when we get more than ``limit`` bytes so a
    compromised repo can't stream an unbounded body into Lambda memory.
    """
    data = response.read(limit + 1)
    if len(data) > limit:
        raise _GitSourceInvalid(
            f"git spec exceeds the {limit}-byte size cap"
        )
    return data


def fetch_workflow_spec(git_source: dict, token_ref: Optional[str]) -> dict:
    """Fetch + validate the workflow JSON spec referenced by ``git_source``.

    Re-validates ``git_source`` (never trust the stored dict shape — see RULE 1
    Bug-122 class), builds the GitHub contents API URL, SSRF-guards the API host,
    fetches the raw JSON under a strict 10s timeout with a body-size cap, and
    validates the result against ``WorkflowDefinition``. Returns the parsed dict
    on success; raises a ``ValueError`` subclass on any structural/SSRF/parse
    failure.

    GitHub note: requesting ``Accept: application/vnd.github.raw`` returns the
    file bytes directly. If a future GitHub change drops raw support it will
    return the default JSON envelope with a base64 ``content`` field — we detect
    that envelope and base64-decode as a fallback.
    """
    from app.models import WorkflowDefinition

    norm = validate_git_source(
        git_source.get("repo_url", ""),
        git_source.get("branch", ""),
        git_source.get("path", ""),
    )
    owner, repo = _parse_github_repo(norm["repo_url"])
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{urllib.parse.quote(norm['path'])}"
        f"?ref={urllib.parse.quote(norm['branch'], safe='')}"
    )
    # Guard the host we ACTUALLY fetch from (api.github.com), not just the repo
    # URL the user pasted. api.github.com must be on the allowlist for this to
    # pass — it is in the default set.
    _validate_fetch_url(f"https://api.github.com/repos/{owner}/{repo}")

    token = _resolve_token(token_ref)
    headers = {
        "Accept": "application/vnd.github.raw",
        "User-Agent": "agentcore-flows-gitops",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(api_url, headers=headers, method="GET")
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        # The URL host is SSRF-validated above (https scheme, allowlisted host,
        # every resolved IP outside the denylist) under a strict timeout. A
        # residual DNS-rebind TOCTOU race remains (urlopen re-resolves) and is
        # mitigated by the timeout, matching gateway_deployer's documented stance.
        # SSRF defence-in-depth: a no-redirect opener so a 3xx from the
        # allowlisted host cannot bounce the request (with the bearer token
        # attached) to an unvalidated/private host — urllib would otherwise
        # auto-follow without re-running the SSRF guard.
        with _NO_REDIRECT_OPENER.open(req, timeout=10) as response:  # noqa: S310
            raw = _read_capped(response)
    except _GitSourceInvalid:
        raise
    except Exception as e:  # network/HTTP errors
        raise _GitSourceBlocked(f"failed to fetch git spec: {e}") from e

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _GitSourceInvalid(f"git spec is not valid JSON: {e}") from e

    # Fallback: GitHub default envelope (base64 content) if raw media not honored.
    if (
        isinstance(parsed, dict)
        and parsed.get("encoding") == "base64"
        and isinstance(parsed.get("content"), str)
    ):
        import base64

        try:
            decoded = base64.b64decode(parsed["content"])
            parsed = json.loads(decoded.decode("utf-8"))
        except Exception as e:
            raise _GitSourceInvalid(
                f"git spec base64 envelope could not be decoded: {e}"
            ) from e

    if not isinstance(parsed, dict):
        raise _GitSourceInvalid("git spec must be a JSON object")

    try:
        WorkflowDefinition.model_validate(parsed)
    except Exception as e:
        raise _GitSourceInvalid(
            f"git spec does not match the workflow schema: {e}"
        ) from e

    return parsed
