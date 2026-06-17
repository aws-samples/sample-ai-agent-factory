#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Set up LiteLLM virtual keys and teams for the workshop.

This script:
1. Reads the proxy URL and admin key from CloudFormation / Secrets Manager
2. Registers Bedrock models in LiteLLM's database (STORE_MODEL_IN_DB=True)
3. Creates workshop teams (platform-team, workload-team)
4. Creates virtual keys with budgets for each team
5. Verifies with a test chat completion

Usage:
    python scripts/setup_keys.py [--stack-name workshop-llm-gateway-stack] [--region <aws-region>]
    python scripts/setup_keys.py --cognito-pool-id us-west-2_AbCdEf
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import boto3
import requests

import bedrock_region


def get_stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    """Read CloudFormation stack outputs into a dict."""
    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def get_admin_key(secret_arn: str, region: str) -> str:
    """Retrieve the LiteLLM admin key from Secrets Manager.

    Note: LITELLM_MASTER_KEY is the upstream LiteLLM env var name; our wrapper uses ADMIN_KEY terminology
    """
    sm = boto3.client("secretsmanager", region_name=region)
    resp = sm.get_secret_value(SecretId=secret_arn)
    return resp["SecretString"]



# Core workshop models to register via /model/new API.
# The full 70-model catalog is in cfn/litellm-config.yaml for reference.
#
# This catalog is REGION-INDEPENDENT. Each entry is:
#   (alias, bare_suffix, needs_profile, prefer_global)
# where ``bare_suffix`` carries NO geo/global prefix and NO ``bedrock/`` segment.
# The concrete LiteLLM model string is built at registration time via
# ``bedrock_region.model_id(suffix, region, needs_profile, prefer_global)`` so a
# deploy in any region resolves the correct identifier.
#
# Selection rules (see bedrock_region.py + region_decisions.md):
#   - Claude 4.x, Nova-2, and any model with a ``global.`` profile -> prefer_global=True
#     (region-agnostic, zero geo derivation).
#   - Older Nova v1 / Llama / Mistral-large / DeepSeek -> needs_profile=True,
#     prefer_global=False (geo prefix derived from the deploy region).
#   - Bare foundation models (titan, cohere, ai21) -> needs_profile=False (no prefix).
WORKSHOP_MODELS = [
    # Anthropic Claude 4.x — global. inference profile (region-agnostic)
    ("claude-opus", "anthropic.claude-opus-4-6-v1", True, True),
    ("claude-sonnet", "anthropic.claude-sonnet-4-6", True, True),
    ("claude-haiku", "anthropic.claude-haiku-4-5-20251001-v1:0", True, True),
    ("claude-opus-4.6", "anthropic.claude-opus-4-6-v1", True, True),
    ("claude-sonnet-4.6", "anthropic.claude-sonnet-4-6", True, True),
    ("claude-opus-4.5", "anthropic.claude-opus-4-5-20251101-v1:0", True, True),
    ("claude-sonnet-4.5", "anthropic.claude-sonnet-4-5-20250929-v1:0", True, True),
    ("claude-haiku-4.5", "anthropic.claude-haiku-4-5-20251001-v1:0", True, True),
    ("claude-opus-4.1", "anthropic.claude-opus-4-1-20250805-v1:0", True, True),
    ("claude-sonnet-4", "anthropic.claude-sonnet-4-20250514-v1:0", True, True),
    ("claude-3.5-haiku", "anthropic.claude-3-5-haiku-20241022-v1:0", True, True),
    # Amazon Nova — Nova-2 uses global.; older Nova v1 uses geo-scoped profiles
    ("nova-premier", "amazon.nova-premier-v1:0", True, False),
    ("nova-pro", "amazon.nova-pro-v1:0", True, False),
    ("nova-lite", "amazon.nova-lite-v1:0", True, False),
    ("nova-2-lite", "amazon.nova-2-lite-v1:0", True, True),
    # Meta Llama — geo-scoped inference profiles (no global. profile)
    ("llama3.3-70b", "meta.llama3-3-70b-instruct-v1:0", True, False),
    ("llama3.1-70b", "meta.llama3-1-70b-instruct-v1:0", True, False),
    # Mistral — large needs a geo-scoped profile; large-2407 is a bare model
    ("mistral-large-3", "mistral.mistral-large-3-675b-instruct", True, False),
    ("mistral-large", "mistral.mistral-large-2407-v1:0", False, False),
    # Cohere — bare foundation models (no prefix)
    ("cohere-command-r-plus", "cohere.command-r-plus-v1:0", False, False),
    ("cohere-command-r", "cohere.command-r-v1:0", False, False),
    # DeepSeek — geo-scoped inference profiles
    ("deepseek-r1", "deepseek.r1-v1:0", True, False),
    ("deepseek-v3", "deepseek.v3-v1:0", True, False),
]


