"""The backend's ``logger.info`` calls must actually be emitted in Lambda.

The defect these tests pin was measured on a real deployment, not inferred: after a
deploy of ``acfe2e-p0920`` that provably deleted a Cognito app client, the
``/aws/lambda/acfe2e-p0920-step-gateway`` log group held 17 ``[WARNING]``, 4
``[ERROR]`` and **zero** ``[INFO]`` records. The AWS Lambda runtime leaves the root
logger at WARNING and every module here builds its logger with
``logging.getLogger(__name__)`` and no ``setLevel``, so all 255 ``logger.info`` calls
in the package failed ``isEnabledFor`` and no record was ever created.

**These tests must not use ``caplog.set_level`` on the logger under test.** That sets
a level on the very logger whose level *is* the defect, so the suite would configure
away what it is measuring and pass in both states. Instead each test reconstructs
Lambda's condition -- root at WARNING, the ``app`` logger's level left to the code
under test -- and attaches a plain handler to root to observe what arrives. That
handler placement is also the point: ``Logger.callHandlers`` consults each *handler's*
level as it walks the ancestor chain and never re-checks an ancestor *logger's* level,
which is why setting the level on the ``app`` parent alone is sufficient.
"""

import importlib
import logging

import pytest
from app.logging_config import (
    DEFAULT_LEVEL,
    PACKAGE_LOGGER,
    configure_logging,
    resolve_level,
)

# A logger name that does not exist as a module, so nothing else in the suite can have
# configured it. It is still a child of ``app`` and therefore inherits from it exactly
# as every real module logger does.
PROBE_LOGGER = "app.services.__logging_probe__"


class _Collector(logging.Handler):
    """Records everything handed to it. Level 0 so the handler filters nothing."""

    def __init__(self) -> None:
        super().__init__(level=0)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def lambda_logging():
    """Reconstruct the Lambda runtime's logging state, and restore it afterwards.

    Yields the collector attached to root. The ``app`` logger's level is reset to
    NOTSET on entry so that a previous import in this process cannot make a broken
    implementation look fixed.
    """
    root = logging.getLogger()
    pkg = logging.getLogger(PACKAGE_LOGGER)
    probe = logging.getLogger(PROBE_LOGGER)

    saved_root_level = root.level
    saved_pkg_level = pkg.level
    saved_probe_level = probe.level
    collector = _Collector()

    root.setLevel(logging.WARNING)  # what the AWS Lambda Python runtime leaves behind
    pkg.setLevel(logging.NOTSET)
    probe.setLevel(logging.NOTSET)
    root.addHandler(collector)
    try:
        yield collector
    finally:
        root.removeHandler(collector)
        root.setLevel(saved_root_level)
        pkg.setLevel(saved_pkg_level)
        probe.setLevel(saved_probe_level)


def test_an_info_record_from_an_app_logger_reaches_a_handler_under_lambda_conditions(
    lambda_logging,
):
    """The whole defect in one assertion.

    With root at WARNING and the module logger at NOTSET -- the deployed state -- an
    INFO call must still produce a record. Revert ``configure_logging`` to a no-op and
    this fails, because the record is never created.
    """
    probe = logging.getLogger(PROBE_LOGGER)

    assert not probe.isEnabledFor(logging.INFO), (
        "precondition: the probe logger must start in the broken state, or this test cannot tell the two states apart"
    )

    configure_logging()

    assert probe.isEnabledFor(logging.INFO)
    probe.info("a security-relevant success line")
    assert [r.getMessage() for r in lambda_logging.records] == ["a security-relevant success line"]


def test_importing_the_package_is_enough(lambda_logging):
    """No entrypoint has to remember to call it.

    The call lives in ``app/__init__.py``, so importing any module in the package
    configures logging. This reloads that module against the reset state to prove the
    import itself does the work -- pinning the placement, not just the function.
    """
    probe = logging.getLogger(PROBE_LOGGER)
    assert not probe.isEnabledFor(logging.INFO)

    importlib.reload(importlib.import_module("app"))

    assert probe.isEnabledFor(logging.INFO), (
        "importing `app` must configure logging; if the configure_logging() call is "
        "removed from app/__init__.py, every entrypoint silently loses its INFO logs"
    )


def test_the_root_logger_is_left_alone(lambda_logging):
    """Scoping to ``app`` is a security property, not a style preference.

    Raising the root instead would enable ``botocore``'s INFO, and ``LOG_LEVEL=DEBUG``
    on the root would make botocore log the headers and bodies of every AWS call --
    signing material and request payloads.
    """
    configure_logging()

    assert logging.getLogger().level == logging.WARNING, "configure_logging must not touch root"
    assert not logging.getLogger("botocore").isEnabledFor(logging.INFO)
    assert not logging.getLogger("urllib3").isEnabledFor(logging.INFO)


def test_debug_is_safe_to_ask_for(lambda_logging, monkeypatch):
    """``LOG_LEVEL=DEBUG`` must reach this package and stop there."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    configure_logging()

    assert logging.getLogger(PROBE_LOGGER).isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("botocore").isEnabledFor(logging.DEBUG), (
        "botocore at DEBUG logs request headers and bodies for every AWS call"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, logging.INFO),
        ("", logging.INFO),
        ("   ", logging.INFO),
        ("INFO", logging.INFO),
        ("info", logging.INFO),
        ("  Debug  ", logging.DEBUG),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
        # A typo must not mean "log nothing", and must not raise at import time and
        # take the function down before it can report why.
        ("VERBOSE", logging.INFO),
        ("20", logging.INFO),
    ],
)
def test_resolve_level(raw, expected):
    assert resolve_level(raw) == expected


def test_the_default_is_a_level_that_emits_the_audit_lines(lambda_logging, monkeypatch):
    """With no ``LOG_LEVEL`` set -- how every function is deployed today -- INFO wins.

    ARCC guidance on security-relevant logging (cnt_6yTkcrHkEBKA7u) requires records
    for permission changes and for the success of an attempt, and the lines carrying
    them here are INFO. A default of WARNING would satisfy the unit test above while
    leaving production exactly as silent as it was.
    """
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert resolve_level(None) <= logging.INFO
    assert configure_logging() <= logging.INFO
    assert logging.getLogger(PACKAGE_LOGGER).level == logging.getLevelName(DEFAULT_LEVEL)
