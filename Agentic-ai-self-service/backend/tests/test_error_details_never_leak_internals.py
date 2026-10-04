"""A failed deploy must not show the user our stack trace.

The defect, measured rather than reasoned about. ``error_details`` on a deployment
item is returned by ``GET /api/deploy/{id}`` and thrown as the browser's ``Error``
message at ``frontend/src/components/deploy/useDeployment.ts:235``. On a step-Lambda
failure, Step Functions' Catch branch put the Lambda runtime's raw failure envelope
there. Captured live from execution ``deploy-db55fe3e-...`` on
``acfe2e-p0920-deployment`` (see ``REAL_CAUSE`` below): it contains an absolute path
inside our Lambda, the module and line that raised, the raising source line, and a
request id.

ARCC ``cnt_94E30Xo4RZHtSJ``: "Return generic error messages that does not include
details such as: Internal system components, Stack traces, Debug information ...". It
also permits the useful half -- "If the caller has the ability to fix the issue, you
can include simple solutions in the message" -- so these tests pin BOTH directions:
the trace must be gone AND the actionable message must survive. A sanitizer that
returned a constant would satisfy the first and destroy the product.
"""

import json

import pytest
from app.services.error_sanitizer import (
    GENERIC_MESSAGE,
    MAX_LENGTH,
    redact_secrets,
    sanitize_error_details,
)

# Verbatim from the live execution history. Kept as the real thing rather than a
# hand-written approximation, because the exact key names are the contract.
REAL_CAUSE = json.dumps(
    {
        "errorMessage": "Knowledge Base ZZZZZZZZZZ not found",
        "errorType": "ValueError",
        "requestId": "59f7f13c-08a2-4e5d-89b5-171ae66662e8",
        "stackTrace": [
            '  File "/var/task/src/app/step_handlers/knowledge_base_step.py", line 856, in handler\n'
            '    raise ValueError(f"Knowledge Base {kb_id} not found") from None\n'
        ],
    }
)


class TestTheRealEnvelope:
    def test_the_actionable_message_survives(self):
        """The whole point of not replacing everything with a constant."""
        assert sanitize_error_details(REAL_CAUSE) == "Knowledge Base ZZZZZZZZZZ not found"

    @pytest.mark.parametrize(
        "leak",
        [
            "stackTrace",
            "/var/task/",
            "knowledge_base_step.py",
            "line 856",
            "requestId",
            "59f7f13c-08a2-4e5d-89b5-171ae66662e8",
            "raise ValueError",
        ],
    )
    def test_no_internal_detail_survives(self, leak):
        assert leak not in sanitize_error_details(REAL_CAUSE), f"{leak!r} reached the user"

    def test_the_step_functions_catch_wrapper_is_unwrapped(self):
        """What status_update_step actually receives: the envelope is nested inside a
        Catch payload whose Cause is a JSON *string*."""
        wrapped = {"Error": "ValueError", "Cause": REAL_CAUSE}
        out = sanitize_error_details(wrapped)
        assert out == "Knowledge Base ZZZZZZZZZZ not found"
        assert "stackTrace" not in out