def _delete_existing_models(proxy_url: str, headers: dict) -> int:
    """Delete all existing model entries from LiteLLM's database."""
    deleted = 0
    try:
        resp = requests.get(
            f"{proxy_url}/model/info",
            headers=headers,
            timeout=10,
        )
        if not resp.ok:
            return 0
        models = resp.json().get("data", [])
        for model in models:
            model_id = model.get("model_info", {}).get("id", "")
            if model_id:
                requests.post(
                    f"{proxy_url}/model/delete",
                    json={"id": model_id},
                    headers=headers,
                    timeout=10,
                )
                deleted += 1
    except Exception:
        pass
    return deleted


def register_models(proxy_url: str, headers: dict, region: str) -> int:
    """Register Bedrock models in LiteLLM's database via /model/new API.

    Deletes all existing model entries first, so that model ID updates
    (e.g. switching to inference profile IDs) are applied cleanly.
    """
    deleted = _delete_existing_models(proxy_url, headers)
    if deleted:
        print(f"    Cleaned up {deleted} existing model entries.")

    registered = 0
    for model_name, suffix, needs_profile, prefer_global in WORKSHOP_MODELS:
        # Build the region-correct LiteLLM model id at registration time.
        litellm_model = bedrock_region.model_id(
            suffix, region, needs_profile=needs_profile, prefer_global=prefer_global
        )
        try:
            resp = requests.post(
                f"{proxy_url}/model/new",
                json={
                    "model_name": model_name,
                    "litellm_params": {
                        "model": litellm_model,
                        "aws_region_name": region,
                    },
                },
                headers=headers,
                timeout=10,
            )
            if resp.ok:
                registered += 1
            elif resp.status_code == 400 and "already exists" in resp.text.lower():
                registered += 1  # Already registered, count as success
            else:
                print(f"    Warning: Failed to register {model_name}: {resp.status_code}")
        except Exception as e:
            print(f"    Warning: Failed to register {model_name}: {e}")
    return registered


