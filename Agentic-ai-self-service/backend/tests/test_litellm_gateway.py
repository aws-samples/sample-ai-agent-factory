"""Workstream A — LiteLLM MCP Gateway as an ADDITIONAL gateway provider.

Every assertion here is about the *new* provider or about the fact that the
AgentCore path is unchanged. No existing test was edited to make this pass;
that is the regression signal for "additive, not a substitution".

The properties worth defending, in order of how badly they bite:

1. Dispatch. ``gateway_step`` must route on the provider, and must still call
   ``deploy_gateway`` for everything that is not explicitly ``litellm``.
2. Secret hygiene. The raw virtual key must be gone from the dict that
   ``{**event}`` re-emits into the Step Functions payload — in BOTH spellings.
3. SSRF. The base URL is customer-supplied and reaches ``urllib`` inside a
   Lambda with an IMDS endpoint, so the existing guard must run before any
   request goes out.
4. Fail loud. A gateway serving zero tools deploys an agent that looks healthy
   and has no capabilities. Same stance as the AgentCore empty-tool-plane gate.
5. Auth handoff. ``client_info["provider"] == "litellm"`` is the only signal
   that reaches ``runtime_configure_step``, and the generated agent behaves
   entirely off the env vars that arm produces.
"""

import json
import urllib.error

import pytest
from app.models.components import GatewayConfiguration
from app.services import litellm_gateway_deployer as lgd
from app.services.code_generator import _generate_memory_agent, _generate_strands_gateway
from pydantic import ValidationError

BASE = "https://litellm.example.com"
_LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:tool"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStore:
    """Captures record_resource calls; update_step is a no-op like the real one."""

    def __init__(self):
        self.resources = []
        self.steps = []

    def update_step(self, *a, **kw):
        self.steps.append((a, kw))

    def record_resource(self, deployment_id, resource):
        self.resources.append((deployment_id, resource))


def _fake_get_json(servers=None, tools=None, raises=None):
    """Stand in for the two probe GETs, keyed on which path was requested."""
    calls = []

    def _impl(url, api_key, srv=None):
        calls.append((url, api_key, srv))
        if raises is not None:
            raise raises
        if url.endswith(lgd._SERVERS_PATH):
            return servers if servers is not None else [{"server_name": "github"}]
        if url.endswith(lgd._TOOLS_LIST_PATH):
            return tools if tools is not None else [{"name": "list_issues"}]
        raise AssertionError(f"probe hit an unexpected URL: {url}")

    _impl.calls = calls
    return _impl


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


class TestResolveGatewayProvider:
    def test_agentcore_is_the_default_for_a_legacy_canvas(self, monkeypatch):
        """A stored canvas from before this feature has no provider field at all.
        It MUST keep deploying an AgentCore Gateway with no migration."""
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: "agentcore")
        assert lgd.resolve_gateway_provider({}) == "agentcore"
        assert lgd.resolve_gateway_provider(None) == "agentcore"

    @pytest.mark.parametrize("key", ["gateway_provider", "gatewayProvider"])
    def test_per_node_choice_wins_over_the_platform_default(self, monkeypatch, key):
        """The plan's chosen scope: per-agent on the canvas, with a platform
        default. Both the snake_case field and its camelCase alias arrive
        depending on whether the payload came through Pydantic or raw."""
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: "agentcore")
        assert lgd.resolve_gateway_provider({key: "litellm"}) == "litellm"

    def test_the_platform_default_applies_when_the_node_is_silent(self, monkeypatch):
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: "litellm")
        assert lgd.resolve_gateway_provider({}) == "litellm"

    def test_the_platform_default_never_hijacks_an_agentcore_shaped_node(self, monkeypatch):
        """The feature is additive, so flipping the platform default to litellm must
        not reinterpret canvases built for AgentCore. Such a node names no LiteLLM
        base URL and *does* wire targets — deploying it as LiteLLM could only fail
        on the missing base URL while discarding its targets, so it stays
        agentcore. A node that is genuinely empty still follows the default.
        """
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: "litellm")
        for cfg in (
            {"targetType": "lambda", "targetConfig": {"type": "lambda", "functionArn": "arn:x"}},
            {"target_type": "openapi"},
            {"targets": [{"type": "lambda", "functionArn": "arn:x"}]},
        ):
            assert lgd.resolve_gateway_provider(cfg) == "agentcore", cfg
        # A node that names a base URL is a real LiteLLM node even with stale
        # target fields left over from a switched provider.
        assert lgd.resolve_gateway_provider({"targetType": "lambda", "litellmBaseUrl": "https://p"}) == "litellm"

    def test_an_unrecognized_value_degrades_to_agentcore(self, monkeypatch):
        """Garbage means a hand-edited or legacy canvas. The old behaviour is the
        safe answer — never fail a deploy over it."""
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: pytest.fail("must not consult the default"))
        assert lgd.resolve_gateway_provider({"gateway_provider": "nonsense"}) == "agentcore"


