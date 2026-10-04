"""Extract the resolved ``SeedCredentialProvider`` inline policy of the idprov role.

Reads a rendered RuntimeMemory template (``render-runtime-memory-template.ts``),
finds the ``AgenticAI-D03-<env>-<tenant>-<agent>-idprov`` role, resolves the
partition/account/region intrinsics exactly like ``simulate_idprov_policy.py``
and writes the single inline policy document as JSON. Used to repair a deployed
role IN PLACE with byte-for-byte the document the fixed synth would deploy, so
a retried stack delete exercises the real custom-resource delete path.

Read-only; writes only the local output file.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from simulate_idprov_policy import resolve


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--role-name", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--partition", default="aws")
    ap.add_argument("--policy-name", default="SeedCredentialProvider")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    template = json.loads(args.template.read_text())
    role = next(
        (r for r in template["Resources"].values()
         if r["Type"] == "AWS::IAM::Role" and r["Properties"].get("RoleName") == args.role_name),
        None,
    )
    if role is None:
        print("role not found in template", file=sys.stderr)
        return 2
    policy = next(
        (p for p in role["Properties"]["Policies"] if p["PolicyName"] == args.policy_name),
        None,
    )
    if policy is None:
        print("policy not found on role", file=sys.stderr)
        return 2
    doc = resolve(policy["PolicyDocument"], args.partition, args.account, args.region)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True))
    sids = [s.get("Sid") for s in doc["Statement"]]
    print(json.dumps({"roleName": args.role_name, "policyName": args.policy_name, "sids": sids}))
    return 0


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    sys.exit(main())
