"""The dependency bundles must still fit in the BucketDeployment Lambda's /tmp.

``upload_agentcore_deps`` ships whatever is in ``backend/agentcore-deps/`` — nobody edits
CDK to add a bundle, which is exactly how the eight ``provider-<extra>.zip`` bundles started
shipping with no infra change. The unguarded consequence is that the directory can grow past
what the deployment Lambda can hold, and the failure is not graceful or local: a
BucketDeployment that fills /tmp fails the PLATFORM deploy, with a CDK custom-resource error
that names no bundle.

The Lambda downloads the asset zip into /tmp and extracts it beside itself, so peak usage is
roughly twice the directory size. Measured 2026-09-20 with the provider bundles present:
131.8 MiB asset + 131.8 MiB extracted = 263.6 MiB against ``ephemeral_storage_size`` of
1024 MiB. The bundles are already-compressed zips, so the asset zip does not shrink — the
directory size IS the asset size, which is what makes the 2x model a fair one.

This asserts against the value actually set in ``buckets.py``, read out of the synthesized
template, not a copy of the number. A test holding its own copy of 1024 passes happily after
someone lowers the real one.
"""

from __future__ import annotations

import os

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

_DEPS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agentcore-deps"))

# The fraction of /tmp the peak may occupy. 0.75 leaves room for the awscli layer's own
# scratch files and for a bundle that compresses better than these do.
_SAFE_FRACTION = 0.75


def _directory_mib(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024 * 1024)


def _ephemeral_storage_mib() -> float:
    """The value ``upload_agentcore_deps`` really sets, read from the synthesized stack.

    The BucketDeployment's Lambda is a singleton in the stack, so its EphemeralStorage is
    the one this construct configured.
    """
    from aws_cdk import aws_s3 as s3
    from stacks.platform.buckets import upload_agentcore_deps

    app = cdk.App()
    stack = cdk.Stack(app, "EphemeralProbe", env=cdk.Environment(account="111122223333", region="us-east-1"))
    # A plain bucket, not build_artifacts_bucket: the construct under test only needs a
    # destination, and pulling in PlatformConfig would couple this measurement to unrelated
    # platform wiring.
    bucket = s3.Bucket(stack, "ProbeArtifacts")
    if upload_agentcore_deps(stack, bucket) is None:
        pytest.skip("backend/agentcore-deps/ is absent; nothing is uploaded and nothing to bound")

    functions = Template.from_stack(stack).find_resources("AWS::Lambda::Function")
    sizes = [
        props["Properties"]["EphemeralStorage"]["Size"]
        for props in functions.values()
        if props.get("Properties", {}).get("EphemeralStorage")
    ]
    assert sizes, "the BucketDeployment Lambda declares no EphemeralStorage; the /tmp budget is the 512 MiB default"
    assert len(set(sizes)) == 1, f"more than one ephemeral storage value in the probe stack: {sizes}"
    return float(sizes[0])


@pytest.mark.skipif(not os.path.isdir(_DEPS_DIR), reason="bundles not built in this checkout")
def test_the_bundles_fit_with_room_to_extract_them():
    deps_mib = _directory_mib(_DEPS_DIR)
    assert deps_mib > 0, f"{_DEPS_DIR} exists but is empty"

    ephemeral_mib = _ephemeral_storage_mib()
    peak_mib = deps_mib * 2  # asset zip in /tmp, plus its extraction beside it
    budget_mib = ephemeral_mib * _SAFE_FRACTION

    assert peak_mib <= budget_mib, (
        f"backend/agentcore-deps/ is {deps_mib:.1f} MiB, so the BucketDeployment Lambda needs "
        f"about {peak_mib:.1f} MiB of /tmp (the asset zip plus its extraction), which exceeds "
        f"{_SAFE_FRACTION:.0%} of the {ephemeral_mib:.0f} MiB configured in "
        f"stacks/platform/buckets.py. Raise ephemeral_storage_size there, or stop shipping a "
        f"bundle. Leaving it fails the whole platform deploy in a CDK custom resource that "
        f"names no bundle."
    )


@pytest.mark.skipif(not os.path.isdir(_DEPS_DIR), reason="bundles not built in this checkout")
def test_the_measurement_is_of_a_real_directory_with_the_provider_bundles_in_it():
    """Non-vacuity. The bound above is trivially satisfied by an empty directory, and an
    empty directory is also the state in which every non-Bedrock agent is broken."""
    names = os.listdir(_DEPS_DIR)
    assert "strands-mcp.zip" in names, "the baseline bundle is missing; run scripts/install-agentcore-deps.sh"
    providers = sorted(n for n in names if n.startswith("provider-") and n.endswith(".zip"))
    assert providers, "no provider-*.zip bundles are present, so no non-Bedrock agent can import its SDK"
    assert _directory_mib(_DEPS_DIR) > 50, "the bundles are implausibly small; the bound below would pass on nothing"