class TestDefaultGatewayProvider:
    def test_env_override_wins_without_touching_dynamo(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_GATEWAY_PROVIDER", "litellm")
        monkeypatch.setattr(lgd, "_settings_table", lambda: pytest.fail("must not read the table"))
        assert lgd.default_gateway_provider() == "litellm"

    def test_a_bad_env_value_is_ignored_and_the_table_decides(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_GATEWAY_PROVIDER", "not-a-provider")

        class _T:
            def get_item(self, Key):  # noqa: N803 — boto3 kwarg name
                assert Key == {"org_id": "default", "sk": "SETTING#gateway_provider"}
                return {"Item": {"value": "litellm"}}

        monkeypatch.setattr(lgd, "_settings_table", lambda: _T())
        assert lgd.default_gateway_provider() == "litellm"

    def test_an_unreadable_table_resolves_to_agentcore(self, monkeypatch):
        """The lookup swallows its own errors on purpose: a Dynamo blip must not
        flip the platform to a provider nobody chose."""
        monkeypatch.delenv("DEFAULT_GATEWAY_PROVIDER", raising=False)

        def _boom():
            raise RuntimeError("AccessDeniedException")

        monkeypatch.setattr(lgd, "_settings_table", _boom)
        assert lgd.default_gateway_provider() == "agentcore"

    def test_a_missing_row_resolves_to_agentcore(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_GATEWAY_PROVIDER", raising=False)

        class _T:
            def get_item(self, Key):  # noqa: N803
                return {}

        monkeypatch.setattr(lgd, "_settings_table", lambda: _T())
        assert lgd.default_gateway_provider() == "agentcore"

    def test_the_setter_refuses_an_unknown_provider(self, monkeypatch):
        monkeypatch.setattr(lgd, "_settings_table", lambda: pytest.fail("must not write"))
        with pytest.raises(ValueError, match="gateway provider must be one of"):
            lgd.set_default_gateway_provider("litellm-ish")


# ---------------------------------------------------------------------------
# Model layer
# ---------------------------------------------------------------------------


class TestGatewayConfigurationModel:
    def test_agentcore_still_requires_a_target(self):
        """The pre-existing rule. If this ever relaxes, an AgentCore gateway can
        be deployed with nothing wired to it."""
        with pytest.raises(ValidationError, match="target_type and target_config are required"):
            GatewayConfiguration(name="gw")

    def test_the_default_provider_is_agentcore(self):
        cfg = GatewayConfiguration(
            name="gw", target_type="lambda", target_config={"type": "lambda", "function_arn": _LAMBDA_ARN}
        )
        assert cfg.gateway_provider == "agentcore"

    def test_litellm_needs_a_base_url_but_no_target(self):
        cfg = GatewayConfiguration(name="gw", gatewayProvider="litellm", litellmBaseUrl=BASE)
        assert cfg.gateway_provider == "litellm"
        assert cfg.target_type is None

        with pytest.raises(ValidationError, match="litellm_base_url is required"):
            GatewayConfiguration(name="gw", gatewayProvider="litellm")

    def test_an_agentcore_target_riding_along_is_ignored_not_rejected(self):
        """Switching an existing node to LiteLLM leaves the old target fields on
        the canvas. Failing the whole config over them would make the provider
        switch look broken."""
        cfg = GatewayConfiguration(
            name="gw",
            gatewayProvider="litellm",
            litellmBaseUrl=BASE,
            target_type="lambda",
            target_config={"type": "lambda", "function_arn": _LAMBDA_ARN},
        )
        assert cfg.gateway_provider == "litellm"

    def test_the_raw_key_never_serializes(self):
        """``exclude=True`` is what keeps the key out of any response body or
        persisted canvas — only the ARN is allowed to travel."""
        cfg = GatewayConfiguration(name="gw", gatewayProvider="litellm", litellmBaseUrl=BASE, litellmApiKey="sk-secret")
        assert cfg.litellm_api_key == "sk-secret"
        dumped = cfg.model_dump()
        assert "litellm_api_key" not in dumped
        assert "sk-secret" not in json.dumps(cfg.model_dump(mode="json"))
        assert "sk-secret" not in repr(cfg)


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------


class TestBaseUrlIsGuarded:
    @pytest.mark.parametrize(
        "bad",
        [
            "http://litellm.example.com",  # not https
            "https://127.0.0.1:4000",  # loopback
            "https://169.254.169.254/latest/meta-data/",  # IMDS
            "https://10.0.0.5:4000",  # RFC1918
            "file:///etc/passwd",
        ],
    )
    def test_a_disallowed_base_url_is_refused_before_any_request(self, monkeypatch, bad):
        monkeypatch.setattr(lgd, "_get_json", lambda *a, **kw: pytest.fail("SSRF guard did not run first"))
        monkeypatch.setattr(lgd, "_put_connector_secret", lambda *a, **kw: pytest.fail("must not mint a secret"))
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": bad, "litellm_api_key": "sk-x"},
            region="eu-central-1",
        )
        assert result["success"] is False
        assert "rejected" in result["error"].lower()
        assert "sk-x" not in result["error"]

    def test_the_rejection_names_the_litellm_base_url_not_oidc_discovery(self, monkeypatch):
        """The SSRF guard is shared with OIDC discovery; the message must not be.

        Live-observed: an http:// base URL failed with "OIDC discovery URL must use
        https scheme", which points an operator at their IdP config instead of at
        the gateway node they just edited. The guard now takes a label.
        """
        monkeypatch.setattr(lgd, "_put_connector_secret", lambda *a, **kw: pytest.fail("must not mint a secret"))
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": "http://litellm.example.com", "litellm_api_key": "sk-x"},
            region="eu-central-1",
        )
        assert result["success"] is False
        assert "LiteLLM base URL" in result["error"]
        assert "OIDC" not in result["error"]

    def test_the_shared_guard_still_says_oidc_for_its_original_callers(self):
        """The label defaults, so no pre-existing caller's message changed."""
        from app.services.gateway_deployer import _validate_outbound_url

        with pytest.raises(Exception, match="OIDC discovery URL must use https scheme"):
            _validate_outbound_url("http://idp.example.com/.well-known/openid-configuration")

    def test_a_missing_base_url_is_a_clean_failure(self):
        result = lgd.deploy_litellm_gateway(gateway_config={"name": "gw"}, region="eu-central-1")
        assert result["success"] is False
        assert "base URL" in result["error"]

    def test_a_missing_key_is_a_clean_failure(self, monkeypatch):
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": BASE}, region="eu-central-1"
        )
        assert result["success"] is False
        assert "virtual key" in result["error"]


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


