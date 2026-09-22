# Live evidence — AgentCore Identity M2M credential-provider compatibility

- **Date:** 2026-09-22
- **Status:** PASS for the bounded `us-west-2` compatibility envelope below
- **Product Git HEAD:** `0fc98d7468123eea1967275b6d601ab7dbdbcf35`
- **SDK contract:** Boto3 and Botocore `1.43.98`
- **Topology:** one ephemeral AgentCore workload identity, one OAuth2 credential
  provider, and one transient Cognito app-client secret minted and deleted
  in-process, against the existing production inference Gateway

This is a sanitized summary. It contains no AWS account IDs, access keys,
credentials, workload identity IDs, credential-provider IDs, client-secret IDs
or values, bearer tokens, KMS key IDs, ARNs, or other account-scoped resource
identifier. Raw evidence remains in session scratch and is not committed.

## Scope and relationship to prior evidence

The AgentCore Identity M2M → CUSTOM_JWT → `LiteLLMModel` path is independently
live-verified by the central-inference-Gateway spike
(`evidence/live/2026-09-18-agentcore-gateway-spike.md`). This spike adds the
**credential-provider-specific** proof: an AgentCore workload identity and
OAuth2 credential provider brokering a Cognito client-credentials M2M token that
`LiteLLMModel` then uses against the Gateway's OpenAI-compatible inference API.
It is an isolated Platform-account API-contract proof, not Workload-pipeline
integration; the deployed identity artifacts were ephemeral and torn down.

## Preconditions

The exact revision passed:

- Python and TypeScript CodeQL on the PR head;
- 97 focused offline tests against pinned Botocore service models;
- Python compilation and standalone CLI import smoke testing;
- `npm run scrub`;
- independent review of ownership, provenance, secret-handling, cleanup, and
  evidence safety.

A read-only preflight confirmed the expected Platform account, `us-west-2`, the
`Prod-InferenceGateway` stack at `UPDATE_COMPLETE` with its six required
outputs, and the Cognito app client's client-credentials M2M configuration
before any resource was created.

## Defects found and closed during live bring-up

Four live-discovered defects were fixed forward as reviewed commits before the
run passed; none was worked around. All are recorded in the spike README's
"Known limitations (live-discovered)" section.

| # | Defect                                                                                   | Fix                                                                                  |
| - | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| 1 | `ListWorkloadIdentities` / `ListOauth2CredentialProviders` cap `maxResults` at 20        | Pinned `LIST_PAGE_SIZE = 20`; SDK-tied regression                                    |
| 2 | Describe-based secret reader incompatible with a client rotated to the multi-secret store | Mint-and-delete redesign: spike mints its own ephemeral secret in-process            |
| 3 | `ListUserPoolClientSecrets` does not accept `MaxResults`                                 | Drop `MaxResults`, drain `NextToken`; fake and pinned-SDK regression enforce it      |
| 4 | Inference API is a sibling of `/mcp`, not nested under it (`.../mcp/inference/v1` → 400) | `inference_base_url()` strips `/mcp`; HTTP error path now captures the response body |

## Result — 24-event evidence stream, `status: passed`, `unknowns: []`

Positive path:

- workload identity created with five allocation tags; credential provider
  created and reached `READY`;
- Cognito client verified with the M2M flow enabled and the exact Gateway scope;
- workload access token obtained; M2M resource token obtained for the exact
  scope;
- model discovery returned **49 models**;
- `LiteLLMModel` returned content on both non-streaming and streaming
  invocations.

Adversarial twins — all denied with exact statuses:

| Vector               | Error                   | HTTP |
| -------------------- | ----------------------- | ---- |
| wrong-scope          | `ValidationException`   | 400  |
| wrong-provider       | `ValidationException`   | 400  |
| wrong-workload-token | `AccessDeniedException` | 403  |

Secret-lifecycle safety (mint-and-delete on the live production client):

- the transient second client secret minted at deploy carried the same
  fingerprint that was deleted at cleanup;
- the client's real M2M secret was never touched — an independent read-only
  inventory confirmed the client held exactly one secret after the run.

Teardown and residue:

- credential provider deleted → workload identity deleted → transient client
  secret deleted;
- final `residual-inventory` recorded `clientMaterial=false`, `provider=false`,
  `workload=false`;
- an independent read-only live inventory confirmed zero `aiaf-idm2m` workload
  identities, zero `aiaf-idm2m` credential providers, and client-secret count 1.

## Bounded envelope and residual risks

- **Region:** `us-west-2` only. No EMEA/APAC AgentCore Identity region has been
  exercised for this path.
- **Isolation:** a standalone API-contract proof, not generated-agent or
  Workload-pipeline integration.
- **Rate/negative limits:** exact-status auth negatives are proven; a
  destructive rate-limit twin against the production limit was not run.

---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
