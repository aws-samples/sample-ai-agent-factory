"""Path bootstrap for the live-spike test modules.

The spike scripts are standalone executables rather than an installed package,
so they import each other by module name. This conftest puts their directory on
``sys.path`` the same way ``tests/adversarial/conftest.py`` does for its harness.
It makes no AWS calls at import or collection time.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import sys
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
if str(_SPIKE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPIKE_DIR))