class TestProbe:
    def test_zero_tools_fails_loud(self, monkeypatch):
        """Bug 138's stance, ported. A 200 OK with an empty tool list is the
        worst outcome: the deploy succeeds and the agent silently has nothing."""
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(tools=[]))
        with pytest.raises(lgd.LiteLLMGatewayError, match="served 0 tools"):
            lgd.probe_litellm_gateway(BASE, "sk-x")

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ([{"name": "a"}], ["a"]),
            ({"tools": [{"name": "a"}, {"tool_name": "b"}]}, ["a", "b"]),
            ({"data": ["a", "b"]}, ["a", "b"]),
            ({"result": [{"name": "a"}]}, ["a"]),
        ],
    )
    def test_both_the_bare_list_and_the_wrapped_envelope_parse(self, monkeypatch, payload, expected):
        """LiteLLM has shipped both shapes. Pinning the probe to one release's
        envelope would read as "0 tools" against the other and refuse a healthy
        gateway."""
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(tools=payload))
        assert lgd.probe_litellm_gateway(BASE, "sk-x")["tools"] == expected

    def test_a_pinned_server_the_proxy_does_not_serve_is_named(self, monkeypatch):
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(servers=[{"server_name": "github"}]))
        with pytest.raises(lgd.LiteLLMGatewayError, match="does not serve MCP server"):
            lgd.probe_litellm_gateway(BASE, "sk-x", ["githbu"])

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [(401, "rejected the virtual key"), (403, "rejected the virtual key"), (404, "no .* route")],
    )
    def test_http_failures_are_classified(self, monkeypatch, code, fragment):
        """A bad key and a wrong base URL are different user-fixable mistakes,
        and 'something went wrong' sends the user looking in the wrong place."""
        err = urllib.error.HTTPError(BASE, code, "nope", {}, None)
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(raises=err))
        with pytest.raises(lgd.LiteLLMGatewayError, match=fragment):
            lgd.probe_litellm_gateway(BASE, "sk-x")

    def test_a_network_error_reports_the_type_only(self, monkeypatch):
        """A self-hosted LiteLLM may simply be private and unroutable from this
        Lambda. Keep the message typed and short rather than echoing internals."""
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(raises=urllib.error.URLError("timed out")))
        with pytest.raises(lgd.LiteLLMGatewayError, match="unreachable"):
            lgd.probe_litellm_gateway(BASE, "sk-x")

    def test_the_probe_never_calls_a_tool(self, monkeypatch):
        """Readiness is a read. Invoking POST /mcp-rest/tools/call to prove
        liveness would execute a real customer tool as a side effect."""
        fake = _fake_get_json()
        monkeypatch.setattr(lgd, "_get_json", fake)
        lgd.probe_litellm_gateway(BASE, "sk-x")
        assert [u for u, _, _ in fake.calls] == [
            BASE + lgd._SERVERS_PATH,
            BASE + lgd._TOOLS_LIST_PATH,
        ]
        assert not any("tools/call" in u for u, _, _ in fake.calls)


class TestResolveMcpUrl:
    def test_no_pin_uses_the_aggregate_endpoint(self):
        assert lgd.resolve_mcp_url(BASE) == f"{BASE}/mcp/"
        assert lgd.resolve_mcp_url(BASE + "/", []) == f"{BASE}/mcp/"

    def test_exactly_one_pin_uses_the_per_server_endpoint(self):
        assert lgd.resolve_mcp_url(BASE, ["github"]) == f"{BASE}/github/mcp"

    def test_several_pins_keep_the_aggregate_so_none_are_dropped(self):
        """The per-server form can only name one. Picking arbitrarily would
        silently discard the rest; the aggregate + x-mcp-servers scopes properly."""
        assert lgd.resolve_mcp_url(BASE, ["github", "jira"]) == f"{BASE}/mcp/"

    def test_an_alias_is_url_encoded(self):
        assert lgd.resolve_mcp_url(BASE, ["a b/c"]) == f"{BASE}/a%20b%2Fc/mcp"


