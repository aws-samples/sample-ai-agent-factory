"""Agent-side tool adapter (DEPLOYED-CODE TEMPLATE, not app code).

``_tool_safe`` bridges the canonical tool implementations (which raise
``ToolUnavailable`` on hard failure) into generated agent code, where tool
handlers must always RETURN a string for the model's toolResult block.
"""

import json


def _tool_safe(fn, *args):
    """Invoke a tool implementation, converting raised errors to a JSON error string."""
    try:
        return fn(*args)
    except Exception as e:
        return json.dumps({"error": str(e)})
