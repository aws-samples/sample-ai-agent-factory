"""Integration-test fixtures for the authenticated deployed product.

The integration suite drives the same API routes as the browser with a real
Cognito access token. Every deployment is registered for cleanup as soon as its
id is available, before the runtime id exists. If ``POST /api/deploy`` is
accepted but its response is lost, the harness recovers the caller-owned row by
the request's unique ``node_id`` before propagating the original failure.
Teardown then waits for the deployment to settle, deletes the exact runtime,
and requires the durable deployment record to reach
``delete_status == "deleted"``.

These tests make real AWS calls through the deployed API. There is no mocking
in this module.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

logger = logging.getLogger(__name__)

# AgentCore launch commonly takes several minutes. Memory-backed teardown can
# also take ~3 minutes by itself, and the API intentionally dispatches that
# cleanup asynchronously.
DEPLOY_TIMEOUT_SECONDS = 20 * 60
POLL_INTERVAL_SECONDS = 15
DELETE_REQUEST_TIMEOUT_SECONDS = 3 * 60
DELETE_VERIFY_TIMEOUT_SECONDS = 12 * 60
DEPLOY_RECOVERY_TIMEOUT_SECONDS = 90
DEPLOY_RECOVERY_POLL_INTERVAL_SECONDS = 2

DEPLOY_TERMINAL_STATES = frozenset({"succeeded", "failed"})
DELETE_TERMINAL_STATES = frozenset(
    {
        "deleted",
        "delete_failed",
        "delete_retained",
    }
)
RETRYABLE_DELETE_HTTP_STATUSES = frozenset({409, 503})


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker used by this real-AWS suite."""

    config.addinivalue_line(
        "markers",
        "integration: performs authenticated calls against a deployed AWS stack",
    )


@pytest.fixture(scope="session")
def aws_region() -> str:
    """Region of the deployed product under test."""

    return os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture(scope="session")
