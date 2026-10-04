"""Turn a raw Lambda/Step Functions failure into something safe to show a user.

WHY, measured against the live state machine. When a step Lambda raises, Step
Functions hands the Catch branch an ``error_info`` whose ``Cause`` is the Lambda
runtime's own JSON failure envelope. From execution
``deploy-db55fe3e-...`` on ``acfe2e-p0920-deployment``, verbatim::

    {"errorMessage": "Knowledge Base ZZZZZZZZZZ not found",
     "errorType": "ValueError",
     "requestId": "59f7f13c-08a2-4e5d-89b5-171ae66662e8",
     "stackTrace": ["  File \\"/var/task/src/app/step_handlers/knowledge_base_step.py\\",
                     line 856, in handler\\n    raise ValueError(...) from None\\n"]}

That entire blob was stored as ``error_details`` on the deployment item, returned by
``GET /api/deploy/{id}``, and thrown as the browser's ``Error`` message
(``useDeployment.ts:235``). So the UI showed the user an absolute path inside our
Lambda, the module and line number that raised, the source line itself, and a request
id.

ARCC ``cnt_94E30Xo4RZHtSJ`` is directly on point: "Return generic error messages that
does not include details such as: Internal system components, Stack traces, Debug
information, Memory dumps, Network timeouts, Input mismatch." It also allows the part
that makes this usable -- "If the caller has the ability to fix the issue, you can
include simple solutions in the message" -- which is why this module keeps
``errorMessage`` rather than replacing everything with "Deployment failed". Those
messages are the ones an operator acts on ("Knowledge Base X not found").

Related: ``cnt_Yq9sVcaZyQniIv`` (do not return internal architecture or debug
details), ``cnt_SaTYaDCgBBJTcv`` and ``cnt_rHmO501l15qr2W`` (do not emit credentials
or secret-bearing exception text in messages or logs), ``cnt_dTnYSrLtxU6kyd`` (no
stack traces in model/system-facing errors).

WHAT THIS DOES NOT DO. It is not an output encoder. ARCC warns against reflecting
user-provided input because a downstream consumer may render it as code; the
``errorMessage`` above contains a user-supplied id. React escapes text nodes, so the
UI is not the risk -- but that is the renderer's guarantee, not this function's, so
callers must keep treating the result as untrusted text.
"""

from __future__ import annotations

import json
import re

#: What the user sees when nothing safe could be extracted. Deliberately not
#: "unknown error": it tells the operator where to look without describing internals.
GENERIC_MESSAGE = "Deployment failed. See the deployment's step history for the failing step."

#: Hard cap. A long message is a paste of something structured, and truncating is a
#: cheap second line of defence against a shape this module does not recognise.
#:
#: 800, not 300, and the difference is a defect measured live. ARCC ``cnt_94E30Xo4RZHtSJ``
#: permits a message to carry the fix when the caller can act on it, and this platform
#: writes such messages -- but they are long, because a remedy is an ARN plus a CLI call
#: plus the consequence of not doing it. On deployment ``5518b81d-6a3e-4a6c-854b-e63b973e41ef``
#: (``acfe2e-p0920``, 2026-09-21) the bring-your-own-Lambda opt-in error was stored at
#: EXACTLY 300 characters, ending ``...tag the function AgentCoreGatewayTarget=allow (aws
#: lambda`` + the ellipsis. The tag name survived; the command that sets it and the reason
#: the deploy stopped did not. A message engineered to be actionable arrived without its
#: action. Same class as the clipped-reason fix in ``d9164b3``.
#:
#: 800 is measured, not guessed: that message is 616 characters once the account id is
#: redacted, it interpolates the function ARN twice, and a Lambda function name may be 64
#: characters (the probe's was 19), so its own worst case is ~706. 800 covers it with
#: headroom and still bounds an unrecognised paste.
#:
#: Raising it does not weaken a control, because the cap is not one. Everything that
#: actually keeps content out is absolute and runs first: the allow-list in
#: :func:`_from_envelope` (only message-shaped fields are carried forward at all),
#: :data:`_FORBIDDEN_KEYS` and the ``/var/task/`` check (which REFUSE the whole message
#: rather than trimming it), :func:`redact_secrets`, :func:`_redact_principals` and
#: :func:`_strip_pydantic_debug`. Truncation only bounds how much of an already-filtered
#: string is shown.
MAX_LENGTH = 800

