#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Verify the deploy script cannot trust a foreign S3 bucket.

This is a source contract, not an AWS mock. It complements the live test, which
uses real S3 owner-condition requests and a real CloudFormation deployment.

Usage:
    python3 scripts/verify-s3-owner-guards.py [workshop-root]
"""

from __future__ import annotations

import sys
from pathlib import Path


class ContractError(AssertionError):
    """A required S3 ownership control is missing."""


def logical_shell_commands(source: str) -> list[str]:
    """Join backslash-continued shell lines into commands."""
    commands: list[str] = []
    current: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        current.append(line.removesuffix("\\").rstrip())
        if not line.endswith("\\"):
            commands.append(" ".join(current))
            current = []
    if current:
        commands.append(" ".join(current))
    return commands


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def validate_registry_script(source: str) -> None:
    require(
        'DEFAULT_S3_BUCKET="cfn-deploy-agentic-platform-${ACCOUNT_ID}-${REGION}-an"'
        in source,
        "the default bucket must use the S3 account-regional naming suffix",
    )
    require(
        "create_args+=(--bucket-namespace account-regional)" in source,
        "default bucket creation must select the account-regional namespace",
    )
    require(
        'if [[ "${location}" != "${REGION}" ]]' in source,
        "an owned bucket in the wrong Region must be rejected",
    )
    require(
        'if [[ "${BASH_SOURCE[0]}" == "$0" ]]' in source,
        "the script must remain sourceable by the live security test",
    )
    require(
        "ensure_s3_bucket || return $?" in source,
        "template upload must explicitly propagate a rejected owner check",
    )
    require(
        "upload_templates || return $?" in source,
        "deployment must explicitly propagate a failed guarded upload",
    )
    require(
        "cleanup_s3_templates || return $?" in source,
        "destroy must explicitly propagate a failed guarded cleanup",
    )
    require(
        'elif [[ "${delete_error}" == *"BucketNotEmpty"* ]]' in source,
        "cleanup may ignore only BucketNotEmpty, not arbitrary S3 failures",
    )

    guarded_operations = {
        "head-bucket",
        "get-bucket-location",
        "put-public-access-block",
        "put-bucket-encryption",
        "put-object",
        "delete-object",
        "delete-bucket",
    }
    seen: set[str] = set()
    for command in logical_shell_commands(source):
        marker = "aws s3api "
        if marker not in command:
            continue
        aws_command = command[command.index(marker) :]
        fields = aws_command.split()
        require(len(fields) >= 3, f"malformed S3 command: {aws_command}")
        operation = fields[2]
        if operation not in guarded_operations:
            continue
        seen.add(operation)
        require(
            '--expected-bucket-owner "${ACCOUNT_ID}"' in aws_command,
            f"{operation} does not pin ExpectedBucketOwner",
        )

    require(
        seen == guarded_operations,
        f"missing guarded S3 operations: {sorted(guarded_operations - seen)}",
    )
    require(
        not any(
            command.startswith(("aws s3 cp ", "aws s3 rm ")) and "${S3_BUCKET}" in command
            for command in logical_shell_commands(source)
        ),
        "high-level S3 commands cannot carry ExpectedBucketOwner; use s3api",
    )
    require(
        source.index("check_s3_bucket") < source.index("aws s3api put-object"),
        "ownership validation must precede template upload",
    )
    require(
        source.index("aws s3api put-object") < source.index("aws cloudformation create-stack"),
        "templates must be staged through the guarded upload before CloudFormation",
    )


def expect_mutation_failure(label: str, mutated: str) -> None:
    try:
        validate_registry_script(mutated)
    except ContractError:
        return
    raise ContractError(f"mutation survived: {label}")


def run_mutation_checks(source: str) -> None:
    expect_mutation_failure(
        "remove owner condition from HeadBucket",
        source.replace(
            '      --expected-bucket-owner "${ACCOUNT_ID}" \\\n      --region "${REGION}" 2>&1); then',
            '      --region "${REGION}" 2>&1); then',
            1,
        ),
    )
    expect_mutation_failure(
        "remove owner condition from PutObject",
        source.replace(
            '        --expected-bucket-owner "${ACCOUNT_ID}" \\\n        --checksum-algorithm SHA256',
            "        --checksum-algorithm SHA256",
            1,
        ),
    )
    expect_mutation_failure(
        "ignore a rejected owner check",
        source.replace("ensure_s3_bucket || return $?", "ensure_s3_bucket", 1),
    )
    expect_mutation_failure(
        "ignore a failed guarded upload",
        source.replace("upload_templates || return $?", "upload_templates", 1),
    )
    expect_mutation_failure(
        "fall back to the global bucket namespace",
        source.replace("create_args+=(--bucket-namespace account-regional)", ": # namespace removed", 1),
    )
    expect_mutation_failure(
        "remove wrong-Region refusal",
        source.replace(
            'if [[ "${location}" != "${REGION}" ]]',
            'if [[ "${location}" == "${REGION}" ]]',
            1,
        ),
    )


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    script_path = root / "source/module-3a-mcp-registry/deploy-registry.sh"
    source = script_path.read_text(encoding="utf-8")

    validate_registry_script(source)
    run_mutation_checks(source)
    print("S3 owner guard contract: PASS")
    print("Mutation checks killed: 6/6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