def api_gateway_url() -> str:
    """Base URL for the deployed API Gateway or CloudFront distribution."""

    url = os.environ.get("API_GATEWAY_URL") or os.environ.get("CLOUDFRONT_URL", "")
    if not url:
        pytest.skip("API_GATEWAY_URL or CLOUDFRONT_URL is required")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def api_bearer_token() -> str:
    """Real Cognito access token carrying the integration user's scopes."""

    token = (
        os.environ.get("API_BEARER_TOKEN")
        or os.environ.get("INTEGRATION_BEARER_TOKEN")
        or os.environ.get("COGNITO_ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        pytest.skip(
            "API_BEARER_TOKEN (or INTEGRATION_BEARER_TOKEN/COGNITO_ACCESS_TOKEN) "
            "is required for authenticated integration tests"
        )
    if any(character.isspace() for character in token):
        pytest.fail("The integration bearer token contains whitespace")
    return token


@pytest.fixture(scope="session")
def api_session(
    api_gateway_url: str,
    api_bearer_token: str,
) -> Generator[requests.Session, None, None]:
    """Authenticated HTTP session matching the browser's API boundary."""

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    session.base_url = api_gateway_url  # type: ignore[attr-defined]
    yield session
    session.close()


def _base_url(api_session: requests.Session) -> str:
    value = getattr(api_session, "base_url", "")
    if not isinstance(value, str) or not value:
        raise AssertionError("The integration API session has no base_url")
    return value.rstrip("/")


def _deployment_status(
    api_session: requests.Session,
    deployment_id: str,
) -> dict[str, Any]:
    response = api_session.get(
        f"{_base_url(api_session)}/api/deploy/{deployment_id}",
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise AssertionError(f"Deployment {deployment_id} returned a non-object status body")
    return body


def _wait_for_status(
    api_session: requests.Session,
    deployment_id: str,
    *,
    field_name: str,
    terminal_values: frozenset[str],
    timeout: int,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Poll one deployment field until it reaches an explicit terminal value."""

    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_status = _deployment_status(api_session, deployment_id)
        current = str(last_status.get(field_name) or "")
        logger.info(
            "Deployment %s — %s=%s step=%s",
            deployment_id,
            field_name,
            current or "unset",
            last_status.get("current_step", "n/a"),
        )
        if current in terminal_values:
            return last_status
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Deployment {deployment_id} did not reach a terminal {field_name} "
        f"within {timeout}s. Last status: {last_status}"
    )


def wait_for_deployment_terminal(
    api_session: requests.Session,
    deployment_id: str,
    *,
    timeout: int = DEPLOY_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Wait for deploy success/failure."""

    return _wait_for_status(
        api_session,
        deployment_id,
        field_name="status",
        terminal_values=DEPLOY_TERMINAL_STATES,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def wait_for_delete_terminal(
    api_session: requests.Session,
    deployment_id: str,
    *,
    timeout: int = DELETE_VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Wait for the durable teardown verdict."""

    return _wait_for_status(
        api_session,
        deployment_id,
        field_name="delete_status",
        terminal_values=DELETE_TERMINAL_STATES,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def request_delete_and_verify(
    api_session: requests.Session,
    *,
    deployment_id: str,
    runtime_id: str,
    timeout: int = DELETE_VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Start idempotent runtime teardown and require a ``deleted`` tombstone."""

    deadline = time.monotonic() + timeout
    last_response = ""
    while time.monotonic() < deadline:
        response = api_session.delete(
            f"{_base_url(api_session)}/api/runtime/{runtime_id}",
            timeout=DELETE_REQUEST_TIMEOUT_SECONDS,
        )
        last_response = response.text
        if response.status_code in RETRYABLE_DELETE_HTTP_STATUSES:
            logger.info(
                "Delete of runtime %s is temporarily blocked (%s); retrying",
                runtime_id,
                response.status_code,
            )
            time.sleep(poll_interval)
            continue

        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is not True:
            raise AssertionError(f"DELETE /api/runtime/{runtime_id} did not start safely: {body}")
        break
    else:
        raise TimeoutError(
            f"DELETE /api/runtime/{runtime_id} never started within {timeout}s. Last response: {last_response}"
        )

    remaining = max(1, int(deadline - time.monotonic()))
    status = wait_for_delete_terminal(
        api_session,
        deployment_id,
        timeout=remaining,
        poll_interval=poll_interval,
    )
    if status.get("delete_status") != "deleted":
        raise AssertionError(
            f"Deployment {deployment_id} teardown ended as "
            f"{status.get('delete_status')!r}: "
            f"{status.get('delete_message') or status}"
        )

    # Read the tombstone once more. A transient successful response is not the
    # contract; ``deleted`` must be durable and attributable to this deployment.
    stable = _deployment_status(api_session, deployment_id)
    if stable.get("delete_status") != "deleted":
        raise AssertionError(f"Deployment {deployment_id} lost its deleted tombstone: {stable}")
    return stable


@dataclass
class TrackedDeployment:
    """One product deployment registered for guaranteed teardown."""

    deployment_id: str
    runtime_id: str | None = None
    deleted: bool = False
    last_status: dict[str, Any] = field(default_factory=dict)


class DeploymentCleanupTracker:
    """Registers deployments early and verifies every teardown outcome."""

    def __init__(self, api_session: requests.Session) -> None:
        self._api_session = api_session
        self._records: dict[str, TrackedDeployment] = {}

    @property
    def records(self) -> tuple[TrackedDeployment, ...]:
        return tuple(self._records.values())

    def track(self, deployment_id: str) -> TrackedDeployment:
        if not deployment_id:
            raise AssertionError("Cannot track an empty deployment id")
        return self._records.setdefault(
            deployment_id,
            TrackedDeployment(deployment_id=deployment_id),
        )

    def recover_by_node_id(
        self,
        node_id: str,
        *,
        timeout: int = DEPLOY_RECOVERY_TIMEOUT_SECONDS,
        poll_interval: float = DEPLOY_RECOVERY_POLL_INTERVAL_SECONDS,
    ) -> TrackedDeployment | None:
        """Recover an accepted deploy whose HTTP response was lost.

        The list route is authenticated and returns only the caller's rows.
        ``node_id`` is generated uniquely for each matrix case and persisted
        before the asynchronous execution starts. The user-id GSI is eventually
        consistent, so absence is retried for a short, bounded interval.
        Ambiguity fails closed rather than choosing a deployment to delete.
        """

        if not node_id:
            raise AssertionError("Cannot recover a deployment without its node id")

        deadline = time.monotonic() + timeout
        last_detail = "no list response"
        while True:
            try:
                response = self._api_session.get(
                    f"{_base_url(self._api_session)}/api/deployments",
                    timeout=30,
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, list):
                    raise AssertionError("GET /api/deployments returned a non-list recovery body")
                matches = [item for item in body if isinstance(item, dict) and item.get("node_id") == node_id]
                if len(matches) > 1:
                    ids = [item.get("deployment_id") for item in matches]
                    raise AssertionError(f"Deployment recovery for node_id {node_id!r} is ambiguous: {ids}")
                if matches:
                    deployment_id = matches[0].get("deployment_id")
                    if not isinstance(deployment_id, str) or not deployment_id:
                        raise AssertionError(f"Recovered deployment for node_id {node_id!r} has no deployment_id")
                    logger.warning(
                        "Recovered deployment %s by node_id %s after its POST response was unavailable",
                        deployment_id,
                        node_id,
                    )
                    return self.track(deployment_id)
                last_detail = f"caller deployment list contained no row for node_id {node_id!r}"
            except requests.RequestException as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Deployment recovery list request failed for node_id %s: %s",
                    node_id,
                    last_detail,
                )

            if time.monotonic() >= deadline:
                logger.error(
                    "Could not recover a deployment for node_id %s within %ss: %s",
                    node_id,
                    timeout,
                    last_detail,
                )
                return None
            time.sleep(poll_interval)

    def bind_runtime(
        self,
        record: TrackedDeployment,
        runtime_id: str,
    ) -> None:
        if not runtime_id:
            raise AssertionError(f"Deployment {record.deployment_id} produced an empty runtime id")
        record.runtime_id = runtime_id

    def delete_and_verify(
        self,
        record: TrackedDeployment,
        *,
        timeout: int = DELETE_VERIFY_TIMEOUT_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Delete now; fixture teardown remains a safety net."""

        if record.deleted:
            return record.last_status
        if not record.runtime_id:
            latest = _deployment_status(self._api_session, record.deployment_id)
            record.last_status = latest
            runtime_id = latest.get("runtime_id")
            record.runtime_id = runtime_id if isinstance(runtime_id, str) else None
        if not record.runtime_id:
            raise AssertionError(f"Deployment {record.deployment_id} has no runtime id to delete: {record.last_status}")
        record.last_status = request_delete_and_verify(
            self._api_session,
            deployment_id=record.deployment_id,
            runtime_id=record.runtime_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        record.deleted = True
        return record.last_status

    def cleanup(self, record: TrackedDeployment) -> None:
        """Resolve a possibly in-flight deployment and leave no unverified result."""

        if record.deleted:
            stable = _deployment_status(self._api_session, record.deployment_id)
            if stable.get("delete_status") != "deleted":
                raise AssertionError(
                    f"Deployment {record.deployment_id} was marked cleaned locally "
                    f"but its durable delete_status is "
                    f"{stable.get('delete_status')!r}"
                )
            record.last_status = stable
            return

        status = wait_for_deployment_terminal(
            self._api_session,
            record.deployment_id,
        )
        record.last_status = status
        runtime_id = status.get("runtime_id")
        if not record.runtime_id and isinstance(runtime_id, str):
            record.runtime_id = runtime_id

        delete_status = str(status.get("delete_status") or "")
        if delete_status == "deleting":
            status = wait_for_delete_terminal(
                self._api_session,
                record.deployment_id,
            )
            delete_status = str(status.get("delete_status") or "")
            record.last_status = status
        if delete_status == "deleted":
            record.deleted = True
            return

        # A failed deploy may have finished its automatic cleanup before a
        # runtime existed. Give that finalizer its full verdict window.
        if status.get("status") == "failed" and not record.runtime_id:
            status = wait_for_delete_terminal(
                self._api_session,
                record.deployment_id,
            )
            record.last_status = status
            if status.get("delete_status") != "deleted":
                raise AssertionError(
                    f"Failed deployment {record.deployment_id} could not be cleaned without a runtime id: {status}"
                )
            record.deleted = True
            return

        if not record.runtime_id:
            raise AssertionError(
                f"Deployment {record.deployment_id} reached "
                f"{status.get('status')!r} without a runtime id or a completed "
                f"automatic cleanup: {status}"
            )

        # Retry explicit deletion after a retained/failed automatic attempt.
        # The product delete endpoint is idempotent and preserves the deployment
        # row precisely so an operator/test can make this recovery attempt.
        self.delete_and_verify(record)


@pytest.fixture()
def deployment_cleanup(
    api_session: requests.Session,
) -> Generator[DeploymentCleanupTracker, None, None]:
    """Cleanup safety net that fails the run on any unverified teardown."""

    tracker = DeploymentCleanupTracker(api_session)
    yield tracker

    failures: list[str] = []
    for record in reversed(tracker.records):
        try:
            tracker.cleanup(record)
        except Exception as exc:  # noqa: BLE001 - gather every cleanup failure
            logger.exception(
                "Verified cleanup failed for deployment %s",
                record.deployment_id,
            )
            failures.append(f"{record.deployment_id}: {type(exc).__name__}: {exc}")
    if failures:
        pytest.fail(
            "Integration teardown was not proven complete:\n" + "\n".join(f"  - {failure}" for failure in failures),
            pytrace=False,
        )


@pytest.fixture()
def wait_for_deployment(
    api_session: requests.Session,
) -> Callable[[str, int], dict[str, Any]]:
    """Fixture form of :func:`wait_for_deployment_terminal`."""

    def _poll(
        deployment_id: str,
        timeout: int = DEPLOY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return wait_for_deployment_terminal(
            api_session,
            deployment_id,
            timeout=timeout,
        )

    return _poll
