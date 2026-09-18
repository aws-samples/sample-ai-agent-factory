# Adversarial verification framework

A real-test framework for the three-account deployment
(**Management/Governance**, **Platform**, **Workstream**). It is a *scaffold with
teeth*, not a set of passing placeholders: the harness and its own unit tests
are complete and run offline, while every live case is catalogued, gated, and
reported as an open gap until a probe implements it.

Nothing here calls AWS. All live interaction happens in probes you register
under `cases/probes/`.

## Layout

```
tests/adversarial/
├── README.md                      this file
├── conftest.py                    sys.path bootstrap, marker registration, session fixtures
├── fixtures/
│   └── manifest.example.json      account/role manifest template (no account ids, no secrets)
├── harness/                       the framework — AWS-free by construction
│   ├── manifest.py                account/role schema, resolution, manifest SHA
│   ├── livemode.py                the live-mode gate (skip / error / run)
│   ├── outcome.py                 observed-outcome capture and evidence classification
│   ├── assertions.py              assertions + the positive-twin ledger
│   ├── evidence.py                sanitized evidence records and bundle writer
│   ├── catalog.py                 the declarative case matrix
│   ├── provenance.py              commit SHA / manifest SHA / catalog SHA
│   ├── sanitize.py                redaction and the refuse-to-record check
│   └── errors.py                  the exception hierarchy
├── cases/
│   ├── test_catalog_cases.py      the single catalog-driven live runner
│   └── probes/__init__.py         the probe registry — the plug-in point
└── unit/                          tests of the harness itself (no AWS, no network)
```

## Running

Harness unit tests only — this is what ordinary CI runs:

```bash
pytest tests/adversarial/unit -v
```

Collect the live cases without running them (they skip, loudly):

```bash
pytest tests/adversarial/cases -v
```

Run live cases against a real three-account deployment:

```bash
export AGENTICAI_ADVERSARIAL_LIVE=1
export AGENTICAI_ADVERSARIAL_MANIFEST=/secure/path/manifest.json
export AGENTICAI_ACCOUNT_MANAGEMENT=... AGENTICAI_ACCOUNT_PLATFORM=... AGENTICAI_ACCOUNT_WORKSTREAM=...
export AWS_ACCESS_KEY_ID_PLATFORM=... AWS_SECRET_ACCESS_KEY_PLATFORM=... AWS_SESSION_TOKEN_PLATFORM=...
# ... and the MANAGEMENT / WORKSTREAM equivalents, or AWS_PROFILE_<PREFIX> per account
export AGENTICAI_ADVERSARIAL_EXTERNAL_ID=...
export AGENTICAI_ADVERSARIAL_EVIDENCE_DIR=build/adversarial-evidence
pytest tests/adversarial/cases -v
```

No `pytest.ini` or `package.json` change is needed: `testpaths` already includes
`tests`, and the `adversarial` / `adversarial_live` markers are registered by
this subtree's `conftest.py`.

## The live-mode gate

| State | Condition | Behaviour |
|---|---|---|
| off | `AGENTICAI_ADVERSARIAL_LIVE` unset and `AGENTICAI_ADVERSARIAL_REQUIRE_LIVE` unset | live cases **skip**, with the reason stated |
| requested but unavailable | live mode on, but the manifest is missing/invalid, an account id is unmapped, credentials are absent, or the caller identity does not match the declared account | the session **errors** |
| required but unavailable | `AGENTICAI_ADVERSARIAL_REQUIRE_LIVE=1` and anything above is missing | the session **errors** |
| available | manifest resolves, credentials present, identity verified per account | live cases **run** |

"Requested but unavailable" is deliberately an error, never a skip. A run that
asked for live evidence and produced none is the false-green this suite exists
to prevent.

Identity verification is injected (`identity_probe`), so the harness unit tests
exercise every gate state without an SDK or a network call.

## Account/role manifest

`fixtures/manifest.example.json` is the template. Copy it outside the
repository, keep the structure, and supply account ids through the environment.

Rules the loader enforces:

* exactly three accounts for this **nonproduction validation profile** —
  `management-governance`, `platform`, `workstream`; production account
  separation is defined by the architecture RFC, not by this test manifest;
* a literal `accountId` is **rejected**; declare `accountIdEnv` instead, so no
  account number is ever committed;
* credential sources may only be `env-prefix`, `profile`, or
  `assume-role-chain`. A `csv`/`file`/`static` source is rejected outright, and
  any value that looks like an access-key export (`*_credentials.csv`) is
  refused — the harness never opens one;
* role names must be bare names, not ARNs (an ARN would embed an account id);
* each account must declare the role refs the catalog addresses
  (`REQUIRED_ROLES` in `harness/manifest.py`), including an
  `unprivileged_probe` attacker identity;
