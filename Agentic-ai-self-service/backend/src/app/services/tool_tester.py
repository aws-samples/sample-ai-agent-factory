"""Tool Tester service — deploys a temporary Lambda, invokes with test cases, validates, cleans up.

Tests generated Lambda code by deploying it to a real AWS Lambda environment,
running each test case, and validating the responses. This catches real runtime
issues (import errors, missing modules, timeout) that local sandbox testing misses.

Reuses Lambda/IAM helper functions from gateway_deployer to avoid duplication.
"""

import ast
import json
import logging
import time
import uuid

from app.services.gateway_deployer import (
    _create_iam_client,
    _create_lambda_client,
    _create_lambda_zip,
    _ensure_lambda_role,
)

logger = logging.getLogger(__name__)

TOOL_TEST_ROLE_NAME = "AgentCoreToolTestRole"
TOOL_TEST_ROLE_DESC = "Shared IAM role for AI Tool Generator test Lambdas"
TOOL_TEST_FN_PREFIX = "AgentCore-ToolTest-"

# Imports that could allow arbitrary system access or network abuse
BLOCKED_IMPORTS = frozenset(
    {
        "subprocess",
        "shutil",
        "ctypes",
        "multiprocessing",
        "socket",
        "http.server",
        "xmlrpc",
        "ftplib",
        "telnetlib",
        "importlib",
        "code",
        "codeop",
        "pty",
        "pipes",
        "commands",
        "os",
        "sys",
        "pickle",
        "marshal",
        "pathlib",
        "tempfile",
        "threading",
        "asyncio",
    }
)

# Built-in functions that enable dynamic code execution
BLOCKED_CALLS = frozenset({"exec", "eval", "compile", "__import__", "breakpoint"})


MAX_CODE_SIZE = 50_000  # 50KB max for generated tool code