class TestHeaders:
    def test_the_key_and_the_server_scope_ride_their_own_headers(self):
        h = lgd._headers("sk-x", ["github", "jira"])
        assert h[lgd._KEY_HEADER] == "Bearer sk-x"
        assert h[lgd._SERVERS_HEADER] == "github,jira"

    def test_no_scope_header_when_nothing_is_pinned(self):
        assert lgd._SERVERS_HEADER not in lgd._headers("sk-x", [])

    def test_the_key_value_carries_a_bearer_prefix(self):
        """Verified against a live proxy: the REST probe paths accept a bare key,
        but the MCP protocol endpoint /mcp/ does NOT — it falls through to a
        virtual-key DB lookup and fails, while ``Bearer <key>`` handshakes. Since
        LiteLLM strips the prefix server-side, prefixing is free on the paths that
        already worked. Probing with a form the agent cannot use would make the
        deploy pass and the agent fail, so both must send the same thing."""
        assert lgd.bearer_key_value("sk-x") == "Bearer sk-x"

    def test_an_already_prefixed_key_is_not_doubled(self):
        """An operator may paste the value in the form LiteLLM's own mcp_debug.py
        prints, prefix included."""
        assert lgd.bearer_key_value("Bearer sk-x") == "Bearer sk-x"

    def test_an_empty_key_stays_empty_rather_than_becoming_a_bare_bearer(self):
        assert lgd.bearer_key_value("") == ""
        assert lgd._KEY_HEADER not in lgd._headers("")


# ---------------------------------------------------------------------------
# deploy_litellm_gateway contract
# ---------------------------------------------------------------------------


class TestDeployContract:
    def _deploy(self, monkeypatch, **overrides):
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u.rstrip("/"))
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json())
        minted = []

        def _mint(region, owner, payload):
            minted.append((region, owner, payload))
            return "arn:aws:secretsmanager:eu-central-1:1:secret:agentcore-connector/x"

        monkeypatch.setattr(lgd, "_put_connector_secret", _mint)
        cfg = {"name": "gw", "litellm_base_url": BASE, "litellm_api_key": "sk-x"}
        cfg.update(overrides)
        return lgd.deploy_litellm_gateway(gateway_config=cfg, region="eu-central-1"), minted

    def test_it_returns_the_same_shape_deploy_gateway_returns(self, monkeypatch):
        """This is what lets the Step Functions state machine stay unchanged."""
        result, _ = self._deploy(monkeypatch)
        assert result["success"] is True
        assert result["gateway_url"] == f"{BASE}/mcp/"
        assert result["gateway_name"] == "gw"
        assert result["client_info"]["provider"] == "litellm"
        assert result["qualified_tools"] == ["list_issues"]
        assert result["expected_tool_count"] == 1
        for key in (
            "gateway_id",
            "gateway_arn",
            "gateway_name",
            "client_info",
            "lambda_function_name",
            "custom_tool_lambdas",
            "custom_tool_roles",
            "kb_lambda_name",
            "connector_credential_providers",
            "connector_secret_arns",
            "connector_spec_s3_uris",
        ):
            assert key in result, f"missing contract key {key}"

    def test_no_gateway_id_is_reported_because_none_exists(self, monkeypatch):
        """``_record_gateway_resources`` and the teardown dispatcher both key off
        truthiness. A faked id would make teardown chase something unresolvable,
        and a truthy id would make it record an AgentCoreGateway-* role that was
        never created."""
        result, _ = self._deploy(monkeypatch)
        assert result["gateway_id"] is None
        assert result["gateway_arn"] is None

    def test_the_only_recorded_aws_resource_is_the_secret(self, monkeypatch):
        result, _ = self._deploy(monkeypatch)
        assert result["connector_secret_arns"] == [result["client_info"]["api_key_ref"]]
        assert result["custom_tool_lambdas"] == []
        assert result["connector_credential_providers"] == []

    def test_the_secret_is_minted_only_after_the_probe_passes(self, monkeypatch):
        """Otherwise every attempt with a typo'd key leaves an orphan secret."""
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(tools=[]))
        monkeypatch.setattr(lgd, "_put_connector_secret", lambda *a: pytest.fail("minted despite a failed probe"))
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": BASE, "litellm_api_key": "sk-x"},
            region="eu-central-1",
        )
        assert result["success"] is False
        assert "0 tools" in result["error"]

    def test_the_error_string_never_carries_the_key(self, monkeypatch):
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        err = urllib.error.HTTPError(BASE, 401, "nope", {}, None)
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json(raises=err))
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": BASE, "litellm_api_key": "sk-topsecret"},
            region="eu-central-1",
        )
        assert result["success"] is False
        assert "sk-topsecret" not in result["error"]

    def test_a_redeploy_reuses_the_stored_arn_instead_of_minting_again(self, monkeypatch):
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json())
        monkeypatch.setattr(lgd, "_put_connector_secret", lambda *a: pytest.fail("re-minted"))
        monkeypatch.setattr(lgd, "_read_secret_key", lambda region, arn: "sk-from-secret")
        result = lgd.deploy_litellm_gateway(
            gateway_config={
                "name": "gw",
                "litellm_base_url": BASE,
                "litellm_api_key_ref": "arn:aws:secretsmanager:eu-central-1:1:secret:x",
            },
            region="eu-central-1",
        )
        assert result["success"] is True
        assert result["client_info"]["api_key_ref"].endswith(":secret:x")

    def test_a_comma_string_of_servers_is_accepted(self, monkeypatch):
        """The modal collects aliases as free text; the deploy payload can carry
        either a list or the raw comma string depending on the path taken."""
        result, _ = self._deploy(monkeypatch, litellm_servers="github", litellm_base_url=BASE)
        assert result["litellm_servers"] == ["github"]
        assert result["gateway_url"] == f"{BASE}/github/mcp"

    def test_agentcore_only_kwargs_are_absorbed_not_half_applied(self, monkeypatch):
        """The direct path and the step handler both pass the full AgentCore
        kwarg set by name. Accepting and ignoring them beats a TypeError, but
        they must have no effect."""
        monkeypatch.setattr(lgd, "_validate_outbound_url", lambda u, **kw: u)
        monkeypatch.setattr(lgd, "_get_json", _fake_get_json())
        monkeypatch.setattr(lgd, "_put_connector_secret", lambda *a: "arn:secret")
        result = lgd.deploy_litellm_gateway(
            gateway_config={"name": "gw", "litellm_base_url": BASE, "litellm_api_key": "sk-x"},
            region="eu-central-1",
            gateway_tools=["duckduckgo_search"],
            custom_tools=[{"name": "x"}],
            connectors=[{"connector_id": "github"}],
            knowledge_base_result={"knowledge_base_id": "KB1"},
        )
        assert result["success"] is True
        assert result["kb_lambda_name"] is None
        assert result["custom_tool_lambdas"] == []


