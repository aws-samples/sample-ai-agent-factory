"""Named VPC config profiles (Loom-study 4.2).

Lets an admin name a set of subnets + security groups once (e.g. "prod-private")
and pick it by name at deploy time, instead of retyping raw IDs on every agent.
The deploy path resolves a profile name to its {subnet_ids, security_group_ids}
and threads it into the runtime's VPC network mode (Phase-0 0.1).

Stored in the shared org-config table (reused: PK ``org_id``, SK ``VPCPROFILE#<name>``)
— no new table, same pattern as approval policies + tag policies.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import boto3
from pydantic import BaseModel, Field

_VPC_PREFIX = "VPCPROFILE#"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,}$")
_SG_RE = re.compile(r"^sg-[0-9a-f]{8,}$")


class VpcProfile(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    subnet_ids: list[str] = Field(min_length=1, max_length=16)
    security_group_ids: list[str] = Field(min_length=1, max_length=16)
    description: str = Field(default="", max_length=256)


class VpcProfileStore:
    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, org_id: str, profile: VpcProfile) -> VpcProfile:
        item = profile.model_dump()
        item["org_id"] = org_id
        item["sk"] = _VPC_PREFIX + profile.name
        self._table.put_item(Item=item)
        return profile

    def get(self, org_id: str, name: str) -> Optional[VpcProfile]:
        item = self._table.get_item(Key={"org_id": org_id, "sk": _VPC_PREFIX + name}).get("Item")
        return _to_profile(item) if item else None

    def delete(self, org_id: str, name: str) -> bool:
        self._table.delete_item(Key={"org_id": org_id, "sk": _VPC_PREFIX + name})
        return True

    def list(self, org_id: str) -> list[VpcProfile]:
        resp = self._table.query(
            KeyConditionExpression="org_id = :o AND begins_with(sk, :p)",
            ExpressionAttributeValues={":o": org_id, ":p": _VPC_PREFIX},
        )
        return [_to_profile(i) for i in resp.get("Items", [])]


def _to_profile(item: dict) -> VpcProfile:
    return VpcProfile(
        name=item.get("name", (item.get("sk", "") or "").replace(_VPC_PREFIX, "")),
        subnet_ids=list(item.get("subnet_ids", [])),
        security_group_ids=list(item.get("security_group_ids", [])),
        description=item.get("description", ""),
    )


def validate_profile(profile: VpcProfile) -> None:
    """Reject malformed names / subnet / SG ids at the API boundary. Raises ValueError."""
    if not _NAME_RE.match(profile.name):
        raise ValueError("Invalid profile name")
    for s in profile.subnet_ids:
        if not _SUBNET_RE.match(s):
            raise ValueError(f"Invalid subnet id: {s}")
    for g in profile.security_group_ids:
        if not _SG_RE.match(g):
            raise ValueError(f"Invalid security group id: {g}")


def resolve_vpc_config(org_id: str, name: str, table_name: str, region: str) -> Optional[dict]:
    """Resolve a profile name to a vpc_config dict for the deployer, or None."""
    if not name or not table_name:
        return None
    prof = VpcProfileStore(table_name, region).get(org_id, name)
    if prof is None:
        return None
    return {"subnet_ids": prof.subnet_ids, "security_group_ids": prof.security_group_ids}


def get_vpc_profile_store() -> VpcProfileStore:
    return VpcProfileStore(
        table_name=os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy"),
        region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
    )