* the whole document is scanned for secrets before it is accepted.

The declaration hashes to `manifestSha`, which every evidence record carries.

## Evidence schema

Every case produces one sanitized record (`evidence.jsonl`) plus a run
`summary.json`. Required fields:

| Field | Why |
|---|---|
| `testId`, `caseId`, `domain`, `expectation`, `severity` | which case ran, under which test |
| `commitSha` (40-hex), `manifestSha` (64-hex), `catalogSha` (64-hex) | ties the record to code, topology and control matrix |
| `principal` (`principalRef`, `accountAlias`, `roleName`, `privilege`) | who attacked — by alias, never an account number |
| `target`, `region` | what was attacked, where |
| `expectedResult` / `observedResult` / `outcomeClass` | the claim and the measurement |
| `requestId` or `traceId` | correlates with the provider's own logs (mandatory for live records) |
| `auditEvidence[]` (`source`, `locator`, `matchedFields`) | independent corroboration; mandatory for any passing negative |
| `positiveTwin.caseId` / `positiveTwin.testId` | the authorized twin that passed in the same run |
| `verdict` | `pass` or `fail` |

A record is sanitized, then re-scanned, then validated. If anything sensitive
remains, or a required field is missing, the record is refused — so a case
cannot pass while silently producing unusable evidence.

Sanitization maps known account ids to `<account:ALIAS>`, redacts unknown
12-digit ids, access-key ids, session tokens, bearer tokens, private keys,
emails and credential-file references, and preserves commit/manifest hashes and
request ids. Sanitization failures report the finding *kind* and path, never the
matched value.

## What counts as proof

`harness/outcome.py` classifies every observed result, and the assertions accept
only the matching class:

| Observed | Class | Usable as authorization proof? |
|---|---|---|
| explicit `AccessDenied` / `AccessDeniedException` / `UnauthorizedOperation`, 403 | `AUTHORIZATION_DENIAL` | yes |
| 401 / `Unauthorized` | `AUTHENTICATION_DENIAL` | no — proves the caller was not identified |
| `ThrottlingException`, 429 | `RATE_LIMIT` | no — only proof for rate-limit cases |
| guardrail trace with `guardrail_intervened` | `GUARDRAIL_INTERVENTION` | no — only proof for guardrail cases |
| `ResourceNotFoundException`, 404 | `NOT_FOUND` | **no** — the call may never have reached a policy decision (only the `ABSENT` teardown expectation accepts it) |
| `ValidationException`, 400 | `VALIDATION` | **no** — a malformed request records no decision |
| 5xx, `InternalServerError`, `ServiceUnavailable` | `SERVER_ERROR` | **no** — indistinguishable from an outage, and may mask an allow |
| timeouts | `TIMEOUT` | **no** — the request may have been authorized and succeeded server-side |
| connection failures | `NETWORK` | **no** — never reached the service |
| `ExpiredToken`, `InvalidClientTokenId`, `UnrecognizedClientException` | `CREDENTIAL` | **no** — a harness misconfiguration, not a control |
| bare nonzero exit code with no service error code | `UNSPECIFIED_FAILURE` | **no** |

Additional rules:

* a denial case must name the exact error code(s) and HTTP status it accepts;
* SCP cases additionally require the `explicit deny in a service control policy`
  message fragment, so an identity-policy denial cannot masquerade as an SCP;
* a denial with no `requestId`/trace id is rejected — it cannot be corroborated;
* a **successful** call can never satisfy a denial assertion. This is what makes
  the suite fail when a control is removed
  (`unit/test_case_runner.py::test_a_control_removal_fails_the_case_end_to_end`);
* `tools/list` filtering cannot pass on an empty listing, and cannot pass unless
  the tools the principal *should* see are present.

## Positive-twin requirement

Every negative expectation declares a `positiveTwin` whose expectation is
`ALLOW`. At run time the `TwinLedger` records which twins passed, and a negative
case raises `PositiveTwinMissing` unless its twin passed **in the same run**.
Without this, a wholly broken deployment would "prove" every control at once.

Positives are ordered before negatives by `harness.catalog.execution_order`.

## Case catalog

`harness/catalog.py` covers all twelve required domains. `validate_catalog`
fails if a domain has no negative case, a negative has no valid twin, a denial
names a forbidden proof code, a rate-limit case names a non-throttle code, or a
principal ref does not exist in the manifest contract.

