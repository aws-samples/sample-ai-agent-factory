#!/usr/bin/env python3
"""Guard the five participant IAM policies against drift.

The same five documents are consumed in two places:

  1. static/cfn/workshop-iam-policy-{network,infra,core,core-2,core-3}.json
     -- attached to WSParticipantRole by contentspec.yaml.
  2. static/cfn/code-editor.yaml
     -- embedded as AWS::IAM::ManagedPolicy resources and attached to
        CodeEditorInstanceBootstrapRole, which is the identity that actually
        executes every CLI command and notebook cell in the workshop.

CloudFormation cannot read a sibling file, so (2) has to be a copy. This script
makes that copy's divergence a hard failure instead of a silent one -- the same
mechanism that previously caught interceptors.py drifting from its CFN-inline
twin.

  verify-ide-policy-parity.py          check only; exit 1 on drift
  verify-ide-policy-parity.py --sync   rewrite the embedded copies from source

It also re-checks the IAM managed-policy size quota (6144 chars minified), which
is the constraint that forced these into five files in the first place.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "static/cfn/code-editor.yaml"
MANAGED_POLICY_MAX = 6144

# CFN logical id -> source file slug
POLICIES = OrderedDict([
    ("CodeEditorParticipantPolicyNetwork", "network"),
    ("CodeEditorParticipantPolicyInfra", "infra"),
    ("CodeEditorParticipantPolicyCore", "core"),
    ("CodeEditorParticipantPolicyCore2", "core-2"),
    ("CodeEditorParticipantPolicyCore3", "core-3"),
])


def source_path(slug):
    return REPO / f"static/cfn/workshop-iam-policy-{slug}.json"


def minified_len(doc):
    return len(json.dumps(doc, separators=(",", ":")))


def to_yaml(obj, indent):
    """Render a JSON policy document as the YAML this template embeds.

    Keys and scalars are emitted via json.dumps so every string is quoted. That
    keeps YAML from coercing values that look like numbers or booleans, and it
    makes the output byte-stable so --sync produces no spurious diffs.
    """
    pad = " " * indent
    if isinstance(obj, dict):
        lines = []
        for key, val in obj.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}{json.dumps(key)}:\n" + to_yaml(val, indent + 2))
            else:
                lines.append(f"{pad}{json.dumps(key)}: {json.dumps(val)}")
        return "\n".join(lines) + "\n"
    if isinstance(obj, list):
        lines = []
        for val in obj:
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}-\n" + to_yaml(val, indent + 2))
            else:
                lines.append(f"{pad}- {json.dumps(val)}")
        return "\n".join(lines) + "\n"
    return f"{pad}{json.dumps(obj)}\n"


def block_pattern(logical_id):
    return re.compile(
        r"(  " + logical_id + r":\n    Type: AWS::IAM::ManagedPolicy\n"
        r".*?      PolicyDocument:\n)(.*?)(?=\n  [#A-Za-z])",
        re.S,
    )


def load_embedded(template_text, logical_id):
    """Parse the embedded PolicyDocument without a YAML dependency.

    The embedded block is plain quoted-scalar YAML emitted by to_yaml(), never
    a CFN intrinsic, so re-reading it with the same shape it was written in is
    sufficient -- and it keeps this script runnable with the stdlib alone.
    """
    match = block_pattern(logical_id).search(template_text)
    if not match:
        return None
    return match.group(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync", action="store_true",
                        help="rewrite the embedded copies from the source JSON")
    args = parser.parse_args()

    text = TEMPLATE.read_text()
    drift, oversize, missing = [], [], []
    updated = text

    for logical_id, slug in POLICIES.items():
        path = source_path(slug)
        if not path.exists():
            missing.append(str(path.relative_to(REPO)))
            continue
        doc = json.loads(path.read_text(), object_pairs_hook=OrderedDict)

        size = minified_len(doc)
        status = "ok"
        if size > MANAGED_POLICY_MAX:
            oversize.append((slug, size))
            status = "OVERSIZE"

        expected = to_yaml(doc, 8).rstrip("\n")
        actual = load_embedded(updated, logical_id)
        if actual is None:
            missing.append(f"{logical_id} (no AWS::IAM::ManagedPolicy block in template)")
            continue
        actual = actual.rstrip("\n")

        if actual != expected:
            if args.sync:
                match = block_pattern(logical_id).search(updated)
                updated = updated[:match.start(2)] + expected + updated[match.end(2):]
                status = "synced"
            else:
                drift.append(logical_id)
                status = "DRIFT"

        # Keep the "Minified size N/6144" comment honest too.
        updated = re.sub(
            r"(# Minified size )\d+(/6144 bytes \(IAM managed-policy limit\).\n  "
            + logical_id + r":)",
            lambda m: m.group(1) + str(size) + m.group(2),
            updated,
        )

        print(f"  {logical_id:38} {slug:8} {size:5}/{MANAGED_POLICY_MAX}  {status}")

    if args.sync and updated != text:
        TEMPLATE.write_text(updated)
        print("\nembedded copies synced from source JSON")
    elif args.sync:
        print("\nalready in sync")

    if missing:
        print("\nERROR: missing inputs:", ", ".join(missing), file=sys.stderr)
        return 1
    if oversize:
        for slug, size in oversize:
            print(f"\nERROR: workshop-iam-policy-{slug}.json is {size} chars minified, "
                  f"over the {MANAGED_POLICY_MAX}-char IAM managed-policy quota. Move "
                  f"statements into a new file and add it to contentspec.yaml "
                  f"iamPolicies and to POLICIES in this script.", file=sys.stderr)
        return 1
    if drift:
        print(f"\nERROR: embedded copies in {TEMPLATE.relative_to(REPO)} have drifted "
              f"from source: {', '.join(drift)}\n"
              f"Run: scripts/verify-ide-policy-parity.py --sync", file=sys.stderr)
        return 1

    print("\nIDE policy parity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
