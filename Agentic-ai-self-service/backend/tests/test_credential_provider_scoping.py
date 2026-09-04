"""AgentCore credential providers must be per-gateway and must not go stale.

Two defects, both found only by deploying for real and then reading the account:

1. **Cross-tenant credential sharing.** Provider names were derived from the
   catalog id / connector id / a user-typed target label — none per-tenant — while
   the token vault is account-global. Two users who both wired the same MCP server
   collided on one provider, so the second user's target authenticated with the
   FIRST user's API key and their own minted secret was never read.

2. **Silently stale credentials.** The "already exists" branch returned the
   existing provider untouched, so a rotated or corrected key never took effect.

The real-AWS evidence: two deployments of one custom MCP target minted two fresh
secrets and both reused a provider created by an earlier deployment, whose
``lastUpdatedTime`` still equalled its ``createdTime``. A deliberately invalid key
produced a **READY** target, because the invalid key was never the one being sent.
"""

import pytest
from app.services.gateway_deployer import (
    _ensure_api_key_credential_provider,
    _scoped_provider_name,
)


class _Vault:
    """Account-global credential-provider vault, keyed by name — which is exactly
    the property that made the original bug possible."""

    def __init__(self):
        self.providers: dict[str, str] = {}  # name -> secret arn
        self.updates: list[tuple[str, str]] = []

    def create_api_key_credential_provider(self, *, name, apiKeySecretConfig, apiKeySecretSource):  # noqa: N803
        if name in self.providers:
            raise RuntimeError(f"ValidationException: provider {name} already exists")
        self.providers[name] = apiKeySecretConfig["secretId"]
        return {"credentialProviderArn": f"arn:aws:bedrock-agentcore:::provider/{name}"}

    def get_api_key_credential_provider(self, *, name):
        if name not in self.providers:
            raise RuntimeError("ResourceNotFoundException")
        return {
            "credentialProviderArn": f"arn:aws:bedrock-agentcore:::provider/{name}",
            "apiKeySecretArn": {"secretArn": self.providers[name]},
        }

    def update_api_key_credential_provider(self, *, name, apiKeySecretConfig, apiKeySecretSource):  # noqa: N803
        self.providers[name] = apiKeySecretConfig["secretId"]
        self.updates.append((name, apiKeySecretConfig["secretId"]))
        return {"credentialProviderArn": f"arn:aws:bedrock-agentcore:::provider/{name}"}


class TestTheNameIsScopedToTheGateway:
    def test_two_gateways_wiring_the_same_server_get_different_providers(self):
        """The tenant-crossover case. Same target name, different gateways —
        which is what two users wiring the same catalog entry looks like."""
        a = _scoped_provider_name("mcp-mcp-exa", "gw-alice-abc123")
        b = _scoped_provider_name("mcp-mcp-exa", "gw-bob-def456")
        assert a != b

    def test_the_same_gateway_is_stable_across_redeploys(self):
        """Stability is what lets teardown find the provider it created."""
        assert _scoped_provider_name("mcp-mcp-exa", "gw-1") == _scoped_provider_name("mcp-mcp-exa", "gw-1")

    def test_the_name_stays_within_the_agentcore_limit_and_charset(self):
        """AgentCore provider names are <=64 chars, ^[a-zA-Z0-9_-]+$. A long target
        label plus a gateway id would blow the cap, and a blind truncation could
        re-collide the very names being separated."""
        import re

        name = _scoped_provider_name("mcp-" + "x" * 90, "gateway-with-a-very-long-identifier-0123456789")
        assert len(name) <= 64
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", name)

    def test_a_long_name_still_separates_two_gateways(self):
        """The truncation must never eat the part that carries the scope."""
        long_target = "mcp-" + "x" * 90
        assert _scoped_provider_name(long_target, "gw-1") != _scoped_provider_name(long_target, "gw-2")

    def test_no_scope_leaves_the_name_alone(self):
        """Callers that genuinely have no gateway (direct helper use) keep the
        old name rather than getting an unexplained digest."""
        assert _scoped_provider_name("acc-github-0", None) == "acc-github-0"


