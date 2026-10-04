"""deploy.sh is not the oracle: the TEMPLATE has to refuse a cross-region key secret.

The defect this file pins was found the way the worst ones are: the fix for it was
already written, and was wrong. A cross-region LiteLLM key secret was being refused in
``deploy.sh``, which looks complete until you read the README the generator emits
alongside the template — it tells a Terraform user to upload ``template.yaml`` and wrap
it in ``aws_cloudformation_stack``. On that path no script of ours runs at all, so the
ARN baked into the template as a parameter Default still reaches AWS unchecked.

What that costs the recipient is specific, and was measured against a real secret rather
than reasoned about. The generated runtime builds its Secrets Manager client from its OWN
region (``boto3.client("secretsmanager", region_name=REGION)`` where ``REGION`` comes from
the container's ``AWS_REGION``), not from the region inside the ARN it was handed. So a
stack in region B holding a region-A ARN reaches CREATE_COMPLETE, and then fails on the
first tool call with::

    An error occurred (ResourceNotFoundException) when calling the GetSecretValue
    operation: Secrets Manager can't find the specified secret.

That message never mentions a region, even though the call passed an ARN naming one. A
green deploy followed hours later by an error that hides its own cause is the shape of
failure worth spending a custom-resource property on.

ARCC guidance is what makes this a refusal rather than something to make work:
cnt_IMvJNFIFGGpGIU ("[Service Credentials - Single Region Credentials]") requires that a
credential used by a service in a region be stored, deployed and used solely for that
region, and cnt_LuG2TKuO0errRp says secrets "must not be shared globally or between
regions and partitions". Cross-*account* is treated differently on purpose — see
``TestCrossAccountIsAPolicyChoiceNotACorrectnessOne``.

The tests are deliberately about ORDERING as much as outcome. A check that validates
after the first ``get_object`` still fails the stack, so asserting only "it failed" would
pass a patch that downloads and hashes a 130 MB dependency bundle before refusing.
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator

_BACKEND = Path(__file__).resolve().parents[1]

# us-east-1 in both, so a test that wants a mismatch has to say so explicitly.
HOME = "us-east-1"
AWAY = "eu-west-1"
ACCOUNT = "111122223333"
OTHER_ACCOUNT = "444455556666"

KEY_ARN_HOME = f"arn:aws:secretsmanager:{HOME}:{ACCOUNT}:secret:litellm-key-AbCdEf"
KEY_ARN_AWAY = f"arn:aws:secretsmanager:{AWAY}:{ACCOUNT}:secret:litellm-key-AbCdEf"
KEY_ARN_OTHER_ACCOUNT = f"arn:aws:secretsmanager:{HOME}:{OTHER_ACCOUNT}:secret:litellm-key-AbCdEf"

ACK = "i-accept-cross-account-secret-setup"


def _import_cfn_provider_handler():
    """Import the provider Lambda the way Lambda does.

    ``handler.py`` uses ``import cfn_response`` — a flat absolute import, because it is
    packaged as a flat zip and deliberately cannot import ``app.*``. So its own directory
    has to be on the path.
    """
    provider_dir = _BACKEND / "src" / "app" / "services" / "cfn_provider"
    if str(provider_dir) not in sys.path:
        sys.path.insert(0, str(provider_dir))
    import handler  # noqa: PLC0415

    return handler


provider = _import_cfn_provider_handler()


LITELLM_GATEWAY = {
    "gateway_provider": "litellm",
    "litellm_base_url": "https://litellm.example.internal",
    "litellm_servers": ["github"],
}


def _litellm(**overrides):
    return {**LITELLM_GATEWAY, **overrides}


def _bundle(**kwargs):
    kwargs.setdefault("nodeId", "node-1")
    # RuntimeConfig rejects a model outside the supported window, so this cannot be an
    # arbitrary placeholder.
    config = RuntimeConfig(name="regiontest", model={"modelId": "us.anthropic.claude-sonnet-5"})
    return CfnTemplateGenerator().generate(DeployRequest(config=config, **kwargs))


def _template(**kwargs):
    return yaml.safe_load(_bundle(**kwargs).template_yaml)


@pytest.fixture
def no_aws(monkeypatch):
    """Make ANY boto3 client construction a test failure.

    The point of the ordering tests: the region check must run before the provider has
    reached for AWS at all. Patching ``boto3.client`` rather than recording S3 calls is
    deliberate — it catches a validate-after-download patch even if the download is done
    through a resource, a session, or a second client this module does not have today.
    """
    constructed = []

    def _forbidden(*args, **kwargs):
        constructed.append(args[0] if args else kwargs.get("service_name"))
        raise AssertionError(
            f"the provider built a boto3 client ({constructed[-1]!r}) before validating "
            f"the LiteLLM secret ARN; the region check must come first"
        )

    monkeypatch.setattr(provider.boto3, "client", _forbidden)
    return constructed


def _event(request_type="Create", **props):
    return {
        "RequestType": request_type,
        "ResourceType": "Custom::AgentCodePackage",
        "LogicalResourceId": "AgentCodePackage",
        "StackId": f"arn:aws:cloudformation:{HOME}:{ACCOUNT}:stack/regiontest/abc-123",
        "ResourceProperties": {
            "ArtifactsBucket": "some-bucket",
            "AgentCodeKey": "cfn-assets/regiontest/agent-code.zip",
            "DependencyBundleKey": "agentcore-deps/base.zip",
            "OutputKey": "cfn-assets/regiontest/code.zip",
            "SourceDigest": "abc123",
            "BundleDigest": "none",
            **props,
        },
    }


class TestTheProviderRefusesACrossRegionSecret:
    """The half of the fix that Terraform cannot bypass."""

    @pytest.mark.parametrize("request_type", ["Create", "Update"])
    def test_a_cross_region_arn_is_refused_before_any_aws_call(self, request_type, monkeypatch, no_aws):
        """Both lifecycle events, and before the first client.

        UPDATE matters independently of CREATE: the parameter is overridable on a stack
        update, so a stack that was created correctly can be updated to a cross-region ARN
        and would otherwise re-package 130 MB before discovering it.
        """
        monkeypatch.setenv("AWS_REGION", HOME)
        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_code_package_create_update(_event(request_type, LiteLLMApiKeySecretArn=KEY_ARN_AWAY))
        message = str(excinfo.value)
        assert AWAY in message, message
        assert HOME in message, message
        assert no_aws == [], f"a client was built anyway: {no_aws}"

    def test_the_refusal_reaches_the_operator_instead_of_being_reduced_to_a_class_name(self, monkeypatch):
        """``ProviderError``, not ``ValueError`` — the difference is the whole remedy.

        ``_safe_failure_reason`` deliberately reduces every other exception to its class
        name, because botocore builds ClientError messages out of API responses and some
        calls in that module carry a Cognito client secret in their request parameters. The
        cost is that a bare ``ValueError`` here would surface in the recipient's stack
        events as "cfn-provider failed with ValueError" and the actionable part would live
        only in a Lambda log group. This test is the one that fails if somebody
        "simplifies" the raise.
        """
        monkeypatch.setenv("AWS_REGION", HOME)
        error = provider.ProviderError("x")
        try:
            provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": KEY_ARN_AWAY})
        except provider.ProviderError as e:  # noqa: PERF203 - one call, one assertion
            error = e
        reason = provider._safe_failure_reason(error)
        assert "Create the secret in" in reason, reason
        assert AWAY in reason and HOME in reason, reason
        assert reason != f"cfn-provider failed with {type(error).__name__}"

    def test_a_same_region_arn_is_accepted(self, monkeypatch):
        """The happy path, asserted on its own.

        A guard exercised only by what it rejects is compatible with rejecting
        everything, which has bitten this repo before.
        """
        monkeypatch.setenv("AWS_REGION", HOME)
        assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": KEY_ARN_HOME}) is None

    def test_a_non_litellm_deployment_is_untouched(self, monkeypatch):
        """No property, no new failure path.

        Every export carries ``Custom::AgentCodePackage``, so a check that required this
        property would break every AgentCore deployment rather than only mis-validate a
        LiteLLM one.
        """
        monkeypatch.setenv("AWS_REGION", HOME)
        assert provider._verify_litellm_secret_region({}) is None
        assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": ""}) is None
        assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": "   "}) is None

    def test_an_unknowable_region_does_not_fail_the_stack(self, monkeypatch):
        """``AWS_REGION`` unset means we have nothing to compare against.

        Lambda always sets it, so this is defence against a future harness rather than a
        real deployment — and the right behaviour is to let the stack proceed. Failing a
        recipient's deploy because our own check could not determine its region would turn
        a safety net into an outage.
        """
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": KEY_ARN_AWAY}) is None

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("sk-a-virtual-key-pasted-here", id="a-key-not-an-arn"),
            pytest.param("arn:aws:secretsmanager", id="truncated"),
            pytest.param("arn:aws:secretsmanager::111122223333:secret:x", id="empty-region-segment"),
        ],
    )
    def test_a_malformed_arn_fails_closed_without_repeating_the_value(self, malformed, monkeypatch, no_aws):
        """Fails closed, and does NOT echo what it was given.

        The deliberate departure from every other message in the provider, which names the
        resource id it choked on. The likeliest way to reach this branch is a virtual key
        pasted where its ARN belonged, and this string is bound for stack events — readable
        for 90 days by anyone who can DescribeStackEvents, and copied into Terraform state
        under an ``aws_cloudformation_stack`` wrapper. A malformed value is exactly the
        value that must not be repeated back.
        """
        monkeypatch.setenv("AWS_REGION", HOME)
        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_code_package_create_update(_event(LiteLLMApiKeySecretArn=malformed))
        message = str(excinfo.value)
        assert "not a Secrets Manager ARN" in message, message
        assert malformed not in message, f"the provider echoed the value back: {message}"
        assert no_aws == [], f"a client was built anyway: {no_aws}"

    def test_validation_is_the_first_statement_of_the_handler(self):
        """Read the AST, not the behaviour.

        The behavioural tests above use a fixture that forbids client construction, which
        proves ordering against today's implementation. This one pins the intent directly
        so that a refactor which introduces a new pre-flight step — a tag read, a metric,
        an S3 head through a helper — cannot quietly move the validation behind it.
        """
        tree = ast.parse(inspect.getsource(provider._handle_code_package_create_update))
        body = tree.body[0].body
        # Skip the docstring and the plain property reads, which touch nothing.
        calls = [
            node
            for node in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        names = [c.func.id for c in calls]
        assert "_verify_litellm_secret_region" in names, names
        assert names.index("_verify_litellm_secret_region") == 0, (
            f"something is called before the LiteLLM region check: {names}"
        )


class TestTheTemplateCarriesTheCheckIntoTheTerraformPath:
    """What makes deploy.sh not the oracle."""

    def test_a_litellm_export_hands_the_arn_to_the_custom_resource(self):
        resources = _template(gateway_config=_litellm())["Resources"]
        props = resources["AgentCodePackage"]["Properties"]
        assert props["LiteLLMApiKeySecretArn"] == {"Ref": "LiteLLMApiKeySecretArn"}, props

    def test_the_arn_travels_by_reference_so_the_key_is_never_a_property(self):
        """A ``Ref`` to the ARN parameter, never a literal and never the key.

        CloudFormation copies resolved ResourceProperties into stack events, which are
        readable for 90 days and which ``NoEcho`` does not reach — measured on a live
        stack, where 78 of 110 events carried them. An ARN is safe there. A key is not, so
        the property must be the ARN parameter and nothing else.
        """
        bundle = _bundle(gateway_config=_litellm(litellm_api_key="sk-super-secret-value"))
        template = yaml.safe_load(bundle.template_yaml)
        props = template["Resources"]["AgentCodePackage"]["Properties"]
        assert props["LiteLLMApiKeySecretArn"] == {"Ref": "LiteLLMApiKeySecretArn"}
        assert "sk-super-secret-value" not in bundle.template_yaml

    def test_a_non_litellm_export_adds_no_such_property(self):
        """The AgentCore path must be byte-identical in this respect.

        Otherwise the fix for one provider becomes a required property for every
        deployment, and the provider's ``props[...]`` reads would start raising KeyError on
        stacks that have nothing to do with LiteLLM.
        """
        props = _template()["Resources"]["AgentCodePackage"]["Properties"]
        assert "LiteLLMApiKeySecretArn" not in props, props

    def test_a_terraform_consumer_cannot_silently_accept_the_baked_default(self):
        """The end-to-end statement of the defect, in one test.

        Everything a direct CloudFormation or Terraform consumer sees is the template.
        So: the template must declare the parameter, must pass it to the one custom
        resource that exists in every export, and the code behind that resource — which is
        shipped in the same bundle — must contain the comparison. If all three hold, the
        cross-region ARN cannot reach the runtime no matter which consumer applies the
        template, because no part of the chain is ``deploy.sh``.
        """
        bundle = _bundle(gateway_config=_litellm(litellm_api_key_ref=KEY_ARN_AWAY))
        template = yaml.safe_load(bundle.template_yaml)

        assert "LiteLLMApiKeySecretArn" in template["Parameters"]
        code_package = template["Resources"]["AgentCodePackage"]
        assert code_package["Properties"]["LiteLLMApiKeySecretArn"] == {"Ref": "LiteLLMApiKeySecretArn"}

        # The resource is served by the provider Lambda the bundle ships, and that Lambda
        # is what enforces the comparison.
        source = (_BACKEND / "src" / "app" / "services" / "cfn_provider" / "handler.py").read_text()
        assert "_verify_litellm_secret_region" in source
        assert "_handle_code_package_create_update" in source

        # And the parameter's Default is the exported environment's ARN — i.e. exactly the
        # value the provider now has to reject. This is the defect's mechanism, asserted
        # rather than described: without the check, applying this template in another
        # region succeeds.
        assert template["Parameters"]["LiteLLMApiKeySecretArn"]["Default"] == KEY_ARN_AWAY


class TestCrossAccountIsAPolicyChoiceNotACorrectnessOne:
    """Cross-region cannot work. Cross-account can, and the asymmetry is deliberate.

    A cross-account Secrets Manager read succeeds once the owning account has granted a
    resource policy on the secret and a key policy on the customer-managed KMS key
    encrypting it — the two mechanisms ARCC cnt_LuG2TKuO0errRp names for scoping access to
    a secret and to a key. So an operator who has done that setup is not making a mistake,
    and refusing them from inside the template — the one path they cannot bypass — would
    break a working configuration. The refusal therefore lives in ``deploy.sh``, where it
    defaults closed and can be overridden explicitly.
    """

    def test_the_provider_does_not_refuse_a_cross_account_same_region_arn(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", HOME)
        assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": KEY_ARN_OTHER_ACCOUNT}) is None

    def test_deploy_sh_refuses_cross_account_by_default(self):
        script = _bundle(gateway_config=_litellm()).deploy_sh
        assert "LITELLM_ARN_ACCOUNT" in script
        assert "$CALLER_ACCOUNT" in script
        assert ACK in script

    def test_the_acknowledgement_is_an_argument_and_not_an_environment_variable(self):
        """An ``export`` in a CI job or a shell profile must not be able to set it.

        A variable weakens every subsequent deploy from that shell, silently and
        invisibly. An argument has to be typed at the one deploy it applies to, and it
        stays in shell history and CI logs where it can be audited.
        """
        script = _bundle(gateway_config=_litellm()).deploy_sh
        assert 'LITELLM_CROSS_ACCOUNT_ACK="${5:-}"' in script
        assert "LITELLM_CROSS_ACCOUNT_ACK:-" not in script.replace('LITELLM_CROSS_ACCOUNT_ACK="${5:-}"', "")
        assert "LITELLM_ALLOW_CROSS_ACCOUNT" not in script

    def test_the_acknowledgement_is_discoverable_from_usage(self):
        """Findable before the failure, not only after it.

        An input that appears only in an error message reads like a workaround somebody
        discovered rather than a supported argument.
        """
        script = _bundle(gateway_config=_litellm()).deploy_sh
        usage = [line for line in script.splitlines() if "Usage: ./deploy.sh" in line]
        assert usage, "the deploy script has no usage line"
        assert any("cross-account-ack" in line for line in usage), usage
        assert ACK in script

    def test_the_kms_ordering_advice_is_present_exactly_once(self):
        """The diagnostic that saves the most time, and only one copy of it.

        A cross-account read fails on ``kms:Decrypt`` BEFORE it fails on
        ``secretsmanager:GetSecretValue``, so a correct secret policy with a missing key
        policy denies on what looks like the wrong service entirely. Asserted as a count
        rather than as presence so that a duplicated ``echo`` — which a reviewer reported
        against an earlier revision of this guard — cannot come back unnoticed.
        """
        script = _bundle(gateway_config=_litellm()).deploy_sh
        assert script.count("BEFORE it fails on secretsmanager:GetSecretValue") == 1
        assert script.count("aws/secretsmanager") >= 1

    def test_the_generated_readme_states_the_region_rule(self):
        """The customer-facing artifact, not just the code.

        The README is what the recipient reads before they deploy, and the baked default
        is a trap that only documentation can warn them about in advance.
        """
        readme = _bundle(gateway_config=_litellm()).readme
        assert "must live in the region you deploy to" in readme
        assert "kms:Decrypt" in readme
        assert ACK in readme

    def test_the_generated_script_is_valid_bash(self):
        """``bash -n`` over the real emitted script.

        The guards are assembled from interpolated Python strings, so an unbalanced quote
        or a stray brace is a live possibility and would break the script for every
        recipient rather than only on the cross-account path.
        """
        script = _bundle(gateway_config=_litellm(litellm_api_key_ref=KEY_ARN_HOME)).deploy_sh
        result = subprocess.run(  # noqa: S603
            ["bash", "-n", "/dev/stdin"],  # noqa: S607
            input=script,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
