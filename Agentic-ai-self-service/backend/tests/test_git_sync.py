"""Unit tests for Gap 3D GitOps — services/git_sync + routers/git_sync.

Standalone: no real AWS, no Cognito. Secrets Manager calls run under moto;
``socket.getaddrinfo`` and ``urllib.request.urlopen`` are patched for the SSRF
and fetch paths (mirroring tests/test_gateway_deployer_ssrf.py).

The endpoint tests build a FastAPI app mounting only the git_sync router over a
real in-memory ``WorkflowStorage`` with ``app.dependency_overrides[get_caller_sub]``
to simulate distinct tenants. ``model_copy(update={"git_source": ...})`` carries
the value forward even before the WorkflowDefinition.git_source shared edit lands
(same contract routers/workspaces.py relies on for ``acl``), so these tests pass
pre-edit.
"""

from __future__ import annotations

import io
import json
import socket
import sys
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import patch

import pytest

sys.path.insert(0, "src")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.models import WorkflowDefinition, WorkflowMetadata, Viewport  # noqa: E402
from app.services import git_sync  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.git_sync import (  # noqa: E402
    _GitSourceBlocked,
    _GitSourceInvalid,
    fetch_workflow_spec,
    store_git_token,
    validate_git_source,
)

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402
import boto3  # noqa: E402


REGION = "us-east-1"
ALICE = "alice-sub"
BOB = "bob-sub"


@pytest.fixture(autouse=True)
def _pin_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the AWS region so the service's lazy region resolution matches the
    boto3 clients the tests build, regardless of the host's AWS_REGION env."""
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _addrinfo_for(ip: str, family: int = socket.AF_INET):
    if family == socket.AF_INET:
        sockaddr = (ip, 443)
    else:
        sockaddr = (ip, 443, 0, 0)
    return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]


