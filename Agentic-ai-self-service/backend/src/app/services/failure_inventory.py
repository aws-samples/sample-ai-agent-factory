"""Manifest rows a failing step could not append, carried out through its own exception.

A step that fails raises, and the state machine's Catch keeps only the step's INPUT plus
``error_info`` (``{"Error", "Cause"}``). Anything the step built in memory is gone, so a
manifest row whose DynamoDB append failed is named nowhere: not in ``created_resources``,
not in the event, and the resource it describes is orphaned for good. Writing it to a
second attribute of the same item does not help, because the reasons an append fails (an
exhausted transport retry, the 400KB item limit) fail that write too.

The one channel that survives is the Catch's ``Cause``, so the step appends the rows to
its exception message, and failure finalization reads them back and strips them before
the text is logged or stored. The rows are manifest rows: ids, names and regions, never a
secret value. They grant nothing either: every delete they reach still passes the
dispatchers' live ownership checks.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re

logger = logging.getLogger(__name__)

MARKER = "unrecorded-manifest-rows:"

#: The gateway-name claim lease the failed step left for failure cleanup to take over
#: (F-66f): its unrecorded rows name a gateway graph, so the name must not come free
#: between the step and the cleanup. A fence, not a credential: it only re-takes a
#: lease the failed invocation held, and every delete still proves ownership live.
TOKEN_MARKER = "gateway-name-claim-token:"

# base64url survives both the raw Cause (a JSON document with errorMessage escaped inside
# it) and the parsed message unchanged, so one pattern finds and strips it in either.
_PATTERN = re.compile(r"(?:\\n|\n)?" + re.escape(MARKER) + r"([A-Za-z0-9_-]+=*)")
_TOKEN_PATTERN = re.compile(r"(?:\\n|\n)?" + re.escape(TOKEN_MARKER) + r"([0-9a-f]{32})\b")


class StepFailedWithUnrecordedRows(RuntimeError):
    """A step failure whose message carries the manifest rows it could not append,
    and the name-claim lease it handed to failure cleanup, if any."""

    def __init__(self, message: str, rows: list[dict], *, claim_token: str | None = None):
        self.rows = [dict(r) for r in rows]
        self.claim_token = claim_token
        encoded = base64.urlsafe_b64encode(json.dumps(self.rows, sort_keys=True, default=str).encode()).decode()
        text = f"{message}\n{MARKER}{encoded}" if self.rows else message
        if claim_token:
            if not re.fullmatch(r"[0-9a-f]{32}", claim_token):
                raise ValueError("a claim token is 32 lower-case hex characters")
            text += f"\n{TOKEN_MARKER}{claim_token}"
        super().__init__(text)


class GatewayRefusedBeforeSideEffects(StepFailedWithUnrecordedRows):
    """The gateway step refused before its first side effect, so it created nothing.

    The Catch reports the class name as ``errorType``, and failure cleanup reads that
    name, not the message, to accept an empty manifest as complete. Renaming the
    class breaks that link, which ``test_f67_failure_before_runtime.py`` pins.
    """

    def __init__(self, message: str):
        super().__init__(message, [])


def strip(text: str) -> str:
    """The failure text without the carried rows or token: what is logged and shown."""
    return _TOKEN_PATTERN.sub("", _PATTERN.sub("", text))


def claim_token_from_error_info(error_info) -> str | None:
    """The claim lease a failed gateway step handed to failure cleanup, if any."""
    text = str((error_info.get("Cause") if isinstance(error_info, dict) else error_info) or "")
    found = _TOKEN_PATTERN.findall(text)
    return found[-1] if found else None


def rows_from_error_info(error_info) -> list[dict]:
    """The rows a Catch payload carries, or [] when it carries none or they are unreadable."""
    if isinstance(error_info, dict):
        text = str(error_info.get("Cause") or "")
    else:
        text = str(error_info or "")
    rows: list[dict] = []
    for encoded in _PATTERN.findall(text):
        try:
            decoded = json.loads(base64.urlsafe_b64decode(encoded.encode()))
        except (binascii.Error, ValueError):
            # Truncated (a Cause is capped at 32768 characters) or corrupted. Say so:
            # the rows it carried are now named nowhere.
            logger.warning("A failed step's unrecorded manifest rows could not be read back")
            continue
        for row in decoded if isinstance(decoded, list) else []:
            if (
                isinstance(row, dict)
                and isinstance(row.get("type"), str)
                and (row.get("id") or row.get("name"))
                and isinstance(row.get("created_by_deployment"), bool)
            ):
                rows.append(row)
    return rows