def wait_for_proxy(proxy_url: str, retries: int = 20, interval: int = 5) -> bool:
    """Poll the LiteLLM health endpoint until it is ready."""
    health_url = f"{proxy_url}/health/liveliness"
    for i in range(1, retries + 1):
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.ok:
                return True
        except requests.ConnectionError:
            pass
        print(f"  Waiting for proxy... ({i}/{retries})")
        time.sleep(interval)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up LiteLLM virtual keys and teams")
    parser.add_argument("--stack-name", default="workshop-llm-gateway-stack")
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region. Defaults to the boto3 session region "
        "(AWS_REGION / AWS_DEFAULT_REGION / `aws configure get region`).",
    )
    parser.add_argument(
        "--cognito-pool-id",
        default="",
        help="Cognito User Pool ID for identity mapping. Auto-detected from "
        "platform-identity or Tools Gateway stack.",
    )
    parser.add_argument(
        "--identity-stack",
        default="platform-identity",
        help="Platform identity CFN stack name (primary source for Cognito Pool ID)",
    )
    parser.add_argument(
        "--tools-gateway-stack",
        default="ToolsGatewayStack",
        help="Tools Gateway CDK stack name (fallback for Cognito Pool ID)",
    )
    args = parser.parse_args()

    # Resolve the region (explicit flag > AWS_REGION > AWS_DEFAULT_REGION >
    # boto3 session). Refuse ONLY when empty; never fall back to a literal.
    try:
        args.region = bedrock_region.resolve_region(args.region)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print("=" * 55)
    print("  LLM Gateway — Set Up Models, Keys & Teams")
    print("=" * 55)
    print()

    # Step 1: Get stack outputs and admin key
    print("[1/6] Reading CloudFormation stack outputs...")
    outputs = get_stack_outputs(args.stack_name, args.region)
    proxy_url = outputs.get("ProxyUrl", "").rstrip("/")
    secret_arn = outputs.get("AdminKeySecretArn", "")

    if not proxy_url:
        print("ERROR: Could not find ProxyUrl in stack outputs.")
        sys.exit(1)
    if not secret_arn:
        print("ERROR: Could not find AdminKeySecretArn in stack outputs.")
        sys.exit(1)

    raw_secret = get_admin_key(secret_arn, args.region)
    # Note: LITELLM_MASTER_KEY is the upstream LiteLLM env var name; our wrapper uses ADMIN_KEY terminology
    # The CFN template prepends "sk-" to the secret value for LITELLM_MASTER_KEY
    admin_key = f"sk-{raw_secret}" if not raw_secret.startswith("sk-") else raw_secret
    print(f"  Proxy URL:   {proxy_url}")
    print(f"  Admin Key:   (retrieved from Secrets Manager)")

    # Auto-detect Cognito User Pool ID from CloudFormation exports
    # Primary: workshop-CognitoUserPoolId (our canonical export)
    # Fallback: mcp-gateway-CognitoUserPoolId (upstream default EnvironmentName)
    cognito_pool_id = args.cognito_pool_id
    if not cognito_pool_id:
        try:
            cfn = boto3.client("cloudformation", region_name=args.region)
            all_exports = {}
            for page in cfn.get_paginator("list_exports").paginate():
                for exp in page.get("Exports", []):
                    all_exports[exp["Name"]] = exp["Value"]
            for name in ("workshop-CognitoUserPoolId", "mcp-gateway-CognitoUserPoolId"):
                if name in all_exports:
                    cognito_pool_id = all_exports[name]
                    print(f"  Cognito Pool: {cognito_pool_id} (from {name} export)")
                    break
        except Exception:
            pass
        if not cognito_pool_id:
            # Fallback: try legacy stack names
            for stack in [args.identity_stack, args.tools_gateway_stack]:
                try:
                    stack_outputs = get_stack_outputs(stack, args.region)
                    cognito_pool_id = stack_outputs.get("UserPoolId", "")
                    if cognito_pool_id:
                        print(f"  Cognito Pool: {cognito_pool_id} (from {stack} stack)")
                        break
                except Exception:
                    continue
        if not cognito_pool_id:
            print("  Cognito Pool: not detected (deploy Module 3 Registry stack first)")
    else:
        print(f"  Cognito Pool: {cognito_pool_id}")
    print()

    # Step 2: Wait for proxy to be ready
    print("[2/6] Checking proxy health...")
    if not wait_for_proxy(proxy_url):
        print("ERROR: LiteLLM Proxy did not become healthy. Check ECS task logs.")
        sys.exit(1)
    print("  Proxy is healthy!")
    print()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {admin_key}",
    }

    # Step 3: Register models in LiteLLM database
    print(f"[3/6] Registering {len(WORKSHOP_MODELS)} Bedrock models...")
    count = register_models(proxy_url, headers, args.region)
    print(f"  Registered {count}/{len(WORKSHOP_MODELS)} models successfully.")
    print()

    # Step 4: Create teams
    print("[4/6] Creating workshop teams...")
    teams = {}
    for alias, budget in [("platform-team", 10.0), ("workload-team", 5.0)]:
        resp = requests.post(
            f"{proxy_url}/team/new",
            json={
                "team_alias": alias,
                "max_budget": budget,
                "models": ["claude-sonnet", "claude-haiku", "llama3.3-70b", "nova-2-lite"],
            },
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        team_data = resp.json()
        team_id = team_data.get("team_id", "")
        teams[alias] = team_id
        print(f"  Created team '{alias}' (id={team_id[:12]}..., budget=${budget})")
    print()

    # Step 5: Create virtual keys (with Cognito identity metadata)
    print("[5/6] Creating virtual keys...")
    keys = {}
    # The admin key gets every registered model; the dev key gets a
    # *restricted* subset (no `llama3.3-70b`) so Step 3.3 can actually
    # demonstrate that virtual keys enforce their allowlist — the content
    # calls llama3.3-70b with the dev key and expects an HTTP 403 with
    # type "key_model_access_denied" (verified live 2026-06-03).
    for key_name, team_alias, budget, cognito_group, model_list in [
        ("workshop-admin-key", "platform-team", 10.0, "admins",
         ["claude-sonnet", "claude-haiku", "llama3.3-70b", "nova-2-lite"]),
        ("workshop-dev-key", "workload-team", 5.0, "developers",
         ["claude-sonnet", "claude-haiku", "nova-2-lite"]),
    ]:
        key_payload: dict = {
            "key_name": key_name,
            "team_id": teams[team_alias],
            "max_budget": budget,
            "models": model_list,
        }
        # Link virtual key to the shared Cognito identity
        if cognito_pool_id:
            key_payload["metadata"] = {
                "cognito_group": cognito_group,
                "cognito_user_pool_id": cognito_pool_id,
                "identity_provider": "cognito",
            }
        resp = requests.post(
            f"{proxy_url}/key/generate",
            json=key_payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        key_data = resp.json()
        virtual_key = key_data.get("key", "")
        keys[key_name] = virtual_key
        group_label = f" → Cognito '{cognito_group}'" if cognito_pool_id else ""
        print(f"  Created key '{key_name}' = {virtual_key[:16]}... (budget=${budget}){group_label}")
    print()

    # Step 6: Test completion with the dev key
    print("[6/6] Testing chat completion with virtual key...")
    test_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {keys['workshop-dev-key']}",
    }
    try:
        resp = requests.post(
            f"{proxy_url}/chat/completions",
            json={
                "model": "claude-sonnet",
                "messages": [
                    {"role": "user", "content": "Say hello in exactly 5 words."}
                ],
                "max_tokens": 50,
                "temperature": 0.5,
            },
            headers=test_headers,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        content = result["choices"][0]["message"]["content"]
        print(f"  Model response: {content}")
        print()
        print("  Bedrock is working through the LiteLLM Proxy!")
    except Exception as e:
        print(f"  Test completion failed: {e}")
        print("  Ensure Bedrock model access is enabled in your account.")

    print()
    print("=" * 55)
    print("  Setup complete!")
    print("=" * 55)
    print()
    print("  Export these for use in other scripts and the notebook.")
    print("  LLM_GATEWAY_API_KEY is the 'workshop-dev-key' virtual key —")
    print("  the scoped, developer-facing key the workshop uses for all")
    print("  chat calls. LLM_GATEWAY_ADMIN_KEY is the administrative key")
    print("  used for spend + key management endpoints only.")
    print()
    print(f"    export LLM_GATEWAY_URL={proxy_url}")
    # These are the participant's own freshly-created, budget-scoped ephemeral keys
    # printed to their own terminal. The Module 2 CLI walkthrough (content/module-2/
    # step-3) instructs participants to copy these export lines verbatim, so the full
    # values MUST be emitted here — masking them breaks the workshop. Not a secret leak.
    # NOTE: printing credentials is acceptable only in this ephemeral workshop
    # sandbox; never do this in production.
    print(f"    export LLM_GATEWAY_API_KEY={keys.get('workshop-dev-key', '')}       # = workshop-dev-key")
    # NOTE: printing credentials is acceptable only in this ephemeral workshop
    # sandbox; never do this in production.
    print(f"    export LLM_GATEWAY_ADMIN_KEY={admin_key}  # administrative key")
    print()

    if cognito_pool_id:
        print("  Identity mapping (virtual key → Cognito group):")
        print(f"    workshop-admin-key → Cognito group 'admins'")
        print(f"    workshop-dev-key   → Cognito group 'developers'")
        print(f"    Cognito User Pool: {cognito_pool_id}")
        print()
        print("  This means LLM spend is attributable to Cognito identities")
        print("  across both the LLM Gateway and the Tools Gateway.")
        print()


if __name__ == "__main__":
    main()