class TestUnrecognisedShapesFailClosed:
    """An allow-list, not a deny-list: a shape the sanitizer does not understand must
    collapse to the generic message rather than pass through. Otherwise the next new
    field in a Lambda failure envelope leaks by default."""

    def test_none_and_empty(self):
        assert sanitize_error_details(None) == GENERIC_MESSAGE
        assert sanitize_error_details("") == GENERIC_MESSAGE
        assert sanitize_error_details("   ") == GENERIC_MESSAGE

    def test_a_truncated_envelope_is_not_passed_through_as_text(self):
        """A Cause clipped mid-JSON still starts with '{' and still contains the
        trace. Returning it as a plain message would defeat the whole thing."""
        truncated = REAL_CAUSE[: len(REAL_CAUSE) // 2]
        out = sanitize_error_details(truncated)
        assert out == GENERIC_MESSAGE
        assert "/var/task/" not in out

    def test_an_envelope_with_no_message_falls_back_to_the_type_only(self):
        out = sanitize_error_details(json.dumps({"errorType": "ValueError", "stackTrace": ["x"]}))
        assert out == "ValueError"

    def test_a_message_that_embeds_a_trace_is_refused_entirely(self):
        """Not trimmed -- refused. A partial redaction of a trace is still a trace."""
        out = sanitize_error_details('Step failed: File "/var/task/src/app/x.py", line 3, in handler')
        assert out == GENERIC_MESSAGE

    def test_a_non_string_non_dict_is_refused(self):
        assert sanitize_error_details(12345) == GENERIC_MESSAGE
        assert sanitize_error_details(["a", "b"]) == GENERIC_MESSAGE

    def test_a_plain_exception_string_is_kept(self):
        """The common, benign case: a handler raised with a clear message."""
        assert sanitize_error_details("Runtime name already in use") == "Runtime name already in use"


class TestSecretRedaction:
    """Defence in depth. The primary control is not putting secrets in exceptions, but
    a botocore error echoes the request parameters that produced it -- which is how a
    Cognito client secret reaches an exception string. ARCC cnt_rHmO501l15qr2W /
    cnt_SaTYaDCgBBJTcv forbid credentials in messages and logs."""

    @pytest.mark.parametrize(
        "secret",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAY34FZKBOKMUTVV7A",
            # A Cognito app-client secret is a long base64-ish run.
            "26iu4fkk0hpmv8k5ek1lhnecfstuqnj9dmd3v0lm18mhc4rtg4p2",
        ],
    )
    def test_credential_shaped_values_are_removed(self, secret):
        out = sanitize_error_details(f"Auth failed using {secret} against the token endpoint")
        assert secret not in out, f"{secret} survived redaction"

    def test_named_secret_assignments_are_redacted(self):
        out = redact_secrets("client_secret=hunter2 and Authorization: Bearer abc.def")
        assert "hunter2" not in out
        # `abc.def` is asserted absent HERE because this test used to assert only
        # `"redacted" in out`, and that passed while the bearer token survived in full.
        # See TestAuthorizationHeaders below: a marker appearing proves a substitution
        # happened somewhere, not that the credential is gone.
        assert "abc.def" not in out
        assert "redacted" in out

    def test_presigned_url_credentials_are_redacted(self):
        url = "https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeefcafe&X-Amz-Credential=AKIA/x"
        out = redact_secrets(url)
        assert "deadbeefcafe" not in out

    def test_redaction_leaves_ordinary_text_alone(self):
        """A redactor that mangles normal messages gets disabled by the next person."""
        message = "Knowledge Base ZZZZZZZZZZ not found"
        assert redact_secrets(message) == message