# ---------------------------------------------------------------------------
# gateway_step dispatch + secret hygiene
# ---------------------------------------------------------------------------


class TestGatewayStepDispatch:
    def _patch(self, monkeypatch, litellm_result=None):
        from app.step_handlers import gateway_step

        # Pin the region explicitly: the step reads APP_AWS_REGION/AWS_REGION and
        # the developer shell exports us-west-2, so an ambient value would make
        # the manifest assertion below pass or fail by accident.
        monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
        store = _FakeStore()
        monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
        monkeypatch.setattr(gateway_step, "deploy_gateway", lambda **kw: pytest.fail("AgentCore path taken"))
        seen = {}

        def _fake_litellm(*, gateway_config, region, owner_sub="", deployment_id=None):
            seen["gateway_config"] = dict(gateway_config)
            seen["region"] = region
            seen["owner_sub"] = owner_sub
            return litellm_result or {
                "success": True,
                "gateway_url": f"{BASE}/mcp/",
                "gateway_id": None,
                "gateway_name": "gw",
                "client_info": {"provider": "litellm", "api_key_ref": "arn:secret:k"},
                "litellm_servers": [],
            }

        monkeypatch.setattr(gateway_step, "deploy_litellm_gateway", _fake_litellm)
        return gateway_step, store, seen

    @pytest.mark.parametrize("key", ["litellm_api_key", "litellmApiKey"])
    def test_the_raw_key_is_dropped_from_the_re_emitted_event(self, monkeypatch, key):
        """``{**event}`` is the Step Functions payload for every later step. A
        plaintext key surviving on ``gateway_config`` would land in the execution
        history. Both spellings must be popped unconditionally — the
        short-circuit form ``a.pop() or b.pop()`` skips the second one."""
        gateway_step, _store, seen = self._patch(monkeypatch)
        event = {
            "deployment_id": "d1",
            "gateway_config": {
                "name": "gw",
                "gateway_provider": "litellm",
                "litellm_base_url": BASE,
                key: "sk-topsecret",
            },
        }
        out = gateway_step.handler(event, None)

        assert "sk-topsecret" not in json.dumps(out)
        assert "litellm_api_key" not in out["gateway_config"]
        assert "litellmApiKey" not in out["gateway_config"]
        # The deployer still needs it for the readiness probe.
        assert seen["gateway_config"]["litellm_api_key"] == "sk-topsecret"

    def test_both_spellings_are_popped_even_when_both_arrive(self, monkeypatch):
        gateway_step, _store, _seen = self._patch(monkeypatch)
        out = gateway_step.handler(
            {
                "deployment_id": "d1",
                "gateway_config": {
                    "gateway_provider": "litellm",
                    "litellm_base_url": BASE,
                    "litellm_api_key": "sk-snake",
                    "litellmApiKey": "sk-camel",
                },
            },
            None,
        )
        assert "sk-snake" not in json.dumps(out)
        assert "sk-camel" not in json.dumps(out)

    def test_the_key_arn_is_recorded_under_id_so_teardown_can_delete_it(self, monkeypatch):
        """``_delete_managed_resource`` derives its target as
        ``res.get("id") or res.get("name")`` — it never reads ``arn``, so an
        "arn" key here would call delete_secret(SecretId="")."""
        gateway_step, store, _seen = self._patch(monkeypatch)
        gateway_step.handler(
            {
                "deployment_id": "d1",
                "gateway_config": {
                    "gateway_provider": "litellm",
                    "litellm_base_url": BASE,
                    "litellm_api_key": "sk-x",
                },
            },
            None,
        )
        assert store.resources == [("d1", {"type": "secret", "id": "arn:secret:k", "region": "us-east-1"})]

    def test_the_arn_is_written_back_for_a_redeploy(self, monkeypatch):
        gateway_step, _store, _seen = self._patch(monkeypatch)
        out = gateway_step.handler(
            {
                "deployment_id": "d1",
                "gateway_config": {
                    "gateway_provider": "litellm",
                    "litellm_base_url": BASE,
                    "litellm_api_key": "sk-x",
                },
            },
            None,
        )
        assert out["gateway_config"]["litellm_api_key_ref"] == "arn:secret:k"

    def test_a_failed_litellm_deploy_raises_like_the_agentcore_path(self, monkeypatch):
        gateway_step, _store, _seen = self._patch(
            monkeypatch, litellm_result={"success": False, "error": "LiteLLM gateway served 0 tools."}
        )
        with pytest.raises(RuntimeError, match="Gateway deployment failed"):
            gateway_step.handler(
                {
                    "deployment_id": "d1",
                    "gateway_config": {
                        "gateway_provider": "litellm",
                        "litellm_base_url": BASE,
                        "litellm_api_key": "sk-x",
                    },
                },
                None,
            )

    def test_the_event_is_otherwise_passed_through_untouched(self, monkeypatch):
        gateway_step, _store, _seen = self._patch(monkeypatch)
        out = gateway_step.handler(
            {
                "deployment_id": "d1",
                "config": {"name": "agent"},
                "role_arn": "arn:role",
                "gateway_config": {
                    "gateway_provider": "litellm",
                    "litellm_base_url": BASE,
                    "litellm_api_key": "sk-x",
                },
            },
            None,
        )
        assert out["config"] == {"name": "agent"}
        assert out["role_arn"] == "arn:role"

    @pytest.mark.parametrize("cfg", [{}, {"gateway_provider": "agentcore"}, {"gateway_provider": "bogus"}])
    def test_anything_not_litellm_still_goes_to_agentcore(self, monkeypatch, cfg):
        """The whole point of the workstream: this is additive. Everything that
        does not explicitly say ``litellm`` must reach ``deploy_gateway``."""
        from app.step_handlers import gateway_step

        store = _FakeStore()
        monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
        monkeypatch.setattr(
            gateway_step,
            "deploy_litellm_gateway",
            lambda **kw: pytest.fail("LiteLLM path taken for an AgentCore node"),
        )
        monkeypatch.setattr(lgd, "default_gateway_provider", lambda: "agentcore")
        called = {}

        def _fake_agentcore(**kwargs):
            called["kwargs"] = kwargs
            return {"success": True, "gateway_id": "gw-1", "gateway_name": "gw", "client_info": {}}

        monkeypatch.setattr(gateway_step, "deploy_gateway", _fake_agentcore)
        out = gateway_step.handler({"deployment_id": "d1", "gateway_config": dict(cfg)}, None)
        assert called["kwargs"]["gateway_config"] == cfg
        assert out["gateway_result"]["gateway_id"] == "gw-1"


