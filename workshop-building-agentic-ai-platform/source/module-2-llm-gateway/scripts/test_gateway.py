#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test the LLM Gateway (LiteLLM Proxy) with various exercises.

Demonstrates: health checks, model listing, chat completions, virtual key
spend tracking, caching, multi-model routing, and Strands Agent integration.

Usage:
    python scripts/test_gateway.py [--url https://...] [--api-key ...]

    Or using environment variables (always use the HTTPS gateway endpoint):
        export LLM_GATEWAY_URL=https://<api-gateway-endpoint>
        export LLM_GATEWAY_API_KEY=<virtual-key>
        python scripts/test_gateway.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Add parent directory to path so we can import the client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm_gateway_client import LLMGatewayClient


def section(title: str) -> None:
    print()
    print(f"{'=' * 55}")
    print(f"  {title}")
    print(f"{'=' * 55}")
    print()


def test_health(client: LLMGatewayClient) -> None:
    section("1. Health Check")
    try:
        result = client.health_check()
        print(f"  Status: {json.dumps(result, indent=2)}")
    except Exception as e:
        print(f"  Health check failed: {e}")


def test_model_health(client: LLMGatewayClient) -> None:
    section("2. Model Health")
    try:
        result = client.model_health()
        healthy = result.get("healthy_endpoints", [])
        unhealthy = result.get("unhealthy_endpoints", [])
        print(f"  Healthy models:   {len(healthy)}")
        print(f"  Unhealthy models: {len(unhealthy)}")
        if unhealthy:
            print()
            print("  Unhealthy (not enabled in Bedrock Model Access):")
            for ep in unhealthy[:8]:
                print(f"    - {ep.get('model', 'unknown')}")
    except Exception as e:
        print(f"  Model health check failed: {e}")


def test_list_models(client: LLMGatewayClient) -> None:
    section("3. List Available Models")
    try:
        models = client.list_models()
        if models.data:
            # Note: the OpenAI-compatible /v1/models surface reports owned_by="openai"
            # for every model regardless of the real provider — it reflects the API
            # compatibility layer, not the backend. These are all Amazon Bedrock models
            # routed through LiteLLM, so we don't print the misleading owner field.
            for m in models.data:
                print(f"  - {m.id}")
        else:
            print("  No models found. Check litellm-config.yaml.")
    except Exception as e:
        print(f"  Failed: {e}")


def test_chat_completion(client: LLMGatewayClient) -> None:
    section("4. Chat Completion (Bedrock Claude via LiteLLM)")
    try:
        response = client.chat(
            prompt="What are three benefits of using an LLM Gateway in enterprise AI?",
            model="claude-sonnet",
            max_tokens=200,
        )
        print(f"  Response:\n  {response}")
    except Exception as e:
        print(f"  Chat completion failed: {e}")
        print("  Ensure Bedrock model access is enabled in your account.")


def test_caching(client: LLMGatewayClient) -> None:
    section("5. Caching Demonstration")
    prompt = "What is 2 + 2? Reply with just the number."

    print("  Sending the same request twice to demonstrate caching...")
    print()

    start = time.time()
    try:
        r1 = client.chat(prompt=prompt, model="claude-sonnet", max_tokens=10, temperature=0)
        t1 = time.time() - start
        print(f"  Request 1: '{r1}' ({t1:.2f}s)")
    except Exception as e:
        print(f"  Request 1 failed: {e}")
        return

    start = time.time()
    try:
        r2 = client.chat(prompt=prompt, model="claude-sonnet", max_tokens=10, temperature=0)
        t2 = time.time() - start
        print(f"  Request 2: '{r2}' ({t2:.2f}s)")
    except Exception as e:
        print(f"  Request 2 failed: {e}")
        return

    if t2 < t1 * 0.5:
        print(f"\n  Cache hit! Second request was {t1/t2:.1f}x faster.")
    else:
        print("\n  Both requests took similar time — caching may need a moment to warm up.")


def test_multi_model(client: LLMGatewayClient) -> None:
    section("6. Multi-Model Routing")
    prompt = "In one sentence, what makes you unique?"
    models = ["claude-sonnet", "claude-haiku", "nova-2-lite"]

    for model_id in models:
        try:
            response = client.chat(prompt=prompt, model=model_id, max_tokens=100)
            print(f"  [{model_id}]")
            print(f"  {response}")
            print()
        except Exception as e:
            print(f"  [{model_id}] Failed: {e}")
            print()


def test_spend_tracking(client: LLMGatewayClient) -> None:
    section("7. Spend Tracking")
    try:
        logs = client.get_spend_logs()
        if logs:
            total_spend = sum(log.get("spend", 0) for log in logs)
            print(f"  Total requests logged: {len(logs)}")
            print(f"  Total spend: ${total_spend:.6f}")
            for log in logs[:3]:
                print(f"    - model={log.get('model', '?')}, "
                      f"tokens={log.get('total_tokens', 0)}, "
                      f"spend=${log.get('spend', 0):.6f}")
        else:
            print("  No spend logs yet. Logs appear after a short delay.")
    except Exception as e:
        print(f"  Spend tracking failed: {e}")


def test_strands_agent(proxy_url: str, api_key: str) -> None:
    section("8. Strands Agent Integration (LiteLLMModel)")
    try:
        from strands import Agent
        from strands.models.litellm import LiteLLMModel

        # Use "openai/" prefix so litellm routes through the proxy
        # (OpenAI-compatible endpoint), and pass connection details via params
        model = LiteLLMModel(
            model_id="openai/claude-sonnet",
            params={
                "api_base": proxy_url,
                "api_key": api_key,
            },
        )

        agent = Agent(model=model)
        result = agent("What is 15 * 23? Think step by step and give the answer.")
        print(f"  Agent response: {result}")
        print()
        print("  Strands Agent → LiteLLM Proxy → Bedrock is working!")
    except ImportError:
        print("  strands-agents not installed. Install the pinned workshop version with:")
        print("    pip install 'strands-agents[litellm]==0.1.5'")
    except Exception as e:
        print(f"  Strands Agent test failed: {e}")
        print("  This is expected if strands-agents[litellm] is not installed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the LLM Gateway (LiteLLM Proxy)")
    parser.add_argument(
        "--url",
        default=os.environ.get("LLM_GATEWAY_URL", ""),
        help="LiteLLM Proxy base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LLM_GATEWAY_API_KEY", ""),
        help="Virtual key or admin key",
    )
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("LLM_GATEWAY_ADMIN_KEY", ""),
        help="Admin key for admin endpoints (spend tracking). "
        "Defaults to LLM_GATEWAY_ADMIN_KEY env var.",
    )
    parser.add_argument(
        "--skip-strands",
        action="store_true",
        help="Skip the Strands Agent integration test",
    )
    args = parser.parse_args()

    if not args.url:
        print("ERROR: Provide --url or set LLM_GATEWAY_URL environment variable.")
        sys.exit(1)

    client = LLMGatewayClient(proxy_url=args.url, api_key=args.api_key)

    print("=" * 55)
    print("  LLM Gateway (LiteLLM Proxy) — Test Suite")
    print("=" * 55)
    print(f"  URL: {args.url}")
    print(f"  Key: {'*' * 8 + args.api_key[-4:] if args.api_key else '(none)'}")

    test_health(client)
    test_model_health(client)
    test_list_models(client)
    test_chat_completion(client)
    test_caching(client)
    test_multi_model(client)

    # Spend tracking requires admin key (admin endpoint)
    if args.admin_key:
        admin_client = LLMGatewayClient(proxy_url=args.url, api_key=args.admin_key)
        test_spend_tracking(admin_client)
    else:
        section("7. Spend Tracking")
        print("  Skipped — set LLM_GATEWAY_ADMIN_KEY or pass --admin-key")
        print("  (spend tracking requires admin credentials)")

    if not args.skip_strands:
        test_strands_agent(args.url, args.api_key)

    section("Done!")
    print("  All tests completed. Check the LiteLLM Admin UI at:")
    print(f"  {args.url}/ui")
    print()
    print("  View spend tracking at:")
    print(f"  {args.url}/spend/logs")


if __name__ == "__main__":
    main()