class TestAuthorizationHeaders:
    """The credential after the auth scheme, which the first version of this module left
    behind entirely.

    The named-secret rule matched ``authorization\\s*[:=]\\s*\\S+``, and ``\\S+`` stops at
    the first space. So it consumed the word ``Bearer`` -- the scheme, not the secret --
    and produced ``Authorization=[redacted] abc.def``. The long-base64 rule did not cover
    for it either: that needs a 40-character run of ``[A-Za-z0-9+/]``, and real tokens are
    often shorter, while a JWT's ``.`` separators break the run into segments.

    Every assertion here is on the ABSENCE OF THE CREDENTIAL. Asserting that
    ``"redacted" in out`` is what let the bug through the original suite: it is satisfied
    by a substitution happening anywhere in the string.
    """

    #: (input, the credential that must not survive)
    CASES = [
        ("Authorization: Bearer abc.def", "abc.def"),
        ("authorization=Bearer short-token", "short-token"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ('Authorization: "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"', "eyJhbGciOiJIUzI1NiJ9.payload.sig"),
        ("AUTHORIZATION : bearer abc.def", "abc.def"),
        # No scheme at all: the value itself is the credential.
        ("Authorization: s3cr3tvalue", "s3cr3tvalue"),
        # Presented without the header key, as a botocore message or prose would.
        ("using Bearer abc.def to call the gateway", "abc.def"),
        ("Basic dXNlcjpwYXNz was rejected", "dXNlcjpwYXNz"),
        # Alongside another secret, which is the shape the original test used.
        ("client_secret=hunter2 and Authorization: Bearer abc.def", "abc.def"),
    ]

    @pytest.mark.parametrize(("raw", "credential"), CASES, ids=[c[0][:42] for c in CASES])
    def test_the_credential_does_not_survive(self, raw, credential):
        out = redact_secrets(raw)
        assert credential not in out, f"credential survived redaction: {out!r}"

    @pytest.mark.parametrize(("raw", "credential"), CASES, ids=[c[0][:42] for c in CASES])
    def test_the_credential_does_not_survive_the_public_entry_point(self, raw, credential):
        """``redact_secrets`` is also reached via ``sanitize_error_details``, which is what
        actually writes to DynamoDB and to the browser. Pinned separately so a future
        refactor cannot fix one path and leave the other."""
        envelope = json.dumps({"errorMessage": f"Call failed: {raw}", "errorType": "ClientError"})
        out = sanitize_error_details(envelope)
        assert credential not in out, f"credential survived sanitize_error_details: {out!r}"

    def test_a_bare_scheme_with_no_credential_is_still_handled(self):
        """Degenerate input must not crash or pass through as-is."""
        assert "Bearer" not in redact_secrets("Authorization: Bearer")

    @pytest.mark.parametrize(
        "benign",
        [
            "Retrieved the authorization decision for the user",
            "The authorization flow completed",
            "Knowledge Base ZZZZZZZZZZ not found",
        ],
    )
    def test_prose_containing_the_word_is_left_alone(self, benign):
        """The counterpart. Redacting the word ``authorization`` wherever it appears would
        destroy the actionable messages this module exists to preserve, and a redactor
        that mangles ordinary errors is one the next person turns off."""
        assert redact_secrets(benign) == benign


class TestShape:
    def test_newlines_are_collapsed(self):
        out = sanitize_error_details("first line\nsecond line")
        assert "\n" not in out
        assert out == "first line second line"

    def test_length_is_capped(self):
        """``"x" * N`` was passing vacuously: an unbroken run of 40+ base64 characters is
        replaced wholesale by the long-secret rule, so the result was the 21-character
        ``[redacted]`` form and the cap was never exercised at all. Spaces keep the input
        long and unredactable, which is what a real paste looks like."""
        out = sanitize_error_details("unstructured paste. " * MAX_LENGTH)
        assert len(out) <= MAX_LENGTH
        assert out.endswith("…"), out[-40:]

    def test_the_result_is_never_empty(self):
        """The UI falls back to its own text on an empty message, but an empty stored
        value also makes a failed deploy look like it failed for no reason."""
        for raw in (None, "", "{}", "{ not json", 0, []):
            assert sanitize_error_details(raw).strip()


class TestTheStoreSeamCannotBeBypassed:
    """The sanitizer is applied inside ``DeploymentStateStore.update_status`` rather
    than at each caller, so a future failure path cannot reintroduce the leak by
    forgetting to sanitize. Pin that, structurally and behaviourally."""

    def test_the_store_sanitizes_what_it_writes(self, monkeypatch):
        """Drive the real ``update_status`` and inspect the actual DynamoDB call.

        Stubbed at the boto3 Table, not at ``_update_item``, so the assertion is on the
        bytes that would go over the wire -- the value that ends up in the item that
        ``GET /api/deploy/{id}`` returns.
        """
        from datetime import datetime, timezone

        from app.models.deployment_models import DeploymentStatusEnum
        from app.services import deployment_state_store as mod

        captured: dict = {}

        class _FakeTable:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        class _Existing:
            started_at = datetime.now(timezone.utc)

        store = mod.DeploymentStateStore.__new__(mod.DeploymentStateStore)
        store._table = _FakeTable()
        monkeypatch.setattr(store, "get", lambda _id: _Existing(), raising=False)

        store.update_status("dep-1", DeploymentStatusEnum.FAILED, error_details=REAL_CAUSE)

        written = captured["ExpressionAttributeValues"][":error_details"]
        assert written == "Knowledge Base ZZZZZZZZZZ not found"
        assert "stackTrace" not in written and "/var/task/" not in written

    def test_the_store_write_is_reached_at_all(self, monkeypatch):
        """Vacuity guard for the test above: if ``update_status`` ever stopped writing
        ``:error_details``, the assertion would fail on a KeyError that reads like a
        broken test rather than a lost field. Pin the field's presence separately."""
        from datetime import datetime, timezone

        from app.models.deployment_models import DeploymentStatusEnum
        from app.services import deployment_state_store as mod

        captured: dict = {}

        class _FakeTable:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        class _Existing:
            started_at = datetime.now(timezone.utc)

        store = mod.DeploymentStateStore.__new__(mod.DeploymentStateStore)
        store._table = _FakeTable()
        monkeypatch.setattr(store, "get", lambda _id: _Existing(), raising=False)

        store.update_status("dep-1", DeploymentStatusEnum.FAILED, error_details="boom")

        assert "error_details = :error_details" in captured["UpdateExpression"]
        assert captured["ExpressionAttributeValues"][":error_details"] == "boom"

    def test_every_error_details_write_goes_through_the_sanitizer(self):
        """Structural guard: if a second place ever assigns ``:error_details``, it must
        also sanitize. Catches a new write site that this test file would otherwise
        not know about."""
        import pathlib

        src = pathlib.Path(mod_path()).read_text()
        lines = src.splitlines()
        unguarded = []
        for i, line in enumerate(lines):
            if '":error_details"' not in line:
                continue
            if "sanitize_error_details" not in line:
                unguarded.append(f"{i + 1}: {line.strip()}")
        assert not unguarded, (
            "an error_details value is written without sanitize_error_details:\n  "
            + "\n  ".join(unguarded)
            + "\nEverything written here is returned by GET /api/deploy/{id} and shown to the user."
        )


class TestThePlatformsOwnIdentityIsNotDisclosed:
    """An ``AccessDenied`` from AWS quotes the principal the call was made AS.

    Measured live: ``POST /api/workflows/{id}/deploy`` on ``acfe2e-p0920`` returned the
    platform's assumed-role ARN and account id to any ``agent:write`` caller. ARCC
    ``cnt_94E30Xo4RZHtSJ`` lists "Internal system components" among the details a
    message must not contain.
    """

    DENIAL = (
        "User: arn:aws:sts::123456789012:assumed-role/"
        "acfe2e-p0920-WorkflowLambdaRole80E0B348-aT0dkiL4sxj2/abc "
        "is not authorized to perform: iam:CreateRole"
    )

    def test_the_assumed_role_arn_is_removed(self):
        out = sanitize_error_details(self.DENIAL)
        assert "assumed-role" not in out
        assert "WorkflowLambdaRole" not in out

    def test_the_account_id_is_removed(self):
        assert "123456789012" not in sanitize_error_details(self.DENIAL)

    def test_a_bare_account_id_is_removed_too(self):
        """Not every message wraps the account in an ARN."""
        assert "123456789012" not in sanitize_error_details("Access denied in account 123456789012")

    def test_an_iam_role_arn_is_removed(self):
        out = sanitize_error_details("Cannot assume arn:aws:iam::123456789012:role/AgentCoreRuntime-shared")
        assert "arn:aws:iam" not in out

    def test_the_actionable_part_of_the_denial_survives(self):
        """The vacuity direction. Collapsing the whole thing to GENERIC_MESSAGE would
        pass every test above and destroy the only part an operator can act on -- which
        ARCC explicitly permits keeping ("you can include simple solutions")."""
        out = sanitize_error_details(self.DENIAL)
        assert "iam:CreateRole" in out
        assert out != GENERIC_MESSAGE

    def test_a_resource_arn_is_NOT_removed(self):
        """This is a principal redactor, not a blanket ARN redactor. A resource ARN
        usually names the thing the caller asked about, and removing it would turn an
        actionable message into a riddle."""
        out = sanitize_error_details("Gateway arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/g-1 not found")
        assert "gateway/g-1" in out

    def test_a_partition_other_than_aws_is_covered(self):
        """govcloud and China ARNs use aws-us-gov / aws-cn."""
        out = sanitize_error_details("User: arn:aws-us-gov:sts::123456789012:assumed-role/X/y is not authorized")
        assert "assumed-role" not in out

    def test_a_uuids_last_group_is_not_mistaken_for_an_account_id(self):
        """A uuid4's final group is also 12 characters, and **0.355%** of them are all
        decimal (measured over 200k, ~1 in 281). Found on a live
        ``GET /api/deploy/{id}``: the account rule rewrote the id in the very message that
        exists to tell the caller which id was not found, so they could not match the
        response against their own request. Over-redaction rather than a leak, and far too
        rare to notice by hand."""
        deployment_id = "a3f19c22-7b41-4de8-9c02-481920374615"
        out = sanitize_error_details(f"Deployment '{deployment_id}' not found")
        assert deployment_id in out, f"the caller's own id was redacted out of the message: {out}"

    def test_an_account_id_after_a_hyphen_is_still_removed(self):
        """The narrow direction. Excluding every hyphen-adjacent 12-digit run would be a
        simpler rule and would stop redacting the account id where it most often appears:
        the tail of a generated resource name. So the exclusion is a fixed 5-character
        ``xxxx-`` hex lookbehind, not 'preceded by a hyphen'."""
        out = sanitize_error_details("Denied on s3://acfe2e-p0920-frontend-us-east-1-123456789012/key")
        assert "123456789012" not in out, out


class TestAPrefixedSecretNameIsStillASecretName:
    """The named-secret rule began ``\\b(api_?key|...)``, and ``_`` is a word character,
    so there is no word boundary before ``api`` in ``PROVIDER_API_KEY``. The rule caught a
    bare ``api_key=`` and missed every prefixed spelling. Measured before the fix::

        redact_secrets("Environment={'Variables': {'GATEWAY_API_KEY': 'sk-litellm-v1-abc123'}}")
          -> unchanged

    ``PROVIDER_API_KEY`` and ``GATEWAY_API_KEY`` are the env var names this platform sets
    on a runtime, so a botocore error from Create/UpdateAgentRuntime echoing its request
    parameters -- the scenario these patterns exist for -- put a live provider key in
    ``error_details`` and in the CloudWatch failure log. The long-base64 rule does not
    cover it: it needs a 40-char run and ``sk-`` keys are shorter and contain ``-``.
    """

    SECRET = "sk-litellm-v1-abc123"

    @pytest.mark.parametrize(
        "text",
        [
            "Environment={'Variables': {'GATEWAY_API_KEY': 'sk-litellm-v1-abc123'}}",
            "Environment={'Variables': {'PROVIDER_API_KEY': 'sk-litellm-v1-abc123'}}",
            "provider_api_key=sk-litellm-v1-abc123",
            "GATEWAY_API_KEY: sk-litellm-v1-abc123",
            "x_client_secret: sk-litellm-v1-abc123",
            "aws_secret_access_key=sk-litellm-v1-abc123",
            "my_session_token=sk-litellm-v1-abc123",
            "db_password=sk-litellm-v1-abc123",
        ],
    )
    def test_the_value_does_not_survive(self, text):
        assert self.SECRET not in redact_secrets(text), (
            "a prefixed secret name left its value in the clear, in a string that reaches "
            "both the browser and CloudWatch"
        )

    def test_the_unprefixed_spelling_still_works(self):
        """Vacuity guard: the fix must not have been to delete the rule."""
        assert self.SECRET not in redact_secrets(f"api_key={self.SECRET}")

    def test_a_secret_REFERENCE_still_survives(self):
        """A ``*_ref`` is a pointer, not a credential, and naming it is what makes a
        failure diagnosable. The ``\\b`` after the name is what keeps ``_ref`` out: this
        pins that the fix did not become a blanket "redact anything key-shaped"."""
        out = redact_secrets("provider_api_key_ref=agentcore/connector/abc-123")
        assert "agentcore/connector/abc-123" in out, out


class TestPydanticDebugInformationIsNotReturned:
    """Measured live. ``POST /api/workflows/{id}/deploy`` on ``acfe2e-p0920`` returned this
    in the HTTP body, and therefore in the browser::

        1 validation error for RuntimeConfig Value error, Bedrock model '...' is not in
        the known-active list ... [type=value_error, input_value={'name': 'agent…

    ``[type=...]`` is "Debug information" in ARCC ``cnt_94E30Xo4RZHtSJ``'s list, and
    ``input_value=`` reflects the caller's whole payload into a message that is stored in
    DynamoDB and re-served by ``GET /api/deploy/{id}``. Only the length cap stopped the
    full config dict from being dumped -- a cap is not a control.
    """

    LIVE = (
        "1 validation error for RuntimeConfig\nmodel\n  Value error, Bedrock model "
        "'anthropic.claude-sonnet-4-5-20250929-v1:0' is not one of the models this "
        "platform supports. Choose one from the model list. "
        "[type=value_error, input_value={'name': 'agent', 'api_key': 'sk-zzz'}, input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.9/v/value_error"
    )

    @pytest.mark.parametrize("fragment", ["input_value", "input_type", "[type=", "pydantic.dev"])
    def test_the_debug_annotation_is_gone(self, fragment):
        assert fragment not in sanitize_error_details(self.LIVE)

    def test_the_reflected_input_is_gone(self):
        assert "sk-zzz" not in sanitize_error_details(self.LIVE)

    def test_the_actionable_sentence_survives(self):
        """Not a collapse to GENERIC_MESSAGE: the caller CAN fix this one, which ARCC
        explicitly allows a message to help with."""
        out = sanitize_error_details(self.LIVE)
        assert "not one of the models this platform supports" in out
        assert out != GENERIC_MESSAGE

    def test_the_truncated_form_is_handled_too(self):
        """What actually shipped live was already chopped by the length cap, so the
        closing bracket was absent. A pattern requiring ``]`` would have missed it."""
        out = sanitize_error_details(
            "Value error, Bedrock model 'x' is bad. [type=value_error, input_value={'name': 'ag…"
        )
        assert "input_value" not in out and "[type=" not in out
        assert "is bad" in out

    def test_no_orphan_fragment_is_left_behind(self):
        """Regression guard on ordering. Running ``redact_secrets`` first introduced a
        ``]`` via ``[redacted]``, which terminated the strip pattern's ``[^\\]]*`` scan
        early and left ``input_type=dict]`` in the output."""
        assert "dict]" not in sanitize_error_details(self.LIVE)


class TestAUserFacingValidatorMessageNamesNoInternals:
    """``_validate_bedrock_model_id``'s messages reach the browser by the same route. One
    named a private module constant (``_BEDROCK_ACTIVE_MODEL_SUBSTRINGS``) and the other
    cited an internal doc path (``tasks/lessons.md Bug 113``) -- neither is something the
    reader can act on, and both are internal system components per ARCC
    ``cnt_94E30Xo4RZHtSJ``."""

    @pytest.mark.parametrize("model_id", ["totally.made-up-model-v9:0", "anthropic.claude-v2"])
    def test_the_message_names_no_internal_identifier_or_path(self, model_id):
        from app.models.deployment_models import _validate_bedrock_model_id

        with pytest.raises(ValueError) as exc:
            _validate_bedrock_model_id(model_id)
        msg = str(exc.value)
        for leak in ("_BEDROCK_ACTIVE_MODEL_SUBSTRINGS", "tasks/lessons", "lessons.md", ".py"):
            assert leak not in msg, f"{leak!r} reaches the user in: {msg}"

    def test_the_message_still_suggests_something_usable(self):
        """Vacuity guard: it must not have become a bare "invalid model"."""
        from app.models.deployment_models import _validate_bedrock_model_id

        with pytest.raises(ValueError) as exc:
            _validate_bedrock_model_id("totally.made-up-model-v9:0")
        assert "claude-sonnet-5" in str(exc.value)


def _the_real_byo_lambda_refusal(function_arn: str) -> str:
    """Produce the bring-your-own-Lambda opt-in refusal by running the REAL code.

    Not a copy of the message. A copy is what let this defect exist: the message was
    asserted at the raiser (``test_gateway_mixed_targets.py``) and the cap was asserted
    at the sanitizer, both green, and the interaction between them was tested nowhere.
    Calling the production function means editing the message cannot drift away from
    this test -- it would break it.
    """
    from unittest.mock import MagicMock, patch

    from app.services import gateway_deployer as gd
    from botocore.exceptions import ClientError

    class _Conflict(Exception): ...

    class _InvalidParam(Exception): ...

    lam = MagicMock()
    # Real exception classes on ``.exceptions``: the code under test uses them in
    # ``except`` clauses, where a MagicMock attribute raises TypeError instead.
    lam.exceptions.ResourceConflictException = _Conflict
    lam.exceptions.InvalidParameterValueException = _InvalidParam
    lam.add_permission.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User: arn:aws:sts::123456789012:assumed-role/"
                    "acfe2e-p0920-StepGatewayRoleAAFE0C07-m9tHG9ZTMy3k/x is not authorized to "
                    "perform: lambda:AddPermission on resource: " + function_arn
                ),
            }
        },
        "AddPermission",
    )
    with (
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_prune_orphaned_lambda_permissions"),
        pytest.raises(ValueError) as ei,
    ):
        gd._grant_gateway_invoke_on_lambda(
            "us-east-1", function_arn, "arn:aws:iam::123456789012:role/AgentCoreGateway-gw"
        )
    # The step handler's own wrapper prefix, which is part of what has to fit the cap.
    return f"Gateway deployment failed: {ei.value}"


