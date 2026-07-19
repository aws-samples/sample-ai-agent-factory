"""Integration test fixtures for real AWS API calls.

Provides pytest fixtures for AWS credentials, API Gateway URL, deployment
cleanup, polling helpers, and HTTP session management. All fixtures use
real AWS resources — zero mocking.

Requirements: 10.4, 10.5, 10.6
"""

import logging
import os
import time
from collections.abc import Generator

import pytest
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deployment timeout constants
# ---------------------------------------------------------------------------
# Each template deployment can take 10+ minutes (agentcore launch is slow).
DEPLOY_TIMEOUT_SECONDS = 15 * 60  # 15 minutes max per deployment
POLL_INTERVAL_SECONDS = 15  # poll every 15 seconds
DELETE_TIMEOUT_SECONDS = 3 * 60  # 3 minutes for cleanup


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``integration`` marker so pytest doesn't warn about it."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests that perform real AWS API calls (deselect with '-m \"not integration\"')",
    )


# ---------------------------------------------------------------------------
# AWS environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def aws_region() -> str:
    """AWS region sourced from the ``AWS_REGION`` env var (default ``us-east-1``)."""
    return os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture(scope="session")
def api_gateway_url() -> str:
    """Base URL for the deployed API Gateway or CloudFront distribution.

    Reads from ``API_GATEWAY_URL`` first, then falls back to ``CLOUDFRONT_URL``.
    Strips any trailing slash for consistent URL joining.
    """
    url = os.environ.get("API_GATEWAY_URL") or os.environ.get("CLOUDFRONT_URL", "")
    if not url:
        pytest.skip("API_GATEWAY_URL or CLOUDFRONT_URL environment variable is required")
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# HTTP session fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_session(api_gateway_url: str) -> Generator[requests.Session, None, None]:
    """Reusable ``requests.Session`` pre-configured with the base URL.

    The base URL is stored as ``session.base_url`` for convenience.
    The session is closed after all tests complete.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    # Attach base_url as a custom attribute for easy URL building
    session.base_url = api_gateway_url  # type: ignore[attr-defined]
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Deployment cleanup tracker
# ---------------------------------------------------------------------------


@pytest.fixture()
def deployment_cleanup(
    api_session: requests.Session,
    aws_region: str,
) -> Generator[list[dict], None, None]:
    """Track deployed runtimes and clean them up after the test — even on failure.

    Usage inside a test::

        def test_deploy(deployment_cleanup, api_session):
            # ... deploy a runtime ...
            deployment_cleanup.append({
                "runtime_id": "rt-abc123",
                "gateway_config": {...},   # optional
            })
            # test assertions ...
            # cleanup runs automatically in the finally block

    Each entry in the list should be a dict with at least ``runtime_id``.
    Optionally include ``gateway_config`` for gateway resource cleanup.
    """
    tracked: list[dict] = []
    yield tracked

    # --- cleanup runs even if the test raised an exception ---
    for entry in tracked:
        runtime_id = entry.get("runtime_id")
        if not runtime_id:
            continue
        try:
            logger.info("Cleaning up runtime %s", runtime_id)
            base = api_session.base_url  # type: ignore[attr-defined]
            resp = api_session.delete(
                f"{base}/api/runtime/{runtime_id}",
                timeout=DELETE_TIMEOUT_SECONDS,
            )
            if resp.ok:
                logger.info("Cleanup succeeded for %s: %s", runtime_id, resp.json())
            else:
                logger.warning(
                    "Cleanup returned %s for %s: %s",
                    resp.status_code,
                    runtime_id,
                    resp.text,
                )
        except Exception:
            logger.exception("Cleanup failed for runtime %s", runtime_id)


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def wait_for_deployment(api_session: requests.Session) -> callable:
    """Return a helper that polls deployment status until a terminal state.

    Usage::

        status = wait_for_deployment(deployment_id)
        assert status["status"] == "succeeded"

    Raises ``TimeoutError`` if the deployment does not reach a terminal state
    within ``DEPLOY_TIMEOUT_SECONDS``.
    """

    def _poll(deployment_id: str, timeout: int = DEPLOY_TIMEOUT_SECONDS) -> dict:
        base = api_session.base_url  # type: ignore[attr-defined]
        url = f"{base}/api/deploy/{deployment_id}"
        terminal_states = {"succeeded", "failed"}
        deadline = time.monotonic() + timeout

        last_status: dict = {}
        while time.monotonic() < deadline:
            resp = api_session.get(url, timeout=30)
            resp.raise_for_status()
            last_status = resp.json()
            current = last_status.get("status", "")
            logger.info(
                "Deployment %s — status=%s  step=%s",
                deployment_id,
                current,
                last_status.get("current_step", "n/a"),
            )
            if current in terminal_states:
                return last_status
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(
            f"Deployment {deployment_id} did not reach a terminal state within {timeout}s. Last status: {last_status}"
        )

    return _poll


@pytest.fixture()
def wait_for_ready(api_session: requests.Session) -> callable:
    """Return a helper that waits for a runtime endpoint to become reachable.

    Useful after a deployment succeeds — the runtime may need a few extra
    seconds before it starts accepting requests.
    """

    def _poll(endpoint: str, timeout: int = 120) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = api_session.get(endpoint, timeout=10)
                if resp.status_code < 500:
                    return True
            except requests.ConnectionError:
                pass
            time.sleep(5)
        return False

    return _poll