def _validate_code_safety(code: str) -> tuple[bool, str]:
    """AST-validate generated Lambda code before deployment.

    Returns (is_safe, error_message). Checks for syntax errors,
    blocked imports, dangerous function calls, and required entry point.
    """
    if len(code) > MAX_CODE_SIZE:
        return False, f"Code too large: {len(code)} bytes exceeds {MAX_CODE_SIZE} limit"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"

    # Dangerous attribute calls like os.system(), os.popen()
    blocked_attrs = frozenset(
        {
            "system",
            "popen",
            "exec",
            "execl",
            "execle",
            "execlp",
            "execv",
            "execve",
            "execvp",
            "spawn",
            "spawnl",
            "spawnle",
        }
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    return False, f"Blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    return False, f"Blocked import: {node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                return False, f"Blocked function call: {node.func.id}"
            # Check attribute calls like os.system(), os.popen()
            if isinstance(node.func, ast.Attribute) and node.func.attr in blocked_attrs:
                return False, f"Blocked function call: {node.func.attr}"

    func_names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if "lambda_handler" not in func_names:
        return False, "Missing required function: lambda_handler"

    return True, ""


def test_tool(lambda_code: str, test_cases: list[dict], region: str = "us-east-1") -> dict:
    """Deploy a temporary Lambda, run test cases, return results, clean up.

    Args:
        lambda_code: Python source code with lambda_handler(event, context).
        test_cases: List of dicts with name, input, expectedOutputKeys, description.
        region: AWS region.

    Returns:
        Dict with keys: success, results (list), allPassed (bool), error (str|None).
    """
    # Step 0: Validate code safety before any AWS calls
    is_safe, safety_error = _validate_code_safety(lambda_code)
    if not is_safe:
        return {
            "success": False,
            "results": [],
            "allPassed": False,
            "error": f"Code safety validation failed: {safety_error}",
        }

    function_name = f"{TOOL_TEST_FN_PREFIX}{uuid.uuid4().hex[:8]}"
    lambda_client = _create_lambda_client(region)
    results = []

    try:
        # Step 1: Ensure shared test role exists (fast if already created)
        iam_client = _create_iam_client()
        role_arn = _ensure_lambda_role(iam_client, TOOL_TEST_ROLE_NAME, TOOL_TEST_ROLE_DESC)

        # Step 2: Deploy temporary Lambda
        zip_bytes = _create_lambda_zip(lambda_code)
        _deploy_temp_lambda(lambda_client, function_name, role_arn, zip_bytes)

        # Step 3: Run each test case
        for tc in test_cases:
            result = _run_test_case(lambda_client, function_name, tc)
            results.append(result)

        all_passed = all(r["passed"] for r in results)
        return {
            "success": True,
            "results": results,
            "allPassed": all_passed,
            "error": None,
        }

    except Exception as exc:
        logger.exception("Tool testing failed: %s", exc)
        return {
            "success": False,
            "results": results,
            "allPassed": False,
            "error": str(exc),
        }

    finally:
        # Step 4: Cleanup — delete temp Lambda (role persists for reuse)
        _cleanup_temp_lambda(lambda_client, function_name)


def _deploy_temp_lambda(lambda_client, function_name: str, role_arn: str, zip_bytes: bytes) -> None:
    """Create a temporary Lambda function and wait for it to become Active."""
    lambda_client.create_function(
        FunctionName=function_name,
        Runtime="python3.12",
        Role=role_arn,
        Handler="lambda_function.lambda_handler",
        Code={"ZipFile": zip_bytes},
        Description="Temporary test Lambda for AI Tool Generator",
        Timeout=10,
        MemorySize=128,
    )

    # Wait for Active state
    for _ in range(30):
        fn = lambda_client.get_function(FunctionName=function_name)
        if fn["Configuration"]["State"] == "Active":
            return
        time.sleep(2)

    raise TimeoutError(f"Lambda {function_name} did not reach Active state within 60s")


def _run_test_case(lambda_client, function_name: str, test_case: dict) -> dict:
    """Invoke the Lambda with a single test case and validate the response."""
    tc_name = test_case.get("name", "unnamed")
    tc_input = test_case.get("input", {})
    expected_keys = test_case.get("expectedOutputKeys", test_case.get("expected_output_keys", []))

    payload = json.dumps({"toolName": tc_name, "input": tc_input})

    start = time.time()
    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=payload.encode(),
        )
        duration_ms = int((time.time() - start) * 1000)

        # Check for Lambda-level errors (unhandled exceptions)
        if response.get("FunctionError"):
            error_payload = json.loads(response["Payload"].read().decode())
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": error_payload,
                "error": error_payload.get("errorMessage", str(error_payload)),
                "durationMs": duration_ms,
            }

        # Parse Lambda response
        raw = json.loads(response["Payload"].read().decode())
        status_code = raw.get("statusCode", 0)
        body = raw.get("body", "{}")

        # Parse body (may be a JSON string or already a dict)
        if isinstance(body, str):
            try:
                body_parsed = json.loads(body)
            except json.JSONDecodeError:
                body_parsed = {"raw": body}
        else:
            body_parsed = body

        # Validate status code
        if status_code != 200:
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": body_parsed,
                "error": f"Expected statusCode 200, got {status_code}",
                "durationMs": duration_ms,
            }

        # Validate expected output keys
        missing_keys = [k for k in expected_keys if k not in body_parsed]
        if missing_keys:
            return {
                "testCaseName": tc_name,
                "passed": False,
                "actualOutput": body_parsed,
                "error": f"Missing expected keys in response: {missing_keys}",
                "durationMs": duration_ms,
            }

        return {
            "testCaseName": tc_name,
            "passed": True,
            "actualOutput": body_parsed,
            "error": None,
            "durationMs": duration_ms,
        }

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "testCaseName": tc_name,
            "passed": False,
            "actualOutput": None,
            "error": str(exc),
            "durationMs": duration_ms,
        }


def _cleanup_temp_lambda(lambda_client, function_name: str) -> None:
    """Delete the temporary test Lambda. Role is shared and persists."""
    try:
        lambda_client.delete_function(FunctionName=function_name)
        logger.info("Cleaned up test Lambda: %s", function_name)
    except Exception as exc:
        logger.warning("Failed to clean up test Lambda %s: %s", function_name, exc)
