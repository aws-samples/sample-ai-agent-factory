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

import secrets
import string

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


def _add_to_groups(user_pool_id: str, email: str, groups: list[str]) -> None:
    """Grant the user its groups. Without one, an enforcing API (RBAC_ENFORCE)
    gives the user zero scopes and 403s every call. AdminAddUserToGroup is
    idempotent, so an existing membership is not an error."""
    for group in groups:
        cognito.admin_add_user_to_group(UserPoolId=user_pool_id, Username=email, GroupName=group)


def _delete_user(user_pool_id: str, email: str) -> None:
    try:
        cognito.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("UserNotFoundException", "ResourceNotFoundException"):
            raise


def handler(event: dict, context) -> dict:
    """The onEvent handler of a CDK Provider (cognito_auth.py), which owns the reply.

    The framework invokes this function with the real ResponseURL and then submits
    its own response built from the return value. A handler that also PUT to the
    URL sent two replies with different physical ids; which one CloudFormation took
    decided whether an Update was a replacement, whose Delete removes the user.
    Return the id and raise on failure; never reply directly.
    """
    props = event.get("ResourceProperties", {})
    user_pool_id = props["UserPoolId"]
    email = props["Email"]
    groups = [g for g in props.get("Groups", []) if g]
    old = event.get("OldResourceProperties", {})
    request = event["RequestType"]

    if request == "Update" and (old.get("UserPoolId"), old.get("Email")) == (user_pool_id, email):
        # Same user, other properties changed (its groups). Re-running the create
        # would reset a live user's password to a new temporary one. The id is kept:
        # a changed id is a replacement, and its cleanup Delete removes this user.
        _add_to_groups(user_pool_id, email, groups)
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    if request in ("Create", "Update"):
        # A new pool or email is a new user: a new id makes CloudFormation delete
        # the old one once this succeeds.
        _create_user(user_pool_id, email, _generate_password())
        _add_to_groups(user_pool_id, email, groups)
        return {"PhysicalResourceId": f"{user_pool_id}:{email}"}
    if request == "Delete":
        _delete_user(user_pool_id, email)
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    raise ValueError(f"Unknown RequestType: {request}")
