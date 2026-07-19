"""Shared configuration passed to every PlatformStack builder module."""

from dataclasses import dataclass

from aws_cdk import RemovalPolicy


@dataclass(frozen=True)
class PlatformConfig:
    """Environment-level knobs shared by every builder.

    ``removal_policy`` / ``allow_destroy`` implement audit issue #9: gate
    RemovalPolicy.DESTROY on environment so prod-like envs don't lose data on
    teardown. dev/test/sandbox/preview environments use DESTROY; everything
    else uses RETAIN. Override via env var AGENTCORE_ALLOW_DESTROY=true.
    """

    env: str
    project: str
    removal_policy: RemovalPolicy
    allow_destroy: bool
