"""CloudFormation Custom Resource response signaling.

Sends SUCCESS/FAILED responses back to the CloudFormation pre-signed URL
so the stack can proceed or roll back.

Delivery is best-effort and never raises. Failing to deliver a response is the
worst outcome available here: CloudFormation waits out the full one-hour custom
resource timeout before it gives up, so the stack sits wedged in
CREATE_IN_PROGRESS and cannot be updated or deleted in the meantime. Retrying and
swallowing beats propagating, so ``send`` returns a bool instead of throwing.
"""

import json
import logging
import time
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SUCCESS = "SUCCESS"
FAILED = "FAILED"

# CloudFormation rejects a Reason longer than 1024 bytes, and rejecting the
# response is indistinguishable from never sending one — the stack hangs for an
# hour either way. Truncate below the limit rather than risk it.
MAX_REASON_LENGTH = 1000

_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = (1, 3, 7)


def send(
    event: dict,
    context,
    status: str,
    data: dict | None = None,
    physical_resource_id: str | None = None,
    reason: str | None = None,
) -> bool:
    """Send a response to the CloudFormation pre-signed S3 URL.

    Returns True when CloudFormation acknowledged the response. Never raises:
    see the module docstring for why a raised exception here is worse than a
    False return.
    """
    log_stream = getattr(context, "log_stream_name", "N/A")
    fallback_reason = f"See CloudWatch Log Stream: {log_stream}"
    if reason:
        reason = reason[:MAX_REASON_LENGTH]
        if len(fallback_reason) + 2 <= MAX_REASON_LENGTH:
            # Always point at the log stream. The caller's reason is deliberately
            # redacted (see handler._safe_failure_reason), so the log stream is
            # the only route to the actual error detail.
            reason = f"{reason[: MAX_REASON_LENGTH - len(fallback_reason) - 2]}. {fallback_reason}"
    else:
        reason = fallback_reason

    response_body = {
        "Status": status,
        "Reason": reason,
        "PhysicalResourceId": physical_resource_id or event.get("LogicalResourceId", ""),
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data or {},
    }

    body = json.dumps(response_body).encode("utf-8")
    url = event.get("ResponseURL", "")
    # The ResponseURL is a CloudFormation-issued pre-signed S3 URL, but enforce
    # https anyway so a forged event can't make urlopen dereference file:// etc.
    # Returning False rather than raising: with no usable URL there is no way to
    # respond at all, and throwing from here would only mask that in the caller.
    if not url.startswith("https://"):
        logger.error("ResponseURL is missing or not https — cannot signal CloudFormation")
        return False

    # No part of the ResponseURL is logged, not even with the query string stripped.
    # That query string IS the credential — anyone holding it can PUT this resource's
    # response and force the stack to see SUCCESS or FAILED — and an earlier version
    # of this line logged ``url.split("?", 1)[0]``, which is correct today and one
    # careless edit from not being: the redaction lives at the call site, so anyone
    # adding ``url`` to this format string defeats it silently and no test notices.
    # ARCC guidance on log hygiene is categorical — credentials and secrets must never
    # reach application logs, and neither may exception messages carrying them — so the
    # value is kept out of the sink entirely rather than sanitized on the way in.
    #
    # Nothing diagnostic is lost. The host is always a CloudFormation-owned regional S3
    # endpoint, and the path is the stack ARN plus logical id plus request id, all of
    # which the handler already logs by name. What a delivery failure actually needs is
    # below: which status failed, how many attempts, and the exception type.
    logger.info("Sending %s (physical_id=%s)", status, physical_resource_id)

    req = Request(url, data=body, method="PUT")
    req.add_header("Content-Type", "")
    req.add_header("Content-Length", str(len(body)))

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with (
                urlopen(req) as resp
            ):  # nosemgrep: dynamic-urllib-use-detected -- URL from CloudFormation ResponseURL (AWS-controlled, not user input)
                logger.info("CFN response status: %s", resp.status)
            return True
        except Exception as e:  # noqa: BLE001
            # Log the type, not the message: a urllib error can echo the
            # pre-signed URL, whose query string is a valid S3 credential for
            # this response.
            if attempt == _MAX_ATTEMPTS:
                logger.error(
                    "Failed to send CFN %s response after %d attempts (%s). "
                    "CloudFormation will now wait out the custom-resource timeout.",
                    status,
                    _MAX_ATTEMPTS,
                    type(e).__name__,
                )
                return False
            delay = _BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                "CFN response attempt %d/%d failed (%s); retrying in %ds",
                attempt,
                _MAX_ATTEMPTS,
                type(e).__name__,
                delay,
            )
            time.sleep(delay)

    return False
