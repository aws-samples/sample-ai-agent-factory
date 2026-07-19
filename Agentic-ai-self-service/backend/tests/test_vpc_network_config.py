"""Tests for VPC-egress runtime network configuration (Loom-study 0.1).

The runtime deployer hardcoded networkMode=PUBLIC and never read the modeled
vpc_config (dead config). _build_network_configuration now produces a VPC-mode
block from subnets + security groups, matching the live control-plane model
(networkModeConfig = {subnets, securityGroups}), and falls back to PUBLIC safely.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services.runtime_deployer import _build_network_configuration  # noqa: E402


def test_none_is_public():
    assert _build_network_configuration(None) == {"networkMode": "PUBLIC"}
    assert _build_network_configuration({}) == {"networkMode": "PUBLIC"}


def test_snake_case_vpc_config():
    out = _build_network_configuration(
        {
            "subnet_ids": ["subnet-a", "subnet-b"],
            "security_group_ids": ["sg-1"],
        }
    )
    assert out["networkMode"] == "VPC"
    assert out["networkModeConfig"]["subnets"] == ["subnet-a", "subnet-b"]
    assert out["networkModeConfig"]["securityGroups"] == ["sg-1"]


def test_camel_case_vpc_config():
    out = _build_network_configuration({"subnets": ["subnet-a"], "securityGroups": ["sg-1"]})
    assert out["networkMode"] == "VPC"
    assert out["networkModeConfig"]["subnets"] == ["subnet-a"]


def test_incomplete_vpc_config_falls_back_to_public():
    # Missing SGs → PUBLIC (avoid a guaranteed-reject VPC call).
    assert _build_network_configuration({"subnet_ids": ["subnet-a"]}) == {"networkMode": "PUBLIC"}
    # Missing subnets → PUBLIC.
    assert _build_network_configuration({"security_group_ids": ["sg-1"]}) == {"networkMode": "PUBLIC"}
