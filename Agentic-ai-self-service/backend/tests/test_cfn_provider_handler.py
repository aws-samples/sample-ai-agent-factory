"""Lifecycle contract for the custom-resource Lambda the export ships.

``cfn_provider/handler.py`` is the only executable code the recipient runs that we
wrote, and it runs during CREATE, UPDATE and DELETE of their stack. Until now it had
no tests of its own beyond the digest helpers, and the defects that were found in it
were all of one kind: a path that reports success while leaving the account in a state
the next operation cannot recover from. Each test below pins one of those.

Everything is exercised against fake clients. The real calls are asserted live
elsewhere; what needs pinning here is control flow that only happens on failure —
a rollback after a failed create, a delete that AWS refuses — which is precisely what
a live test cannot be relied on to reproduce on demand.
"""

import sys
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters


def _import_cfn_provider_handler():
    """Import the provider Lambda the way Lambda does.

    ``handler.py`` uses ``import cfn_response`` — a flat absolute import, because it is
    packaged as a flat zip and deliberately cannot import ``app.*``. So its own
    directory has to be on the path.
    """
    provider_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"
    if str(provider_dir) not in sys.path:
        sys.path.insert(0, str(provider_dir))
    import handler  # noqa: PLC0415

    return handler


provider = _import_cfn_provider_handler()


ACCOUNT = "111122223333"
STACK_ID = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/agent-demo/abc-123"

