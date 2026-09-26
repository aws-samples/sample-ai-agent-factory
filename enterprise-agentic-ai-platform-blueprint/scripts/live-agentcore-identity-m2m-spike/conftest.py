"""Path bootstrap for the AgentCore Identity M2M spike test modules.

The spike scripts are standalone executables rather than an installed package,
so they import each other by module name. This conftest puts this spike's
directory *and* the sibling gateway-spike directory on ``sys.path`` -- the
latter because the identity spike reuses the shared helpers in ``gateway_spike``
(``SpikeError``, ``Evidence``, ``JsonStore``, ``aws_error_code``, ``utc_now``),
mirroring the sibling Runtime+Memory spike's conftest. It makes no AWS calls at
import or collection time.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import sys
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_GATEWAY_SPIKE_DIR = _SPIKE_DIR.parent / "live-agentcore-gateway-spike"
for _path in (_SPIKE_DIR, _GATEWAY_SPIKE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
