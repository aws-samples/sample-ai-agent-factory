"""Shared synth + statement helpers for the P1 IAM tests (F-01, F-03..F-07). Not a test module.

Statements are flattened to TEXT for resource matching because a resource with the account
token is an ``Fn::Join``/``Fn::Sub`` structure; a parser that misread one shape would silently
match nothing and pass. Conditions are kept structured because the assertions here are about
exact keys and values.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
ENVIRONMENT = "test"


def synth(stack_id: str, context: dict | None = None) -> dict:
    app = cdk.App(context=context or {})
    stack = PlatformStack(
        app,
        stack_id,
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def actions(st: dict) -> list[str]:
    a = st.get("Action")
    return [a] if isinstance(a, str) else list(a or [])


def resources_text(st: dict) -> list[str]:
    r = st.get("Resource")
    r = [r] if not isinstance(r, list) else r
    return [x if isinstance(x, str) else json.dumps(x) for x in r]


def all_statements(tpl: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for lid, res in tpl["Resources"].items():
        if res["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            out.append((lid, st))
    return out


def lambda_roles(tpl: dict) -> list[str]:
    """Logical ids of every role Lambda can assume -- the platform's own execution roles."""
    out = []
    for lid, res in tpl["Resources"].items():
        if res["Type"] != "AWS::IAM::Role":
            continue
        for st in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if st.get("Principal", {}).get("Service") == "lambda.amazonaws.com":
                out.append(lid)
                break
    return out
