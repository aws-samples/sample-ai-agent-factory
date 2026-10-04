"""Small fail-safe helpers for AWS APIs that expose token pagination."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def list_all(
    client,
    operation: str,
    *,
    item_keys: Iterable[str],
    request: dict,
    request_token: str = "nextToken",
    response_token: str = "nextToken",
    continuation_flag: str | None = None,
) -> list[Any]:
    """Return every item from an AWS list operation.

    ``item_keys`` supports SDK response-name drift (for example ``policies`` vs
    ``items``). A token is followed only when it is a real non-empty string;
    this also prevents an unconfigured ``MagicMock`` from becoming an infinite
    pagination loop in tests. Repeated tokens fail closed rather than looping
    forever or returning an incomplete inventory to a destructive caller.
    """
    keys = tuple(item_keys)
    items: list[Any] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        kwargs = dict(request)
        if token:
            kwargs[request_token] = token
        page = getattr(client, operation)(**kwargs)
        page_items = []
        for key in keys:
            candidate = page.get(key)
            if candidate is not None:
                page_items = candidate or []
                break
        if not isinstance(page_items, list):
            raise RuntimeError(f"{operation} returned a non-list collection under one of {keys!r}")
        items.extend(page_items)
        if continuation_flag is not None and page.get(continuation_flag) is not True:
            return items
        candidate_token = page.get(response_token)
        token = candidate_token if isinstance(candidate_token, str) and candidate_token else None
        if not token:
            if continuation_flag is not None:
                raise RuntimeError(f"{operation} reported {continuation_flag}=true without a usable {response_token}")
            return items
        if token in seen_tokens:
            raise RuntimeError(
                f"{operation} repeated pagination token {token!r}; refusing an incomplete or non-terminating listing"
            )
        seen_tokens.add(token)