# ---------------------------------------------------------------------------
# Runtime auth handoff
# ---------------------------------------------------------------------------


class TestRuntimeConfigureAuthMode:
    def _run(self, monkeypatch, gateway_result, secret_payload='{"apiKey": "sk-resolved"}'):
        from app.step_handlers import runtime_configure_step as rcs

        monkeypatch.setattr(rcs, "_get_deployment_store", lambda: _FakeStore())
        monkeypatch.setattr(rcs.step_clients, "client", lambda event, svc, **kw: object())
        monkeypatch.setattr(rcs, "sanitize_runtime_name", lambda n: "agent_x")
        monkeypatch.setattr(rcs, "build_otel_env_vars", lambda *a, **kw: {})
        monkeypatch.setattr(rcs, "get_platform_observability_defaults", lambda: {})
        monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)

        class _SM:
            def get_secret_value(self, SecretId):  # noqa: N803
                if secret_payload is None:
                    raise RuntimeError("AccessDeniedException")
                return {"SecretString": secret_payload}

        import boto3

        monkeypatch.setattr(boto3, "client", lambda svc, **kw: _SM())

        captured = {}

        def _fake_create(**kwargs):
            captured["env_vars"] = kwargs.get("env_vars") or {}
            return {"runtime_id": "agent_x-123", "runtime_arn": "arn:runtime"}

        monkeypatch.setattr(rcs, "create_agent_runtime", _fake_create)
        rcs.handler(
            {
                "deployment_id": "d1",
                "config": {
                    "name": "agent",
                    "entrypoint": "agent.py",
                    "model": {"modelId": "anthropic.claude-sonnet-5"},
                },
                "role_arn": "arn:role",
                "s3_bucket": "b",
                "s3_key": "k",
                "gateway_result": gateway_result,
            },
            None,
        )
        return captured["env_vars"]

    def test_litellm_switches_the_agent_to_a_static_bearer(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {
                "gateway_url": f"{BASE}/mcp/",
                "client_info": {"provider": "litellm", "api_key_ref": "arn:secret:k"},
                "litellm_servers": ["github", "jira"],
            },
        )
        assert env["GATEWAY_URL"] == f"{BASE}/mcp/"
        assert env["GATEWAY_AUTH_MODE"] == "static_bearer"
        assert env["GATEWAY_API_KEY"] == "sk-resolved"
        assert env["GATEWAY_MCP_SERVERS"] == "github,jira"
        # No Cognito exchange exists for LiteLLM; leaking these would make the
        # generated agent try a token endpoint that isn't there.
        assert "COGNITO_CLIENT_ID" not in env
        assert "COGNITO_TOKEN_ENDPOINT" not in env

    def test_no_server_scope_header_when_nothing_is_pinned(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {
                "gateway_url": f"{BASE}/mcp/",
                "client_info": {"provider": "litellm", "api_key_ref": "arn:secret:k"},
                "litellm_servers": [],
            },
        )
        assert "GATEWAY_MCP_SERVERS" not in env

    def test_an_unreadable_secret_does_not_fail_the_deploy(self, monkeypatch):
        """The deploy still produces a runtime; the gateway simply comes up
        unauthenticated and the MCP discovery gate catches it loudly. Failing
        here would be worse: a half-created runtime with no manifest row."""
        env = self._run(
            monkeypatch,
            {
                "gateway_url": f"{BASE}/mcp/",
                "client_info": {"provider": "litellm", "api_key_ref": "arn:secret:k"},
            },
            secret_payload=None,
        )
        assert env["GATEWAY_AUTH_MODE"] == "static_bearer"
        assert "GATEWAY_API_KEY" not in env

    def test_the_cognito_path_is_unchanged(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {
                "gateway_url": "https://gw.example.com/mcp",
                "client_info": {
                    "provider": "cognito",
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "token_endpoint": "https://auth/token",
                    "scope": "gw/invoke",
                },
            },
        )
        assert env["COGNITO_CLIENT_ID"] == "cid"
        assert env["COGNITO_TOKEN_ENDPOINT"] == "https://auth/token"
        assert "GATEWAY_AUTH_MODE" not in env
        assert "GATEWAY_API_KEY" not in env

    def test_an_external_idp_is_unchanged(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {
                "gateway_url": "https://gw.example.com/mcp",
                "client_info": {"provider": "okta", "client_id": "cid", "token_endpoint": "https://okta/token"},
            },
        )
        assert env["AUTH_PROVIDER"] == "okta"
        assert env["OAUTH_CLIENT_ID"] == "cid"
        assert "GATEWAY_AUTH_MODE" not in env


