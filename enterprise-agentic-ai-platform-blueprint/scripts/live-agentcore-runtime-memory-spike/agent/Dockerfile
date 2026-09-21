# syntax=docker/dockerfile:1
#
# ARM64, non-root container image for the AgentCore Runtime+Memory
# compatibility probe agent.
#
# AgentCore Runtime requires linux/arm64 images that listen on :8080 and expose
# the /invocations and /ping contract that BedrockAgentCoreApp implements. This
# image serves only that deterministic handshake: it has no credentials baked
# in and runs as an unprivileged user so the probe exercises the platform
# contract rather than any agent logic. (Network egress is governed by the
# Runtime's networkConfiguration at deploy time, NOT by this image; PUBLIC mode
# means outbound access is available, so nothing here should be read as a claim
# that the container cannot reach the network.)
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# Fail fast, no bytecode churn, unbuffered logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install exact-pinned runtime dependency first for layer caching.
# Deterministic install: no cache, no implicit pip self-upgrade. bedrock-agentcore
# resolves its own transitive deps; the agent imports nothing else.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt \
    && pip check

# Copy only the agent entrypoint.
COPY agent.py /app/agent.py

# Create and drop to an unprivileged, non-root user (no shell, no home writes).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent \
    && chown -R agent:agent /app
USER 10001

# AgentCore Runtime speaks HTTP to the container on 8080.
EXPOSE 8080

ENTRYPOINT ["python", "-u", "agent.py"]
