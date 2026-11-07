#!/usr/bin/env python3
"""Render Figure 3 of the blueprint README: how telemetry flows from every source account into the
CloudWatch Omni hub, and what the hub shows.

One definition, three outputs, kept in sync by `scripts/omni/test_render_telemetry_flow_figure.py`:

  assets/enterprise-agent-factory-omni-telemetry-flow.svg      static figure embedded in the README
  assets/enterprise-agent-factory-omni-telemetry-flow.drawio   editable Draw.io source (same geometry)
  assets/omni/telemetry-flow-interactive.html                  the same figure, inline, with hover/click
                                                               highlighting of one telemetry flow at a time
                                                               and a detail panel; no network, no dependencies

Usage:  python3 scripts/omni/render_telemetry_flow_figure.py [--out-dir <blueprint root>]

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import pathlib
import re
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve()
BLUEPRINT = HERE.parents[2]

W, H = 1920, 1400
FONT = "Liberation Sans,Helvetica,Arial,sans-serif"
MONO = "Liberation Mono,Menlo,Consolas,monospace"
BG, PANEL_STROKE, CARD_STROKE = "#F4F6FA", "#9AA4B2", "#C3CBD6"
INK, MUTED = "#0F1722", "#5A6474"
PURPLE, BLUE, GREEN, ORANGE, GRAY, MAGENTA = "#7C3AED", "#2563EB", "#1E7B45", "#D45B07", "#5A6474", "#C925D1"
TINT = {PURPLE: "#F5F0FE", BLUE: "#EFF4FE", GREEN: "#EFF7F1", ORANGE: "#FDF1E7", GRAY: "#F7F8FA", MAGENTA: "#FBE9F5"}

# One colour per telemetry flow; every chip, arrow and table row of a flow carries data-flow="<key>".
FLOWS = {
    "spans": {"colour": PURPLE, "label": "agent, model and tool spans"},
    "records": {"colour": BLUE, "label": "prompt and completion records"},
    "gateway": {"colour": GREEN, "label": "Gateway and tool logs"},
    "evals": {"colour": ORANGE, "label": "evaluation results"},
    "metrics": {"colour": GRAY, "label": "metrics and Context Graph"},
    "dev": {"colour": MAGENTA, "label": "developer IDE traces"},
}

# What the interactive page tells about each flow (plain text; rendered with textContent, never as HTML).
FLOW_DETAILS = {
    "spans": {
        "title": "Agent, model and tool spans",
        "what": "OpenTelemetry spans from the agent process: invoke_agent, chat (model calls) and execute_tool spans with gen_ai.* attributes, session.id and user.id.",
        "source": "aws/spans in each source account, written by Transaction Search from the X-Ray OTLP endpoint (AWS Distro for OpenTelemetry).",
        "transport": "Organization centralization rule, selection LogGroupName = 'aws/spans'. Transaction Search must be on in the source account (pipeline-owned; SCP-13 keeps it on).",
        "destination": "/centralized/<account>/<region>aws/spans in the hub, encrypted with the hub centralized-logs CMK; every row carries @aws.account and @aws.region.",
        "shown": "Session Explorer, Trace compare, Agent topology, telemetry queries; the input of every evaluator.",
        "fields": "traceId, spanId, name, startTimeUnixNano, attributes.session.id, attributes.gen_ai.tool.name, resource.attributes.service.name",
    },
    "records": {
        "title": "Prompt and completion records",
        "what": "GenAI log-event records the distro mirrors out of the spans: body.input.messages and body.output.messages per model call (content only when the version opted in, ADR-18, redacted in process).",
        "source": "AgentCore Runtime: /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT. Self-hosted agents: /agenticai/<env>/<tenant>/<agent> (OTLP logs exporter, x-aws-log-group / x-aws-log-stream).",
        "transport": "Organization centralization rule, selections LIKE '/aws/bedrock-agentcore/%' and LIKE '/agenticai/%'.",
        "destination": "/centralized/<account>/<region>/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT and /centralized/<account>/<region>/agenticai/<env>/<tenant>/<agent>.",
        "shown": "Session detail beside the spans; the content the evaluators judge (option A in the hub, option B in the cell, the golden-dataset gate).",
        "fields": "traceId, spanId, attributes.session.id, attributes.event.name, body.input.messages[], body.output.messages[]",
    },
    "gateway": {
        "title": "Gateway and tool logs",
        "what": "AgentCore Gateway application logs delivered through CloudWatch Logs vended deliveries: tool list/call requests, Verified Permissions decisions, denials.",
        "source": "/aws/vendedlogs/bedrock-agentcore/... in the platform account (shared Gateway) and in cells that run their own Gateway.",
        "transport": "Organization centralization rule, selection LIKE '/aws/vendedlogs/bedrock-agentcore/%'. SCP-13 denies deleting or re-pointing these deliveries outside the pipeline.",
        "destination": "/centralized/<account>/<region>/aws/vendedlogs/bedrock-agentcore/... in the hub.",
        "shown": "Tool-call views next to the execute_tool spans; telemetry queries across accounts.",
        "fields": "@timestamp, requestId, gatewayId, toolName, decision, @aws.account",
    },
    "evals": {
        "title": "Evaluation results",
        "what": "OpenTelemetry GenAI evaluation events: gen_ai.evaluation.name, score value and label, explanation, evaluation level (session, trace, tool call), plus the metrics the service publishes.",
        "source": "Option B (cell): /aws/bedrock-agentcore/evaluations/results/<config-id>. Option A and the golden-dataset gate write directly in the hub.",
        "transport": "Option B results travel with the rule (LIKE '/aws/bedrock-agentcore/%'); hub-side results need no copy.",
        "destination": "/centralized/<account>/<region>/aws/bedrock-agentcore/evaluations/results/<config-id>; hub: /aws/bedrock-agentcore/evaluations/results/<config-id> (option A) and /agenticai/golden-gate/<tenant>-<agent> (golden gate).",
        "shown": "Evaluations dashboard, scores beside each trace in Session Explorer, metrics in Bedrock-AgentCore/Evaluations and agenticai/golden-gate; the pre-promotion verdict (PASS / FAIL) in the Workload pipeline.",
        "fields": "gen_ai.evaluation.name, gen_ai.evaluation.score.value, gen_ai.evaluation.score.label, gen_ai.evaluation.explanation, session.id, traceId",
    },
    "metrics": {
        "title": "Metrics and Context Graph",
        "what": "Every CloudWatch metric namespace of the source account (AWS/Bedrock, AWS/BedrockAgentCore, Bedrock-AgentCore/Evaluations, custom) and the entity relationships behind Agent topology.",
        "source": "CloudWatch metrics and the Context Graph of each source account.",
        "transport": "Organization centralization rule, MetricsSelectionCriteria '*'; Context Graph centralization enabled by omni_space_bootstrap.py centralization enable-context-graph.",
        "destination": "Hub-owned centralized metrics (read with IncludeLinkedAccounts=true) and the hub Context Graph.",
        "shown": "Agent topology, dashboards, alarms that feed the evaluation-gates failures topic, Ask Assistant.",
        "fields": "Namespace, MetricName, Dimensions, @aws.account; entity type, relationship",
    },
    "dev": {
        "title": "Developer IDE traces (nonprod only)",
        "what": "Traces and records an engineer emits from Kiro or VS Code with Cloud Login into the team's nonprod cell: the GA developer loop.",
        "source": "/agenticai/dev/<workstream>/* plus aws/spans in the nonprod cell, written by the Developer permission set (DeveloperTelemetryWrite, DeveloperTelemetrySpans; pinned to the nonprod accounts).",
        "transport": "Organization centralization rule, selection LIKE '/agenticai/%' (dev is an env slot no deployed stack uses, so the grant never reaches a runtime group).",
        "destination": "/centralized/<nonprod-account>/<region>/agenticai/dev/<workstream>/... in the hub.",
        "shown": "The same views as production traces, keyed by the developer's account (@aws.account) and the Omni data scope cloud.account.id.",
        "fields": "same as spans and records; resource.attributes.service.name = the developer's agent",
    },
}

TABLE_ROWS = [
    ("spans", "Agent, model and tool spans", "aws/spans · Transaction Search (ADOT → X-Ray OTLP)", "/centralized/<account>/<region>aws/spans", "Session Explorer · Trace compare · Agent topology · telemetry queries"),
    ("records", "Prompt and completion records", "/aws/bedrock-agentcore/runtimes/<id>-DEFAULT · /agenticai/<env>/<tenant>/<agent> (content only with the ADR-18 opt-in, redacted in process)", "/centralized/<account>/<region>/aws/bedrock-agentcore/runtimes/… · …/agenticai/<env>/<tenant>/<agent>", "Session detail · the input of options A and B and of the golden-dataset gate"),
    ("gateway", "Gateway and tool logs", "/aws/vendedlogs/bedrock-agentcore/* (Gateway deliveries)", "/centralized/<account>/<region>/aws/vendedlogs/bedrock-agentcore/…", "Tool-call views · telemetry queries"),
    ("evals", "Evaluation results", "Option B in the cell: /aws/bedrock-agentcore/evaluations/results/<config> · option A and the golden gate write in the hub", "/centralized/<account>/<region>/aws/bedrock-agentcore/evaluations/results/<config> · hub /agenticai/golden-gate/<tenant>-<agent>", "Evaluations dashboard · scores beside traces · Bedrock-AgentCore/Evaluations and agenticai/golden-gate metrics"),
    ("metrics", "Metrics and Context Graph", "All namespaces (AWS/Bedrock, AWS/BedrockAgentCore, Bedrock-AgentCore/Evaluations, custom) · entity relationships", "Centralized metrics (IncludeLinkedAccounts=true) · hub Context Graph", "Agent topology · dashboards · alarms → evaluation-gates failures topic"),
    ("dev", "Developer IDE traces (nonprod)", "/agenticai/dev/<workstream>/* + aws/spans, written by the Developer permission set", "/centralized/<nonprod>/<region>/agenticai/dev/<workstream>/…", "Same views, keyed by the developer's account (@aws.account)"),
]
NOT_COPIED = ("Not copied: log groups outside the selection, the management account (organization domain only), accounts outside the rule's source scope. "
              "Copies start when the rule is Healthy and both observabilityadmin service-linked roles exist in the source account; nothing is back-filled.")


# ----------------------------------------------------------------------------------------------------
# Text metrics (approximations tuned to the generator that produced figures 1 and 2)
# ----------------------------------------------------------------------------------------------------
def est(text: str, size: float, bold: bool = False, mono: bool = False) -> float:
    """Conservative advance-width estimate (Liberation Mono is 0.6em per glyph; sans averages ~0.5em,
    bold ~0.58em). Slightly over-estimating keeps chips and labels from colliding in every renderer."""
    per = size * (0.68 if mono else (0.6 if bold else 0.52))
    return len(text) * per


def wrap(text: str, size: float, width: float, mono: bool = False, bold: bool = False) -> list[str]:
    words, lines, line = text.split(" "), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if est(candidate, size, bold, mono) <= width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


# ----------------------------------------------------------------------------------------------------
# Scene: every drawable is a dict; the three writers walk the same list
# ----------------------------------------------------------------------------------------------------
class Scene:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.counter = 0

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def rect(self, x, y, w, h, *, fill="#FFFFFF", stroke=CARD_STROKE, sw=1.2, rx=8, dashed=False, flow=None, cid=None):
        self.items.append({"t": "rect", "id": cid or self._id("r"), "x": x, "y": y, "w": w, "h": h, "fill": fill, "stroke": stroke,
                           "sw": sw, "rx": rx, "dashed": dashed, "flow": flow})

    def text(self, x, y, s, *, size=11, bold=False, fill=INK, anchor="start", mono=False, spacing=None, flow=None, cid=None):
        self.items.append({"t": "text", "id": cid or self._id("t"), "x": x, "y": y, "s": s, "size": size, "bold": bold, "fill": fill,
                           "anchor": anchor, "mono": mono, "spacing": spacing, "flow": flow})

    def para(self, x, y, s, width, *, size=11, fill=MUTED, lead=None, bold=False, mono=False, flow=None):
        lead = lead or size * 1.3
        for i, line in enumerate(wrap(s, size, width, mono, bold)):
            self.text(x, y + i * lead, line, size=size, fill=fill, bold=bold, mono=mono, flow=flow)
        return y + len(wrap(s, size, width, mono, bold)) * lead

    def accent(self, x, y, h, colour):
        self.rect(x, y, 3.5, h, fill=colour, stroke="none", sw=0, rx=0)

    def pill(self, x_right, y, label, colour):
        w = est(label, 10, True) + 18
        self.rect(x_right - w, y, w, 18, fill=TINT[colour], stroke=colour, sw=0.8, rx=9)
        self.text(x_right - w / 2, y + 13, label, size=10, bold=True, fill=colour, anchor="middle", spacing=0.6)

    def chip(self, x, y, label, flow, *, w=None, sub=None):
        """A log-group chip: monospace label, optional one-line sans subtitle, sized to the longer of the two."""
        colour = FLOWS[flow]["colour"]
        width = w or max(est(label, 10, mono=True), est(sub or "", 10)) + 20
        h = 24 if sub is None else 40
        self.rect(x, y, width, h, fill=TINT[colour], stroke=colour, sw=1, rx=6, flow=flow, cid=self._id("c"))
        self.text(x + 10, y + 16, label, size=10, mono=True, fill=INK, flow=flow)
        if sub:
            self.text(x + 10, y + 32, sub, size=10, fill=MUTED, flow=flow)
        return width, h

    def arrow(self, points, colour, *, dashed=True, sw=2.2, flow=None, cid=None):
        self.items.append({"t": "arrow", "id": cid or self._id("a"), "points": points, "colour": colour, "dashed": dashed, "sw": sw, "flow": flow})

    def icon(self, key, x, y, scale=0.525):
        self.items.append({"t": "icon", "id": self._id("i"), "key": key, "x": x, "y": y, "scale": scale})


def build() -> Scene:
    s = Scene()
    s.text(56, 50, "CloudWatch Omni — fleet telemetry flow into the Observability hub", size=26, bold=True)
    s.rect(56, 62, 48, 3, fill=ORANGE, stroke="none", sw=0, rx=0)
    s.text(56, 90, "Every source account emits · one organization centralization rule copies · one hub account shows the fleet · OAM links stay for the console",
           size=13.5, fill=MUTED)

    # Organization frame (sources, transport and hub); the table and the legend sit below it
    s.rect(40, 110, 1840, 880, fill="none", stroke=PANEL_STROKE, sw=1.2, rx=10)
    s.text(64, 138, "AWS ORGANIZATION / ENTERPRISE LANDING ZONE", size=11, fill=MUTED, spacing=2.4)

    # ---------------- Sources column ----------------
    sx, sw_ = 64, 600
    # Management account
    s.rect(sx, 150, sw_, 100, stroke=PANEL_STROKE)
    s.accent(sx, 158, 22, GRAY)
    s.text(sx + 18, 177, "Management account", size=15, bold=True)
    s.pill(sx + sw_ - 16, 162, "123456789012", GRAY)
    s.icon("identity_center", sx + 20, 198, 0.42)
    s.text(sx + 62, 212, "CloudWatch Omni organization domain", size=12, bold=True)
    s.text(sx + 62, 228, "sign-in URL · IAM Identity Center (primary Region) · AWS::CloudWatch::DomainForOrganization", size=10.5, fill=MUTED)
    s.text(sx + 62, 243, "not a telemetry source: no space, no copies", size=10.5, fill=MUTED)

    # Platform account
    py = 266
    s.rect(sx, py, sw_, 200, stroke=PANEL_STROKE)
    s.accent(sx, py + 8, 22, BLUE)
    s.text(sx + 18, py + 27, "Agent Factory platform account — control plane", size=15, bold=True)
    s.pill(sx + sw_ - 16, py + 12, "666666666666", BLUE)
    s.text(sx + 20, py + 50, "Shared Gateway, inference gateway, online evaluation of platform tools", size=10.5, fill=MUTED)
    cy = py + 64
    w1, _ = s.chip(sx + 20, cy, "aws/spans", "spans", sub="Transaction Search · ADOT → X-Ray OTLP")
    s.chip(sx + 20 + w1 + 12, cy, "/aws/vendedlogs/bedrock-agentcore/*", "gateway", sub="Gateway application logs (vended delivery)")
    cy += 48
    w1, _ = s.chip(sx + 20, cy, "/agenticai/online-eval/<env>/…", "evals", sub="platform-side evaluation results")
    s.chip(sx + 20 + w1 + 12, cy, "/agenticai/bedrock-invocations", "records", sub="model invocation logging (CMK)")
    cy += 48
    s.chip(sx + 20, cy, "AWS/Bedrock · AWS/BedrockAgentCore metrics", "metrics")

    # Workstream cell A
    cy0 = 482
    s.rect(sx, cy0, sw_, 400, stroke=PANEL_STROKE)
    s.accent(sx, cy0 + 8, 22, GREEN)
    s.text(sx + 18, cy0 + 27, "Workstream cell account A — product / domain", size=15, bold=True)
    s.pill(sx + sw_ - 16, cy0 + 12, "777777777777", GREEN)
    s.text(sx + 20, cy0 + 50, "Agent telemetry", size=11.5, bold=True, fill=INK)
    cy = cy0 + 60
    w1, _ = s.chip(sx + 20, cy, "aws/spans", "spans", sub="invoke_agent · chat · execute_tool spans")
    s.chip(sx + 20 + w1 + 12, cy, "/aws/bedrock-agentcore/runtimes/<id>-DEFAULT", "records", sub="AgentCore Runtime: prompt/completion records")
    cy += 48
    w1, _ = s.chip(sx + 20, cy, "/agenticai/<env>/<tenant>/<agent>", "records", sub="self-hosted agents (OTLP logs exporter)")
    s.chip(sx + 20 + w1 + 12, cy, "/aws/vendedlogs/bedrock-agentcore/*", "gateway", sub="cell Gateway logs")
    cy += 56
    s.text(sx + 20, cy + 8, "Evaluation (option B, in the cell)", size=11.5, bold=True, fill=INK)
    cy += 18
    s.chip(sx + 20, cy, "/aws/bedrock-agentcore/evaluations/results/<config>", "evals", sub="AgentCoreOnlineEvaluationConstruct results")
    cy += 54
    s.text(sx + 20, cy + 8, "Developer IDE loop (nonprod cells only)", size=11.5, bold=True, fill=INK)
    cy += 18
    s.chip(sx + 20, cy, "/agenticai/dev/<workstream>/*", "dev", sub="Kiro / VS Code with Cloud Login · Developer permission set")
    cy += 50
    s.chip(sx + 20, cy, "Bedrock-AgentCore/Evaluations · AWS/BedrockAgentCore metrics · Context Graph", "metrics")
    cy += 34
    s.text(sx + 20, cy + 10, "Transaction Search and the deliveries are pipeline-owned; SCP-13 denies a cell principal switching them off (reads stay open).",
           size=10.5, fill=MUTED)
    s.text(sx + 20, cy + 26, "OAM link to the Observability sink shares metrics, logs and traces with the console view (unchanged by Omni).",
           size=10.5, fill=MUTED)

    # Cells 2..N
    s.rect(sx, 898, sw_, 70, stroke=GREEN, dashed=True)
    s.accent(sx, 906, 22, GREEN)
    s.text(sx + 18, 925, "Workstream cells 2 … N", size=15, bold=True)
    s.pill(sx + sw_ - 16, 910, "1 … N", GREEN)
    s.text(sx + 20, 952, "same log-group prefixes, same rule, one centralized copy per source account and Region", size=10.5, fill=MUTED)

    # ---------------- Transport column ----------------
    tx, tw = 700, 300
    ry = 300
    s.rect(tx, ry, tw, 320, fill=TINT[PURPLE], stroke=PURPLE, sw=1.3, rx=8)
    s.text(tx + 16, ry + 26, "Organization centralization rule", size=13.5, bold=True, fill=PURPLE)
    s.text(tx + 16, ry + 44, "agenticai-fleet-<region>", size=10.5, mono=True, fill=INK)
    yy = s.para(tx + 16, ry + 66, "Owned by the Observability account as CloudWatch delegated administrator (AWS::ObservabilityAdmin::OrganizationCentralizationRule).", tw - 32, size=10.5)
    yy = s.para(tx + 16, yy + 6, "Logs: LogGroupName LIKE '/agenticai/%' OR LIKE '/aws/bedrock-agentcore/%' OR LIKE '/aws/vendedlogs/bedrock-agentcore/%' OR = 'aws/spans'", tw - 32, size=10.5)
    yy = s.para(tx + 16, yy + 6, "Metrics: * · Context Graph · encrypted source groups allowed · destination encrypted with the hub CMK · allocation tags propagated.", tw - 32, size=10.5)
    s.para(tx + 16, yy + 6, "Forward-only from the moment the rule is Healthy; every source account needs both observabilityadmin service-linked roles.", tw - 32, size=10.5)

    qy = 648
    s.rect(tx, qy, tw, 196, stroke=PANEL_STROKE, sw=1.2, rx=8)
    s.text(tx + 16, qy + 26, "Source-account prerequisites", size=13.5, bold=True)
    yy = s.para(tx + 16, qy + 48, "Both observabilityadmin service-linked roles (omni_space_bootstrap.py centralization ensure-service-linked-roles).", tw - 32, size=10.5)
    yy = s.para(tx + 16, yy + 6, "Transaction Search on (agenticai/enableTransactionSearch) so aws/spans exists; log groups under the selected prefixes.", tw - 32, size=10.5)
    s.para(tx + 16, yy + 6, "SCP-13 keeps Transaction Search and the Gateway deliveries pipeline-owned. Nothing is back-filled: copies start when the rule is Healthy.", tw - 32, size=10.5)

    oy = 868
    s.rect(tx, oy, tw, 100, fill=TINT[GRAY], stroke=GRAY, sw=1.2, rx=8)
    s.text(tx + 16, oy + 26, "CloudWatch OAM link (unchanged)", size=13.5, bold=True, fill=GRAY)
    s.para(tx + 16, oy + 46, "Cross-account console views and Grafana. Omni does not read OAM links: a space reads only the Dataset of the account that hosts it.", tw - 32, size=10.5)

    # ---------------- Hub column ----------------
    hx, hw = 1036, 820
    s.rect(hx, 150, hw, 818, stroke=PURPLE, sw=1.4)
    s.accent(hx, 158, 22, PURPLE)
    s.icon("cloudwatch", hx + 18, 160, 0.42)
    s.text(hx + 62, 177, "Observability account — CloudWatch Omni hub (delegated administrator)", size=15, bold=True)
    s.pill(hx + hw - 16, 162, "555555555555", PURPLE)
    s.text(hx + 62, 196, "one hub space per Omni Region · hosts the copies, the Dataset, the space and the hub-side evaluations", size=10.5, fill=MUTED)

    # Row 1: centralized copies
    r1y = 214
    s.rect(hx + 20, r1y, hw - 40, 256, stroke=CARD_STROKE)
    s.text(hx + 36, r1y + 24, "Centralized copies", size=13, bold=True)
    s.text(hx + 36, r1y + 42, "/centralized/<account>/<region><logGroup> · hub CMK OmniCentralizedLogsKey · @aws.account and @aws.region on every row", size=10.5, fill=MUTED)
    cx, cy = hx + 36, r1y + 56
    for label, flow in [("/centralized/<acct>/<region>aws/spans", "spans"),
                        ("/centralized/<acct>/<region>/aws/bedrock-agentcore/runtimes/<id>-DEFAULT", "records"),
                        ("/centralized/<acct>/<region>/agenticai/<env>/<tenant>/<agent>", "records"),
                        ("/centralized/<acct>/<region>/aws/vendedlogs/bedrock-agentcore/…", "gateway"),
                        ("/centralized/<acct>/<region>/aws/bedrock-agentcore/evaluations/results/…", "evals"),
                        ("/centralized/<nonprod>/<region>/agenticai/dev/<workstream>/…", "dev")]:
        s.chip(cx, cy, label, flow)
        cy += 30
    # right sub-column of row 1
    mx = hx + 580
    s.rect(mx, r1y + 56, 220, 84, fill=TINT[GRAY], stroke=GRAY, sw=1, rx=6, flow="metrics", cid=s._id("c"))
    s.text(mx + 12, r1y + 76, "Centralized metrics · Context Graph", size=11, bold=True, flow="metrics")
    s.para(mx + 12, r1y + 93, "all namespaces, read with IncludeLinkedAccounts=true; entity relationships feed Agent topology", 196, size=10, flow="metrics")
    s.rect(mx, r1y + 152, 220, 84, fill="#FFFFFF", stroke=CARD_STROKE, sw=1, rx=6)
    s.text(mx + 12, r1y + 172, "CloudWatch Dataset", size=11, bold=True)
    s.para(mx + 12, r1y + 189, "SQL telemetry queries over default; Dataset integration role logs.amazonaws.com; 365-day window", 196, size=10)

    # Row 2: space
    r2y = 494
    s.rect(hx + 20, r2y, hw - 40, 220, fill=TINT[PURPLE], stroke=PURPLE, sw=1.3)
    s.text(hx + 36, r2y + 24, "Omni space", size=13, bold=True, fill=PURPLE)
    s.text(hx + 122, r2y + 24, "agenticai-fleet-<region>", size=10.5, mono=True, fill=INK)
    s.text(hx + 36, r2y + 42, "what the hub shows — six surfaces over the centralized copies, scoped per grant by cloud.account.id", size=10.5, fill=MUTED)
    surfaces = [
        ("Session Explorer", "sessions by session.id and user.id across every source account", "spans"),
        ("Trace compare", "spans, model and tool calls side by side; prompt/completion records beside them", "records"),
        ("Agent topology", "agents, tools and models from the centralized Context Graph", "metrics"),
        ("Evaluations", "option A online scores, golden-dataset batch verdicts, option B results copied from the cells", "evals"),
        ("Ask Assistant · NL queries", "natural-language questions over the Dataset (judge inference off by default, ADR-16)", "gateway"),
        ("Telemetry queries", "SQL over the hub Dataset, grouped by @aws.account and @aws.region", "dev"),
    ]
    for i, (title, desc, flow) in enumerate(surfaces):
        col, row = i % 3, i // 3
        bx, by = hx + 36 + col * 256, r2y + 56 + row * 72
        s.rect(bx, by, 244, 64, fill="#FFFFFF", stroke=CARD_STROKE, sw=1, rx=6, flow=flow, cid=s._id("c"))
        s.text(bx + 12, by + 20, title, size=11.5, bold=True, flow=flow)
        s.para(bx + 12, by + 36, desc, 222, size=10, flow=flow)
    s.text(hx + 36, r2y + 210, "Access: grants with data scopes (Identity Center users and groups, IAM roles by ARN) · operator role CloudWatchOmniOperatorRole · CMK on space and Dataset", size=10, fill=MUTED)

    # Row 3
    r3y = 734
    s.rect(hx + 20, r3y, 380, 214, fill=TINT[GRAY], stroke=GRAY, sw=1.2)
    s.text(hx + 36, r3y + 24, "CloudWatch console and Grafana (via OAM)", size=13, bold=True, fill=GRAY)
    s.para(hx + 36, r3y + 46, "GenAI Observability view and fleet dashboards over the OAM sink; alarms on evaluation metrics raise the evaluation-gates failures topic. Unchanged by Omni.", 348, size=10.5)
    s.rect(hx + 420, r3y, 400, 214, fill=TINT[GREEN], stroke=GREEN, sw=1.2, flow="evals", cid=s._id("c"))
    s.text(hx + 436, r3y + 24, "Evaluation and promotion in the hub", size=13, bold=True, fill=GREEN, flow="evals")
    yy = s.para(hx + 436, r3y + 46, "Option A: online evaluation inside the space over the centralized groups, role AgentCoreEvaluationRole-omni-<region>.", 368, size=10.5, flow="evals")
    yy = s.para(hx + 436, yy + 4, "Golden-dataset gate: scenarios curated from the copies into immutable Dataset versions; the Workload pipeline assumes AgenticAIGoldenGateRunner-<region> to batch-evaluate the candidate against a version.", 368, size=10.5, flow="evals")
    s.chip(hx + 436, yy + 2, "/agenticai/golden-gate/<tenant>-<agent>", "evals")
    s.text(hx + 436, yy + 42, "metrics agenticai/golden-gate · results encrypted with the hub CMK", size=10, fill=MUTED, flow="evals")

    # ---------------- Arrows ----------------
    # sources -> rule
    s.arrow([(664, 364), (682, 364), (682, 420), (700, 420)], PURPLE, cid="a_platform")
    s.arrow([(664, 678), (682, 678), (682, 480), (700, 480)], PURPLE, cid="a_cell")
    s.arrow([(664, 933), (682, 933), (682, 540), (700, 540)], PURPLE, cid="a_cells")
    # rule -> hub copies (thick)
    s.arrow([(1000, 450), (1018, 450), (1018, 332), (1056, 332)], PURPLE, sw=3.2, cid="a_rule_hub")
    # copies -> space (dataset integration)
    s.arrow([(hx + 400, 470), (hx + 400, 494)], PURPLE, dashed=False, sw=2.2, cid="a_dataset")
    s.text(hx + 410, 487, "Dataset integration", size=10, fill=PURPLE, bold=True)
    # space -> evaluation (hub-side evaluators read the copies)
    s.arrow([(hx + 620, 714), (hx + 620, 734)], GREEN, dashed=False, sw=2.2, flow="evals", cid="a_evals")
    # cells -> OAM -> console
    s.arrow([(664, 860), (682, 860), (682, 916), (700, 916)], GRAY, cid="a_oam_in")
    s.arrow([(1000, 916), (1018, 916), (1018, 836), (1056, 836)], GRAY, cid="a_oam_out")
    # management -> space sign-in
    s.arrow([(664, 200), (1046, 200), (1046, 600), (1056, 600)], BLUE, sw=1.8, cid="a_signin")
    s.text(690, 193, "sign-in through the organization domain · Identity Center", size=10, fill=BLUE, bold=True)

    # ---------------- Table ----------------
    ty = 1018
    s.rect(64, ty, 1792, 320, stroke=PANEL_STROKE)
    s.accent(64, ty + 8, 22, GRAY)
    s.text(82, ty + 27, "What the hub shows, per telemetry type", size=14, bold=True)
    cols = [(84, 236), (330, 480), (820, 540), (1370, 470)]
    heads = ["Telemetry", "Source signal (in the source account)", "Centralized copy (in the hub)", "Shown in the hub"]
    hy = ty + 56
    for (x, w), h_ in zip(cols, heads):
        s.text(x, hy, h_, size=11, bold=True, fill=MUTED)
    s.rect(84, hy + 8, 1756, 1, fill=CARD_STROKE, stroke="none", sw=0, rx=0)
    ry_ = hy + 30
    for flow, name, src, dst, shown in TABLE_ROWS:
        colour = FLOWS[flow]["colour"]
        s.rect(84, ry_ - 13, 8, 8, fill=colour, stroke="none", sw=0, rx=4, flow=flow, cid=s._id("c"))
        s.text(98, ry_ - 6, name, size=11, bold=True, flow=flow)
        s.para(cols[1][0], ry_ - 6, src, cols[1][1], size=10.5, fill=INK, lead=14, flow=flow)
        s.para(cols[2][0], ry_ - 6, dst, cols[2][1], size=10.5, fill=INK, lead=14, flow=flow)
        s.para(cols[3][0], ry_ - 6, shown, cols[3][1], size=10.5, fill=INK, lead=14, flow=flow)
        ry_ += 36
    s.para(84, ry_ - 2, NOT_COPIED, 1740, size=10.5, fill=MUTED, lead=14)

    # ---------------- Legend ----------------
    ly = 1372
    lx = 64
    for colour, dashed, label in [(PURPLE, True, "centralization rule copy"), (BLUE, True, "sign-in and access"), (GRAY, True, "OAM link (console)"),
                                  (GREEN, False, "evaluation and promotion")]:
        s.arrow([(lx, ly), (lx + 30, ly)], colour, dashed=dashed, sw=2.2)
        s.text(lx + 40, ly + 4, label, size=11, bold=True)
        lx += 40 + est(label, 11, True) + 40
    s.text(1856, ly + 4, "Account IDs are placeholders · one hub per Omni Region · official AWS Architecture Icons", size=10.5, fill=MUTED, anchor="end")
    return s


# ----------------------------------------------------------------------------------------------------
# Icons: reuse the embedded AWS Architecture Icons of figure 2 (SVG groups and Draw.io image styles)
# ----------------------------------------------------------------------------------------------------
SVG_NS = "http://www.w3.org/2000/svg"


def Q(tag: str) -> str:
    """Qualified SVG tag for ElementTree."""
    return f"{{{SVG_NS}}}{tag}"
ICON_SOURCE_SVG = BLUEPRINT / "assets" / "enterprise-agent-factory-aws-services.svg"
ICON_SOURCE_DRAWIO = BLUEPRINT / "assets" / "enterprise-agent-factory-aws-services.drawio"
ICON_SVG_IDS = {"cloudwatch": "cloudwatch--Icon-Architecture/64/Arch_Amazon-CloudWatch_64--instance-1",
                "identity_center": "identity_center--Icon-Architecture/64/Arch_AWS-IAM-Identity-Center_64"}
ICON_DRAWIO_CELLS = {"cloudwatch": "i_t_cw_cloudwatch", "identity_center": "i_t_idc_identity_center"}


def load_svg_icons() -> dict[str, ET.Element]:
    root = ET.parse(ICON_SOURCE_SVG).getroot()
    found: dict[str, ET.Element] = {}
    for el in root.iter(f"{{{SVG_NS}}}g"):
        for key, wanted in ICON_SVG_IDS.items():
            if el.get("id") == wanted:
                found[key] = el
    missing = set(ICON_SVG_IDS) - set(found)
    if missing:
        raise SystemExit(f"icons not found in figure 2: {sorted(missing)}")
    return found


def load_drawio_icon_styles() -> dict[str, str]:
    cells = ET.parse(ICON_SOURCE_DRAWIO).getroot().find(".//root")
    styles = {c.get("id"): c.get("style") for c in cells}
    return {key: styles[cid] for key, cid in ICON_DRAWIO_CELLS.items()}


# ----------------------------------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------------------------------
def write_svg(scene: Scene, *, interactive: bool) -> str:
    icons = load_svg_icons()
    attrs = {"width": str(W), "height": str(H), "viewBox": f"0 0 {W} {H}", "role": "img",
             "aria-labelledby": "omni-flow-title omni-flow-desc"}
    if interactive:
        attrs["id"] = "omni-flow"
    # The namespace goes on the root through ElementTree (registered as the default prefix below), so the
    # icon subtrees copied from figure 2, which are already in the SVG namespace, serialise without prefixes.
    ET.register_namespace("", SVG_NS)
    root = ET.Element(f"{{{SVG_NS}}}svg", attrs)
    ET.SubElement(root, Q("title"), {"id": "omni-flow-title"}).text = "CloudWatch Omni fleet telemetry flow into the Observability hub"
    ET.SubElement(root, Q("desc"), {"id": "omni-flow-desc"}).text = (
        "Source accounts (management, platform, workstream cells) and their log groups; the organization centralization rule copying "
        "spans, prompt and completion records, Gateway logs, evaluation results, metrics and the Context Graph into the Observability hub "
        "account; the Omni space surfaces that show them; the OAM link to the console; and a table of what the hub shows per telemetry type.")
    ET.SubElement(root, Q("rect"), {"width": str(W), "height": str(H), "fill": BG})
    defs = ET.SubElement(root, Q("defs"))
    for name, colour in [("purple", PURPLE), ("blue", BLUE), ("green", GREEN), ("gray", GRAY), ("orange", ORANGE), ("magenta", MAGENTA)]:
        m = ET.SubElement(defs, Q("marker"), {"id": f"ah_{name}", "viewBox": "0 0 10 10", "refX": "8.5", "refY": "5", "markerWidth": "7.5",
                                           "markerHeight": "7.5", "orient": "auto-start-reverse"})
        ET.SubElement(m, Q("path"), {"d": "M0 0L10 5L0 10z", "fill": colour})
    marker_by_colour = {PURPLE: "purple", BLUE: "blue", GREEN: "green", GRAY: "gray", ORANGE: "orange", MAGENTA: "magenta"}
    icon_instances = 0
    for it in scene.items:
        common = {}
        if it.get("flow"):
            common["data-flow"] = it["flow"]
            common["class"] = "flow"
        if it["t"] == "rect":
            a = {"x": f"{it['x']:.1f}", "y": f"{it['y']:.1f}", "width": f"{it['w']:.1f}", "height": f"{it['h']:.1f}", "fill": it["fill"]}
            if it["rx"]:
                a["rx"] = str(it["rx"])
            if it["stroke"] != "none":
                a["stroke"] = it["stroke"]
                a["stroke-width"] = str(it["sw"])
            if it["dashed"]:
                a["stroke-dasharray"] = "6 4"
            a.update(common)
            if interactive and it.get("flow") and it["id"].startswith("c"):
                a.update({"tabindex": "0", "role": "button", "aria-label": f"Highlight {FLOWS[it['flow']]['label']}"})
            ET.SubElement(root, Q("rect"), a)
        elif it["t"] == "text":
            a = {"x": f"{it['x']:.1f}", "y": f"{it['y']:.1f}", "font-family": MONO if it["mono"] else FONT, "font-size": str(it["size"]),
                 "fill": it["fill"], "text-anchor": it["anchor"]}
            if it["bold"]:
                a["font-weight"] = "bold"
            if it["spacing"]:
                a["letter-spacing"] = str(it["spacing"])
            a.update(common)
            ET.SubElement(root, Q("text"), a).text = it["s"]
        elif it["t"] == "arrow":
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in it["points"])
            a = {"points": pts, "fill": "none", "stroke": it["colour"], "stroke-width": str(it["sw"]),
                 "marker-end": f"url(#ah_{marker_by_colour[it['colour']]})", "stroke-linejoin": "round"}
            if it["dashed"]:
                a["stroke-dasharray"] = "7 5"
            a["id"] = it["id"]
            a.update(common)
            ET.SubElement(root, Q("polyline"), a)
        elif it["t"] == "icon":
            icon_instances += 1
            g = ET.SubElement(root, Q("g"), {"transform": f"translate({it['x']:.2f},{it['y']:.2f}) scale({it['scale']:.4f})"})
            inner = copy.deepcopy(icons[it["key"]])
            for el in inner.iter():
                if el.get("id"):
                    base = re.sub(r"--instance-\d+$", "", el.get("id"))
                    el.set("id", f"{base}--flow-{icon_instances}")
            g.append(inner)
    return ET.tostring(root, encoding="unicode")


def write_drawio(scene: Scene) -> str:
    icon_styles = load_drawio_icon_styles()
    mxfile = ET.Element("mxfile", {"host": "app.diagrams.net"})
    diagram = ET.SubElement(mxfile, "diagram", {"id": "enterprise-agent-factory-omni-telemetry-flow", "name": "CloudWatch Omni — fleet telemetry flow"})
    model = ET.SubElement(diagram, "mxGraphModel", {"dx": "1000", "dy": "600", "grid": "0", "gridSize": "10", "guides": "1", "tooltips": "1", "connect": "1",
                                                    "arrows": "1", "fold": "1", "page": "1", "pageScale": "1", "pageWidth": str(W), "pageHeight": str(H),
                                                    "math": "0", "shadow": "0"})
    rootel = ET.SubElement(model, "root")
    ET.SubElement(rootel, "mxCell", {"id": "0"})
    ET.SubElement(rootel, "mxCell", {"id": "1", "parent": "0"})
    for it in scene.items:
        if it["t"] == "rect":
            style = f"rounded={1 if it['rx'] else 0};arcSize={min(50, int(it['rx'] * 4)) if it['rx'] else 0};fillColor={it['fill']};strokeColor={it['stroke']};strokeWidth={it['sw']};html=1;"
            if it["dashed"]:
                style += "dashed=1;"
            c = ET.SubElement(rootel, "mxCell", {"id": it["id"], "value": "", "style": style, "vertex": "1", "parent": "1"})
            ET.SubElement(c, "mxGeometry", {"x": f"{it['x']:.1f}", "y": f"{it['y']:.1f}", "width": f"{it['w']:.1f}", "height": f"{it['h']:.1f}", "as": "geometry"})
        elif it["t"] == "text":
            width = est(it["s"], it["size"], it["bold"], it["mono"]) + 4
            x = {"start": it["x"], "middle": it["x"] - width / 2, "end": it["x"] - width}[it["anchor"]]
            align = {"start": "left", "middle": "center", "end": "right"}[it["anchor"]]
            style = f"text;html=1;fontSize={it['size']};fontColor={it['fill']};align={align};verticalAlign=middle;whiteSpace=nowrap;"
            if it["bold"]:
                style += "fontStyle=1;"
            if it["mono"]:
                style += "fontFamily=Liberation Mono;"
            c = ET.SubElement(rootel, "mxCell", {"id": it["id"], "value": it["s"], "style": style, "vertex": "1", "parent": "1"})
            ET.SubElement(c, "mxGeometry", {"x": f"{x:.1f}", "y": f"{it['y'] - it['size'] * 0.75:.1f}", "width": f"{width:.1f}", "height": f"{it['size'] * 1.5:.1f}", "as": "geometry"})
        elif it["t"] == "arrow":
            style = f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeWidth={it['sw']};strokeColor={it['colour']};endArrow=block;endFill=1;"
            if it["dashed"]:
                style += "dashed=1;"
            c = ET.SubElement(rootel, "mxCell", {"id": it["id"], "style": style, "parent": "1", "edge": "1"})
            g = ET.SubElement(c, "mxGeometry", {"relative": "1", "as": "geometry"})
            pts = it["points"]
            ET.SubElement(g, "mxPoint", {"x": f"{pts[0][0]:.1f}", "y": f"{pts[0][1]:.1f}", "as": "sourcePoint"})
            ET.SubElement(g, "mxPoint", {"x": f"{pts[-1][0]:.1f}", "y": f"{pts[-1][1]:.1f}", "as": "targetPoint"})
            if len(pts) > 2:
                arr = ET.SubElement(g, "Array", {"as": "points"})
                for x, y in pts[1:-1]:
                    ET.SubElement(arr, "mxPoint", {"x": f"{x:.1f}", "y": f"{y:.1f}"})
        elif it["t"] == "icon":
            size = 80 * it["scale"]
            c = ET.SubElement(rootel, "mxCell", {"id": it["id"], "value": "", "style": icon_styles[it["key"]], "vertex": "1", "parent": "1"})
            ET.SubElement(c, "mxGeometry", {"x": f"{it['x']:.1f}", "y": f"{it['y']:.1f}", "width": f"{size:.1f}", "height": f"{size:.1f}", "as": "geometry"})
    return ET.tostring(mxfile, encoding="unicode")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudWatch Omni — fleet telemetry flow (interactive)</title>
<style>
  :root { color-scheme: light; --ink: #0F1722; --muted: #5A6474; --bg: #F4F6FA; --panel: #FFFFFF; --stroke: #C3CBD6; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 "Liberation Sans", Helvetica, Arial, sans-serif; }
  header { padding: 18px 24px 0; }
  header h1 { font-size: 20px; margin: 0 0 4px; }
  header p { margin: 0; color: var(--muted); }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; padding: 12px 24px; }
  .controls button { border: 1px solid var(--stroke); background: var(--panel); border-radius: 8px; padding: 6px 12px; font: inherit; cursor: pointer; }
  .controls button[aria-pressed="true"] { border-color: var(--ink); font-weight: bold; }
  .controls .sw { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  main { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; padding: 0 24px 24px; }
  @media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
  .stage { background: var(--panel); border: 1px solid var(--stroke); border-radius: 10px; overflow: auto; }
  .stage svg { display: block; width: 100%; height: auto; min-width: 960px; }
  aside { background: var(--panel); border: 1px solid var(--stroke); border-radius: 10px; padding: 16px; align-self: start; position: sticky; top: 12px; }
  aside h2 { font-size: 16px; margin: 0 0 8px; }
  aside dl { margin: 0; }
  aside dt { font-weight: bold; margin-top: 10px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
  aside dd { margin: 2px 0 0; }
  aside code { font: 12px "Liberation Mono", Menlo, Consolas, monospace; background: var(--bg); padding: 1px 4px; border-radius: 4px; }
  svg .flow { cursor: pointer; transition: opacity .15s ease; }
  svg[data-active] .flow:not(.is-active) { opacity: .22; }
  svg[data-active] polyline.flow.is-active, svg polyline.flow:hover { stroke-width: 3.6; }
  svg rect.flow:hover, svg rect.flow:focus-visible { stroke-width: 2.4; outline: none; }
  svg.animating polyline[marker-end] { animation: march 1.2s linear infinite; }
  @keyframes march { to { stroke-dashoffset: -24; } }
  @media (prefers-reduced-motion: reduce) { svg.animating polyline[marker-end] { animation: none; } }
</style>
</head>
<body>
<header>
  <h1>CloudWatch Omni — fleet telemetry flow into the Observability hub</h1>
  <p>Click a log-group chip, a surface card or a table row to follow one telemetry type from its source account into the hub. Nothing here leaves the page: no scripts or data are loaded from the network.</p>
</header>
<div class="controls" role="toolbar" aria-label="Telemetry flows">
  <button type="button" data-select="">Show all</button>
  __BUTTONS__
  <button type="button" id="animate" aria-pressed="false">Animate copies</button>
</div>
<main>
  <div class="stage">
__SVG__
  </div>
  <aside aria-live="polite">
    <h2 id="panel-title">Pick a telemetry type</h2>
    <p id="panel-intro">One organization centralization rule, owned by the Observability account, copies the selected log groups, all metrics and the Context Graph of every source account into <code>/centralized/&lt;account&gt;/&lt;region&gt;&lt;logGroup&gt;</code>. The Omni space reads those copies; OAM links keep serving the CloudWatch console.</p>
    <dl id="panel" hidden>
      <dt>What it is</dt><dd id="d-what"></dd>
      <dt>Source</dt><dd id="d-source"></dd>
      <dt>Transport</dt><dd id="d-transport"></dd>
      <dt>In the hub</dt><dd id="d-destination"></dd>
      <dt>Shown in</dt><dd id="d-shown"></dd>
      <dt>Fields you will query</dt><dd><code id="d-fields"></code></dd>
    </dl>
  </aside>
</main>
<script>
(function () {
  "use strict";
  var DETAILS = __DETAILS__;
  var svg = document.getElementById("omni-flow");
  var panel = document.getElementById("panel");
  var title = document.getElementById("panel-title");
  var intro = document.getElementById("panel-intro");
  var fields = ["what", "source", "transport", "destination", "shown", "fields"];
  function select(key) {
    var nodes = svg.querySelectorAll(".flow");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].classList.toggle("is-active", !!key && nodes[i].getAttribute("data-flow") === key);
    }
    var buttons = document.querySelectorAll("[data-select]");
    for (var b = 0; b < buttons.length; b++) {
      buttons[b].setAttribute("aria-pressed", String(buttons[b].getAttribute("data-select") === (key || "")));
    }
    if (key && DETAILS[key]) {
      svg.setAttribute("data-active", key);
      title.textContent = DETAILS[key].title;
      intro.hidden = true;
      panel.hidden = false;
      for (var f = 0; f < fields.length; f++) {
        document.getElementById("d-" + fields[f]).textContent = DETAILS[key][fields[f]];
      }
    } else {
      svg.removeAttribute("data-active");
      title.textContent = "Pick a telemetry type";
      intro.hidden = false;
      panel.hidden = true;
    }
  }
  svg.addEventListener("click", function (event) {
    var target = event.target.closest ? event.target.closest(".flow") : null;
    if (target) { select(target.getAttribute("data-flow")); }
  });
  svg.addEventListener("keydown", function (event) {
    if ((event.key === "Enter" || event.key === " ") && event.target.classList.contains("flow")) {
      event.preventDefault();
      select(event.target.getAttribute("data-flow"));
    }
  });
  var buttons = document.querySelectorAll("[data-select]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function () { select(this.getAttribute("data-select")); });
  }
  var animate = document.getElementById("animate");
  animate.addEventListener("click", function () {
    var on = !svg.classList.contains("animating");
    svg.classList.toggle("animating", on);
    animate.setAttribute("aria-pressed", String(on));
  });
  select("");
})();
</script>
</body>
</html>
"""


