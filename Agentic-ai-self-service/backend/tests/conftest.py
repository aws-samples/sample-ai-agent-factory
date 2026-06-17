"""Root conftest — fix sys.path so vendored pydantic stubs don't shadow the real package."""

import sys
import os

# The backend/ directory contains vendored pydantic/pydantic_core stubs for Lambda
# packaging. These are pure-Python stubs without the compiled _pydantic_core extension.
# When pytest runs from backend/, '' (cwd) in sys.path picks up these stubs instead
# of the real pydantic from site-packages. Fix by removing the backend dir from path.
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_dir = os.path.join(_backend_dir, "src")

# Remove backend dir entries that would shadow site-packages
sys.path = [
    p
    for p in sys.path
    if p == _src_dir  # keep src/
    or "site-packages" in p  # keep venv packages
    or (p and not os.path.samefile(p, _backend_dir) if os.path.isdir(p) and p else True)
]

# Ensure src/ is on the path
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


# ---------------------------------------------------------------------------
# moto cross-file isolation
# ---------------------------------------------------------------------------
# When many test files each open their own `mock_aws()` context across one
# pytest process, boto3's module-level DEFAULT_SESSION gets cleared on teardown
# and a LATER file's `mock_aws()` setup raises
#   AttributeError: <module 'boto3'> does not have the attribute 'DEFAULT_SESSION'
# (moto patches boto3.DEFAULT_SESSION, but if a prior context deleted the attr
# the patch target is gone). Each file passes alone / pairwise; the failure only
# appears in the full Phase-3 suite. This is a test-runner ordering artifact,
# NOT a product defect. Defensively ensure the attribute exists before each test
# so moto always has a patch target. See tasks/lessons.md (Phase 3 integration).
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_boto3_default_session():
    try:
        import boto3

        if not hasattr(boto3, "DEFAULT_SESSION"):
            boto3.DEFAULT_SESSION = None
    except Exception:
        pass
    yield
    try:
        import boto3

        if not hasattr(boto3, "DEFAULT_SESSION"):
            boto3.DEFAULT_SESSION = None
    except Exception:
        pass
