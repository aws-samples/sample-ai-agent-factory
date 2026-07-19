"""Guard: Converse inferenceConfig must omit `temperature` for models that
reject it (Sonnet-5+/Opus-5+/Fable/Mythos), else CustomerFacing generators
(agent + tool) fail with ValidationException 'temperature is deprecated'."""

from app.services.agent_generator import _inference_config as agen_cfg
from app.services.tool_generator import _inference_config as tool_cfg


def test_omits_temperature_for_sonnet5():
    for fn in (agen_cfg, tool_cfg):
        cfg = fn("us.anthropic.claude-sonnet-5", 600, 0.4)
        assert cfg == {"maxTokens": 600}, cfg


def test_omits_temperature_for_opus5_and_fable():
    for fn in (agen_cfg, tool_cfg):
        assert "temperature" not in fn("us.anthropic.claude-opus-5", 100, 0.3)
        assert "temperature" not in fn("us.anthropic.claude-fable-5", 100, 0.3)


def test_keeps_temperature_for_legacy_models():
    cfg = agen_cfg("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 600, 0.4)
    assert cfg == {"maxTokens": 600, "temperature": 0.4}, cfg
