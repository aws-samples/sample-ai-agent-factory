"""Pydantic models for the AI Tool Generator feature.

Request/response types for POST /api/generate-tool, which uses Claude Sonnet
on Bedrock to generate Lambda tool code from natural language descriptions.
"""

from pydantic import BaseModel, ConfigDict, Field


class ToolGenerateRequest(BaseModel):
    """Request body for POST /api/generate-tool."""

    model_config = ConfigDict(populate_by_name=True)

    prompt: str
    conversation_history: list[dict] = Field(alias="conversationHistory", default_factory=list)
    existing_tool: dict | None = Field(alias="existingTool", default=None)


class GeneratedTool(BaseModel):
    """A single AI-generated tool definition."""

    model_config = ConfigDict(populate_by_name=True)

    tool_name: str = Field(alias="toolName")
    display_name: str = Field(alias="displayName")
    description: str
    lambda_code: str = Field(alias="lambdaCode")
    input_schema: dict = Field(alias="inputSchema")


class TestCase(BaseModel):
    """A single test case for validating a generated Lambda tool."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    input: dict
    expected_output_keys: list[str] = Field(alias="expectedOutputKeys", default_factory=list)
    description: str = ""


class TestResult(BaseModel):
    """Result of running a single test case against a deployed Lambda."""

    model_config = ConfigDict(populate_by_name=True)

    test_case_name: str = Field(alias="testCaseName")
    passed: bool
    actual_output: dict | None = Field(alias="actualOutput", default=None)
    error: str | None = None
    duration_ms: int = Field(alias="durationMs", default=0)


class ToolTestRequest(BaseModel):
    """Request body for POST /api/test-tool."""

    model_config = ConfigDict(populate_by_name=True)

    lambda_code: str = Field(alias="lambdaCode")
    test_cases: list[TestCase] = Field(alias="testCases")


class ToolTestResponse(BaseModel):
    """Response body for POST /api/test-tool."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    results: list[TestResult] = Field(default_factory=list)
    all_passed: bool = Field(alias="allPassed", default=False)
    error: str | None = None


class ToolGenerateResponse(BaseModel):
    """Response body for POST /api/generate-tool."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    tool: GeneratedTool | None = None
    message: str = ""
    error: str | None = None
    response_type: str = Field(alias="responseType", default="generation")
    test_cases: list[TestCase] | None = Field(alias="testCases", default=None)


# ============================================================================
# Phase 1 Gap 1E — NL agent (canvas) generation
# ============================================================================


class AgentGenerateRequest(BaseModel):
    """Request body for POST /api/generate-canvas."""

    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(min_length=1, max_length=4000)
    conversation_history: list[dict] = Field(alias="conversationHistory", default_factory=list, max_length=20)


class AgentGenerateResponse(BaseModel):
    """Response body for POST /api/generate-canvas.

    Returns either a clarification message (first turn) or a canvas spec
    suitable for the frontend's ``instantiateTemplate`` helper.
    """

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    response_type: str = Field(alias="responseType", default="spec")
    message: str | None = None
    spec: dict | None = None
    error: str | None = None
