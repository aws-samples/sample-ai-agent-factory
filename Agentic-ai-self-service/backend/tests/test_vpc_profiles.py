"""Tests for named VPC config profiles (Loom-study 4.2)."""

from __future__ import annotations

import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "src")

from app.services.vpc_profile_store import (  # noqa: E402
    VpcProfile,
    VpcProfileStore,
    resolve_vpc_config,
    validate_profile,
)

TABLE = "tag-policy-test"


def _make_table():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )


@mock_aws
def test_crud_and_resolve():
    _make_table()
    store = VpcProfileStore(TABLE, "us-east-1")
    store.put("org1", VpcProfile(
        name="prod-private",
        subnet_ids=["subnet-0123456789abcdef0"],
        security_group_ids=["sg-0123456789abcdef0"],
        description="private egress",
    ))
    got = store.get("org1", "prod-private")
    assert got is not None and got.subnet_ids == ["subnet-0123456789abcdef0"]
    assert {p.name for p in store.list("org1")} == {"prod-private"}
    # resolve → vpc_config dict for the deployer
    cfg = resolve_vpc_config("org1", "prod-private", TABLE, "us-east-1")
    assert cfg == {"subnet_ids": ["subnet-0123456789abcdef0"], "security_group_ids": ["sg-0123456789abcdef0"]}
    # unknown profile → None (deploy boundary turns this into a 400)
    assert resolve_vpc_config("org1", "nope", TABLE, "us-east-1") is None
    store.delete("org1", "prod-private")
    assert store.get("org1", "prod-private") is None


def test_validate_rejects_bad_ids():
    with pytest.raises(ValueError, match="profile name"):
        validate_profile(VpcProfile(name="bad name!", subnet_ids=["subnet-0123456789abcdef0"], security_group_ids=["sg-0123456789abcdef0"]))
    with pytest.raises(ValueError, match="subnet"):
        validate_profile(VpcProfile(name="ok", subnet_ids=["not-a-subnet"], security_group_ids=["sg-0123456789abcdef0"]))
    with pytest.raises(ValueError, match="security group"):
        validate_profile(VpcProfile(name="ok", subnet_ids=["subnet-0123456789abcdef0"], security_group_ids=["not-a-sg"]))
    # a valid one does not raise
    validate_profile(VpcProfile(name="ok-1", subnet_ids=["subnet-0123456789abcdef0"], security_group_ids=["sg-0123456789abcdef0"]))
