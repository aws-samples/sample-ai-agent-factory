"""The model-provider API key and a LiteLLM virtual key travel as REFERENCES.

Both used to be resolved during the deploy and injected into the AgentCore runtime's
environment as ``PROVIDER_API_KEY`` / ``GATEWAY_API_KEY``. That is not a place a secret
can live: ``GetAgentRuntime`` returns a runtime's environment variables verbatim to any
principal holding that one describe call, and every Task in the deployment state machine
re-emits the whole event into the execution history, where it is readable for 90 days.
The reference travels instead and the generated agent dereferences it inside the
container at the moment of use (``_provider_api_key`` / ``_resolve_gateway_key`` in
``services/code_generator.py``).

Three failure modes this pins, all of which leave the deploy GREEN:

1. **The plaintext comes back.** Nothing goes red; the key is simply readable again.
2. **The reference is injected but the read is not granted.** The agent raises
   ``AccessDeniedException`` on its first model call. So the injection and the grant are
   asserted to be the SAME decision (``runtime_key_grant_targets``), not two agreeing
   ones.
3. **The generated code calls a resolver it never defined.** ``NameError`` on first
   invoke. The emitted-source check below is over the real dependency — text that calls
   ``_provider_api_key(`` — because the interesting case is a *Bedrock parent with one
   non-Bedrock sub-agent*, which a per-provider table would have missed.

ARCC ``cnt_dAiE0OyXKvfeow``: prefer a scoped role plus a Secrets Manager read at runtime
over an environment variable. ``cnt_n8LpZcqYi2t3I2``: never hold a secret in an env var.
``cnt_LuG2TKuO0errRp``: a secret grant belongs only where the principal needs it.
"""

import json
from pathlib import Path

import pytest
from app.services import per_agent_identity
from app.services.runtime_deployer import (
    canvas_model_providers,
    needs_provider_api_key,
    runtime_key_grant_targets,
)

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROVIDER_REF = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-provider/openai-AbCdEf"
GATEWAY_REF = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/u1/deadbeef-AbCdEf"
# Obviously fake. No real key appears in this repo, including in tests.
FAKE_KEY = "sk-fake-not-a-real-key-0000"

LITELLM_GATEWAY = {
    "gateway_url": "https://proxy.example.com/mcp",
    "client_info": {"provider": "litellm", "api_key_ref": GATEWAY_REF},
}
COGNITO_GATEWAY = {
    "gateway_url": "https://gw.example.com/mcp",
    "client_info": {"provider": "cognito", "client_id": "abc", "user_pool_id": "us-east-1_AbC123"},
}


def _sids(policy: dict) -> set[str]:
    return {st.get("Sid") for st in policy["Statement"]}


def _statement(policy: dict, sid: str) -> dict:
    matches = [st for st in policy["Statement"] if st.get("Sid") == sid]
    assert len(matches) == 1, f"expected exactly one {sid} statement, found {len(matches)}"
    return matches[0]


class _FakeStore:
    def __init__(self):
        self.resources: list[dict] = []

    def update_step(self, *a, **kw):
        pass

    def record_resource(self, deployment_id, resource):
        self.resources.append(resource)


# ---------------------------------------------------------------------------
# The shared decision
# ---------------------------------------------------------------------------


class TestCanvasModelProviders:
    def test_a_bedrock_only_canvas_needs_no_key(self):
        assert canvas_model_providers({"model_provider": "bedrock"}) == ["bedrock"]
        assert needs_provider_api_key({"model_provider": "bedrock"}) is False

    def test_an_absent_provider_defaults_to_bedrock(self):
        """``model_provider`` defaults to bedrock on the model and is absent from a raw
        dict built by an older caller. Defaulting the other way would grant a
        secretsmanager read to every Bedrock agent in the account."""
        assert needs_provider_api_key({}) is False
        assert needs_provider_api_key({"name": "a", "model": {"modelId": "x"}}) is False

    def test_a_bedrock_parent_with_a_non_bedrock_sub_agent_needs_a_key(self):
        """The regression this function exists for. ``code_generator`` builds one model
        per sub-agent from ``multiAgentConfig.agents[*].modelProvider``, so a Bedrock
        parent can still instantiate an OpenAIModel. The old gate read only the parent
        and handed that canvas no key at all — a green deploy whose sub-agent's first
        model call 401s, with nothing in the deploy log to point at."""
        config = {
            "model_provider": "bedrock",
            "provider_api_key_ref": PROVIDER_REF,
            "multi_agent_config": {
                "agents": [
                    {"agentId": "researcher", "modelProvider": "openai"},
                    {"agentId": "writer"},  # inherits bedrock
                ]
            },
        }
        assert canvas_model_providers(config) == ["bedrock", "openai", "bedrock"]
        assert needs_provider_api_key(config) is True
        assert runtime_key_grant_targets(config, None) == (PROVIDER_REF, None)

    def test_the_camel_case_spelling_is_accepted_too(self):
        """The config crosses a JSON boundary into the SFN event, and the API models are
        camelCase. ``iam_step`` reads the RAW dict while ``runtime_configure_step`` reads
        a validated ``RuntimeConfig`` — so the helper sees both spellings depending on
        which side calls it, and reading only one silently grants nothing."""
        config = {
            "modelProvider": "openai",
            "providerApiKeyRef": PROVIDER_REF,
            "multiAgentConfig": {"agents": [{"agentId": "a", "modelProvider": "anthropic"}]},
        }
        assert canvas_model_providers(config) == ["openai", "anthropic"]
        assert runtime_key_grant_targets(config, None) == (PROVIDER_REF, None)

    def test_a_provider_on_the_model_block_is_read(self):
        """Some callers put the provider on ``model`` rather than at the top level."""
        assert needs_provider_api_key({"model": {"provider": "mistral", "modelId": "m"}}) is True

    @pytest.mark.parametrize("provider", ["bedrock", "sagemaker", "ollama"])
    def test_iam_or_local_providers_need_no_api_key(self, provider):
        assert needs_provider_api_key({"model_provider": provider}) is False


