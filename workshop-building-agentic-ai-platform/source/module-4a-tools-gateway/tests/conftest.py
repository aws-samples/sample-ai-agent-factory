# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared test fixtures for the Module 4 AgentCore Gateway test suite."""

import os
import sys
from pathlib import Path

import pytest

# Add module root to path so handlers/services are importable
MODULE_ROOT = Path(__file__).resolve().parent.parent
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))


@pytest.fixture
def aws_credentials():
    """Mock AWS credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"  # nosec B105
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"  # nosec B105
    os.environ["AWS_SECURITY_TOKEN"] = "testing"  # nosec B105
    os.environ["AWS_SESSION_TOKEN"] = "testing"  # nosec B105
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
