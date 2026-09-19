"""The contract the exported CloudFormation bundle owes its recipient.

This file exists because the generator that produces our only customer-facing
artifact had effectively no test coverage: of 118 backend test files, two
mentioned ``CfnTemplateGenerator`` and both did so incidentally. That is how an
entire gateway provider went unhandled without a single failing test, and how the
generated README came to tell customers that the stack has one Custom Resource
when it has three.

So these are not unit tests of internal helpers. Each one asserts something a
recipient of the zip would notice if it broke:

  * a canvas we cannot export faithfully is refused, not silently mis-exported
  * deleting the stack does not take user identities or conversation history
  * the outputs are sufficient to actually call the gateway the stack created
  * no secret is reachable through a stack Output or a Parameter
  * the emitted YAML passes cfn-lint
  * the README describes the bundle it is actually inside

Run against every component combination, because the bugs live in the
combinations rather than in the common path.
"""

import ast
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from unittest import mock

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import cfn_template_generator
from app.services.cfn_template_generator import (
    DATA_BEARING_RESOURCE_TYPES,
    DELETION_DEPENDENCIES,
    EMPTY_CONTENT_DIGEST,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
    content_digest,
)
from app.services.region_models import current_region

MODEL_ID = "us.anthropic.claude-sonnet-5"

# The custom-resource Lambda's own source. Several tests below read it as text rather
# than importing it: it is packaged as a flat zip and cannot import ``app.*``, and what
# they check is the wiring between the template and the handler, which only exists as
# matching string literals on either side.
CFN_PROVIDER_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"

# The only Cedar shape AgentCore will store. A permit whose action is unconstrained
# — including `action == AgentCore::Action::"x"` for a single action — is accepted by
# CreatePolicy with a 200 and then goes CREATE_FAILED as "Overly Permissive"; see
# CfnTemplateGenerator._cedar_permit. These fixtures used to carry
# `permit(principal, action, resource);`, which no deploy could ever have accepted.
_VALID_CEDAR = (
    'permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"KBTool___knowledge_base_query"], '
    'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc");'
)

AGENTCORE_GATEWAY = {"gateway_provider": "agentcore", "targetType": "lambda"}
# Keys the canvas actually sends (frontend/src/types/components.ts KBConfig), not
# plausible-looking ones: this fixture originally said `storageType`/`s3Uri`/`name`,
# which the generator never reads, so it silently tested a KB with no data source
# and the platform-default vector store instead of the one it named.
KB_CONFIG = {
    "kbMode": "create_new",
    "kbName": "kb",
    "embeddingModelId": "amazon.titan-embed-text-v2:0",
    "vectorStoreType": "s3_vectors",
    "dataSourceType": "s3",
    "s3BucketUri": "s3://example-bucket/docs",
}
KB_CONFIG_OPENSEARCH = {
    **KB_CONFIG,
    "vectorStoreType": "opensearch_serverless",
    "opensearchCollectionArn": "arn:aws:aoss:us-east-1:123456789012:collection/abcdefghij1234567890",
}


def _require_scanner(tool, how):
    """Skip locally when *tool* is absent from PATH, but never in CI.

    ``pytest.importorskip`` was the original hole: cfn-lint was declared as a
    dependency nowhere, so the only lint gate over our customer-facing artifact
    would report "skipped" in a pipeline and nobody would notice. A gate that skips
    itself is not a gate, so where CI is set, a missing scanner is a broken pipeline
    and it says so.

    PATH and not importability. Both scanners are run through ``subprocess`` below,
    and the two questions are genuinely different: an importable package does not
    guarantee its console script is on PATH, and — the case that matters here —
    checkov deliberately is NOT installed in this interpreter. checkov pins
    ``boto3==1.35.49``, this backend requires ``boto3>=1.43.66`` for the GA Agent
    Registry service models, and pip resolves that standoff by silently downgrading
    checkov to a build with a different rule set than the accepted-findings baseline
    below was recorded against. So checkov is installed as an isolated tool (pipx,
    uv tool, or a container) and found on PATH, never as a sibling of the runtime.
    """
    if shutil.which(tool) is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"{tool} is not on PATH; this gate cannot run. Install it: {how}")
    pytest.skip(f"{tool} not on PATH ({how})")


def _config(name="exporttest"):
    return RuntimeConfig(name=name, model={"modelId": MODEL_ID})


def _generate(**kwargs):
    kwargs.setdefault("nodeId", "node-1")
    return CfnTemplateGenerator().generate(DeployRequest(config=_config(), **kwargs))


def _template(**kwargs):
    return yaml.safe_load(_generate(**kwargs).template_yaml)


def _walk_for_refs(expr):
    """Every parameter name reachable by ``Ref`` anywhere inside an intrinsic expression.

    Yields names, not the nodes, because callers compare sets of names. Recurses
    through the intrinsics we actually compose — ``Fn::Join``, ``Fn::Split``,
    ``Fn::Select`` — since a digest reaches a key through a nest of those rather than
    as a bare ``Ref``.
    """
    if isinstance(expr, dict):
        for key, value in expr.items():
            if key == "Ref" and isinstance(value, str):
                yield value
            else:
                yield from _walk_for_refs(value)
    elif isinstance(expr, list):
        for item in expr:
            yield from _walk_for_refs(item)


def _runtime_env(template, resource="AgentCoreRuntime"):
    """The environment the agent process actually sees.

    Named so a move of ``EnvironmentVariables`` within the resource breaks one
    helper rather than every test that reads the environment.
    """
    return template["Resources"][resource]["Properties"]["EnvironmentVariables"]


# Every combination of the optional components, named so a failure says which one.
COMPONENT_COMBINATIONS = {
    "runtime-only": {},
    "gateway": {"gateway_config": AGENTCORE_GATEWAY},
    "memory": {"memory_config": {"enabled": True}},
    "gateway+memory": {"gateway_config": AGENTCORE_GATEWAY, "memory_config": {"enabled": True}},
    "gateway+kb": {"gateway_config": AGENTCORE_GATEWAY, "knowledge_base_config": KB_CONFIG},
    "gateway+policy": {
        "gateway_config": AGENTCORE_GATEWAY,
        "policy_config": {"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
    },
    # The generated default policy rather than a supplied statement: policy_config
    # is present but names no policies, which is what the canvas sends when a user
    # switches the policy engine on and writes no Cedar. The KB is here because that
    # default is built from the gateway tools the template emits, so a canvas with no
    # tool has no policy to generate (and this combination asserts the KB tool one).
    "gateway+kb+default-policy": {
        "gateway_config": AGENTCORE_GATEWAY,
        "knowledge_base_config": KB_CONFIG,
        "policy_config": {},
    },
    "mcp-server": {
        "template_id": "mcp-server-gateway-target",
        "gateway_config": AGENTCORE_GATEWAY,
        "mcp_server_config": {"tools": []},
    },
    "everything": {
        "template_id": "mcp-server-gateway-target",
        "gateway_config": AGENTCORE_GATEWAY,
        "memory_config": {"enabled": True},
        "mcp_server_config": {"tools": []},
        "knowledge_base_config": KB_CONFIG,
        "policy_config": {"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
    },
}

ALL_COMBINATIONS = pytest.mark.parametrize("combo", COMPONENT_COMBINATIONS.values(), ids=list(COMPONENT_COMBINATIONS))


# ---------------------------------------------------------------------------
# The gateway provider must be honoured, not ignored
# ---------------------------------------------------------------------------


class TestAnEmptyGatewayIsNotSilent:
    """A gateway with no targets serves nothing, forever, and used to say nothing.

    It is not an error — a canvas that defines no gateway tools should get what it
    asked for. But the export is the last moment anyone can tell, and without a word
    at export time the discovery that returns an empty list looks like a deployment
    fault. It read as one for ~450s of retries before the export itself was suspected.
    """

    def test_a_gateway_with_no_targets_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.cfn_template_generator"):
            template = yaml.safe_load(_generate(**COMPONENT_COMBINATIONS["gateway"]).template_yaml)

        # Precondition, so this test fails loudly rather than vacuously if the gateway
        # combo ever starts emitting a target of its own.
        types = [r["Type"] for r in template["Resources"].values()]
        assert "AWS::BedrockAgentCore::Gateway" in types
        assert "AWS::BedrockAgentCore::GatewayTarget" not in types

        assert any("no targets" in r.getMessage() for r in caplog.records), (
            "an empty gateway was exported without a word: " + repr([r.getMessage() for r in caplog.records])
        )

    @pytest.mark.parametrize("combo", ["gateway+kb", "mcp-server", "everything"])
    def test_a_gateway_that_has_targets_does_not_warn(self, combo, caplog):
        """The warning has to discriminate, or it is noise that gets filtered out."""
        with caplog.at_level(logging.WARNING, logger="app.services.cfn_template_generator"):
            template = yaml.safe_load(_generate(**COMPONENT_COMBINATIONS[combo]).template_yaml)

        assert any(r["Type"] == "AWS::BedrockAgentCore::GatewayTarget" for r in template["Resources"].values())
        assert not any("no targets" in r.getMessage() for r in caplog.records)

    def test_a_runtime_only_export_does_not_warn_about_a_gateway_it_has_not_got(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.cfn_template_generator"):
            _generate(**COMPONENT_COMBINATIONS["runtime-only"])
        assert not any("no targets" in r.getMessage() for r in caplog.records)


class TestGatewayProviderIsHonoured:
    """A LiteLLM canvas must never export an AgentCore Gateway.

    ``has_gateway`` used to be computed from the presence of a gateway config,
    gateway tools or a matching template id, and never read the provider. A
    LiteLLM agent therefore exported a full AgentCore Gateway plus a Cognito user
    pool, resource server, domain and client, and dropped the proxy base URL and
    virtual key on the floor with no error. The stack deployed cleanly and did the
    wrong thing, which is the worst available outcome.
    """

    @pytest.mark.parametrize(
        "gateway_config",
        [
            pytest.param(
                {"gateway_provider": "litellm", "litellm_base_url": "https://proxy.example.internal"},
                id="snake_case",
            ),
            pytest.param(
                {"gatewayProvider": "litellm", "litellmBaseUrl": "https://proxy.example.internal"},
                id="camelCase-as-sent-by-the-frontend",
            ),
        ],
    )
    def test_litellm_canvas_emits_no_agentcore_gateway(self, gateway_config):
        """Both spellings of the provider key must reach the LiteLLM branch.

        The camelCase case is the one that matters: the frontend sends camelCase,
        so a resolver that only read snake_case would send every real canvas down
        the AgentCore path while the snake_case test passed.
        """
        template = yaml.safe_load(_generate(gateway_config=gateway_config).template_yaml)
        types = {r["Type"] for r in template["Resources"].values()}
        assert "AWS::BedrockAgentCore::Gateway" not in types
        assert not [t for t in types if t.startswith("AWS::Cognito::")]

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"gateway_config": AGENTCORE_GATEWAY}, id="explicit-agentcore"),
            pytest.param({"gateway_config": {"targetType": "lambda"}}, id="agentcore-shaped-no-provider"),
            pytest.param({"template_id": "strands-gateway-agent"}, id="gateway-from-template-id"),
            pytest.param({}, id="no-gateway"),
        ],
    )
    def test_agentcore_paths_are_untouched(self, kwargs):
        """The guard must not regress any canvas that worked before it existed."""
        template = yaml.safe_load(_generate(**kwargs).template_yaml)
        assert template["Resources"], "export produced no resources"

    def test_uses_the_same_resolver_as_the_live_deploy_path(self):
        """The two paths must agree about what provider a canvas has.

        The export and the Step Functions gateway step disagreeing is the bug
        this whole workstream is about, so assert they share one implementation
        rather than each having their own idea of what "litellm" means.
        """
        import app.services.cfn_template_generator as gen
        from app.services.litellm_gateway_deployer import resolve_gateway_provider

        assert gen.resolve_gateway_provider is resolve_gateway_provider


# ---------------------------------------------------------------------------
# The LiteLLM branch must produce a stack that can actually reach the proxy
# ---------------------------------------------------------------------------

LITELLM_KEY_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:litellm-key-AbCdEf"
LITELLM_GATEWAY = {
    "gateway_provider": "litellm",
    "litellm_base_url": "https://litellm.example.internal",
    "litellm_servers": ["github", "jira"],
}


def _litellm(**overrides):
    """A LiteLLM gateway config, with the canvas keys the frontend really sends."""
    return {**LITELLM_GATEWAY, **overrides}


class TestLiteLLMExport:
    """What a LiteLLM canvas exports instead of an AgentCore gateway.

    Refusing was the interim fix; this is the real one. A recipient of this bundle
    has to be able to deploy it and have the agent reach *their* proxy, which
    means the stack needs the endpoint, the server scope and a way to obtain the
    virtual key — and must not contain the key.
    """

    def test_no_gateway_and_no_cognito_resources(self):
        template = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)
        types = {name: r["Type"] for name, r in template["Resources"].items()}
        offenders = {
            n: t
            for n, t in types.items()
            if t.startswith("AWS::Cognito::")
            or t in ("AWS::BedrockAgentCore::Gateway", "AWS::BedrockAgentCore::GatewayTarget")
        }
        assert not offenders, f"LiteLLM export created AgentCore gateway/Cognito resources: {offenders}"

    def test_no_tool_lambdas_are_created_or_shipped(self):
        """The tools live on the customer's proxy. Shipping ours would be wrong.

        Not cosmetic: a gateway target Lambda gets an execution role and a
        resource policy, so emitting them would hand the recipient live functions
        nothing invokes.
        """
        bundle = _generate(gateway_config=_litellm())
        template = yaml.safe_load(bundle.template_yaml)
        assert not [
            r for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Function" and "Tool" in str(r)
        ]
        assert bundle.tool_lambda_code is None

    def test_the_runtime_is_told_where_the_proxy_is_and_how_to_scope_it(self):
        template = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)
        env = _runtime_env(template)
        assert env["GATEWAY_URL"] == {"Ref": "LiteLLMGatewayUrl"}
        assert env["GATEWAY_AUTH_MODE"] == "static_bearer"
        assert env["GATEWAY_MCP_SERVERS"] == {"Ref": "LiteLLMMcpServers"}
        assert env["GATEWAY_API_KEY_SECRET_ARN"] == {"Ref": "LiteLLMApiKeySecretArn"}
        # The value, never. An env var holding the key would be readable through
        # DescribeAgentRuntime by anyone who can read the runtime.
        assert "GATEWAY_API_KEY" not in env

    def test_the_endpoint_default_is_the_mcp_url_not_the_bare_base_url(self):
        """A bare base URL is not an MCP endpoint; connecting to it 404s.

        Two pinned servers means the aggregate ``/mcp/`` endpoint plus the
        ``x-mcp-servers`` header, and one pinned server means the per-server form.
        Both are the live path's rules, reused rather than reimplemented.
        """
        from app.services.litellm_gateway_deployer import resolve_mcp_url

        many = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)
        assert many["Parameters"]["LiteLLMGatewayUrl"]["Default"] == resolve_mcp_url(
            "https://litellm.example.internal", ["github", "jira"]
        )
        assert many["Parameters"]["LiteLLMMcpServers"]["Default"] == "github,jira"

        one = yaml.safe_load(_generate(gateway_config=_litellm(litellm_servers=["github"])).template_yaml)
        assert one["Parameters"]["LiteLLMGatewayUrl"]["Default"] == resolve_mcp_url(
            "https://litellm.example.internal", ["github"]
        )

    def test_the_key_parameter_has_no_default_when_the_canvas_has_no_arn(self):
        """No default means CloudFormation refuses to deploy without a value.

        The alternative — a placeholder default — produces a stack that creates
        cleanly and then 401s on every tool call, which is far harder to diagnose
        than a missing parameter.
        """
        params = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)["Parameters"]
        assert "Default" not in params["LiteLLMApiKeySecretArn"]
        assert params["LiteLLMApiKeySecretArn"]["AllowedPattern"]

    def test_a_stored_arn_becomes_the_default_but_a_key_never_does(self):
        with_arn = yaml.safe_load(_generate(gateway_config=_litellm(litellm_api_key_ref=LITELLM_KEY_ARN)).template_yaml)
        assert with_arn["Parameters"]["LiteLLMApiKeySecretArn"]["Default"] == LITELLM_KEY_ARN

        # A canvas carrying the key itself, or a non-ARN reference, must not put it
        # in the template — the reference is dropped and the parameter goes back to
        # having no default.
        leaky = yaml.safe_load(
            _generate(
                gateway_config=_litellm(litellm_api_key_ref="sk-not-an-arn", litellm_api_key="sk-super-secret")
            ).template_yaml
        )
        assert "Default" not in leaky["Parameters"]["LiteLLMApiKeySecretArn"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"gateway_config": _litellm(litellm_api_key="sk-super-secret-value")}, id="key-on-canvas"),
            pytest.param(
                {"gateway_config": _litellm(litellm_api_key_ref="sk-super-secret-value")}, id="key-in-the-ref-field"
            ),
        ],
    )
    def test_the_virtual_key_never_appears_anywhere_in_the_bundle(self, kwargs):
        """Every file the recipient receives, not just the template.

        deploy.sh and the README are as public as the template — people paste all
        three into tickets.
        """
        bundle = _generate(**kwargs)
        for label, content in (
            ("template.yaml", bundle.template_yaml),
            ("deploy.sh", bundle.deploy_sh),
            ("README.md", bundle.readme),
            ("agent.py", bundle.agent_code),
        ):
            assert "sk-super-secret-value" not in content, f"the virtual key leaked into {label}"

    def test_the_runtime_role_can_read_that_one_secret_and_nothing_else(self):
        role = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)["Resources"]["RuntimeExecutionRole"]
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        by_sid = {s.get("Sid"): s for s in statements}

        read = by_sid["LiteLLMVirtualKey"]
        assert read["Action"] == ["secretsmanager:GetSecretValue"]
        assert read["Resource"] == [{"Ref": "LiteLLMApiKeySecretArn"}]

        # kms:Decrypt cannot be scoped by resource here: the key that encrypts a
        # customer's secret is not known at export time. ARCC guidance on
        # wildcards (cnt_SFJJhkOueCPRkd) asks for condition keys instead, and
        # Secrets Manager always passes SecretARN in the encryption context, so
        # the effective reach is exactly the one secret above.
        decrypt = by_sid["LiteLLMVirtualKeyDecrypt"]
        assert decrypt["Resource"] == "*"
        conditions = decrypt["Condition"]["StringEquals"]
        assert conditions["kms:EncryptionContext:SecretARN"] == {"Ref": "LiteLLMApiKeySecretArn"}
        assert conditions["kms:ViaService"] == {"Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}

    @pytest.mark.parametrize("connected", [[], ["gateway"]], ids=["config-only", "with-connected-tools"])
    def test_no_agentcore_gateway_permissions_are_granted(self, connected):
        """A LiteLLM agent never calls bedrock-agentcore gateway APIs.

        Parametrised on ``connected_tools`` because that is where this test had a hole
        wide enough for the bug to live in. The generator passes
        ``has_gateway and not is_litellm`` into the role builder, deliberately — and the
        builder then said ``if has_gateway or "gateway" in connected_tools``, which put
        the AgentCore gateway grants straight back for any canvas that connects a
        gateway node. Which is every real canvas: the frontend sends both. The old test
        passed ``gateway_config`` alone, so it only ever exercised the half that worked.
        """
        template = yaml.safe_load(_generate(gateway_config=_litellm(), connected_tools=connected).template_yaml)
        role = template["Resources"]["RuntimeExecutionRole"]
        actions = [
            a for s in role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] for a in _as_list(s["Action"])
        ]
        assert not [a for a in actions if a.startswith("bedrock-agentcore:") and "Gateway" in a]
        # The stronger statement: nothing in this role may reference a resource the
        # LiteLLM template does not contain. A GetAtt on AgentCoreGateway would make the
        # template unresolvable, not merely over-permissive.
        assert "AgentCoreGateway" not in json.dumps(role), "the role references a resource this template omits"

    def test_the_outputs_name_the_provider_and_skip_cognito(self):
        """Same output name, different provider: callers should not have to care."""
        outputs = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)["Outputs"]
        assert outputs["GatewayUrl"]["Value"] == {"Ref": "LiteLLMGatewayUrl"}
        assert outputs["GatewayProvider"]["Value"] == "litellm"
        assert not [k for k in outputs if k.startswith("Cognito")]

    def test_a_knowledge_base_still_reaches_the_agent(self):
        """The KB used to be silently dropped on any non-AgentCore-gateway path.

        ``_add_kb_creation_resources`` was only called from inside the gateway-only
        KB tool Lambda helper, so a LiteLLM canvas with a Knowledge Base node
        exported a stack with no knowledge base at all and an agent that could not
        retrieve. Here the KB is created and the runtime is given its id directly.
        """
        template = yaml.safe_load(_generate(gateway_config=_litellm(), knowledge_base_config=KB_CONFIG).template_yaml)
        types = {r["Type"] for r in template["Resources"].values()}
        assert "AWS::Bedrock::KnowledgeBase" in types

        env = _runtime_env(template)
        assert env["KB_ID"] == {"Fn::GetAtt": ["BedrockKnowledgeBase", "KnowledgeBaseId"]}

        sids = {
            s.get("Sid")
            for s in template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
                "Statement"
            ]
        }
        assert "KnowledgeBaseRetrieve" in sids

    def test_an_existing_knowledge_base_is_referenced_by_parameter(self):
        template = yaml.safe_load(
            _generate(
                gateway_config=_litellm(),
                knowledge_base_config={"kbMode": "existing", "knowledgeBaseId": "KB1234567890"},
            ).template_yaml
        )
        assert template["Parameters"]["KnowledgeBaseId"]["Default"] == "KB1234567890"
        env = _runtime_env(template)
        assert env["KB_ID"] == {"Ref": "KnowledgeBaseId"}
        # No KB resources of our own: we must not manage a KB the customer owns.
        assert "AWS::Bedrock::KnowledgeBase" not in {r["Type"] for r in template["Resources"].values()}

    @pytest.mark.parametrize(
        ("gateway_config", "expected"),
        [
            pytest.param(_litellm(litellm_base_url=""), "base URL", id="no-base-url"),
            pytest.param(_litellm(litellm_base_url="http://proxy.example.internal"), "https", id="not-https"),
        ],
    )
    def test_a_canvas_that_cannot_be_exported_faithfully_is_refused(self, gateway_config, expected):
        """Only the shapes that genuinely cannot work. Refusal is not the default.

        http is refused because the runtime sends the virtual key to this URL on
        every request. A *private* https URL is fine — the recipient may deploy
        this stack inside the VPC that can reach it.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(gateway_config=gateway_config)
        assert expected in str(exc.value)

    def test_litellm_plus_an_mcp_server_node_is_refused_with_both_ways_out(self):
        """The one combination with no faithful export.

        An MCP Server node becomes an AgentCore Gateway *target*, and a LiteLLM
        proxy cannot host one — its MCP servers are registered on the proxy. Both
        remedies belong in the message because either is legitimate.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=_litellm(),
                template_id="mcp-server-gateway-target",
                mcp_server_config={"tools": []},
            )
        message = str(exc.value)
        assert "MCP Server" in message
        assert "AgentCore gateway provider" in message
        assert "register the MCP server on your" in message

    def test_litellm_plus_a_policy_node_is_refused_with_the_right_advice(self):
        """The other combination with no faithful export — and the wrong error before.

        A Cedar policy engine authorizes calls through an AgentCore Gateway: the
        statement scopes to a gateway ARN and its actions are that gateway's targets'
        tools. A LiteLLM proxy has neither.

        This was not merely unhandled. ``_add_policies`` found no gateway target and
        raised its "no gateway tool whose actions can be named" ValueError — a bare
        ValueError, so a generic 500 to the caller, carrying advice ("add gateway
        tools") that can never work on a LiteLLM canvas because no tool there produces
        an AgentCore action id. So this asserts the *advice* as much as the refusal.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=_litellm(),
                policy_config={"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
            )
        message = str(exc.value)
        assert "policy" in message
        assert "virtual-key permissions" in message, "must say where LiteLLM authorization actually lives"
        assert "AgentCore gateway provider" in message
        assert "gateway tool" not in message, "the old message sent the user down a path that cannot work"

    def test_a_policy_engine_switched_on_with_no_cedar_is_refused_too(self):
        """``policy_config={}`` is what the canvas sends for a switch with no Cedar.

        It has to be refused on the same grounds; being empty does not make it
        exportable, and the empty case is the one that used to reach _add_policies.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(gateway_config=_litellm(), policy_config={})
        assert "LiteLLM" in str(exc.value)

    def test_the_deploy_script_refuses_to_run_without_the_secret_arn(self):
        """Fail before uploading artifacts, not after.

        The parameter has no default, so without this the operator uploads three
        zips and then gets a CloudFormation validation error naming a parameter
        the README explains and the script did not ask for.
        """
        script = _generate(gateway_config=_litellm()).deploy_sh
        assert "LITELLM_API_KEY_SECRET_ARN" in script
        assert 'PARAM_OVERRIDES+=("LiteLLMApiKeySecretArn=$LITELLM_SECRET_ARN")' in script
        # The check has to precede the first upload, or "fail early" is a lie.
        assert script.index("LITELLM_SECRET_ARN") < script.index("aws s3 cp")

    @pytest.mark.parametrize(
        ("args", "env", "expect_exit", "expect_text"),
        [
            pytest.param(["st", "us-east-1", "bkt"], {}, 1, "ERROR: this stack needs the ARN", id="arn-missing"),
            pytest.param(
                ["st", "us-east-1", "bkt", "sk-a-key-not-an-arn"],
                {},
                1,
                "is not a Secrets Manager ARN",
                id="key-pasted-where-an-arn-belongs",
            ),
        ],
    )
    def test_the_generated_deploy_script_actually_behaves_that_way(self, args, env, expect_exit, expect_text, tmp_path):
        """Run the script. A refusal asserted only by grep is not a refusal."""
        script = tmp_path / "deploy.sh"
        script.write_text(_generate(gateway_config=_litellm()).deploy_sh)
        proc = subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            # No AWS credentials reach this: the script must fail on the ARN check
            # before it ever tries to call AWS.
            env={"PATH": "/usr/bin:/bin", **env},
        )
        assert proc.returncode == expect_exit, proc.stdout + proc.stderr
        assert expect_text in proc.stdout
        assert "sk-a-key-not-an-arn" not in proc.stdout, "the script echoed back a value that may be a key"

    def test_the_generated_deploy_script_is_valid_bash(self, tmp_path):
        script = tmp_path / "deploy.sh"
        script.write_text(_generate(gateway_config=_litellm()).deploy_sh)
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    def test_the_readme_documents_the_secret_prerequisite(self):
        """The recipient cannot deploy without it, so it cannot be undocumented."""
        readme = _generate(gateway_config=_litellm()).readme
        assert "LiteLLM Gateway" in readme
        assert "LITELLM_API_KEY_SECRET_ARN" in readme
        assert "create-secret" in readme, "must say how to create the secret"
        assert "apiKey" in readme, "must state the accepted secret shapes"
        # The README must not still promise the Cognito flow this stack has no
        # resources for.
        assert "CognitoTokenEndpoint" not in readme

    def test_the_generated_agent_reads_the_secret_at_runtime(self):
        """The other half of by-reference: someone has to resolve the ARN."""
        code = _generate(gateway_config=_litellm()).agent_code
        assert "GATEWAY_API_KEY_SECRET_ARN" in code
        assert "_resolve_gateway_key" in code
        assert "get_secret_value" in code
        compile(code, "agent.py", "exec")

    @pytest.mark.parametrize("combo_name", ["gateway", "everything"])
    def test_the_generated_agent_resolves_its_client_secret_from_cognito(self, combo_name):
        """Runs the generated function, rather than asserting the string contains it.

        The template stopped passing ``COGNITO_CLIENT_SECRET``, so if this code path is
        wrong the export deploys green and then cannot mint a token at all — a worse
        outcome than the leak it replaced. Compiling proves only that it parses, and a
        substring check proves less than that, so execute it.

        ``agent.py`` cannot be imported whole (it imports ``strands`` at module level),
        so the function is lifted out by name and run against a fake ``boto3``.
        """
        code = _generate(**COMPONENT_COMBINATIONS[combo_name]).agent_code
        compile(code, "agent.py", "exec")
        tree = ast.parse(code)
        source = next(
            (
                ast.get_source_segment(code, node)
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_resolve_client_secret"
            ),
            None,
        )
        assert source, "the generated agent has no _resolve_client_secret to resolve the secret with"

        calls = []

        class _FakeIdp:
            def describe_user_pool_client(self, UserPoolId, ClientId):  # noqa: N803 - boto3 casing
                calls.append((UserPoolId, ClientId))
                return {"UserPoolClient": {"ClientSecret": "secret-from-cognito"}}

        fake_boto3 = types.SimpleNamespace(client=lambda service, region_name=None: _FakeIdp())

        def _run(env_secret, pool_id, client_id="client-abc"):
            namespace = {
                "COGNITO_CLIENT_SECRET": env_secret,
                "COGNITO_USER_POOL_ID": pool_id,
                "COGNITO_CLIENT_ID": client_id,
                "REGION": "us-east-1",
                "_client_secret_cache": {},
            }
            with mock.patch.dict(sys.modules, {"boto3": fake_boto3}):
                exec(source, namespace)  # noqa: S102 - the point is to run generated code
                return namespace["_resolve_client_secret"]()

        # The export's path: no secret in the environment, so it must come from Cognito,
        # asked for against the pool and client the template supplied.
        assert _run("", "us-east-1_pool") == "secret-from-cognito"
        assert calls == [("us-east-1_pool", "client-abc")]

        # The platform's Step Functions path still injects the value, and must keep
        # working without calling AWS at all — that is what keeps this change's blast
        # radius off the live deployments.
        calls.clear()
        assert _run("secret-from-env", "us-east-1_pool") == "secret-from-env"
        assert calls == [], "the env var must short-circuit before any AWS call"

        # Neither source configured is an empty string, not an exception: the gateway
        # path is optional and _get_gateway_token already handles an empty credential.
        calls.clear()
        assert _run("", "") == ""
        assert calls == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="plain"),
            pytest.param({"memory_config": {"enabled": True}}, id="memory"),
            pytest.param({"knowledge_base_config": KB_CONFIG}, id="kb"),
            pytest.param({"memory_config": {"enabled": True}, "knowledge_base_config": KB_CONFIG}, id="kb+memory"),
        ],
    )
    def test_cfn_lint_passes_on_every_litellm_variant(self, kwargs):
        _require_scanner("cfn-lint", "pip install -e '.[dev]'")
        bundle = _generate(gateway_config=_litellm(), **kwargs)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(bundle.template_yaml)
            path = handle.name
        try:
            result = subprocess.run(
                ["cfn-lint", path, "--format", "json", "--ignore-checks", "W"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                findings = json.loads(result.stdout or "[]")
                rendered = "\n".join(
                    f"  {f['Rule']['Id']} {f['Level']}: {f['Message']} (line {f['Location']['Start']['LineNumber']})"
                    for f in findings
                )
                pytest.fail(f"cfn-lint found {len(findings)} error(s):\n{rendered}")
        finally:
            Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The code the stack deploys must be the code the operator deployed
# ---------------------------------------------------------------------------


class TestSourceIntegrity:
    """One digest algorithm, three implementations, all of which must agree.

    The generator computes the parameter default, the generated deploy.sh
    recomputes it in bash from the operator's files, and the cfn-provider Lambda
    recomputes it from the uploaded zip. If any two disagree, either every deploy
    fails or the check silently passes anything — so this class pins them against
    each other rather than against a hardcoded string.

    Two defects motivated it. Every AgentCodePackage property was a Ref to a
    parameter that does not change between deploys, so editing the agent code
    produced a byte-identical resource: CloudFormation saw no change, the packaging
    step never ran, and the stack kept serving the old code. And the handler merged
    whatever happened to be at the staging key, so s3:PutObject on that key — a far
    lower bar than cloudformation:UpdateStack — was enough to choose what the
    runtime role executes.
    """

    MCP_COMBO = COMPONENT_COMBINATIONS["mcp-server"]

    def _bash_digest(self, directory, *files):
        """Run the digest helper the generated deploy.sh actually ships."""
        script = _generate(**self.MCP_COMBO).deploy_sh
        helpers = re.findall(r"^(?:sha256_stdin|content_digest)\(\).*?^\}$", script, re.MULTILINE | re.DOTALL)
        assert len(helpers) == 2, "deploy.sh no longer defines the digest helpers"
        proc = subprocess.run(
            ["bash", "-c", "\n".join(helpers) + '\ncontent_digest "$@"', "_", str(directory), *files],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def test_the_code_package_resource_changes_when_the_code_changes(self):
        bundle = _generate()
        other = CfnTemplateGenerator().generate(
            DeployRequest(config=RuntimeConfig(name="exporttest", model={"modelId": MODEL_ID}), nodeId="node-1")
        )
        assert bundle.agent_code == other.agent_code, "same input must be reproducible"

        template = yaml.safe_load(bundle.template_yaml)
        digest = template["Parameters"]["AgentCodeDigest"]["Default"]
        assert digest == content_digest({"agent.py": bundle.agent_code})
        assert template["Resources"]["AgentCodePackage"]["Properties"]["SourceDigest"] == {"Ref": "AgentCodeDigest"}
        # Different code must not produce the same digest — that is the whole point.
        assert digest != content_digest({"agent.py": bundle.agent_code + "\n# edited\n"})

    def test_the_default_is_never_a_value_that_disables_the_check(self):
        """An empty default would let a hand-rolled deploy opt out silently."""
        for combo in COMPONENT_COMBINATIONS.values():
            params = _template(**combo)["Parameters"]
            for name in ("AgentCodeDigest", "McpServerCodeDigest"):
                if name in params:
                    assert re.fullmatch(r"sha256:[0-9a-f]{64}", params[name]["Default"]), name
                    assert params[name]["Default"] != EMPTY_CONTENT_DIGEST, f"{name} default was never pinned"

    def test_the_mcp_server_digest_covers_its_own_zip_not_the_agent_one(self):
        """mcp-server-code.zip holds mcp_server.py alone; agent-code.zip holds both."""
        bundle = _generate(**self.MCP_COMBO)
        params = yaml.safe_load(bundle.template_yaml)["Parameters"]
        assert params["McpServerCodeDigest"]["Default"] == content_digest({"mcp_server.py": bundle.mcp_server_code})
        assert params["AgentCodeDigest"]["Default"] == content_digest(
            {"agent.py": bundle.agent_code, "mcp_server.py": bundle.mcp_server_code}
        )
        assert params["AgentCodeDigest"]["Default"] != params["McpServerCodeDigest"]["Default"]

    def test_deploy_script_computes_the_same_digest_as_the_generator(self, tmp_path):
        """The bash implementation is the one that runs in anger. Pin it."""
        bundle = _generate(**self.MCP_COMBO)
        code_dir = tmp_path / "agent-code"
        code_dir.mkdir()
        (code_dir / "agent.py").write_text(bundle.agent_code)
        (code_dir / "mcp_server.py").write_text(bundle.mcp_server_code)
        params = yaml.safe_load(bundle.template_yaml)["Parameters"]

        assert self._bash_digest(code_dir) == params["AgentCodeDigest"]["Default"]
        assert self._bash_digest(code_dir, "mcp_server.py") == params["McpServerCodeDigest"]["Default"]

    def test_deploy_script_ignores_compiled_python(self, tmp_path):
        """A stray __pycache__ from running the code locally must not break the deploy."""
        bundle = _generate()
        code_dir = tmp_path / "agent-code"
        (code_dir / "__pycache__").mkdir(parents=True)
        (code_dir / "agent.py").write_text(bundle.agent_code)
        clean = self._bash_digest(code_dir)
        (code_dir / "__pycache__" / "agent.cpython-313.pyc").write_bytes(b"\x00compiled")
        (code_dir / "agent.pyc").write_bytes(b"\x00compiled")
        assert self._bash_digest(code_dir) == clean

    def test_deploy_script_passes_the_digest_it_computed(self):
        script = _generate(**self.MCP_COMBO).deploy_sh
        assert "AGENT_CODE_DIGEST=$(content_digest agent-code)" in script
        assert '"AgentCodeDigest=$AGENT_CODE_DIGEST"' in script
        assert '"McpServerCodeDigest=$MCP_SERVER_DIGEST"' in script

    def test_a_bundle_already_in_the_bucket_is_still_hashed(self, tmp_path):
        """The branch where the dependency bundle is ALREADY in S3 must hash it.

        This branch used to leave the recorded digest alone, and that made a
        dependency-bundle-only change a silent no-op rather than the loud failure
        the comment there claimed. With the digest unchanged, every property of
        Custom::AgentCodePackage is identical between deploys, so CloudFormation
        skips the resource, the packaging step never runs, and the Lambda's
        _verify_bundle_digest is never reached to notice that the bundle in the
        bucket is not the one the stack was built against. Green stack, old
        dependencies.

        Run rather than grepped, with a fake ``aws`` that reports the object
        present and serves known bytes: the assertion is that the digest the
        script ends up with is the digest of what is IN THE BUCKET.
        """
        script = _generate(**self.MCP_COMBO).deploy_sh
        region = re.search(
            r"^# 3\. Check/build/upload the dependency bundle\n(.*?)^# 4\. Package and upload assets",
            script,
            re.MULTILINE | re.DOTALL,
        )
        assert region, "deploy.sh no longer has a recognisable dependency-bundle section"
        helpers = re.findall(r"^sha256_stdin\(\).*?^\}$", script, re.MULTILINE | re.DOTALL)
        assert len(helpers) == 1, "deploy.sh no longer defines sha256_stdin"

        in_the_bucket = b"pretend-dependency-bundle-bytes\n"
        expected = "sha256:" + hashlib.sha256(in_the_bucket).hexdigest()
        served = tmp_path / "served.zip"
        served.write_bytes(in_the_bucket)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        # head-object succeeds (the object is there); cp copies the known bytes to
        # whatever destination the script chose. Anything else is a failure, so the
        # test cannot pass by taking some other path through the branch.
        (fake_bin / "aws").write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "s3api" && "$2" == "head-object" ]]; then exit 0; fi\n'
            'if [[ "$1" == "s3" && "$2" == "cp" ]]; then cp ' + str(served) + ' "$4"; exit 0; fi\n'
            'echo "unexpected aws call: $*" >&2; exit 64\n'
        )
        (fake_bin / "aws").chmod(0o755)

        proc = subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                "BUCKET=bkt\nREGION=us-east-1\n"
                + helpers[0]
                + "\n"
                + region.group(1)
                + '\necho "DIGEST=$BUNDLE_DIGEST"\n',
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"DIGEST={expected}" in proc.stdout, (
            "the already-in-the-bucket branch did not hash the object it found: " + proc.stdout
        )

    def test_the_readme_does_not_promise_a_check_that_does_not_happen(self):
        """The prose and the script have to agree about when the digest is computed.

        Both used to say the value was "left as it is" for a bundle already in the
        bucket, and both drew the same wrong conclusion from it: that a changed
        object would therefore fail the deploy. It would not — the resource is
        skipped and nothing is ever checked. The script is fixed; a README still
        describing the old behaviour would be worse than one that said nothing,
        because it tells an operator the account is covered when it is not.
        """
        readme = _generate(**COMPONENT_COMBINATIONS["everything"]).readme

        assert "left as it is" not in readme, "README still describes the stale-digest behaviour"
        assert "nothing local to hash" not in readme

        # And states what actually happens, including the limit of it: hashing the
        # object proves provenance from the bucket, not from a build you trust.
        assert "downloads" in readme and "hashes the bytes themselves" in readme
        # Substring kept short deliberately: the README is hard-wrapped, so a longer
        # phrase would straddle a newline and fail for reasons of layout, not content.
        assert "cannot, by itself, tell you" in readme

    def test_the_lambda_verifies_the_zip_the_deploy_script_builds(self, tmp_path):
        """End of the chain: the real zip, hashed by the real handler."""
        handler = _import_cfn_provider_handler()
        bundle = _generate(**self.MCP_COMBO)
        expected = yaml.safe_load(bundle.template_yaml)["Parameters"]["AgentCodeDigest"]["Default"]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("agent.py", bundle.agent_code)
            zf.writestr("mcp_server.py", bundle.mcp_server_code)
        handler._verify_source_digest(buf.getvalue(), expected)  # must not raise

    def test_substituted_code_is_refused_before_it_is_merged(self):
        handler = _import_cfn_provider_handler()
        bundle = _generate()
        expected = yaml.safe_load(bundle.template_yaml)["Parameters"]["AgentCodeDigest"]["Default"]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("agent.py", bundle.agent_code + "\n# substituted by someone with s3:PutObject\n")
        with pytest.raises(ValueError) as exc:
            handler._verify_source_digest(buf.getvalue(), expected)
        assert "has NOT been deployed" in str(exc.value), "the operator must know the old code is still live"

    def test_a_rebuilt_zip_of_identical_code_still_verifies(self):
        """Why the digest covers members, not the archive: `zip` embeds mtimes."""
        handler = _import_cfn_provider_handler()
        bundle = _generate()
        expected = yaml.safe_load(bundle.template_yaml)["Parameters"]["AgentCodeDigest"]["Default"]

        digests = set()
        for year in (2020, 2030):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(zipfile.ZipInfo("agent.py", date_time=(year, 1, 1, 0, 0, 0)), bundle.agent_code)
            handler._verify_source_digest(buf.getvalue(), expected)
            digests.add(hashlib.sha256(buf.getvalue()).hexdigest())
        assert len(digests) == 2, "the two archives were identical; the test proves nothing"


