from __future__ import annotations

import asyncio

from app.routers import deploy_targets
from app.services import deploy_target as dt


def test_disabled_target_catalog_exposes_only_the_platform_default(monkeypatch):
    monkeypatch.setattr(dt, "targets_enabled", lambda: False)
    monkeypatch.setattr(dt, "home_region", lambda: "us-east-1")
    monkeypatch.setattr(
        dt,
        "list_accounts",
        lambda: (_ for _ in ()).throw(AssertionError("disabled catalog should not enumerate accounts")),
    )
    monkeypatch.setattr(
        dt,
        "list_regions",
        lambda: (_ for _ in ()).throw(AssertionError("disabled catalog should not enumerate regions")),
    )

    result = asyncio.run(deploy_targets.list_deploy_target_options(_caller_sub="deployer"))

    assert result == {
        "enabled": False,
        "home_region": "us-east-1",
        "regions": [],
        "accounts": [],
    }


def test_target_catalog_is_sanitized_sorted_and_contains_no_role_arns(monkeypatch):
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "home_region", lambda: "us-east-1")
    monkeypatch.setattr(
        dt,
        "list_regions",
        lambda: ["eu-west-1", "us-west-2", "eu-west-1"],
    )
    monkeypatch.setattr(
        dt,
        "list_accounts",
        lambda: [
            {
                "account_id": "222222222222",
                "region": "us-west-2",
                "role_arn": "arn:aws:iam::222222222222:role/secret-deployment-role",
                "runtime_role_arn": "arn:aws:iam::222222222222:role/secret-runtime-role",
            },
            {
                "account_id": "111111111111",
                "region": "eu-west-1",
                "harness_role_arn": "arn:aws:iam::111111111111:role/secret-harness-role",
            },
        ],
    )

    result = asyncio.run(deploy_targets.list_deploy_target_options(_caller_sub="deployer"))

    assert result == {
        "enabled": True,
        "home_region": "us-east-1",
        "regions": ["eu-west-1", "us-west-2"],
        "accounts": [
            {"account_id": "111111111111", "region": "eu-west-1"},
            {"account_id": "222222222222", "region": "us-west-2"},
        ],
    }
    assert "role_arn" not in str(result)