class TestLiveDeployRejectsMissingProviderCredentials:
    @staticmethod
    def _request(provider: str, ref: str | None = None, **overrides):
        from app.models.deployment_models import DeployRequest

        payload = {
            "nodeId": "runtime-node",
            "config": {
                "name": "agent",
                "model": {"modelId": ("us.anthropic.claude-sonnet-5" if provider == "bedrock" else "provider-model")},
                "modelProvider": provider,
                "providerApiKeyRef": ref,
            },
        }
        payload.update(overrides)
        return DeployRequest.model_validate(payload)

    def test_openai_without_a_ref_fails_before_deployment_state_exists(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        from app.deployment_handler import _reject_missing_provider_credential
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="providerApiKeyRef") as exc:
            _reject_missing_provider_credential(self._request("openai"))
        assert exc.value.status_code == 400

    def test_a_non_bedrock_sub_agent_also_requires_the_shared_ref(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        from app.deployment_handler import _reject_missing_provider_credential
        from fastapi import HTTPException

        request = self._request("bedrock")
        request.config.multi_agent_config = {
            "agents": [
                {
                    "agentId": "researcher",
                    "modelProvider": "openai",
                    "modelId": "gpt-4o",
                }
            ]
        }
        with pytest.raises(HTTPException):
            _reject_missing_provider_credential(request)

    @pytest.mark.parametrize("provider", ["bedrock", "sagemaker", "ollama"])
    def test_keyless_provider_is_allowed(self, provider, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        from app.deployment_handler import _reject_missing_provider_credential

        _reject_missing_provider_credential(self._request(provider))

    def test_harness_mode_is_not_forced_through_runtime_provider_auth(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        from app.deployment_handler import _reject_missing_provider_credential

        _reject_missing_provider_credential(self._request("openai", deploymentMode="harness"))

    def test_owned_source_ref_allows_the_live_runtime_to_continue(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        from app.deployment_handler import _reject_missing_provider_credential

        _reject_missing_provider_credential(self._request("openai", PROVIDER_REF))


class TestRuntimeKeyGrantTargets:
    def test_a_non_bedrock_canvas_returns_its_provider_ref(self):
        config = {"model_provider": "openai", "provider_api_key_ref": PROVIDER_REF}
        assert runtime_key_grant_targets(config, None) == (PROVIDER_REF, None)

    def test_a_bedrock_canvas_is_granted_nothing_even_if_a_ref_is_present(self):
        """A leftover ref on a canvas switched back to Bedrock must not become a grant.
        The env var is not injected for Bedrock either, so a grant here would be a live
        secretsmanager read that nothing uses — surplus authority on every agent."""
        config = {"model_provider": "bedrock", "provider_api_key_ref": PROVIDER_REF}
        assert runtime_key_grant_targets(config, None) == (None, None)

    def test_a_litellm_gateway_returns_its_virtual_key_ref(self):
        assert runtime_key_grant_targets({}, LITELLM_GATEWAY) == (None, GATEWAY_REF)

    def test_a_cognito_gateway_returns_no_virtual_key(self):
        """An AgentCore gateway authenticates by OAuth client-credentials; there is no
        virtual key, and ``client_secret_grant_targets`` covers that credential."""
        assert runtime_key_grant_targets({}, COGNITO_GATEWAY) == (None, None)

    @pytest.mark.parametrize("gateway_result", [None, {}, {"client_info": {}}])
    def test_an_agent_with_no_gateway_is_granted_nothing(self, gateway_result):
        assert runtime_key_grant_targets({}, gateway_result) == (None, None)

    def test_a_litellm_gateway_with_no_ref_is_granted_nothing(self):
        """The deployer warns and the agent has no key; a grant on ``None`` would be an
        IAM statement with an empty resource, which put_role_policy rejects."""
        assert runtime_key_grant_targets({}, {"client_info": {"provider": "litellm"}}) == (None, None)

    def test_both_can_be_present_at_once(self):
        """Unlike the pool/secret pair, these two are NOT mutually exclusive: an OpenAI
        agent behind a LiteLLM MCP proxy legitimately reads both secrets."""
        config = {"model_provider": "openai", "provider_api_key_ref": PROVIDER_REF}
        assert runtime_key_grant_targets(config, LITELLM_GATEWAY) == (PROVIDER_REF, GATEWAY_REF)


# ---------------------------------------------------------------------------
# The runtime environment
# ---------------------------------------------------------------------------


def _configure_env(monkeypatch, config: dict, gateway_result: dict | None = None) -> dict:
    """Drive the real ``runtime_configure_step.handler`` and return the env vars it would
    put on the AgentCore runtime. Asserting through the handler rather than a helper is
    the point: the plaintext injection that was removed lived in the handler."""
    from app.step_handlers import runtime_configure_step as rcs

    monkeypatch.setattr(rcs, "_get_deployment_store", lambda: _FakeStore())
    monkeypatch.setattr(rcs.step_clients, "client", lambda event, svc, **kw: object())
    monkeypatch.setattr(rcs, "sanitize_runtime_name", lambda n: "agent_x")
    monkeypatch.setattr(rcs, "build_otel_env_vars", lambda *a, **kw: {})
    monkeypatch.setattr(rcs, "get_platform_observability_defaults", lambda: {})
    # Runtime-log governance is covered independently; this helper measures only
    # the secret references passed into CreateAgentRuntime.
    monkeypatch.setattr(rcs, "govern_default_runtime_log_group", lambda *_a, **_kw: None)
    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)

    captured: dict = {}

    def _fake_create(**kwargs):
        captured["env_vars"] = kwargs.get("env_vars") or {}
        return {"runtime_id": "agent_x-123", "runtime_arn": "arn:runtime"}

    monkeypatch.setattr(rcs, "create_agent_runtime", _fake_create)
    rcs.handler(
        {
            "deployment_id": "d1",
            "config": config,
            "role_arn": "arn:role",
            "s3_bucket": "b",
            "s3_key": "k",
            "gateway_result": gateway_result or {},
        },
        None,
    )
    return captured["env_vars"]


def _base_config(**overrides) -> dict:
    config = {
        "name": "agent",
        "entrypoint": "agent.py",
        "model": {"modelId": "gpt-4o"},
        "modelProvider": "openai",
        "providerApiKeyRef": PROVIDER_REF,
    }
    config.update(overrides)
    return config


class TestTheRuntimeEnvironmentCarriesOnlyReferences:
    def test_the_provider_reference_replaces_the_plaintext_key(self, monkeypatch):
        env = _configure_env(monkeypatch, _base_config())
        assert env["PROVIDER_API_KEY_SECRET_ARN"] == PROVIDER_REF
        assert "PROVIDER_API_KEY" not in env, (
            "the plaintext model-provider key is back in the runtime environment. "
            "GetAgentRuntime returns these verbatim."
        )

    def test_a_bedrock_agent_gets_neither(self, monkeypatch):
        env = _configure_env(
            monkeypatch,
            {
                "name": "agent",
                "entrypoint": "agent.py",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "modelProvider": "bedrock",
            },
        )
        assert "PROVIDER_API_KEY_SECRET_ARN" not in env
        assert "PROVIDER_API_KEY" not in env

    def test_a_bedrock_parent_with_a_non_bedrock_sub_agent_gets_the_reference(self, monkeypatch):
        env = _configure_env(
            monkeypatch,
            {
                "name": "agent",
                "entrypoint": "agent.py",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "modelProvider": "bedrock",
                "providerApiKeyRef": PROVIDER_REF,
                "multiAgentPattern": "graph",
                "multiAgentConfig": {
                    "agents": [{"agentId": "a", "modelProvider": "openai", "modelId": "gpt-4o"}],
                    "edges": [],
                },
            },
        )
        assert env["PROVIDER_API_KEY_SECRET_ARN"] == PROVIDER_REF, (
            "a sub-agent on a non-Bedrock provider was handed no key. The generated code "
            "instantiates an OpenAIModel for it and every call 401s."
        )

    def test_the_litellm_virtual_key_reference_replaces_the_plaintext_key(self, monkeypatch):
        env = _configure_env(monkeypatch, _base_config(), LITELLM_GATEWAY)
        assert env["GATEWAY_API_KEY_SECRET_ARN"] == GATEWAY_REF
        assert "GATEWAY_API_KEY" not in env, "the plaintext LiteLLM virtual key is back in the runtime environment"
        assert env["GATEWAY_AUTH_MODE"] == "static_bearer"

    def test_no_environment_value_looks_like_a_key_at_all(self, monkeypatch):
        """A blunt backstop for the whole class of regression, not just the two names
        above: a future third credential injected under a new name passes every
        name-specific assertion in this file."""
        env = _configure_env(
            monkeypatch,
            _base_config(providerApiKeyRef=PROVIDER_REF),
            LITELLM_GATEWAY,
        )
        for name, value in env.items():
            text = str(value)
            assert FAKE_KEY not in text, f"{name} carries a raw key"
            assert not text.startswith("sk-"), f"{name} looks like a bearer/API key: {name}"
            if "secretsmanager" in text:
                assert text.startswith("arn:aws:secretsmanager:"), (
                    f"{name} mentions Secrets Manager but is not an ARN — a resolved value?"
                )


# ---------------------------------------------------------------------------
# The injection and the grant are one decision
# ---------------------------------------------------------------------------


def _per_agent_policy(monkeypatch, config: dict, gateway_result: dict | None = None) -> dict:
    """Drive the real ``iam_step.handler`` on the per-agent path and return the inline
    policy it attached."""
    import time

    from app.step_handlers import iam_step

    monkeypatch.setattr(iam_step, "_get_deployment_store", lambda: _FakeStore())
    monkeypatch.setattr(iam_step, "sanitize_runtime_name", lambda n: "agent_x")
    monkeypatch.setattr(iam_step, "get_platform_observability_defaults", lambda: {})
    monkeypatch.setattr(iam_step.step_clients, "account_id_for_event", lambda event: ACCOUNT)
    # The per-agent path sleeps 15s to let IAM propagate before the runtime is created.
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "artifacts-bucket")

    from unittest.mock import MagicMock

    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
    iam.get_role.return_value = {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreRuntime-agent_x"}}
    monkeypatch.setattr(iam_step.step_clients, "client", lambda event, svc, **kw: iam)

    iam_step.handler(
        {
            "deployment_id": "d1",
            "config": config,
            "connected_tools": ["gateway"],
            "identity_config": {"mode": "per_agent"},
            "agentcore_runtime_name": "agent_x",
            "gateway_result": gateway_result or {},
        },
        None,
    )
    calls = iam.put_role_policy.call_args_list
    assert calls, "iam_step attached no inline policy"
    return json.loads(calls[-1].kwargs["PolicyDocument"])


# Every env var that hands the agent a secret ARN, paired with the Sid that must grant
# the read. Adding a third reference without adding a row here is what the pairing test
# below is designed to catch.
_REFERENCE_TO_SID = {
    "PROVIDER_API_KEY_SECRET_ARN": "ModelProviderApiKey",
    "GATEWAY_API_KEY_SECRET_ARN": "LiteLLMGatewayVirtualKey",
    "OAUTH_CLIENT_SECRET_REF": "ExternalIdpClientSecret",
}


@pytest.mark.parametrize(
    ("config", "gateway_result"),
    [
        (_base_config(), None),
        (_base_config(), LITELLM_GATEWAY),
        (_base_config(modelProvider="bedrock", model={"modelId": "us.anthropic.claude-sonnet-5"}), None),
        (_base_config(), COGNITO_GATEWAY),
    ],
    ids=["openai", "openai+litellm", "bedrock", "openai+cognito"],
)
def test_every_injected_reference_is_granted_and_nothing_more(monkeypatch, config, gateway_result):
    """The pairing property, over the two handlers that actually run in the state machine.

    Injected-but-not-granted is an AccessDeniedException on the agent's first model call,
    after a deploy that reported SUCCESS. Granted-but-not-injected is surplus authority on
    a role that outlives the deploy. Both directions are asserted because the two steps
    used to decide independently.
    """
    env = _configure_env(monkeypatch, config, gateway_result)
    policy = _per_agent_policy(monkeypatch, config, gateway_result)
    sids = _sids(policy)

    for env_name, sid in _REFERENCE_TO_SID.items():
        ref = env.get(env_name)
        if ref:
            assert sid in sids, f"{env_name} was injected but {sid} grants no read for it"
            resource = _statement(policy, sid)["Resource"]
            assert ref in resource or f"{ref}-*" in resource, (
                f"{sid} grants {resource}, which does not cover the injected {ref}"
            )
        else:
            assert sid not in sids, f"{sid} grants a read for {env_name}, which is not injected"


class TestPerAgentPolicyEmitsTheGrants:
    def test_the_provider_key_gets_getsecretvalue_on_exactly_one_secret(self):
        policy = per_agent_identity.build_scoped_runtime_policy([], provider_key_secret_arn=PROVIDER_REF)
        statement = _statement(policy, "ModelProviderApiKey")
        assert statement["Action"] == ["secretsmanager:GetSecretValue"]
        assert statement["Resource"] == [PROVIDER_REF]
        assert statement["Effect"] == "Allow"

    def test_the_gateway_key_gets_its_own_statement(self):
        """One statement per secret rather than one merged statement with two resources:
        per ARCC cnt_LuG2TKuO0errRp the grant is auditable per credential, and the two
        appear independently of each other."""
        policy = per_agent_identity.build_scoped_runtime_policy(["gateway"], gateway_key_secret_arn=GATEWAY_REF)
        assert _statement(policy, "LiteLLMGatewayVirtualKey")["Resource"] == [GATEWAY_REF]
        assert "ModelProviderApiKey" not in _sids(policy)

    def test_neither_statement_appears_when_no_arn_is_supplied(self):
        sids = _sids(per_agent_identity.build_scoped_runtime_policy(["gateway", "memory"]))
        assert "ModelProviderApiKey" not in sids
        assert "LiteLLMGatewayVirtualKey" not in sids

    def test_the_grants_never_widen_to_a_wildcard_resource(self):
        """These two have no ``"*"`` fallback. A wildcard would be read access to every
        secret in the account, including every other tenant's connector credential."""
        policy = per_agent_identity.build_scoped_runtime_policy(
            ["gateway"],
            provider_key_secret_arn=PROVIDER_REF,
            gateway_key_secret_arn=GATEWAY_REF,
        )
        for sid in ("ModelProviderApiKey", "LiteLLMGatewayVirtualKey"):
            for resource in _statement(policy, sid)["Resource"]:
                assert resource != "*"
                assert not resource.endswith(":secret:*")
                assert not resource.endswith("/*")


class TestLegacyRoleEmitsTheGrants:
    """The legacy per-deploy path must agree with the per-agent path. It is reachable on
    any stack deployed before ``SHARED_RUNTIME_ROLE_ARN`` existed, and a divergence there
    fails the same silent way."""

    def _policy_for(self, **kwargs) -> dict:
        from unittest.mock import MagicMock

        from app.services import runtime_deployer

        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
        iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreRuntime-t"}}
        runtime_deployer.create_runtime_iam_role(
            iam_client=iam,
            role_name="AgentCoreRuntime-t",
            account_id=ACCOUNT,
            region=REGION,
            connected_tools=["gateway"],
            **kwargs,
        )
        merged: dict = {"Version": "2012-10-17", "Statement": []}
        for call in iam.put_role_policy.call_args_list:
            merged["Statement"].extend(json.loads(call.kwargs["PolicyDocument"])["Statement"])
        assert merged["Statement"], "create_runtime_iam_role attached no inline policy"
        return merged

    def test_the_two_grants_appear_when_the_arns_are_supplied(self):
        policy = self._policy_for(provider_key_secret_arn=PROVIDER_REF, gateway_key_secret_arn=GATEWAY_REF)
        assert _statement(policy, "ModelProviderApiKey")["Resource"] == [PROVIDER_REF]
        assert _statement(policy, "LiteLLMGatewayVirtualKey")["Resource"] == [GATEWAY_REF]

    def test_neither_grant_appears_by_default(self):
        sids = _sids(self._policy_for())
        assert "ModelProviderApiKey" not in sids
        assert "LiteLLMGatewayVirtualKey" not in sids


# ---------------------------------------------------------------------------
# The generated agent
# ---------------------------------------------------------------------------

_KEYED_PROVIDERS = ["openai", "anthropic", "gemini", "litellm", "mistral", "groq", "deepseek", "together", "writer"]
_KEYLESS_PROVIDERS = ["bedrock", "ollama", "sagemaker"]


def _compile(source: str, label: str) -> None:
    """Compile the generated module. Compiling the GENERATOR proves nothing: the defect
    class here is a broken f-string substitution or a doubled brace, which only shows up
    in the emitted text."""
    try:
        compile(source, f"<generated:{label}>", "exec")
    except SyntaxError as e:  # pragma: no cover - only on a real regression
        pytest.fail(f"generated {label} does not compile: {e}\n{source[:2000]}")


def _assert_resolver_pairing(source: str, label: str) -> None:
    defines = "def _provider_api_key(" in source
    calls = "_provider_api_key(" in source.replace("def _provider_api_key(", "")
    assert defines == calls, (
        f"{label}: the generated agent {'calls' if calls else 'defines'} _provider_api_key "
        f"but does not {'define' if calls else 'call'} it. A call without a definition is "
        "a NameError on first invoke, after a deploy that reported SUCCESS."
    )
    if defines:
        assert "PROVIDER_API_KEY_SECRET_ARN" in source
    _compile(source, label)


@pytest.mark.parametrize("provider", _KEYED_PROVIDERS)
def test_a_keyed_provider_defines_and_calls_the_resolver(provider):
    from app.services.code_generator import _generate_strands_default

    source = _generate_strands_default("You are helpful.", "some-model", REGION, provider)
    assert "def _provider_api_key(" in source, f"{provider} reads no key at all"
    _assert_resolver_pairing(source, f"single/{provider}")


@pytest.mark.parametrize("provider", _KEYLESS_PROVIDERS)
def test_a_keyless_provider_emits_no_resolver(provider):
    """Bedrock authenticates with the runtime's IAM role. Emitting the resolver anyway
    would be dead code that imports boto3's Secrets Manager client for nothing, and would
    make the emitted-source check above unable to tell the two cases apart."""
    from app.services.code_generator import _generate_strands_default

    source = _generate_strands_default("You are helpful.", "some-model", REGION, provider)
    assert "_provider_api_key(" not in source
    _compile(source, f"single/{provider}")


@pytest.mark.parametrize("pattern", ["graph", "swarm", "workflow"])
@pytest.mark.parametrize("parent", ["bedrock", "openai"])
def test_every_multi_agent_template_pairs_the_resolver(pattern, parent):
    """The case a per-provider table would have got wrong: ``parent=bedrock`` with one
    OpenAI sub-agent. The parent needs no key, the sub-agent does, and the resolver is
    emitted because the EMITTED TEXT calls it — not because a list said bedrock might."""
    from app.services import code_generator

    generate = {
        "graph": code_generator._generate_graph_agent,
        "swarm": code_generator._generate_swarm_agent,
        "workflow": code_generator._generate_workflow_agent,
    }[pattern]
    multi = {
        "agents": [
            {"agentId": "a", "modelProvider": "openai", "modelId": "gpt-4o", "systemPrompt": "A"},
            {"agentId": "b", "modelId": "some-model", "systemPrompt": "B"},
        ],
        "edges": [{"source": "a", "target": "b"}],
        "entryPoint": "a",
    }
    source = generate("You are helpful.", "some-model", REGION, parent, multi)
    assert "def _provider_api_key(" in source, (
        f"{pattern}/{parent}: a non-Bedrock sub-agent is present and the resolver was not emitted"
    )
    _assert_resolver_pairing(source, f"{pattern}/{parent}")


@pytest.mark.parametrize("pattern", ["graph", "swarm", "workflow"])
def test_an_all_bedrock_multi_agent_template_emits_no_resolver(pattern):
    from app.services import code_generator

    generate = {
        "graph": code_generator._generate_graph_agent,
        "swarm": code_generator._generate_swarm_agent,
        "workflow": code_generator._generate_workflow_agent,
    }[pattern]
    multi = {
        "agents": [
            {"agentId": "a", "modelId": "some-model", "systemPrompt": "A"},
            {"agentId": "b", "modelId": "some-model", "systemPrompt": "B"},
        ],
        "edges": [{"source": "a", "target": "b"}],
        "entryPoint": "a",
    }
    source = generate("You are helpful.", "some-model", REGION, "bedrock", multi)
    assert "_provider_api_key(" not in source
    _compile(source, f"{pattern}/all-bedrock")


# ---------------------------------------------------------------------------
# The OTHER module-emitting site
# ---------------------------------------------------------------------------
#
# ``_get_model_init_code``'s output is embedded into a generated module by exactly two
# functions: ``code_generator``'s template generators (above) and
# ``deployment.generate_unified_agent_code``. The second one builds its OWN module
# template and therefore has to emit the resolver itself; it did not, which made every
# non-Bedrock agent on that path a module that NameErrors at container import. The tests
# above all passed while that was true, because they only ever drove the first site.
#
# Reached by ``generate_agent_code`` / ``generate_gateway_agent_code`` in the same module
# and by ``WorkflowExecutor.deploy``.


def _unified_config(provider: str, **overrides):
    from app.models.components import RuntimeConfiguration

    return RuntimeConfiguration(
        name="unified",
        model={"provider": provider, "model_id": "some-model"},
        system_prompt="You are helpful.",
        model_provider=provider,
        **overrides,
    )


@pytest.mark.parametrize("provider", _KEYED_PROVIDERS)
def test_the_unified_generator_defines_the_resolver_it_calls(provider):
    from app.services.deployment import generate_unified_agent_code

    source = generate_unified_agent_code(_unified_config(provider), connected_tools=[], region=REGION)
    assert "_provider_api_key(" in source, f"{provider} reads no key at all on the unified path"
    _assert_resolver_pairing(source, f"unified/{provider}")


@pytest.mark.parametrize("provider", _KEYLESS_PROVIDERS)
def test_the_unified_generator_emits_no_resolver_for_a_keyless_provider(provider):
    from app.services.deployment import generate_unified_agent_code

    source = generate_unified_agent_code(_unified_config(provider), connected_tools=[], region=REGION)
    assert "_provider_api_key(" not in source
    _compile(source, f"unified/{provider}")


@pytest.mark.parametrize("tools", [[], ["memory"], ["code_interpreter"], ["browser"], ["code_interpreter", "browser"]])
def test_the_unified_generator_pairs_the_resolver_for_every_component_set(tools):
    """The resolver is emitted into the same ``helpers`` list the component helpers go
    into, so a component combination is the way an ordering mistake would surface — as a
    module where the resolver is defined after the function that uses it, or not at all."""
    from app.services.deployment import generate_unified_agent_code

    source = generate_unified_agent_code(
        _unified_config("openai"), connected_tools=tools, memory_id="mem-1", region=REGION
    )
    _assert_resolver_pairing(source, f"unified/openai/{'+'.join(tools) or 'bare'}")


def test_the_unified_generator_defines_the_resolver_before_load_model_uses_it():
    """Python resolves the name at call time, so definition order does not actually
    matter here — but ``load_model`` is called from module-level lazy init on some
    templates, and a resolver defined after that line would be a NameError at import
    even though the pairing check above passes."""
    from app.services.deployment import generate_unified_agent_code

    source = generate_unified_agent_code(_unified_config("openai"), connected_tools=[], region=REGION)
    assert source.index("def _provider_api_key(") < source.index("def load_model(")


def test_the_unified_generator_carries_no_plaintext_key_env_var():
    """``PROVIDER_API_KEY`` survives only inside the resolver's own fallback, for a local
    run. The generated module must not read it anywhere else, or the reference change is
    cosmetic on this path."""
    from app.services.code_generator import _PROVIDER_KEY_HELPER
    from app.services.deployment import generate_unified_agent_code

    source = generate_unified_agent_code(_unified_config("openai"), connected_tools=[], region=REGION)
    assert source.count('os.environ.get("PROVIDER_API_KEY"') == _PROVIDER_KEY_HELPER.count(
        'os.environ.get("PROVIDER_API_KEY"'
    )


# Every function that touches ``_get_model_init_code`` and does NOT itself emit the
# resolver, mapped to the function that is responsible for emitting it instead. A new
# entry here is a decision someone has to write down; a new call site that is in neither
# this map nor the emitting set fails the test below, because it is the shape of the
# defect this whole file exists for.
_DELEGATES_THE_RESOLVER = {
    # Reads only the import line to build the generated module's import block. The model
    # init text never reaches the emitted module from here.
    "_collect_multi_agent_imports": None,
    # A thin adapter: unwraps the provider enum and returns (import, init) to its one
    # caller, which is the function that builds the module.
    "_get_model_code": "generate_unified_agent_code",
    # Returns ONE sub-agent's `model = …` line to whichever multi-agent generator is
    # building the module; all three of them emit the resolver once for the whole file,
    # which is the only correct place for it (a resolver emitted per sub-agent would be
    # a duplicate def).
    "_sub_agent_model_init": ("_generate_graph_agent", "_generate_swarm_agent", "_generate_workflow_agent"),
}


def test_every_function_that_embeds_model_init_code_emits_the_resolver():
    """A guard on the guard. ``generate_unified_agent_code`` embedded the model init into
    its own module template and never emitted the resolver, and every test above passed
    while that was true — they only ever drove ``code_generator``. So the coverage itself
    has to be checked, not assumed.

    Keyed off the real dependency: a function that handles ``_get_model_init_code``'s
    output either emits ``_provider_key_helper_for(...)`` alongside it or is recorded above
    as delegating that to a named function which does.
    """
    import ast
    import inspect

    from app.services import code_generator, deployment

    unresolved = []
    for module in (code_generator, deployment):
        source = inspect.getsource(module)
        lines = source.splitlines()
        by_name = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                by_name[node.name] = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        for name, body in by_name.items():
            if name == "_get_model_init_code" or "_get_model_init_code(" not in body:
                continue
            if "_provider_key_helper_for(" in body:
                continue
            if name not in _DELEGATES_THE_RESOLVER:
                unresolved.append(f"{module.__name__}.{name}")
                continue
            owners = _DELEGATES_THE_RESOLVER[name]
            if owners is None:
                continue
            owners = (owners,) if isinstance(owners, str) else owners
            assert owners, f"{name} is recorded as delegating to nothing at all"
            for owner in owners:
                assert owner in by_name, f"{name} delegates to {owner}, which no longer exists in {module.__name__}"
                assert "_provider_key_helper_for(" in by_name[owner], (
                    f"{name} delegates emitting the resolver to {owner}, and {owner} no "
                    "longer emits it — every non-Bedrock agent on that path NameErrors at "
                    "container import"
                )

    assert not unresolved, (
        f"{unresolved} embed _get_model_init_code's output without emitting "
        "_provider_key_helper_for(...) output. The generated module will call "
        "_provider_api_key() with no definition: a deploy that reports SUCCESS and a "
        "container that NameErrors at import. Emit the resolver, or record the delegation "
        "in _DELEGATES_THE_RESOLVER and add a pairing test for the emitting function."
    )


def test_the_resolver_never_logs_the_key_it_resolved():
    """The resolver runs inside the customer's container and its failure path is the one
    place a key could reach CloudWatch. It must name the ARN and the expected shape only."""
    from app.services.code_generator import _PROVIDER_KEY_HELPER

    assert "_key" not in _PROVIDER_KEY_HELPER.split("raise RuntimeError")[1].split(")")[0]
    for line in _PROVIDER_KEY_HELPER.splitlines():
        if "print(" in line or "logger" in line or "logging" in line:
            pytest.fail(f"the provider-key resolver logs: {line.strip()}")


# ---------------------------------------------------------------------------
# The second deploy path makes the same decision
# ---------------------------------------------------------------------------
# Everything above drives the Step Functions handlers. There is a second path that creates
# a runtime: WorkflowExecutor's in-process direct deploy in services/deployment.py, which
# calls create_runtime_iam_role and builds env_vars itself. It injected NEITHER reference
# and granted NEITHER read, so a non-Bedrock agent deployed there had no key at all — the
# refusal side of this whole contract was upheld there only because there was nothing to
# refuse.
#
# Asserted structurally, over the parsed call site rather than over behaviour, because the
# env_vars block lives ~1300 lines inside one method that creates fourteen AWS resources
# and cannot be driven without mocking essentially all of it. The properties these pin are
# exactly the ones a source read gets wrong: which keyword a value is passed as, and
# whether the two sides use the same helper call. What each keyword then DOES is covered
# by TestLegacyRoleEmitsTheGrants above, which drives the real create_runtime_iam_role.


def _deployment_ast():
    import ast

    from app.services import deployment as deployment_module

    return ast.parse(Path(deployment_module.__file__).read_text(encoding="utf-8"))


def _calls_named(tree, name: str) -> list:
    import ast

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if attr == name:
                found.append(node)
    return found


class TestTheDirectDeployPathPairsTheGrantAndTheInjection:
    def test_it_resolves_both_arns_from_the_one_shared_helper(self):
        """Not its own copy of the decision. runtime_key_grant_targets is the single
        function the injecting step and the granting step share; a third implementation
        here is a third thing that can answer differently for the same canvas."""
        calls = _calls_named(_deployment_ast(), "runtime_key_grant_targets")
        assert len(calls) == 1, f"expected exactly one runtime_key_grant_targets call, found {len(calls)}"

    def test_it_passes_both_arns_to_the_agent_role(self):
        import ast

        calls = _role_calls_by_target()
        assert "_runtime_role_result" in calls, "deployment.py no longer creates the agent runtime's role"
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in calls["_runtime_role_result"].keywords if kw.arg}
        assert "provider_key_secret_arn" in kwargs, (
            "create_runtime_iam_role is called without provider_key_secret_arn, so the "
            "agent is handed PROVIDER_API_KEY_SECRET_ARN and cannot read it: "
            "AccessDeniedException on its first model call"
        )
        assert kwargs["provider_key_secret_arn"] == "_provider_key_arn", kwargs
        assert kwargs["gateway_key_secret_arn"] == "_gateway_key_arn", kwargs

    def test_the_generated_mcp_servers_role_is_granted_neither(self):
        """Found by the test above being too broad, and worth keeping for that reason. This
        path creates a SECOND runtime role, for the generated MCP server, and that one must
        stay ungranted: an MCP server's emitted code imports only mcp.server.fastmcp and
        instantiates no model, so it has no key to read. Granting it anyway would widen a
        secret read to a container that has no use for one."""
        calls = _role_calls_by_target()
        assert "_mcp_role_result" in calls, "the generated MCP server no longer gets its own role"
        kwargs = {kw.arg for kw in calls["_mcp_role_result"].keywords if kw.arg}
        assert "provider_key_secret_arn" not in kwargs
        assert "gateway_key_secret_arn" not in kwargs

    def test_it_injects_both_references_and_neither_plaintext(self):
        source = Path(_deployment_source_path()).read_text(encoding="utf-8")
        assert 'env_vars["PROVIDER_API_KEY_SECRET_ARN"] = _provider_key_arn' in source
        assert 'env_vars["GATEWAY_API_KEY_SECRET_ARN"] = _gateway_key_arn' in source
        assert 'env_vars["PROVIDER_API_KEY"]' not in source, "the plaintext model-provider key is back"
        assert 'env_vars["GATEWAY_API_KEY"]' not in source, "the plaintext LiteLLM virtual key is back"
        assert 'env_vars["COGNITO_CLIENT_SECRET"]' not in source

    def test_a_litellm_gateway_does_not_also_get_cognito_variables(self):
        """The two branches must stay mutually exclusive on this path too. A LiteLLM
        gateway has no token endpoint, so COGNITO_CLIENT_ID sends the generated agent to
        an OAuth2 exchange against a host that does not exist — and this path had no
        provider branch at all, treating every gateway as Cognito."""
        source = Path(_deployment_source_path()).read_text(encoding="utf-8")
        assert 'env_vars["GATEWAY_AUTH_MODE"] = "static_bearer"' in source
        litellm_at = source.index('env_vars["GATEWAY_AUTH_MODE"] = "static_bearer"')
        cognito_at = source.index('env_vars["COGNITO_CLIENT_ID"]')
        between = source[litellm_at:cognito_at]
        assert "elif gateway_result:" in between, (
            "the Cognito variables must sit in a branch the LiteLLM case cannot also enter"
        )


def _deployment_source_path() -> str:
    from app.services import deployment as deployment_module

    return deployment_module.__file__


def _role_calls_by_target() -> dict:
    """Every ``create_runtime_iam_role`` call in deployment.py, keyed by the name it is
    assigned to. There are two, and they want opposite things:
    ``_runtime_role_result`` is the agent's provenance-bearing result and must
    carry both secret ARNs; ``_mcp_role_result`` is the generated MCP server's
    and must carry neither."""
    import ast

    out: dict = {}
    for node in ast.walk(_deployment_ast()):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if attr != "create_runtime_iam_role":
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = node.value
    return out
