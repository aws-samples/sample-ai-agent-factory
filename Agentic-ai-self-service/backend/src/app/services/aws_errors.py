"""Small helpers for matching AWS ClientError error codes.

Prefer these over substring checks on ``str(e)`` — an error code embedded in a
nested message (e.g. a wrapped exception quoting another error) should not
change control flow.
"""

from botocore.exceptions import ClientError


def error_code(exc: BaseException) -> str:
    """AWS error code from a ClientError ('' for non-ClientError)."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


def is_error(exc: BaseException, *codes: str) -> bool:
    """True when *exc* is a ClientError whose error code is one of *codes*."""
    return error_code(exc) in codes
