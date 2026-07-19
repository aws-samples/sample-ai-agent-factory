"""AI Tool Generator service — uses Claude Sonnet on Bedrock to generate Lambda tool code.

Accepts a natural language description and returns a complete Lambda function
with tool name, description, input schema, and Python handler code.

Requirements: Phase 3 (AI Tool Generator)
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

TOOL_GENERATOR_MODEL_ID = os.environ.get(
    "TOOL_GENERATOR_MODEL_ID",
    # Bedrock flagged the dated Sonnet 4 (May 2025) IDs as Legacy in 2026-Q2.
    # Use the current date-less generation. See tasks/lessons.md Bug 26.
    "us.anthropic.claude-sonnet-5",
)


def _inference_config(model_id: str, max_tokens: int, temperature: float) -> dict:
    """Converse inferenceConfig omitting temperature for models that reject it.
    Claude Sonnet 5 / Opus 5 and later raise ValidationException
    'temperature is deprecated for this model'."""
    cfg: dict = {"maxTokens": max_tokens}
    mid = (model_id or "").lower()
    if not any(
        m in mid for m in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-5", "claude-fable", "claude-mythos")
    ):
        cfg["temperature"] = temperature
    return cfg


# Compact prompt for clarification mode — fast, low tokens
CLARIFICATION_PROMPT = """You help users create AWS Lambda tools. Ask 2-4 clarifying questions about their request.
Return ONLY: {"responseType": "clarification", "message": "your questions here"}
No markdown. No text outside JSON."""

# Full prompt for generation mode — includes all rules
GENERATION_PROMPT = """Generate an AWS Lambda tool based on the conversation. Return ONLY valid JSON with NO markdown fences.

RULES:
- lambda_handler(event, context) entry point. Return: {"statusCode":200,"body":json.dumps(result)}
- The handler MUST support two invocation modes using this EXACT detection pattern:
    import json
    def lambda_handler(event, context):
        try:
            custom = context.client_context.custom
            if isinstance(custom, str):
                custom = json.loads(custom)
            tool_name = custom.get('bedrockAgentCoreToolName', '')
            params = event  # Gateway mode: inputs are top-level event keys
        except (AttributeError, TypeError):
            tool_name = event.get('toolName', '')
            params = event.get('input', {})
        # Then read ALL input parameters from `params`, e.g. params.get("query", "")