#: Keys in a Lambda failure envelope that must never reach the caller.
_FORBIDDEN_KEYS = ("stackTrace", "stack_trace", "requestId", "request_id", "trace")

# Secret-shaped substrings. This is defence in depth, not the primary control (the
# primary control is not putting secrets in exceptions). A botocore error can echo the
# request parameters that produced it, which is how a Cognito client secret or a
# bearer token reaches an exception message in the first place.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # AWS access key ids and session-token-shaped blobs.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted-access-key-id]"),
    # An Authorization header, INCLUDING its auth scheme. This must run before the
    # generic named-secret pattern below, and it exists because that pattern got this
    # wrong: its `\S+` value matcher stopped at the first space, so it consumed the
    # SCHEME and left the credential.
    #
    #   redact_secrets("Authorization: Bearer abc.def")
    #     -> "Authorization=[redacted] abc.def"     # the token survived
    #
    # The long-base64 rule below did not save it either: that needs a 40-char run, and
    # plenty of real tokens are shorter or contain '.', '-' and '_' (a JWT's dots break
    # the run into segments). So a short bearer token reached the stored error_details
    # and the CloudWatch failure log in full.
    #
    # The scheme is optional so a bare `Authorization: <token>` is still caught: the
    # alternation is tried first, and when it does not match, the token matcher takes
    # the whole value. Quotes are excluded from the token class so a quoted value has
    # its credential removed rather than the match being thrown off by the quote.
    (
        re.compile(
            r"(?i)\b(authorization)\b\s*[:=]\s*[\"']?"
            r"(?:(?:Bearer|Basic|Digest|Token|Negotiate|OAuth)\s+)?"
            r"[^\s\"',;]+[\"']?"
        ),
        r"\1=[redacted]",
    ),
    # A bearer/basic credential presented WITHOUT an Authorization key -- e.g. inside a
    # botocore message that echoes a header value, or prose like "using Bearer <tok>".
    (
        re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{4,}={0,2}"),
        "[redacted]",
    ),
    # Long base64/hex runs: client secrets, session tokens, signatures.
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[redacted]"),
    # Anything else that names itself a secret and then gives a value. `authorization`
    # and `bearer` stay in this alternation as a backstop for shapes the two rules above
    # do not match; they are already handled, so this is idempotent on them.
    #
    # The leading `[a-z0-9_]*` is load-bearing and was the defect. The original pattern
    # began `\b(api_?key|...)`, and `_` is a word character, so there is NO word boundary
    # before `api` in `PROVIDER_API_KEY` -- the rule matched a bare `api_key=` and missed
    # every prefixed spelling. Measured:
    #
    #   redact_secrets("Environment={'Variables': {'GATEWAY_API_KEY': 'sk-litellm-v1-abc123'}}")
    #     -> unchanged; the key survived in full
    #
    # Those two names are not hypothetical: ``PROVIDER_API_KEY`` and ``GATEWAY_API_KEY``
    # are the env vars this platform sets on a runtime, so a botocore error from
    # Create/UpdateAgentRuntime that echoes its request parameters -- the exact scenario
    # the comment above says these patterns exist for -- carried a live provider key into
    # ``error_details`` (DynamoDB, then ``GET /api/deploy/{id}``, then the browser) and
    # into the CloudWatch failure log. The long-base64 rule does not cover it: it needs a
    # 40-char run, and `sk-`-prefixed keys are shorter and contain `-`.
    #
    # A trailing wildcard is deliberately NOT added, so `provider_api_key_ref` still
    # escapes: the `\b` after the name fails against the `_` of `_ref`. A secret
    # *reference* is not a secret and naming it is what makes a failure diagnosable.
    (
        re.compile(
            r"(?i)\b([a-z0-9_]*"
            r"(?:client_?secret|secret_?access_?key|session_?token|password|api_?key|authorization|bearer)"
            r")\b[\"']?\s*[:=]\s*\S+"
        ),
        r"\1=[redacted]",
    ),
    # Pre-signed URLs carry credentials in the query string (ARCC cnt_rHmO501l15qr2W).
    (re.compile(r"(?i)(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=[^&\s\"']+"), r"\1=[redacted]"),
)


