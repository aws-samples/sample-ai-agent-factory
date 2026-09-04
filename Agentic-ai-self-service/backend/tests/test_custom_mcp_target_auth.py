"""How a CUSTOM (non-catalog) MCP endpoint sends its API key outbound.

This is the third of the three supported gateway shapes: a LiteLLM proxy wired as
an ``mcpServer`` **target inside** an AgentCore Gateway, rather than replacing the
gateway (``gateway_provider: litellm``) or not being involved at all.

The curated catalog ships an explicit ``api_key_descriptor`` per entry, so only
the custom path has to choose a default — and the wrong default produces a target
that authenticates against nothing. Verified against a live LiteLLM proxy on its
MCP endpoint ``/mcp/``: ``Authorization: <key>`` (bare, no scheme) answers 500,
while ``Authorization: Bearer <key>`` returns a valid MCP initialize result. So
the default is the Bearer form, and the header name and format are overridable
for servers that want something else.

The prefix carries **no trailing space**, which was learned the hard way against
real AWS: AgentCore joins ``credentialPrefix`` to the key with its own single
space, so ``"Bearer "`` is transmitted as ``Bearer  <key>``. Three otherwise
identical ``mcpServer`` targets on one live gateway settled as
``prefix="Bearer"`` → READY, ``prefix="Bearer "`` → FAILED *"returned HTTP 400 to
the initialize handshake"*, no prefix → FAILED. Hence the rstrip backstop, which
also covers the curated catalog and a user-typed OpenAPI prefix.
"""

from app.services.gateway_deployer import _custom_api_key_descriptor, _mcp_api_key_cred_config

PROVIDER_ARN = "arn:aws:bedrock-agentcore:eu-central-1:1:token-vault/default/apikeycredentialprovider/x"


def _cred_config(sel: dict) -> dict:
    """The credentialProviderConfigurations entry a selection actually produces."""
    descriptor = _custom_api_key_descriptor(sel)
    cfg = _mcp_api_key_cred_config(PROVIDER_ARN, descriptor)
    return cfg["credentialProvider"]["apiKeyCredentialProvider"]


class TestTheDefault:
    def test_a_custom_endpoint_with_no_descriptor_sends_authorization_bearer(self):
        """The pre-fix behavior sent a bare ``Authorization: <key>``, which carries
        no auth scheme, is invalid per RFC 7235, and is refused by LiteLLM."""
        cfg = _cred_config({"endpoint": "https://litellm.example.com/mcp/", "auth_type": "api_key"})
        assert cfg["credentialParameterName"] == "Authorization"
        assert cfg["credentialLocation"] == "HEADER"
        assert cfg["credentialPrefix"] == "Bearer"

    def test_the_prefix_never_carries_a_trailing_space(self):
        """AgentCore supplies the separator, so a trailing space would be sent as
        ``Bearer  <key>``. Proven FAILED on a real gateway."""
        cfg = _cred_config({"auth_type": "api_key"})
        assert cfg["credentialPrefix"] == cfg["credentialPrefix"].rstrip()

    def test_a_caller_supplied_trailing_space_is_stripped_not_honored(self):
        """The backstop: an operator (or an older stored canvas) sending
        ``"Bearer "`` must not produce a double-spaced header value."""
        cfg = _cred_config({"auth_type": "api_key", "api_key_descriptor": {"prefix": "Bearer "}})
        assert cfg["credentialPrefix"] == "Bearer"

    def test_a_whitespace_only_prefix_collapses_to_no_prefix(self):
        """Stripping to empty means "send the raw key", the same as ``prefix: ""``
        — never a credentialPrefix of blanks."""
        cfg = _cred_config({"auth_type": "api_key", "api_key_descriptor": {"prefix": "   "}})
        assert "credentialPrefix" not in cfg

    def test_the_provider_arn_is_carried_through_untouched(self):
        assert _cred_config({})["providerArn"] == PROVIDER_ARN


class TestOverrides:
    def test_a_litellm_proxy_can_use_its_own_header(self):
        """``x-litellm-api-key`` is LiteLLM's documented primary MCP header
        (``Authorization`` is only its secondary fallback), so a customer who
        fronts the proxy with something that consumes Authorization can move the
        key onto the LiteLLM-specific header instead."""
        cfg = _cred_config(
            {
                "endpoint": "https://litellm.example.com/mcp/",
                "auth_type": "api_key",
                "api_key_descriptor": {"parameter_name": "x-litellm-api-key", "prefix": "Bearer"},
            }
        )
        assert cfg["credentialParameterName"] == "x-litellm-api-key"
        assert cfg["credentialPrefix"] == "Bearer"

    def test_an_explicit_empty_prefix_means_send_the_raw_key(self):
        """``prefix: ""`` is distinct from omitting prefix: it is how a caller asks
        for a bare value, for a server (``x-api-key`` style) that rejects a scheme.
        ``_mcp_api_key_cred_config`` drops a falsy prefix, so no key is sent with
        an empty scheme prepended."""
        cfg = _cred_config(
            {
                "auth_type": "api_key",
                "api_key_descriptor": {"parameter_name": "x-api-key", "prefix": ""},
            }
        )
        assert cfg["credentialParameterName"] == "x-api-key"
        assert "credentialPrefix" not in cfg

    def test_a_query_parameter_location_still_works(self):
        cfg = _cred_config(
            {"auth_type": "api_key", "api_key_descriptor": {"location": "QUERY_PARAMETER", "parameter_name": "key"}}
        )
        assert cfg["credentialLocation"] == "QUERY_PARAMETER"

    def test_the_camel_case_alias_the_frontend_could_send_is_accepted(self):
        cfg = _cred_config({"apiKeyDescriptor": {"parameterName": "x-api-key"}})
        assert cfg["credentialParameterName"] == "x-api-key"


class TestTheCatalogPathIsUnaffected:
    def test_a_catalog_descriptor_is_used_verbatim(self):
        """Additive, not a substitution: the curated entries carry their own
        descriptors and must keep producing exactly what they produced before —
        including the one entry that deliberately uses a non-Bearer scheme."""
        from app.services.mcp_catalog import list_mcp_servers

        sentry = next(e for e in list_mcp_servers() if e["id"] == "sentry")
        cfg = _mcp_api_key_cred_config(PROVIDER_ARN, sentry["api_key_descriptor"])
        inner = cfg["credentialProvider"]["apiKeyCredentialProvider"]
        assert inner["credentialParameterName"] == "Authorization"
        assert inner["credentialPrefix"] == "Sentry-Bearer"

    def test_no_catalog_entry_ships_a_trailing_space_prefix(self):
        """The defect was catalog-wide, not custom-endpoint-specific: every
        bearer-style entry used to end in a space, so every API-key MCP target the
        catalog could deploy would have failed its handshake."""
        from app.services.mcp_catalog import list_mcp_servers

        offenders = [
            e["id"]
            for e in list_mcp_servers()
            if (e.get("api_key_descriptor") or {}).get("prefix", "")
            != (e.get("api_key_descriptor") or {}).get("prefix", "").rstrip()
        ]
        assert offenders == []
