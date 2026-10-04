"""A caller-supplied ``litellm_api_key_ref`` must be proven to be the caller's own.

``litellm_api_key_ref`` is a value the CALLER puts on a canvas, and two things read it:
the LiteLLM control plane during the deploy, and — since the virtual key stopped
travelling as a plaintext environment variable — the deployed agent, whose exec role
holds ``secretsmanager:GetSecretValue`` on the whole ``agentcore-connector/`` namespace.
So an unchecked ref is a confused deputy: name any tenant's connector secret and the
platform reads it for you.

The namespace prefix cannot be the check. ``_is_platform_connector_secret`` answers a
different question — "did the platform mint this, so may teardown delete it" — and
``secret_binding_tags``' own docstring says why it is not a tenant check: every tenant's
secrets share that prefix. Proof of ownership is the OWNER SEGMENT of the name,
``agentcore-connector/{safe_owner}/``, fixed on the real secret at creation.

These tests deliberately include the ACCEPT case. A guard tested only by what it rejects
is indistinguishable from a guard that rejects everything, which would silently break
every legitimate redeploy — the ref is exactly the value a previous deploy of the same
canvas round-trips.
"""

import pytest
from app.services.gateway_deployer import (
    connector_secret_owner_prefix,
    is_own_connector_secret,
)

REGION = "us-east-1"
ACCOUNT = "123456789012"
# Cognito subs are UUIDs, which is what makes the sanitize-and-truncate injective.
SUB_A = "54381418-7021-708e-4f3b-30505a2b82ec"
SUB_B = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"


