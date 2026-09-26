# Live evidence — Gateway OTEL rate-limit span correlation

- **Date:** 2026-09-25
- **Status:** PASS — the reproduced `us-west-2` blocker (no `aws/spans`
  records, 2026-09-18) is closed: on the pipeline-owned nonproduction
  inference Gateway every allowed and every throttled request produced one
  rate-limit decision span, and each span was matched one-to-one to its
  request
- **Account / Region:** Platform, `us-west-2`
- **Target:** the pipeline-owned nonproduction inference Gateway (Bedrock
  Mantle target, native rate limit `models-nonprod`, guardrail REQUEST
  interceptor), unchanged by the probe
- **Probe:** a local, reversible runner that attaches observability to the
  existing Gateway, sends real traffic and restores every setting it changed
  (it never touches the Gateway, its targets or its rate limit)

## What was different from the blocked 2026-09-18 attempt

The earlier attempt ran against an isolated spike Gateway and polled
`aws/spans` for six minutes without a single record. This run used the
pipeline-owned Gateway and the documented delivery chain end to end:

1. **Transaction Search** (account-wide, recorded and restored): X-Ray trace
   segments to CloudWatch Logs, the `Default` indexing rule at 100 %, and the
   X-Ray `logs:PutLogEvents` resource policy on `aws/spans`.
2. **Two vended deliveries on the Gateway ARN:** `TRACES` → an `XRAY`
   delivery destination, and `APPLICATION_LOGS` → a dedicated
   `/aws/vendedlogs/...` log group.

Creating the `TRACES` → `XRAY` delivery **without** Transaction Search is
refused by CloudWatch Logs (`ValidationException: X-Ray Delivery Destination
is supported with CloudWatch Logs as a Trace Segment Destination`), so a
Gateway cannot emit spans without the account-level switch. That makes
Transaction Search a hard prerequisite, not an optional console feature.

## Traffic

| Requests | Model                                          | HTTP | Why                                  |
| -------- | ---------------------------------------------- | ---- | ------------------------------------ |
| 3        | the allocated model                            | 200  | inside its RPM/TPM entry             |
| 3        | a Gateway-exposed model outside the allocation | 429  | matches only the zero-rate `*` entry |

No rate limit was changed: the 429s come from the deployed zero-rate
wildcard, the same exact control proven on 2026-09-24.

## Correlation — 6/6

`aws/spans` held exactly six decision spans in the window
(`AgentCore.Gateway.InvokeHttp`, kind `SERVER`), each carrying
`aws.agentcore.gateway.throttle.customer.decision` and `.evaluated`:

| Request | Span                                                      | Joined via                                                                         | Limit key / entry / metric          |
| ------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------- |
| 3 × 429 | `decision = throttled`, `http.response.status_code = 429` | the span's own `aws.request.id` equals the response's request id                   | `models-nonprod` / `*` / `requests` |
| 3 × 200 | `decision = allowed`, `http.response.status_code = 200`   | the Gateway application log's `request_id` → `trace_id` equals the span's trace id | not emitted for allowed decisions   |

Every request matched exactly one span with the expected decision and status;
no span was left unmatched.

**Finding for operators:** throttled spans carry the request id, the limit
key, the matched entry and the metric; **allowed** spans carry none of them.
Per-request correlation of an admitted call therefore needs the Gateway's
`APPLICATION_LOGS` delivery (for `request_id` → `trace_id`), and per-limit
dashboards can only count throttles, not admissions per limit. The
documented Logs Insights queries on `aws/spans` work as written for
throttles.

## Restoration

| Setting                                      | Before            | During                               | After (verified independently)                     |
| -------------------------------------------- | ----------------- | ------------------------------------ | -------------------------------------------------- |
| X-Ray trace segment destination              | `XRay` / `ACTIVE` | `CloudWatchLogs` / `ACTIVE` (13:38Z) | `XRay` / `ACTIVE` (13:51Z, after ~6 min `PENDING`) |
| `Default` indexing rule                      | 0 %               | 100 %                                | 0 %                                                |
| X-Ray logs resource policy                   | none              | one, probe-named                     | none                                               |
| Delivery sources / destinations / deliveries | 0 / 0 / 0         | 2 / 2 / 2                            | 0 / 0 / 0                                          |
| Probe log group                              | absent            | one, 1-day retention                 | absent                                             |

## Residual risks

- Enabling Transaction Search is an account- and region-wide change with its
  own cost (span ingestion into CloudWatch Logs at 100 % indexing); the
  blueprint does not enable it, and a deployer who wants Gateway spans must
  opt in explicitly.
- Proven in `us-west-2` on the nonproduction Gateway; production and EMEA
  regions were not exercised by this probe.
