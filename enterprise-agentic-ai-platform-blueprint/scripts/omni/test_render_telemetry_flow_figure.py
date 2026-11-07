"""Keeps Figure 3 of the README (assets/enterprise-agent-factory-omni-telemetry-flow.*) in sync with its generator
and inside the repository's asset policy: the committed SVG, Draw.io and interactive HTML must be exactly what
scripts/omni/render_telemetry_flow_figure.py produces, the static SVG must stay script-free, the HTML must load
nothing from the network, and every account ID must be a documentation placeholder. No network, no AWS calls.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).parent
BLUEPRINT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("render_telemetry_flow_figure", HERE / "render_telemetry_flow_figure.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

SVG_PATH = BLUEPRINT / "assets" / "enterprise-agent-factory-omni-telemetry-flow.svg"
DRAWIO_PATH = BLUEPRINT / "assets" / "enterprise-agent-factory-omni-telemetry-flow.drawio"
HTML_PATH = BLUEPRINT / "assets" / "omni" / "telemetry-flow-interactive.html"
SVG_NS = "{http://www.w3.org/2000/svg}"
# The same placeholder policy as scripts/scrub-security-leakage.sh and docs-site/scripts/validate_static.py.
PLACEHOLDER = re.compile(r"^(123456789012|(\d)\2{11})$")


def _scene():
    return mod.build()


def test_committed_outputs_match_the_generator():
    scene = _scene()
    assert SVG_PATH.read_text(encoding="utf-8") == mod.write_svg(scene, interactive=False) + "\n", "run scripts/omni/render_telemetry_flow_figure.py"
    assert DRAWIO_PATH.read_text(encoding="utf-8") == mod.write_drawio(scene) + "\n", "run scripts/omni/render_telemetry_flow_figure.py"
    assert HTML_PATH.read_text(encoding="utf-8") == mod.write_html(mod.write_svg(scene, interactive=True)), "run scripts/omni/render_telemetry_flow_figure.py"


def test_static_svg_is_accessible_and_passive():
    root = ET.parse(SVG_PATH).getroot()
    assert root.tag == f"{SVG_NS}svg"
    assert root.get("role") == "img"
    assert root.get("viewBox") == f"0 0 {mod.W} {mod.H}"
    labelled_by = (root.get("aria-labelledby") or "").split()
    ids = {el.get("id") for el in root.iter() if el.get("id")}
    assert labelled_by and set(labelled_by) <= ids
    assert root.find(f"{SVG_NS}title").text
    assert root.find(f"{SVG_NS}desc").text
    text = SVG_PATH.read_text(encoding="utf-8")
    assert "<script" not in text and "onload" not in text and "javascript:" not in text
    assert "http://" not in text.replace("http://www.w3.org/2000/svg", "") and "https://" not in text
    # Every element id is unique; the icons copied from figure 2 are re-suffixed per instance.
    all_ids = [el.get("id") for el in root.iter() if el.get("id")]
    assert len(all_ids) == len(set(all_ids))


def test_interactive_html_loads_nothing_from_the_network():
    text = HTML_PATH.read_text(encoding="utf-8")
    assert 'id="omni-flow"' in text
    assert re.search(r"<(script|link|img|iframe|object|embed)[^>]*\b(src|href)\s*=\s*['\"]?(https?:)?//", text, re.I) is None
    assert "fetch(" not in text and "XMLHttpRequest" not in text and "navigator.sendBeacon" not in text and "WebSocket" not in text
    assert "@import" not in text and "url(http" not in text and "url(//" not in text
    # Detail text reaches the page through textContent only; the JSON cannot close the script element.
    assert "</script>" not in text.split("var DETAILS = ", 1)[1].split("var svg", 1)[0]
    for key in mod.FLOWS:
        assert f'data-select="{key}"' in text
        assert f'data-flow="{key}"' in text


def test_every_table_row_and_detail_belongs_to_a_declared_flow():
    assert set(mod.FLOW_DETAILS) == set(mod.FLOWS)
    assert [row[0] for row in mod.TABLE_ROWS] == list(mod.FLOWS)
    for flow, detail in mod.FLOW_DETAILS.items():
        assert set(detail) == {"title", "what", "source", "transport", "destination", "shown", "fields"}, flow
        assert all(isinstance(value, str) and value for value in detail.values()), flow


def test_drawio_mirrors_the_scene():
    scene = _scene()
    root = ET.parse(DRAWIO_PATH).getroot()
    cells = root.findall(".//root/mxCell")
    assert len(cells) == len(scene.items) + 2  # the two mandatory layer cells
    cell_ids = [c.get("id") for c in cells]
    assert len(cell_ids) == len(set(cell_ids))
    assert {it["id"] for it in scene.items} <= set(cell_ids)
    model = root.find(".//mxGraphModel")
    assert (model.get("pageWidth"), model.get("pageHeight")) == (str(mod.W), str(mod.H))


def test_account_ids_are_placeholders_and_match_figure_2():
    figure2 = (BLUEPRINT / "assets" / "enterprise-agent-factory-aws-services.svg").read_text(encoding="utf-8")
    allowed = set(re.findall(r"\b\d{12}\b", figure2))
    for path in (SVG_PATH, DRAWIO_PATH, HTML_PATH):
        for account in set(re.findall(r"\b\d{12}\b", path.read_text(encoding="utf-8"))):
            assert PLACEHOLDER.match(account), f"{path.name}: {account} is not a documentation placeholder"
            assert account in allowed, f"{path.name}: {account} is not an account of figure 2"


def test_scene_stays_inside_the_canvas():
    for it in _scene().items:
        if it["t"] == "rect":
            assert 0 <= it["x"] and it["x"] + it["w"] <= mod.W and 0 <= it["y"] and it["y"] + it["h"] <= mod.H, it
        elif it["t"] == "arrow":
            assert all(0 <= x <= mod.W and 0 <= y <= mod.H for x, y in it["points"]), it