def write_html(svg_text: str) -> str:
    buttons = "\n  ".join(
        f'<button type="button" data-select="{key}" aria-pressed="false"><span class="sw" style="background:{spec["colour"]}"></span>{html.escape(spec["label"])}</button>'
        for key, spec in FLOWS.items()
    )
    details = json.dumps(FLOW_DETAILS, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__BUTTONS__", buttons).replace("__SVG__", svg_text).replace("__DETAILS__", details)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(BLUEPRINT), help="Blueprint root; files land in assets/ and assets/omni/.")
    args = parser.parse_args()
    out = pathlib.Path(args.out_dir)
    scene = build()
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "assets" / "omni").mkdir(parents=True, exist_ok=True)
    (out / "assets" / "enterprise-agent-factory-omni-telemetry-flow.svg").write_text(write_svg(scene, interactive=False) + "\n", encoding="utf-8")
    (out / "assets" / "enterprise-agent-factory-omni-telemetry-flow.drawio").write_text(write_drawio(scene) + "\n", encoding="utf-8")
    (out / "assets" / "omni" / "telemetry-flow-interactive.html").write_text(write_html(write_svg(scene, interactive=True)), encoding="utf-8")
    print(f"wrote 3 files under {out / 'assets'} ({len(scene.items)} drawables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
