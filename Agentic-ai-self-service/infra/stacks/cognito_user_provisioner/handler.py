"""CloudFormation Custom Resource handler for provisioning Cognito users.

Generates a temporary password from an HTML-safe character set, then calls
AdminCreateUser so Cognito emails the invitation containing that password.
The user lands in FORCE_CHANGE_PASSWORD and must set a new password on
first sign-in (unchanged UX).

Why a custom resource and not AWS::Cognito::UserPoolUser?
  The CloudFormation resource does not expose `TemporaryPassword`, so
  Cognito auto-generates the password. Cognito's generator is allowed to
  emit `<`, `>`, `&`, `'`, `"` as symbols, and the default invitation
  email is HTML with the password interpolated as *raw* text (not escaped).
  Email clients then silently strip any sequence that parses as a tag,
  so the rendered password does not match the stored verifier and every
  sign-in fails with ChallengeResponse=Failure, NoRisk.

  By generating the password ourselves from a safe charset, the password
  Cognito stores is always equal to the password rendered in the email.
"""

from __future__ import annotations

import json
import secrets
import string
import urllib.request

import boto3
from botocore.exceptions import ClientError

cognito = boto3.client("cognito-idp")

# Excludes HTML-special chars (< > & " ') and sentence-punctuation chars (. ,)
# that collide with the default invitation template. All remaining symbols
# are recognized as symbols by Cognito's password policy.
SAFE_SYMBOLS = "!#$%^*_-+="
PASSWORD_LENGTH = 16


def _generate_password() -> str:
    """Generate a password that satisfies a policy requiring upper, lower, digit, symbol."""
    alphabet = string.ascii_letters + string.digits + SAFE_SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in SAFE_SYMBOLS for c in pw)
        ):
            return pw


def _create_user(user_pool_id: str, email: str, temporary_password: str) -> None:
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=email,
            TemporaryPassword=temporary_password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "UsernameExistsException":
            raise
        # Idempotency: if the stack is re-deployed and the user already exists,
        # reset the temporary password so the new email contains a valid one.
        cognito.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=email,
            Password=temporary_password,
            Permanent=False,
        )


def _delete_user(user_pool_id: str, email: str) -> None:
    try:
        cognito.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("UserNotFoundException", "ResourceNotFoundException"):
            raise


def _send_cfn_response(event: dict, context, status: str, physical_id: str, reason: str = "") -> None:
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch logs: {context.log_group_name}/{context.log_stream_name}",
            "PhysicalResourceId": physical_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": True,
            "Data": {},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        resp.read()


def handler(event: dict, context) -> None:
    props = event.get("ResourceProperties", {})
    user_pool_id = props["UserPoolId"]
    email = props["Email"]
    physical_id = f"{user_pool_id}:{email}"

    try:
        if event["RequestType"] in ("Create", "Update"):
            _create_user(user_pool_id, email, _generate_password())
        elif event["RequestType"] == "Delete":
            _delete_user(user_pool_id, email)
        else:
            _send_cfn_response(event, context, "FAILED", physical_id, reason=f"Unknown RequestType: {event['RequestType']}")
            return

        _send_cfn_response(event, context, "SUCCESS", physical_id)
    except Exception as exc:  # noqa: BLE001
        _send_cfn_response(event, context, "FAILED", physical_id, reason=str(exc)[:1000])
        raise
