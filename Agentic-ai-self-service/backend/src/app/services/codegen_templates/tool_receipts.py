"""Tool-use receipts (DEPLOYED-CODE TEMPLATE, not app code).

A generated agent returns one receipt per tool call its own loop executed during the
invocation, read from the Converse-format conversation the loop itself wrote: each
``toolUse`` block the model emitted and the ``toolResult`` the loop produced for it.
The model's reply text cannot forge a receipt, so a caller can tell a tool the runtime
really ran from a tool result the model invented.

Argument VALUES never leave the runtime; only SHA-256 digests of them do, so a receipt
carries no more of the caller's data than the digest of what the caller already sent.

Embedded into generated agent code via a plain ``str.replace`` marker (see
``code_generator._TOOL_RECEIPTS_BLOCK``), never inside an f-string.
"""

import hashlib
import json

_RECEIPT_LIMIT = 64
_ARGUMENT_LIMIT = 32
_RECEIPT_STATUSES = ("success", "error", "missing")


def _receipt_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _tool_blocks(messages):
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                yield block


def _tool_use_ids(messages):
    """The toolUse ids already present in a conversation, so a later call can skip them."""
    ids = set()
    for block in _tool_blocks(messages):
        use = block.get("toolUse")
        if isinstance(use, dict) and use.get("toolUseId"):
            ids.add(use["toolUseId"])
    return ids


def _tool_result_status(known_tool, result_text):
    """Status of a hand-rolled loop's tool call: an unknown tool or a structured error fails."""
    if not known_tool:
        return "error"
    try:
        parsed = json.loads(result_text)
    except (TypeError, ValueError):
        return "success"
    if isinstance(parsed, dict) and parsed.get("error"):
        return "error"
    return "success"


def _tool_receipts(messages, exclude=(), statuses=None, names=None):
    """Receipts for the tool calls in *messages* whose toolUse id is not in *exclude*.

    *statuses* maps a toolUse id to the status a hand-rolled loop decided; a framework
    loop (Strands) records the status on its own toolResult block instead. *names* maps
    a model-facing alias back to the name the tool was published under (a gateway name
    fitted to Bedrock's 64-character cap), so a receipt reports the published name.
    """
    uses = []
    results = {}
    for block in _tool_blocks(messages):
        use = block.get("toolUse")
        if isinstance(use, dict) and use.get("toolUseId") and use["toolUseId"] not in exclude:
            uses.append(use)
        result = block.get("toolResult")
        if isinstance(result, dict) and result.get("toolUseId"):
            results[result["toolUseId"]] = result
    receipts = []
    for use in uses[:_RECEIPT_LIMIT]:
        arguments = use.get("input") if isinstance(use.get("input"), dict) else {}
        use_id = use["toolUseId"]
        if statuses and use_id in statuses:
            status = statuses[use_id]
        elif use_id in results:
            status = results[use_id].get("status") or "success"
        else:
            status = "missing"
        name = str(use.get("name") or "")
        receipts.append(
            {
                "name": str((names or {}).get(name, name))[:256],
                "status": status if status in _RECEIPT_STATUSES else "error",
                "input_sha256": _receipt_digest(arguments),
                "argument_sha256": {
                    str(key)[:128]: _receipt_digest(value) for key, value in list(arguments.items())[:_ARGUMENT_LIMIT]
                },
            }
        )
    return receipts