def _arn(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"


def _minted_for(sub: str) -> str:
    """The exact name ``_put_connector_secret`` would mint, built from the SAME helper it
    uses. Hand-spelling it here would let the mint and the check drift together."""
    return _arn(f"{connector_secret_owner_prefix(sub)}deadbeef1234")


class TestIsOwnConnectorSecret:
    def test_a_secret_this_caller_minted_is_accepted(self):
        """The non-refusal case, and the one that matters for a redeploy to keep working."""
        assert is_own_connector_secret(_minted_for(SUB_A), SUB_A) is True

    def test_a_bare_name_is_accepted_as_well_as_a_full_arn(self):
        """``api_key_ref`` is documented as an ARN, but ``client_secret_ref`` in the same
        family is a NAME, so the check reads the name segment either way rather than
        rejecting a shape that is merely unexpected."""
        assert is_own_connector_secret(f"{connector_secret_owner_prefix(SUB_A)}deadbeef1234", SUB_A) is True

    def test_another_tenants_secret_is_rejected(self):
        """The whole point. Both ARNs carry the platform's namespace prefix, so a check
        built on the prefix accepts this one."""
        assert is_own_connector_secret(_minted_for(SUB_B), SUB_A) is False

    def test_a_secret_outside_the_connector_namespace_is_rejected(self):
        assert is_own_connector_secret(_arn("some/other/secret"), SUB_A) is False
        assert is_own_connector_secret(_arn("agentcore-provider/openai"), SUB_A) is False

    def test_the_namespace_prefix_alone_is_not_enough(self):
        """Pinned explicitly because this is the mistake the function exists to avoid, and
        it reads as correct: the prefix IS the platform's, it just is not the caller's."""
        assert is_own_connector_secret(_arn("agentcore-connector/anon/abc123"), SUB_A) is False

    def test_a_sibling_owner_whose_name_merely_starts_the_same_is_rejected(self):
        """The prefix ends in ``/``, so ``.../54381418-.../`` cannot be matched by a
        longer sub that begins with the same characters. Without the trailing slash a
        truncated-to-48 collision would authorize a different tenant."""
        longer = SUB_A + "-extra"
        assert is_own_connector_secret(_minted_for(longer), SUB_A) is False

    @pytest.mark.parametrize("owner_sub", ["", None])
    def test_no_caller_identity_fails_closed(self, owner_sub):
        """Nothing can be proven without a caller, and ``_put_connector_secret`` mints
        under ``anon`` in that case — so defaulting to allow would make every anonymous
        deploy able to read every other anonymous deploy's key."""
        assert is_own_connector_secret(_minted_for(SUB_A), owner_sub) is False
        assert is_own_connector_secret(_arn("agentcore-connector/anon/abc"), owner_sub) is False

    @pytest.mark.parametrize("secret_arn", ["", None])
    def test_an_empty_ref_is_rejected(self, secret_arn):
        assert is_own_connector_secret(secret_arn, SUB_A) is False


class TestTheOwnerPrefixIsTheOneTheMintUses:
    def test_the_mint_derives_its_name_from_this_helper(self):
        """Structural, because standing up Secrets Manager to observe the minted name
        would test the mock. The two expressions diverging is the concrete way this guard
        starts rejecting every legitimate ref — the failure would look like "redeploy
        broken", not like a security regression, so nobody would look here."""
        import inspect

        from app.services.gateway_deployer import _put_connector_secret

        src = inspect.getsource(_put_connector_secret)
        assert "connector_secret_owner_prefix(owner_sub)" in src, (
            "_put_connector_secret no longer mints under connector_secret_owner_prefix, so "
            "is_own_connector_secret is checking a name the platform does not create"
        )

    def test_the_prefix_is_bounded_and_ends_in_a_separator(self):
        prefix = connector_secret_owner_prefix(SUB_A)
        assert prefix.startswith("agentcore-connector/")
        assert prefix.endswith("/")
        assert len(prefix.split("/")[1]) <= 48

    def test_illegal_characters_are_sanitized_not_dropped(self):
        """A sub is a UUID in practice, but the sanitizer must not map two different
        callers onto one prefix. ``/`` in particular would forge a namespace segment."""
        assert connector_secret_owner_prefix("a/b") == "agentcore-connector/a-b/"
        assert connector_secret_owner_prefix("a b") != connector_secret_owner_prefix("ab")


class TestTheLiteLLMDeployRefusesAForeignRef:
    """Through the real deploy entry point, because the guard's value is that it runs
    BEFORE the ref is read — an ordering a unit test of the predicate cannot show."""

    def _deploy(self, monkeypatch, ref: str, owner_sub: str):
        from app.services import litellm_gateway_deployer as lgd
        from app.services.gateway_deployer import ConnectorSecretBindingError

        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        monkeypatch.setattr(
            lgd,
            "_read_secret_key",
            lambda *a, **kw: pytest.fail("the foreign ref was READ before it was checked"),
        )
        monkeypatch.setattr(
            lgd,
            "bind_connector_secret_for_deployment",
            lambda **kw: (
                pytest.fail("wrong reference passed to the ownership binder")
                if kw.get("secret_ref") != ref
                else (_ for _ in ()).throw(
                    ConnectorSecretBindingError(
                        "The credential belongs to another caller. Supply the raw "
                        "credential so the platform can store your own copy."
                    )
                )
            ),
        )
        monkeypatch.setattr(
            lgd,
            "probe_litellm_gateway",
            lambda *a, **kw: pytest.fail("must not reach the proxy with a foreign key"),
        )
        return lgd.deploy_litellm_gateway(
            gateway_config={
                "name": "gw",
                "litellm_base_url": "https://proxy.example.com",
                "litellm_api_key_ref": ref,
            },
            region=REGION,
            owner_sub=owner_sub,
            deployment_id="d1",
        )

    def test_another_tenants_ref_is_refused_before_it_is_read(self, monkeypatch):
        result = self._deploy(monkeypatch, _minted_for(SUB_B), SUB_A)
        assert result["success"] is False
        assert "another caller" in result["error"], result["error"]
        # The error must tell the caller what to do instead, not just say no.
        assert "litellm_api_key" in result["error"]

    def test_an_anonymous_caller_is_refused(self, monkeypatch):
        result = self._deploy(monkeypatch, _minted_for(SUB_A), "")
        assert result["success"] is False

    def test_the_rejection_never_echoes_the_secret_reference(self, monkeypatch):
        """The ref is not itself a credential, but it names one, and this error is
        surfaced in the UI and copied into the deployment record."""
        foreign = _minted_for(SUB_B)
        result = self._deploy(monkeypatch, foreign, SUB_A)
        assert foreign not in result["error"]
