"""One-time logging configuration for every Lambda that packages this backend.

The AWS Lambda Python runtime leaves the **root logger at WARNING**. Every module in
this package creates its logger as ``logging.getLogger(__name__)`` and, apart from
``services/cfn_provider/handler.py``, none of them call ``setLevel``. A module logger
left at ``NOTSET`` inherits the root's WARNING, so every ``logger.info`` call in the
package fails ``isEnabledFor`` and returns *before any handler is consulted* -- the
record is not filtered out, it is never created.

Measured live rather than inferred: after a deploy of ``acfe2e-p0920`` that provably
deleted a Cognito app client, the ``/aws/lambda/acfe2e-p0920-step-gateway`` log group
held 17 ``[WARNING]`` records, 4 ``[ERROR]`` records and **zero** ``[INFO]`` records.
Counting the level markers in a real log group is the whole diagnosis; no second
logger to compare against is needed.

That silence is an audit gap, not merely missing debug output. ARCC guidance on
security-relevant logging (``cnt_6yTkcrHkEBKA7u``) requires event records for
"management of security principals", for permission changes, and for "the success or
failure of the attempt" -- and it requires logs to be produced "in all lifecycle
states: not just during normal operation, but also ... during provisioning". The
lines that were silent are exactly those events: sharing a workflow with another
principal, promoting a version into the production slot, rolling production back,
deciding a human-in-the-loop request, creating and deleting triggers, and deleting a
Cognito user pool. Those format strings already carry ``owner=`` and ``caller=``
fields; they were written as an audit trail and never reached CloudWatch. Only the
*refusals* beside them, logged at WARNING, were visible -- so the record showed the
cases where nothing happened and was silent on the cases where something did.

Two deliberate choices, both of which matter:

**The level is set on the ``app`` package logger, not on the root logger.** Every
logger in this backend is ``app.*`` and inherits from it, while ``boto3``,
``botocore`` and ``urllib3`` keep the runtime's WARNING. Raising the root instead
would switch on botocore's INFO chatter for no benefit, and a ``LOG_LEVEL=DEBUG`` on
the root would make botocore log the headers and bodies of every AWS call -- signing
material and request payloads. Scoping to ``app`` makes DEBUG a safe thing for an
operator to ask for: every ``logger.debug`` call in this package was audited and the
only one that logs a URI routes it through ``_safe_log_token``.

**Setting the parent's level alone is sufficient.** ``Logger.callHandlers`` walks the
ancestor chain and consults each *handler's* level; it never re-checks an ancestor
*logger's* level. So a record created here because ``app`` is at INFO still reaches
the Lambda runtime's root handler and is emitted, even though the root logger itself
is at WARNING. No handler is added here for that reason -- adding one would duplicate
every line in Lambda and fight ``caplog`` under pytest.
"""

import logging
import os

#: Every logger in this package is a child of this one.
PACKAGE_LOGGER = "app"

#: Chosen so that the audit records above are emitted by default. An operator who
#: needs to trade log volume for noise can set ``LOG_LEVEL`` on the function.
DEFAULT_LEVEL = "INFO"

_LEVEL_ENV_VAR = "LOG_LEVEL"


def resolve_level(raw: str | None) -> int:
    """Map a ``LOG_LEVEL`` string to a level, falling back to :data:`DEFAULT_LEVEL`.

    Unrecognised values fall back rather than raising: a typo in an environment
    variable must not take the function down at import time, and it must not
    silently mean "log nothing" either.
    """
    name = (raw or "").strip().upper()
    if not name:
        return logging.getLevelName(DEFAULT_LEVEL)
    level = logging.getLevelName(name)
    # getLevelName returns the string "Level %s" for anything it does not know.
    if not isinstance(level, int):
        return logging.getLevelName(DEFAULT_LEVEL)
    return level


def configure_logging() -> int:
    """Set the level on the ``app`` package logger. Idempotent; returns the level."""
    level = resolve_level(os.environ.get(_LEVEL_ENV_VAR))
    logging.getLogger(PACKAGE_LOGGER).setLevel(level)
    return level
