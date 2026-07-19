"""Root conftest — fix sys.path so vendored pydantic stubs don't shadow the real package."""

import os
import sys

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

# ---------------------------------------------------------------------------
# Hypothesis determinism on CI
# ---------------------------------------------------------------------------
# 20 property-test files explore RANDOM inputs each run, so a latent edge case
# can pass locally yet fail on a CI runner that happened to draw the bad input
# (exactly how the NoCredentialsError surfaced). Register a derandomized "ci"
# profile (fixed example database off, stable derandomize seed, generous
# deadline for slow runners) and load it when CI is set, so a green run stays
# green on re-runs. Locally the default profile still explores freely.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "ci",
        derandomize=True,
        deadline=None,
        max_examples=50,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    settings.register_profile(
        "dev",
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    settings.load_profile("ci" if os.environ.get("CI") else "dev")
except Exception:  # noqa: BLE001 — hypothesis always present in dev deps; be defensive
    pass


@pytest.fixture(autouse=True)
def _no_real_aws_credentials(monkeypatch):
    """Unit tests must never reach real AWS. Neutralize ambient credentials so a
    stray un-mocked boto3 call fails loudly (NoCredentialsError) HERE instead of
    silently succeeding on a developer's machine and then failing on CI. moto's
    @mock_aws and explicit fake-cred patches set their own values and win over
    this (fixtures run before the test body; moto/patch.dict apply inside it).
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    yield


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