# The fake policy engine and policy ids below look like `engine_demo-a1b2c3d4e5`
# rather than `pe-1` because the service model constrains them: at least 12
# characters, matching `[A-Za-z][A-Za-z0-9_]*-[a-z0-9_]{10}`. Shorter placeholders
# read more easily but cannot be validated against that model at all, which is what
# _accepted_by_the_real_model below needs in order to catch a malformed request.


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The handler sleeps for IAM propagation; the tests must not."""
    monkeypatch.setattr(provider.time, "sleep", lambda _s: None)


def _client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} from {op}"}}, op)


class _Exceptions:
    """The ``client.exceptions`` namespace the handler branches on."""

    class ValidationException(Exception):
        pass

    class ResourceNotFoundException(Exception):
        pass


class FakeAgentCore:
    """A bedrock-agentcore-control stand-in that records what it was asked to do."""

    def __init__(self, **behaviour):
        self.exceptions = _Exceptions
        self.calls: list[str] = []
        self._behaviour = behaviour

    def _record(self, name):
        self.calls.append(name)

    def create_oauth2_credential_provider(self, **kwargs):
        self._record("create")
        self.create_kwargs = kwargs
        exc = self._behaviour.get("create_raises")
        if exc:
            raise exc
        return {"credentialProviderArn": self._behaviour.get("arn", "arn:aws:bedrock-agentcore:::provider/p")}

    def update_oauth2_credential_provider(self, **kwargs):
        self._record("update")
        self.update_kwargs = kwargs
        exc = self._behaviour.get("update_raises")
        if exc:
            raise exc
        return {"credentialProviderArn": self._behaviour.get("arn", "arn:aws:bedrock-agentcore:::provider/p")}

    def get_oauth2_credential_provider(self, **kwargs):
        self._record("get")
        return {
            "credentialProviderArn": self._behaviour.get("arn", "arn:aws:bedrock-agentcore:::provider/p"),
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "clientId": self._behaviour.get("existing_client_id", "client-a"),
                    "oauthDiscovery": {"discoveryUrl": "https://pool-a/.well-known/openid-configuration"},
                }
            },
        }

    def delete_oauth2_credential_provider(self, **kwargs):
        self._record("delete")
        exc = self._behaviour.get("delete_raises")
        if exc:
            raise exc

    def list_policies(self, **kwargs):
        self._record("list_policies")
        exc = self._behaviour.get("list_raises")
        if exc:
            raise exc
        return {"policies": self._behaviour.get("policies", [])}

    def delete_policy(self, **kwargs):
        self._record("delete_policy")
        self.delete_policy_kwargs = kwargs
        exc = self._behaviour.get("delete_policy_raises")
        if exc:
            raise exc


def _oauth_event(request_type: str, **overrides) -> dict:
    event = {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": "McpOAuth2CredentialProvider",
        "ResourceType": "Custom::OAuth2CredentialProvider",
        "ResourceProperties": {
            "ProviderName": "agent-demo-mcp",
            "DiscoveryUrl": "https://pool-a/.well-known/openid-configuration",
            "ClientId": "client-a",
            # Obviously fake. A real client secret must never appear in a test.
            "ClientSecret": "not-a-real-secret",
        },
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# A failed policy create must not leave a stack its recipient cannot delete
# ---------------------------------------------------------------------------


class TestPolicyDeleteAfterAFailedCreate:
    """The ROLLBACK_FAILED wedge, confirmed live before it was fixed.

    A FAILED Create has no physical id to report, so the handler reports the logical
    resource name and CloudFormation sends Delete with that. The delete path required
    ``/policies/`` in the physical id, found none, and skipped ``delete_policy``
    entirely — so a policy the create had already made was left on the engine, and
    ``DeletePolicyEngine`` then failed with a 409. The stack settled in
    ROLLBACK_FAILED and could not be deleted without hunting down an AgentCore policy
    by hand.
    """

    EVENT = {
        "RequestType": "Delete",
        "StackId": STACK_ID,
        # What CloudFormation actually sends after a failed Create: the logical id.
        "PhysicalResourceId": "DefaultPolicy",
        "LogicalResourceId": "DefaultPolicy",
        "ResourceProperties": {"PolicyEngineId": "engine_demo-a1b2c3d4e5", "Name": "default_permit"},
    }

    def test_the_orphaned_policy_is_found_by_name_and_deleted(self, monkeypatch):
        ctrl = FakeAgentCore(policies=[{"name": "default_permit", "policyId": "policy_demo-9a8b7c6d5e"}])
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        provider._handle_policy_delete(dict(self.EVENT))

        assert "delete_policy" in ctrl.calls, "the policy the failed create left behind was not deleted"
        assert ctrl.delete_policy_kwargs == {
            "policyEngineId": "engine_demo-a1b2c3d4e5",
            "policyId": "policy_demo-9a8b7c6d5e",
        }

    def test_a_parseable_physical_id_still_skips_the_lookup(self, monkeypatch):
        """The normal path must not gain a list_policies call it does not need."""
        ctrl = FakeAgentCore()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        provider._handle_policy_delete(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "engine_demo-a1b2c3d4e5/policies/policy_demo-9a8b7c6d5e",
                "ResourceProperties": {"PolicyEngineId": "engine_demo-a1b2c3d4e5", "Name": "default_permit"},
            }
        )
        assert ctrl.calls == ["delete_policy"]

    def test_nothing_to_delete_is_not_an_error(self, monkeypatch):
        """A create that failed before creating anything must still delete cleanly."""
        ctrl = FakeAgentCore(policies=[])
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        data, physical_id = provider._handle_policy_delete(dict(self.EVENT))
        assert data == {}
        assert physical_id == "DefaultPolicy"
        assert "delete_policy" not in ctrl.calls

    def test_a_delete_that_did_not_work_fails_the_resource(self, monkeypatch):
        """Not swallowed, because a surviving policy blocks DeletePolicyEngine.

        The stack fails either way. The only question is whether it fails here, naming
        the policy and the remedy, or later on an engine that cannot explain itself.
        """
        ctrl = FakeAgentCore(
            policies=[{"name": "default_permit", "policyId": "policy_demo-9a8b7c6d5e"}],
            delete_policy_raises=_client_error("AccessDeniedException", "DeletePolicy"),
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        with pytest.raises(provider.ProviderError) as exc:
            provider._handle_policy_delete(dict(self.EVENT))
        message = str(exc.value)
        assert "policy_demo-9a8b7c6d5e" in message and "engine_demo-a1b2c3d4e5" in message
        assert "delete-policy" in message, "the operator needs the command that clears it"

    def test_an_already_deleted_policy_is_benign(self, monkeypatch):
        """The state Delete is trying to reach. Failing on it would wedge teardown."""
        ctrl = FakeAgentCore(
            policies=[{"name": "default_permit", "policyId": "policy_demo-9a8b7c6d5e"}],
            delete_policy_raises=_client_error("ResourceNotFoundException", "DeletePolicy"),
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        provider._handle_policy_delete(dict(self.EVENT))  # must not raise

    def test_a_broken_lookup_does_not_break_the_delete(self, monkeypatch):
        """list_policies failing is a reason to give up on the lookup, not on Delete."""
        ctrl = FakeAgentCore(list_raises=_client_error("ThrottlingException", "ListPolicies"))
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        provider._handle_policy_delete(dict(self.EVENT))  # must not raise


# ---------------------------------------------------------------------------
# A retry of a broken policy deploy has to be able to recover it
# ---------------------------------------------------------------------------


class FakePolicyControl:
    """The policy half of bedrock-agentcore-control, with the live behaviour pinned.

    ``create_raises`` defaults to the ConflictException AgentCore really answers for
    a duplicate name, so any test whose handler reaches ``create_policy`` when it
    should have updated fails loudly rather than quietly taking a second path.
    """

    def __init__(self, **behaviour):
        self.exceptions = _Exceptions
        self.calls: list[str] = []
        self.create_kwargs: list[dict] = []
        self.update_kwargs: list[dict] = []
        self._behaviour = behaviour
        self._list_calls = 0

    def get_policy_engine(self, **kwargs):
        self.calls.append("get_policy_engine")
        return {"status": self._behaviour.get("engine_status", "ACTIVE"), "statusReasons": []}

    def list_policies(self, **kwargs):
        self.calls.append("list_policies")
        pages = self._behaviour.get("list_pages")
        if pages is not None:
            page = pages[min(self._list_calls, len(pages) - 1)]
            self._list_calls += 1
            return {"policies": page}
        return {"policies": self._behaviour.get("policies", [])}

    def create_policy(self, **kwargs):
        self.calls.append("create_policy")
        self.create_kwargs.append(kwargs)
        exc = self._behaviour.get("create_raises", _client_error("ConflictException", "CreatePolicy"))
        if exc is not None and self._behaviour.get("create_raises_when", lambda _k: True)(kwargs):
            raise exc
        return {"policyId": "policy_new-1a2b3c4d5e", "status": self._behaviour.get("create_status", "ACTIVE")}

    def update_policy(self, **kwargs):
        self.calls.append("update_policy")
        self.update_kwargs.append(kwargs)
        exc = self._behaviour.get("update_raises")
        if exc:
            raise exc
        return {"policyId": kwargs.get("policyId"), "status": self._behaviour.get("update_status", "ACTIVE")}

    def get_policy(self, **kwargs):
        self.calls.append("get_policy")
        return {
            "status": self._behaviour.get("poll_status", "ACTIVE"),
            "statusReasons": self._behaviour.get("poll_reasons", []),
        }


def _policy_event(**props) -> dict:
    return {
        "RequestType": "Create",
        "StackId": STACK_ID,
        "LogicalResourceId": "DefaultPolicy",
        "ResourceType": "Custom::AgentCorePolicy",
        "ResourceProperties": {
            "PolicyEngineId": "engine_demo-a1b2c3d4e5",
            "Name": "default_permit",
            "Statement": 'permit(principal, action, resource == AgentCore::Gateway::"arn:gw");',
            **props,
        },
    }


class TestPolicyCreateOverALeftover:
    """Measured against the live service, not reasoned about.

    ``create_policy`` over an existing name answers ConflictException ("Policy with
    the same name already exists") *even when the existing policy is CREATE_FAILED*.
    So the previous behaviour — treat a failed leftover as absent, create a new one —
    could never recover a broken deploy: every retry hit the conflict. ``update_policy``
    on that same CREATE_FAILED policy is accepted and takes it to ACTIVE, and keeps
    the policy id, so the custom resource's physical id survives the Update too.
    """

    def test_a_failed_leftover_is_updated_in_place_rather_than_recreated(self, monkeypatch):
        ctrl = FakePolicyControl(
            policies=[{"name": "default_permit", "policyId": "policy_old-2b3c4d5e6f", "status": "CREATE_FAILED"}]
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        data, physical_id = provider._handle_policy_create_update(_policy_event(), _Context())

        assert "create_policy" not in ctrl.calls, "a create over a leftover name is a guaranteed ConflictException"
        assert ctrl.update_kwargs[0]["policyId"] == "policy_old-2b3c4d5e6f"
        assert "arn:gw" in ctrl.update_kwargs[0]["definition"]["cedar"]["statement"], (
            "the current statement is the point of the update; reusing the old one is a silent no-op"
        )
        assert data == {"PolicyId": "policy_old-2b3c4d5e6f", "PolicyEngineId": "engine_demo-a1b2c3d4e5"}
        assert physical_id == "engine_demo-a1b2c3d4e5/policies/policy_old-2b3c4d5e6f", (
            "the physical id must not change under an Update"
        )

    def test_a_healthy_leftover_is_still_updated(self, monkeypatch):
        """Unchanged behaviour, pinned so the fix above cannot narrow it."""
        ctrl = FakePolicyControl(
            policies=[{"name": "default_permit", "policyId": "policy_ok-3c4d5e6f7a", "status": "ACTIVE"}]
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        provider._handle_policy_create_update(_policy_event(), _Context())
        assert ctrl.update_kwargs[0]["policyId"] == "policy_ok-3c4d5e6f7a"

    def test_nothing_there_is_created(self, monkeypatch):
        ctrl = FakePolicyControl(policies=[], create_raises=None)
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        data, physical_id = provider._handle_policy_create_update(_policy_event(), _Context())
        assert "update_policy" not in ctrl.calls
        assert data["PolicyId"] == "policy_new-1a2b3c4d5e"
        assert physical_id == "engine_demo-a1b2c3d4e5/policies/policy_new-1a2b3c4d5e"

    def test_a_policy_mid_delete_is_waited_out_and_then_created(self, monkeypatch):
        """A name held by a DELETING policy is usable by neither call.

        ``update_policy`` has nothing usable to update and ``create_policy`` conflicts
        on the name, so the only correct move is to wait for the name to free up.
        """
        ctrl = FakePolicyControl(
            list_pages=[
                [{"name": "default_permit", "policyId": "policy_going-4d5e6f7a8b", "status": "DELETING"}],
                [{"name": "default_permit", "policyId": "policy_going-4d5e6f7a8b", "status": "DELETING"}],
                [],
            ],
            create_raises=None,
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        data, _ = provider._handle_policy_create_update(_policy_event(), _Context())
        assert "update_policy" not in ctrl.calls, "a DELETING policy cannot be updated"
        assert data["PolicyId"] == "policy_new-1a2b3c4d5e"

    def test_a_policy_that_never_finishes_deleting_fails_the_resource(self, monkeypatch):
        ctrl = FakePolicyControl(
            policies=[{"name": "default_permit", "policyId": "policy_stuck-5e6f7a8b9c", "status": "DELETING"}]
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)

        class _NoTime(_Context):
            def get_remaining_time_in_millis(self):
                return 0

        with pytest.raises(provider.ProviderError, match="still being deleted"):
            provider._handle_policy_create_update(_policy_event(), _NoTime())
        assert "create_policy" not in ctrl.calls, "giving up must not turn into a conflicting create"


_MODE_PARAMETERS = {"validationMode", "enforcementMode"}


def _accepted_by_the_real_model(operation: str, kwargs: dict) -> None:
    """Run botocore's own validator over what the handler sent.

    Asserting a literal ``{"optionalValue": ...}`` would only pin what I believed the
    shape was; both description defects came from believing the wrong thing. This pins
    what the installed service model actually accepts, so a shape that AWS rejects
    fails here instead of at 3am in a customer's stack.

    The session is built directly rather than through ``boto3.client`` because the
    tests above monkeypatch ``client`` on the shared boto3 module. No credentials and
    no network are involved — parameter validation is entirely local.

    The two mode parameters are removed first: ``_write_policy``'s ladder exists
    precisely for botocore versions that do not know them, so their absence from an
    older model is expected behaviour rather than a defect worth failing on.
    """
    try:
        shape = (
            botocore.session.get_session()
            .get_service_model("bedrock-agentcore-control")
            .operation_model(operation)
            .input_shape
        )
    except Exception as e:  # pragma: no cover - an older botocore has no such operation
        pytest.skip(f"this botocore cannot describe {operation}: {e}")
    validate_parameters({k: v for k, v in kwargs.items() if k not in _MODE_PARAMETERS}, shape)


class TestPolicyDescription:
    """The two calls do not take the same description, and neither takes "".

    Read off the service model: ``CreatePolicy.description`` is a bare string with a
    minimum length of 1, so the empty string a canvas with no description produces is
    rejected outright. ``UpdatePolicy.description`` is the PATCH structure
    ``{"optionalValue": str}`` — absent leaves the description alone, present with no
    value clears it — and a bare string there is a ParamValidationError.

    Both shapes were wrong in this handler, and the update one mattered twice over:
    ParamValidationError is also how ``_write_policy`` recognises an old botocore, so
    the real error would have been reported as a missing parameter.
    """

    def _ctrl(self, monkeypatch, **behaviour):
        behaviour.setdefault("policies", [])
        behaviour.setdefault("create_raises", None)
        ctrl = FakePolicyControl(**behaviour)
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)
        return ctrl

    def test_the_create_sends_a_bare_string(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch)
        provider._handle_policy_create_update(_policy_event(Description="allows the demo gateway"), _Context())
        assert ctrl.create_kwargs[0]["description"] == "allows the demo gateway"
        _accepted_by_the_real_model("CreatePolicy", ctrl.create_kwargs[0])

    def test_the_update_wraps_it(self, monkeypatch):
        ctrl = self._ctrl(
            monkeypatch,
            policies=[{"name": "default_permit", "policyId": "policy_old-2b3c4d5e6f", "status": "ACTIVE"}],
        )
        provider._handle_policy_create_update(_policy_event(Description="allows the demo gateway"), _Context())
        assert ctrl.update_kwargs[0]["description"] == {"optionalValue": "allows the demo gateway"}
        _accepted_by_the_real_model("UpdatePolicy", ctrl.update_kwargs[0])

    def test_an_absent_description_is_omitted_by_the_create(self, monkeypatch):
        """Not sent as "". The generator emits the property whether or not the canvas
        filled it in, so this is the ordinary case, not an edge one."""
        ctrl = self._ctrl(monkeypatch)
        provider._handle_policy_create_update(_policy_event(Description=""), _Context())
        assert "description" not in ctrl.create_kwargs[0]
        _accepted_by_the_real_model("CreatePolicy", ctrl.create_kwargs[0])

    def test_an_absent_description_is_omitted_by_the_update(self, monkeypatch):
        """Omitted means "leave it alone", which is right: an Update that carried
        ``{"optionalValue": ""}`` would wipe a description the operator had set."""
        ctrl = self._ctrl(
            monkeypatch,
            policies=[{"name": "default_permit", "policyId": "policy_old-2b3c4d5e6f", "status": "ACTIVE"}],
        )
        provider._handle_policy_create_update(_policy_event(), _Context())
        assert "description" not in ctrl.update_kwargs[0]
        _accepted_by_the_real_model("UpdatePolicy", ctrl.update_kwargs[0])


class TestPolicyWriteParameters:
    """What is sent with the write, and what survives an older botocore."""

    def _ctrl(self, monkeypatch, **behaviour):
        behaviour.setdefault("policies", [])
        behaviour.setdefault("create_raises", None)
        ctrl = FakePolicyControl(**behaviour)
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)
        return ctrl

    def test_the_create_states_both_the_validation_and_the_enforcement_mode(self, monkeypatch):
        """LOG_ONLY records a denial instead of enforcing it.

        It is not today's service default, but a policy engine whose policies are all
        LOG_ONLY permits everything while looking exactly like one that does not — so
        the value is stated rather than inherited.
        """
        ctrl = self._ctrl(monkeypatch)
        provider._handle_policy_create_update(_policy_event(), _Context())
        assert ctrl.create_kwargs[0]["validationMode"] == "IGNORE_ALL_FINDINGS"
        assert ctrl.create_kwargs[0]["enforcementMode"] == "ACTIVE"

    def test_the_update_states_them_too(self, monkeypatch):
        ctrl = FakePolicyControl(
            policies=[{"name": "default_permit", "policyId": "policy_old-2b3c4d5e6f", "status": "ACTIVE"}]
        )
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: ctrl)
        provider._handle_policy_create_update(_policy_event(), _Context())
        assert ctrl.update_kwargs[0]["validationMode"] == "IGNORE_ALL_FINDINGS"
        assert ctrl.update_kwargs[0]["enforcementMode"] == "ACTIVE"

    def test_the_template_can_ask_for_the_strict_mode(self, monkeypatch):
        """``PolicyValidationMode`` is a real knob, not a documented constant.

        The lenient default is the handler's choice, made for the propagation race in
        ``_write_policy``'s docstring. An account that wants AgentCore's findings
        analysis to gate its deploys has to be able to say so, so the property has to
        reach the API rather than being read and discarded.
        """
        ctrl = self._ctrl(monkeypatch)
        provider._handle_policy_create_update(_policy_event(ValidationMode="FAIL_ON_ANY_FINDINGS"), _Context())
        assert ctrl.create_kwargs[0]["validationMode"] == "FAIL_ON_ANY_FINDINGS"
        assert ctrl.create_kwargs[0]["enforcementMode"] == "ACTIVE", "the mode chosen must not disturb enforcement"

    def test_the_update_honours_it_too(self, monkeypatch):
        ctrl = self._ctrl(
            monkeypatch,
            policies=[{"name": "default_permit", "policyId": "policy_old-2b3c4d5e6f", "status": "ACTIVE"}],
        )
        provider._handle_policy_create_update(_policy_event(ValidationMode="FAIL_ON_ANY_FINDINGS"), _Context())
        assert ctrl.update_kwargs[0]["validationMode"] == "FAIL_ON_ANY_FINDINGS"

    def test_the_strict_mode_is_what_the_ladder_keeps_longest(self, monkeypatch):
        """The chosen mode must survive the drop of ``enforcementMode``.

        The ladder drops the newest parameter first, and a bug that reset the mode to
        the default on the second attempt would silently give a strict-mode stack the
        lenient one on exactly the botocore builds where nobody is looking.
        """
        ctrl = self._ctrl(
            monkeypatch,
            create_raises=provider.ParamValidationError(report='Unknown parameter in input: "enforcementMode"'),
            create_raises_when=lambda kwargs: "enforcementMode" in kwargs,
        )
        provider._handle_policy_create_update(_policy_event(ValidationMode="FAIL_ON_ANY_FINDINGS"), _Context())
        assert [kw["validationMode"] for kw in ctrl.create_kwargs] == [
            "FAIL_ON_ANY_FINDINGS",
            "FAIL_ON_ANY_FINDINGS",
        ]

    @pytest.mark.parametrize("event", [_policy_event(), _policy_event(ValidationMode="")])
    def test_a_template_that_says_nothing_gets_the_lenient_default(self, monkeypatch, event):
        """Templates emitted before the parameter existed carry no such property.

        They deployed under IGNORE_ALL_FINDINGS, so an Update of one of those stacks
        must not quietly change the mode its policies were written with.
        """
        ctrl = self._ctrl(monkeypatch)
        provider._handle_policy_create_update(event, _Context())
        assert ctrl.create_kwargs[0]["validationMode"] == "IGNORE_ALL_FINDINGS"

    def test_a_mode_the_service_does_not_have_fails_rather_than_being_guessed(self, monkeypatch):
        """Fail loudly, on the reasoning that the two failure modes are not symmetric.

        Falling back to the default here would hand a hand-edited template asking for
        strict validation a policy validated less than it asked for, and nothing in the
        stack events would say so. Refusing costs one readable failure.
        """
        ctrl = self._ctrl(monkeypatch)
        with pytest.raises(provider.ProviderError, match="ValidationMode"):
            provider._handle_policy_create_update(_policy_event(ValidationMode="FAIL_ON_ANY_FINDING"), _Context())
        assert not ctrl.create_kwargs, "nothing may be written under a mode that was not understood"

    def test_the_error_names_the_modes_that_do_exist(self, monkeypatch):
        self._ctrl(monkeypatch)
        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_policy_create_update(_policy_event(ValidationMode="OFF"), _Context())
        for mode in ("IGNORE_ALL_FINDINGS", "FAIL_ON_ANY_FINDINGS"):
            assert mode in str(excinfo.value)

    def test_an_old_botocore_loses_the_newer_parameter_first(self, monkeypatch):
        """Dropping both at once would silently re-enable FAIL_ON_ANY_FINDINGS.

        That is the mode which fails asynchronously on a gateway created moments
        earlier, so it is worth keeping for as long as the installed botocore can
        express it.
        """
        ctrl = self._ctrl(
            monkeypatch,
            create_raises=provider.ParamValidationError(report="unknown parameter enforcementMode"),
            create_raises_when=lambda kwargs: "enforcementMode" in kwargs,
        )
        provider._handle_policy_create_update(_policy_event(), _Context())
        assert [sorted(set(k) & {"validationMode", "enforcementMode"}) for k in ctrl.create_kwargs] == [
            ["enforcementMode", "validationMode"],
            ["validationMode"],
        ]

    def test_a_botocore_that_knows_neither_still_writes_the_policy(self, monkeypatch):
        ctrl = self._ctrl(
            monkeypatch,
            # Botocore names the parameter it does not recognise, one line per key,
            # which is what the ladder reads to decide whether the error is about the
            # parameter this step would drop.
            create_raises=provider.ParamValidationError(
                report='Unknown parameter in input: "enforcementMode"\nUnknown parameter in input: "validationMode"'
            ),
            create_raises_when=lambda kwargs: bool({"validationMode", "enforcementMode"} & set(kwargs)),
        )
        data, _ = provider._handle_policy_create_update(_policy_event(), _Context())
        assert data["PolicyId"] == "policy_new-1a2b3c4d5e"
        assert len(ctrl.create_kwargs) == 3, "one attempt per parameter set, most capable first"

    def test_a_parameter_error_that_is_not_about_those_two_surfaces_at_once(self, monkeypatch):
        """A malformed call must not be mistaken for an old botocore.

        This is not hypothetical: ``update_policy``'s description is a structure, and
        sending create's bare string raises ParamValidationError too. Treating every
        such error as "this boto3 is too old" would have retried that three times,
        dropped both mode parameters along the way, and then reported the failure as
        if the modes were the problem.
        """
        ctrl = self._ctrl(
            monkeypatch,
            create_raises=provider.ParamValidationError(report="Missing required parameter: name"),
        )
        with pytest.raises(provider.ParamValidationError):
            provider._handle_policy_create_update(_policy_event(), _Context())
        assert len(ctrl.create_kwargs) == 1, "it surfaces the real error rather than dropping parameters"

    def test_the_modes_are_the_only_thing_the_ladder_ever_drops(self, monkeypatch):
        """Everything else has to reach the service on the first attempt.

        The ladder retries by sending *fewer* parameters. If the statement or the
        engine id could be dropped the retry would write a different policy than the
        template asked for, so the difference between the attempts is pinned, not just
        the count.
        """
        ctrl = self._ctrl(
            monkeypatch,
            create_raises=provider.ParamValidationError(
                report='Unknown parameter in input: "enforcementMode"\nUnknown parameter in input: "validationMode"'
            ),
            create_raises_when=lambda kwargs: bool({"validationMode", "enforcementMode"} & set(kwargs)),
        )
        provider._handle_policy_create_update(_policy_event(), _Context())
        stable = [{k: v for k, v in kw.items() if k not in _MODE_PARAMETERS} for kw in ctrl.create_kwargs]
        assert stable[0] == stable[1] == stable[2]
        assert stable[0]["definition"]["cedar"]["statement"].startswith("permit(")

    def test_a_policy_that_settles_failed_after_a_successful_call_fails_the_resource(self, monkeypatch):
        """The asynchronous rejection, reproduced live under FAIL_ON_ANY_FINDINGS.

        ``CreatePolicy`` answered 200/CREATING and the policy then settled
        CREATE_FAILED with "Overly Permissive: ... (Any Future Tools) and resource
        (gateway/*)". Without the status poll the stack reports CREATE_COMPLETE and
        the agent has no policy at all.
        """
        self._ctrl(
            monkeypatch,
            create_status="CREATING",
            poll_status="CREATE_FAILED",
            poll_reasons=["Overly Permissive: Policy Engine will allow every request"],
        )
        with pytest.raises(provider.ProviderError, match="Overly Permissive"):
            provider._handle_policy_create_update(_policy_event(), _Context())


# ---------------------------------------------------------------------------
# An update must not delete the resource it is updating
# ---------------------------------------------------------------------------


class TestOAuth2ProviderUpdate:
    """Proven live: the old update deleted the provider and then created a new one.

    When the create leg failed, CloudFormation reported UPDATE_ROLLBACK_COMPLETE — a
    stack that looks healthy — over a provider that no longer existed, because
    rollback cannot restore a resource the handler destroyed itself. Even the happy
    path broke live traffic for the length of the create, since the gateway target
    cannot fetch a token while the provider is gone.
    """

    def test_the_provider_is_updated_in_place_and_never_deleted(self, monkeypatch):
        ctrl = FakeAgentCore(arn="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        data, physical_id = provider._handle_oauth2_cred_update(
            _oauth_event("Update", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")
        )

        assert "delete" not in ctrl.calls, "an update deleted the resource it was updating"
        assert ctrl.calls == ["update"]
        assert data["CredentialProviderArn"] == "arn:aws:bedrock-agentcore:::provider/agent-demo-mcp"
        # A changed physical id tells CloudFormation the resource was REPLACED, and it
        # then deletes what it believes is the old one — which here is the same
        # provider that was just updated.
        assert physical_id == "arn:aws:bedrock-agentcore:::provider/agent-demo-mcp"

    def test_the_new_secret_is_the_one_sent(self, monkeypatch):
        """A secret rotation is the reason this resource gets updated at all."""
        ctrl = FakeAgentCore()
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)
        event = _oauth_event("Update")
        event["ResourceProperties"]["ClientSecret"] = "rotated-not-a-real-secret"

        provider._handle_oauth2_cred_update(event)

        config = ctrl.update_kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert config["clientSecret"] == "rotated-not-a-real-secret"
        assert config["clientId"] == "client-a"

    def test_an_updated_provider_returns_the_same_attributes_a_created_one_does(self, monkeypatch):
        """Or a GetAtt that resolved after Create goes missing after Update."""
        ctrl = FakeAgentCore()
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)
        runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/demo-abc"
        event = _oauth_event("Update")
        event["ResourceProperties"]["RuntimeArn"] = runtime_arn

        updated, _ = provider._handle_oauth2_cred_update(event)
        created, _ = provider._handle_oauth2_cred_create(event)
        assert set(updated) == set(created) == {"CredentialProviderArn", "McpEndpointUrl"}

    def test_a_provider_someone_removed_out_of_band_is_recreated(self, monkeypatch):
        ctrl = FakeAgentCore(update_raises=_Exceptions.ResourceNotFoundException("gone"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_update(_oauth_event("Update"))
        assert ctrl.calls == ["update", "create"]

    def test_any_other_update_failure_is_not_papered_over_with_a_create(self, monkeypatch):
        """AccessDenied means fix the permission, not create a second provider."""
        ctrl = FakeAgentCore(update_raises=_client_error("AccessDeniedException", "UpdateOauth2CredentialProvider"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(ClientError):
            provider._handle_oauth2_cred_update(_oauth_event("Update"))
        assert "create" not in ctrl.calls


# ---------------------------------------------------------------------------
# Create must not adopt a resource whose Delete would break somebody else
# ---------------------------------------------------------------------------


class TestOAuth2ProviderAdoption:
    """Create used to answer "already exists" by adopting the provider, and Delete
    then destroys whatever was adopted.

    The name is derived from the deployment name, so the same name is reached by the
    platform's own live deploy of the same agent and by any stack sharing that
    deployment name. Adopting there means tearing down THIS stack deletes a
    credential provider a different, working deployment depends on. The client id
    settles ownership: every incarnation of this stack makes its own Cognito app
    client.
    """

    ALREADY_EXISTS = _Exceptions.ValidationException("provider already exists")

    def test_a_provider_for_this_stacks_client_is_taken_over_and_refreshed(self, monkeypatch):
        ctrl = FakeAgentCore(create_raises=self.ALREADY_EXISTS, existing_client_id="client-a")
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        data, _ = provider._handle_oauth2_cred_create(_oauth_event("Create"))

        # Updated, not merely fetched: a retry may follow a secret rotation, and the
        # secret is the one thing `get` cannot tell us is current.
        assert ctrl.calls == ["create", "get", "update"]
        assert data["CredentialProviderArn"]

    def test_a_provider_belonging_to_something_else_is_refused_not_hijacked(self, monkeypatch):
        ctrl = FakeAgentCore(create_raises=self.ALREADY_EXISTS, existing_client_id="someone-elses-client")
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError) as exc:
            provider._handle_oauth2_cred_create(_oauth_event("Create"))

        message = str(exc.value)
        assert "agent-demo-mcp" in message
        assert "delete-oauth2-credential-provider" in message, "the operator needs the way out"
        assert "DeploymentName" in message, "and the other way out"
        # Nothing was taken over and nothing was destroyed.
        assert "update" not in ctrl.calls
        assert "delete" not in ctrl.calls

    def test_a_validation_error_that_is_not_a_collision_still_propagates(self, monkeypatch):
        ctrl = FakeAgentCore(create_raises=_Exceptions.ValidationException("discoveryUrl is not reachable"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(_Exceptions.ValidationException):
            provider._handle_oauth2_cred_create(_oauth_event("Create"))
        assert ctrl.calls == ["create"]


class TestOAuth2ProviderDelete:
    def test_a_delete_that_did_not_work_fails_the_resource(self, monkeypatch):
        """Silent before. It now blocks the *next* deployment, so it must be loud.

        Create refuses to take over a same-named provider bound to a different OAuth
        client, so a provider left behind by a swallowed delete stops the next stack
        with that DeploymentName — far from the delete that actually failed.
        """
        ctrl = FakeAgentCore(delete_raises=_client_error("AccessDeniedException", "DeleteOauth2CredentialProvider"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError) as exc:
            provider._handle_oauth2_cred_delete(
                _oauth_event("Delete", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")
            )
        assert "delete-oauth2-credential-provider" in str(exc.value)

    def test_an_already_deleted_provider_is_benign(self, monkeypatch):
        ctrl = FakeAgentCore(delete_raises=_client_error("ResourceNotFoundException", "DeleteOauth2CredentialProvider"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_delete(
            _oauth_event("Delete", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")
        )  # must not raise

    def test_a_failure_reason_never_carries_a_botocore_message(self, monkeypatch):
        """The create/update requests for this resource carry a client secret.

        botocore builds ClientError messages out of the request, so the reason that
        reaches CloudFormation stack events — permanent, and visible to anyone who can
        DescribeStackEvents — must never be built from one.
        """
        ctrl = FakeAgentCore(delete_raises=_client_error("AccessDeniedException", "DeleteOauth2CredentialProvider"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError) as exc:
            provider._handle_oauth2_cred_delete(
                _oauth_event("Delete", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")
            )
        reason = provider._safe_failure_reason(exc.value)
        assert "AccessDeniedException from DeleteOauth2CredentialProvider" not in reason
        assert "not-a-real-secret" not in reason


# ---------------------------------------------------------------------------
# The S3 artifact path
# ---------------------------------------------------------------------------


class FakeS3:
    def __init__(self, **behaviour):
        self.calls: list[tuple[str, dict]] = []
        self._behaviour = behaviour

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        bodies = self._behaviour.get("bodies", {})
        return {"Body": _Body(bodies.get(kwargs.get("Key"), self._behaviour.get("body", b"")))}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        exc = self._behaviour.get("delete_raises")
        if exc:
            raise exc


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _zip(members: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


class TestCodePackageDelete:
    """A Delete arrives with whatever properties CloudFormation recorded.

    Both were indexed, so a resource that failed before its properties were recorded —
    or one created by an older template — raised KeyError here. On a Delete that
    becomes a FAILED Delete, and a FAILED Delete leaves the stack in DELETE_FAILED
    over an S3 object nobody needs.
    """

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-properties"),
            pytest.param({"OutputKey": "deployments/demo/code.zip"}, id="no-bucket"),
            pytest.param({"ArtifactsBucket": "bkt"}, id="no-key"),
        ],
    )
    def test_missing_properties_do_not_fail_the_delete(self, monkeypatch, props):
        s3 = FakeS3()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        data, physical_id = provider._handle_code_package_delete(
            {
                "RequestType": "Delete",
                "StackId": STACK_ID,
                "LogicalResourceId": "AgentCodePackage",
                "PhysicalResourceId": "bkt/deployments/demo/code.zip",
                "ResourceProperties": props,
            }
        )
        assert data == {}
        assert physical_id == "bkt/deployments/demo/code.zip"
        assert s3.calls == [], "tried to delete an object it could not name"

    def test_a_failed_object_delete_does_not_block_teardown(self, monkeypatch):
        """Deliberately the opposite choice from the policy handler.

        The object is a build artifact, not data; the generated README already says S3
        artifacts survive teardown. Failing here would block deletion of the whole
        stack over a few megabytes.
        """
        s3 = FakeS3(delete_raises=_client_error("AccessDenied", "DeleteObject"))
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        provider._handle_code_package_delete(
            {
                "RequestType": "Delete",
                "StackId": STACK_ID,
                "PhysicalResourceId": "bkt/k",
                "ResourceProperties": {"ArtifactsBucket": "bkt", "OutputKey": "k"},
            }
        )  # must not raise


class TestTheStagingBucketIsPinnedToItsOwner:
    """The bucket name arrives as a resource property, so ownership must be asserted.

    Without ``ExpectedBucketOwner`` the handler reads the agent's code from — and
    writes the merged code.zip to — whichever account owns a bucket of that name.
    S3 answers 403 when the owner does not match, which turns a name mix-up, or a
    name someone else claimed in another account, into a refusal instead of a
    cross-account artifact exchange.
    """

    def test_the_expected_owner_is_the_account_that_owns_the_stack(self):
        assert provider._stack_account_id({"StackId": STACK_ID}) == ACCOUNT
        assert provider._owner_kwargs({"StackId": STACK_ID}) == {"ExpectedBucketOwner": ACCOUNT}

    @pytest.mark.parametrize(
        "stack_id",
        [pytest.param("", id="absent"), pytest.param("not-an-arn", id="unparseable")],
    )
    def test_an_unreadable_stack_id_omits_the_parameter_rather_than_guessing(self, stack_id):
        """A wrong owner would fail every call; omitting it only loses the check."""
        assert provider._owner_kwargs({"StackId": stack_id}) == {}

    def test_every_s3_call_in_the_merge_carries_it(self, monkeypatch):
        s3 = FakeS3(bodies={"a.zip": _zip({"agent.py": "print('hello')\n"}), "b.zip": _zip({"deps/x.py": "X = 1\n"})})
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        provider._handle_code_package_create_update(
            {
                "RequestType": "Create",
                "StackId": STACK_ID,
                "ResourceProperties": {
                    "ArtifactsBucket": "bkt",
                    "AgentCodeKey": "a.zip",
                    "DependencyBundleKey": "b.zip",
                    "OutputKey": "code.zip",
                },
            }
        )
        assert s3.calls, "no S3 call was made at all"
        for name, kwargs in s3.calls:
            assert kwargs.get("ExpectedBucketOwner") == ACCOUNT, f"{name} did not pin the bucket owner"

    def test_the_delete_carries_it_too(self, monkeypatch):
        s3 = FakeS3()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        provider._handle_code_package_delete(
            {
                "RequestType": "Delete",
                "StackId": STACK_ID,
                "ResourceProperties": {"ArtifactsBucket": "bkt", "OutputKey": "k"},
            }
        )
        assert s3.calls[0][1].get("ExpectedBucketOwner") == ACCOUNT


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class _Context:
    log_stream_name = "2026/09/18/[$LATEST]abc"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 900_000


class TestDispatch:
    @pytest.fixture
    def sent(self, monkeypatch):
        """Capture what would be sent to the CloudFormation response URL."""
        calls = []
        monkeypatch.setattr(
            provider.cfn_response,
            "send",
            lambda event, context, status, **kwargs: calls.append((status, kwargs)) or True,
        )
        return calls

    def test_an_unrecognised_resource_type_fails_instead_of_guessing(self, monkeypatch, sent):
        """It used to fall through to the code packager as a default.

        A typo in a template's "Type", or a fourth custom resource added without
        wiring it up here, therefore ran the WRONG handler and reported SUCCESS.
        """
        s3 = FakeS3()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        provider.handler(
            {
                "RequestType": "Create",
                "StackId": STACK_ID,
                "LogicalResourceId": "Mystery",
                "ResourceType": "Custom::AgentCodePackages",  # one letter out
                "ResourceProperties": {},
            },
            _Context(),
        )

        assert s3.calls == [], "an unrecognised type reached the code packager"
        ((status, kwargs),) = sent
        assert status == provider.cfn_response.FAILED
        assert kwargs["physical_resource_id"] == "Mystery"

    def test_a_delete_of_an_unrecognised_type_succeeds_instead_of_wedging_the_stack(self, monkeypatch, sent):
        """The one exception to failing closed, and it was found live.

        Adding ``Custom::RuntimeLogGroup`` to a stack whose provider Lambda predated it
        failed the create; the rollback then reverted the Lambda's CODE before sending
        the Delete, so the Delete arrived at a handler that had never heard of the type.
        Failing it produced three DELETE_FAILED retries and left the stack
        UPDATE_ROLLBACK_COMPLETE with "One or more resources could not be deleted" — and
        a recipient hits the same thing rolling back any update that adds a new type.

        Succeeding is safe here in a way it is not on Create: a Delete this code cannot
        interpret names nothing it could destroy.
        """
        s3 = FakeS3()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: s3)

        provider.handler(
            {
                "RequestType": "Delete",
                "StackId": STACK_ID,
                "LogicalResourceId": "FromTheFuture",
                "ResourceType": "Custom::SomethingThisCodeIsOlderThan",
                "PhysicalResourceId": "whatever-the-newer-code-returned",
                "ResourceProperties": {},
            },
            _Context(),
        )

        assert s3.calls == [], "the delete of an unknown type touched something"
        ((status, kwargs),) = sent
        assert status == provider.cfn_response.SUCCESS
        # Echoed back unchanged: CloudFormation matches the response to the resource by
        # this id, and inventing a new one on a Delete leaves it unable to.
        assert kwargs["physical_resource_id"] == "whatever-the-newer-code-returned"

    def test_an_unknown_request_type_fails_too(self, monkeypatch, sent):
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: FakeS3())

        provider.handler(
            {
                "RequestType": "Snapshot",
                "StackId": STACK_ID,
                "LogicalResourceId": "AgentCodePackage",
                "ResourceType": "Custom::AgentCodePackage",
                "ResourceProperties": {},
            },
            _Context(),
        )
        assert sent[0][0] == provider.cfn_response.FAILED

    def test_the_supported_types_are_the_ones_the_template_emits(self):
        assert provider.SUPPORTED_RESOURCE_TYPES == frozenset(
            {
                "Custom::AgentCodePackage",
                "Custom::OAuth2CredentialProvider",
                "Custom::AgentCorePolicy",
                "Custom::RuntimeLogGroup",
            }
        )

    def test_a_provider_error_reaches_the_operator_but_a_client_error_does_not(self, monkeypatch, sent):
        """The reason lands in stack events, which are permanent and widely readable.

        ProviderError messages are written here from literals and service status
        fields, so they are safe and are the most actionable thing this Lambda can
        say. Anything else is reduced to a class name, because botocore builds its
        messages from the request and these requests carry a Cognito client secret.
        """
        ctrl = FakeAgentCore(delete_raises=_client_error("AccessDeniedException", "DeleteOauth2CredentialProvider"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)
        event = _oauth_event("Delete", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/agent-demo-mcp")

        provider.handler(event, _Context())
        assert sent[0][0] == provider.cfn_response.FAILED
        assert "delete-oauth2-credential-provider" in sent[0][1]["reason"]

        sent.clear()
        monkeypatch.setattr(
            provider,
            "_delete_oauth2_cred",
            lambda _arn: (_ for _ in ()).throw(_client_error("ThrottlingException", "Delete")),
        )
        provider.handler(event, _Context())
        assert sent[0][0] == provider.cfn_response.FAILED
        assert "ThrottlingException from Delete" not in sent[0][1]["reason"]
        assert "not-a-real-secret" not in sent[0][1]["reason"]


# ---------------------------------------------------------------------------
# An undelivered response is the one failure worth failing the invocation for
# ---------------------------------------------------------------------------


class TestResponseDelivery:
    """A response CloudFormation never receives wedges the stack for an hour.

    ``cfn_response.send`` already retries four times and never raises, so by the
    time it returns False the only lever left is Lambda's own asynchronous-invoke
    retry — and that only happens if the invocation ends in an exception. Returning
    normally, which is what this did, threw those two extra attempts away and left
    the resource to time out.
    """

    URL = "https://cloudformation-custom-resource-response.s3.amazonaws.com/resp?X-Amz-Signature=fake"

    @pytest.fixture
    def undeliverable(self, monkeypatch):
        sent = []

        def _send(event, context, status, **kwargs):
            sent.append(status)
            return False

        monkeypatch.setattr(provider.cfn_response, "send", _send)
        return sent

    def _event(self, monkeypatch, **overrides):
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: FakeAgentCore())
        return _oauth_event("Delete", PhysicalResourceId="arn:aws:bedrock-agentcore:::provider/x", **overrides)

    def test_an_undelivered_success_fails_the_invocation(self, monkeypatch, undeliverable):
        with pytest.raises(provider.ResponseDeliveryError):
            provider.handler(self._event(monkeypatch, ResponseURL=self.URL), _Context())
        assert undeliverable == [provider.cfn_response.SUCCESS]

    def test_an_undelivered_failure_fails_the_invocation_too(self, monkeypatch, undeliverable):
        """Otherwise the stack waits out the timeout to learn something already known."""
        monkeypatch.setattr(
            provider,
            "_delete_oauth2_cred",
            lambda _arn: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(provider.ResponseDeliveryError):
            provider.handler(self._event(monkeypatch, ResponseURL=self.URL), _Context())
        assert undeliverable == [provider.cfn_response.FAILED]

    def test_a_missing_response_url_is_not_retried(self, monkeypatch, undeliverable):
        """A retry cannot make an unusable URL usable, and the work would be redone.

        ``send`` refuses a non-https URL outright — that is the forged-event guard —
        so failing the invocation here would only run the resource three times over.
        """
        provider.handler(self._event(monkeypatch), _Context())
        provider.handler(self._event(monkeypatch, ResponseURL="http://attacker.example/collect"), _Context())

    def test_a_delivered_response_does_not_raise(self, monkeypatch):
        """The ordinary path. Raising here would retry every resource three times."""
        monkeypatch.setattr(provider.cfn_response, "send", lambda *a, **k: True)
        provider.handler(self._event(monkeypatch, ResponseURL=self.URL), _Context())

    def test_the_error_carries_no_part_of_the_presigned_url(self, monkeypatch, undeliverable):
        """The query string of that URL is a credential for this resource's response.

        Anyone holding it can PUT SUCCESS or FAILED for the resource. The exception
        message reaches CloudWatch, so it names the resource and not the URL.
        """
        with pytest.raises(provider.ResponseDeliveryError) as excinfo:
            provider.handler(self._event(monkeypatch, ResponseURL=self.URL), _Context())
        assert "X-Amz-Signature" not in str(excinfo.value)
        assert "s3.amazonaws.com" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Custom::RuntimeLogGroup — the runtime's own logs are the conversation
# ---------------------------------------------------------------------------


DEFAULT_GROUP = "/aws/bedrock-agentcore/runtimes/demo_runtime-aBcDeF1234-DEFAULT"
NAMED_GROUP = "/aws/bedrock-agentcore/runtimes/demo_runtime-aBcDeF1234-demo_endpoint"
KEY_ARN = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"
OTHER_KEY_ARN = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/87654321-4321-4321-4321-210987654321"


def _accepted_by_the_logs_model(operation: str, kwargs: dict) -> None:
    """Validate one call against the real CloudWatch Logs model.

    Same argument as ``_accepted_by_the_real_model``: a fake that happily accepts
    ``kmsKeyArn`` where the API wants ``kmsKeyId``, or a retention value the API
    rejects, lets a green suite ship a handler that fails on the recipient's first
    deploy. Every method on ``FakeLogs`` runs this before doing anything.
    """
    try:
        shape = botocore.session.get_session().get_service_model("logs").operation_model(operation).input_shape
    except Exception as e:  # pragma: no cover - an older botocore may not know it
        pytest.skip(f"this botocore cannot describe logs.{operation}: {e}")
    validate_parameters(kwargs, shape)


class FakeLogs:
    """A CloudWatch Logs stand-in holding only the state the handler branches on.

    ``groups`` maps a log group name to its ``kmsKeyId``, mirroring what
    DescribeLogGroups reports. A group the AgentCore runtime created for itself is
    present with an empty key and no retention — the exact state this resource exists
    to change, and the one the live probe found on both of its groups.
    """

    def __init__(
        self,
        groups=None,
        *,
        create_raises=None,
        associate_raises=None,
        delete_retention_raises=None,
        region="us-east-1",
    ):
        self.groups = dict(groups or {})
        self.retention: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self._create_raises = create_raises
        self._associate_raises = associate_raises
        self._delete_retention_raises = delete_retention_raises
        self.meta = type("Meta", (), {"region_name": region})()

    @property
    def operations(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def _record(self, operation: str, api_operation: str, kwargs: dict) -> None:
        self.calls.append((operation, kwargs))
        _accepted_by_the_logs_model(api_operation, kwargs)

    def create_log_group(self, **kwargs):
        self._record("create_log_group", "CreateLogGroup", kwargs)
        if self._create_raises:
            raise self._create_raises
        name = kwargs["logGroupName"]
        if name in self.groups:
            raise _client_error("ResourceAlreadyExistsException", "CreateLogGroup")
        self.groups[name] = kwargs.get("kmsKeyId", "")

    def describe_log_groups(self, **kwargs):
        self._record("describe_log_groups", "DescribeLogGroups", kwargs)
        prefix = kwargs.get("logGroupNamePrefix", "")
        return {
            "logGroups": [
                {"logGroupName": name, "kmsKeyId": key} for name, key in self.groups.items() if name.startswith(prefix)
            ]
        }

    def associate_kms_key(self, **kwargs):
        self._record("associate_kms_key", "AssociateKmsKey", kwargs)
        if self._associate_raises:
            raise self._associate_raises
        self.groups[kwargs["logGroupName"]] = kwargs["kmsKeyId"]

    def disassociate_kms_key(self, **kwargs):
        self._record("disassociate_kms_key", "DisassociateKmsKey", kwargs)
        self.groups[kwargs["logGroupName"]] = ""

    def put_retention_policy(self, **kwargs):
        self._record("put_retention_policy", "PutRetentionPolicy", kwargs)
        self.retention[kwargs["logGroupName"]] = kwargs["retentionInDays"]

    def delete_retention_policy(self, **kwargs):
        self._record("delete_retention_policy", "DeleteRetentionPolicy", kwargs)
        if self._delete_retention_raises:
            raise self._delete_retention_raises
        self.retention.pop(kwargs["logGroupName"], None)


def _install_logs(monkeypatch, fake: FakeLogs) -> FakeLogs:
    """Hand the handler *fake* instead of a real client, and only for ``logs``."""

    def client(service_name, *args, **kwargs):
        assert service_name == "logs", f"the log group resource asked for a {service_name} client"
        return fake

    monkeypatch.setattr(provider.boto3, "client", client)
    return fake


def _log_group_event(request_type="Create", names=(DEFAULT_GROUP, NAMED_GROUP), **props):
    """A Create/Update/Delete event for the resource the generator emits.

    ``RetentionInDays`` is a string because that is what it is by the time it arrives:
    CloudFormation stringifies every Custom Resource property, so the handler's own
    int conversion is on the live path and not a defensive nicety.
    """
    return {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": "AgentCoreRuntimeLogGroups",
        "ResourceType": "Custom::RuntimeLogGroup",
        "ResourceProperties": {"LogGroupNames": list(names), "RetentionInDays": "7", **props},
    }


class TestRuntimeLogGroupGovernance:
    """AgentCore creates these groups itself, so the handler adopts rather than owns.

    The live finding that shapes all of this: a runtime with one named endpoint has TWO
    log groups, ``<id>-DEFAULT`` and ``<id>-<endpointName>``, both created by the
    service at stack-create time before any invoke, both with no retention and no key —
    and an invoke against the named qualifier writes to the NAMED one while -DEFAULT
    stays empty. A resource that governed only the group it created itself, or only
    -DEFAULT, would have governed the empty one and looked correct doing it.
    """

    def test_both_existing_groups_get_the_retention_and_the_key(self, monkeypatch):
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: "", NAMED_GROUP: ""}))

        data, _physical_id = provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=KEY_ARN))

        assert logs.groups == {DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}
        assert logs.retention == {DEFAULT_GROUP: 7, NAMED_GROUP: 7}
        assert data["LogGroupNames"] == f"{DEFAULT_GROUP},{NAMED_GROUP}"

    def test_a_group_the_service_has_not_made_yet_is_created_with_the_key(self, monkeypatch):
        """Not hypothetical: the group for a named endpoint appears when the endpoint does.

        Creating it with the key already set is also the only way to govern a group
        before the service writes its first event into it.
        """
        logs = _install_logs(monkeypatch, FakeLogs())

        provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=KEY_ARN))

        assert logs.groups == {DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}
        assert logs.retention == {DEFAULT_GROUP: 7, NAMED_GROUP: 7}
        assert "associate_kms_key" not in logs.operations
        assert logs.calls[0] == ("create_log_group", {"logGroupName": DEFAULT_GROUP, "kmsKeyId": KEY_ARN})

    def test_without_a_key_the_group_is_created_on_the_aws_owned_key(self, monkeypatch):
        """No ``kmsKeyId`` at all rather than an empty string, which the API rejects."""
        logs = _install_logs(monkeypatch, FakeLogs())

        provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=""))

        assert logs.calls[0] == ("create_log_group", {"logGroupName": DEFAULT_GROUP})
        assert logs.groups == {DEFAULT_GROUP: "", NAMED_GROUP: ""}

    def test_the_group_is_never_read_before_being_written(self, monkeypatch):
        """No DescribeLogGroups, and this is the assertion that keeps it that way.

        DescribeLogGroups is a list operation, so IAM authorizes it against
        ``arn:aws:logs:<region>:<account>:log-group::log-stream:`` — an EMPTY log group
        name — which no resource-scoped grant can ever match. Verified live: a grant on
        ``log-group:/aws/bedrock-agentcore/runtimes/*`` failed the stack create with
        "not authorized to perform: logs:DescribeLogGroups on resource:
        arn:aws:logs:us-east-1:...:log-group::log-stream:". Re-introducing the read
        means either widening the grant to every log group in the account or breaking
        the deploy, so it is pinned here rather than left to review.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: "", NAMED_GROUP: ""}))

        provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=KEY_ARN))

        assert "describe_log_groups" not in logs.operations

    def test_a_group_already_on_the_right_key_is_associated_again(self, monkeypatch):
        """Not a no-op, because the current key is deliberately not read.

        Verified live that this costs nothing: AssociateKmsKey with the key already
        attached returns success and changes nothing.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}))

        provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=KEY_ARN))

        assert logs.groups == {DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}
        assert logs.operations.count("associate_kms_key") == 2
        assert "disassociate_kms_key" not in logs.operations
        assert logs.retention == {DEFAULT_GROUP: 7, NAMED_GROUP: 7}

    def test_a_group_on_the_wrong_key_is_re_associated(self, monkeypatch):
        """The recipient rotated CustomerManagedKeyArn to a different key."""
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: OTHER_KEY_ARN, NAMED_GROUP: ""}))

        provider._handle_runtime_log_group_create_update(_log_group_event(KmsKeyArn=KEY_ARN))

        assert logs.groups == {DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}
        assert logs.operations.count("associate_kms_key") == 2

    def test_dropping_the_key_reverts_the_group_to_the_aws_owned_key(self, monkeypatch):
        """Otherwise the group stays on a key the recipient is now free to delete —
        which would make every event already written into it permanently unreadable.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: KEY_ARN, NAMED_GROUP: KEY_ARN}))

        provider._handle_runtime_log_group_create_update(_log_group_event(request_type="Update"))

        assert logs.groups == {DEFAULT_GROUP: "", NAMED_GROUP: ""}
        assert logs.operations.count("disassociate_kms_key") == 2

    def test_an_unkeyed_group_is_disassociated_anyway(self, monkeypatch):
        """The price of not reading the group first, and verified live to be safe:
        DisassociateKmsKey on a group that has no key returns success.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: "", NAMED_GROUP: ""}))

        provider._handle_runtime_log_group_create_update(_log_group_event())

        assert logs.operations.count("disassociate_kms_key") == 2
        assert logs.groups == {DEFAULT_GROUP: "", NAMED_GROUP: ""}

    def test_retention_is_applied_to_every_governed_name(self, monkeypatch):
        """Retention, not the key, is the part ARCC cnt_bO6I1SM60fP0J4 turns on."""
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: "", NAMED_GROUP: ""}))

        provider._handle_runtime_log_group_create_update(_log_group_event(RetentionInDays="3653"))

        assert logs.retention == {DEFAULT_GROUP: 3653, NAMED_GROUP: 3653}

    def test_zero_retention_removes_the_policy_rather_than_setting_zero(self, monkeypatch):
        """CloudWatch's "never expire" is the ABSENCE of a policy; 0 is not a valid value."""
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}, region="eu-central-1"))

        provider._handle_runtime_log_group_create_update(_log_group_event(names=(DEFAULT_GROUP,), RetentionInDays="0"))

        assert "put_retention_policy" not in logs.operations
        assert logs.operations.count("delete_retention_policy") == 1

    def test_a_group_with_no_retention_policy_to_delete_is_not_an_error(self, monkeypatch):
        """DeleteRetentionPolicy on a group that never had one; the resource is idempotent."""
        logs = _install_logs(
            monkeypatch,
            FakeLogs(
                {DEFAULT_GROUP: ""},
                delete_retention_raises=_client_error("ResourceNotFoundException", "DeleteRetentionPolicy"),
            ),
        )

        provider._handle_runtime_log_group_create_update(_log_group_event(names=(DEFAULT_GROUP,), RetentionInDays="0"))

        assert logs.operations.count("delete_retention_policy") == 1

    def test_a_real_retention_failure_is_not_swallowed(self, monkeypatch):
        """Only the benign codes are tolerated: AccessDenied must fail the stack."""
        _install_logs(
            monkeypatch,
            FakeLogs(
                {DEFAULT_GROUP: ""},
                delete_retention_raises=_client_error("AccessDeniedException", "DeleteRetentionPolicy"),
            ),
        )

        with pytest.raises(ClientError):
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), RetentionInDays="0")
            )

    def test_a_non_numeric_retention_falls_back_to_never_expire(self, monkeypatch):
        """Never to a number: guessing 30 on a garbled value would silently DELETE logs
        the recipient asked to keep, and that loss is not recoverable.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}))

        provider._handle_runtime_log_group_create_update(_log_group_event(names=(DEFAULT_GROUP,), RetentionInDays=""))

        assert "put_retention_policy" not in logs.operations

    def test_a_single_name_as_a_string_is_tolerated(self, monkeypatch):
        """CloudFormation collapses a one-element list in some paths."""
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}))

        data, _physical_id = provider._handle_runtime_log_group_create_update(
            _log_group_event(LogGroupNames=DEFAULT_GROUP)
        )

        assert data["LogGroupNames"] == DEFAULT_GROUP
        assert logs.retention == {DEFAULT_GROUP: 7}

    def test_duplicate_and_empty_names_are_reduced_to_the_real_ones(self, monkeypatch):
        """A deployment whose endpoint is literally named DEFAULT produces the same name
        twice from the template, and governing it twice would issue redundant KMS calls.
        """
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}))

        data, _physical_id = provider._handle_runtime_log_group_create_update(
            _log_group_event(names=(DEFAULT_GROUP, f"  {DEFAULT_GROUP}  ", "", "   "))
        )

        assert data["LogGroupNames"] == DEFAULT_GROUP
        assert logs.operations.count("put_retention_policy") == 1

    def test_no_names_at_all_is_a_no_op_rather_than_a_failure(self, monkeypatch):
        """A template with no runtime emits no names; failing here would fail that stack."""
        logs = _install_logs(monkeypatch, FakeLogs())

        data, physical_id = provider._handle_runtime_log_group_create_update(_log_group_event(names=()))

        assert data == {"LogGroupNames": ""}
        assert logs.calls == []
        assert physical_id == "runtime-log-groups/AgentCoreRuntimeLogGroups"


class TestRuntimeLogGroupKeyPolicyFailure:
    """The one failure a recipient will actually hit, and the one AWS explains worst.

    Live, both CreateLogGroup and AssociateKmsKey answer "The specified KMS key does
    not exist or is not allowed to be used with Arn '<log group arn>'" when the key
    policy has no statement for the logs service principal. That message points at the
    log group and implies the key is missing; the key is fine and the log group is
    irrelevant. The stack event has to say what to add.
    """

    DENIED = "AccessDeniedException"

    def _assert_names_the_remedy(self, excinfo):
        message = str(excinfo.value)
        assert "logs.us-east-1.amazonaws.com" in message
        assert "kms:EncryptionContext:aws:logs:arn" in message
        assert "README.md > Encryption" in message
        assert KEY_ARN in message
        assert DEFAULT_GROUP in message

    def test_create_says_which_statement_the_key_policy_needs(self, monkeypatch):
        _install_logs(monkeypatch, FakeLogs(create_raises=_client_error(self.DENIED, "CreateLogGroup")))

        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN)
            )

        self._assert_names_the_remedy(excinfo)

    def test_associate_says_the_same_thing_on_an_adopted_group(self, monkeypatch):
        """The likelier path of the two: the group already exists, so create never runs."""
        _install_logs(
            monkeypatch,
            FakeLogs({DEFAULT_GROUP: ""}, associate_raises=_client_error(self.DENIED, "AssociateKmsKey")),
        )

        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN)
            )

        self._assert_names_the_remedy(excinfo)

    def test_the_region_comes_from_the_client_not_a_hardcoded_default(self, monkeypatch):
        """The handler imports no ``os``, so the service principal has to be read off the
        client. Hardcoding us-east-1 would send a Frankfurt recipient the wrong statement.
        """
        _install_logs(
            monkeypatch,
            FakeLogs(
                {DEFAULT_GROUP: ""},
                associate_raises=_client_error(self.DENIED, "AssociateKmsKey"),
                region="eu-central-1",
            ),
        )

        with pytest.raises(provider.ProviderError) as excinfo:
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN)
            )

        assert "logs.eu-central-1.amazonaws.com" in str(excinfo.value)
        assert "arn:aws:logs:eu-central-1:<account>:log-group:*" in str(excinfo.value)

    def test_access_denied_without_a_key_is_not_reported_as_a_key_problem(self, monkeypatch):
        """Same code, entirely different cause: the role is missing logs:CreateLogGroup.
        Handing that operator a key policy to edit would send them days in the wrong place.
        """
        _install_logs(monkeypatch, FakeLogs(create_raises=_client_error(self.DENIED, "CreateLogGroup")))

        with pytest.raises(ClientError):
            provider._handle_runtime_log_group_create_update(_log_group_event(names=(DEFAULT_GROUP,)))

    def test_an_unrelated_create_failure_propagates_untouched(self, monkeypatch):
        """Throttling is retried by botocore and then real; it must not be mistaken for
        "the group already exists" and silently skipped.
        """
        _install_logs(
            monkeypatch,
            FakeLogs(create_raises=_client_error("ThrottlingException", "CreateLogGroup")),
        )

        with pytest.raises(ClientError):
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN)
            )

    def test_an_unrelated_associate_failure_propagates_untouched(self, monkeypatch):
        _install_logs(
            monkeypatch,
            FakeLogs({DEFAULT_GROUP: ""}, associate_raises=_client_error("ThrottlingException", "AssociateKmsKey")),
        )

        with pytest.raises(ClientError):
            provider._handle_runtime_log_group_create_update(
                _log_group_event(names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN)
            )


class TestRuntimeLogGroupDeleteAndIdentity:
    """Delete must not delete, and update must not replace."""

    def test_delete_leaves_the_logs_in_place(self, monkeypatch):
        """The whole point. These groups hold the agent's conversations; per ARCC
        cnt_bO6I1SM60fP0J4 security-relevant logs are retained for years, and a stack
        teardown is not authority to destroy the audit trail of what the agent did.
        No client is created at all, so there is nothing that could delete them.
        """

        def no_clients(service_name, *args, **kwargs):
            raise AssertionError(f"Delete built a {service_name} client; it must touch nothing")

        monkeypatch.setattr(provider.boto3, "client", no_clients)

        data, physical_id = provider._handle_runtime_log_group_delete(_log_group_event(request_type="Delete"))

        assert data == {}
        assert physical_id == "runtime-log-groups/AgentCoreRuntimeLogGroups"

    def test_delete_of_a_resource_with_no_recorded_names_still_succeeds(self, monkeypatch):
        """A rollback of a failed Create sends a Delete with whatever properties it had.
        Raising here is what wedges a stack in DELETE_FAILED.
        """
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: pytest.fail("no client expected"))

        data, _physical_id = provider._handle_runtime_log_group_delete(
            {"RequestType": "Delete", "LogicalResourceId": "AgentCoreRuntimeLogGroups", "ResourceProperties": {}}
        )

        assert data == {}

    def test_an_update_keeps_the_physical_id_cloudformation_already_has(self, monkeypatch):
        """Returning a new id makes CloudFormation treat the update as a replacement and
        send a Delete for the old one — which for this resource is a no-op, so the stack
        would look fine while the id churned on every update.
        """
        _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}))
        event = _log_group_event(request_type="Update", names=(DEFAULT_GROUP,))
        event["PhysicalResourceId"] = "runtime-log-groups/SomethingOlder"

        _data, physical_id = provider._handle_runtime_log_group_create_update(event)

        assert physical_id == "runtime-log-groups/SomethingOlder"

    def test_create_and_delete_derive_the_same_id(self, monkeypatch):
        """They must agree, or a Create whose response never arrived cannot be matched
        to the Delete that follows it.
        """
        _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: ""}))
        create = _log_group_event(names=(DEFAULT_GROUP,))

        _data, created_id = provider._handle_runtime_log_group_create_update(create)
        _data, deleted_id = provider._handle_runtime_log_group_delete(_log_group_event(request_type="Delete"))

        assert created_id == deleted_id


class TestRuntimeLogGroupDispatch:
    """The router has to reach this handler, which is the bug the router already had."""

    @pytest.fixture
    def sent(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            provider.cfn_response,
            "send",
            lambda event, context, status, **kwargs: calls.append((status, kwargs)) or True,
        )
        return calls

    def _event(self, monkeypatch, request_type="Create", **props):
        event = _log_group_event(request_type, **props)
        event["ResponseURL"] = "https://cloudformation-custom-resource-response.example/x"
        return event

    def test_a_create_is_routed_and_reports_success(self, monkeypatch, sent):
        logs = _install_logs(monkeypatch, FakeLogs({DEFAULT_GROUP: "", NAMED_GROUP: ""}))

        provider.handler(self._event(monkeypatch), _Context())

        status, kwargs = sent[0]
        assert status == "SUCCESS"
        assert kwargs["data"]["LogGroupNames"] == f"{DEFAULT_GROUP},{NAMED_GROUP}"
        assert logs.retention == {DEFAULT_GROUP: 7, NAMED_GROUP: 7}

    def test_a_delete_is_routed_and_reports_success_without_touching_logs(self, monkeypatch, sent):
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: pytest.fail("no client expected"))

        provider.handler(self._event(monkeypatch, request_type="Delete"), _Context())

        assert sent[0][0] == "SUCCESS"

    def test_the_key_policy_remedy_reaches_the_stack_event(self, monkeypatch, sent):
        """A ProviderError's message is the only place the recipient will read it."""
        _install_logs(
            monkeypatch,
            FakeLogs({DEFAULT_GROUP: ""}, associate_raises=_client_error("AccessDeniedException", "AssociateKmsKey")),
        )

        provider.handler(self._event(monkeypatch, names=(DEFAULT_GROUP,), KmsKeyArn=KEY_ARN), _Context())

        status, kwargs = sent[0]
        assert status == "FAILED"
        assert "README.md > Encryption" in kwargs["reason"]


# ---------------------------------------------------------------------------
# The client secret must not travel through CloudFormation
# ---------------------------------------------------------------------------


class _FakeCognito:
    """Minimal cognito-idp stand-in recording what it was asked for."""

    def __init__(self, secret="not-a-real-secret-from-cognito"):
        self._secret = secret
        self.calls = []

    def describe_user_pool_client(self, **kwargs):
        self.calls.append(kwargs)
        client = {"ClientId": kwargs["ClientId"]}
        if self._secret is not None:
            client["ClientSecret"] = self._secret
        return {"UserPoolClient": client}


class TestTheClientSecretIsReadFromCognitoNotFromTheEvent:
    """A secret in a Custom:: resource property is a secret in the stack's events.

    CloudFormation copies the resolved ``ResourceProperties`` of every ``Custom::``
    resource into the event stream, on every event, and keeps them for 90 days — so
    anyone with ``cloudformation:DescribeStackEvents`` could read the Cognito client
    secret. This was found live: the 52-character secret was recovered verbatim from
    three events on a deployed stack, matched against its known value with a positive
    control. ``NoEcho`` was never a defence, because it applies to parameters and this
    arrived as a ``GetAtt``.

    So the template now passes ``UserPoolId`` and the handler reads the secret from
    Cognito itself. These tests pin that the secret is fetched, that the fetch wins
    over any legacy property, and that the deprecated path still works for a stack
    whose template is older than its Lambda.
    """

    def test_the_secret_comes_from_cognito_scoped_to_the_given_pool(self, monkeypatch):
        cognito = _FakeCognito()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: cognito)

        secret = provider._resolve_client_secret({"UserPoolId": "us-east-1_abc", "ClientId": "client-a"})

        assert secret == "not-a-real-secret-from-cognito"
        assert cognito.calls == [{"UserPoolId": "us-east-1_abc", "ClientId": "client-a"}]

    def test_cognito_wins_over_a_legacy_property(self, monkeypatch):
        """Precedence matters: if the property still won, re-exporting would silently
        keep using the leaked value and the fix would look applied while doing nothing."""
        cognito = _FakeCognito()
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: cognito)

        secret = provider._resolve_client_secret(
            {"UserPoolId": "us-east-1_abc", "ClientId": "client-a", "ClientSecret": "stale-not-a-real-secret"}
        )

        assert secret == "not-a-real-secret-from-cognito"

    def test_a_client_with_no_secret_fails_with_the_cause(self, monkeypatch):
        """Better here, naming GenerateSecret, than as an opaque AgentCore rejection."""
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: _FakeCognito(secret=None))

        with pytest.raises(ValueError, match="GenerateSecret"):
            provider._resolve_client_secret({"UserPoolId": "us-east-1_abc", "ClientId": "client-a"})

    def test_the_legacy_property_still_works_without_a_pool(self, monkeypatch):
        """A stack created by an older template keeps updating while its
        cfn-provider.zip is newer than the template that deployed it."""

        def _no_aws(*a, **k):
            raise AssertionError("must not call AWS when falling back to the legacy property")

        monkeypatch.setattr(provider.boto3, "client", _no_aws)

        assert provider._resolve_client_secret({"ClientSecret": "legacy-not-a-real-secret"}) == (
            "legacy-not-a-real-secret"
        )

    def test_neither_source_is_a_hard_error(self, monkeypatch):
        """Fail closed. Passing "" to AgentCore would create a provider that can never
        mint a token, and the stack would go green over it."""
        monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: _FakeCognito())

        with pytest.raises(ValueError, match="UserPoolId"):
            provider._resolve_client_secret({"ClientId": "client-a"})
