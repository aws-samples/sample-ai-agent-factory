"""CloudFormation Custom Resource response signaling.

Sends SUCCESS/FAILED responses back to the CloudFormation pre-signed URL
so the stack can proceed or roll back.
"""

import json
import logging
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(
    event: dict,
    context,
    status: str,
    data: dict | None = None,
    physical_resource_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Send a response to the CloudFormation pre-signed S3 URL."""
    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {getattr(context, 'log_stream_name', 'N/A')}",
        "PhysicalResourceId": physical_resource_id or event.get("LogicalResourceId", ""),
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data or {},
    }

    body = json.dumps(response_body).encode("utf-8")
    url = event["ResponseURL"]

    logger.info("Sending %s to %s (physical_id=%s)", status, url[:80], physical_resource_id)

    req = Request(url, data=body, method="PUT")
    req.add_header("Content-Type", "")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urlopen(req) as resp:  # nosemgrep: dynamic-urllib-use-detected -- URL from CloudFormation ResponseURL (AWS-controlled, not user input)
            logger.info("CFN response status: %s", resp.status)
    except Exception:
        logger.exception("Failed to send CFN response")
        raise