class TestAStaleCredentialIsRepointed:
    def test_a_changed_secret_updates_the_existing_provider(self):
        """The rotation case: same gateway, same target, new key. Reusing the name
        while leaving the OLD secret attached is how a corrected key silently
        fails to take effect — proven on real AWS."""
        vault = _Vault()
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v1", scope="gw-1")
        arn = _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v2", scope="gw-1")

        name = _scoped_provider_name("mcp-x", "gw-1")
        assert vault.providers[name] == "arn:secret:v2"
        assert vault.updates == [(name, "arn:secret:v2")]
        assert name in arn

    def test_an_unchanged_secret_is_not_needlessly_updated(self):
        vault = _Vault()
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v1", scope="gw-1")
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v1", scope="gw-1")
        assert vault.updates == []

    def test_one_gateways_rotation_cannot_touch_anothers_credential(self):
        """The two fixes together: scoping means a redeploy of gateway 2 repoints
        only its own provider, and cannot swap the key out from under an agent
        already running on gateway 1."""
        vault = _Vault()
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:alice", scope="gw-1")
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:bob", scope="gw-2")
        assert vault.providers[_scoped_provider_name("mcp-x", "gw-1")] == "arn:secret:alice"
        assert vault.providers[_scoped_provider_name("mcp-x", "gw-2")] == "arn:secret:bob"
        assert vault.updates == []

    def test_a_repoint_that_cannot_be_performed_fails_the_deploy(self):
        """If the provider is bound to an older secret and cannot be updated (no
        UpdateApiKeyCredentialProvider grant, say), the deploy must stop. Handing
        back the provider anyway would send the key the deployer already knows is
        stale — the silent failure the whole branch exists to close."""

        class _NoUpdate(_Vault):
            def update_api_key_credential_provider(self, **kw):
                raise RuntimeError("AccessDeniedException")

        vault = _NoUpdate()
        _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v1", scope="gw-1")
        with pytest.raises(RuntimeError, match="could not be repointed"):
            _ensure_api_key_credential_provider(vault, "mcp-x", secret_arn="arn:secret:v2", scope="gw-1")

    def test_a_create_failure_that_is_not_a_conflict_still_raises(self):
        """Only "already exists" may be swallowed — a real error must not be
        reinterpreted as a successful reuse."""

        class _Broken(_Vault):
            def create_api_key_credential_provider(self, **kw):
                raise RuntimeError("AccessDeniedException: not allowed")

        with pytest.raises(RuntimeError, match="AccessDenied"):
            _ensure_api_key_credential_provider(_Broken(), "mcp-x", secret_arn="arn:secret:v1", scope="gw-1")


class TestTeardownFindsWhatWasCreated:
    def test_the_recorded_name_matches_the_created_name(self, monkeypatch):
        """A teardown manifest name that does not match byte-for-byte orphans a
        credential provider in the account forever."""
        from app.services import gateway_deployer as gd

        vault = _Vault()
        monkeypatch.setattr(gd, "_put_connector_secret", lambda region, owner, payload: "arn:secret:minted")
        monkeypatch.setattr(
            gd,
            "_create_gateway_target_with_retry",
            lambda ctrl, gw, name, params: {"targetId": "t-1", "name": name},
        )
        monkeypatch.setattr(gd, "_wait_for_mcp_target_ready", lambda *a, **k: None)

        out = gd._deploy_external_mcp_targets(
            vault,
            "gw-77",
            "us-east-1",
            [{"endpoint": "https://example.com/mcp", "auth_type": "api_key", "secret_value": "sk-x", "name": "My MCP"}],
            owner_sub="alice",
        )
        # Each record is "TYPE:name" — the type is what routes teardown to the right
        # namespace (see tests/test_credential_provider_teardown.py). The NAME half
        # is what this test is about, and it still has to match byte-for-byte.
        recorded = out["credential_provider_names"]
        assert [e.split(":", 1)[0] for e in recorded] == ["API_KEY"]
        assert [e.split(":", 1)[1] for e in recorded] == list(vault.providers.keys())
