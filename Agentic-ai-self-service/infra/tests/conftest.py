"""Pytest configuration for CDK stack tests.

Ensures tests run from the infra/ directory so that:
1. Relative Docker asset paths (e.g., ``../backend``) resolve correctly
2. The ``stacks`` package is importable
"""

import os
import sys

# Resolve the infra/ directory (parent of tests/)
_INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure stacks package is importable regardless of where pytest is invoked
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)

# Change to infra/ so ContainerImage.from_asset("../backend") resolves
os.chdir(_INFRA_DIR)