class TestAnActionableRemedyIsNotClippedBeforeTheRemedy:
    """Measured live, 2026-09-21, deployment ``5518b81d-6a3e-4a6c-854b-e63b973e41ef`` on
    ``acfe2e-p0920``. A real failed deploy against an untagged bring-your-own Lambda
    stored ``error_details`` at EXACTLY 300 characters, ending::

        ... must be opted in by its owner: tag the function AgentCoreGatewayTarget=allow (aws lambda…

    The tag name survived. The command that sets it, and the reason the deploy was
    stopped rather than shipping a gateway whose only tool fails at invoke time, did
    not. ARCC ``cnt_94E30Xo4RZHtSJ`` permits -- and this platform relies on -- putting
    the fix in the message when the caller can act on it; a cap that cuts the fix off
    turns that into a dead end. Same class as the clipped-reason fix in ``d9164b3``.

    The tests below pin both halves: the remedy reaches the user, and the cap and the
    redaction that share this path still do their jobs.
    """

    ARN = "arn:aws:lambda:us-east-1:123456789012:function:acf-byoe2e-4d2d0038"

    #: Lambda allows a 64-character function name; the probe's was 19. The ARN is
    #: interpolated twice, so the worst case is ~90 characters longer than the measured
    #: one -- which is why it gets its own test rather than relying on the slack.
    LONG_ARN = (
        "arn:aws:lambda:us-east-1:123456789012:function:"
        "my-customer-support-tool-function-with-a-really-long-name-01234z"
    )

    def _stored(self, arn: str | None = None) -> str:
        """What ``error_details`` really becomes: the raised message, wrapped in the
        Lambda failure envelope, handed over as a Step Functions ``Cause`` string, and
        sanitized at the store seam. Every hop the live value took."""
        cause = json.dumps(
            {
                "errorMessage": _the_real_byo_lambda_refusal(arn or self.ARN),
                "errorType": "RuntimeError",
                "requestId": "59f7f13c-08a2-4e5d-89b5-171ae66662e8",
                "stackTrace": ['  File "/var/task/src/app/step_handlers/gateway_step.py", line 305, in handler\n'],
            }
        )
        return sanitize_error_details(cause)

    def test_the_remedy_command_survives(self):
        out = self._stored()
        assert "aws lambda tag-resource" in out, (
            f"the message tells the user to tag the function but not how; len={len(out)}, ends {out[-40:]!r}"
        )

    def test_the_command_is_complete_enough_to_paste(self):
        """``aws lambda tag-resource`` alone is not a remedy: it needs the resource and
        the tag. This is what the 300-char cap actually destroyed."""
        out = self._stored()
        for fragment in ("--resource", "--tags", "AgentCoreGatewayTarget=allow", "and redeploy"):
            assert fragment in out, f"{fragment!r} missing from: {out}"

    def test_the_invoke_time_consequence_survives(self):
        """Why the deploy stopped instead of shipping a gateway with a dead tool. Without
        it the refusal reads as an arbitrary platform failure."""
        assert "at invoke time" in self._stored()

    def test_the_function_is_still_named_with_its_account_redacted(self):
        """The user must know WHICH function to tag, and must not be told the platform's
        account id to learn it. Both, in one message -- the redaction is not sacrificed
        to make room."""
        out = self._stored()
        assert self.ARN.replace("123456789012", "[redacted-account]") in out
        assert "123456789012" not in out

    def test_the_platform_principal_is_still_gone(self):
        """The underlying denial names the step role by its CloudFormation logical id.
        Fitting a longer message must not have widened what passes through."""
        out = self._stored()
        for leak in ("assumed-role", "StepGatewayRole", "arn:aws:sts::"):
            assert leak not in out, f"{leak!r} reaches the user in: {out}"

    def test_no_trace_or_request_id_rides_along_on_the_longer_message(self):
        out = self._stored()
        for leak in ("/var/task/", "stackTrace", "59f7f13c", "gateway_step.py"):
            assert leak not in out

    def test_the_whole_message_fits_the_cap_unclipped(self):
        out = self._stored()
        assert len(out) <= MAX_LENGTH
        assert "…" not in out, f"still truncated at {len(out)} of {MAX_LENGTH}"

    def test_it_still_fits_with_a_maximum_length_function_name(self):
        """The measured message is 616 characters for a 19-character function name, which
        leaves the test above ~184 characters of slack -- enough that a future added
        sentence would keep it green and still clip for a real customer whose function
        has a long name. At the 64-character maximum the message is 706 characters."""
        out = self._stored(self.LONG_ARN)
        assert "…" not in out, f"clipped at {len(out)} of {MAX_LENGTH}"
        assert "aws lambda tag-resource" in out and "at invoke time" in out
        assert self.LONG_ARN.replace("123456789012", "[redacted-account]") in out

    def test_the_cap_still_truncates_something_unrecognised(self):
        """Vacuity guard in the other direction: the fix must not have been to delete the
        cap. An unbounded paste is still bounded."""
        # Spaces are deliberate: an unbroken run of 40+ base64 characters is redacted to
        # ``[redacted]`` by the long-secret rule, so ``"x" * 3200`` measures that rule
        # instead of the cap and the assertion below would pass for the wrong reason.
        out = sanitize_error_details("A failure. " + "unstructured paste. " * (MAX_LENGTH // 4))
        assert len(out) <= MAX_LENGTH
        assert out.endswith("…")

    def test_the_cap_is_still_a_cap(self):
        """And not raised to a number that bounds nothing. 800 is derived from the
        longest message this platform authors (616 characters redacted, with the
        function ARN twice and a name that may be 64 characters), not chosen to make a
        test pass."""
        assert 700 <= MAX_LENGTH <= 1000


def mod_path() -> str:
    from app.services import deployment_state_store

    return deployment_state_store.__file__


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
