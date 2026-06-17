# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sync Lambda: reads MCP servers from Registry API and syncs to AgentCore Gateway targets.

Triggered by EventBridge schedule (every 5 minutes) or manual invocation.
Uses the RegistryClient for M2M-authenticated access to the Registry API,
and GatewaySyncService to manage gateway targets.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

GATEWAY_ID = os.environ.get("GATEWAY_ID", "")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "")
M2M_SECRET_NAME = os.environ.get("M2M_SECRET_NAME", "")
CLOUDFRONT_URL = os.environ.get("CLOUDFRONT_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
SYNC_FILTER_TAGS = [
    t.strip() for t in os.environ.get("SYNC_FILTER_TAGS", "").split(",") if t.strip()
]


def handler(event, context):
    """Sync Registry servers → AgentCore Gateway targets.

    Workflow:
    1. Fetch all servers from Registry API
    2. List current gateway targets
    3. For each Registry server, build target config and create if missing
    4. Report created/skipped/failed counts
    """
    if not GATEWAY_ID:
        logger.warning("GATEWAY_ID not set — skipping sync")
        return {"synced": 0, "created": 0, "skipped": 0, "errors": 0}

    if not REGISTRY_URL or not M2M_SECRET_NAME:
        logger.error("REGISTRY_URL or M2M_SECRET_NAME not set")
        return {"synced": 0, "created": 0, "skipped": 0, "errors": 0}

    # Import here to allow Lambda cold start optimization
    from services.registry_client import RegistryClient
    from services.gateway_sync import GatewaySyncService

    registry = RegistryClient(REGISTRY_URL, M2M_SECRET_NAME, AWS_REGION)
    sync_svc = GatewaySyncService(region=AWS_REGION)

    # Step 1: Fetch all servers from Registry
    try:
        servers = registry.list_servers()
        logger.info("Fetched %d servers from Registry", len(servers))
    except Exception as e:
        logger.error("Failed to fetch servers from Registry: %s", e)
        return {"synced": 0, "created": 0, "skipped": 0, "errors": 1}

    # Step 2: Get current gateway targets for dedup
    existing_targets = sync_svc.list_targets(GATEWAY_ID)
    existing_names = {t.get("name", "") for t in existing_targets}
    logger.info("Found %d existing targets", len(existing_names))

    created = 0
    skipped = 0
    filtered = 0
    errors = 0

    # Step 3: Process each server
    for server in servers:
        name = server.get("display_name") or server.get("server_name") or server.get("name", "")
        target_name = f"tg-{name}"

        if target_name in existing_names:
            logger.debug("Target %s already exists, skipping", target_name)
            skipped += 1
            continue

        proxy_url = server.get("proxy_pass_url", "")

        # Tag-based selection: only sync servers matching filter tags
        if SYNC_FILTER_TAGS:
            server_tags = server.get("tags", [])
            if isinstance(server_tags, str):
                server_tags = [t.strip() for t in server_tags.split(",")]
            if not any(tag in SYNC_FILTER_TAGS for tag in server_tags):
                logger.info(
                    "Filtered %s — tags %s don't match filter %s",
                    name, server_tags, SYNC_FILTER_TAGS,
                )
                filtered += 1
                continue

        try:
            # Lambda targets → create in AgentCore Gateway (Path B)
            if proxy_url.startswith("lambda://"):
                config = sync_svc.build_target_config(server)
            else:
                # HTTP/MCP servers are accessible via Path A (CloudFront/NGINX)
                # They don't need AgentCore Gateway targets
                logger.info(
                    "Skipping %s — HTTP server uses Path A (direct via CloudFront)", name
                )
                skipped += 1
                continue

            if config is None:
                logger.debug("Skipping %s — unknown target type", name)
                skipped += 1
                continue

            result = sync_svc.create_target(GATEWAY_ID, config)
            if result:
                created += 1
            else:
                errors += 1
        except ValueError as e:
            logger.warning("Invalid target config for %s: %s", name, e)
            skipped += 1
        except Exception as e:
            logger.error("Error processing server %s: %s", name, e)
            errors += 1

    logger.info(
        "Sync complete: %d created, %d skipped, %d filtered, %d errors (from %d servers)",
        created, skipped, filtered, errors, len(servers),
    )

    return {
        "synced": len(servers),
        "created": created,
        "skipped": skipped,
        "filtered": filtered,
        "errors": errors,
    }