# ---------------------------------------------------------------------------
# Generated agent code — BOTH duplicated generator bodies
# ---------------------------------------------------------------------------


def _gateway_bodies() -> dict[str, str]:
    """The gateway MCP block is duplicated across two generators. A fix applied
    to only one of them is the exact failure mode this guards: an agent with
    memory wired would silently keep the OAuth2-only transport."""
    creds = {"url": f"{BASE}/mcp/", "client_id": "", "client_secret": "", "token_endpoint": "", "scope": ""}
    return {
        "strands_gateway": _generate_strands_gateway("You are helpful.", "eu.anthropic.claude-sonnet-5", creds),
        "memory_agent": _generate_memory_agent(
            "You are helpful.", "eu.anthropic.claude-sonnet-5", "eu-central-1", has_gateway=True, creds=creds
        ),
    }


class TestGeneratedAgent:
    @pytest.mark.parametrize("which", ["strands_gateway", "memory_agent"])
    def test_the_static_bearer_path_is_emitted(self, which):
        code = _gateway_bodies()[which]
        assert 'GATEWAY_AUTH_MODE = os.environ.get("GATEWAY_AUTH_MODE", "oauth2")' in code
        assert 'GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")' in code
        assert 'GATEWAY_MCP_SERVERS = os.environ.get("GATEWAY_MCP_SERVERS", "")' in code
        assert 'if GATEWAY_AUTH_MODE == "static_bearer"' in code
        assert '"x-litellm-api-key"' in code
        assert '"x-mcp-servers"' in code

    @pytest.mark.parametrize("which", ["strands_gateway", "memory_agent"])
    def test_the_virtual_key_is_sent_with_a_bearer_prefix(self, which):
        """The deployed agent's header value must match what LiteLLM's /mcp/
        endpoint accepts. A bare key probes fine over REST and then fails at
        invoke time, which is the worst shape of this bug: a green deploy and a
        toolless agent. Guarded in both duplicated generator bodies."""
        code = _gateway_bodies()[which]
        assert 'token if token.startswith("Bearer ") else f"Bearer {token}"' in code
        assert '{"x-litellm-api-key": _lkey}' in code

    @pytest.mark.parametrize("which", ["strands_gateway", "memory_agent"])
    def test_oauth2_remains_the_default_so_agentcore_is_unaffected(self, which):
        code = _gateway_bodies()[which]
        assert 'headers = {"Authorization": f"Bearer {token}"} if token else {}' in code
        assert "COGNITO_TOKEN_ENDPOINT" in code

    @pytest.mark.parametrize("which", ["strands_gateway", "memory_agent"])
    def test_the_emitted_code_is_valid_python(self, which):
        """Both bodies are f-strings with hand-doubled braces. A single missed
        brace produces code that only fails once it is running in AgentCore."""
        compile(_gateway_bodies()[which], f"<{which}>", "exec")

    def test_an_all_empty_cognito_creds_dict_still_emits_the_gateway_block(self):
        """A LiteLLM gateway_result carries a gateway_url and NO Cognito fields.
        ``_extract_gateway_credentials`` returns a dict that is populated only at
        ``url``; if the generator gated on client_id the block would vanish and
        the agent would come up with no tools."""
        from app.services.code_generator import _extract_gateway_credentials

        creds = _extract_gateway_credentials(
            {"gateway_url": f"{BASE}/mcp/", "client_info": {"provider": "litellm", "api_key_ref": "arn:secret:k"}}
        )
        assert creds["url"] == f"{BASE}/mcp/"
        assert creds["client_id"] == ""
        assert creds, "creds must stay truthy — the generators gate on `has_gateway and creds`"
        code = _generate_memory_agent("p", "m", "eu-central-1", has_gateway=True, creds=creds)
        assert "MCPClient" in code
        assert 'if GATEWAY_AUTH_MODE == "static_bearer"' in code