class TestTheLambdaZipsAreContentAddressed:
    """The zips that bypass the packaging step need the digest in the S3 KEY.

    ``AgentCodeDigest`` above solves this for the code-packaging path, but three
    zips go straight into ``AWS::Lambda::Function.Code``: cfn-provider.zip,
    tool-lambdas.zip and custom-tools.zip. CloudFormation only replaces a
    function's code when ``Code`` changes, and ``Code.S3Key`` was a bare ``Ref`` to
    a parameter deploy.sh set to a FIXED key. So uploading a new zip over that key
    changed nothing CloudFormation could see: "No changes to deploy", green stack,
    old code still running.

    Found live, not by reading. A fixed cfn-provider.zip was uploaded to stack
    ``logprobe0918``, the deploy re-run, and the same bug reproduced from the same
    Lambda — because it was still the old bytes. The fix puts the digest in the key
    so changed bytes are a changed parameter.
    """

    # The parameters deploy.sh must content-address, and the local zip each is
    # built from. Not "every code key": AgentCodeKey and McpServerCodeKey go to the
    # packaging Custom Resource, which has its own digest parameter and re-reads the
    # object on every stack update, so a fixed key is correct for those.
    DIRECT_TO_LAMBDA = {
        "CfnProviderCodeKey": ("cfn-provider.zip", "CFN_PROVIDER_KEY"),
        "ToolLambdaCodeKey": ("tool-lambdas.zip", "TOOL_LAMBDA_KEY"),
        "CustomToolCodeKey": ("custom-tools.zip", "CUSTOM_TOOL_KEY"),
    }

    def _staged_key(self, script, path, name, stack="my-agent"):
        """Run the ``staged_key`` helper the generated deploy.sh actually ships."""
        helpers = re.findall(
            r"^(?:sha256_stdin|staged_key)\(\).*?^\}$",
            script,
            re.MULTILINE | re.DOTALL,
        )
        assert len(helpers) == 2, "deploy.sh no longer defines sha256_stdin and staged_key"
        proc = subprocess.run(
            ["bash", "-c", f'STACK_NAME="{stack}"\n' + "\n".join(helpers) + '\nstaged_key "$@"', "_", str(path), name],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    @ALL_COMBINATIONS
    def test_the_deploy_script_uploads_to_a_key_it_derived_from_the_bytes(self, combo):
        script = _generate(**combo).deploy_sh
        for parameter, (zip_name, shell_var) in self.DIRECT_TO_LAMBDA.items():
            stem = zip_name.removesuffix(".zip")
            assert f"{shell_var}=$(staged_key {zip_name} {stem})" in script, parameter
            assert f'aws s3 cp {zip_name} "s3://$BUCKET/${shell_var}"' in script, parameter
            assert f'"{parameter}=${shell_var}"' in script, parameter
            # And the fixed key it used to upload to is gone, in both places.
            assert f"${{STACK_NAME}}/{zip_name}" not in script, f"{parameter} is still a fixed key"

    def test_changed_bytes_change_the_key(self, tmp_path):
        """The property the whole fix rests on."""
        script = _generate().deploy_sh
        zip_path = tmp_path / "cfn-provider.zip"

        zip_path.write_bytes(b"PK\x03\x04 first build")
        first = self._staged_key(script, zip_path, "cfn-provider")
        zip_path.write_bytes(b"PK\x03\x04 second build, one byte longer")
        second = self._staged_key(script, zip_path, "cfn-provider")

        assert first != second, "a changed zip produced the same key; CloudFormation will skip it"
        assert first.startswith("cfn-assets/my-agent/cfn-provider-")
        assert first.endswith(".zip")
        # Identical bytes must round-trip to the same key, or every deploy churns
        # the Lambda and the bucket fills with duplicates of one build.
        zip_path.write_bytes(b"PK\x03\x04 first build")
        assert self._staged_key(script, zip_path, "cfn-provider") == first

    def test_the_key_is_a_sha256_prefix_and_not_something_weaker(self, tmp_path):
        """Per ARCC cnt_NBPcOqwR3163yt: SHA-256, and enough of it to collide on."""
        zip_path = tmp_path / "custom-tools.zip"
        zip_path.write_bytes(b"PK\x03\x04 payload")
        key = self._staged_key(_generate().deploy_sh, zip_path, "custom-tools")

        digest = key.removeprefix("cfn-assets/my-agent/custom-tools-").removesuffix(".zip")
        assert re.fullmatch(r"[0-9a-f]{16}", digest), key
        assert hashlib.sha256(b"PK\x03\x04 payload").hexdigest().startswith(digest)

    @ALL_COMBINATIONS
    def test_no_function_takes_its_code_from_a_key_that_can_go_stale(self, combo):
        """The structural half: a fourth Lambda added later gets caught here.

        Anything reading ``Code.S3Key`` from a parameter has to be a parameter
        deploy.sh content-addresses, or it inherits the bug this class exists for.
        """
        template = _template(**combo)
        functions = {
            name: body for name, body in template["Resources"].items() if body["Type"] == "AWS::Lambda::Function"
        }
        assert functions, "no Lambda functions at all; this test would pass vacuously"

        for name, body in functions.items():
            key = body["Properties"]["Code"].get("S3Key")
            if not isinstance(key, dict) or "Ref" not in key:
                continue  # inline or Fn::Sub'd code is not staged, so cannot go stale
            assert key["Ref"] in self.DIRECT_TO_LAMBDA, (
                f"{name} takes its code from parameter {key['Ref']!r}, which deploy.sh does not "
                "content-address, so a code change will not be deployed"
            )

    @ALL_COMBINATIONS
    def test_the_parameters_say_they_are_content_addressed(self, combo):
        """A hand-rolled `cloudformation deploy` has to be told, or it hits the bug."""
        params = _template(**combo)["Parameters"]
        for parameter in self.DIRECT_TO_LAMBDA:
            if parameter not in params:
                continue
            assert "content-addressed" in params[parameter]["Description"], parameter

    def test_the_readme_explains_why_the_old_objects_are_left_behind(self):
        readme = _generate().readme
        assert "cfn-provider-<digest>.zip" in readme
        # A recipient tidying the bucket would break exactly the rollback that needs
        # the previous object, so the README has to say so.
        assert "rollback" in readme.lower()


class TestDependencyBundleIntegrity:
    """The bundle is the larger half of what runs, and it was merged on trust.

    ``agent.py`` has been digest-checked since the source-integrity work above, but
    the dependency bundle sitting next to it in the same bucket — every third-party
    package the agent imports, all of it executing under the runtime role — was
    downloaded and merged with no check at all. ARCC's artifact-management standard
    names this directly (CWE-494, Download of Code Without Integrity Check) and
    requires the *ability* to verify a checksum; these tests pin that ability and,
    separately, pin the honesty of the case where no digest is available.
    """

    @ALL_COMBINATIONS
    def test_every_code_package_states_a_bundle_digest(self, combo):
        template = _template(**combo)
        packages = {
            logical_id: r for logical_id, r in template["Resources"].items() if r["Type"] == "Custom::AgentCodePackage"
        }
        assert packages, "no combination should package code without a code package resource"
        for logical_id, resource in packages.items():
            assert resource["Properties"]["BundleDigest"] == {"Ref": "DependencyBundleDigest"}, logical_id

    def test_the_parameter_only_accepts_none_or_a_sha256(self):
        """`none` has to be spelled out. An empty string would look like a digest
        that simply had not been filled in, and nothing would ever say otherwise."""
        spec = _template()["Parameters"]["DependencyBundleDigest"]
        assert spec["Default"] == "none"
        pattern = spec["AllowedPattern"]
        assert re.fullmatch(pattern, "none")
        assert re.fullmatch(pattern, "sha256:" + "a" * 64)
        assert not re.fullmatch(pattern, "")
        assert not re.fullmatch(pattern, "sha256:" + "a" * 63)
        assert not re.fullmatch(pattern, "a" * 64), "a bare hash could be any algorithm"

    def test_the_lambda_verifies_the_bytes_the_deploy_script_hashed(self):
        """Whole archive, not members: the bundle is uploaded exactly as built."""
        handler = _import_cfn_provider_handler()
        bundle_bytes = b"PK\x03\x04 pretend this is a 90MB wheel bundle"
        digest = "sha256:" + hashlib.sha256(bundle_bytes).hexdigest()
        handler._verify_bundle_digest(bundle_bytes, digest)  # must not raise

    def test_a_substituted_bundle_is_refused_before_it_is_merged(self):
        handler = _import_cfn_provider_handler()
        original = b"PK\x03\x04 the bundle the deploy uploaded"
        digest = "sha256:" + hashlib.sha256(original).hexdigest()
        with pytest.raises(ValueError) as exc:
            handler._verify_bundle_digest(original + b" plus something else", digest)
        assert "has NOT been deployed" in str(exc.value)

    @pytest.mark.parametrize("absent", ["", "none"])
    def test_no_digest_warns_rather_than_failing(self, absent, caplog):
        """A recipient whose platform team pre-staged the bundle has nothing to hash.

        Failing that deploy would be wrong, but so would a log line that reads like
        the bundle was checked — so the warning has to say it was not.
        """
        handler = _import_cfn_provider_handler()
        with caplog.at_level(logging.WARNING):
            handler._verify_bundle_digest(b"anything at all", absent)
        assert "WITHOUT an integrity check" in caplog.text

    def test_the_bash_hash_and_the_lambda_hash_agree(self, tmp_path):
        """Two implementations of one value, as with the source digest.

        deploy.sh hashes the local file with whichever of sha256sum/shasum the
        operator's machine has; the Lambda hashes the downloaded object in Python. A
        disagreement here would not fail safe — it would fail *every* deploy, which
        is how a check like this gets switched off in a hurry.
        """
        handler = _import_cfn_provider_handler()
        helper = re.search(r"^sha256_stdin\(\).*?^\}$", _generate().deploy_sh, re.MULTILINE | re.DOTALL)
        assert helper, "deploy.sh no longer defines sha256_stdin"

        blob = tmp_path / "bundle.zip"
        blob.write_bytes(os.urandom(8192))
        proc = subprocess.run(
            ["bash", "-c", f'{helper.group(0)}\nprintf "sha256:%s" "$(sha256_stdin <"{blob}")"'],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        handler._verify_bundle_digest(blob.read_bytes(), proc.stdout.strip())  # must not raise

    def test_the_deploy_script_hashes_the_bundle_it_uploads(self):
        script = _generate().deploy_sh
        assert 'BUNDLE_DIGEST="sha256:$(sha256_stdin <"$BUNDLE_FILE")"' in script
        assert '"DependencyBundleDigest=$BUNDLE_DIGEST"' in script
        assert "DEPENDENCY_BUNDLE_DIGEST" in script, "no way to pin the digest by hand"

    def test_the_digest_is_omitted_rather_than_sent_as_none(self):
        """`cloudformation deploy` keeps a parameter it is not given (UsePreviousValue),
        so omitting it means a stack that recorded a digest keeps checking it. Sending
        "none" would silently switch the check off on the next deploy."""
        script = _generate().deploy_sh
        assert 'if [[ -n "$BUNDLE_DIGEST" ]]; then' in script
        assert "DependencyBundleDigest=none" not in script

    def test_the_readme_says_where_the_digest_comes_from(self):
        readme = _generate().readme
        assert "DependencyBundleDigest" in readme
        assert "DEPENDENCY_BUNDLE_DIGEST" in readme, "the README must say how to pin it"

    def test_the_bundle_digest_is_checked_before_the_merge(self):
        """Order matters: the merge builds the output on top of the bundle's bytes,
        so a bundle verified afterwards is already at the output key."""
        source = (CFN_PROVIDER_DIR / "handler.py").read_text()
        body = source.split("def _handle_code_package_create_update")[1]
        assert body.index("_verify_bundle_digest(") < body.index("_merge_code_and_deps(")


# ---------------------------------------------------------------------------
# The Knowledge Base vector store must be a store the stack can actually reach
# ---------------------------------------------------------------------------


class TestKnowledgeBaseVectorStore:
    """A Knowledge Base export has to bring its own vector store.

    The generator used to emit ``StorageConfiguration: {Type: S3_VECTORS}`` and
    nothing else. That is not valid CloudFormation — the schema is a oneOf over
    the seven vector stores and requires the matching nested block — so *every*
    exported Knowledge Base stack failed validation before creating a resource.
    The lint gate in this file is what caught it.

    It is not only a schema problem. Bedrock does not auto-provision an S3 Vectors
    bucket (lessons Bug 145); the live path creates the bucket and index by API
    call first. A template has no imperative step, so it must declare them.
    """

    def _kb(self, kb_config):
        return _template(gateway_config=AGENTCORE_GATEWAY, knowledge_base_config=kb_config)

    def test_storage_configuration_carries_the_nested_block(self):
        storage = self._kb(KB_CONFIG)["Resources"]["BedrockKnowledgeBase"]["Properties"]["StorageConfiguration"]
        assert storage["Type"] == "S3_VECTORS"
        assert "S3VectorsConfiguration" in storage, "bare Type is rejected by CloudFormation"

    def test_the_stack_creates_the_bucket_and_index_it_points_at(self):
        resources = self._kb(KB_CONFIG)["Resources"]
        assert resources["KBVectorBucket"]["Type"] == "AWS::S3Vectors::VectorBucket"
        assert resources["KBVectorIndex"]["Type"] == "AWS::S3Vectors::Index"
        storage = resources["BedrockKnowledgeBase"]["Properties"]["StorageConfiguration"]
        assert storage["S3VectorsConfiguration"] == {"IndexArn": {"Fn::GetAtt": ["KBVectorIndex", "IndexArn"]}}

    def test_the_vector_bucket_name_is_always_valid_and_unique(self):
        """Vector bucket names are lowercase-only and unique per account+region.

        ``DeploymentName`` permits uppercase (AllowedPattern ^[a-zA-Z]...), so
        interpolating it would produce a name S3 Vectors rejects. The stack id's
        GUID is lowercase and unique per stack.
        """
        name = self._kb(KB_CONFIG)["Resources"]["KBVectorBucket"]["Properties"]["VectorBucketName"]
        joined = json.dumps(name)
        assert "DeploymentName" not in joined and "StackName" not in joined
        assert "AWS::StackId" in joined

    def test_the_index_dimension_matches_the_embedding_model(self):
        """A mismatched Dimension fails at the first vector write, not at deploy."""
        v1 = self._kb({**KB_CONFIG, "embeddingModelId": "amazon.titan-embed-text-v1"})
        assert v1["Resources"]["KBVectorIndex"]["Properties"]["Dimension"] == 1536
        v2 = self._kb({**KB_CONFIG, "embeddingModelId": "amazon.titan-embed-text-v2:0"})
        assert v2["Resources"]["KBVectorIndex"]["Properties"]["Dimension"] == 1024

    def test_the_index_name_matches_the_live_deploy_default(self):
        """A different index name does not fail — it silently retrieves nothing."""
        from app.step_handlers.knowledge_base_step import _build_storage_config

        live = _build_storage_config({"vectorStoreType": "s3_vectors"})
        assert (
            self._kb(KB_CONFIG)["Resources"]["KBVectorIndex"]["Properties"]["IndexName"]
            == live["s3VectorsConfiguration"]["indexName"]
        )

    def test_an_existing_index_arn_is_used_as_is(self):
        arn = "arn:aws:s3vectors:us-east-1:123456789012:bucket/mine/index/myidx"
        resources = self._kb({**KB_CONFIG, "s3VectorsIndexArn": arn})["Resources"]
        assert "KBVectorBucket" not in resources, "must not create a store the canvas already has"
        storage = resources["BedrockKnowledgeBase"]["Properties"]["StorageConfiguration"]
        assert storage["S3VectorsConfiguration"] == {"IndexArn": arn}

    def test_an_existing_bucket_arn_is_paired_with_an_index_name(self):
        """The other half of the S3VectorsConfiguration oneOf: bucket AND index name."""
        arn = "arn:aws:s3vectors:us-east-1:123456789012:bucket/mine"
        resources = self._kb({**KB_CONFIG, "s3VectorsBucketArn": arn, "s3VectorsIndexName": "myidx"})["Resources"]
        assert "KBVectorBucket" not in resources
        assert resources["BedrockKnowledgeBase"]["Properties"]["StorageConfiguration"]["S3VectorsConfiguration"] == {
            "VectorBucketArn": arn,
            "IndexName": "myidx",
        }

    def test_the_kb_role_can_reach_the_store(self):
        """Missing s3vectors grants surface as "unable to assume the given role".

        Which is why the sub-resource ARN matters as much as the bucket ARN
        (lessons Bug 78 and Bug 84) — and why this asserts both.
        """
        resources = self._kb(KB_CONFIG)["Resources"]
        statements = resources["KnowledgeBaseRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        vector_statements = [s for s in statements if any("s3vectors:" in a for a in s["Action"])]
        assert vector_statements, "the KB role has no access to its own vector store"
        scoped = next(s for s in vector_statements if s["Resource"] != "*")
        assert "s3vectors:QueryVectors" in scoped["Action"]
        rendered = json.dumps(scoped["Resource"])
        assert "KBVectorBucket" in rendered, "grant must point at the bucket this stack creates"
        assert "/index/*" in rendered, "several verbs act on the index sub-resource"
        assert "s3vectors:CreateVectorBucket" not in json.dumps(statements), (
            "the stack creates the store, so the role never needs to"
        )

    def test_opensearch_without_a_collection_is_refused(self):
        """The live path provisions the collection AND its index by API call.

        The index lives on the OpenSearch data plane, which CloudFormation cannot
        reach, so an exported stack would fail inside Bedrock's validation.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                knowledge_base_config={**KB_CONFIG, "vectorStoreType": "opensearch_serverless"},
            )
        assert "collection ARN" in str(exc.value)
        assert "S3 Vectors" in str(exc.value), "must name the store that does work"

    def test_opensearch_with_a_collection_exports(self):
        resources = self._kb(KB_CONFIG_OPENSEARCH)["Resources"]
        storage = resources["BedrockKnowledgeBase"]["Properties"]["StorageConfiguration"]
        assert storage["Type"] == "OPENSEARCH_SERVERLESS"
        assert (
            storage["OpensearchServerlessConfiguration"]["CollectionArn"]
            == (KB_CONFIG_OPENSEARCH["opensearchCollectionArn"])
        )
        assert "KBVectorBucket" not in resources

    def test_no_vector_store_grant_is_left_on_a_wildcard(self):
        """The `"*"` fallbacks these grants used to carry are unreachable now."""
        statements = self._kb(KB_CONFIG_OPENSEARCH)["Resources"]["KnowledgeBaseRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        aoss = next(s for s in statements if any(a.startswith("aoss:") for a in s["Action"]))
        assert aoss["Resource"] != "*"

    def test_rds_without_a_cluster_is_refused(self):
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                knowledge_base_config={**KB_CONFIG, "vectorStoreType": "rds"},
            )
        assert "cluster ARN" in str(exc.value)

    def test_web_crawler_on_a_non_opensearch_store_is_refused(self):
        """Same rule the live path enforces (lessons Bug 186), enforced earlier."""
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                knowledge_base_config={
                    **KB_CONFIG,
                    "dataSourceType": "web_crawler",
                    "webCrawlerUrl": "https://docs.example.com",
                },
            )
        assert "Web Crawler" in str(exc.value)
        assert "OpenSearch Serverless" in str(exc.value)

    @pytest.mark.parametrize("bucket", [None, "", "   "], ids=["absent", "empty", "whitespace"])
    def test_an_s3_data_source_with_no_bucket_is_refused(self, bucket):
        """Two silent defaults used to cover for this, each wrong in its own direction.

        The IAM statement fell back to ``arn:aws:s3:::*``, which granted the Knowledge
        Base role ``s3:GetObject`` and ``s3:ListBucket`` on every bucket in the account
        — the widest grant the export was capable of emitting, against ARCC
        cnt_AGx9pUNpmdOVZB. The data source itself fell back to ``s3://my-bucket/``, a
        bucket the recipient does not own. So the stack either failed on an
        unrecognised bucket or ingested nothing, and left the account-wide grant behind
        either way. Refusing at export is the only outcome that is not one of those two.
        """
        with pytest.raises(CfnExportUnsupportedError) as exc:
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                knowledge_base_config={k: v for k, v in KB_CONFIG.items() if k != "s3BucketUri"}
                | ({} if bucket is None else {"s3BucketUri": bucket}),
            )
        assert "s3://" in str(exc.value), "the message must show the shape we want"

    def test_no_kb_grant_is_account_wide(self):
        """The rule, not the case: no statement on the KB role may name every bucket."""
        statements = self._kb(KB_CONFIG)["Resources"]["KnowledgeBaseRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        rendered = json.dumps(statements)
        assert "arn:aws:s3:::*" not in rendered
        assert '"arn:${AWS::Partition}:s3:::*"' not in rendered


# ---------------------------------------------------------------------------
# Deleting the stack must not delete the data
# ---------------------------------------------------------------------------


class TestDataRetention:
    """ARCC cnt_h02wszR9St529D: storage-like resources need BOTH DeletionPolicy
    and UpdateReplacePolicy.

    The emitted template previously had neither, on any resource. Under a
    Terraform ``aws_cloudformation_stack`` wrapper a single ``terraform destroy``
    is a stack delete, so a Cognito user pool full of real user identities and an
    AgentCore Memory full of conversation history went with it.
    """

    @ALL_COMBINATIONS
    def test_every_data_bearing_resource_carries_both_attributes(self, combo):
        resources = _template(**combo)["Resources"]
        data_resources = {k: v for k, v in resources.items() if v["Type"] in DATA_BEARING_RESOURCE_TYPES}
        for logical_id, resource in data_resources.items():
            assert resource.get("DeletionPolicy") == "Retain", f"{logical_id} lacks DeletionPolicy"
            assert resource.get("UpdateReplacePolicy") == "Retain", (
                f"{logical_id} lacks UpdateReplacePolicy — a replacing update destroys data "
                "just as thoroughly as a delete, and this is the half everyone forgets"
            )

    def test_retain_is_the_default_without_being_asked(self):
        """A caller who says nothing must get the safe behaviour."""
        resources = _template(gateway_config=AGENTCORE_GATEWAY, memory_config={"enabled": True})["Resources"]
        assert resources["CognitoUserPool"]["DeletionPolicy"] == "Retain"
        assert resources["AgentCoreMemory"]["DeletionPolicy"] == "Retain"

    def test_delete_is_available_for_throwaway_stacks(self):
        resources = _template(
            gateway_config=AGENTCORE_GATEWAY,
            memory_config={"enabled": True},
            dataRetentionPolicy="Delete",
        )["Resources"]
        assert resources["CognitoUserPool"]["DeletionPolicy"] == "Delete"
        assert resources["CognitoUserPool"]["UpdateReplacePolicy"] == "Delete"

    @ALL_COMBINATIONS
    def test_non_data_resources_are_not_stamped(self, combo):
        """Retaining an IAM role or a Lambda leaves litter and breaks redeploys.

        One deliberate exception, and it is allowed here only because it is declared
        in DELETION_DEPENDENCIES rather than stamped ad hoc: a retained knowledge base
        cannot purge its vectors without KnowledgeBaseRole, so retaining the data and
        not the role leaves the operator a resource they can never delete. Litter is
        the lesser harm. ``test_a_deletion_dependency_is_retained_only_alongside_its_
        data_resource`` pins the narrowness.
        """
        allowed = {dep for deps in DELETION_DEPENDENCIES.values() for dep in deps}
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] not in DATA_BEARING_RESOURCE_TYPES and logical_id not in allowed:
                assert "DeletionPolicy" not in resource, f"{logical_id} ({resource['Type']}) wrongly retained"
                assert "UpdateReplacePolicy" not in resource, f"{logical_id} wrongly retained"

    def test_a_deletion_dependency_is_retained_only_alongside_its_data_resource(self):
        """The narrow contract: retained with Retain, never retained with Delete.

        Under Delete the knowledge base goes with the stack, so nothing needs to
        outlive it and a surviving IAM role would be a gratuitous orphan. Probed by
        reverting the fix: without it the Retain case leaves the role unstamped and
        the retained KB is undeletable, which is the live-proven defect
        (DELETE_UNSUCCESSFUL, "Unable to delete data from vector store").
        """
        combo = COMPONENT_COMBINATIONS["gateway+kb"]
        retained = _template(**combo, data_retention_policy="Retain")["Resources"]
        assert retained["BedrockKnowledgeBase"]["DeletionPolicy"] == "Retain", "precondition"
        assert retained["KnowledgeBaseRole"]["DeletionPolicy"] == "Retain", (
            "a retained knowledge base purges its vectors with this role; without it "
            "the knowledge base can never be deleted"
        )
        assert retained["KnowledgeBaseRole"]["UpdateReplacePolicy"] == "Retain"

        deleted = _template(**combo, data_retention_policy="Delete")["Resources"]
        assert deleted["BedrockKnowledgeBase"]["DeletionPolicy"] == "Delete", "precondition"
        assert "DeletionPolicy" not in deleted["KnowledgeBaseRole"], (
            "under Delete the knowledge base goes with the stack, so retaining the role only leaves an orphan behind"
        )

    def test_a_combination_with_no_knowledge_base_retains_no_role(self):
        """The dependency is keyed on the data resource being present, not on the combo."""
        resources = _template(**COMPONENT_COMBINATIONS["gateway"], data_retention_policy="Retain")["Resources"]
        assert "BedrockKnowledgeBase" not in resources, "precondition: this combo has no KB"
        assert "KnowledgeBaseRole" not in resources

    def test_the_teardown_notice_does_not_tell_the_operator_to_build_a_wildcard_role(self):
        """It used to hand over an ``s3vectors:*`` on ``*`` create-role recipe.

        That was a workaround for the role not being retained. Now that it is, the
        recipe is both unnecessary and the worst part of the notice — ARCC
        cnt_SFJJhkOueCPRkd is specifically about not leaving high-privilege wildcards
        unconditioned, and a documented workaround that ends in one is still one.
        """
        script = _generate(**COMPONENT_COMBINATIONS["gateway+kb"], data_retention_policy="Retain").teardown_sh
        assert "iam create-role" not in script, "teardown still tells the operator to recreate the role"
        assert "s3vectors:*" not in script
        # It must still say what the real constraint is, or the role becomes the thing
        # that wedges the delete instead.
        assert "AgentCoreKBRole-$STACK_NAME" in script
        assert "last" in script.lower()

    def test_the_notice_names_the_delete_that_actually_purges(self):
        """The purge runs at the DATA SOURCE delete, which was measured, not assumed.

        ``list-vectors`` went 1 -> 0 on ``delete-data-source``, before the knowledge
        base was touched at all; the knowledge base re-attempts the same purge on its
        own delete, so both fail when the role is missing. The order the notice gives
        is right either way — but an operator told only that "the knowledge base purges
        its vectors" goes and debugs ``delete-knowledge-base``, which is the one place
        the answer is not.
        """
        script = _generate(**COMPONENT_COMBINATIONS["gateway+kb"], data_retention_policy="Retain").teardown_sh
        readme = _generate(**COMPONENT_COMBINATIONS["gateway+kb"], data_retention_policy="Retain").readme

        # Short substrings on purpose: both documents are hard-wrapped, and "DATA
        # SOURCE is deleted" straddles a newline in the notice. A phrase that spans a
        # line break fails for layout reasons and tells you nothing about the content.
        assert "SOURCE is deleted" in script, "the notice still blames the wrong delete"
        assert "DATA SOURCE first" in script
        assert "*data source* is deleted" in readme

    def test_the_teardown_notice_resolves_the_role_name_rather_than_assuming_it(self):
        """``UseExplicitRoleNames=false`` means the readable name is a guess.

        The default is ``true``, which yields ``AgentCoreKBRole-<stack>`` — so a
        hardcoded name is right in the common case and silently wrong in exactly the
        accounts that opted out because their naming rules forbade it. Since the notice
        prints while the stack still exists, it can ask CloudFormation for the physical
        id instead of guessing, and fall back to the readable form only if that fails.
        """
        script = _generate(**COMPONENT_COMBINATIONS["gateway+kb"], data_retention_policy="Retain").teardown_sh

        assert "describe-stack-resource" in script, "the notice still assumes the role name"
        assert "--logical-resource-id KnowledgeBaseRole" in script
        assert "StackResourceDetail.PhysicalResourceId" in script
        # The readable name must survive only as the fallback, not as the claim.
        assert 'KB_ROLE_NAME="AgentCoreKBRole-$STACK_NAME"' in script

    def test_the_teardown_notice_omits_the_role_paragraph_without_a_knowledge_base(self):
        """It names one role and gives an ordering rule that exists for its sake alone.

        Printed to a stack with no knowledge base it would be noise at best, and the
        resolution step above would run a describe-stack-resource for a logical id the
        stack does not have.
        """
        no_kb = _generate(**COMPONENT_COMBINATIONS["gateway"], data_retention_policy="Retain").teardown_sh

        assert "Retain" in no_kb, "precondition: this combo still retains something"
        assert "ONE ORDERING RULE MATTERS" not in no_kb
        assert "KnowledgeBaseRole" not in no_kb
        # The part that applies to every retained stack must still be there.
        assert "resourcegroupstaggingapi get-resources" in no_kb

    def test_the_readme_mentions_the_retained_role_only_when_there_is_one(self):
        """A paragraph about a resource the recipient does not have is worse than none.

        The same mistake this notice already made once, when it told a runtime-only
        export that its retained Lambda log group held user identities: a warning that
        is wrong once is a warning the recipient stops reading.
        """
        marker = "One IAM role is retained with them"
        with_kb = _generate(**COMPONENT_COMBINATIONS["gateway+kb"], data_retention_policy="Retain").readme
        assert marker in with_kb
        assert "AgentCoreKBRole-<stack-name>" in with_kb

        without_kb = _generate(**COMPONENT_COMBINATIONS["gateway"], data_retention_policy="Retain").readme
        assert "Retain" in without_kb, "precondition: this combo still retains something"
        assert marker not in without_kb, "promised a retained role to a stack that has none"

    def test_the_type_list_covers_every_data_bearing_type_actually_emitted(self):
        """Guard against the set going stale.

        Keyed on type rather than logical id precisely so that adding a second
        Cognito pool (which this template does — gateway and MCP server) cannot
        escape protection. If a future change emits a new storage type, this test
        is the one that should fail.
        """
        emitted = {r["Type"] for r in _template(**COMPONENT_COMBINATIONS["everything"])["Resources"].values()}
        known_storage_shaped = {
            t
            for t in emitted
            if any(word in t for word in ("UserPool", "KnowledgeBase", "DataSource", "Memory", "Vector"))
            # UserPoolClient/Domain/ResourceServer and VectorBucketPolicy are
            # configuration attached to a store, not stores of record: they hold no
            # data that survives it and are cheap to recreate.
            and not any(
                t.endswith(suffix)
                for suffix in (
                    "UserPoolClient",
                    "UserPoolDomain",
                    "UserPoolResourceServer",
                    "VectorBucketPolicy",
                )
            )
        }
        assert known_storage_shaped <= DATA_BEARING_RESOURCE_TYPES, (
            f"these emitted types look data-bearing but are unprotected: "
            f"{known_storage_shaped - DATA_BEARING_RESOURCE_TYPES}"
        )

    def test_the_known_data_resources_are_all_covered(self):
        """Regression pin on the specific resources found unprotected."""
        resources = _template(**COMPONENT_COMBINATIONS["everything"])["Resources"]
        protected = {k for k, v in resources.items() if v.get("DeletionPolicy")}
        assert protected == {
            "CognitoUserPool",
            "McpCognitoUserPool",
            "BedrockKnowledgeBase",
            "KBDataSource",
            "AgentCoreMemory",
            # The embeddings, and the index whose every property is createOnly —
            # any edit to it is a replacement, which is why UpdateReplacePolicy
            # matters as much as DeletionPolicy here.
            "KBVectorBucket",
            "KBVectorIndex",
            # The Lambda log groups. Data-bearing for the same reason as the rest:
            # they hold what the agent was asked and what it answered.
            "CfnProviderLambdaLogGroup",
            "KBToolLambdaLogGroup",
            # Not data — the role a retained knowledge base needs in order to purge
            # its vectors and be deletable at all. Listed here rather than filtered
            # out because this assertion is a pin on what actually carries a
            # DeletionPolicy, and quietly excluding a category is how the pin would
            # stop noticing a resource that gets retained by accident.
            "KnowledgeBaseRole",
        }, protected


# ---------------------------------------------------------------------------
# Cognito: the two emitted pools
# ---------------------------------------------------------------------------


def _cognito_domains(template):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::Cognito::UserPoolDomain"}


def _cognito_clients(template):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::Cognito::UserPoolClient"}


def _cognito_pools(template):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::Cognito::UserPool"}


COGNITO_COMBINATIONS = pytest.mark.parametrize(
    "combo",
    [c for k, c in COMPONENT_COMBINATIONS.items() if k != "runtime-only" and k != "memory"],
    ids=[k for k in COMPONENT_COMBINATIONS if k != "runtime-only" and k != "memory"],
)


class TestCognitoPoolsAreHardened:
    """Three separate defects in the two emitted pools.

    Both are pure client-credentials M2M pools, so MFA and token revocation are
    not the levers here — nothing holds a refresh token and no human ever signs
    in. What did matter: the hosted-UI domain published the AWS account id to
    anyone who could resolve a name, the access token lifetime was left to
    Cognito's implicit default, and a pool carrying ``DeletionPolicy: Retain``
    had no Cognito-side guard of its own.
    """

    def test_deletion_protection_agrees_with_the_retention_policy(self):
        """These two MUST be stamped from the same knob.

        ``DeletionProtection: ACTIVE`` makes ``DeleteUserPool`` fail outright.
        Pair it with ``DeletionPolicy: Delete`` and the stack deadlocks its own
        teardown on the resource it was told to remove; pair ``INACTIVE`` with
        ``Retain`` and the pool is retained but unguarded against a later manual
        delete. Hardcoding either one is how that mismatch happens.
        """
        for retention, expected in (("Retain", "ACTIVE"), ("Delete", "INACTIVE")):
            template = _template(**COMPONENT_COMBINATIONS["everything"], dataRetentionPolicy=retention)
            pools = _cognito_pools(template)
            assert pools, "this combination emits no user pool; the test is vacuous"
            for logical_id, pool in pools.items():
                assert pool["DeletionPolicy"] == retention, logical_id
                assert pool["Properties"]["DeletionProtection"] == expected, (
                    f"{logical_id} has DeletionPolicy {retention} but DeletionProtection "
                    f"{pool['Properties'].get('DeletionProtection')}; teardown cannot succeed"
                )

    def test_the_default_pool_is_protected(self):
        """A caller who says nothing gets the guard, not the hole."""
        pool = _template(gateway_config=AGENTCORE_GATEWAY)["Resources"]["CognitoUserPool"]
        assert pool["Properties"]["DeletionProtection"] == "ACTIVE"

    @COGNITO_COMBINATIONS
    def test_no_hosted_ui_domain_publishes_the_account_id(self, combo):
        """The domain is world-resolvable; the account id is not public data.

        ``ac-${DeploymentName}-${AWS::AccountId}`` put the account id into a DNS
        name that anyone could resolve, and it is exactly the identifier an
        attacker wants first when enumerating a target. It also failed at the job
        it was doing: two stacks with the same DeploymentName in one account
        derived the *same* domain and the second create failed on a collision.
        """
        template = _template(**combo)
        domains = _cognito_domains(template)
        assert domains, "this combination emits no hosted-UI domain; the parametrization is stale"
        for logical_id, domain in domains.items():
            rendered = json.dumps(domain["Properties"]["Domain"])
            assert "AWS::AccountId" not in rendered, f"{logical_id} publishes the account id: {rendered}"

    def test_the_domain_is_unique_per_stack_without_the_account_id(self):
        """Uniqueness now comes from this stack's own id.

        ``AWS::StackId`` ends in a UUID whose last field is 12 hex characters,
        unique per stack and meaningless outside it. Two stacks with the same
        DeploymentName in one account now get different domains, which is what
        the account id was mistakenly there to do.
        """
        domain = _template(gateway_config=AGENTCORE_GATEWAY)["Resources"]["CognitoUserPoolDomain"]
        unpinned = domain["Properties"]["Domain"]["Fn::If"][2]
        assert "AWS::StackId" in json.dumps(unpinned), unpinned

    def test_an_existing_domain_can_be_pinned_across_the_change(self):
        """Domain is createOnly: changing it REPLACES the domain.

        A stack created before this change has a live token endpoint ending in
        the account id, and callers are configured against it. Replacing it
        silently would break them, so the old value has to be settable. The
        parameter must therefore accept an AWS account id, which is 12 digits.
        """
        template = _template(gateway_config=AGENTCORE_GATEWAY)
        spec = template["Parameters"]["CognitoDomainSuffix"]
        assert spec["Default"] == "", "the safe, account-id-free form must be what you get by default"
        assert re.fullmatch(spec["AllowedPattern"], "123456789012"), (
            "an upgrading stack must be able to pin its old account-id suffix"
        )
        assert re.fullmatch(spec["AllowedPattern"], ""), "empty must stay valid; it is the default"
        assert not re.fullmatch(spec["AllowedPattern"], "1234567890123"), "13 characters would overflow the 63-char cap"

        pinned = template["Resources"]["CognitoUserPoolDomain"]["Properties"]["Domain"]["Fn::If"][1]
        assert pinned == {"Fn::Sub": "ac-${DeploymentName}-${CognitoDomainSuffix}"}, pinned

    def test_the_worst_case_domain_fits_the_63_character_cap(self):
        """Derived from DeploymentName's own constraint so it cannot go stale.

        A Cognito hosted-UI domain prefix is capped at 63 characters. The longest
        emitted prefix is ``ac-mcp-`` plus a maximum-length DeploymentName plus a
        hyphen plus the 12-character suffix. If someone widens DeploymentName,
        this is the test that should fail rather than a customer's stack.
        """
        pattern = _template()["Parameters"]["DeploymentName"]["AllowedPattern"]
        repeat = re.search(r"\{0,(\d+)\}", pattern)
        assert repeat, f"DeploymentName's pattern no longer states a length bound: {pattern}"
        max_name = int(repeat.group(1)) + 1  # the leading single-character class
        longest = len("ac-mcp-") + max_name + len("-") + 12
        assert longest <= 63, f"worst-case domain prefix is {longest} characters, over Cognito's 63-character cap"

    @COGNITO_COMBINATIONS
    def test_every_client_states_both_the_token_lifetime_and_its_unit(self, combo):
        """The unit is not optional in practice.

        ``AccessTokenValidity`` without ``TokenValidityUnits`` is read by Cognito
        as HOURS. A value written to mean 15 minutes becomes 15 hours — longer
        than the 60-minute default it was meant to shorten. That is the failure
        mode this asserts against, which is why the unit is checked per client
        rather than once.
        """
        clients = _cognito_clients(_template(**combo))
        assert clients, "this combination emits no user pool client; the parametrization is stale"
        for logical_id, client in clients.items():
            properties = client["Properties"]
            assert properties.get("AccessTokenValidity") == {"Ref": "AccessTokenValidityMinutes"}, logical_id
            assert properties.get("TokenValidityUnits") == {"AccessToken": "minutes"}, (
                f"{logical_id} sets a lifetime with no unit; Cognito would read it as hours"
            )

    def test_the_lifetime_parameter_is_bounded_and_defaults_to_no_change(self):
        spec = _template(gateway_config=AGENTCORE_GATEWAY)["Parameters"]["AccessTokenValidityMinutes"]
        assert spec["Type"] == "Number"
        assert spec["Default"] == 60, "60 is Cognito's own default; changing it silently would change behaviour"
        assert spec["MinValue"] >= 5, "a lifetime short enough to expire mid-call is an outage, not hardening"
        assert spec["MaxValue"] <= 1440, "Cognito rejects an access token lifetime over 24 hours"

    def test_both_pools_share_one_set_of_parameters(self):
        """The MCP pool and the gateway pool are added by separate code paths.

        Each calls the same idempotent parameter helper, so a template with both
        must end up with one copy of each parameter and one condition — not a
        duplicate, and not a second condition name.
        """
        template = _template(**COMPONENT_COMBINATIONS["everything"])
        assert len(_cognito_pools(template)) == 2, "this test is about the two-pool case"
        assert "CognitoDomainSuffix" in template["Parameters"]
        assert "AccessTokenValidityMinutes" in template["Parameters"]
        assert [c for c in template["Conditions"] if "CognitoDomain" in c] == ["HasCognitoDomainSuffix"]

    def test_no_cognito_parameters_when_there_is_no_pool(self):
        """A runtime-only export must not grow knobs it cannot act on."""
        template = _template()
        assert not _cognito_pools(template), "runtime-only should emit no pool"
        assert "CognitoDomainSuffix" not in template["Parameters"]
        assert "AccessTokenValidityMinutes" not in template["Parameters"]
        assert "HasCognitoDomainSuffix" not in template.get("Conditions", {})


# ---------------------------------------------------------------------------
# Role governance: permissions boundary and optional role names
# ---------------------------------------------------------------------------


def _roles(template):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}


def _inline_policies(role):
    """Every inline policy on *role*, including the conditional ones.

    An entry in ``Policies`` may be a policy or an ``Fn::If`` that resolves to one
    or to ``AWS::NoValue`` — that is how the customer-managed-key grant is attached
    only when a key is supplied. Unconditional readers would raise ``KeyError`` on
    the wrapper and, worse, a reader that skipped it would stop checking the
    conditional policies for wildcards. So unwrap, never skip.
    """
    for entry in role.get("Properties", {}).get("Policies", []):
        branches = entry.get("Fn::If")
        if branches is None:
            yield entry
            continue
        for branch in branches[1:]:
            if isinstance(branch, dict) and "PolicyDocument" in branch:
                yield branch


class TestRoleGovernance:
    """ARCC cnt_AGx9pUNpmdOVZB: the stack creates its own roles, so the account
    needs a way to bound them.

    Neither knob existed. Every role was unbounded, and eight of them set an
    explicit ``RoleName``, so the stack could only ever be deployed with
    ``CAPABILITY_NAMED_IAM`` — which plenty of regulated accounts deny outright,
    leaving the recipient with no deploy at all rather than a constrained one.
    """

    @ALL_COMBINATIONS
    def test_every_role_is_boundary_aware(self, combo):
        template = _template(**combo)
        roles = _roles(template)
        assert roles, "no roles emitted at all — the assertion below would pass vacuously"
        for logical_id, role in roles.items():
            boundary = role["Properties"].get("PermissionsBoundary")
            assert boundary is not None, f"{logical_id} cannot be bounded"
            assert boundary == {
                "Fn::If": ["HasPermissionsBoundary", {"Ref": "PermissionsBoundaryArn"}, {"Ref": "AWS::NoValue"}]
            }, f"{logical_id} has a PermissionsBoundary that is not the optional form: {boundary}"

    @ALL_COMBINATIONS
    def test_no_role_name_is_mandatory(self, combo):
        """A bare RoleName string is what forces CAPABILITY_NAMED_IAM."""
        for logical_id, role in _roles(_template(**combo)).items():
            name = role["Properties"].get("RoleName")
            if name is None:
                continue  # CloudFormation names it; already fine
            assert isinstance(name, dict) and "Fn::If" in name, (
                f"{logical_id} pins RoleName unconditionally ({name!r}), so this template "
                "can never be deployed without CAPABILITY_NAMED_IAM"
            )
            condition, named, unnamed = name["Fn::If"]
            assert condition == "HasExplicitRoleNames"
            assert unnamed == {"Ref": "AWS::NoValue"}
            assert "Fn::Sub" in named, f"{logical_id} lost its readable name in the true branch"

    def test_readable_names_stay_the_default(self):
        """Audit-findable names are the point of the default; the knob is the exception."""
        template = _template(**COMPONENT_COMBINATIONS["everything"])
        assert template["Parameters"]["UseExplicitRoleNames"]["Default"] == "true"
        assert template["Parameters"]["UseExplicitRoleNames"]["AllowedValues"] == ["true", "false"]
        runtime_name = _roles(template)["RuntimeExecutionRole"]["Properties"]["RoleName"]
        assert runtime_name["Fn::If"][1] == {"Fn::Sub": "AgentCoreRuntime-${AWS::StackName}"}

    def test_no_boundary_is_the_default(self):
        """An empty default keeps every existing deploy working unchanged."""
        parameter = _template()["Parameters"]["PermissionsBoundaryArn"]
        assert parameter["Default"] == ""

    def test_boundary_parameter_rejects_things_that_are_not_policy_arns(self):
        """The pattern is the only thing standing between a typo and a confusing
        CreateRole failure deep into the deploy."""
        pattern = re.compile(_template()["Parameters"]["PermissionsBoundaryArn"]["AllowedPattern"])
        assert pattern.fullmatch("")
        assert pattern.fullmatch("arn:aws:iam::123456789012:policy/MyBoundary")
        assert pattern.fullmatch("arn:aws-us-gov:iam::123456789012:policy/path/MyBoundary")
        assert not pattern.fullmatch("MyBoundary")
        assert not pattern.fullmatch("arn:aws:iam::123456789012:role/MyBoundary")

    @ALL_COMBINATIONS
    def test_both_conditions_are_declared(self, combo):
        """An Fn::If naming an undeclared condition is a template that will not
        even validate, so this is the pin on the two being emitted together."""
        conditions = _template(**combo).get("Conditions", {})
        assert "HasPermissionsBoundary" in conditions
        assert "HasExplicitRoleNames" in conditions

    @ALL_COMBINATIONS
    def test_nothing_but_a_role_gets_a_boundary(self, combo):
        """PermissionsBoundary is a role property; on anything else it is a lint error."""
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                assert "PermissionsBoundary" not in resource.get("Properties", {}), (
                    f"{logical_id} ({resource['Type']}) has a PermissionsBoundary"
                )

    def test_every_role_the_largest_stack_creates_is_accounted_for(self):
        """Regression pin on the inventory: seven roles, six of them named."""
        roles = _roles(_template(**COMPONENT_COMBINATIONS["everything"]))
        assert len(roles) == 7, sorted(roles)
        named = {k for k, v in roles.items() if "RoleName" in v["Properties"]}
        assert len(named) == 6, sorted(named)

    def test_deploy_script_exposes_both_knobs(self):
        deploy = _generate(**COMPONENT_COMBINATIONS["everything"]).deploy_sh
        assert "PERMISSIONS_BOUNDARY_ARN" in deploy
        assert "PermissionsBoundaryArn=$PERMISSIONS_BOUNDARY_ARN" in deploy
        assert "UseExplicitRoleNames=false" in deploy

    def test_deploy_script_drops_named_iam_when_names_are_generated(self):
        """Offering the parameter while still demanding CAPABILITY_NAMED_IAM would
        be pointless: the capability is the thing the account denies."""
        deploy = _generate(**COMPONENT_COMBINATIONS["everything"]).deploy_sh
        assert 'CAPABILITIES="CAPABILITY_IAM"' in deploy
        assert '--capabilities "$CAPABILITIES"' in deploy
        assert "--capabilities CAPABILITY_NAMED_IAM" not in deploy

    def test_readme_tells_the_recipient_the_knobs_exist(self):
        readme = _generate(**COMPONENT_COMBINATIONS["everything"]).readme
        assert "PERMISSIONS_BOUNDARY_ARN=" in readme
        assert "USE_EXPLICIT_ROLE_NAMES=false" in readme
        assert "PermissionsBoundaryArn" in readme


# Every statement still allowed to say Resource "*", and the reason it survives.
# ARCC cnt_AGx9pUNpmdOVZB wants specific resources; where AWS offers no resource
# to be specific about, the answer is an exact action list in an isolated
# statement, not a widened one. Adding an entry here should require the same
# argument, which is the whole point of the allowlist being in the test.
WILDCARD_ALLOWLIST = {
    # Policy, credential-provider, token-vault, workload-identity and evaluation
    # ids are minted by AgentCore during stack create. There is nothing to name.
    ("CfnProviderRole", "AgentCorePolicyLifecycle"),
    ("CfnProviderRole", "OAuth2CredentialProviderLifecycle"),
    ("RuntimeExecutionRole", "EvaluationAccess"),
    ("EvaluationRole", "AgentCoreEvaluation"),
    ("GatewayRole", "CredentialProviderAccess"),
    ("GatewayRole", "PolicyEngineAuthorization"),
    # List/Describe calls that take no resource: authorized against "*" or not at
    # all. Read-only and metadata-only — but read-only across every tenant in the
    # account, which is why the runtime role's two are gone rather than listed here.
    # `ListGateways` and `ListMemories` were granted to the runtime on the belief that
    # the agent's MCP and memory clients discover their own resources; they do not, and
    # the grants let one tenant's agent enumerate every other tenant's. The two that
    # remain below are called by AWS services assuming these roles, not by code in this
    # repository, so they cannot be settled by reading it.
    ("GatewayRole", "AgentCoreGatewayDiscovery"),
    ("MemoryExecutionRole", "MemoryControlPlaneDiscovery"),
    ("KnowledgeBaseRole", "VectorBucketDiscovery"),
    ("EvaluationRole", "EvaluationLogsDiscovery"),
    # A CloudWatch query id does not exist until StartQuery returns it.
    ("RuntimeExecutionRole", "EvaluationInsightsResults"),
    # Sessions are created at runtime, and the built-in browser and interpreter
    # live under the service's own `aws` account, so even account-scoping denies
    # them. See the comments on these statements.
    ("RuntimeExecutionRole", "BrowserAccess"),
    ("RuntimeExecutionRole", "CodeInterpreterAccess"),
    # No guardrail is created by this template; the id arrives at runtime.
    ("RuntimeExecutionRole", "GuardrailsAccess"),
}


def _as_list(value):
    """A policy ``Resource``/``Action`` may be a single value or a list."""
    return value if isinstance(value, list) else [value]


class TestOutboundOauthIsGrantedAsAWholeCall:
    """Outbound OAuth is two calls, and the export granted one of them.

    Diagnosed live. The gateway mints a workload identity token with
    ``GetWorkloadAccessToken`` and only then exchanges it with
    ``GetResourceOauth2Token``. Missing the first, the gateway is denied before it
    issues any request, so an instrumented target sees *nothing* -- no 4xx to read,
    no log line. `tools/list` still worked, because the gateway serves it from its
    own stored catalogue without an outbound call, so discovery looked healthy and
    only `tools/call` failed. Both necessity and sufficiency were measured: adding
    this one action to the deployed role, changing nothing else, turned the failure
    into a 200 with the tool's real output.

    The reason it survived review is the thing these tests fix. The only assertion
    covering this statement was the tuple ``("GatewayRole",
    "CredentialProviderAccess")`` in WILDCARD_ALLOWLIST -- which pins the Sid's
    existence and says nothing about the actions inside it, so a least-privilege
    pass could drop one and stay green. Pinned below as an invariant between the two
    actions rather than as a literal action list, so it holds wherever the pair is
    granted rather than only in the statement that had the bug.
    """

    @staticmethod
    def _actions_by_role(template):
        actions = {}
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            granted = set()
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    if statement.get("Effect") == "Allow":
                        granted.update(_as_list(statement.get("Action", [])))
            actions[logical_id] = granted
        return actions

    @ALL_COMBINATIONS
    def test_a_role_that_can_exchange_a_token_can_also_mint_one(self, combo):
        by_role = self._actions_by_role(_template(**combo))
        exchangers = [r for r, a in by_role.items() if "bedrock-agentcore:GetResourceOauth2Token" in a]
        for role in exchangers:
            assert "bedrock-agentcore:GetWorkloadAccessToken" in by_role[role], (
                f"{role} can exchange a workload identity token but not mint one, so every "
                "outbound OAuth call it makes is denied before it leaves the gateway"
            )

    def test_the_mcp_server_export_grants_it(self):
        """The combination that actually exercises the OAuth path.

        Lambda-backed targets authorise with GATEWAY_IAM and never reach it, which is
        why the blast radius was this one combination and why no other test noticed.
        """
        by_role = self._actions_by_role(_template(**COMPONENT_COMBINATIONS["mcp-server"]))
        assert "bedrock-agentcore:GetWorkloadAccessToken" in by_role["GatewayRole"]

    def test_the_grant_names_both_arns_the_call_checks(self):
        """Neither line is belt-and-braces: the call checks both resources separately.

        Measured on a deployed stack by removing one line at a time from the live role
        and re-invoking, A/B/A/B/A. Parent removed, and the 403 names the parent
        directory; child wildcard removed instead, and the 403 names the workload
        identity. Restore either and the `tools/call` returns 200 with the tool's real
        output again. So a grant naming one of the two is as broken as no grant at all,
        and fails in the same invisible place -- discovery still works, and the target
        logs nothing because nothing reaches it.
        """
        statements = [
            statement
            for policy in _inline_policies(
                _template(**COMPONENT_COMBINATIONS["mcp-server"])["Resources"]["GatewayRole"]
            )
            for statement in policy["PolicyDocument"]["Statement"]
            if "bedrock-agentcore:GetWorkloadAccessToken" in _as_list(statement.get("Action", []))
        ]
        assert len(statements) == 1, "the grant should live in exactly one statement"
        resources = [r["Fn::Sub"] for r in _as_list(statements[0]["Resource"])]
        assert any(r.endswith("workload-identity-directory/default") for r in resources), (
            f"the parent directory is not named: {resources}"
        )
        assert any(r.endswith("workload-identity-directory/default/workload-identity/*") for r in resources), (
            f"the workload identity itself is not named: {resources}"
        )


class TestLeastPrivilegeResources:
    """ARCC cnt_AGx9pUNpmdOVZB: specific resources, not "*".

    The actions were already exact — no ``bedrock-agentcore:*`` anywhere. The
    resources were not: eighteen statements said ``"*"``, including the gateway
    role's ``secretsmanager:GetSecretValue``, which in the live account means every
    RDS master password and third-party API key in it.
    """

    @staticmethod
    def _wildcard_statements(template):
        found = set()
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    target = statement.get("Resource")
                    if target == "*" or (isinstance(target, list) and "*" in target):
                        found.add((logical_id, statement.get("Sid", "<unnamed>")))
        return found

    @ALL_COMBINATIONS
    def test_no_unjustified_wildcard_resources(self, combo):
        unexpected = self._wildcard_statements(_template(**combo)) - WILDCARD_ALLOWLIST
        assert not unexpected, (
            f"these statements grant an action on every resource in the account: {sorted(unexpected)}. "
            "Scope them, or add them to WILDCARD_ALLOWLIST with the reason AWS offers no ARN"
        )

    def test_no_unjustified_wildcard_resources_with_every_tool(self):
        """The tool-driven statements only exist on a canvas that connects them,
        so the parametrised combinations above never reach them."""
        template = _template(
            gateway_config=AGENTCORE_GATEWAY,
            memory_config={"enabled": True},
            connectedTools=["browser", "code_interpreter", "guardrails", "observability"],
            evaluation_config={"enabled": True},
        )
        unexpected = self._wildcard_statements(template) - WILDCARD_ALLOWLIST
        assert not unexpected, sorted(unexpected)

    @ALL_COMBINATIONS
    def test_every_wildcard_statement_is_named(self, combo):
        """An unnamed statement cannot be allowlisted, reviewed or pointed at."""
        assert "<unnamed>" not in {sid for _, sid in self._wildcard_statements(_template(**combo))}

    def test_the_allowlist_has_not_gone_stale(self):
        """An entry that no longer matches anything is a comment pretending to be
        a control: it makes the list look considered while covering nothing."""
        reachable = self._wildcard_statements(
            _template(
                template_id="mcp-server-gateway-target",
                gateway_config=AGENTCORE_GATEWAY,
                memory_config={"enabled": True},
                mcp_server_config={"tools": []},
                knowledge_base_config=KB_CONFIG,
                policy_config={"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
                connectedTools=["browser", "code_interpreter", "guardrails", "observability"],
                evaluation_config={"enabled": True},
            )
        )
        assert WILDCARD_ALLOWLIST - reachable == set(), sorted(WILDCARD_ALLOWLIST - reachable)

    def test_the_gateway_can_no_longer_read_every_secret_in_the_account(self):
        """The specific finding this class exists for.

        A live ``list-secrets`` on the account this was found in returned RDS
        cluster credentials, Postgres knowledge-base credentials and a Pinecone API
        key. None of them belong to any exported stack, and all of them were
        readable by the gateway role.
        """
        statements = _template(gateway_config=AGENTCORE_GATEWAY)["Resources"]["GatewayRole"]["Properties"]["Policies"][
            0
        ]["PolicyDocument"]["Statement"]
        secret_reads = [s for s in statements if "secretsmanager:GetSecretValue" in s["Action"]]
        assert secret_reads, "the statement was removed rather than scoped — check this is intended"
        for statement in secret_reads:
            assert statement["Resource"] != "*"
            for arn in statement["Resource"]:
                pattern = arn["Fn::Sub"]
                assert ":secret:bedrock-agentcore-" in pattern or ":secret:agentcore-" in pattern, pattern
                assert "${AWS::AccountId}" in pattern, f"{pattern} reaches other accounts"

    @ALL_COMBINATIONS
    def test_model_invocation_is_scoped_to_bedrock_model_arns(self, combo):
        """Four roles invoke Bedrock; all four must agree, and none may say "*".

        The region stays wildcarded on the foundation-model family on purpose:
        invoking a ``us.`` cross-region inference profile calls the model in
        whichever region the profile routes to.
        """
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"]
                    if "bedrock:InvokeModel" not in (actions if isinstance(actions, list) else [actions]):
                        continue
                    arns = [a["Fn::Sub"] for a in _as_list(statement["Resource"])]
                    assert any("foundation-model/" in a for a in arns), f"{logical_id}: {arns}"
                    assert all("bedrock" in a for a in arns), f"{logical_id} grants beyond Bedrock: {arns}"

    @ALL_COMBINATIONS
    def test_log_writes_cannot_reach_another_account_or_region(self, combo):
        """``PutLogEvents`` is authorized against the log-stream ARN, so every entry
        must either be a stream ARN or end in a wildcard that covers one — an IAM
        ``*`` spans ``:`` and ``/``. Confirmed with IAM Access Analyzer, which reports
        listing both shapes as REDUNDANT_RESOURCE."""
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    if "logs:PutLogEvents" not in _as_list(statement["Action"]):
                        continue
                    arns = [a["Fn::Sub"] for a in _as_list(statement["Resource"])]
                    assert any(a.endswith("*") for a in arns), (
                        f"{logical_id} names no log-stream ARN and no wildcard that covers one, "
                        f"so PutLogEvents is denied: {arns}"
                    )
                    for arn in arns:
                        assert "${AWS::AccountId}" in arn and "${AWS::Region}" in arn, arn

    @pytest.mark.parametrize(
        ("role", "action", "expected"),
        [
            # The roles the resource does not depend on name it exactly, by its own ARN
            # attribute. `GatewayArn` and `MemoryArn` were read from the CloudFormation
            # resource registry (`describe-type`), not assumed: no schema ships for
            # these types, so cfn-lint cannot catch a wrong GetAtt — only the deploy can.
            ("RuntimeExecutionRole", "bedrock-agentcore:CreateEvent", ("AgentCoreMemory", "MemoryArn")),
            ("RuntimeExecutionRole", "bedrock-agentcore:InvokeGateway", ("AgentCoreGateway", "GatewayArn")),
        ],
    )
    def test_the_runtime_can_only_reach_its_own_gateway_and_memory(self, role, action, expected):
        """The cross-agent isolation finding, and it was demonstrated rather than argued:
        a replica of one agent's runtime role called ``get-memory`` and ``list-actors`` on
        a *different* agent's memory and enumerated its end users' Cognito subject ids.
        ``memory/*`` and ``gateway/*`` are account-wide, and an account with two exported
        agents in it is the normal case, not the edge one."""
        template = _template(gateway_config=AGENTCORE_GATEWAY, memory_config={"enabled": True})
        statements = template["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        statement = next(s for s in statements if action in _as_list(s["Action"]))
        resources = _as_list(statement["Resource"])
        assert {"Fn::GetAtt": list(expected)} in resources, (
            f"{role}/{action} does not name its own resource: {resources}"
        )
        for entry in resources:
            flat = json.dumps(entry)
            assert expected[1] in flat, f"{role}/{action} reaches beyond this stack: {flat}"

    @pytest.mark.parametrize(
        ("role", "action", "expected"),
        [
            # The two roles that ARE passed to the resource they would name, so naming its
            # ARN is a dependency cycle. Prefix scoping on the name this template chose is
            # what is left, and it is still not `kind/*`.
            ("MemoryExecutionRole", "bedrock-agentcore:CreateEvent", "memory/${DeploymentName}_memory-*"),
            ("GatewayRole", "bedrock-agentcore:InvokeGateway", "gateway/${DeploymentName}-gateway-*"),
        ],
    )
    def test_the_circular_roles_are_scoped_to_this_deployments_names(self, role, action, expected):
        template = _template(gateway_config=AGENTCORE_GATEWAY, memory_config={"enabled": True})
        statements = template["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        statement = next(s for s in statements if action in _as_list(s["Action"]))
        arns = [a["Fn::Sub"] for a in _as_list(statement["Resource"])]
        assert any(a.endswith(expected) for a in arns), f"{role}/{action}: {arns}"
        for arn in arns:
            assert arn.startswith("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:"), arn
            assert not arn.endswith((":memory/*", ":gateway/*")), f"{role}/{action} is account-wide: {arn}"

    @ALL_COMBINATIONS
    def test_no_agentcore_statement_anywhere_is_account_wide(self, combo):
        """The rule, rather than the four cases above. ``memory/*`` and ``gateway/*``
        must not come back into any role, under any component combination."""
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    for entry in _as_list(statement["Resource"]):
                        pattern = entry.get("Fn::Sub") if isinstance(entry, dict) else None
                        if not isinstance(pattern, str):
                            continue
                        assert not pattern.endswith((":memory/*", ":gateway/*")), (
                            f"{logical_id}/{statement.get('Sid')} reaches every agent in the account: {pattern}"
                        )

    @ALL_COMBINATIONS
    def test_the_provider_can_resolve_the_gateway_a_cedar_policy_names(self, combo):
        """Without these four the policy reaches CREATE_FAILED, and nothing says why.

        AgentCore validates a resource-scoped Cedar statement by resolving the gateway's
        targets as the *caller*, so the permission has to be on this role even though no
        line of the handler calls those APIs. The live statusReason was "Insufficient
        permissions to list targets on gateway with ID <id>" and the stack still reported
        the policy as the only failure — a CREATE_COMPLETE gateway with a dead policy.

        ``InvokeGateway`` is the fourth and was withheld on the theory that the second
        symptom — "Insufficient permissions to *call* gateway with ID <id>" — was an
        engine-to-gateway convergence race that the lenient validation mode avoided. A
        controlled live experiment disproved both halves: same principal, same statement,
        gateway READY for minutes, the grant as the only variable, CREATE_FAILED in BOTH
        modes without it and ACTIVE in both with it. Every statement this generator emits
        names real tools on a concrete gateway ARN, which is the shape that has to be
        resolved, so without this grant the policy feature did not function at all.

        Paired with the negative half in the same test: the grant must be ABSENT when the
        template has no gateway, because it names one by ``GetAtt``. A grant that would be
        merely over-permissive elsewhere makes those templates unresolvable.
        """
        template = _template(**combo)
        policy = next(
            p
            for p in template["Resources"]["CfnProviderRole"]["Properties"]["Policies"]
            if p["PolicyName"] == "AgentCorePolicyManagement"
        )
        statements = policy["PolicyDocument"]["Statement"]
        resolution = [s for s in statements if s.get("Sid") == "AgentCorePolicyGatewayResolution"]
        if "AgentCoreGateway" not in template["Resources"]:
            assert not resolution, "this template has no gateway to GetAtt"
            return
        assert resolution, "a Cedar policy cannot be validated without resolving its gateway"
        actions = _as_list(resolution[0]["Action"])
        assert set(actions) == {
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:ListGatewayTargets",
            "bedrock-agentcore:GetGatewayTarget",
            "bedrock-agentcore:InvokeGateway",
        }, actions
        # InvokeGateway is the one action here that is not read-only, so it is also the
        # one whose scope has to be exact: this stack's own gateway and its targets,
        # never a wildcard that would reach a gateway some other export created.
        resources = _as_list(resolution[0]["Resource"])
        assert {"Fn::GetAtt": ["AgentCoreGateway", "GatewayArn"]} in resources
        assert "*" not in resources, "InvokeGateway on every gateway in the account"
        for entry in resources:
            rendered = json.dumps(entry)
            assert "AgentCoreGateway" in rendered, f"not scoped to this stack's gateway: {entry}"

    @ALL_COMBINATIONS
    def test_the_grant_that_widens_blast_radius_is_declared_not_buried(self, combo):
        """A recipient must be able to find out that a deploy can call their tools.

        ``InvokeGateway`` lets the custom-resource Lambda invoke the tools the gateway
        fronts for as long as the stack is deploying. That is defensible — creating the
        policy is the role's whole function — but only if it is stated, so the README
        has to say it rather than leaving it to whoever reads the IAM diff.
        """
        bundle = _generate(**combo)
        template = yaml.safe_load(bundle.template_yaml)
        if "PolicyValidationMode" not in template.get("Parameters", {}):
            pytest.skip("this template creates no policy, so the README has no policy section")
        readme = bundle.readme
        assert "bedrock-agentcore:InvokeGateway" in readme
        assert "invoke the tools this gateway fronts" in readme
        # And it must not repeat the claim the live experiment disproved.
        assert "have not converged" not in readme, "the README still blames a convergence race"
        assert "both" in readme.lower(), "the README must say the mode makes no difference"

    def test_the_provider_can_create_a_resource_scoped_policy(self):
        """``ManageResourceScopedPolicy`` is undocumented and was missing.

        Every Cedar statement this export emits is scoped to a gateway's tools, which
        makes it a resource-scoped policy; ``ManageAdminPolicy`` alone is not enough for
        one, and CreatePolicy fails without it.
        """
        policy = next(
            p
            for p in _template(gateway_config=AGENTCORE_GATEWAY)["Resources"]["CfnProviderRole"]["Properties"][
                "Policies"
            ]
            if p["PolicyName"] == "AgentCorePolicyManagement"
        )
        lifecycle = next(s for s in policy["PolicyDocument"]["Statement"] if s["Sid"] == "AgentCorePolicyLifecycle")
        assert "bedrock-agentcore:ManageResourceScopedPolicy" in lifecycle["Action"]

    def test_the_gateway_role_cannot_reach_an_agent_runtime(self):
        """The other half of dropping the inert ``bedrock-agentcore:InvokeAgent``.

        That grant was the only reason ``runtime/*`` was listed on this statement.
        Removing the action and leaving the resource would have kept a gateway role
        addressable against every agent runtime in the account for no purpose — a
        resource nothing in the statement needs is still a resource an added action
        would silently inherit.
        """
        statements = _template(gateway_config=AGENTCORE_GATEWAY)["Resources"]["GatewayRole"]["Properties"]["Policies"][
            0
        ]["PolicyDocument"]["Statement"]
        for statement in statements:
            for arn in _as_list(statement["Resource"]):
                if isinstance(arn, dict) and "Fn::Sub" in arn and isinstance(arn["Fn::Sub"], str):
                    assert ":runtime/" not in arn["Fn::Sub"], (
                        f"{statement.get('Sid')} names an agent runtime: {arn['Fn::Sub']}"
                    )

    def test_the_knowledge_base_lambda_reads_one_knowledge_base(self):
        statements = _template(gateway_config=AGENTCORE_GATEWAY, knowledge_base_config=KB_CONFIG)["Resources"][
            "KBToolLambdaRole"
        ]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        retrieve = next(s for s in statements if "bedrock:Retrieve" in s["Action"])
        assert retrieve["Resource"] != "*"
        (arn,) = retrieve["Resource"]
        template_string, variables = arn["Fn::Sub"]
        assert "knowledge-base/${KbId}" in template_string
        assert "KbId" in variables

    def test_a_permissive_action_list_is_still_refused(self):
        """The other half of least privilege, pinned so it cannot regress while
        attention is on resources: no service-level action wildcards."""
        for name, combo in COMPONENT_COMBINATIONS.items():
            for logical_id, resource in _template(**combo)["Resources"].items():
                if resource["Type"] != "AWS::IAM::Role":
                    continue
                for policy in _inline_policies(resource):
                    for statement in policy["PolicyDocument"]["Statement"]:
                        actions = statement["Action"]
                        for action in actions if isinstance(actions, list) else [actions]:
                            assert not action.endswith(":*"), f"{name}/{logical_id}: {action}"
                            assert action != "*", f"{name}/{logical_id}: bare wildcard action"
                        assert "NotAction" not in statement, f"{name}/{logical_id} uses NotAction"


# ---------------------------------------------------------------------------
# Customer-managed encryption keys
# ---------------------------------------------------------------------------

# Resource types this template emits that CANNOT take a customer-managed key,
# checked against the live CloudFormation registry rather than documentation. Held
# here so the claim is testable: if AWS adds an encryption property to one of
# these, test_unencryptable_types_still_have_no_key_property fails and the README's
# "what it does not cover" list gets revisited instead of quietly going stale.
NO_CMK_SUPPORT = {
    "AWS::Bedrock::KnowledgeBase",
    "AWS::BedrockAgentCore::Runtime",
    "AWS::Cognito::UserPool",
}

# The property each encryptable type takes, and the shape of the value. Duplicated
# from the generator on purpose: a test that imports the mapping it is checking
# proves only that the mapping is self-consistent.
EXPECTED_CMK_PROPERTY = {
    "AWS::BedrockAgentCore::Memory": "EncryptionKeyArn",
    "AWS::BedrockAgentCore::Gateway": "KmsKeyArn",
    "AWS::S3Vectors::VectorBucket": "EncryptionConfiguration",
    "AWS::S3Vectors::Index": "EncryptionConfiguration",
    "AWS::Bedrock::DataSource": "ServerSideEncryptionConfiguration",
    # Holds the Cedar statements — who may invoke which tool. This entry was the
    # one missing from this duplicated map, which made _encryptable under-report
    # and silently disabled the CfnProviderRole branch of
    # test_roles_that_touch_encrypted_data_can_use_the_key: the branch was there,
    # its condition could simply never be true. The generator had it right.
    "AWS::BedrockAgentCore::PolicyEngine": "EncryptionKeyArn",
    # Note ``KmsKeyId``, not ``KmsKeyArn`` — CloudWatch Logs is the odd one out.
    # This grants no role anything, and deliberately: the key here is used by the
    # logs service principal, not by a role in this stack, so it is authorized in
    # the key policy (see the generated README) rather than by an inline policy.
    "AWS::Logs::LogGroup": "KmsKeyId",
    # The log groups AgentCore creates for the runtime itself, which no declared
    # resource can reach. ``KmsKeyArn`` and not ``KmsKeyId`` because this is a
    # Custom Resource property read by our own Lambda, not a Logs API field — and
    # unlike ``AWS::Logs::LogGroup`` it DOES cost a role grant, because that Lambda
    # is the caller CloudWatch Logs validates the key against.
    "Custom::RuntimeLogGroup": "KmsKeyArn",
    # Encrypts the function's environment variables. Only functions that HAVE
    # environment variables — see _encryptable.
    "AWS::Lambda::Function": "KmsKeyArn",
}


def _encryptable(template):
    """Resources in *template* that take a customer-managed key, by logical id.

    A Lambda with no ``Environment`` is excluded deliberately: ``KmsKeyArn`` there
    would encrypt nothing this template uses while costing the function's role a
    ``kms:Decrypt`` grant. ``CfnProviderLambda`` is the case in point, and it is why
    this is a function rather than a set membership test.
    """
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] in EXPECTED_CMK_PROPERTY
        and not (resource["Type"] == "AWS::Lambda::Function" and "Environment" not in resource["Properties"])
    }


class TestCustomerManagedKey:
    """ARCC cnt_KSKFeuhiZef0bA: transparent encryption AND an opt-in customer key.

    The first half was already true — every service here encrypts at rest with an
    AWS-owned key and the recipient does nothing. The second half did not exist:
    there was no way for a customer to point this stack at a key they control, so
    no way for them to audit use of their data in CloudTrail or to revoke access to
    it. That is a hard blocker for a regulated recipient, which is the recipient
    this export is for.

    The tests below pin the three things that can silently break: that the property
    lands on every encryptable resource, that it is conditional so the default
    deploy is unchanged, and that the roles which have to use the key are granted
    it — an encrypted resource whose role cannot decrypt is a stack that deploys
    and then fails at runtime.
    """

    @ALL_COMBINATIONS
    def test_every_encryptable_resource_takes_the_key(self, combo):
        template = _template(**combo)
        for logical_id, resource in _encryptable(template).items():
            prop = EXPECTED_CMK_PROPERTY[resource["Type"]]
            assert prop in resource["Properties"], f"{logical_id} ({resource['Type']}) ignores the key parameter"

    @ALL_COMBINATIONS
    def test_the_key_is_optional_everywhere_it_is_used(self, combo):
        """Every use must be an ``Fn::If`` onto ``AWS::NoValue``.

        A bare ``{"Ref": "CustomerManagedKeyArn"}`` would put the empty string into
        an ARN property on the default deploy, which fails the create. The whole
        feature has to be invisible when unused.
        """
        template = _template(**combo)
        for logical_id, resource in _encryptable(template).items():
            value = resource["Properties"][EXPECTED_CMK_PROPERTY[resource["Type"]]]
            assert list(value) == ["Fn::If"], f"{logical_id} uses the key unconditionally"
            condition, _when_set, when_unset = value["Fn::If"]
            assert condition == "HasCustomerManagedKey"
            assert when_unset == {"Ref": "AWS::NoValue"}, f"{logical_id} has no unencrypted fallback"

    @ALL_COMBINATIONS
    def test_the_s3_vectors_shape_flips_sse_type_too(self, combo):
        """``KmsKeyArn`` alone is rejected: the schema allows it if and only if
        ``SseType`` is ``aws:kms``, which defaults to ``AES256``."""
        for logical_id, resource in _encryptable(_template(**combo)).items():
            if not resource["Type"].startswith("AWS::S3Vectors::"):
                continue
            config = resource["Properties"]["EncryptionConfiguration"]["Fn::If"][1]
            assert config["SseType"] == "aws:kms", f"{logical_id} sets a key without switching SseType"
            assert config["KmsKeyArn"] == {"Ref": "CustomerManagedKeyArn"}

    @ALL_COMBINATIONS
    def test_unencryptable_types_still_have_no_key_property(self, combo):
        """The README tells the recipient these three are not covered. If AWS adds
        support, that sentence becomes wrong — fail here rather than there."""
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] not in NO_CMK_SUPPORT:
                continue
            props = resource["Properties"]
            assert not [k for k in props if "Kms" in k or "Encryption" in k], (
                f"{logical_id} ({resource['Type']}) now has an encryption property; "
                "update CUSTOMER_KEY_PROPERTIES and the README's coverage table"
            )

    @ALL_COMBINATIONS
    def test_roles_that_touch_encrypted_data_can_use_the_key(self, combo):
        """The failure this catches is a deploy that succeeds and a runtime that
        cannot read its own memory."""
        template = _template(**combo)
        encrypted_types = {r["Type"] for r in _encryptable(template).values()}
        expected = set()
        if {"AWS::BedrockAgentCore::Memory", "AWS::BedrockAgentCore::Gateway"} & encrypted_types:
            expected.add("RuntimeExecutionRole")
        if "AWS::BedrockAgentCore::Memory" in encrypted_types:
            expected.add("MemoryExecutionRole")
        if "AWS::BedrockAgentCore::Gateway" in encrypted_types:
            expected.add("GatewayRole")
        if {"AWS::S3Vectors::VectorBucket", "AWS::Bedrock::DataSource"} & encrypted_types:
            expected.add("KnowledgeBaseRole")
        if "AWS::BedrockAgentCore::PolicyEngine" in encrypted_types:
            # The custom-resource Lambda writes the Cedar statements into the
            # engine. AgentCore's policy APIs run a forward-access-session KMS
            # check against the caller, so without this grant the failure surfaces
            # as AccessDenied on CreatePolicy — a stack that deploys the engine and
            # then cannot put a single rule in it.
            expected.add("CfnProviderRole")
        if "Custom::RuntimeLogGroup" in encrypted_types:
            # The same Lambda, for a different reason: it hands the key ARN to
            # CreateLogGroup and AssociateKmsKey, and CloudWatch Logs validates the
            # key against the CALLER. Without this grant the failure is an
            # AccessDenied from the logs call, which reads like a missing logs
            # permission rather than a missing kms one.
            expected.add("CfnProviderRole")
        # A Lambda whose environment is encrypted cannot start unless its own
        # execution role can decrypt, so the expectation is read off the function's
        # Role property rather than hardcoded — same as the generator does.
        for resource in _encryptable(template).values():
            if resource["Type"] == "AWS::Lambda::Function":
                expected.add(resource["Properties"]["Role"]["Fn::GetAtt"][0])

        granted = {
            logical_id
            for logical_id, role in _roles(template).items()
            if any(p["PolicyName"] == "CustomerManagedKeyAccess" for p in _inline_policies(role))
        }
        assert granted == expected & set(_roles(template)), f"key access granted to {granted}, expected {expected}"

    @ALL_COMBINATIONS
    def test_the_runtime_role_gets_no_key_access_it_does_not_need(self, combo):
        """The complement of the above, stated separately because it is the half
        that regresses. The runtime role is the one that would pick the grant up
        from a static list: it exists in every combination, and in a runtime-only
        export nothing it touches is encrypted with the customer key.

        This used to be phrased as "a combination with nothing encrypted grants
        nobody the key", and it skipped itself the moment every export started
        governing the runtime's log groups — every combination now encrypts
        something. Narrowed to the one role rather than left as a permanent skip.
        """
        template = _template(**combo)
        encrypted_types = {r["Type"] for r in _encryptable(template).values()}
        if {"AWS::BedrockAgentCore::Memory", "AWS::BedrockAgentCore::Gateway"} & encrypted_types:
            pytest.skip("this combination legitimately gives the runtime role the key")
        names = [p["PolicyName"] for p in _inline_policies(_roles(template)["RuntimeExecutionRole"])]
        assert "CustomerManagedKeyAccess" not in names, "the runtime role was granted a key it cannot use"

    def test_key_access_is_scoped_to_the_one_supplied_key(self):
        """``kms:Decrypt`` on ``*`` would defeat the point: the customer could no
        longer revoke access to this workload's data by disabling one key."""
        template = _template(memory_config={"enabled": True}, gateway_config=AGENTCORE_GATEWAY)
        for logical_id, role in _roles(template).items():
            for policy in _inline_policies(role):
                if policy["PolicyName"] != "CustomerManagedKeyAccess":
                    continue
                for statement in policy["PolicyDocument"]["Statement"]:
                    assert statement["Resource"] == {"Ref": "CustomerManagedKeyArn"}, (
                        f"{logical_id}/{statement['Sid']} is not scoped to the supplied key"
                    )

    def test_the_envelope_encryption_action_is_granted(self):
        """``kms:GenerateDataKeyWithoutPlaintext`` was missing, and only a
        least-privilege caller ever notices.

        A service that needs the wrapped data key and nothing else calls this action
        rather than ``GenerateDataKey``. Live symptom: ``missing required
        kms:GenerateDataKeyWithoutPlaintext permission`` mid-create. An
        AdministratorAccess deploy proves nothing about it, because the deploying
        principal's own permissions mask the role's.
        """
        template = _template(memory_config={"enabled": True}, knowledge_base_config=KB_CONFIG)
        seen = 0
        for logical_id, role in _roles(template).items():
            for policy in _inline_policies(role):
                if policy["PolicyName"] != "CustomerManagedKeyAccess":
                    continue
                for statement in policy["PolicyDocument"]["Statement"]:
                    if "kms:GenerateDataKey" not in _as_list(statement["Action"]):
                        continue
                    seen += 1
                    assert "kms:GenerateDataKeyWithoutPlaintext" in _as_list(statement["Action"]), (
                        f"{logical_id} can generate a plaintext data key but not a wrapped one"
                    )
        assert seen, "no data-key statement in this combination — the loop asserted nothing"

    def test_create_grant_carries_the_aws_resource_condition(self):
        """ARCC cnt_SFJJhkOueCPRkd. Unconditional ``kms:CreateGrant`` on a key lets
        the holder grant that key to a principal of their choosing, which is a
        privilege-escalation path out of the workload. The condition limits it to
        grants KMS creates for an AWS service handling this request."""
        template = _template(memory_config={"enabled": True})
        statements = [
            s
            for role in _roles(template).values()
            for policy in _inline_policies(role)
            if policy["PolicyName"] == "CustomerManagedKeyAccess"
            for s in policy["PolicyDocument"]["Statement"]
            if "kms:CreateGrant" in _as_list(s["Action"])
        ]
        assert statements, "no CreateGrant statement found"
        for statement in statements:
            assert statement["Condition"] == {"Bool": {"kms:GrantIsForAWSResource": "true"}}

    def test_the_parameter_defaults_to_transparent_encryption(self):
        param = _template()["Parameters"]["CustomerManagedKeyArn"]
        assert param["Default"] == "", "a required key would break the default deploy"

    @pytest.mark.parametrize(
        "value,accepted",
        [
            ("", True),
            ("arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012", True),
            ("arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/12345678-1234-1234-1234-123456789012", True),
            # An alias ARN is the trap: CloudWatch Logs accepts one, and
            # AWS::Bedrock::DataSource does not. Reject it at the parameter.
            ("arn:aws:kms:us-east-1:123456789012:alias/my-key", False),
            ("12345678-1234-1234-1234-123456789012", False),
            ("arn:aws:iam::123456789012:role/NotAKey", False),
        ],
    )
    def test_the_parameter_pattern_matches_the_strictest_consumer(self, value, accepted):
        pattern = _template()["Parameters"]["CustomerManagedKeyArn"]["AllowedPattern"]
        assert bool(re.fullmatch(pattern, value)) is accepted, f"{value!r} should {'' if accepted else 'not '}match"

    @ALL_COMBINATIONS
    def test_the_condition_is_declared(self, combo):
        """Declared in every combination, including the ones with nothing to
        encrypt: the roles reference it, and an undeclared condition is a
        template-validation error rather than a no-op."""
        assert "HasCustomerManagedKey" in _template(**combo).get("Conditions", {})

    def test_deploy_sh_exposes_the_key(self):
        deploy = _generate(memory_config={"enabled": True}).deploy_sh
        assert "CUSTOMER_MANAGED_KEY_ARN" in deploy
        assert "CustomerManagedKeyArn=$CUSTOMER_MANAGED_KEY_ARN" in deploy

    def test_readme_documents_coverage_and_the_gap(self):
        """The Cognito gap is the one a recipient must not discover after the fact:
        if their requirement covers user identities, this pool does not meet it."""
        readme = _generate(memory_config={"enabled": True}, knowledge_base_config=KB_CONFIG).readme
        assert "## Encryption" in readme
        assert "CUSTOMER_MANAGED_KEY_ARN" in readme
        assert "Cognito" in readme
        for prop in set(EXPECTED_CMK_PROPERTY.values()):
            assert prop in readme, f"README does not say where {prop} applies"
        # The create-only replacement trap, and why an alias is refused.
        assert "create-only" in readme
        assert "not an alias" in readme


# ---------------------------------------------------------------------------
# Cross-account invocation of this stack's Lambdas
# ---------------------------------------------------------------------------


class TestLambdaPermissionsAreSourceConstrained:
    """Checkov CKV_AWS_364, and it was a real hole.

    All three ``AWS::Lambda::Permission`` resources named the
    ``bedrock-agentcore.amazonaws.com`` service principal with no ``SourceAccount``
    and no ``SourceArn``. A service-principal grant with no source constraint
    authorizes that service acting on behalf of *any* account, so somebody else's
    AgentCore gateway, pointed at this function's ARN, was allowed to invoke it.
    The KB tool Lambda holds ``bedrock:Retrieve`` on this account's knowledge base,
    which makes it a read path into the recipient's documents.
    """

    @ALL_COMBINATIONS
    def test_every_service_principal_grant_names_a_source(self, combo):
        template = _template(**combo)
        permissions = {
            k: v["Properties"] for k, v in template["Resources"].items() if v["Type"] == "AWS::Lambda::Permission"
        }
        for logical_id, props in permissions.items():
            principal = props["Principal"]
            if not (isinstance(principal, str) and principal.endswith(".amazonaws.com")):
                continue
            assert "SourceAccount" in props or "SourceArn" in props, (
                f"{logical_id} lets {principal} invoke on behalf of any account"
            )
            if "SourceAccount" in props:
                assert props["SourceAccount"] == {"Ref": "AWS::AccountId"}, (
                    f"{logical_id} constrains to an account that is not this one"
                )

    def test_the_kb_tool_lambda_is_covered(self):
        """Named explicitly because it is the one with knowledge-base read access,
        so a regression here is a data-exposure regression, not a hygiene one."""
        props = _template(gateway_config=AGENTCORE_GATEWAY, knowledge_base_config=KB_CONFIG)["Resources"][
            "KBToolLambdaPermission"
        ]["Properties"]
        assert props["SourceAccount"] == {"Ref": "AWS::AccountId"}


# ---------------------------------------------------------------------------
# The policy scanner's verdict, with every exception argued
# ---------------------------------------------------------------------------

# Checkov findings this template is knowingly shipped with. Anything NOT in here
# fails the gate, which is the point: a permanently-red scan gets ignored, and a
# fully-suppressed one checks nothing. Each entry is a decision with a reason, and
# a reason that stops being true is a reason to remove the entry.
CHECKOV_ACCEPTED = {
    # A DLQ only applies to ASYNCHRONOUS invocation. Both Lambdas here are invoked
    # synchronously — the custom-resource provider by CloudFormation, the tool
    # Lambdas by the AgentCore Gateway on the request path — so a DLQ would never
    # receive anything. Errors surface to the caller, which is where they belong.
    "CKV_AWS_116",
    # CKV_AWS_117 (Lambda not in a VPC) and CKV_AWS_158 (log group without a CMK)
    # were both here. Both are now fixed rather than accepted, by LambdaSubnetIds /
    # LambdaSecurityGroupIds and by CustomerManagedKeyArn respectively, which is why
    # test_the_baseline_has_not_gone_stale would fail if they were left behind.
}


class TestEmittedActionsAreRealIamActions:
    """Every action's service prefix must be a real IAM service prefix.

    This exists because IAM does not check. A policy naming a service that does
    not exist is accepted at ``PutRolePolicy``, accepted at stack create, and the
    stack reaches CREATE_COMPLETE; the statement authorizes nothing. The only
    symptom is an AccessDeniedException at runtime on a call whose permission the
    console displays as granted, which is why this survived in the template for as
    long as it did. Two flavours were present:

      * ``bedrock-agentcore-control:GetMemory`` and six others. Both
        ``bedrock-agentcore`` and ``bedrock-agentcore-control`` are real API
        endpoints with their own botocore client, so the control-plane prefix reads
        as correct. But an IAM service prefix is the SigV4 *signing name*, not the
        endpoint prefix, and both clients sign as ``bedrock-agentcore``.
      * ``agent-credential-provider:GetCredentials`` and ``ListCredentialProviders``,
        under a prefix that is not an AWS service at all. That statement
        (``Sid: CredentialProviderAccess``) was void in its entirety.

    The oracle is botocore's own service models rather than a hand-written list,
    so the check keeps working as AgentCore adds services. It deliberately checks
    the *prefix* only: IAM actions do not map 1:1 to API operations, and
    AuthorizeAction, PartiallyAuthorizeActions, InvokeGateway, ManageAdminPolicy
    and ConnectBrowserAutomationStream are all real IAM actions with no SDK
    operation behind them. IAM Access Analyzer is the oracle for individual
    actions; see the ``TestPolicyScanner`` sibling for what runs automatically.
    """

    # signingName is not universally equal to the IAM prefix — cloudwatch's actions
    # are `cloudwatch:` while it signs as `monitoring` — so an escape hatch has to
    # exist or the first legitimate CloudWatch metric grant turns this into a
    # blocker with a misleading message. Empty today: every prefix this template
    # emits (bedrock, bedrock-agentcore, kms, lambda, logs, s3, s3vectors,
    # secretsmanager) is a signing name. Add a prefix here only with the IAM
    # documentation reference that shows the mismatch is real.
    PREFIX_EXCEPTIONS: dict[str, str] = {}

    @staticmethod
    def _valid_prefixes():
        """Every SigV4 signing name botocore knows about."""
        import botocore.session

        session = botocore.session.get_session()
        prefixes = set()
        for service in session.get_available_services():
            metadata = session.get_service_model(service).metadata
            prefixes.add(metadata.get("signingName") or metadata["endpointPrefix"])
        return prefixes

    @staticmethod
    def _actions(template):
        found = set()
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            for policy in _inline_policies(resource):
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"]
                    for action in [actions] if isinstance(actions, str) else actions:
                        found.add((logical_id, statement.get("Sid", "<unnamed>"), action))
        return found

    def _check(self, template):
        valid = self._valid_prefixes() | set(self.PREFIX_EXCEPTIONS)
        return sorted(
            (logical_id, sid, action)
            for logical_id, sid, action in self._actions(template)
            if action.split(":")[0] not in valid
        )

    @ALL_COMBINATIONS
    def test_every_action_names_a_real_service(self, combo):
        bad = self._check(_template(**combo))
        assert not bad, (
            "these grants authorize nothing — the service prefix does not exist, and neither IAM "
            f"nor CloudFormation will tell you: {bad}"
        )

    def test_every_action_names_a_real_service_with_every_component(self):
        """The parametrised combinations do not reach the tool-driven, evaluation
        or policy-engine statements, which is where three of the nine bad grants
        were."""
        bad = self._check(
            _template(
                template_id="mcp-server-gateway-target",
                gateway_config=AGENTCORE_GATEWAY,
                memory_config={"enabled": True},
                mcp_server_config={"tools": []},
                knowledge_base_config=KB_CONFIG,
                policy_config={"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
                connectedTools=["browser", "code_interpreter", "guardrails", "observability"],
                evaluation_config={"enabled": True},
            )
        )
        assert not bad, bad

    def test_the_specific_inert_grants_are_gone(self):
        """Named individually, because the prefix rule above cannot catch the four
        that had a valid prefix and a nonexistent verb, and because a regression
        here is silent by construction. GetLastKTurns and RetrieveMemories were
        carried in a comment as "legacy verbs kept for older SDK paths" — they were
        never actions; the memory verbs are RetrieveMemoryRecords and ListEvents.
        """
        template_yaml = _generate(
            template_id="mcp-server-gateway-target",
            gateway_config=AGENTCORE_GATEWAY,
            memory_config={"enabled": True},
            mcp_server_config={"tools": []},
            knowledge_base_config=KB_CONFIG,
            policy_config={"policies": [{"name": "p", "statement": _VALID_CEDAR}]},
            connectedTools=["browser", "code_interpreter", "guardrails", "observability"],
            evaluation_config={"enabled": True},
        ).template_yaml
        # Matched against the actions rather than the raw YAML, so that a mention in
        # an explanatory comment does not fail the test and a real grant cannot hide
        # behind YAML quoting.
        actions = {action for _, _, action in self._actions(yaml.safe_load(template_yaml))}
        for action in (
            "bedrock-agentcore-control:GetMemory",
            "bedrock-agentcore-control:ListMemories",
            "bedrock-agentcore-control:GetEvaluator",
            "agent-credential-provider:GetCredentials",
            "agent-credential-provider:ListCredentialProviders",
            # bedrock-agentcore:CreateTokenVault was in this list and does not
            # belong here. It has no API operation — the service model's only vault
            # operations are GetTokenVault and SetTokenVaultCMK — which is why it
            # looked inert, but the IAM ACTION exists and is authorized during
            # CreateOauth2CredentialProvider. This repo already knew that on the
            # platform path: test_iam_completeness.py requires it of the harness
            # step as "Bug 153 — first OAuth2 cred provider provisions the token
            # vault". Asserting its ABSENCE here meant the exported template was
            # stripped of an action the identical API call needs, and a live deploy
            # confirmed it: "not authorized to perform:
            # bedrock-agentcore:CreateTokenVault on resource:
            # arn:aws:bedrock-agentcore:us-east-1:<account>:token-vault/default"
            # failed the very first stack in a fresh account. The other entries
            # below are genuinely nonexistent verbs and stay.
            "bedrock-agentcore:GetLastKTurns",
            "bedrock-agentcore:RetrieveMemories",
            "bedrock-agentcore:InvokeAgent",
            "bedrock-agentcore:CheckAuthorizePermissions",
            # Added 2026-09-19, when these two were retired from the PLATFORM's
            # policy step role and deployment Lambda role. The export never granted
            # them, so this is not a regression guard for something that happened
            # here — it makes the two paths symmetric. The generator emits roles from
            # the same AgentCore verb vocabulary as the platform stack, and until
            # now a copy-paste of a fake verb into the generator would have been
            # caught on the platform side only (its guard reads
            # infra/stacks/platform/*.py and never this file).
            #
            # Established the way the CreateTokenVault note above says to: IAM
            # Access Analyzer returns INVALID_ACTION "does not exist" for exactly
            # these two, while ManageResourceScopedPolicy — the real verb they sat
            # next to, and the one that actually authorizes gateway-scoped policy
            # create/delete — validates clean in the same document. Not inferred
            # from botocore's model, which is silent about real IAM-only actions too.
            "bedrock-agentcore:GetResourceScopedPolicy",
            "bedrock-agentcore:ListResourceScopedPolicies",
        ):
            assert action not in actions, f"{action} authorizes nothing and is back in the template"

    def test_the_credential_provider_lifecycle_is_completely_granted(self):
        """The complement of the test above, and the reason it is needed.

        Pruning inert grants is how both of these went missing, and neither
        absence is visible from the template: the stack simply fails on a live
        AccessDenied in someone else's account. Each action here is one the
        handler's code path actually reaches.

        ``CreateTokenVault`` has no API operation, so an audit that checks actions
        against botocore deletes it; the first ``CreateOauth2CredentialProvider``
        in a region provisions the default vault and IAM authorizes it by that
        name. ``UpdateOauth2CredentialProvider`` does have an operation and the
        handler calls it twice, yet it was granted nowhere — so the in-place
        update path, which exists specifically to keep the provider ARN stable
        across a secret rotation, could not run at all.
        """
        template = _template(
            template_id="mcp-server-gateway-target",
            gateway_config=AGENTCORE_GATEWAY,
            mcp_server_config={"tools": []},
        )
        actions = {action for _, _, action in self._actions(template)}
        for action in (
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:UpdateOauth2CredentialProvider",
            "bedrock-agentcore:DeleteOauth2CredentialProvider",
            "bedrock-agentcore:CreateTokenVault",
            "bedrock-agentcore:GetTokenVault",
        ):
            assert action in actions, (
                f"{action} is not granted anywhere in the emitted template; the credential "
                "provider lifecycle fails live with AccessDenied"
            )


class TestAuthorizationPolicyIsAcceptable:
    """The Cedar policy the export ships has to be one AgentCore will store.

    Every assertion here corresponds to a rejection observed from a live policy
    engine, because none of this is visible from the API contract: ``CreatePolicy``
    returns 200 and ``CREATING`` for statements the service goes on to refuse, and
    the refusal arrives seconds later in ``GetPolicy``'s ``statusReasons``. The
    generator previously emitted ``permit(principal, action, resource is
    AgentCore::Gateway) when {{ true }}`` as its default, which is rejected every
    time — so a stack could reach CREATE_COMPLETE with a policy engine in ENFORCE
    mode and no policy on it, and every tool call the agent made was denied with
    nothing in the stack events to say why.

    Live results these tests encode:

    ==========================================================  =============
    statement                                                   result
    ==========================================================  =============
    ``permit(principal, action, resource is AgentCore::Gateway)``  CREATE_FAILED
    ``... action == AgentCore::Action::"T___tool" ...``            CREATE_FAILED
    ``... action in [AgentCore::Action::"T___tool"] ...``          ACTIVE
    ``principal is AgentCore::IamEntity`` on a JWT gateway         rejected synchronously
    a gateway ARN in another account                              rejected synchronously
    ==========================================================  =============
    """

    POLICY_COMBOS = ["gateway+policy", "gateway+kb+default-policy", "everything"]

    @staticmethod
    def _statements(template):
        out = []
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] == "Custom::AgentCorePolicy":
                statement = resource["Properties"]["Statement"]
                out.append((logical_id, statement["Fn::Sub"] if isinstance(statement, dict) else statement))
        return out

    @pytest.mark.parametrize("combo", POLICY_COMBOS)
    def test_no_emitted_permit_leaves_the_action_unconstrained(self, combo):
        for logical_id, statement in self._statements(_template(**COMPONENT_COMBINATIONS[combo])):
            if not statement.lstrip().startswith("permit"):
                continue
            assert "action in [" in statement, (
                f"{logical_id} permits an unconstrained action. AgentCore reports this as "
                f"'Overly Permissive ... (Any Future Tools)' and the policy goes CREATE_FAILED: {statement}"
            )

    @pytest.mark.parametrize("combo", POLICY_COMBOS)
    def test_no_emitted_permit_uses_action_equality(self, combo):
        # `==` on a single action reads as narrower than `in [...]` on the same one
        # action, and is not: the validator rejects it and accepts the list form.
        for logical_id, statement in self._statements(_template(**COMPONENT_COMBINATIONS[combo])):
            assert "action ==" not in statement, f"{logical_id} uses `action ==`, which AgentCore rejects: {statement}"

    def test_the_default_policy_names_the_tools_the_template_deploys(self):
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        ((_, statement),) = self._statements(template)
        # The one tool this combination emits, under the id format AgentCore uses:
        # <GatewayTarget Name>___<tool name>.
        assert 'AgentCore::Action::"KBTool___knowledge_base_query"' in statement
        assert statement.count("AgentCore::Action::") == 1

    def test_the_default_policy_is_scoped_to_this_stacks_gateway(self):
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        ((logical_id, statement),) = self._statements(template)
        # A Cedar resource scope has to be a concrete same-account ARN; a placeholder
        # or another account's ARN is refused synchronously. Fn::Sub is how the
        # gateway's own ARN gets in, so the property must stay a Sub, not a string.
        assert isinstance(template["Resources"][logical_id]["Properties"]["Statement"], dict)
        assert 'resource == AgentCore::Gateway::"${AgentCoreGateway.GatewayArn}"' in statement

    def test_the_principal_type_matches_the_gateway_authorizer(self):
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        ((_, statement),) = self._statements(template)
        # The gateway is CUSTOM_JWT, and AgentCore rejects the mismatch outright:
        # "Gateway uses OAuth authorizer which requires 'AgentCore::OAuthUser'
        # principal type". If the authorizer ever changes, this fails here rather
        # than at deploy time.
        assert template["Resources"]["AgentCoreGateway"]["Properties"]["AuthorizerType"] == "CUSTOM_JWT"
        assert "principal is AgentCore::OAuthUser" in statement

    @pytest.mark.parametrize(
        "statement",
        [
            # The bare form, and the single-action `==` form that reads restrictive
            # and is refused identically. Both must be the real unconstrained text:
            # passing _VALID_CEDAR here made this test assert nothing at all.
            "permit(principal, action, resource);",
            'permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::"KBTool___query", '
            'resource == AgentCore::Gateway::"${AgentCoreGateway.GatewayArn}");',
        ],
    )
    def test_a_supplied_permit_with_an_unconstrained_action_is_refused_at_export(self, statement):
        with pytest.raises(ValueError, match="unconstrained action"):
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                policy_config={"policies": [{"name": "p", "statement": statement}]},
            )

    def test_a_supplied_forbid_is_left_alone(self):
        # A broad forbid is restrictive, not permissive, so the Overly Permissive
        # finding should not apply. That was not confirmed against the service, so
        # the generator passes it through rather than guessing either way.
        template = yaml.safe_load(
            _generate(
                gateway_config=AGENTCORE_GATEWAY,
                policy_config={"policies": [{"name": "p", "statement": "forbid(principal, action, resource);"}]},
            ).template_yaml
        )
        assert self._statements(template) == [("DefaultPolicy", "forbid(principal, action, resource);")]

    def test_a_policy_engine_with_no_nameable_tool_is_refused_at_export(self):
        # Rather than shipping a template that deploys green and denies everything.
        with pytest.raises(ValueError, match="no gateway tool whose actions can be named"):
            _generate(gateway_config=AGENTCORE_GATEWAY, policy_config={})

    def test_a_runtime_discovered_target_cannot_get_a_generated_policy(self):
        # An MCP server target's tools are not knowable when the template is written,
        # so under ENFORCE a generated default would deny them all. The export fails
        # with the workaround in the message instead.
        with pytest.raises(ValueError, match="discover their tools at runtime"):
            _generate(
                template_id="mcp-server-gateway-target",
                gateway_config=AGENTCORE_GATEWAY,
                mcp_server_config={"tools": []},
                knowledge_base_config=KB_CONFIG,
                policy_config={},
            )

    def test_the_policy_waits_for_the_engine_and_the_gateway(self):
        # The statement interpolates the gateway ARN and binds to the engine, so both
        # have to exist first. The custom resource is also what polls the policy to a
        # terminal status, which is why this is not the native CFN policy type.
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        policy = template["Resources"]["DefaultPolicy"]
        assert set(policy["DependsOn"]) >= {"PolicyEngine", "AgentCoreGateway", "CfnProviderLambda"}


# Split at module level rather than in the class body: a comprehension there cannot
# see the class's own names. Derived from COMPONENT_COMBINATIONS so a new combination
# is covered by one side or the other without anyone remembering to add it.
_POLICY_COMBOS = ["gateway+policy", "gateway+kb+default-policy", "everything"]
_NO_POLICY_COMBOS = [name for name in COMPONENT_COMBINATIONS if name not in _POLICY_COMBOS]


class TestPolicyValidationModeIsTheRecipientsChoice:
    """The strictness of AgentCore's policy validation is a parameter, not a constant.

    The handler picks IGNORE_ALL_FINDINGS for a measured reason — the findings analysis
    calls a gateway created seconds earlier and regularly fails on permissions that have
    not converged, leaving the policy CREATE_FAILED — but that is this project's
    risk judgement, not the recipient's. An account whose controls require the analysis
    to gate the deploy has to be able to ask for it without editing the Lambda.

    The asymmetry these tests pin: the *validation* mode is exposed, and
    ``enforcementMode`` deliberately is not. Its other value, LOG_ONLY, records a denial
    instead of enforcing it, so an engine whose policies were all LOG_ONLY would permit
    every tool while looking exactly like one that did not — and the emitted template's
    entire authorization story is that the gateway denies tools the policy does not name.
    """

    @pytest.mark.parametrize("combo", _POLICY_COMBOS)
    def test_every_policy_takes_the_mode_from_the_parameter(self, combo):
        template = _template(**COMPONENT_COMBINATIONS[combo])
        policies = [r for r in template["Resources"].values() if r["Type"] == "Custom::AgentCorePolicy"]
        assert policies, f"{combo} should emit at least one policy"
        for policy in policies:
            # A literal here would be the same hardcoding with extra steps: the point
            # is that one parameter moves every policy in the stack at once.
            assert policy["Properties"]["ValidationMode"] == {"Ref": "PolicyValidationMode"}

    @pytest.mark.parametrize("combo", _POLICY_COMBOS)
    def test_the_parameter_offers_exactly_the_two_modes_the_api_has(self, combo):
        spec = _template(**COMPONENT_COMBINATIONS[combo])["Parameters"]["PolicyValidationMode"]
        # AllowedValues rather than a free string: the handler refuses a value it does
        # not recognise, and CloudFormation rejecting it up front is the cheaper of the
        # two failures. The list is CreatePolicy's own enum.
        assert spec["AllowedValues"] == ["IGNORE_ALL_FINDINGS", "FAIL_ON_ANY_FINDINGS"]
        assert spec["Default"] == "IGNORE_ALL_FINDINGS", "the default must stay the one that deploys"

    @pytest.mark.parametrize("combo", _NO_POLICY_COMBOS)
    def test_a_template_with_no_policy_engine_does_not_carry_the_parameter(self, combo):
        # A parameter that controls nothing is an invitation to set it and wonder why
        # nothing happened. test_every_parameter_is_referenced enforces the same rule
        # from the other direction.
        template = _template(**COMPONENT_COMBINATIONS[combo])
        assert not [r for r in template["Resources"].values() if r["Type"] == "Custom::AgentCorePolicy"]
        assert "PolicyValidationMode" not in template.get("Parameters", {})

    @ALL_COMBINATIONS
    def test_enforcement_mode_is_not_exposed_anywhere(self, combo):
        """The one knob that must not be a parameter.

        Asserted on the serialized template rather than the parsed one so a parameter,
        a property, or a Mapping entry named for it all fail the same way.
        """
        combo_yaml = _generate(**combo).template_yaml
        assert "enforcementMode" not in combo_yaml
        assert "EnforcementMode" not in combo_yaml
        assert "LOG_ONLY" not in combo_yaml

    def test_the_handler_reads_the_property_the_template_emits(self):
        """The two halves ship separately and are wired by name only.

        The template's property name and the handler's ``props.get`` key have nothing
        but this test holding them together: a rename on either side would produce a
        stack that deploys green with every policy silently back on the default mode.
        """
        handler_src = (CFN_PROVIDER_DIR / "handler.py").read_text()
        assert 'props.get("ValidationMode")' in handler_src
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        assert "ValidationMode" in template["Resources"]["DefaultPolicy"]["Properties"]

    def test_the_readme_tells_the_recipient_which_way_to_turn_it(self):
        readme = _generate(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"]).readme
        assert "PolicyValidationMode" in readme
        assert "FAIL_ON_ANY_FINDINGS" in readme
        # The default is counter-intuitive, so the README has to justify it rather than
        # leaving a reader to "harden" it into a stack that fails to deploy.
        assert "CREATE_FAILED" in readme
        # And it must say what is *not* skipped, so the lenient mode is not read as
        # validation being switched off.
        assert "wildcard resource" in readme
        assert "enforcementMode" in readme, "the README must say why that one is not a knob"

    def test_a_policyless_readme_does_not_document_the_knob(self):
        readme = _generate(**COMPONENT_COMBINATIONS["runtime-only"]).readme
        assert "PolicyValidationMode" not in readme


class TestPolicyScanner:
    """A policy scan over the artifact we ship, gated on a reasoned baseline.

    No scanner has ever run against this template. The first run found two things
    worth fixing — an unencrypted Lambda environment (CKV_AWS_173, fixed by the
    customer-managed key) and a Lambda permission open to every AWS account
    (CKV_AWS_364, fixed above) — and three that are arguable. This test keeps the
    two fixed and forces the next new finding to be argued rather than absorbed.

    Of the three arguable ones, the reserved-concurrency finding turned out to be
    worth acting on rather than accepting: the ``LambdaReservedConcurrency``
    parameter satisfies it, which took the scan from 34 findings to 20.
    """

    def test_the_concurrency_knob_satisfies_the_scanner(self, findings):
        """CKV_AWS_115. The knob is optional, and the scanner counts the property
        being present rather than a particular value — so this passes on the default
        export too, which is the reason it is asserted rather than accepted."""
        assert "CKV_AWS_115" not in {c["check_id"] for c in findings}

    @staticmethod
    def _version():
        """The scanner's own version, for the failure message only.

        The baseline above is a property of a particular rule set. When this gate goes
        red the first question is "did the template change or did checkov?", and
        without the version in the output there is no way to tell from a CI log.
        """
        try:
            return subprocess.run(["checkov", "--version"], capture_output=True, text=True).stdout.strip()
        except OSError:  # pragma: no cover - guarded by _require_scanner
            return "unknown"

    @staticmethod
    def _scan(directory):
        result = subprocess.run(
            ["checkov", "-d", str(directory), "--framework", "cloudformation", "--compact", "--quiet", "-o", "json"],
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            pytest.fail(f"checkov produced no output (rc={result.returncode}): {result.stderr[-2000:]}")
        payload = json.loads(result.stdout)
        runs = payload if isinstance(payload, list) else [payload]
        return [check for run in runs for check in run.get("results", {}).get("failed_checks", [])]

    @pytest.fixture(scope="class")
    def findings(self, tmp_path_factory):
        _require_scanner("checkov", "pipx install checkov==3.3.1 (isolated, see _require_scanner)")
        directory = tmp_path_factory.mktemp("emitted")
        for name, combo in COMPONENT_COMBINATIONS.items():
            (directory / f"{name}.yaml").write_text(_generate(**combo).template_yaml)
        return self._scan(directory)

    def test_no_unargued_finding(self, findings):
        unexpected = {}
        for check in findings:
            if check["check_id"] in CHECKOV_ACCEPTED:
                continue
            unexpected.setdefault(check["check_id"], (check["check_name"], set()))[1].add(check["resource"])
        rendered = "\n".join(
            f"  {cid} {name}: {', '.join(sorted(resources))}" for cid, (name, resources) in sorted(unexpected.items())
        )
        assert not unexpected, (
            f"checkov {self._version()} reported findings with no recorded decision:\n{rendered}\n"
            "Either fix the template or add the id to CHECKOV_ACCEPTED with the reason."
        )

    def test_the_lambda_environment_finding_stays_fixed(self, findings):
        """CKV_AWS_173. Passes because the environment takes the customer-managed
        key; stated separately so the reason for it passing is recorded."""
        assert "CKV_AWS_173" not in {c["check_id"] for c in findings}

    def test_the_open_invoke_permission_stays_fixed(self, findings):
        """CKV_AWS_364, the cross-account invoke path."""
        assert "CKV_AWS_364" not in {c["check_id"] for c in findings}

    def test_the_baseline_has_not_gone_stale(self, findings):
        """An accepted finding that no longer occurs is a decision to delete, not to
        keep carrying: a stale suppression is how a real finding gets absorbed
        later under a check id somebody already agreed to ignore."""
        stale = CHECKOV_ACCEPTED - {c["check_id"] for c in findings}
        assert not stale, f"no longer reported, remove from CHECKOV_ACCEPTED: {sorted(stale)}"


# ---------------------------------------------------------------------------
# The outputs must be usable, and must not leak
# ---------------------------------------------------------------------------


def _iter_nodes(obj):
    """Yield every (key, value) pair anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _iter_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_nodes(item)


class TestTheEmittedYamlIsParseableByCloudFormation:
    """No YAML anchors or aliases, in any combination.

    This is the one defect class in this file that a passing test suite could not
    have caught before, and it made every emitted template undeployable:
    CloudFormation refuses a template containing an anchor with ``Template error:
    YAML aliases are not allowed in CloudFormation templates``, at CreateChangeSet,
    before it evaluates a single resource. Live evidence: a bundle whose deploy.sh
    had already built the 48 MB dependency bundle, created the bucket and uploaded
    all three artifacts died there with exit 254.

    Nothing else in this file detects it, because every other test reads the
    template through ``yaml.safe_load``, which resolves aliases and returns exactly
    the structure the assertions expect. cfn-lint passes too. Only a real
    CreateChangeSet — or this test — sees it.
    """

    ANCHOR_OR_ALIAS = re.compile(r"[&*]id\d+")

    @ALL_COMBINATIONS
    def test_no_anchor_or_alias_is_emitted(self, combo):
        text = _generate(**combo).template_yaml
        found = self.ANCHOR_OR_ALIAS.findall(text)
        assert not found, (
            f"emitted template contains YAML anchors/aliases {sorted(set(found))}; "
            "CloudFormation rejects the whole template. Some value is one shared "
            "Python object reachable from two places — build it per use site."
        )

    def test_the_dumper_would_repeat_a_shared_node_anyway(self):
        """Belt and braces, asserted separately from the absence of shared objects.

        The two halves of the fix fail differently: a new shared constant is a
        mistake anyone can make next month, while the dumper is what stops that
        mistake being fatal. Testing only the emitted output would let the dumper be
        removed silently.
        """
        from app.services.cfn_template_generator import _dump_template

        shared = {"Ref": "Shared"}
        text = _dump_template({"A": shared, "B": shared})
        assert not self.ANCHOR_OR_ALIAS.search(text)
        assert text.count("Ref: Shared") == 2
        assert yaml.safe_load(text) == {"A": {"Ref": "Shared"}, "B": {"Ref": "Shared"}}


class TestLambdasCanBePlacedInAVpc:
    """The recipient can put the functions on their own network, or not.

    Every emitted Lambda ran outside any VPC with unrestricted egress, and there was
    no parameter to change it — so a recipient whose controls require least-privilege
    egress (ARCC cnt_dh50RmkA8h91jK) had to fork the template. Checkov reported it as
    CKV_AWS_117 and it was in the accepted baseline as a known gap.
    """

    @ALL_COMBINATIONS
    def test_every_lambda_takes_the_vpc_config(self, combo):
        resources = _template(**combo)["Resources"]
        functions = [k for k, v in resources.items() if v["Type"] == "AWS::Lambda::Function"]
        assert functions, "no Lambda in this combination — the assertion below would be vacuous"
        for logical_id in functions:
            vpc = resources[logical_id]["Properties"]["VpcConfig"]["Fn::If"]
            assert vpc[0] == "HasLambdaVpcConfig", logical_id
            assert vpc[1] == {
                "SubnetIds": {"Ref": "LambdaSubnetIds"},
                "SecurityGroupIds": {"Ref": "LambdaSecurityGroupIds"},
            }
            # The other branch must be NoValue, not an empty list: an empty
            # SubnetIds is rejected, so a stack that does not want a VPC has to omit
            # the property entirely.
            assert vpc[2] == {"Ref": "AWS::NoValue"}, logical_id

    def test_it_is_off_by_default(self):
        """An existing stack must not change behaviour on redeploy."""
        params = _template()["Parameters"]
        assert params["LambdaSubnetIds"]["Default"] == ""
        assert params["LambdaSecurityGroupIds"]["Default"] == ""
        # CommaDelimitedList, not List<AWS::EC2::Subnet::Id>: the typed version
        # rejects an empty value, so it could not have an "outside a VPC" default.
        assert params["LambdaSubnetIds"]["Type"] == "CommaDelimitedList"

    @ALL_COMBINATIONS
    def test_the_lambda_roles_can_create_the_network_interface(self, combo):
        """Otherwise the stack goes green and every invocation then fails.

        Attaching a Lambda to a VPC makes it create an ENI, and it is the execution
        role that has to be allowed to. AWSLambdaBasicExecutionRole does not grant
        it, so without this the failure is "The provided execution role does not have
        permissions to call CreateNetworkInterface on EC2" — at invoke time, after a
        successful deploy.
        """
        resources = _template(**combo)["Resources"]
        lambda_roles = {
            resources[k]["Properties"]["Role"]["Fn::GetAtt"][0]
            for k, v in resources.items()
            if v["Type"] == "AWS::Lambda::Function"
        }
        assert lambda_roles
        for role_id in lambda_roles:
            managed = resources[role_id]["Properties"]["ManagedPolicyArns"]
            conditional = [m for m in managed if isinstance(m, dict) and "Fn::If" in m]
            assert conditional, f"{role_id} cannot create an ENI in a VPC"
            branch = conditional[0]["Fn::If"]
            assert branch[0] == "HasLambdaVpcConfig"
            assert "AWSLambdaVPCAccessExecutionRole" in branch[1]["Fn::Sub"]
            # Off by default too: a recipient not using a VPC gains no ec2 access.
            assert branch[2] == {"Ref": "AWS::NoValue"}

    def test_no_security_group_is_emitted(self):
        """A default group would have to allow egress to something.

        Anything permissive enough to work is what the recipients who want this are
        trying to prevent, and anything tight enough depends on their endpoint
        layout. So they supply it, and the README says what access is needed.
        """
        for combo in COMPONENT_COMBINATIONS.values():
            resources = _template(**combo)["Resources"]
            assert not [k for k, v in resources.items() if v["Type"].startswith("AWS::EC2::")], (
                "the template creates network resources it cannot know the right shape of"
            )

    def test_the_readme_says_which_endpoints_are_needed(self):
        """A private subnet with no route produces a timeout, not an error message.

        That is the single most expensive way for this to go wrong, so the README has
        to name the endpoints rather than just the parameters.
        """
        readme = _generate().readme
        assert "Running the Lambdas in your VPC" in readme
        assert "LAMBDA_SECURITY_GROUP_IDS" in readme
        assert "com.amazonaws.<region>.s3" in readme
        # The boundary interaction: it has to permit the ENI actions or it caps away
        # the grant the template just added.
        assert "ec2:CreateNetworkInterface" in readme


class TestARuntimeVpcCanvasIsNotSilentlyDowngraded:
    """A canvas that asks for a VPC must not export a runtime on the open network.

    The live path honours ``vpc_config``: ``runtime_deployer._build_network_configuration``
    returns ``networkMode: VPC`` with the subnets and security groups. Every runtime
    this template emits hardcodes ``NetworkMode: PUBLIC``. So the export used to hand
    back a runtime with unrestricted egress on a canvas that had been deliberately
    confined to a private network — a weakening of a network control, in the one
    direction nobody re-checks, with nothing in the template or the stack events
    saying so.

    The end state is for the export to emit VPC mode, which the resource supports
    (``NetworkConfiguration.NetworkModeConfig {Subnets, SecurityGroups}``, confirmed
    against the live CloudFormation resource schema). Until then the export refuses,
    because a wrong network posture must not be the quiet outcome.
    """

    @staticmethod
    def _export(**config_kwargs):
        config = RuntimeConfig(name="exporttest", model={"modelId": MODEL_ID}, **config_kwargs)
        return CfnTemplateGenerator().generate(DeployRequest(config=config, nodeId="node-1"))

    @pytest.mark.parametrize(
        "config_kwargs",
        [
            pytest.param(
                {"vpc_config": {"subnet_ids": ["subnet-1"], "security_group_ids": ["sg-1"]}},
                id="vpc-config-snake-case",
            ),
            pytest.param(
                {"vpc_config": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}},
                id="vpc-config-camel-case",
            ),
            # A profile is a *named* set of subnets and security groups, resolved to
            # vpc_config at the deploy boundary — which the export path never crosses.
            # So this canvas reaches the generator with vpc_config still None, and a
            # check that read only vpc_config would let exactly this one through.
            pytest.param({"vpc_profile": "regulated-private"}, id="vpc-profile"),
        ],
    )
    def test_a_vpc_canvas_is_refused_rather_than_exported_as_public(self, config_kwargs):
        with pytest.raises(CfnExportUnsupportedError) as exc:
            self._export(**config_kwargs)
        message = str(exc.value)
        assert "VPC" in message
        # The message has to say what the recipient would have got instead, or the
        # refusal reads like a missing feature rather than a protected control.
        assert "unrestricted egress" in message
        # And both ways out: deploy through the platform, or drop the VPC config.
        assert "through the platform" in message
        # The Lambda half already works; not saying so would send someone to fork the
        # template for something it already has parameters for.
        assert "LambdaSubnetIds" in message

    @pytest.mark.parametrize(
        "vpc_config",
        [
            pytest.param({"subnet_ids": ["subnet-1"]}, id="subnets-without-security-groups"),
            pytest.param({"security_group_ids": ["sg-1"]}, id="security-groups-without-subnets"),
            pytest.param({}, id="empty"),
        ],
    )
    def test_an_inert_vpc_config_is_not_refused(self, vpc_config):
        """The refusal asks the live path's own function, so it agrees with it.

        ``_build_network_configuration`` treats an incomplete vpc_config as PUBLIC —
        it would not send a VPC create call for it either. Refusing here would then
        block an export the platform deploys on the open network anyway: a refusal
        that protects nothing and blocks something.
        """
        template = yaml.safe_load(self._export(vpc_config=vpc_config).template_yaml)
        assert template["Resources"]["AgentCoreRuntime"]["Properties"]["NetworkConfiguration"] == {
            "NetworkMode": "PUBLIC"
        }


class TestServiceTrustIsScopedToThisAccount:
    """A trust policy that names only a service trusts every account's copy of it.

    A role trust policy is a resource policy. ``Principal: {Service: bedrock}`` with
    no condition lets Bedrock assume the role for whoever it is acting for, including
    a caller in another account who gets Bedrock to reach into this one. ARCC
    cnt_vBC0kXE8PNHqrW requires aws:SourceAccount / aws:SourceArn on service-to-service
    access for exactly this reason.

    The condition can only go on principals confirmed to send the source headers —
    one that does not simply fails to assume the role, and the failure names the
    wrong resource. So every principal in the allowlist was proven live with a
    wrong-value control, and these tests pin the shape that was proven, including the
    narrowing that would pass a gateway test and break every memory deploy.
    """

    def _trust_statements(self, template):
        return {
            logical_id: resource["Properties"]["AssumeRolePolicyDocument"]["Statement"]
            for logical_id, resource in template["Resources"].items()
            if resource["Type"] == "AWS::IAM::Role"
        }

    def test_the_knowledge_base_role_is_scoped_to_this_account(self):
        template = _template(gateway_config=AGENTCORE_GATEWAY, knowledge_base_config=KB_CONFIG)
        statement = template["Resources"]["KnowledgeBaseRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement["Principal"]["Service"] == "bedrock.amazonaws.com"
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
        assert "aws:SourceArn" in statement["Condition"]["ArnLike"]

    @ALL_COMBINATIONS
    def test_every_bedrock_principal_carries_the_condition(self, combo):
        """Not just the KB role — the pass is generic, so a new one is covered too."""
        for logical_id, statements in self._trust_statements(_template(**combo)).items():
            for statement in statements:
                if statement.get("Principal", {}).get("Service") == "bedrock.amazonaws.com":
                    assert "aws:SourceAccount" in statement.get("Condition", {}).get("StringEquals", {}), (
                        f"{logical_id} trusts Bedrock from any account"
                    )

    @ALL_COMBINATIONS
    def test_every_agentcore_principal_carries_the_condition(self, combo):
        """Verified live before this was allowed to become an assertion.

        Both keys were proven on the gateway path (stack CREATE_COMPLETE and a real
        MCP ``tools/call`` that returned the target Lambda's own output) and on the
        memory path (``CreateMemory`` reached ACTIVE), each against a wrong-value
        control that failed — which is the part that makes the pass mean anything,
        since an absent context key makes the condition evaluate false.
        """
        seen = 0
        for logical_id, statements in self._trust_statements(_template(**combo)).items():
            for statement in statements:
                if statement.get("Principal", {}).get("Service") != "bedrock-agentcore.amazonaws.com":
                    continue
                seen += 1
                condition = statement.get("Condition", {})
                assert condition.get("StringEquals", {}).get("aws:SourceAccount") == {"Ref": "AWS::AccountId"}, (
                    f"{logical_id} trusts AgentCore from any account"
                )
                assert "aws:SourceArn" in condition.get("ArnLike", {}), logical_id
        assert seen, "no AgentCore trust statement in this combination — the loop asserted nothing"

    @ALL_COMBINATIONS
    def test_the_agentcore_source_arn_is_not_narrowed_to_one_resource_type(self, combo):
        """The narrowing that passes every gateway test and breaks every memory deploy.

        aws:SourceArn is the ARN of the calling AgentCore resource, so it is
        ``gateway/...`` for a gateway role and ``memory/...`` for a memory execution
        role. Verified live in both directions: ``...:*`` works for both, while
        ``...:gateway/*`` was rejected by CreateMemory with "Please provide a role
        with a valid trust policy". One shared allowlist entry cannot be tighter than
        the union.
        """
        for logical_id, statements in self._trust_statements(_template(**combo)).items():
            for statement in statements:
                if statement.get("Principal", {}).get("Service") != "bedrock-agentcore.amazonaws.com":
                    continue
                arn = statement["Condition"]["ArnLike"]["aws:SourceArn"]["Fn::Sub"]
                assert arn.endswith(":*"), (
                    f"{logical_id} narrows the AgentCore source ARN to {arn!r}; if this is deliberate the "
                    "condition has to be emitted per role type, not per service principal"
                )

    def test_an_existing_condition_is_merged_not_replaced(self):
        """A pass that overwrites Condition would loosen a policy while looking strict."""
        from app.services.cfn_template_generator import _apply_trust_source_conditions

        statement = {
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Condition": {"StringEquals": {"sts:ExternalId": "keep-me"}},
        }
        assert _apply_trust_source_conditions(statement) is True
        assert statement["Condition"]["StringEquals"]["sts:ExternalId"] == "keep-me"
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

    def test_an_unlisted_principal_is_left_alone(self):
        from app.services.cfn_template_generator import _apply_trust_source_conditions

        statement = {"Principal": {"Service": "lambda.amazonaws.com"}}
        assert _apply_trust_source_conditions(statement) is False
        assert "Condition" not in statement


class TestMcpServerToolsAreGeneratable:
    """An MCP tool we cannot generate is refused at export, not dropped.

    ``mcpServerConfig.tools`` accepts strings or dicts under any of three name keys,
    so the code generator cannot distinguish a tool it has no implementation for from
    one the caller mistyped: both produce a server missing that tool. The recipient
    learns about it from a CloudFormation event saying the gateway target has no
    tools, or not at all — the runtime just comes up short one tool.
    """

    _BASE = {
        "template_id": "mcp-server-gateway-target",
        "gateway_config": AGENTCORE_GATEWAY,
    }

    def _export(self, tools):
        return _template(**self._BASE, mcp_server_config={"tools": tools})

    def test_an_unimplemented_tool_is_refused(self):
        with pytest.raises(CfnExportUnsupportedError) as err:
            self._export(["refund_everything"])
        assert "refund_everything" in str(err.value)
        # The message has to say what to do about it, not just that it failed.
        assert "implementation" in str(err.value)

    def test_a_tool_with_a_body_is_accepted(self):
        template = self._export([{"name": "refund_everything", "implementation": "return 'ok'"}])
        assert "McpServerRuntime" in template["Resources"]

    def test_a_builtin_tool_needs_no_body(self):
        template = self._export(["process_refund"])
        assert "McpServerRuntime" in template["Resources"]

    def test_a_nameless_entry_is_refused(self):
        with pytest.raises(CfnExportUnsupportedError) as err:
            self._export([{"description": "does something"}])
        assert "no name" in str(err.value)

    def test_a_name_that_is_not_a_python_identifier_is_refused(self):
        # It becomes a def in the generated server, so this would be a SyntaxError
        # inside the runtime container rather than an export failure.
        with pytest.raises(CfnExportUnsupportedError) as err:
            self._export([{"name": "process refund", "code": "pass"}])
        assert "identifier" in str(err.value)

    def test_the_message_names_every_bad_entry_not_just_the_first(self):
        with pytest.raises(CfnExportUnsupportedError) as err:
            self._export(["refund_everything", "wire_money"])
        assert "refund_everything" in str(err.value)
        assert "wire_money" in str(err.value)

    def test_an_empty_list_still_exports(self):
        # generate_mcp_server_code deliberately substitutes its sample tools here, so
        # this is a warning rather than a refusal — and every other combination in this
        # file passes tools=[], so making it an error would be a large behaviour change.
        assert "McpServerRuntime" in self._export([])["Resources"]

    def test_an_empty_list_says_so_in_the_log(self, caplog):
        with caplog.at_level("WARNING"):
            self._export([])
        assert any("sample support tools" in r.getMessage() for r in caplog.records)


class TestLambdaLogsAreOwnedAndBounded:
    """Every Lambda's log group belongs to the stack and expires.

    A Lambda with no log group resource creates its own on first invocation. That
    group is not part of the stack, so a stack delete leaves it behind, and CloudWatch
    Logs retains indefinitely by default (ARCC cnt_qf7wYkuSRSM5fl), so it keeps
    whatever the agent was asked and answered for ever. Deploy and destroy a few
    times and the account accumulates log groups nobody owns and nobody deletes.
    """

    @ALL_COMBINATIONS
    def test_every_lambda_has_an_owned_log_group(self, combo):
        resources = _template(**combo)["Resources"]
        functions = [k for k, v in resources.items() if v["Type"] == "AWS::Lambda::Function"]
        assert functions, "no Lambda in this combination — the assertion below would be vacuous"
        for logical_id in functions:
            group_id = f"{logical_id}LogGroup"
            assert resources.get(group_id, {}).get("Type") == "AWS::Logs::LogGroup", (
                f"{logical_id} would create its own log group outside the stack"
            )
            assert resources[logical_id]["Properties"]["LoggingConfig"] == {"LogGroup": {"Ref": group_id}}
            assert group_id in resources[logical_id]["DependsOn"]

    @ALL_COMBINATIONS
    def test_every_log_group_expires(self, combo):
        for logical_id, resource in _template(**combo)["Resources"].items():
            if resource["Type"] != "AWS::Logs::LogGroup":
                continue
            assert resource["Properties"]["RetentionInDays"] == {"Ref": "LogRetentionInDays"}, (
                f"{logical_id} would retain logs indefinitely"
            )

    def test_the_group_name_is_not_the_one_lambda_would_have_created(self):
        """Otherwise this cannot be applied to a stack already in service.

        Adding a log group under the default `/aws/lambda/<function>` name to a stack
        whose Lambda has already run fails the update with "already exists": the group
        exists but belongs to nobody, so CloudFormation cannot adopt it. Naming it
        under the stack means the name has never existed.
        """
        resources = _template(**COMPONENT_COMBINATIONS["everything"])["Resources"]
        for logical_id, resource in resources.items():
            if resource["Type"] != "AWS::Logs::LogGroup":
                continue
            name = resource["Properties"]["LogGroupName"]["Fn::Sub"]
            assert "${AWS::StackName}" in name, f"{logical_id} would collide with the group Lambda creates: {name}"

    def test_the_retention_parameter_only_accepts_values_cloudwatch_takes(self):
        param = _template()["Parameters"]["LogRetentionInDays"]
        assert param["Default"] == 30
        # An unlisted value is rejected at create time with an unhelpful message, so
        # the allowed set is pinned where the failure is legible.
        assert 30 in param["AllowedValues"]
        assert 45 not in param["AllowedValues"]

    def test_log_groups_survive_a_stack_delete_by_default(self):
        for logical_id, resource in _template(**COMPONENT_COMBINATIONS["everything"])["Resources"].items():
            if resource["Type"] != "AWS::Logs::LogGroup":
                continue
            # Logs are the only record of what the agent was asked and answered. A
            # stack delete should not be what destroys an audit trail.
            assert resource["DeletionPolicy"] == "Retain", logical_id

    def test_log_group_retention_follows_the_recipients_choice(self):
        """One knob, not two.

        The groups used to carry a hardcoded Retain, which meant a demo stack that
        asked for Delete still leaked a log group per Lambda on every teardown. They
        are data-bearing resources now, so the data-retention policy governs them.
        """
        for logical_id, resource in _template(**COMPONENT_COMBINATIONS["everything"], data_retention_policy="Delete")[
            "Resources"
        ].items():
            if resource["Type"] != "AWS::Logs::LogGroup":
                continue
            assert resource["DeletionPolicy"] == "Delete", logical_id
            assert resource["UpdateReplacePolicy"] == "Delete", logical_id


RUNTIME_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/"


def _governance_resources(template):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == "Custom::RuntimeLogGroup"}


def _governed_names(resource):
    """The literal ``Fn::Sub`` strings a governance resource will resolve."""
    return [entry["Fn::Sub"][0] for entry in resource["Properties"]["LogGroupNames"]]


class TestTheRuntimesOwnLogGroupsAreGoverned:
    """The agent's conversations live in groups no declared resource can reach.

    ``AWS::BedrockAgentCore::Runtime`` has no logging or encryption properties at all.
    AgentCore creates ``/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>`` itself,
    at stack-create time and before any invoke, with no retention and no key — so the
    groups that hold what the agent was asked and answered were the only data in this
    export that ignored both ``LogRetentionInDays`` and ``CustomerManagedKeyArn``.
    Declaring them as ``AWS::Logs::LogGroup`` is impossible: they already exist and
    belong to nobody, so CloudFormation cannot adopt them. Hence a Custom Resource
    that creates-or-adopts.

    Verified live before any of this was written: runtime
    ``logprobe0918_runtime-pRuBlMHsuB`` with one named endpoint produced BOTH
    ``-DEFAULT`` and ``-logprobe0918_endpoint``, both ungoverned, and an invoke against
    the named qualifier wrote to the NAMED group while ``-DEFAULT`` stayed empty. A pass
    that governed only ``-DEFAULT`` would have governed the empty one and looked right.
    """

    @ALL_COMBINATIONS
    def test_every_runtime_gets_exactly_one_governance_resource(self, combo):
        resources = _template(**combo)["Resources"]
        runtimes = {k for k, v in resources.items() if v["Type"] == "AWS::BedrockAgentCore::Runtime"}
        assert runtimes, "no runtime in this combination — the assertions below would be vacuous"
        governed = {k: v for k, v in resources.items() if v["Type"] == "Custom::RuntimeLogGroup"}
        assert set(governed) == {f"{runtime}LogGroups" for runtime in runtimes}

    @ALL_COMBINATIONS
    def test_every_endpoint_of_every_runtime_is_covered(self, combo):
        """The one that catches the real mistake. An endpoint added to the generator
        without being added here gets a log group that is never governed, and nothing
        else in the template or the stack events would say so.
        """
        resources = _template(**combo)["Resources"]
        for runtime in [k for k, v in resources.items() if v["Type"] == "AWS::BedrockAgentCore::Runtime"]:
            names = _governed_names(resources[f"{runtime}LogGroups"])
            # DEFAULT always exists: the service creates it whether or not the
            # template declares a named endpoint.
            assert any(name.endswith("-DEFAULT") for name in names), f"{runtime}: -DEFAULT is ungoverned"
            for endpoint_id, endpoint in resources.items():
                if endpoint["Type"] != "AWS::BedrockAgentCore::RuntimeEndpoint":
                    continue
                if endpoint["Properties"]["AgentRuntimeId"]["Fn::GetAtt"][0] != runtime:
                    continue
                qualifier = endpoint["Properties"]["Name"]["Fn::Sub"]
                assert any(name.endswith(f"-{qualifier}") for name in names), (
                    f"{endpoint_id}'s log group is ungoverned; the service will still create it"
                )

    @ALL_COMBINATIONS
    def test_the_names_are_built_from_the_id_the_service_mints(self, combo):
        """``AgentRuntimeId`` is not knowable at template-authoring time, which is the
        other half of why this cannot be a declared log group."""
        resources = _template(**combo)["Resources"]
        for logical_id, resource in _governance_resources(_template(**combo)).items():
            runtime = logical_id.removesuffix("LogGroups")
            for entry in resource["Properties"]["LogGroupNames"]:
                template_string, variables = entry["Fn::Sub"]
                assert template_string.startswith(f"{RUNTIME_LOG_PREFIX}${{RuntimeId}}-"), template_string
                assert variables == {"RuntimeId": {"Fn::GetAtt": [runtime, "AgentRuntimeId"]}}
        assert resources  # the loop above is over the same template

    @ALL_COMBINATIONS
    def test_no_two_entries_share_one_dict_object(self, combo):
        """A YAML anchor is not a template. ``yaml.dump`` emits ``&id001``/``*id001``
        for the same object reached twice, and CloudFormation rejects the file outright
        — so the identical ``{"RuntimeId": ...}`` maps have to be rebuilt per entry,
        not shared. This failed once already and the error was at deploy time.
        """
        for resource in _governance_resources(_template(**combo)).values():
            variable_maps = [entry["Fn::Sub"][1] for entry in resource["Properties"]["LogGroupNames"]]
            assert len({id(m) for m in variable_maps}) == len(variable_maps), "shared object will become a YAML alias"

    @ALL_COMBINATIONS
    def test_retention_is_the_same_knob_as_the_declared_log_groups(self, combo):
        """One parameter governs all logs, per ARCC cnt_sSTfcrsdyTSviN item 6. Two knobs
        is how a recipient ends up with the Lambda logs expiring and the conversations
        kept for ever."""
        for logical_id, resource in _governance_resources(_template(**combo)).items():
            assert resource["Properties"]["RetentionInDays"] == {"Ref": "LogRetentionInDays"}, logical_id

    @ALL_COMBINATIONS
    def test_governance_waits_for_the_endpoints_it_names(self, combo):
        """The endpoint is what makes the service create the named group. Running before
        it means creating the group ourselves — which works, but then the group exists
        with our key before the service has ever written to it, and any ordering bug
        there is invisible. Depend on the endpoint instead.
        """
        resources = _template(**combo)["Resources"]
        for logical_id, resource in _governance_resources(_template(**combo)).items():
            runtime = logical_id.removesuffix("LogGroups")
            endpoints = [
                k
                for k, v in resources.items()
                if v["Type"] == "AWS::BedrockAgentCore::RuntimeEndpoint"
                and v["Properties"]["AgentRuntimeId"]["Fn::GetAtt"][0] == runtime
            ]
            if not endpoints:
                continue
            assert set(endpoints) <= set(resource.get("DependsOn", [])), f"{logical_id} does not wait for {endpoints}"

    def test_the_provider_role_can_govern_these_groups_and_only_these(self):
        """``logs:DisassociateKmsKey`` on ``log-group:*`` would let this Lambda make an
        unrelated team's existing log data unreadable. The grant is prefix-scoped for
        that reason and not merely for tidiness.
        """
        template = _template(**COMPONENT_COMBINATIONS["everything"])
        policies = [
            p
            for p in _inline_policies(_roles(template)["CfnProviderRole"])
            if p["PolicyName"] == "RuntimeLogGroupGovernance"
        ]
        assert len(policies) == 1, "the provider role cannot govern the runtime log groups"
        for statement in policies[0]["PolicyDocument"]["Statement"]:
            actions = set(_as_list(statement["Action"]))
            assert {"logs:PutRetentionPolicy", "logs:AssociateKmsKey", "logs:DisassociateKmsKey"} <= actions
            for resource in _as_list(statement["Resource"]):
                assert resource["Fn::Sub"].endswith(f"log-group:{RUNTIME_LOG_PREFIX}*"), (
                    f"the grant is not scoped to the runtime log groups: {resource}"
                )

    def test_the_readme_says_the_logs_survive_a_teardown(self):
        """They do, and deliberately — the handler's Delete is a no-op, so a recipient
        who tears the stack down still finds the groups and is charged for them. Saying
        so, with the command to remove them, is the difference between a documented
        choice and a surprise.
        """
        for policy in ("Retain", "Delete"):
            readme = _generate(**COMPONENT_COMBINATIONS["everything"], data_retention_policy=policy).readme
            assert "aws logs delete-log-group" in readme, f"{policy}: no way to remove them is documented"
            assert RUNTIME_LOG_PREFIX in readme, f"{policy}: the group names are not stated"


class TestOutputs:
    @ALL_COMBINATIONS
    def test_the_endpoint_name_is_an_output_because_invoking_needs_it(self, combo):
        """`invoke-agent-runtime --qualifier` takes the endpoint NAME, not an ARN.

        The stack exported EndpointArn and nothing else, so the only way to obtain a
        qualifier was to know that it is the ARN's last path segment and split it
        yourself. The alternative a recipient actually reaches for is typing the name
        from memory, which fails against any stack whose DeploymentName differs.
        """
        outputs = _template(**combo)["Outputs"]
        assert "EndpointName" in outputs, "nothing in the outputs supplies an invoke qualifier"

    def test_the_endpoint_name_output_cannot_drift_from_the_endpoint_it_names(self):
        """The output is only useful if it is the name of the endpoint that exists.

        Pinned as an equality against the resource rather than against a literal:
        a rename of the endpoint that forgets the output would otherwise pass here
        and hand every recipient a qualifier for an endpoint that is not there.
        """
        template = _template(**COMPONENT_COMBINATIONS["everything"])
        assert (
            template["Outputs"]["EndpointName"]["Value"]
            == template["Resources"]["RuntimeEndpoint"]["Properties"]["Name"]
        )

    def test_a_knowledge_base_stack_exports_its_ids(self):
        """The knowledge base was created and then never named in the outputs.

        Every supported operation on it — starting an ingestion job, attaching
        another data source, querying it from outside the agent — needs the id, and a
        caller had no way to get one except by listing every knowledge base in the
        account and guessing by name.
        """
        outputs = _template(**COMPONENT_COMBINATIONS["gateway+kb"])["Outputs"]
        for required in ("KnowledgeBaseId", "KnowledgeBaseArn", "KnowledgeBaseDataSourceId"):
            assert required in outputs, f"missing {required}"

    def test_the_ingestion_command_is_emitted_because_nothing_ingests_automatically(self):
        # CloudFormation creates the data source and ingests nothing, so a stack that
        # deployed cleanly has an empty knowledge base and an agent that answers from
        # nothing. The command needs both ids, which is why it is an output rather
        # than a sentence in the README.
        bundle = _generate(**COMPONENT_COMBINATIONS["gateway+kb"])
        command = yaml.safe_load(bundle.template_yaml)["Outputs"]["KnowledgeBaseIngestCommand"]["Value"]["Fn::Sub"]
        assert "bedrock-agent start-ingestion-job" in command
        assert "${BedrockKnowledgeBase.KnowledgeBaseId}" in command
        assert "${KBDataSource.DataSourceId}" in command
        assert "Ingesting Documents Into the Knowledge Base" in bundle.readme

    def test_no_knowledge_base_output_without_a_knowledge_base(self):
        outputs = _template(gateway_config=AGENTCORE_GATEWAY)["Outputs"]
        assert not [k for k in outputs if "KnowledgeBase" in k]

    def test_gateway_stack_exports_everything_needed_to_get_a_token(self):
        """The stack used to export GatewayUrl and nothing else about auth.

        It creates the Cognito pool, resource server, domain and
        client_credentials client, so a caller could see the endpoint but had no
        supported way to mint a token for it.
        """
        outputs = _template(gateway_config=AGENTCORE_GATEWAY)["Outputs"]
        for required in ("CognitoUserPoolId", "CognitoClientId", "CognitoTokenEndpoint", "CognitoScope"):
            assert required in outputs, f"missing {required} — caller cannot authenticate"

    def test_token_endpoint_is_built_from_the_domain_this_stack_creates(self):
        endpoint = _template(gateway_config=AGENTCORE_GATEWAY)["Outputs"]["CognitoTokenEndpoint"]["Value"]
        sub = endpoint["Fn::Sub"]
        assert "${CognitoUserPoolDomain}" in sub, "must reference the stack's own domain resource"
        assert "${AWS::Region}" in sub, "must not hardcode a region"
        assert sub.endswith("/oauth2/token")

    def test_scope_matches_the_resource_server_the_stack_creates(self):
        template = _template(gateway_config=AGENTCORE_GATEWAY)
        scope = template["Outputs"]["CognitoScope"]["Value"]["Fn::Sub"]
        allowed = template["Resources"]["CognitoUserPoolClient"]["Properties"]["AllowedOAuthScopes"]
        assert scope in [s["Fn::Sub"] for s in allowed], (
            "the advertised scope must be one the app client is actually allowed to request"
        )

    def test_no_output_resolves_a_secret(self):
        """Outputs are readable by anyone who can DescribeStacks, and Terraform
        writes them into state. ARCC cnt_9OT33u5q3kyAPq: keep secrets off the
        template surface. The client secret is available via Fn::GetAtt, so this
        is a live foot-gun, not a hypothetical one.
        """
        outputs = _template(**COMPONENT_COMBINATIONS["everything"])["Outputs"]
        for key, value in _iter_nodes(outputs):
            if key == "Fn::GetAtt":
                assert not any("Secret" in str(part) for part in value), (
                    f"an Output resolves a secret attribute: {value}"
                )

    def test_client_secret_is_documented_rather_than_exported(self):
        """Refusing to output it is only helpful if we say where to get it."""
        outputs = _template(gateway_config=AGENTCORE_GATEWAY)["Outputs"]
        assert "CognitoClientSecretCommand" in outputs
        command = outputs["CognitoClientSecretCommand"]["Value"]["Fn::Sub"]
        assert "describe-user-pool-client" in command

    def test_no_cognito_outputs_when_there_is_no_gateway(self):
        outputs = _template()["Outputs"]
        assert not [k for k in outputs if k.startswith("Cognito")]


class TestParameters:
    @ALL_COMBINATIONS
    def test_no_parameter_carries_a_secret_without_noecho(self, combo):
        """Forward-looking guard rather than a fixed bug.

        Today every parameter is a name or an S3 key, so nothing needs NoEcho.
        The LiteLLM branch will want to accept a virtual key, and the tempting
        wrong answer is a plain parameter: CloudFormation retains parameter
        values, they are readable by anyone who can operate the stack, and
        Terraform persists them in state. Per ARCC cnt_9OT33u5q3kyAPq the value
        must arrive as a Secrets Manager reference. Fail the build if a
        secret-shaped parameter ever appears without NoEcho.
        """
        secret_words = ("secret", "password", "token", "apikey", "api_key", "credential", "privatekey")
        for name, spec in _template(**combo).get("Parameters", {}).items():
            lowered = name.lower()
            if any(word in lowered for word in secret_words):
                # An ARN is a reference, not the secret itself, and is safe in the clear.
                if "arn" in lowered:
                    continue
                # A Number cannot carry a credential: CloudFormation rejects any
                # value that is not numeric, so the widest thing this type can hold
                # is a bounded integer. AccessTokenValidityMinutes is the case that
                # matters - a token *lifetime*, which must stay readable precisely
                # because an operator has to be able to see what it is set to.
                # The String-typed scan below is deliberately left as strict as it
                # was: that is where a virtual key would actually arrive.
                if spec.get("Type") == "Number":
                    continue
                assert spec.get("NoEcho") is True, f"parameter {name} looks sensitive but has no NoEcho"


class TestNoSecretReachesAStackEvent:
    """The other half of the NoEcho story, and the half that actually leaked.

    ``TestParameters`` above guards template PARAMETERS. This guards resource
    PROPERTIES, which is a separate exposure with a separate mechanism and no
    mitigation available at all:

    CloudFormation copies the fully-resolved ``ResourceProperties`` of a resource —
    NATIVE types included, not only ``Custom::`` ones, measured live as 78 of 110
    events covering every logical id in the stack — into the stack's event stream, on
    every event in every status, and retains those events for 90 days. So a secret
    put in a property is
    readable by any principal with ``cloudformation:DescribeStackEvents`` — a much
    wider set than the people trusted with the credential — and there is no way to
    scrub it afterwards. ``NoEcho`` does not help, because ``NoEcho`` is a property
    of a parameter and this is a ``GetAtt`` on a resource.

    This is a fixed bug, not a forward-looking guard. The template shipped
    ``"ClientSecret": {"Fn::GetAtt": ["McpCognitoClient", "ClientSecret"]}`` on
    ``Custom::OAuth2CredentialProvider``, and on a deployed stack the 52-character
    Cognito client secret was recovered verbatim out of three stack events by
    grepping the event JSON for its known value, against a positive control. The fix
    passes ``UserPoolId`` instead and has the handler call
    ``DescribeUserPoolClient`` for itself, so the secret never enters CloudFormation.

    The same defect was then found a second time, on a NATIVE resource: the agent
    runtime's ``COGNITO_CLIENT_SECRET`` environment variable was a ``GetAtt`` on
    ``CognitoUserPoolClient.ClientSecret``, and the 51-character secret was measured in
    3 events after create and 5 after one update on the same live stack. That instance
    survived precisely because the generic scan below was scoped to ``Custom::``
    resources, so it is now scoped to all of them. That leak had a second mouth as
    well: an environment variable is also returned in plaintext by
    ``GetAgentRuntime``. Both are closed by the same fix — pass
    ``COGNITO_USER_POOL_ID`` and let the agent read the secret itself.

    Asserted generically rather than against those two property names: any future
    ``GetAtt`` on a secret-shaped attribute, on any resource, in any component
    combination, fails here. Per ARCC guidance on CloudFormation template secrets, and
    on runtime secrets (``cnt_n8LpZcqYi2t3I2``, which rejects holding a secret in an
    environment variable even when it was injected at deploy time), sensitive values
    belong in Secrets Manager or behind a runtime API call, never in template
    properties and never in a runtime's configuration.
    """

    # Attributes whose value IS the credential. Matched on the attribute name at the
    # end of a GetAtt, so "SecretArn" (a reference) does not trip it but
    # "ClientSecret" (the thing itself) does.
    SECRET_ATTRS = ("clientsecret", "password", "privatekey", "secretstring")

    @ALL_COMBINATIONS
    def test_no_resource_property_getatts_a_secret(self, combo):
        template = _template(**combo)
        offenders = []

        def walk(node, path):
            if isinstance(node, dict):
                target = node.get("Fn::GetAtt")
                if target is not None:
                    parts = target.split(".") if isinstance(target, str) else list(target)
                    attr = str(parts[-1]).lower() if parts else ""
                    if any(word in attr for word in self.SECRET_ATTRS):
                        offenders.append(f"{path} -> Fn::GetAtt {target}")
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        # EVERY resource, not just Custom::. Scoping this to custom resources is what
        # left the second instance open: the identical secret was live in the events of
        # AgentCoreRuntime, a native type, while this test was passing.
        for name, resource in template.get("Resources", {}).items():
            if not isinstance(resource, dict):
                continue
            walk(resource.get("Properties", {}), f"{name}.Properties")

        assert not offenders, (
            "a resource property resolves to a secret, which CloudFormation then copies "
            "into this stack's events for 90 days where DescribeStackEvents can read it, "
            "and NoEcho cannot prevent it. Pass a reference that is resolved at runtime "
            "instead — see _resolve_client_secret in cfn_provider/handler.py for the "
            "custom-resource shape, and the generated agent.py's _resolve_client_secret "
            f"for the runtime shape. Offending properties: {offenders}"
        )

    def test_the_oauth2_provider_gets_the_pool_and_not_the_secret(self):
        """The specific regression, pinned so the generic scan above cannot be satisfied
        by removing the resource rather than fixing it."""
        template = _template(**COMPONENT_COMBINATIONS["mcp-server"])
        props = template["Resources"]["McpOAuth2CredentialProvider"]["Properties"]
        assert "ClientSecret" not in props, "the client secret is back in a stack-event-visible property"
        assert props["UserPoolId"] == {"Ref": "McpCognitoUserPool"}, (
            "the handler needs the pool id to read the secret from Cognito itself"
        )

    @pytest.mark.parametrize("combo_name", ["gateway", "gateway+memory", "everything"])
    def test_the_runtime_gets_the_pool_and_not_the_secret(self, combo_name):
        """The native-resource instance, pinned the same way and for the same reason.

        Also asserts the grant, because the two are what make each other correct: a
        runtime told to read its own secret without ``DescribeUserPoolClient`` fails at
        its first gateway call, which is a worse outcome than the leak it replaced.
        """
        template = _template(**COMPONENT_COMBINATIONS[combo_name])
        env = _runtime_env(template)
        assert "COGNITO_CLIENT_SECRET" not in env, (
            "the client secret is back in the runtime's environment, where it is both "
            "echoed into stack events and returned in plaintext by GetAgentRuntime"
        )
        assert env["COGNITO_USER_POOL_ID"] == {"Ref": "CognitoUserPool"}, (
            "the agent needs the pool id to read the secret from Cognito itself"
        )
        statements = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        grants = [s for s in statements if "cognito-idp:DescribeUserPoolClient" in s.get("Action", [])]
        assert len(grants) == 1, f"expected exactly one DescribeUserPoolClient grant, got {grants}"
        assert grants[0]["Resource"] == {"Fn::GetAtt": ["CognitoUserPool", "Arn"]}, (
            "DescribeUserPoolClient returns the client secret of whatever pool it is "
            "given, so this must be scoped to this stack's pool and never a wildcard: "
            f"{grants[0]['Resource']!r}"
        )


class TestAnUpdateActuallyReachesTheRunningCode:
    """Two defects that both produce a green UPDATE_COMPLETE over unchanged behaviour.

    Both were proven live on the agent runtime, fixed there, and then found still
    present on the MCP server runtime — which is the reason these are written as
    invariants over every matching resource instead of as assertions about the two
    resources that happened to be caught.

    1. A code-package key that does not move. The Runtime resource takes its code
       prefix from the package resource's ``CodeZipPrefix`` attribute, so if the
       output key is a constant the attribute is a constant, every property of the
       Runtime is unchanged, and CloudFormation publishes no new version. Live: a
       changed system prompt reached UPDATE_COMPLETE with only version 1 ever
       existing, and the runtime kept answering with the old prompt's marker. The key
       must therefore be content-addressed on both halves of the merged zip — the
       agent code AND the dependency bundle, since upgrading a pinned library with no
       code change is the same no-op by the other route.

    2. An endpoint that does not follow the version. ``AgentRuntimeId`` and ``Name``
       are both createOnly on a RuntimeEndpoint, so leaving ``AgentRuntimeVersion``
       unset means nothing about the resource ever changes and it serves the version
       that existed when it was created, permanently. Live: after an update the
       DEFAULT endpoint served v2 correctly while the NAMED endpoint — the one the
       stack's EndpointArn output points at, and the only qualifier a recipient is
       told to use — still served v1.
    """

    @ALL_COMBINATIONS
    def test_every_runtime_endpoint_pins_its_runtime_version(self, combo):
        endpoints = {
            name: res
            for name, res in _template(**combo).get("Resources", {}).items()
            if isinstance(res, dict) and res.get("Type") == "AWS::BedrockAgentCore::RuntimeEndpoint"
        }
        for name, res in endpoints.items():
            pinned = res.get("Properties", {}).get("AgentRuntimeVersion")
            assert pinned is not None, (
                f"{name} does not pin AgentRuntimeVersion, so it will serve whatever "
                "version existed when it was created and never move again"
            )
            assert isinstance(pinned, dict) and "Fn::GetAtt" in pinned, (
                f"{name} must take the version from its runtime via Fn::GetAtt, not a "
                f"literal, or it pins the wrong version forever: {pinned!r}"
            )

    @staticmethod
    def _parameters_reaching_the_rendered_string(expr):
        """The parameter names that actually change the value ``expr`` renders to.

        Deliberately not a substring search over the serialized expression. The first
        version of this test did that, and it passed against a template whose key had
        been reverted to a constant: an ``Fn::Sub`` carries a variable MAP, and a
        variable that the format string never interpolates is dead weight that still
        appears in the JSON. So the assertion held while the rendered key did not move
        — the exact defect, reported green. Resolve the string instead: collect only the
        placeholders the string really uses, then follow each one into the map.
        """
        if not isinstance(expr, dict):
            return set()
        body = expr.get("Fn::Sub")
        if body is None:
            return {ref for ref in _walk_for_refs(expr)}
        fmt, variables = (body, {}) if isinstance(body, str) else (body[0], body[1])
        used = set()
        for placeholder in re.findall(r"\$\{([^}]+)\}", fmt):
            if placeholder in variables:
                # A local variable: whatever it is built from is what moves the key.
                used |= set(_walk_for_refs(variables[placeholder]))
            else:
                # A direct reference to a parameter or pseudo-parameter.
                used.add(placeholder)
        return used

    @ALL_COMBINATIONS
    def test_every_code_package_key_moves_with_both_digests(self, combo):
        packages = {
            name: res
            for name, res in _template(**combo).get("Resources", {}).items()
            if isinstance(res, dict) and res.get("Type") == "Custom::AgentCodePackage"
        }
        assert packages, "no code packages in this combination, so this proves nothing"
        for name, res in packages.items():
            props = res.get("Properties", {})
            key = props.get("OutputKey")
            moves_with = self._parameters_reaching_the_rendered_string(key)
            # Whatever parameter THIS package treats as its source digest, rather than a
            # hardcoded name: the agent runtime and the MCP server runtime have separate
            # code parameters and the MCP half is the one that was missed.
            source = set(_walk_for_refs(props.get("SourceDigest")))
            assert source, f"{name} has no SourceDigest to key on: {props.get('SourceDigest')!r}"
            assert source <= moves_with, (
                f"{name}'s OutputKey renders without {sorted(source - moves_with)}, so "
                "changing that code re-uploads the bytes under the same key, leaves "
                f"CodeZipPrefix identical, and publishes no new runtime version: {key!r}"
            )
            assert "DependencyBundleDigest" in moves_with, (
                f"{name}'s OutputKey renders without DependencyBundleDigest, so upgrading "
                f"a pinned dependency with no code change is a silent no-op: {key!r}"
            )


class TestTheExportedModelIsTheDesignedModel:
    """A CREATE_COMPLETE stack running a model nobody chose.

    ``ModelId``'s default was hardcoded to ``us.anthropic.claude-sonnet-5`` and
    ignored ``config.model`` entirely, and deploy.sh never passed the parameter
    (``grep -c ModelId deploy.sh`` was 0), so a canvas built on Opus 4.8 exported a
    template that deployed Sonnet 5. Nothing failed: the stack completed and the
    agent answered questions, just not as the agent that was designed. The README
    made it worse by documenting ``config.model.get("id", ...)`` — the canonical key
    is ``modelId`` (see ``code_generator._get_model_id``), so that lookup always
    missed and the parameter table advertised the fallback model as well.
    """

    CANVAS_MODEL = "us.anthropic.claude-opus-4-8"

    def _bundle(self, model_id=None):
        config = RuntimeConfig(name="modeltest", model={"modelId": model_id or self.CANVAS_MODEL})
        return CfnTemplateGenerator().generate(DeployRequest(nodeId="node-1", config=config))

    def test_the_parameter_default_is_the_canvas_model(self):
        bundle = self._bundle()
        default = yaml.safe_load(bundle.template_yaml)["Parameters"]["ModelId"]["Default"]
        assert default == self.CANVAS_MODEL, "the export deploys a model the canvas did not choose"

    def test_the_template_and_the_agent_code_cannot_disagree(self):
        """The two halves of the same decision, and the one that made the bug
        invisible: the generated agent code always had the right model, so reading
        the code proved nothing about what the stack would run."""
        bundle = self._bundle()
        default = yaml.safe_load(bundle.template_yaml)["Parameters"]["ModelId"]["Default"]
        assert default in bundle.agent_code or f'"{default}"' in bundle.agent_code, (
            "the code and the template name different models"
        )

    def test_the_readme_names_the_same_model(self):
        rows = [line for line in self._bundle().readme.splitlines() if line.startswith("| ModelId")]
        assert rows, "the parameter table does not document ModelId"
        assert self.CANVAS_MODEL in rows[0], f"README advertises a different model: {rows[0]}"

    def test_the_recipient_can_change_it_without_editing_the_template(self):
        script = self._bundle().deploy_sh
        assert 'PARAM_OVERRIDES+=("ModelId=$MODEL_ID")' in script
        assert "MODEL_ID:-" in script, "the override must be opt-in, not a required variable"
        # The script has to say which model it is about to deploy; the whole defect
        # was that nobody could see it.
        assert self.CANVAS_MODEL in script

    def test_the_cross_region_prefix_is_repointed_for_the_platform_region(self, monkeypatch):
        """A ``us.`` inference profile does not exist in eu-central-1, and the agent
        fails at invoke time rather than at deploy time. Reading the canvas value
        must not mean passing it through verbatim."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        default = yaml.safe_load(self._bundle().template_yaml)["Parameters"]["ModelId"]["Default"]
        assert default == "eu.anthropic.claude-opus-4-8"


class TestThePolicyWaitsForTheToolsItNames:
    """A policy cannot be created before the targets whose actions it names.

    Proven live, on a stack whose only defect was the missing dependency:

        unrecognized action `AgentCore::Action::"KBTool___knowledge_base_query"`
        ... did you mean `AgentCore::Action::"InvokeAgent"`?
        * unable to find an applicable action given the policy scope constraints

    ``AgentCore::Action`` is a closed set the policy engine derives by enumerating
    the *gateway's targets* at validation time, so a tool action does not exist
    until its target does. The policy declared ``DependsOn`` on the PolicyEngine and
    the Gateway only, and CloudFormation is free to create it before any target —
    which it did. "did you mean InvokeAgent" is the tell: that is the built-in
    action set a gateway with no tools has.

    This failed *every* policy-engine export, so the assertion is that the
    dependency is derived from the template rather than hardcoded to one target
    name: the target set depends on the canvas, and the MCP-server path adds a
    second one under a different logical id.
    """

    POLICY_COMBINATIONS = {
        "gateway+policy": COMPONENT_COMBINATIONS["gateway+policy"],
        "gateway+kb+default-policy": COMPONENT_COMBINATIONS["gateway+kb+default-policy"],
        "everything": COMPONENT_COMBINATIONS["everything"],
    }

    @pytest.mark.parametrize("combo", POLICY_COMBINATIONS.values(), ids=list(POLICY_COMBINATIONS))
    def test_every_gateway_target_is_a_dependency_of_every_policy(self, combo):
        template = _template(**combo)
        resources = template["Resources"]
        targets = {lid for lid, res in resources.items() if res.get("Type") == "AWS::BedrockAgentCore::GatewayTarget"}
        policies = {lid for lid, res in resources.items() if res.get("Type") == "Custom::AgentCorePolicy"}
        assert policies, "this combination emits no policy; the parametrization is stale"
        for lid in policies:
            depends = set(resources[lid].get("DependsOn") or [])
            missing = targets - depends
            assert not missing, f"{lid} can be created before {sorted(missing)}, whose actions its statement names"

    def test_the_kb_tool_target_is_what_the_default_statement_names(self):
        """Guards against the assertion above passing vacuously.

        If the KB combination ever stopped emitting a target, ``targets`` would be
        empty and the subset check would hold for a template that still cannot
        deploy. So pin the specific pairing that failed live: the generated default
        statement names the KB tool action, and the KB target is the resource that
        brings that action into existence.
        """
        template = _template(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"])
        assert template["Resources"]["KBToolTarget"]["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
        policy = template["Resources"]["DefaultPolicy"]
        # The statement is an Fn::Sub so it can name the gateway ARN, so match the
        # substitutable text rather than the resolved string.
        assert "KBTool___knowledge_base_query" in policy["Properties"]["Statement"]["Fn::Sub"]
        assert "KBToolTarget" in policy["DependsOn"]


class TestABedrockModelIsNamedByTheArnItsOwnKindUses:
    """A cross-region inference profile is not a foundation model.

    ``us.anthropic.claude-sonnet-5`` is a profile, and profiles are
    account-qualified: ``…:bedrock:REGION:ACCOUNT:inference-profile/us.anthropic.…``.
    The template hardcoded the unqualified ``foundation-model/`` path for every
    model id it emitted, which CloudFormation accepts — the ARN is only a string to
    it — so the stack reached CREATE_COMPLETE and failed at invoke:

        ValidationException ... Received validation exception when calling
        GetInferenceProfile for
        arn:aws:bedrock:us-east-1::foundation-model/us.anthropic.claude-sonnet-5

    and it arrived in the KB Lambda's *response body* with ``FunctionError: null``,
    so an invoke-based smoke test reads it as a pass. IAM review passes it too,
    because the role's ``Resource`` list grants both families.

    The inverse matters just as much: embedding models have no cross-region profiles
    at all, so ``amazon.titan-embed-text-v2:0`` must keep the unqualified
    foundation-model form. Both directions are asserted, because a fix that
    profile-qualified everything would break the KB instead of the KB tool.
    """

    PROFILE_MODEL = "us.anthropic.claude-sonnet-5"
    ON_DEMAND_MODEL = "amazon.titan-embed-text-v2:0"

    def _kb(self, **overrides):
        return _template(gateway_config=AGENTCORE_GATEWAY, knowledge_base_config={**KB_CONFIG, **overrides})

    @staticmethod
    def _sub(value):
        assert isinstance(value, dict) and "Fn::Sub" in value, f"expected an Fn::Sub ARN, got {value!r}"
        return value["Fn::Sub"]

    def test_a_geography_prefixed_kb_tool_model_is_a_profile_arn(self):
        env = self._kb(foundationModelId=self.PROFILE_MODEL)["Resources"]["KBToolLambda"]["Properties"]["Environment"]
        arn = self._sub(env["Variables"]["FOUNDATION_MODEL_ARN"])
        assert arn == (
            "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}"
            f":inference-profile/{self.PROFILE_MODEL}"
        )

    def test_a_plain_kb_tool_model_stays_a_foundation_model_arn(self):
        env = self._kb(foundationModelId="anthropic.claude-sonnet-5")["Resources"]["KBToolLambda"]["Properties"][
            "Environment"
        ]
        arn = self._sub(env["Variables"]["FOUNDATION_MODEL_ARN"])
        assert arn == "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/anthropic.claude-sonnet-5"

    def test_an_embedding_model_is_never_profile_qualified(self):
        """Titan and Cohere embeddings publish no inference profiles, so a
        profile-shaped ARN here fails the KB create rather than an invoke."""
        kb = self._kb(embeddingModelId=self.ON_DEMAND_MODEL)["Resources"]["BedrockKnowledgeBase"]
        arn = self._sub(
            kb["Properties"]["KnowledgeBaseConfiguration"]["VectorKnowledgeBaseConfiguration"]["EmbeddingModelArn"]
        )
        assert arn == f"arn:${{AWS::Partition}}:bedrock:${{AWS::Region}}::foundation-model/{self.ON_DEMAND_MODEL}"

    def test_no_emitted_model_arn_puts_a_profile_under_foundation_model(self):
        """The whole template, not the three sites known to have had the bug.

        Any ``foundation-model/`` ARN whose model id carries a geography prefix is
        the same defect wherever it appears, and this catches the next site to be
        added without a test of its own.
        """
        yaml_text = _generate(
            gateway_config=AGENTCORE_GATEWAY,
            knowledge_base_config={
                **KB_CONFIG,
                "parsingStrategy": "bedrock_foundation_model",
                "parsingModelId": self.PROFILE_MODEL,
            },
        ).template_yaml
        offenders = re.findall(r"foundation-model/((?:us|eu|apac|ap|global)\.[^\s'\"}]+)", yaml_text)
        # A `bedrock:*::foundation-model/BASE` wildcard grant is legitimate — the
        # base model behind a profile — so only prefixed ids are a defect, and the
        # generator strips the prefix before emitting that one.
        assert not offenders, f"cross-region profiles emitted as foundation models: {sorted(set(offenders))}"

    def test_the_kb_role_can_invoke_the_parsing_model_it_is_configured_with(self):
        """Bedrock parsing was configured and never authorized.

        The KB role was granted the embedding model only, so a KB with
        ``bedrock_foundation_model`` parsing ingested nothing — and the failure
        surfaces on the data-source sync, not on the stack, so the stack is green.
        The parsing default is itself geography-prefixed, which is why this needs
        both the profile ARN and the underlying foundation model: Bedrock invokes
        the profile, which fans out to the base model in some region.
        """
        template = self._kb(parsingStrategy="bedrock_foundation_model", parsingModelId=self.PROFILE_MODEL)
        statements = [
            st
            for policy in _inline_policies(template["Resources"]["KnowledgeBaseRole"])
            for st in policy["PolicyDocument"]["Statement"]
            if st.get("Sid") == "ParsingModelInvoke"
        ]
        assert statements, "the parsing model is configured but the role cannot invoke it"
        resources = [self._sub(r) for r in _as_list(statements[0]["Resource"])]
        assert any("inference-profile/" + self.PROFILE_MODEL in r for r in resources)
        assert any(r.endswith("foundation-model/anthropic.claude-sonnet-5") for r in resources), (
            "invoking a cross-region profile also authorizes against the base model"
        )


class TestDeploymentNameCannotBeAValueThatGuaranteesRollback:
    """The parameter's own ``AllowedPattern`` used to admit values that cannot deploy.

    ``DeploymentName`` is interpolated into a Cognito hosted-UI domain prefix
    (``ac-${DeploymentName}-${AWS::AccountId}``) and into S3 Vectors bucket names.
    Neither accepts uppercase, and a bucket-style name cannot start with a digit —
    but the pattern was ``^[a-zA-Z][a-zA-Z0-9]{0,39}$``, so ``MyAgent`` passed
    validation and then failed mid-create, after other resources existed.

    Three places have to agree on the rule: the pattern, the default, and deploy.sh's
    derivation of ``DEPLOY_NAME`` from the stack name. Tightening the pattern alone
    would have moved the failure rather than removing it — deploy.sh derived
    ``MyStack`` → ``MyStack`` and would now be refused at validation.
    """

    PATTERN = re.compile("^[a-z][a-z0-9]{0,39}$")

    @ALL_COMBINATIONS
    def test_the_pattern_admits_only_deployable_values(self, combo):
        spec = _template(**combo)["Parameters"]["DeploymentName"]
        assert spec["AllowedPattern"] == self.PATTERN.pattern
        assert "ConstraintDescription" in spec, "a rejected value must come with the reason"

    @pytest.mark.parametrize(
        "name",
        ["MyAgent", "ECB Demo Agent", "9lives", "has-hyphens", "under_score", "x" * 60, "!!!"],
    )
    def test_the_default_is_deployable_whatever_the_canvas_was_called(self, name):
        config = RuntimeConfig(name=name, model={"modelId": MODEL_ID})
        bundle = CfnTemplateGenerator().generate(DeployRequest(nodeId="node-1", config=config))
        default = yaml.safe_load(bundle.template_yaml)["Parameters"]["DeploymentName"]["Default"]
        assert self.PATTERN.match(default), f"canvas name {name!r} produced an unusable default {default!r}"

    @pytest.mark.parametrize(
        "stack_name",
        ["MyStack", "9agent", "a-very-long-stack-name-with-hyphens", "UPPER", "123"],
    )
    def test_deploy_sh_derives_a_value_the_template_will_accept(self, stack_name, tmp_path):
        """Runs the emitted lines rather than reading them. The failure mode is a
        validation error quoting a regex and naming no cause."""
        script = _generate().deploy_sh
        lines = [line for line in script.splitlines() if line.startswith("DEPLOY_NAME=")]
        assert lines, "deploy.sh no longer derives DEPLOY_NAME; this test is stale"
        probe = tmp_path / "probe.sh"
        probe.write_text("\n".join(["set -euo pipefail", f'STACK_NAME="{stack_name}"', *lines, 'echo "$DEPLOY_NAME"']))
        proc = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        assert proc.returncode == 0, proc.stderr
        derived = proc.stdout.strip()
        assert self.PATTERN.match(derived), f"stack name {stack_name!r} derives {derived!r}, which the template rejects"


class TestTheScriptsDefaultToTheRecipientsRegion:
    """The region default was the region the *exporting platform* ran in.

    It reached the script through ``current_region()``, so it was not a hardcoded
    literal -- but from the recipient's seat that is a distinction without a
    difference: run ``./deploy.sh my-stack`` with the argument omitted and the stack
    landed in whatever region the machine that generated the bundle happened to be
    using, which may be a region they do not operate in at all. Nothing said so.

    The fix inserts the AWS CLI's own precedence in front of it, so an omitted
    argument now agrees with every other ``aws`` command on the recipient's machine.
    The export-time region survives only as a last resort, which is the one case
    where there is genuinely nothing better to guess.
    """

    def test_the_default_consults_the_recipients_own_configuration(self):
        deploy = _generate().deploy_sh
        assert "AWS_REGION:-" in deploy, "an omitted argument must respect the caller's own region"
        assert "AWS_DEFAULT_REGION:-" in deploy
        assert "aws configure get region" in deploy

    def test_teardown_resolves_the_region_exactly_as_deploy_does(self):
        """The dangerous asymmetry, not a style point.

        A teardown that defaults to a different region than the deploy finds no stack
        of that name, and `delete-stack` on a nonexistent stack is not an error -- so
        it reports success at having deleted nothing while the real stack keeps
        running and keeps billing. Pinned as an equality between the two scripts so
        the two defaults cannot drift apart.
        """
        bundle = _generate()
        deploy = [line for line in bundle.deploy_sh.splitlines() if "AWS_DEFAULT_REGION" in line]
        teardown = [line for line in bundle.teardown_sh.splitlines() if "AWS_DEFAULT_REGION" in line]
        assert deploy and deploy == teardown, f"deploy resolves {deploy}, teardown resolves {teardown}"

    def test_the_export_time_region_is_only_the_last_resort(self):
        """It must still be there -- a recipient with no region configured anywhere
        would otherwise get an empty `--region ""` and a confusing CLI error -- but it
        must come after the caller's own configuration, not before it.
        """
        deploy = _generate().deploy_sh
        fallback = [line for line in deploy.splitlines() if line.startswith('REGION="${REGION:-')]
        assert len(fallback) == 1, fallback
        assert current_region() in fallback[0]
        assert deploy.index("AWS_REGION:-") < deploy.index(fallback[0]), (
            "the export-time region is being consulted before the recipient's own"
        )

    def test_both_scripts_say_which_region_they_picked(self):
        # A default that is now resolved at run time instead of baked in is only safe
        # if the operator can see what it resolved to.
        bundle = _generate()
        echo = 'echo "Region: $REGION"'
        # Exactly once each. deploy.sh already printed the region in its banner, so the
        # obvious place to add this -- next to the resolution -- makes a single run
        # announce its region twice, which reads like the script changed its mind.
        assert bundle.deploy_sh.count(echo) == 1, "deploy.sh announces its region twice"
        assert bundle.teardown_sh.count(echo) == 1

    def test_deploy_names_the_region_before_it_calls_aws(self):
        """Printing it after the first API call is too late to be a warning."""
        deploy = _generate().deploy_sh
        assert deploy.index('echo "Region: $REGION"') < deploy.index("aws sts get-caller-identity")

    def test_the_readme_examples_do_not_bake_in_the_exporting_machines_region(self):
        """Seventeen example commands used to carry the exporter's region as the value.

        The same root cause as the scripts, and it survived the fix to them: every
        ``./deploy.sh my-agent <region>`` and ``REGION=`` in the README was rendered from
        ``current_region()``, so a bundle exported from a machine configured for one
        region suggested that region to a recipient who may not operate in it -- while
        the scripts it documents now default to the recipient's own. A README that
        contradicts the tool it describes is worse than one that says nothing.

        Asserted against an implausible region rather than the real one, so the test
        cannot pass by coincidence if a genuine region name appears in prose somewhere.
        """
        with mock.patch.object(cfn_template_generator, "current_region", return_value="xx-nowhere-9"):
            bundle = _generate()
        assert "xx-nowhere-9" not in bundle.readme
        assert "YOUR-REGION" in bundle.readme, "the placeholder the rest of the README uses"
        # The scripts are the one place it belongs: there it is the last-resort default,
        # not an example, and it is only reached when the recipient set nothing at all.
        assert "xx-nowhere-9" in bundle.deploy_sh
        assert "xx-nowhere-9" in bundle.teardown_sh


class TestTheShippedScriptsAreExecutable:
    """``zipfile.writestr`` with a plain string name stores mode 0600.

    Every script we tell the recipient to run arrived non-executable, so the
    README's own first command failed with ``permission denied`` (exit 126). The
    quick-start's ``chmod +x`` hid it for anyone who copied the whole block; the
    recipient who ran ``./build-dependency-bundle.sh`` on its own did not.
    """

    EXECUTABLE = {"deploy.sh", "teardown.sh", "build-dependency-bundle.sh"}

    @ALL_COMBINATIONS
    def test_every_script_the_recipient_is_told_to_run_has_the_execute_bit(self, combo):
        archive = zipfile.ZipFile(io.BytesIO(_generate(**combo).to_zip()))
        found = set()
        for info in archive.infolist():
            name = info.filename.rsplit("/", 1)[-1]
            if name not in self.EXECUTABLE:
                continue
            found.add(name)
            mode = (info.external_attr >> 16) & 0o777
            assert mode & 0o111, f"{info.filename} arrives mode {mode:04o}; the recipient cannot run it"
        assert found == self.EXECUTABLE, f"missing from the bundle: {self.EXECUTABLE - found}"

    @ALL_COMBINATIONS
    def test_nothing_else_becomes_executable(self, combo):
        """The execute bit belongs on the three scripts and nowhere else — an
        executable template.yaml is a signal that the mode is being set wholesale."""
        archive = zipfile.ZipFile(io.BytesIO(_generate(**combo).to_zip()))
        for info in archive.infolist():
            if info.filename.rsplit("/", 1)[-1] in self.EXECUTABLE:
                continue
            mode = (info.external_attr >> 16) & 0o777
            assert not mode & 0o111, f"{info.filename} is executable and should not be"


# ---------------------------------------------------------------------------
# The custom resources, and the documentation of them
# ---------------------------------------------------------------------------


class TestCustomResources:
    """Alvaro Fernandez-Moris asked whether the three custom resources in
    ``cfn_provider/handler.py`` were the complete set. They were — but the module
    docstring, the internals doc and the generated README all said there was one,
    which is very likely what prompted the question. These tests make the three
    statements impossible to drift apart again.

    There are four now: ``Custom::RuntimeLogGroup`` was added for the log groups
    AgentCore creates for the runtime itself, which no declared resource can reach.
    """

    EXPECTED = {
        "Custom::AgentCodePackage",
        "Custom::OAuth2CredentialProvider",
        "Custom::AgentCorePolicy",
        "Custom::RuntimeLogGroup",
    }

    def test_the_generator_emits_no_custom_resource_type_the_handler_cannot_serve(self):
        emitted = set()
        for combo in COMPONENT_COMBINATIONS.values():
            emitted |= {r["Type"] for r in _template(**combo)["Resources"].values() if r["Type"].startswith("Custom::")}
        assert emitted <= self.EXPECTED, f"generator emits unserved types: {emitted - self.EXPECTED}"

    def test_the_handler_serves_exactly_what_the_generator_emits(self):
        """Reads the handler's own allowlist so the two cannot disagree.

        The handler used to default any unrecognized type to the code-packaging
        branch, so this mismatch was silent: a wrong type ran the wrong handler
        and reported SUCCESS.
        """
        handler = _import_cfn_provider_handler()
        assert handler.SUPPORTED_RESOURCE_TYPES == self.EXPECTED

    def test_all_four_are_reachable_from_some_canvas(self):
        """A type in the allowlist that nothing emits is dead code; a type
        something emits that is not in the allowlist now fails closed."""
        emitted = set()
        for combo in COMPONENT_COMBINATIONS.values():
            emitted |= {r["Type"] for r in _template(**combo)["Resources"].values() if r["Type"].startswith("Custom::")}
        assert emitted == self.EXPECTED, f"never emitted by any combination: {self.EXPECTED - emitted}"

    def test_readme_lists_the_custom_resources_this_bundle_contains(self):
        """The generated README is customer-facing and shipped inside the zip;
        it is the artifact that told people there was only one."""
        readme = _generate(**COMPONENT_COMBINATIONS["everything"]).readme
        for resource_type in self.EXPECTED:
            assert resource_type in readme, f"README does not mention {resource_type}"

    def test_readme_does_not_claim_a_single_custom_resource(self):
        for name, combo in COMPONENT_COMBINATIONS.items():
            readme = _generate(**combo).readme.lower()
            for false_claim in ("only one custom resource", "no custom resource lambdas are needed"):
                assert false_claim not in readme, f"{name}: README still claims '{false_claim}'"

    def test_runtime_only_readme_does_not_advertise_resources_it_lacks(self):
        readme = _generate().readme
        assert "Custom::AgentCodePackage" in readme
        # Present in every bundle, because every bundle has a runtime and every
        # runtime gets log groups the recipient has to be told about.
        assert "Custom::RuntimeLogGroup" in readme
        assert "Custom::AgentCorePolicy" not in readme, "README lists a custom resource this bundle has no use for"
        assert "Custom::OAuth2CredentialProvider" not in readme

    @ALL_COMBINATIONS
    def test_the_provider_lambda_keeps_its_asynchronous_retries(self, combo):
        """The handler's recovery from an undeliverable response depends on them.

        ``handler.py`` fails the whole invocation when it cannot PUT its response to
        the pre-signed URL, because Lambda's async retry is then the only thing that
        can still reach CloudFormation; without it the stack blocks on the resource
        for the full one-hour custom-resource timeout and then rolls back. Two is the
        Lambda default, so the template states it explicitly and this pins the value
        rather than the mere presence of the config.
        """
        template = _template(**combo)
        if "CfnProviderLambda" not in template["Resources"]:
            pytest.skip("this combination needs no custom resources")
        configs = [r for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::EventInvokeConfig"]
        assert len(configs) == 1, "the provider Lambda's async retry behaviour is unstated"
        props = configs[0]["Properties"]
        assert props["FunctionName"] == {"Ref": "CfnProviderLambda"}
        assert props["MaximumRetryAttempts"] >= 1, "an undelivered response then has no second chance"


class TestStagingBucketIsHardened:
    """The bucket deploy.sh creates holds the code the agent executes.

    ``deploy.sh`` used to create it with a bare ``aws s3 mb`` and nothing else, so the
    merged ``code.zip`` that the AgentCore Runtime downloads and runs under the agent's
    execution role sat in a bucket with no public-access block, no default encryption, no
    versioning and no TLS requirement. The digest checks elsewhere in this file catch a
    substituted artifact; these settings are what stops one being put there in the first
    place, and what makes the previous version recoverable when one is.

    ARCC guidance applied: cnt_QbO3G5Nzv7jmP7 (all four public-access-block flags),
    cnt_XL9e2sbGgxAvce (versioning MUST come with a non-current expiry),
    cnt_TFTC9MGIxuXhqa (deny requests that did not arrive over TLS).
    """

    @staticmethod
    def _creation_branch(script: str) -> str:
        """Only the branch taken when the script creates the bucket itself.

        Asserting against the whole script would pass on a version that hardened a
        bucket it did not create, which is the thing this must not do.
        """
        after_mb = script.split('aws s3 mb "s3://$BUCKET"')[1]
        return after_mb.split("\nfi\n")[0]

    @ALL_COMBINATIONS
    def test_a_bucket_it_creates_blocks_public_access(self, combo):
        branch = self._creation_branch(_generate(**combo).deploy_sh)
        assert "put-public-access-block" in branch
        for flag in ("BlockPublicAcls=true", "IgnorePublicAcls=true", "BlockPublicPolicy=true"):
            assert flag in branch, f"{flag} is not set; a public ACL or policy would be accepted"
        # The one most often left out, and the one that neutralises a public policy that
        # is already attached rather than only rejecting a new one.
        assert "RestrictPublicBuckets=true" in branch

    @ALL_COMBINATIONS
    def test_a_bucket_it_creates_is_encrypted_by_default(self, combo):
        branch = self._creation_branch(_generate(**combo).deploy_sh)
        assert "put-bucket-encryption" in branch
        assert '"SSEAlgorithm":"AES256"' in branch
        assert '"BucketKeyEnabled":true' in branch

    @ALL_COMBINATIONS
    def test_versioning_never_ships_without_an_expiry(self, combo):
        """The pairing ARCC cnt_XL9e2sbGgxAvce requires.

        Every re-export overwrites the same keys, so versioning with no expiry grows the
        bucket by one code.zip and one bundle per deploy, forever, and the recipient pays
        for it. Asserted as a pair so neither half can be added alone.
        """
        branch = self._creation_branch(_generate(**combo).deploy_sh)
        assert "Status=Enabled" in branch, "versioning is what makes an overwrite recoverable"
        lifecycle = json.loads(re.search(r"<<'LIFECYCLE_JSON'\n(.*?)\nLIFECYCLE_JSON", branch, re.DOTALL).group(1))
        ((rule,),) = (lifecycle["Rules"],)
        assert rule["Status"] == "Enabled"
        assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] > 0
        assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] > 0

    @ALL_COMBINATIONS
    def test_a_bucket_it_creates_refuses_plaintext_requests(self, combo):
        branch = self._creation_branch(_generate(**combo).deploy_sh)
        raw = re.search(r"<<TLS_POLICY_JSON\n(.*?)\nTLS_POLICY_JSON", branch, re.DOTALL).group(1)
        # The partition must still be a shell variable at this point: resolving it to a
        # literal "aws" gives GovCloud and China a Deny that matches nothing.
        assert "$PARTITION" in raw
        policy = json.loads(raw.replace("$PARTITION", "aws").replace("$BUCKET", "example-bucket"))
        ((statement,),) = (policy["Statement"],)
        assert statement["Effect"] == "Deny"
        assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        # Both ARNs: the bucket one covers ListBucket, the /* one covers the objects.
        assert set(statement["Resource"]) == {"arn:aws:s3:::example-bucket", "arn:aws:s3:::example-bucket/*"}

    @ALL_COMBINATIONS
    def test_the_partition_is_read_from_the_caller_not_assumed(self, combo):
        script = _generate(**combo).deploy_sh
        assert "PARTITION=$(printf '%s' \"$CALLER_ARN\" | cut -d: -f2)" in script

    @ALL_COMBINATIONS
    def test_a_bucket_that_already_exists_is_not_reconfigured(self, combo):
        """It is the recipient's bucket, and versioning cannot be undone.

        Reaching into a bucket the operator already had — possibly shared, possibly
        governed by their own controls — is a bigger surprise than a printed notice. The
        notice has to name the README section that tells them how, though, or this is
        just a silent gap with a message in front of it.
        """
        script = _generate(**combo).deploy_sh
        before_else = script.split('aws s3 mb "s3://$BUCKET"')[0]
        existing = before_else.split('head-bucket --bucket "$BUCKET"')[1]
        assert "left untouched" in existing
        assert "README.md > Staging Bucket" in existing
        for command in ("put-public-access-block", "put-bucket-encryption", "put-bucket-versioning"):
            assert command not in existing, f"{command} runs against a bucket the recipient already had"

    def test_the_readme_section_the_notice_points_at_exists(self):
        readme = _generate().readme
        assert "## Staging Bucket" in readme
        # Every command the script runs on a bucket it creates, so an operator can apply
        # them to one it does not.
        for command in (
            "put-public-access-block",
            "put-bucket-encryption",
            "put-bucket-versioning",
            "put-bucket-lifecycle-configuration",
            "put-bucket-policy",
        ):
            assert command in readme, f"the README does not tell the recipient how to run {command}"
        assert "left exactly as it is" in readme

    def test_the_readme_and_the_script_apply_the_same_documents(self):
        """Two copies of a JSON policy diverge; the divergence is invisible.

        Both come from the same module constant, and this is what says so — the README's
        copy is what a recipient will run against a bucket this script refuses to touch,
        so it being *nearly* the same is worse than useless.
        """
        from app.services.cfn_template_generator import (
            _STAGING_BUCKET_LIFECYCLE_JSON,
            _STAGING_BUCKET_TLS_POLICY_JSON,
        )

        bundle = _generate()
        for document in (_STAGING_BUCKET_LIFECYCLE_JSON, _STAGING_BUCKET_TLS_POLICY_JSON):
            assert document in bundle.deploy_sh
            assert document in bundle.readme

    @ALL_COMBINATIONS
    def test_the_hardened_script_is_still_valid_bash(self, combo):
        """Heredocs and embedded JSON inside an f-string is exactly where this breaks.

        Every brace in the deploy.sh f-string has to be doubled, which is why these two
        documents are module constants — and this is the gate that would have caught it
        either way. The recipient runs this file; a syntax error in it is a total failure
        discovered by them.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(_generate(**combo).deploy_sh)
            path = handle.name
        try:
            result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        finally:
            Path(path).unlink()


# ---------------------------------------------------------------------------
# The recipient must be able to deploy without asking us for a file
# ---------------------------------------------------------------------------


class TestDependencyBundleIsObtainable:
    """The export was undeployable by anyone outside this repository.

    The template requires the dependency bundle to already be in S3, the README
    said "upload the pre-built dependency bundle" and never said where to get it,
    and deploy.sh exited 1 pointing back at that README. The bundle is a gitignored
    build artifact of scripts/install-agentcore-deps.sh and is not in the download,
    so an external recipient was hard-blocked at prerequisite 3.
    """

    def _install_script_packages(self):
        """The package list the platform's own build script uses.

        Parsed rather than duplicated: two hand-maintained copies of a dependency
        list diverge, and the failure mode is a stack that deploys and then dies at
        import with a 30s-timeout message that names nothing useful.
        """
        script = Path(__file__).resolve().parents[2] / "scripts" / "install-agentcore-deps.sh"
        text = script.read_text()
        otel = re.search(r"local otel_packages=\(\s*(.*?)\)", text, re.DOTALL).group(1)
        otel_pkgs = re.findall(r'"([^"]+)"', otel)
        pin = re.search(r'local mcp_pin="([^"]+)"', text).group(1)

        def literals(target):
            """The literal package names on one install_packages line.

            Shell expansions (``"${otel_packages[@]}"``, ``"${mcp_pin}"``) are
            dropped and substituted back by the caller, so a package added to the
            otel array or a changed pin is still compared.
            """
            line = re.search(rf'install_packages "\$\{{{target}\}}" (.*)', text).group(1)
            tokens = [t.strip('"') for t in line.split()]
            return [t for t in tokens if "${" not in t]

        return {
            "base": [*literals("base_dir"), *otel_pkgs],
            "strands": [*literals("strands_dir"), pin, *otel_pkgs],
        }

    def test_the_exported_recipe_matches_the_platforms_own_build(self):
        from app.services.cfn_template_generator import (
            BASE_BUNDLE_KEY,
            DEPENDENCY_BUNDLE_PACKAGES,
            STRANDS_BUNDLE_KEY,
        )

        expected = self._install_script_packages()
        assert sorted(DEPENDENCY_BUNDLE_PACKAGES[BASE_BUNDLE_KEY]) == sorted(expected["base"])
        assert sorted(DEPENDENCY_BUNDLE_PACKAGES[STRANDS_BUNDLE_KEY]) == sorted(expected["strands"])

    def test_the_mcp_pin_survives_into_the_exported_recipe(self):
        """Unpinned mcp broke every gateway agent and every generated MCP server.

        mcp 2.x renamed both `streamablehttp_client` and
        `mcp.server.fastmcp.FastMCP` with no back-compat alias, so the container
        died at import and the user saw only "Runtime initialization time
        exceeded". Asserted separately from the list comparison because this is the
        one entry whose *form* matters, not just its presence.
        """
        script = _generate(**COMPONENT_COMBINATIONS["gateway"]).build_bundle_sh
        assert '"mcp<2"' in script

    def test_the_build_script_targets_the_runtimes_architecture(self):
        """A bundle built for the build machine imports nowhere.

        AgentCore Runtime is aarch64/cp313. Without these flags pip happily
        produces x86-64 or macOS wheels and the only symptom is, again, the 30s
        init timeout.
        """
        script = _generate().build_bundle_sh
        for flag in ("--platform manylinux2014_aarch64", "--python-version 3.13", "--only-binary=:all:"):
            assert flag in script, f"the build script does not pass {flag}"

    @ALL_COMBINATIONS
    def test_the_build_script_ships_in_the_download(self, combo):
        bundle = _generate(**combo)
        names = zipfile.ZipFile(io.BytesIO(bundle.to_zip())).namelist()
        assert any(n.endswith("/build-dependency-bundle.sh") for n in names), names

    @ALL_COMBINATIONS
    def test_the_build_script_is_valid_bash(self, combo):
        script = _generate(**combo).build_bundle_sh
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        finally:
            Path(path).unlink()

    @ALL_COMBINATIONS
    def test_the_recipe_builds_the_bundle_the_template_asks_for(self, combo):
        """Three places name the bundle and all three must agree.

        The template parameter default, the file deploy.sh builds and uploads, and
        the file the build script writes. Any mismatch uploads a bundle to a key
        the stack does not read, and the stack then fails at the code-packaging
        step with a missing-object error.
        """
        bundle = _generate(**combo)
        key = yaml.safe_load(bundle.template_yaml)["Parameters"]["DependencyBundleKey"]["Default"]
        filename = key.rsplit("/", 1)[-1]
        assert f'BUNDLE_KEY="{key}"' in bundle.deploy_sh
        assert f'BUNDLE_FILE="{filename}"' in bundle.deploy_sh
        assert f'OUT="${{1:-{filename}}}"' in bundle.build_bundle_sh

    @ALL_COMBINATIONS
    def test_deploy_builds_and_uploads_the_bundle_instead_of_giving_up(self, combo):
        script = _generate(**combo).deploy_sh
        assert "build-dependency-bundle.sh" in script
        assert 'aws s3 cp "$BUNDLE_FILE"' in script
        # The old behaviour, which must not come back.
        assert "See README.md for instructions." not in script

    @ALL_COMBINATIONS
    def test_the_readme_no_longer_tells_the_recipient_to_find_a_file_we_never_gave_them(self, combo):
        bundle = _generate(**combo)
        key = yaml.safe_load(bundle.template_yaml)["Parameters"]["DependencyBundleKey"]["Default"]
        readme = bundle.readme
        assert "build-dependency-bundle.sh" in readme
        assert key in readme, "the README must name the key the template actually reads"
        # It named strands-mcp.zip unconditionally, including in bundles whose
        # template defaults to base.zip.
        other = "base.zip" if "strands" in key else "strands-mcp.zip"
        assert other not in readme, f"the README mentions {other} but this stack uses {key}"


# ---------------------------------------------------------------------------
# The README must describe the bundle it is inside
# ---------------------------------------------------------------------------


class TestTheReadmeSaysHowToInvokeTheAgent:
    """The bundle documented how to deploy the agent and never how to call it.

    Every fact pinned here was established against the live CLI, and each one costs a
    recipient real time to rediscover. The substrings are short on purpose: the README is
    hard-wrapped, so a longer phrase fails when the line happens to break inside it,
    which tells you about the layout and nothing about the content.
    """

    @staticmethod
    def _section(readme):
        assert "## Invoking the Agent" in readme, "no invoke section at all"
        return readme.split("## Invoking the Agent")[1].split("\n## ")[0]

    @ALL_COMBINATIONS
    def test_the_section_is_present_for_every_export(self, combo):
        # Every combination deploys a runtime, so every recipient needs this.
        section = self._section(_generate(**combo).readme)
        assert "invoke-agent-runtime" in section

    def test_it_names_the_flag_whose_absence_reads_as_a_broken_deployment(self):
        """Omitting --cli-binary-format surfaces as an unexplained runtime 400.

        The rejected-locally case is self-explanatory. The other one is not: a payload
        that happens to be valid base64 is decoded to binary and sent, and the 400 comes
        back from the runtime, so it reads like the stack is broken. Naming the error
        text is the whole point of documenting it -- that is what a recipient will paste
        into a search box.
        """
        section = self._section(_generate().readme)
        assert "--cli-binary-format raw-in-base64-out" in section
        assert "Invalid base64" in section
        assert "RuntimeClientError" in section

    def test_it_puts_the_utf8_decode_message_where_the_caller_will_find_it(self):
        """The message this section first quoted is never printed to the caller.

        Measured: the terminal gets only `RuntimeClientError ... Received error (400)
        from runtime. Please check your CloudWatch logs`, and the `Invalid encoding`
        sentence exists only as a WARNING in the runtime's log group. A recipient told to
        expect it on the terminal, and not seeing it, concludes the paragraph is about
        some other problem -- the precise opposite of what documenting it was for. The
        byte in that message is also whatever the decoded payload began with (0xab in one
        run, 0xa6 in another), so the README must not name one.
        """
        section = self._section(_generate().readme)
        assert "codec can't decode byte" in section, "still the confirming detail, just not the symptom"
        assert "CloudWatch" in section, "the reader has to be told where that line lives"
        assert "0xa6" not in section and "0xab" not in section, "the byte varies with the payload"

    def test_it_deletes_the_output_file_before_invoking(self):
        """A failed invoke leaves the previous answer in place, byte for byte.

        Verified by sha256 across a rejected call. Without the `rm`, a recipient working
        through an argument they have wrong sees a plausible reply from an earlier
        attempt and believes the failing call succeeded. The snippet is built to be run
        repeatedly, so this is the normal case rather than an edge one.
        """
        section = self._section(_generate().readme)
        assert "rm -f response.json" in section
        assert section.index("rm -f response.json") < section.index("invoke-agent-runtime")

    def test_it_gets_the_qualifier_from_the_output_rather_than_a_typed_name(self):
        # A hand-typed endpoint name is wrong for every stack but the one it was
        # written against, which is why the EndpointName output exists.
        section = self._section(_generate().readme)
        assert "$ENDPOINT_NAME" in section
        assert "OutputKey=='EndpointName'" in section

    def test_it_warns_that_the_answer_goes_to_the_file_not_the_terminal(self):
        # Otherwise a successful call looks like it returned only metadata.
        section = self._section(_generate().readme)
        assert "response.json" in section
        assert "statusCode" in section

    def test_it_does_not_tell_the_recipient_to_raise_the_read_timeout(self):
        """This section nearly shipped `--cli-read-timeout 180` as advice, on the
        plausible-sounding theory that a cold start outruns the 60s default. Measured,
        a first-ever cold start took 7.1s, and 180 took 7.8s -- noise, because the flag
        is a client socket timeout and cannot make the service faster.

        Worse, the flag is not inert in the other direction. At `--cli-read-timeout 2`
        the CLI printed one clean `statusCode 200` and exit 0 while the runtime logged
        THREE completed invocations under that session id: botocore retried, and each
        retry re-ran the agent. So the advice we nearly gave points at a foot-gun, and
        what the README has to say is the opposite of it.
        """
        section = self._section(_generate().readme)
        assert "--cli-read-timeout 180" not in section, "measured as pointless; do not recommend it"
        assert "Do not lower `--cli-read-timeout`" in section
        assert "retry runs your agent again" in section

    def test_it_states_the_session_id_minimum(self):
        # 33 is a validation error client-side, and nothing about the message suggests
        # a length rule to somebody who reached for a short readable id.
        section = self._section(_generate().readme)
        assert "33" in section


class TestGeneratedDocumentation:
    @staticmethod
    def _parameter_rows(readme):
        """The Parameters table, as ``{name: {"default", "set_with", "description"}}``."""
        section = readme.split("## Parameters")[1].split("\n## ")[0]
        rows = [line for line in section.splitlines() if line.startswith("|") and not line.startswith("|-")]
        cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
        return {
            name: {"default": default, "set_with": set_with, "description": description}
            for name, default, set_with, description in cells
            if name != "Parameter"
        }

    @ALL_COMBINATIONS
    def test_every_parameter_the_template_takes_is_documented(self, combo):
        """The table was nine hand-written rows against a template emitting up to twenty.

        The omissions were not obscure: both VPC knobs, ``LogRetentionInDays``,
        ``AccessTokenValidityMinutes`` and every S3 key and digest were absent, so the
        only way to find most of what a controlled account has to set was to read the
        YAML -- in the document whose purpose is to save the recipient that. Two of the
        rows that did exist had to be appended conditionally by hand, which is the
        mechanism by which the unconditional ones went missing.

        Asserted both ways. A parameter with no row is the original bug; a row with no
        parameter tells the recipient to set something this template will reject.
        """
        bundle = _generate(**combo)
        template = yaml.safe_load(bundle.template_yaml)
        documented = self._parameter_rows(bundle.readme)
        assert set(documented) == set(template["Parameters"]), (
            f"undocumented: {sorted(set(template['Parameters']) - set(documented))}; "
            f"documented but not in the template: {sorted(set(documented) - set(template['Parameters']))}"
        )

    @ALL_COMBINATIONS
    def test_every_parameter_can_be_set_through_deploy_sh(self, combo):
        """The README describing a parameter the script cannot pass is advice, not a knob.

        Five were in exactly that state -- LogRetentionInDays, LambdaReservedConcurrency,
        AccessTokenValidityMinutes, CognitoDomainSuffix and PolicyValidationMode. The
        Quick Start tells the recipient to deploy with deploy.sh, so reaching any of
        them meant abandoning the script and writing the aws cloudformation deploy
        invocation by hand -- including PolicyValidationMode, the switch that turns on
        the Cedar findings analysis, whose own README example had to do exactly that.

        Asserted through the two sets the generator partitions parameters into, because
        the failure this catches is a parameter belonging to neither. The README's
        "Set with" column is asserted against the same variable name, since a knob the
        recipient cannot find the name of is no more reachable than one that does not
        exist.
        """
        bundle = _generate(**combo)
        template = yaml.safe_load(bundle.template_yaml)
        parameters = set(template["Parameters"])

        owned = parameters & cfn_template_generator._DEPLOY_SCRIPT_OWNED_PARAMETERS
        passthrough = parameters - owned
        stale = cfn_template_generator._DEPLOY_SCRIPT_OWNED_PARAMETERS - parameters
        documented = self._parameter_rows(bundle.readme)

        for name in owned:
            assert name in bundle.deploy_sh, (
                f"{name} is declared as one deploy.sh sets itself, but the script never names it"
            )
        for name in passthrough:
            variable = cfn_template_generator.parameter_env_var(name)
            assert f'"{name}:{variable}"' in bundle.deploy_sh, (
                f'{name} can only be set by bypassing deploy.sh; expected the pass-through pair "{name}:{variable}"'
            )
            assert documented[name]["set_with"] == f"`{variable}`", (
                f"deploy.sh reads {variable} for {name}, but the README's Set with column "
                f"says {documented[name]['set_with']}"
            )
        # A row saying `deploy.sh` must be telling the truth: the script has to set that
        # parameter without asking, or the recipient has been told not to set the one
        # thing standing between them and a deploy.
        for name, row in documented.items():
            if row["set_with"] == "`deploy.sh`":
                assert name in owned, f"the README tells the recipient deploy.sh sets {name}, and it does not"
        assert not stale - {
            # Only emitted when the canvas has gateway tools or custom tools, which
            # not every combination does; the script sets them when they exist.
            "ToolLambdaCodeKey",
            "CustomToolCodeKey",
            # Only on the LiteLLM path. The URL and the server list are not here
            # because they are no longer script-owned: deploy.sh never set them.
            "LiteLLMApiKeySecretArn",
            # Only when the export has an MCP server.
            "McpServerCodeKey",
            "McpServerCodeDigest",
        }, f"declared as deploy.sh-owned but not in this template: {sorted(stale)}"

    def test_the_virtual_key_arn_is_owned_by_the_script_not_the_passthrough(self):
        """The virtual key's ARN is a positional argument, and must stay one.

        If LiteLLMApiKeySecretArn ever fell out of the owned set it would be handled by
        the generic pass-through instead, which would quietly drop deploy.sh's preflight
        check -- the one that fails before a single artifact is uploaded when the canvas
        carried no ARN and the parameter therefore has no default.

        Only the ARN. This used to assert the same of LiteLLMGatewayUrl and
        LiteLLMMcpServers, for which the reason above does not hold and no other was
        given -- and deploy.sh sets neither, so declaring them owned took them out of
        the pass-through as well and left the README saying "filled in by the script
        itself ... you do not set those" about the proxy URL. Proven live: the recipient
        was told not to set the one value that differs between them and the canvas
        author, the stack deployed green, and the runtime reached nothing.
        """
        bundle = _generate(gateway_config=_litellm())
        assert "LiteLLMApiKeySecretArn" in cfn_template_generator._DEPLOY_SCRIPT_OWNED_PARAMETERS
        assert '"LiteLLMApiKeySecretArn:' not in bundle.deploy_sh, "the ARN reached the generic pass-through"

    @ALL_COMBINATIONS
    def test_every_owned_parameter_is_really_assigned_by_the_script(self, combo):
        """ "Owned" has to mean the script assigns it, not merely that it is on the list.

        The list is what excludes a parameter from the pass-through AND what makes the
        README's table say the recipient must not set it, so a name on it that deploy.sh
        never assigns is unsettable by either documented route. That is exactly how the
        proxy URL became unreachable, and the two tests above it both passed throughout,
        because each only checked the README against the list and the list is where the
        wrong claim lived. So check the script's own text.
        """
        bundle = _generate(**combo)
        parameters = set(yaml.safe_load(bundle.template_yaml)["Parameters"])
        for name in sorted(parameters & cfn_template_generator._DEPLOY_SCRIPT_OWNED_PARAMETERS):
            assert f'"{name}=' in bundle.deploy_sh, (
                f"{name} is declared deploy.sh-owned, so nothing else can set it, "
                f"but deploy.sh never adds it to PARAM_OVERRIDES"
            )

    @ALL_COMBINATIONS
    def test_no_parameter_row_is_empty_or_truncated_mid_markup(self, combo):
        """The descriptions are derived, so a bad Description shows up here, not in review.

        Two failure shapes the derivation can produce and this catches: a parameter
        whose Description is missing entirely leaves an empty cell, and one whose first
        sentence ends inside a code span or a link leaves unbalanced markup that
        swallows the rest of the row when rendered.
        """
        for name, row in self._parameter_rows(_generate(**combo).readme).items():
            description = row["description"]
            assert description, f"{name} has an empty description cell"
            assert description.count("`") % 2 == 0, f"{name} has an unclosed code span: {description}"
            assert description.count("[") == description.count("]"), f"{name} has an unbalanced link: {description}"

    @ALL_COMBINATIONS
    def test_data_protection_section_matches_the_template(self, combo):
        bundle = _generate(**combo)
        template = yaml.safe_load(bundle.template_yaml)
        retained = sorted(k for k, v in template["Resources"].items() if v.get("DeletionPolicy"))
        section = bundle.readme.split("## Data Protection")[1]
        if retained:
            for logical_id in retained:
                assert logical_id in section, f"{logical_id} is retained but the README does not say so"
        else:
            assert "no data-bearing resources" in section

    def test_gateway_readme_explains_how_to_get_a_token(self):
        readme = _generate(gateway_config=AGENTCORE_GATEWAY).readme
        assert "## Authenticating to the Gateway" in readme
        assert "client_credentials" in readme
        assert "describe-user-pool-client" in readme, "must say how to read the secret it refuses to output"

    def test_the_gateway_smoke_test_is_one_that_can_actually_fail(self):
        """The documented smoke test used to be a GET, which proves nothing.

        Run live against a deployed export: `curl "$GATEWAY_URL" -H "Authorization:
        Bearer $ACCESS_TOKEN"` returns `405 Method Not Allowed` from the load balancer,
        and it returns the same 405 for a deliberately invalid token — the request never
        reaches the authorizer. So a recipient following the README saw a failure whether
        their credentials worked or not, which is worse than no smoke test: it makes a
        working deploy look broken and a broken one look the same.

        The MCP POST is the check that discriminates: 200 with the tool list on a good
        token, 401 "Invalid Bearer token" on a bad one. Both verified live.
        """
        readme = _generate(gateway_config=AGENTCORE_GATEWAY).readme
        section = readme.split("## Authenticating to the Gateway")[1].split("\n## ")[0]
        assert '"method":"tools/list"' in section, "the smoke test is not an MCP call"
        assert "-X POST" in section
        # Streamable HTTP: the gateway rejects a POST that does not accept both.
        assert "Accept: application/json, text/event-stream" in section
        assert "405" in section, "the recipient is not warned what a GET returns"
        assert "401" in section, "nothing tells the recipient what a real auth failure looks like"
        # A GET with only an Authorization header is the command that was wrong; it must
        # not reappear as the thing a recipient is told to run.
        assert 'curl -s "$GATEWAY_URL" -H "Authorization' not in section

    def test_the_readme_explains_a_policy_denial_before_it_looks_like_a_bug(self):
        """Authentication succeeding and authorization denying look alike to a caller.

        Verified live on a deployed export: with the generated policy in place the tool
        call returns 200 and the tool's output; with the policy deleted, the identical
        call returns `Tool Execution Denied ... [No policy applies to the request (denied
        by default).]` — inside a JSON-RPC error, with HTTP 200. A recipient who reads
        that as a broken gateway will go looking in the wrong place, so the README names
        the message and says it is the default-deny working.
        """
        readme = _generate(**COMPONENT_COMBINATIONS["gateway+kb+default-policy"]).readme
        assert "denied by default" in readme
        assert "ENFORCE" in readme, "the recipient is not told the engine is enforcing"

    def test_the_readme_says_an_empty_tool_list_is_a_policy_result(self):
        """`tools/list` is policy-filtered, which makes its failure mode look like success.

        Found live, and it is the one response shape a recipient will misread: with the
        generated policy in place the call returns the tool; with the policy deleted the
        SAME call returns `{"tools":[]}` and HTTP 200 — no error, no denial, nothing to
        search for. Someone debugging that will suspect the gateway target or the tool
        lambda, neither of which is involved.
        """
        section = (
            _generate(gateway_config=AGENTCORE_GATEWAY)
            .readme.split("## Authenticating to the Gateway")[1]
            .split("\n## ")[0]
        )
        assert '{"tools":[]}' in section
        assert "filtered by the policy engine" in section, "the list looks complete but is not"

    def test_readme_warns_that_the_secret_does_not_belong_in_state(self):
        readme = _generate(gateway_config=AGENTCORE_GATEWAY).readme
        assert "Terraform state" in readme

    def test_no_auth_section_without_a_gateway(self):
        assert "Authenticating to the Gateway" not in _generate().readme

    @ALL_COMBINATIONS
    def test_generated_shell_scripts_are_valid_bash(self, combo):
        """These are shipped to customers and run with `set -euo pipefail`; a
        syntax error is discovered by the customer, not by us."""
        bundle = _generate(**combo)
        for name, script in (("deploy.sh", bundle.deploy_sh), ("teardown.sh", bundle.teardown_sh)):
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                handle.write(script)
                path = handle.name
            try:
                result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
                assert result.returncode == 0, f"{name} is not valid bash: {result.stderr}"
            finally:
                Path(path).unlink()

    def test_teardown_warns_before_deleting_when_resources_are_retained(self):
        """The operator running teardown is the person who needs to know that a
        user pool and a memory store will outlive the stack."""
        bundle = _generate(gateway_config=AGENTCORE_GATEWAY, memory_config={"enabled": True})
        before_delete = bundle.teardown_sh.split("aws cloudformation delete-stack")[0]
        assert "SURVIVE" in before_delete
        assert "AgentCoreMemory" in before_delete

    def test_teardown_warns_about_permanent_loss_when_retention_is_disabled(self):
        bundle = _generate(
            gateway_config=AGENTCORE_GATEWAY, memory_config={"enabled": True}, dataRetentionPolicy="Delete"
        )
        before_delete = bundle.teardown_sh.split("aws cloudformation delete-stack")[0]
        assert "permanently destroy" in before_delete

    def test_teardown_stays_quiet_when_there_is_no_data_to_lose(self):
        """A warning about destroying memory a stack never created teaches the
        operator to ignore the warnings that matter.

        A runtime-only stack retains exactly one resource, the CFN provider Lambda's
        log group, and the notice used to tell the operator it held "user identities,
        ingested documents and/or conversation history" — none of which is in a Lambda
        log group. So this asserts the absence of the data-store claim specifically,
        not the absence of every notice: the log-group notice is true and load-bearing
        (see the redeploy test below).
        """
        teardown = _generate().teardown_sh
        assert "permanently destroy" not in teardown
        assert "user identities" not in teardown, "no data store here — this warning would be false"
        assert "DeletionPolicy=Retain on its data-bearing" not in teardown

    @ALL_COMBINATIONS
    def test_teardown_removes_the_artifacts_this_stack_staged(self, combo):
        """Deleting the stack left the recipient's own agent source in S3 forever.

        deploy.sh stages agent-code.zip and the Lambda zips under cfn-assets/<stack>/ and
        deliberately keeps superseded ones so a rollback can reach them. Nothing deleted
        them: the code-packaging resource removes only the merged code.zip it wrote, and
        teardown deleted the stack and stopped. staged_key's comment meanwhile promised
        the teardown disposed of the bucket.

        The bucket lifecycle does not save it either — it expires NONCURRENT versions, and
        these are current.
        """
        teardown = _generate(**combo).teardown_sh
        assert "cfn-assets/$STACK_NAME/" in teardown, "teardown does not name the staged prefix"
        assert "purge_staged_objects" in teardown
        # After the delete, not before: while the delete is in flight the stack still
        # references these keys, and a failed delete gets retried.
        after_delete = teardown.split("wait stack-delete-complete")[1]
        assert "purge_staged_objects " in after_delete, "the purge must run after the stack is gone"

    @ALL_COMBINATIONS
    def test_teardown_resolves_the_bucket_before_it_deletes_the_stack(self, combo):
        """The bucket name is a stack Parameter and no Output carries it, so reading it
        after the delete reads nothing and the purge silently does nothing."""
        teardown = _generate(**combo).teardown_sh
        before_delete = teardown.split("aws cloudformation delete-stack")[0]
        assert "ArtifactsBucket" in before_delete, "the bucket must be resolved while the stack exists"

    @ALL_COMBINATIONS
    def test_teardown_purges_versions_rather_than_just_the_current_objects(self, combo):
        """`aws s3 rm --recursive` looks like it empties the prefix and does not.

        Measured live on a versioned bucket: seven versions and markers under the prefix,
        `aws s3 rm --recursive` left NINE (it adds a delete marker per object) while
        `aws s3 ls` reported the prefix empty. The agent source was still fully readable
        through list-object-versions. So the version-aware form is the fix, not a nicety.
        """
        teardown = _generate(**combo).teardown_sh
        assert "list-object-versions" in teardown
        assert "DeleteMarkers" in teardown, "delete markers are left behind without this"
        assert "--version-id" in teardown

    @ALL_COMBINATIONS
    def test_teardown_does_not_claim_to_have_removed_what_it_leaves(self, combo):
        """It leaves the shared dependency bundle and the bucket, and says so.

        Per ARCC cnt_Hr4zJD4KntOWIt a public-facing document must carry a clear statement
        of what a deletion actually does; the previous statement was false. The bundle is
        shared by every stack deployed from the bucket, and the bucket may be one the
        recipient already had — deploy.sh leaves an existing bucket untouched for that
        reason — so neither is deleted, and both are named with the command to remove them.
        """
        bundle = _generate(**combo)
        teardown = bundle.teardown_sh
        assert "$BUNDLE_KEY" in teardown or "BUNDLE_KEY=" in teardown
        assert "aws s3 rb" in teardown, "teardown does not say how to remove the bucket it leaves"
        # The claim that started this: it must not survive anywhere in the bundle.
        for name, script in (("deploy.sh", bundle.deploy_sh), ("teardown.sh", teardown)):
            assert "teardown removes the bucket" not in script, f"{name} still makes the false claim"

    def test_teardown_reports_a_failed_purge_instead_of_a_false_success(self):
        """Every delete is failure-tolerant so one missing permission cannot abort a
        teardown, which means the loop finishing proves nothing about the outcome.

        Measured live with a Deny on the bucket: denying only s3:DeleteObject did NOT
        stop the purge, because removing a named version is s3:DeleteObjectVersion.
        Denying both left the object in place, and the re-listing is what turns that into
        a warning rather than a green "Removed staged artifacts".
        """
        teardown = _generate().teardown_sh
        assert "WARNING" in teardown
        assert "s3:DeleteObjectVersion" in teardown, "the warning must name the action that is actually needed"
        # The success line must be guarded by the function's return value, not printed
        # unconditionally after it.
        assert "elif purge_staged_objects" in teardown or "if purge_staged_objects" in teardown

    def test_readme_says_retained_log_groups_block_a_same_name_redeploy(self):
        """The README used to promise the opposite of what happens.

        Its trade-off paragraph said a same-name redeploy "creates *new* resources
        alongside the retained ones". True of a Cognito pool or an AgentCore Memory;
        false of a log group, whose name is derived from the stack name, so the redeploy
        does not create a second one — it fails. A recipient told the redeploy will
        succeed has no reason to connect the failure to this setting.
        """
        section = _generate().readme.split("## Data Protection")[1].split("\n## ")[0]
        assert "block a\nsame-name redeploy" in section or "block a same-name redeploy" in section
        assert "ResourceExistenceCheck" in section
        assert "aws logs delete-log-group" in section
        assert "no user identities" in section, "a runtime-only stack must not imply it holds user data"

    def test_readme_names_the_log_groups_the_stack_governs_but_does_not_declare(self):
        """The runtime's own groups are the ones holding the conversations.

        The AgentCore service creates them itself, one per endpoint, during this
        stack's own create — verified live arriving with `retentionInDays: None` and
        `kmsKeyId: None`. No declared resource can reach them, so the README used to
        say they escaped the retention and CMK wiring entirely. `Custom::RuntimeLogGroup`
        now adopts them, and the README has to say which of the two it is: governed,
        but deliberately not deleted, so a recipient reading the retention parameter
        is neither over- nor under-promised.
        """
        section = _generate().readme.split("## Data Protection")[1].split("\n## ")[0]
        assert "/aws/bedrock-agentcore/runtimes/" in section
        assert "Custom::RuntimeLogGroup" in section, "the recipient is not told what governs them"
        assert "CustomerManagedKeyArn" in section
        assert "per endpoint" in section, "one group per endpoint, not one per runtime"
        # Governed is not the same as deleted, and conflating the two is how an
        # investigation loses the only record of what the agent was asked.
        assert "survive teardown" in section
        assert "aws logs delete-log-group" in section

    def test_teardown_says_why_a_same_name_redeploy_will_fail(self):
        """Retained log groups break the next deploy, with an error naming no resource.

        `[AWS::EarlyValidation::ResourceExistenceCheck]` reports `"Hooks": []` and does
        not say which resource already exists, so an operator who has not been told in
        advance cannot act on it. Every export retains at least the provider Lambda's
        log group, so this applies to all of them, not just the ones holding data.
        """
        teardown = _generate().teardown_sh
        assert "ResourceExistenceCheck" in teardown
        assert "aws logs delete-log-group" in teardown, "naming the problem without the fix is not enough"
        assert "/aws/bedrock-agentcore/runtimes/" in teardown, (
            "the runtime's own group is outside the stack and outlives this cleanup"
        )

    def test_teardown_says_a_retained_user_pool_takes_two_calls_to_delete(self):
        """``DeletionProtection: ACTIVE`` makes the documented cleanup command fail.

        The notice above tells the operator the pool survives and to "delete it by
        hand if you want it gone", and the obvious hand command — ``delete-user-pool``
        — returns ``InvalidParameterException: deletion protection is activated``.
        Hit live by a teammate clearing up after a *rolled-back* deploy, which is the
        likeliest way to meet it: a rollback retains the pool too, and until the pool
        is gone a same-name redeploy dies at
        ``[AWS::EarlyValidation::ResourceExistenceCheck]`` naming no resource — the
        same symptom as the retained log groups, from a different cause.

        The protection itself is correct (see ``TestDataRetention``); what was missing
        was telling the operator how to clear it.
        """
        teardown = _generate(gateway_config=AGENTCORE_GATEWAY).teardown_sh
        before_delete = teardown.split("aws cloudformation delete-stack")[0]
        assert "deletion protection is activated" in before_delete, (
            "the operator is not told why delete-user-pool will fail"
        )
        # Both calls, in order, and runnable as printed.
        update = (
            "aws cognito-idp update-user-pool --user-pool-id POOL_ID --region $REGION --deletion-protection INACTIVE"
        )
        assert update in before_delete
        assert "aws cognito-idp delete-user-pool --user-pool-id POOL_ID --region $REGION" in before_delete
        assert before_delete.index(update) < before_delete.index("aws cognito-idp delete-user-pool")
        assert "ResourceExistenceCheck" in before_delete
        # update-user-pool is a full replace, not a field edit: pointing it at a pool
        # the operator means to KEEP would silently reset every setting omitted.
        assert "resets" in before_delete and "every setting you do not pass back" in before_delete

    def test_that_notice_is_absent_when_the_stack_has_no_user_pool(self):
        """A Cognito cleanup procedure in a stack with no Cognito teaches the
        operator to skim the notices that do apply. A memory-only export retains a
        data store, so the notice above it is present and this is not vacuous."""
        teardown = _generate(memory_config={"enabled": True}).teardown_sh
        assert "SURVIVE" in teardown, "no retained data store here — the test proves nothing"
        assert "cognito-idp" not in teardown
        assert "deletion protection is activated" not in teardown

    def test_the_log_group_cleanup_command_is_the_one_the_operator_can_run(self):
        """An unquoted heredoc, so the operator sees real values, not $STACK_NAME.

        And therefore no backslash-newline continuations inside it: in an unquoted
        heredoc those are line continuations, and the command would arrive joined into
        one unreadable line with the flags run together.
        """
        teardown = _generate().teardown_sh
        block = teardown.split("aws logs describe-log-groups")[1].split("EOF")[0]
        assert "\\\n" not in block, "a continuation inside an unquoted heredoc mangles the command"
        # Emitted literally for `tr`, not expanded by the heredoc.
        assert "tr '\\t' '\\n'" in teardown


# ---------------------------------------------------------------------------
# The emitted YAML must be valid CloudFormation
# ---------------------------------------------------------------------------


class TestTemplateIsAscii:
    """The emitted template must contain no character outside printable ASCII.

    Not a style rule. Proven live against a Terraform wrapper: the template carried
    four em-dashes, and CloudFormation returns them as ``?`` when Terraform reads
    ``template_body`` back to compare. The result is a diff that can never be
    reconciled — `terraform plan -detailed-exitcode` returns 2 on an unchanged stack
    forever, reporting a change nobody made and burying any real drift underneath it.
    A consumer wrapping this export in a pipeline loses `plan` as a review gate.

    Enforced over the whole template rather than over the one field that broke,
    because the four characters were in four unrelated places: the stack
    ``Description``, a parameter ``Description``, a ``ConstraintDescription``, and a
    string inside the KB tool Lambda's inlined source.
    """

    @ALL_COMBINATIONS
    def test_no_character_outside_printable_ascii(self, combo):
        emitted = _generate(**combo).template_yaml
        offenders = {}
        for number, line in enumerate(emitted.splitlines(), 1):
            bad = {ch for ch in line if not (32 <= ord(ch) < 127) and ch != "\t"}
            if bad:
                offenders[number] = (sorted(bad), line.strip()[:120])
        rendered = "\n".join(
            f"  line {number}: {[hex(ord(c)) for c in chars]} in {text!r}"
            for number, (chars, text) in sorted(offenders.items())
        )
        assert not offenders, f"non-ASCII in the emitted template (see class docstring):\n{rendered}"

    @pytest.mark.parametrize("name", ["Agent — prod", "Agenté", "エージェント", "naïve agent"])
    def test_a_non_ascii_deployment_name_cannot_reach_the_template(self, name):
        """The one field a user controls. An agent named with an accented character
        must not be able to poison the template for everyone downstream of it."""
        bundle = CfnTemplateGenerator().generate(
            DeployRequest(
                config=RuntimeConfig(name=name, model={"modelId": MODEL_ID}),
                nodeId="node-1",
                gateway_config=AGENTCORE_GATEWAY,
            )
        )
        assert bundle.template_yaml.isascii()

    def test_the_name_stays_readable_after_the_ascii_filter(self):
        """The filter drops non-ASCII, it does not reduce the name to alphanumerics.

        The stack ``Description`` is the only human-readable label a console user sees,
        and by the time it gets here the name is already a slug, so the separators are
        all the readability there is. A filter that also stripped punctuation would
        turn `my agent - prod` into `myagentprod`.
        """
        bundle = CfnTemplateGenerator().generate(
            DeployRequest(
                config=RuntimeConfig(name="my agent - prod", model={"modelId": MODEL_ID}),
                nodeId="node-1",
            )
        )
        description = yaml.safe_load(bundle.template_yaml)["Description"]
        assert "my-agent---prod" in description, description
        # And the accented case keeps everything that was already ASCII.
        accented = CfnTemplateGenerator().generate(
            DeployRequest(
                config=RuntimeConfig(name="naïve-agent", model={"modelId": MODEL_ID}),
                nodeId="node-1",
            )
        )
        assert "-agent" in yaml.safe_load(accented.template_yaml)["Description"]


class TestTemplateValidity:
    @ALL_COMBINATIONS
    def test_cfn_lint_passes(self, combo):
        """No lint gate has ever run against the artifact we ship.

        Errors only (``--ignore-checks W``): warnings here are mostly stylistic
        and gating on them would make this test a maintenance tax rather than a
        safety net.
        """
        _require_scanner("cfn-lint", "pip install -e '.[dev]'")
        bundle = _generate(**combo)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(bundle.template_yaml)
            path = handle.name
        try:
            result = subprocess.run(
                ["cfn-lint", path, "--format", "json", "--ignore-checks", "W"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                findings = json.loads(result.stdout or "[]")
                rendered = "\n".join(
                    f"  {f['Rule']['Id']} {f['Level']}: {f['Message']} (line {f['Location']['Start']['LineNumber']})"
                    for f in findings
                )
                pytest.fail(f"cfn-lint found {len(findings)} error(s):\n{rendered}")
        finally:
            Path(path).unlink()

    @ALL_COMBINATIONS
    def test_template_parses_and_has_the_required_top_level_sections(self, combo):
        template = _template(**combo)
        assert template["AWSTemplateFormatVersion"]
        assert template["Resources"]
        assert template["Outputs"]

    @ALL_COMBINATIONS
    def test_every_parameter_is_referenced(self, combo):
        """An unreferenced parameter is either dead or a wiring bug — the
        DependencyBundleKey/McpServerCodeKey family are added conditionally and
        it is easy to add the parameter and forget the reference."""
        template = _template(**combo)
        body = json.dumps({k: v for k, v in template.items() if k != "Parameters"})
        for name in template.get("Parameters", {}):
            assert f'"{name}"' in body or f"${{{name}}}" in body, f"parameter {name} is never used"

    @ALL_COMBINATIONS
    def test_every_ref_and_getatt_resolves(self, combo):
        """A dangling Ref fails at deploy time, in the customer's account."""
        template = _template(**combo)
        known = (
            set(template["Resources"])
            | set(template.get("Parameters", {}))
            | {
                "AWS::Region",
                "AWS::AccountId",
                "AWS::StackName",
                "AWS::StackId",
                "AWS::Partition",
                "AWS::URLSuffix",
                "AWS::NoValue",
            }
        )
        for key, value in _iter_nodes(template["Resources"]):
            if key == "Ref" and isinstance(value, str):
                assert value in known, f"dangling Ref to {value!r}"
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                assert target in known, f"dangling Fn::GetAtt to {target!r}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_cfn_provider_handler():
    """Import the provider Lambda the way Lambda does.

    ``handler.py`` uses ``import cfn_response`` — a flat absolute import, because
    it is packaged as a flat zip and deliberately cannot import ``app.*``. So its
    own directory has to be on the path.
    """
    import sys

    provider_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"
    if str(provider_dir) not in sys.path:
        sys.path.insert(0, str(provider_dir))
    import handler  # noqa: PLC0415

    return handler
