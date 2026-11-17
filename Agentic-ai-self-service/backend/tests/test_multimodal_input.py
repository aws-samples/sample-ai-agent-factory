"""Multimodal (image) input: invoke contract + generated-agent normalization.

A text-only invoke must be byte-identical to before (images only ADD a payload
key); when images are attached the generated single-agent turns them into
Bedrock Converse content blocks, and the multi-agent patterns are left unchanged.
"""

from __future__ import annotations

from app.models.deployment_models import RuntimeConfig
from app.routers.versions import InvokeImageInput, SlotInvokeRequest
from app.services.code_generator import (
    _inject_multimodal,
    generate_agent_code,
)
from app.services.runtime_invocation import runtime_payload

_MODEL = {"modelId": "us.anthropic.claude-sonnet-5"}


def _single_agent_source() -> str:
    # Mirrors the real generated single-agent invoke(): it reads a *string*
    # prompt, runs string logic on it (the compute-intent `.lower()` that
    # crashed live once the prompt was rebound to a content-block list), and
    # calls `agent(prompt)`.
    return (
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp\n"
        "from strands import Agent\n"
        "app = BedrockAgentCoreApp()\n"
        "agent = None\n\n"
        "@app.entrypoint\n"
        "def invoke(payload):\n"
        '    prompt = payload.get("prompt", "Hello")\n'
        '    _pl = (prompt or "").lower()\n'
        "    result = agent(prompt)\n"
        '    return {"response": result, "pl": _pl}\n'
    )


def test_inject_multimodal_adds_a_compiling_normalization_block():
    out = _inject_multimodal(_single_agent_source())
    assert '_mm_images = payload.get("images")' in out
    assert '"image": {"format": _mm_fmt, "source": {"bytes": _mm_raw}}' in out
    assert "_mm_blocks = None" in out
    # The text prompt is NOT rebound to a list, so the template's string logic
    # (`(prompt or "").lower()`) keeps operating on a str — this is the exact
    # regression that returned "Runtime invocation failed." in the live probe.
    assert 'payload = {**payload, "prompt": _mm_blocks}' not in out
    assert '(prompt or "").lower()' in out
    # Only the agent invocation receives the content blocks.
    assert "agent(_mm_blocks if _mm_blocks is not None else prompt)" in out
    compile(out, "<mm-single>", "exec")  # the rewritten agent is valid Python


def test_inject_multimodal_routes_blocks_to_agent_without_breaking_string_logic():
    """Reproduces the live AttributeError('list' object has no attribute 'lower'):
    a content-block list must reach the agent while the text prompt stays a str."""
    import base64

    src = (
        "def invoke(payload):\n"
        '    prompt = payload.get("prompt", "Hello")\n'
        '    _pl = (prompt or "").lower()\n'  # crashed live when prompt was a list
        "    result = agent(prompt)\n"
        '    return {"response": result, "pl": _pl}\n'
    )
    out = _inject_multimodal(src)
    assert "agent(_mm_blocks if _mm_blocks is not None else prompt)" in out
    assert 'payload = {**payload, "prompt":' not in out

    ns: dict = {}
    exec(compile(out, "<mm-exec>", "exec"), ns)  # noqa: S102 — generated-code shape under test
    seen: list = []
    ns["agent"] = lambda x: (seen.append(x), "ANSWER")[1]
    invoke = ns["invoke"]

    # Text-only: the agent receives the str; .lower() runs fine.
    out1 = invoke({"prompt": "Hello There"})
    assert seen[-1] == "Hello There"
    assert out1["pl"] == "hello there"

    # With an image: the agent receives Converse content blocks; prompt stays a str.
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    out2 = invoke({"prompt": "What color?", "images": [{"format": "png", "data": png}]})
    assert isinstance(seen[-1], list)
    assert seen[-1][0] == {"text": "What color?"}
    assert seen[-1][1]["image"] == {"format": "png", "source": {"bytes": base64.b64decode(png)}}
    assert out2["pl"] == "what color?"


def test_inject_multimodal_is_a_noop_without_an_invoke():
    code = "x = 1\n"
    assert _inject_multimodal(code) == code


def test_inject_multimodal_skips_multi_agent_code():
    code = (
        "from strands.multiagent.graph import GraphBuilder\n"
        "@app.entrypoint\n"
        "def invoke(payload):\n"
        "    return {}\n"
    )
    assert _inject_multimodal(code) == code  # unchanged — multi-agent is left alone


def test_runtime_payload_adds_images_only_when_present():
    # Text-only: byte-identical to before (no images key).
    assert "images" not in runtime_payload("hi", None, None)
    # With images: the key is added and the text prompt is preserved.
    payload = runtime_payload("hi", None, None, images=[{"format": "png", "data": "abc"}])
    assert payload["prompt"] == "hi"
    assert payload["images"] == [{"format": "png", "data": "abc"}]


def test_slot_invoke_request_accepts_and_validates_images():
    req = SlotInvokeRequest(input="describe", images=[{"format": "png", "bytesB64": "aGVsbG8="}])
    assert req.images is not None and len(req.images) == 1
    assert req.images[0].format == "png"
    assert req.images[0].data == "aGVsbG8="  # bytesB64 alias populates .data
    # Direct model: default format, strict extra-forbid.
    assert InvokeImageInput(bytesB64="aGk=").format == "png"


def test_generated_single_agent_gets_the_block_multi_agent_does_not():
    single = generate_agent_code(
        config=RuntimeConfig(name="mm_probe", model=_MODEL, modelProvider="bedrock", systemPrompt="hi"),
        tools=[],
        portable=True,
    )
    assert "_mm_images" in single
    assert "agent(_mm_blocks if _mm_blocks is not None else" in single  # call routes blocks
    assert 'payload = {**payload, "prompt": _mm_blocks}' not in single  # prompt stays a str
    compile(single, "<single>", "exec")

    multi = generate_agent_code(
        config=RuntimeConfig(
            name="mm_probe",
            model=_MODEL,
            modelProvider="bedrock",
            systemPrompt="hi",
            multiAgentPattern="graph",
            multiAgentConfig={
                "agents": [
                    {"agentId": "a", "systemPrompt": "x", "modelId": "us.anthropic.claude-sonnet-5"},
                    {"agentId": "b", "systemPrompt": "y", "modelId": "us.anthropic.claude-sonnet-5"},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "entryPoint": "a",
            },
        ),
        tools=[],
        portable=True,
    )
    assert "_mm_images" not in multi
    compile(multi, "<multi>", "exec")
