"""Bug B regression: a gateway must be deployed even when the caller only
selects gateway TOOLS or SaaS CONNECTORS and never sends an explicit
``gateway_config`` (the Harness authoring form does exactly this).

The SFN state machine gates its gateway step on ``$.gateway_config`` being
present, so without synthesizing one the gateway step is skipped, no gateway is
created, and the harness comes up with ZERO tools (falling back to the default
Strands shell/file tools). ``_gateway_implied`` is the predicate that decides
whether to synthesize the minimal config.
"""

from app.deployment_handler import _gateway_implied


def test_gateway_tools_imply_gateway():
    assert _gateway_implied(["web_page_fetcher"], None, None) is True


def test_connectors_imply_gateway():
    assert _gateway_implied(None, [{"connector_id": "asana"}], None) is True


def test_connected_tools_gateway_marker_implies_gateway():
    # The direct path's signal: "gateway" present in connected_tools.
    assert _gateway_implied(None, None, ["memory", "gateway"]) is True


def test_nothing_implies_no_gateway():
    assert _gateway_implied(None, None, None) is False
    assert _gateway_implied([], [], []) is False
    assert _gateway_implied([], [], ["memory"]) is False
