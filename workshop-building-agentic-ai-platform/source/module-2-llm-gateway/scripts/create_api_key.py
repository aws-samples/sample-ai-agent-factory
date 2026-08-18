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
import tempfile

import boto3
import requests


def _mask(secret: str) -> str:
    """Confirm a key exists without revealing any of its bytes.

    The caller prints the key name alongside, so no leading/trailing characters
    are needed to recognise it — the length alone separates a real key from a
    short or empty lookup failure.
    """
    if not secret:
        return "(not created)"
    if len(secret) <= 12:
        return "(unexpectedly short — check the proxy response)"
    return f"(value not printed — {len(secret)} chars)"


def _write_env_file(key_name: str, proxy_url: str, virtual_key: str) -> str:
    """Persist the new key to a 0600 file instead of printing it.

    A virtual key printed to stdout lands in terminal scrollback, shell history
    and any captured session log. Writing it to a mode-0600 file the participant
    sources keeps it out of all three and survives opening a second terminal.
    Mirrors setup_keys.py, which writes /workshop/.llm-gateway-env the same way.
    Returns the path written, or "" if nowhere was writable.
    """
    lines = [
        f"# Written by create_api_key.py for virtual key '{key_name}'.",
        f"export LLM_GATEWAY_URL={proxy_url}",
        f"export LLM_GATEWAY_API_KEY={virtual_key}",
        "",
    ]
    filename = f".llm-gateway-{key_name}-env"
    for base in ("/workshop", os.path.expanduser("~"), tempfile.gettempdir()):
        path = os.path.join(base, filename)
        try:
            with open(path, "w") as f:
                f.write("\n".join(lines))
            os.chmod(path, 0o600)  # contains a live virtual key
            return path
        except OSError:
            continue
    return ""


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
    env_path = _write_env_file(args.key_name, proxy_url, virtual_key)

    print(f"Virtual Key: {_mask(virtual_key)}  (name={args.key_name}, "
          f"budget=${args.max_budget})")
    print()
    if env_path:
        print("Load it into any terminal with:")
        print(f"  source {env_path}")
        print()
        print(f"{env_path} is mode 0600 and holds the full value.")
    else:
        # Nowhere was writable, so the key exists but was not saved. Re-running
        # mints another key, which is cheaper than putting this one in the
        # scrollback for good.
        print("Could not write the env file to /workshop, $HOME or the temp")
        print("directory, so this key was created but not saved. Fix the")
        print("writable path and re-run to mint a replacement key.")


if __name__ == "__main__":
    main()