def _valid_spec(wf_id: str = "wf1", owner: str = ALICE) -> dict:
    """A WorkflowDefinition-valid dict suitable as a git spec."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": wf_id,
        "name": "Synced Agent",
        "description": "from git",
        "version": "2.0.0",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "metadata": {"author": "ci", "aws_region": "us-east-1", "tags": []},
        "created_at": now,
        "updated_at": now,
        "owner_sub": owner,
    }


class _FakeResponse:
    """Minimal urlopen() context-manager stand-in exposing .read(n)."""

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


# ===========================================================================
# SSRF guard — validate_git_source
# ===========================================================================


@pytest.mark.parametrize("scheme", ["http", "file", "gopher", "ftp"])
def test_non_https_scheme_rejected_before_dns(scheme: str) -> None:
    with patch("socket.getaddrinfo") as mock_gai:
        with pytest.raises(_GitSourceInvalid):
            validate_git_source(f"{scheme}://github.com/o/r", "main", "agent.json")
        mock_gai.assert_not_called()


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "169.254.169.254",  # IMDS
        "169.254.170.2",    # Lambda creds
        "127.0.0.1",        # loopback
        "10.0.0.1",         # RFC1918
        "172.16.5.5",       # RFC1918
        "192.168.1.1",      # RFC1918
        "100.64.0.1",       # CGNAT
        "0.0.0.0",          # this network
    ],
)
def test_repo_host_resolving_to_private_ipv4_rejected(blocked_ip: str) -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for(blocked_ip)):
        with pytest.raises(_GitSourceBlocked):
            validate_git_source("https://github.com/o/r", "main", "agent.json")


@pytest.mark.parametrize("blocked_ipv6", ["::1", "fe80::1", "fc00::1"])
def test_repo_host_resolving_to_private_ipv6_rejected(blocked_ipv6: str) -> None:
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for(blocked_ipv6, family=socket.AF_INET6),
    ):
        with pytest.raises(_GitSourceBlocked):
            validate_git_source("https://github.com/o/r", "main", "agent.json")


def test_literal_private_ip_host_rejected() -> None:
    # Even though 10.0.0.1 is not on the allowlist, this also exercises the IP
    # denylist path for hosts that ARE allowlisted but rebind to a private IP.
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("10.0.0.1")):
        with pytest.raises((_GitSourceBlocked, _GitSourceInvalid)):
            validate_git_source("https://10.0.0.1/o/r", "main", "agent.json")


def test_dns_rebind_allowlisted_host_to_imds_rejected() -> None:
    """github.com is allowlisted, but if it resolves to IMDS it must be blocked."""
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("169.254.169.254")):
        with pytest.raises(_GitSourceBlocked):
            validate_git_source("https://github.com/o/r", "main", "agent.json")


def test_multi_record_with_one_blocked_ip_rejected() -> None:
    multi = _addrinfo_for("8.8.8.8") + _addrinfo_for("169.254.169.254")
    with patch("socket.getaddrinfo", return_value=multi):
        with pytest.raises(_GitSourceBlocked):
            validate_git_source("https://github.com/o/r", "main", "agent.json")


def test_dns_failure_rejected_loudly() -> None:
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
        with pytest.raises(_GitSourceBlocked) as exc:
            validate_git_source("https://github.com/o/r", "main", "agent.json")
        assert "could not be resolved" in str(exc.value)


def test_non_allowlisted_host_rejected_even_with_public_ip() -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("8.8.8.8")):
        with pytest.raises(_GitSourceBlocked) as exc:
            validate_git_source("https://evil.example/o/r", "main", "agent.json")
        assert "allowlist" in str(exc.value).lower()


def test_github_public_host_passes() -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        out = validate_git_source("https://github.com/octo/agents", "main", "a.json")
        assert out == {
            "repo_url": "https://github.com/octo/agents",
            "branch": "main",
            "path": "a.json",
        }


def test_gitlab_public_host_passes() -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("172.65.251.78")):
        out = validate_git_source("https://gitlab.com/octo/agents", "main", "a.json")
        assert out["repo_url"] == "https://gitlab.com/octo/agents"


def test_env_allowlist_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SYNC_HOST_ALLOWLIST", "git.acme.io")
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("8.8.8.8")):
        out = validate_git_source("https://git.acme.io/o/r", "main", "a.json")
        assert out["repo_url"] == "https://git.acme.io/o/r"
        # github.com is no longer allowed once the env override is set.
        with pytest.raises(_GitSourceBlocked):
            validate_git_source("https://github.com/o/r", "main", "a.json")


# ===========================================================================
# Path / branch validation
# ===========================================================================


@pytest.mark.parametrize(
    "bad_path",
    ["../../secrets.json", "/etc/passwd", "a/../b.json", "config.yaml", "no-ext"],
)
def test_bad_path_rejected(bad_path: str) -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        with pytest.raises(_GitSourceInvalid):
            validate_git_source("https://github.com/o/r", "main", bad_path)


@pytest.mark.parametrize("bad_branch", ["..", "feat/..", "-rf", "bad branch", ""])
def test_bad_branch_rejected(bad_branch: str) -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        with pytest.raises(_GitSourceInvalid):
            validate_git_source("https://github.com/o/r", bad_branch, "a.json")


def test_repo_url_without_owner_repo_rejected() -> None:
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        with pytest.raises(_GitSourceInvalid):
            validate_git_source("https://github.com/only-owner", "main", "a.json")


def test_exception_classes_are_value_errors() -> None:
    assert issubclass(_GitSourceInvalid, ValueError)
    assert issubclass(_GitSourceBlocked, ValueError)
    assert _GitSourceInvalid is not _GitSourceBlocked


# ===========================================================================
# fetch_workflow_spec — body parsing, schema validation, auth header, size cap
# ===========================================================================


def test_fetch_valid_spec_roundtrips() -> None:
    spec = _valid_spec()
    body = json.dumps(spec).encode()
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(body)
    ):
        out = fetch_workflow_spec(git_source, token_ref=None)
    assert out["name"] == "Synced Agent"
    # Round-trips back through the model.
    WorkflowDefinition.model_validate(out)


def test_fetch_invalid_spec_raises() -> None:
    body = json.dumps({"not": "a workflow"}).encode()
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(body)
    ):
        with pytest.raises(_GitSourceInvalid) as exc:
            fetch_workflow_spec(git_source, token_ref=None)
        assert "schema" in str(exc.value).lower()


def test_fetch_non_json_raises() -> None:
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(b"<<<not json>>>")
    ):
        with pytest.raises(_GitSourceInvalid):
            fetch_workflow_spec(git_source, token_ref=None)


def test_fetch_oversized_body_rejected() -> None:
    """A >1 MiB body must be rejected by the size cap, never fully parsed."""
    big = b"x" * (1 * 1024 * 1024 + 10)
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(big)
    ):
        with pytest.raises(_GitSourceInvalid) as exc:
            fetch_workflow_spec(git_source, token_ref=None)
        assert "size cap" in str(exc.value).lower()


def test_fetch_builds_authorization_header_from_token() -> None:
    """The resolved token must be sent as a Bearer Authorization header."""
    spec = _valid_spec()
    body = json.dumps(spec).encode()
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        return _FakeResponse(body)

    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._resolve_token", return_value="ghp_secrettoken"
    ), patch("app.services.git_sync._NO_REDIRECT_OPENER.open", side_effect=_fake_urlopen):
        fetch_workflow_spec(git_source, token_ref="arn:fake")
    # urllib title-cases header keys.
    assert captured["headers"].get("Authorization") == "Bearer ghp_secrettoken"
    assert "api.github.com/repos/octo/agents/contents/agent.json" in captured["url"]
    assert "ref=main" in captured["url"]


def test_fetch_base64_envelope_fallback() -> None:
    """If GitHub returns the default base64 envelope, we decode it."""
    import base64

    spec = _valid_spec()
    inner = json.dumps(spec).encode()
    envelope = {
        "encoding": "base64",
        "content": base64.b64encode(inner).decode(),
    }
    body = json.dumps(envelope).encode()
    git_source = {
        "repo_url": "https://github.com/octo/agents",
        "branch": "main",
        "path": "agent.json",
    }
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(body)
    ):
        out = fetch_workflow_spec(git_source, token_ref=None)
    assert out["name"] == "Synced Agent"


# ===========================================================================
# Token storage (moto) + namespace guard
# ===========================================================================


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


def test_store_git_token_creates_namespaced_secret(aws: None) -> None:
    arn = store_git_token(ALICE, "ghp_token123")
    assert ":secret:agentcore-git/" in arn
    sm = boto3.client("secretsmanager", region_name=REGION)
    desc = sm.describe_secret(SecretId=arn)
    tags = {t["Key"]: t["Value"] for t in desc.get("Tags", [])}
    assert tags["owner_sub"] == ALICE
    assert tags["Purpose"] == "git-sync-token"
    # The raw token is retrievable from the secret.
    assert sm.get_secret_value(SecretId=arn)["SecretString"] == "ghp_token123"


def test_resolve_token_rejects_arn_outside_namespace(aws: None) -> None:
    """An ARN outside agentcore-git/ must be rejected WITHOUT a GetSecretValue call."""
    sm = boto3.client("secretsmanager", region_name=REGION)
    other = sm.create_secret(Name="agentcore-otel/x-123", SecretString="nope")["ARN"]
    with patch.object(
        boto3, "client", side_effect=AssertionError("must not create a client")
    ):
        with pytest.raises(_GitSourceInvalid):
            git_sync._resolve_token(other)


def test_resolve_token_reads_namespaced_secret(aws: None) -> None:
    arn = store_git_token(ALICE, "ghp_inside")
    assert git_sync._resolve_token(arn) == "ghp_inside"


def test_resolve_token_none_returns_none() -> None:
    assert git_sync._resolve_token(None) is None


# ===========================================================================
# Endpoint tests — tenant isolation + ownership preservation
# ===========================================================================


@pytest.fixture
def storage() -> Iterator:
    from app.services.storage import (
        WorkflowStorage,
        get_workflow_storage,
        set_workflow_storage,
    )

    original = get_workflow_storage()
    fresh = WorkflowStorage()
    set_workflow_storage(fresh)
    yield fresh
    set_workflow_storage(original)


def _make_workflow(
    wf_id: str = "wf1",
    owner: str = ALICE,
    git_source: dict | None = None,
    acl: dict | None = None,
) -> WorkflowDefinition:
    now = datetime.now(timezone.utc)
    wf = WorkflowDefinition(
        id=wf_id,
        name="Original",
        description="orig",
        version="1.0.0",
        nodes=[],
        edges=[],
        viewport=Viewport(x=0, y=0, zoom=1.0),
        metadata=WorkflowMetadata(author="a", aws_region="us-east-1"),
        created_at=now,
        updated_at=now,
        owner_sub=owner,
        acl=acl,
    )
    if git_source is not None:
        wf = wf.model_copy(update={"git_source": git_source})
    return wf


def _client(caller_sub: str) -> TestClient:
    from app.routers.git_sync import router as git_sync_router

    app = FastAPI()
    app.include_router(git_sync_router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app)


_GIT_SOURCE = {
    "repo_url": "https://github.com/octo/agents",
    "branch": "main",
    "path": "agent.json",
    "token_ref": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-git/alice-abc",
}


def test_cross_tenant_git_sync_returns_404(storage) -> None:
    storage.create(_make_workflow(owner=ALICE, git_source=_GIT_SOURCE))
    resp = _client(BOB).post("/api/workflows/wf1/git-sync")
    assert resp.status_code == 404, resp.text


def test_shared_viewer_git_sync_returns_404(storage) -> None:
    """A viewer (can_view but not can_edit) must NOT be able to git-sync."""
    acl = {"owner_sub": ALICE, "editors": [], "viewers": [BOB]}
    storage.create(_make_workflow(owner=ALICE, git_source=_GIT_SOURCE, acl=acl))
    resp = _client(BOB).post("/api/workflows/wf1/git-sync")
    assert resp.status_code == 404, resp.text


def test_no_git_source_returns_400(storage) -> None:
    storage.create(_make_workflow(owner=ALICE, git_source=None))
    resp = _client(ALICE).post("/api/workflows/wf1/git-sync")
    assert resp.status_code == 400, resp.text
    assert "git_source" in resp.text


def test_owner_git_sync_updates_nodes_preserves_owner(storage) -> None:
    storage.create(_make_workflow(owner=ALICE, git_source=_GIT_SOURCE))
    # Repo spec tries to seize ownership + change id/acl.
    malicious = _valid_spec(wf_id="ATTACKER_ID", owner="attacker")
    malicious["acl"] = {"owner_sub": "attacker", "editors": ["attacker"], "viewers": []}
    malicious["name"] = "Pulled From Git"
    malicious["nodes"] = []
    body = json.dumps(malicious).encode()

    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")), patch(
        "app.services.git_sync._resolve_token", return_value="ghp_x"
    ), patch("app.services.git_sync._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(body)):
        resp = _client(ALICE).post("/api/workflows/wf1/git-sync")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # Content updated...
    assert out["name"] == "Pulled From Git"
    assert out["version"] == "2.0.0"
    # ...but ownership/identity PRESERVED (Bug 122 class).
    assert out["id"] == "wf1"
    assert out["owner_sub"] == ALICE
    # Persisted row keeps the real owner + git_source token_ref.
    stored = storage.get("wf1")
    assert stored.owner_sub == ALICE
    assert getattr(stored, "acl", None) != malicious["acl"]
    assert getattr(stored, "git_source")["token_ref"] == _GIT_SOURCE["token_ref"]


def test_owner_git_sync_with_invalid_repo_url_returns_400(storage) -> None:
    bad_source = dict(_GIT_SOURCE, repo_url="http://github.com/o/r")  # non-https
    storage.create(_make_workflow(owner=ALICE, git_source=bad_source))
    resp = _client(ALICE).post("/api/workflows/wf1/git-sync")
    assert resp.status_code == 400, resp.text


def test_git_token_endpoint_stores_and_attaches(storage, aws: None) -> None:
    storage.create(_make_workflow(owner=ALICE, git_source=None))
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        resp = _client(ALICE).post(
            "/api/workflows/wf1/git-token",
            json={
                "token": "ghp_realtoken",
                "repo_url": "https://github.com/octo/agents",
                "branch": "main",
                "path": "agent.json",
            },
        )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # The raw token is NEVER returned to the client.
    assert "ghp_realtoken" not in json.dumps(out)
    # git_source (incl. token_ref) is persisted on the stored row. We read it
    # from storage because the WorkflowDefinition.git_source shared edit has not
    # landed yet, so response_model=WorkflowDefinition strips the extra attr on
    # serialization (post-edit the response will also carry it). model_copy still
    # carries the value onto the stored object — same contract as acl.
    stored = storage.get("wf1")
    git_source = getattr(stored, "git_source")
    assert git_source["repo_url"] == "https://github.com/octo/agents"
    assert ":secret:agentcore-git/" in git_source["token_ref"]
    # The raw PAT is NEVER persisted on the row — only the ARN.
    assert "token" not in git_source
    assert "ghp_realtoken" not in json.dumps(git_source)


def test_git_token_cross_tenant_returns_404(storage, aws: None) -> None:
    storage.create(_make_workflow(owner=ALICE, git_source=None))
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("140.82.112.3")):
        resp = _client(BOB).post(
            "/api/workflows/wf1/git-token",
            json={
                "token": "ghp_x",
                "repo_url": "https://github.com/octo/agents",
                "branch": "main",
                "path": "agent.json",
            },
        )
    assert resp.status_code == 404, resp.text


def test_invalid_workflow_id_rejected(storage) -> None:
    resp = _client(ALICE).post("/api/workflows/..%2F..%2Fetc/git-sync")
    # Path-segment traversal is caught by routing/validation as 400 or 404.
    assert resp.status_code in (400, 404), resp.text
