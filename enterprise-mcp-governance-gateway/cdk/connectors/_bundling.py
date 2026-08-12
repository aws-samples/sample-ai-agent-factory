# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared helper: bundle a custom-resource Lambda with a current boto3 vendored in.

Why: the Lambda runtime's built-in boto3 predates some bedrock-agentcore-control
operations (``update_gateway``, gateway targets), so the handler must ship its own.

How: CDK's built-in Docker bundling (``BundlingOptions``) runs ``pip install`` inside the
official Lambda Python build image and copies the result into the asset. This is the
stable ``aws-cdk-lib`` equivalent of the ``PythonFunction`` construct's packaging, without
taking a dependency on the experimental ``aws-lambda-python-alpha`` module.

Requires a container runtime (Docker or Finch) at synth time — e.g.
``CDK_DOCKER=finch cdk deploy``.
"""
from pathlib import Path

from aws_cdk import BundlingOptions, aws_lambda as lambda_

_BOTO3 = "boto3==1.43.24"


def bundled_asset(src_dir: Path) -> lambda_.AssetCode:
    """A Lambda asset from ``src_dir`` with a current boto3 vendored in."""
    return lambda_.Code.from_asset(
        str(src_dir),
        bundling=BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_12.bundling_image,
            command=["bash", "-c",
                     f"pip install '{_BOTO3}' -t /asset-output >/dev/null && cp -r . /asset-output"],
        ),
    )