| Domain | Representative cases |
|---|---|
| `scp` | model allow-list, guardrail required, approved guardrail only, VPCE-only egress, guardrail/gateway/registry mutation lockdown, region pinning, ECR Public, non-catalogued Lambda invoke, developer mutation of platform-owned resources |
| `iam-sts` | missing/wrong `ExternalId`, privilege escalation to platform admin, read-only `AgentBuilderInspectRole`, governance cannot write workloads |
| `inference-gateway` | unauthenticated call, non-allow-listed model, cross-tenant inference profile, prompt-attack guardrail block, direct Bedrock bypass |
| `tool-gateway` | **unexposed-tool attack** (call denied *and* filtered from `tools/list`), forged tenant context, direct invocation of the backing target |
| `memory-isolation` | substituted `actorId`, namespace outside the static template, cross-tenant data read, `kms:Decrypt` outside `ViaService` |
| `registry` | authorized `RegistryReaderRole`, wrong-account and wrong-`ExternalId` assumptions, cross-tenant discovery/read, guessed record IDs, unauthorized create/update/approve/delete, self-approval, approved-reference mutation, and direct `AgentRegistrationApi` bypass |
| `pipeline-bypass` | direct `CreateStack`, out-of-band runtime update, borrowing the pipeline role, push to a protected branch |
| `supply-chain-tamper` | image tag overwrite, manifest-SHA mismatch blocks promotion, catalogue drift detected |
| `rate-limiting` | RPM, TPM (reconciled against token usage), CPS, catch-all, zero-rate, and **fail-open**: authorization must still deny when the limiter cannot evaluate |
| `failure-injection` | kill switch, circuit breaker, guardrail unavailability fails closed, alarm fires on injected errors |
| `rollback` | failing evaluation gate blocks promotion, canary breach rolls back, rollback reverts version *and* manifest SHA, approval cannot be skipped |
| `teardown` | ephemeral resources absent, no orphaned roles, retained evidence cannot be deleted |

Rate-limit cases are tagged `rpm` / `tpm` / `cps` / `catch-all` / `fail-open`,
and the validator fails if any facet loses coverage.

## Plugging in a live case

A probe performs one case and returns what it observed. It never asserts — the
harness owns every assertion, so no probe can weaken a rule.

1. Create `cases/probes/probe_<domain>.py` (the `probe_` prefix is how it is
   discovered).
2. Register one function per case id:

```python
from harness import AuditEvidence, AuditSource, ObservedOutcome
from cases.probes import ProbeContext, ProbeResult, probe

UNAPPROVED_MODEL = "amazon.titan-text-express-v1"

@probe("SCP-01-N")
def non_allowlisted_model_denied(ctx: ProbeContext) -> ProbeResult:
    client = ctx.session("workstream.agent_runtime").client(
        "bedrock-runtime", region_name=ctx.region
    )
    try:
        client.converse(modelId=UNAPPROVED_MODEL, messages=[...])
        outcome = ObservedOutcome.from_success()
    except Exception as exc:                       # botocore ClientError
        outcome = ObservedOutcome.from_client_error(exc)
    return ProbeResult(
        outcome=outcome,
        audit_evidence=(
            AuditEvidence(
                source=AuditSource.CLOUDTRAIL,
                locator="<cloudtrail event id>",
                matched_fields={"errorCode": outcome.error_code},
            ),
        ),
    )
```

3. Nothing else. `cases/test_catalog_cases.py` picks the case up, applies the
   expectation's assertion, and writes the sanitized evidence record.

`ProbeContext` gives you `region`, `account_id()`, `role_arn()`,
`external_id()`, and `session(principal_ref)` — which derives base credentials
from the manifest's declared source and then assumes the named role. There is no
path in the context to an on-disk credential file.

Expectations that need more than an `ObservedOutcome` take their inputs from
`ProbeResult.extras`:

| Expectation | Extras |
|---|---|
| `NOT_EXPOSED` | `listed_tools`, `forbidden_tools`, `expected_tools` |
| `ROLLED_BACK` | `rollback_event_observed`, `active_version`, `previous_version`, `active_manifest_sha`, `previous_manifest_sha` |
| `DETECTED` | `signal_observed`, `signal_name`, `baseline_signal_observed` |

Until a probe exists for a catalogued case, that case **fails** in live mode
with "no live probe registered". Unimplemented control checks are open gaps, not
tolerated conditions — which is why they are never `skip` or `xfail`.

## Safety

* no secrets, no account ids and no credential-file references in this subtree;
* the harness never opens a CSV credential export, and rejects a manifest that
  names one;
* the harness makes no AWS call, opens no socket and imports no SDK — enforced by
  `unit/test_no_aws_dependency.py`;
* live probes are read-mostly attacks; anything that mutates shared state
  (SCP attach/detach, kill-switch toggling, failure injection) belongs behind the
  sandbox OU and explicit confirmation, per the safety guardrails in
  `tasks/todo.md`.
