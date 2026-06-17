#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create a virtual key in LiteLLM Proxy.

Usage:
    python scripts/create_api_key.py --proxy-url https://... --admin-key sk-...
    python scripts/create_api_key.py --stack-name workshop-llm-gateway-stack

    Or using environment variables (always use the HTTPS gateway endpoint):
        export LLM_GATEWAY_URL=https://<api-gateway-endpoint>
        export LLM_GATEWAY_ADMIN_KEY=sk-...
        python scripts/create_api_key.py
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3
import requests


def get_from_stack(stack_name: str, region: str) -> tuple[str, str]:
    """Get proxy URL and admin key from CloudFormation stack."""
    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in resp["Stacks"][0].get("Outputs", [])
    }
    proxy_url = outputs.get("ProxyUrl", "").rstrip("/")
    secret_arn = outputs.get("AdminKeySecretArn", "")
    if not proxy_url or not secret_arn:
        return proxy_url, ""
    sm = boto3.client("secretsmanager", region_name=region)
    raw_secret = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    # Note: LITELLM_MASTER_KEY is the upstream LiteLLM env var name; our wrapper uses ADMIN_KEY terminology
    # The CFN template prepends "sk-" to the secret value for LITELLM_MASTER_KEY
    admin_key = f"sk-{raw_secret}" if not raw_secret.startswith("sk-") else raw_secret
    return proxy_url, admin_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a LiteLLM virtual key")
    parser.add_argument("--stack-name", default="workshop-llm-gateway-stack")
    parser.add_argument("--region", default=boto3.session.Session().region_name or "us-west-2")
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get("LLM_GATEWAY_URL", ""),
    )
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("LLM_GATEWAY_ADMIN_KEY", ""),
    )
    parser.add_argument("--key-name", default="workshop-key", help="Name for the virtual key")
    parser.add_argument("--max-budget", type=float, default=5.0, help="Budget in USD")
    parser.add_argument("--team-id", default="", help="Team ID to assign the key to")
    args = parser.parse_args()

    proxy_url = args.proxy_url
    admin_key = args.admin_key

    if not proxy_url or not admin_key:
        proxy_url, admin_key = get_from_stack(args.stack_name, args.region)

    if not proxy_url:
        print("ERROR: Could not determine proxy URL. Provide --proxy-url or --stack-name.")
        sys.exit(1)
    if not admin_key:
        print("ERROR: Could not determine admin key. Provide --admin-key or --stack-name.")
        sys.exit(1)

    payload: dict = {
        "key_name": args.key_name,
        "max_budget": args.max_budget,
        # Model IDs for Amazon Bedrock models accessed through LiteLLMs Bedrock integration; the LiteLLM proxy routes these to Bedrock.
        "models": ["claude-sonnet", "claude-haiku", "nova-2-lite"],
    }
    if args.team_id:
        payload["team_id"] = args.team_id

    resp = requests.post(
        f"{proxy_url}/key/generate",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_key}",
        },
        timeout=10,
    )
    resp.raise_for_status()
    key_data = resp.json()

    virtual_key = key_data.get("key", "")
    # NOTE: printing credentials is acceptable only in this ephemeral workshop
    # sandbox (the participant needs to copy their own freshly-created,
    # budget-scoped key); never do this in production.
    print(f"Virtual Key: {virtual_key}")
    print()
    print("Export for use:")
    print(f"  export LLM_GATEWAY_API_KEY={virtual_key}")
    print(f"  export LLM_GATEWAY_URL={proxy_url}")


if __name__ == "__main__":
    main()
