"""Path bootstrap for the generated-agent spike test modules.

The reference agent lives in the ``agent/`` subdirectory (it is copied verbatim
into the container image), so tests import it by putting that directory on
``sys.path``. This conftest makes no AWS calls and imports no Strands/litellm at
collection time — the pure core is exercised with fakes.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import sys
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_AGENT_DIR = _SPIKE_DIR / "agent"
for _path in (_AGENT_DIR, _SPIKE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
