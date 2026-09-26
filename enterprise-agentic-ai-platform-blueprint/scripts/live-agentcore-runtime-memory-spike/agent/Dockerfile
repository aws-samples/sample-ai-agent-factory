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

# AWS-maintained AL2023 Python base, pinned to the live-scanned ARM64 digest.
FROM public.ecr.aws/lambda/python:3.13@sha256:c78a03b745f2c27b349377ae979d88b96bc68b457668705cd24a32b5f433c9cf

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

# Drop to a numeric, unprivileged user. The image needs no passwd entry or home;
# /app and its copied files are world-readable and runtime writes go to /tmp.
USER 10001

# AgentCore Runtime speaks HTTP to the container on 8080.
EXPOSE 8080

ENTRYPOINT ["python", "-u", "agent.py"]
CMD []