def redact_secrets(text: str) -> str:
    """Replace secret-shaped substrings in *text*.

    Applied to the user-facing message AND used by callers that log the full failure,
    because CloudWatch being access-controlled does not make a credential in a log
    acceptable (ARCC ``cnt_rHmO501l15qr2W``).
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_error_details(raw: object) -> str:
    """Return a user-safe one-line description of a failure.

    Accepts whatever the failure paths actually produce: a Lambda failure envelope
    (JSON string or already-parsed dict), a Step Functions ``error_info`` dict with
    ``Error``/``Cause``, a plain exception string, or ``None``.

    The rule is allow-list, not deny-list: only ``errorMessage``/``Error``-shaped
    fields are carried forward. A shape this function does not recognise collapses to
    ``GENERIC_MESSAGE`` rather than being passed through, so a future error envelope
    with a new debug field cannot leak by default.
    """
    if raw is None:
        return GENERIC_MESSAGE

    payload: object = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return GENERIC_MESSAGE
        # A Cause is usually a JSON string. If it parses to a dict, treat it as the
        # envelope; otherwise it is already a plain message.
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                # Malformed JSON: do NOT fall through to returning the raw text, which
                # is exactly the stackTrace-bearing blob. A truncated envelope is
                # still an envelope.
                return GENERIC_MESSAGE
        else:
            return _finish(text)

    if isinstance(payload, dict):
        return _finish(_from_envelope(payload))

    return GENERIC_MESSAGE


def _from_envelope(envelope: dict) -> str:
    """Pull the one safe field out of a failure envelope, preferring the message."""
    # Step Functions Catch shape: {"Error": "ValueError", "Cause": "<json string>"}.
    # Recurse into Cause first -- it holds the message; Error is only the class name.
    cause = envelope.get("Cause")
    if isinstance(cause, (str, dict)) and cause:
        inner = sanitize_error_details(cause)
        if inner != GENERIC_MESSAGE:
            return inner

    for key in ("errorMessage", "error_message", "message", "Message"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # No message, but a type name is still more actionable than nothing -- and a bare
    # exception class name discloses far less than a trace. Only used as a fallback.
    for key in ("errorType", "Error", "error"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return GENERIC_MESSAGE


#: The IAM/STS principal an AWS call was made AS, plus the account it ran in. These are
#: redacted on the user-facing path only (see :func:`_finish`), never in
#: :func:`redact_secrets`, which also feeds the CloudWatch failure log where the real
#: principal is the whole debugging value.
#:
#: Measured: ``POST /api/workflows/{id}/deploy`` against ``acfe2e-p0920`` returned
#: ``User: arn:aws:sts::166827918465:assumed-role/acfe2e-p0920-WorkflowLambdaRole80E0B348-aT0dkiL4sxj2/...
#: is not authorized to perform: iam:CreateRole`` to any ``agent:write`` caller. That
#: names the platform's account, its role-naming scheme, and the CloudFormation logical
#: id the role was minted from -- "Internal system components" in ARCC
#: ``cnt_94E30Xo4RZHtSJ``'s list of things a message must not contain. Every principal
#: that makes an AWS call in this codebase is a platform role, never the caller's own,
#: so there is no case where disclosing it helps them fix anything.
#:
#: Deliberately NOT a blanket "redact every ARN": a resource ARN often names something
#: the caller owns and asked about (``Gateway arn:...:gateway/x not found``), which is
#: the actionable half ARCC explicitly permits keeping.
_PRINCIPAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"arn:aws[a-z-]*:(?:sts|iam)::\d{12}:[^\s\"']+"), "[redacted-principal]"),
    # A bare account id, e.g. from a message that names the account without an ARN.
    # Runs after the ARN rule so it does not chop an ARN into an unrecognisable stub.
    #
    # The lookbehind excludes a UUID's final group, which is also 12 characters. Measured,
    # on a live ``GET /api/deploy/{id}``: a plain ``\b\d{12}\b`` turned
    # ``Deployment 'a3f19c22-7b41-4de8-9c02-481920374615' not found`` into
    # ``...-9c02-[redacted-account]' not found``, so the caller could not match the message
    # against the id they had just requested. Not a leak -- over-redaction -- but it happens
    # to **0.355%** of uuid4s (measured over 200k, ~1 in 281), which is often enough to
    # matter and far too rare to notice by hand.
    #
    # Deliberately a 5-character fixed lookbehind (``xxxx-``) rather than excluding every
    # hyphen-adjacent run: a bucket name like
    # ``acfe2e-p0920-frontend-us-east-1-166827918465`` ends in the account id after a
    # hyphen, and that one must still be redacted. Verified against both, plus the
    # principal ARN and the resource ARN above.
    (re.compile(r"(?<![0-9a-fA-F]{4}-)\b\d{12}\b"), "[redacted-account]"),
)


#: Pydantic's debug annotation on a ValidationError's string form. Stripped because it is
#: "Debug information" in ARCC ``cnt_94E30Xo4RZHtSJ``'s list, and because ``input_value=``
#: reflects the caller's own payload back into a stored, re-served message.
#:
#: Measured live. ``POST /api/workflows/{id}/deploy`` on ``acfe2e-p0920`` returned, in the
#: HTTP body and therefore in the browser::
#:
#:   1 validation error for RuntimeConfig Value error, Bedrock model '...' is not in the
#:   known-active list and may have been decommissioned. ...
#:   [type=value_error, input_value={'name': 'agent…
#:
#: Only the length cap stopped ``input_value=`` from dumping the whole config dict, and a
#: cap is not a control -- which is why this strip exists and why :data:`MAX_LENGTH` could
#: then be raised to stop clipping actionable prose. The actionable sentence is kept; the
#: annotation and the docs URL carry nothing a caller can act on.
_PYDANTIC_DEBUG_PATTERNS: tuple[re.Pattern[str], ...] = (
    # The trailing `[type=..., input_value=..., input_type=...]`. Non-greedy, and tolerant
    # of the cap having already chopped the closing bracket off.
    re.compile(r"\s*\[type=[^\]]*?(?:input_value=|input_type=)[^\]]*(?:\]|$)"),
    re.compile(r"\s*\[type=[a-z_]+,\s*input_value=.*$"),
    re.compile(r"\s*For further information visit https://errors\.pydantic\.dev\S*"),
)


def _strip_pydantic_debug(text: str) -> str:
    """Remove pydantic's ``[type=..., input_value=...]`` annotation and its docs URL."""
    for pattern in _PYDANTIC_DEBUG_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