- Only stdlib + urllib + boto3. No pip packages.
- For APIs needing auth: accept token as input param, include mock fallback for tests.
- Include 2-3 test cases with mock-safe inputs.
- tool MUST have ALL 5 keys: toolName, displayName, description, lambdaCode, inputSchema.
- If previous messages show test failures, fix the code based on the errors.
- Keep code concise with error handling."""


def generate_tool(
    prompt: str,
    conversation_history: list[dict] | None = None,
    existing_tool: dict | None = None,
    region: str = "us-east-1",
) -> dict:
    """Generate a Lambda tool using Claude Sonnet on Bedrock.

    Args:
        prompt: Natural language description of the desired tool.
        conversation_history: Previous messages for multi-turn refinement.
        existing_tool: Existing tool dict to modify/refine.
        region: AWS region for the Bedrock client.

    Returns:
        Dict with keys: success, tool (dict or None), message, error (str or None).
    """
    try:
        client = boto3.client("bedrock-runtime", region_name=region)

        # Build messages for the Converse API (limit to last 4 for speed)
        messages = []
        history = (conversation_history or [])[-4:]

        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": [{"text": content}]})

        # Build the current user message
        user_text = prompt
        if existing_tool:
            user_text += (
                f"\n\nHere is the existing tool to refine:\n```json\n{json.dumps(existing_tool, indent=2)}\n```"
            )

        messages.append({"role": "user", "content": [{"text": user_text}]})

        # Select prompt: use GENERATION if we have conversation history
        # (user already answered clarifying questions) or an existing tool to refine.
        # Use CLARIFICATION for the first message only.
        has_prior_context = len(history) > 0 or existing_tool is not None
        system_prompt = GENERATION_PROMPT if has_prior_context else CLARIFICATION_PROMPT

        if has_prior_context:
            # Generation mode: use tool_use (function calling) for reliable structured output.
            # This avoids the LLM breaking JSON with literal newlines in lambdaCode.
            tool_config = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": "submit_tool",
                            "description": "Submit the generated Lambda tool with code and test cases.",
                            "inputSchema": {
                                "json": {
                                    "type": "object",
                                    "properties": {
                                        "message": {
                                            "type": "string",
                                            "description": "Brief description of the tool",
                                        },
                                        "toolName": {
                                            "type": "string",
                                            "description": "snake_case tool name",
                                        },
                                        "displayName": {
                                            "type": "string",
                                            "description": "Human-readable name",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "One sentence description",
                                        },
                                        "lambdaCode": {
                                            "type": "string",
                                            "description": "Complete Python lambda_handler code",
                                        },
                                        "inputSchema": {
                                            "type": "object",
                                            "description": "JSON Schema for tool input",
                                        },
                                        "testCases": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "name": {"type": "string"},
                                                    "input": {"type": "object"},
                                                    "expectedOutputKeys": {
                                                        "type": "array",
                                                        "items": {"type": "string"},
                                                    },
                                                    "description": {"type": "string"},
                                                },
                                            },
                                        },
                                    },
                                    "required": [
                                        "message",
                                        "toolName",
                                        "displayName",
                                        "description",
                                        "lambdaCode",
                                        "inputSchema",
                                        "testCases",
                                    ],
                                }
                            },
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": "submit_tool"}},
            }

            response = client.converse(
                modelId=TOOL_GENERATOR_MODEL_ID,
                system=[{"text": system_prompt}],
                messages=messages,
                inferenceConfig=_inference_config(TOOL_GENERATOR_MODEL_ID, 8192, 0),
                toolConfig=tool_config,
            )

            # Extract the tool_use block
            output = response["output"]["message"]
            for block in output.get("content", []):
                if "toolUse" in block:
                    parsed = block["toolUse"].get("input", {})
                    # Wrap into our expected format
                    parsed["responseType"] = "generation"
                    parsed["tool"] = {
                        "toolName": parsed.pop("toolName", ""),
                        "displayName": parsed.pop("displayName", ""),
                        "description": parsed.pop("description", ""),
                        "lambdaCode": parsed.pop("lambdaCode", ""),
                        "inputSchema": parsed.pop("inputSchema", {}),
                    }
                    break
            else:
                return {
                    "success": False,
                    "tool": None,
                    "message": "",
                    "error": "No tool_use block in model response",
                    "responseType": "generation",
                }
        else:
            # Clarification mode: simple text response
            response = client.converse(
                modelId=TOOL_GENERATOR_MODEL_ID,
                system=[{"text": system_prompt}],
                messages=messages,
                inferenceConfig=_inference_config(TOOL_GENERATOR_MODEL_ID, 1024, 0),
            )

            output = response["output"]["message"]
            response_text = ""
            for block in output.get("content", []):
                if "text" in block:
                    response_text += block["text"]

            if not response_text.strip():
                return {
                    "success": False,
                    "tool": None,
                    "message": "",
                    "error": "Empty response from model",
                }

            parsed = _parse_tool_response(response_text)
            if parsed is None:
                # If JSON parse fails, treat the raw text as a clarification message
                return {
                    "success": True,
                    "tool": None,
                    "message": response_text.strip(),
                    "error": None,
                    "responseType": "clarification",
                }

        response_type = parsed.get("responseType", "generation")

        # Mode 1: Clarification — LLM is asking questions
        if response_type == "clarification":
            return {
                "success": True,
                "tool": None,
                "message": parsed.get("message", ""),
                "error": None,
                "responseType": "clarification",
            }

        # Mode 2: Generation — tool produced
        tool_data = parsed.get("tool") or parsed
        # Handle both nested {"tool": {...}} and flat {"toolName": ...} formats
        if isinstance(tool_data, dict) and "toolName" not in tool_data:
            # Maybe the LLM put fields at the top level
            if "toolName" in parsed:
                tool_data = parsed
            # Or used snake_case keys
            elif "tool_name" in tool_data or "tool_name" in parsed:
                tool_data = tool_data if "tool_name" in tool_data else parsed

        # Normalize common key variations (snake_case → camelCase)
        _KEY_MAP = {
            "tool_name": "toolName",
            "display_name": "displayName",
            "lambda_code": "lambdaCode",
            "input_schema": "inputSchema",
        }
        for snake, camel in _KEY_MAP.items():
            if snake in tool_data and camel not in tool_data:
                tool_data[camel] = tool_data.pop(snake)

        # If inputSchema is still missing, provide a sensible default
        if "inputSchema" not in tool_data and "toolName" in tool_data:
            tool_data["inputSchema"] = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            logger.warning("inputSchema missing from LLM response, using empty default")

        required_fields = [
            "toolName",
            "displayName",
            "description",
            "lambdaCode",
            "inputSchema",
        ]
        missing = [f for f in required_fields if f not in tool_data]
        if missing:
            return {
                "success": False,
                "tool": None,
                "message": "",
                "error": f"Missing required fields: {', '.join(missing)}",
                "responseType": "generation",
            }

        test_cases = parsed.get("testCases", parsed.get("test_cases", []))

        return {
            "success": True,
            "tool": tool_data,
            "message": parsed.get("message", f"Generated tool: {tool_data['displayName']}"),
            "error": None,
            "responseType": "generation",
            "testCases": test_cases,
        }

    except Exception as exc:
        logger.exception("Tool generation failed")
        return {
            "success": False,
            "tool": None,
            "message": "",
            "error": str(exc),
        }


def _parse_tool_response(text: str) -> dict | None:
    """Parse the model's JSON response, handling markdown fences if present."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
        # Remove closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None
