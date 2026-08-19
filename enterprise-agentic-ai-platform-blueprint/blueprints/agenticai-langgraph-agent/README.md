# agenticai-langgraph-agent

Reference blueprint that runs a **LangGraph** agent on the AgenticAI platform.

Demonstrates:

- MCP-native tool invocation through the workstream AgentCore Gateway (`MCP-Protocol-Version: 2025-06-18` header pinned).
- Bedrock Guardrail required on every model invocation.
- Same Cedar + tool-catalogue contract as the Strands blueprints.
- Multi-framework adapter wiring via `@agenticai/federation`.

```
pytest test_agent.py
```

Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-7.