def _redact_principals(text: str) -> str:
    """Remove the platform's own IAM identity and account id from a user-facing message."""
    for pattern, replacement in _PRINCIPAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _finish(message: str) -> str:
    """Redact, strip newlines, enforce the length cap, and never return empty."""
    # FIRST, before redaction. Order matters twice over. Removing the reflected input
    # wholesale is strictly safer than redacting secrets inside it, and running
    # `redact_secrets` first actively broke the strip: its `[redacted]` replacement
    # introduces a `]`, which terminated the `[^\]]*` scan early and left an orphaned
    # `input_type=dict]` fragment behind. Measured on the live message.
    message = _strip_pydantic_debug(message)
    message = redact_secrets(message)
    message = _redact_principals(message)
    # A newline means a trace or a multi-part dump; collapse so the UI shows one line
    # and so a smuggled second line cannot masquerade as separate output.
    message = " ".join(message.split())
    # Belt and braces: if any forbidden key name survived (e.g. the message itself
    # embedded a serialized envelope), refuse the whole thing rather than trim it.
    if any(key in message for key in _FORBIDDEN_KEYS):
        return GENERIC_MESSAGE
    # An absolute path inside the Lambda is an internal system component either way.
    if "/var/task/" in message or 'File "' in message:
        return GENERIC_MESSAGE
    if len(message) > MAX_LENGTH:
        message = message[: MAX_LENGTH - 1].rstrip() + "\u2026"
    return message or GENERIC_MESSAGE
