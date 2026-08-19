"""Unit test — smoke harness must be a no-op when AWS creds are absent.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    'smoke_harness', os.path.join(os.path.dirname(__file__), 'smoke.py')
)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def test_no_aws_creds_returns_zero(monkeypatch):
    monkeypatch.delenv('AWS_ACCESS_KEY_ID', raising=False)
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.delenv('AWS_SESSION_TOKEN', raising=False)
    assert smoke.main() == 0