class TestTheTeardownManifest:
    """What the deploy writes into ``created_resources[]``, and what teardown does
    with it. A LiteLLM gateway is the CUSTOMER's proxy: we must record enough to
    show what the deploy pointed at, and must never record something that makes
    teardown try to delete infrastructure we do not own."""

    @staticmethod
    def _record(gateway_result: dict) -> list[dict]:
        from app.step_handlers.gateway_step import _record_gateway_resources

        recorded: list[dict] = []

        class _Store:
            def record_resource(self, _deployment_id, resource):
                recorded.append(resource)

        _record_gateway_resources(_Store(), "d-1", "eu-central-1", gateway_result)
        return recorded

    def _litellm_result(self) -> dict:
        return {
            "gateway_id": None,
            "gateway_name": "my-agent-litellm",
            "gateway_provider": "litellm",
            "litellm_base_url": BASE,
            "client_info": {"provider": "litellm", "api_key_ref": "arn:aws:secretsmanager:::secret:k"},
            "connector_secret_arns": ["arn:aws:secretsmanager:::secret:k"],
        }

    def test_the_proxy_is_recorded_informationally(self):
        rows = self._record(self._litellm_result())
        row = next(r for r in rows if r["type"] == "litellm_gateway")
        assert row["id"] == BASE
        assert row["region"] == "eu-central-1"

    def test_no_phantom_agentcore_role_is_recorded(self):
        """The AgentCoreGateway-<name> execution role only exists on the AgentCore
        path. Recording it for LiteLLM would make every teardown issue a delete
        for an IAM role that was never created."""
        rows = self._record(self._litellm_result())
        assert not [r for r in rows if r["type"] == "iam_role"]
        assert not [r for r in rows if r["type"] == "cognito_user_pool"]
        assert not [r for r in rows if r["type"] == "gateway"]

    def test_the_virtual_key_secret_is_still_recorded(self):
        """The one AWS resource the deploy really did create must not be lost —
        otherwise the LiteLLM path leaks a secret on every teardown."""
        rows = self._record(self._litellm_result())
        assert [r["id"] for r in rows if r["type"] == "secret"] == ["arn:aws:secretsmanager:::secret:k"]

    def test_the_agentcore_path_still_records_its_role(self):
        """Regression guard for the branch above: nothing about the default
        provider's manifest may change."""
        rows = self._record({"gateway_id": "gw-123", "gateway_name": "my-agent"})
        assert {"type": "gateway", "id": "gw-123", "region": "eu-central-1"} in rows
        assert {"type": "iam_role", "name": "AgentCoreGateway-my-agent", "region": "eu-central-1"} in rows

    def test_teardown_treats_the_row_as_a_deliberate_no_op(self):
        """Not merely "unknown type falls through" — an explicit arm, so the row
        is self-documenting and cannot be mistaken for a missing deleter."""
        from app.deployment_handler import _delete_managed_resource

        line = _delete_managed_resource({"type": "litellm_gateway", "id": BASE}, "eu-central-1")
        assert "external" in line and "nothing to delete" in line

    def test_the_row_is_ordered_with_the_gateway_band(self):
        import inspect

        from app import deployment_handler

        src = inspect.getsource(deployment_handler)
        assert '"litellm_gateway": 2,' in src, "must share the gateway band for symmetry"
