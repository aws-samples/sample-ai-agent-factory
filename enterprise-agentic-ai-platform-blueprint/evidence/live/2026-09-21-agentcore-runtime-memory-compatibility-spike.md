# Live evidence — AgentCore Runtime and Memory compatibility

- **Date:** 2026-09-21
- **Status:** PASS for the bounded `us-west-2` compatibility envelope below
- **Product Git HEAD:** `88d5381d371944a4dfdc31d424917bf767a92c17`
- **SDK contract:** Boto3 and Botocore `1.43.97`
- **Topology:** one ephemeral container Runtime, one CMK-encrypted Memory, and isolated image-build prerequisites

This is a sanitized summary. It contains no AWS account IDs, access keys,
credentials, Runtime IDs, Memory IDs, event IDs, KMS key IDs, ARNs, image
repository URI, build ID, session ID, or other account-scoped resource
identifier. Raw evidence remains in session scratch and is not committed.

## Preconditions

The exact revision passed:

- Python and TypeScript CodeQL on the PR head;
- 118 focused offline tests against pinned Botocore service models;
- Python compilation and standalone CLI import/help smoke testing;
- `npm run build`;
- `npm run lint`;
- `npm run scrub`;
- Prettier and whitespace checks;
- exact request serialization for Runtime, Memory, invocation, and event APIs;
- independent Critical/High review of ownership, provenance, side-effect scope,
  cleanup, container behavior, and evidence safety; and
- a second independent Critical/High review of the final container-base change.

A read-only preflight confirmed the expected nonproduction Platform account,
`us-west-2`, the documented AgentCore APIs, and zero exact-name Runtime, Memory,
ECR, IAM-role, or CloudFormation collisions before creation.

## Image provenance and vulnerability gate

The host had no Docker or Finch engine, so a temporary native ARM CodeBuild
project cloned the advertised feature branch, checked out the exact 40-character
product revision in detached mode, asserted `HEAD` equality, built for
`linux/arm64`, and pushed to an immutable scan-on-push ECR repository. The
repository, builder, roles, logs, CMK, and alias were isolated from retained
Platform resources and carried the five allocation tags.

The first exact-revision image used the unqualified Python slim base. Its ECR
scan completed with one High zlib finding, so the image was not invoked. Two
candidate rebuilds were also rejected:

| Candidate                                  | Critical | High | Outcome  |
| ------------------------------------------ | -------: | ---: | -------- |
| Python slim Bookworm                       |        4 |   14 | Rejected |
| Python Alpine 3.22                         |        3 |   18 | Rejected |
| AWS-maintained Lambda Python 3.13 / AL2023 |        0 |    0 | Selected |

The selected base was pinned in the Dockerfile to the live-observed ARM64 digest.
A new product commit passed review and CodeQL, then the normal buildspec rebuilt
from that exact commit. The final image scan completed with zero findings. Its
image configuration independently verified:

- operating system `linux` and architecture `arm64`;
- numeric non-root user `10001`;
- exposed TCP port `8080`;
- entrypoint `python -u agent.py`; and
- no inherited Lambda handler command.

The final Runtime consumed the ECR image by digest, never by mutable tag.

## Prerequisite defects found and closed

No Runtime or Memory was created until all four findings were corrected and the
resulting policies were revalidated:

1. A bare commit-SHA fetch depended on Git server unadvertised-object behavior.
   The builder now clones the advertised feature branch, checks out the exact
   revision, and asserts `HEAD` equality.
2. The first Runtime-role policy copied `Converse` and `ConverseStream` API names
   as IAM actions. Access Analyzer rejected them; the valid direct-inference
   actions remain explicitly denied.
3. The CodeBuild log resource appended a wildcard to a LogGroup ARN that already
   ended in one, producing `:*:*`. The corrected role uses the exact generated
   LogGroup ARN.
4. The buildspec read the pushed image digest with `DescribeImages` but the role
   omitted that action. The repository-scoped permission was added.

The correction changed only the image-builder role policy in place. The role and
CodeBuild project identities remained stable. Access Analyzer returned zero
findings for both identity policies and both trust policies. A real CodeBuild run
then proved encrypted log delivery, ECR authentication, layer upload, image push,
and digest read.

## Live results

| Assertion                 | Result                                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------------------------- |
| Caller identity gate      | Expected nonproduction Platform account confirmed before every side effect                            |
| Image contract            | Exact reviewed revision, digest-pinned, zero scan findings, `linux/arm64`, non-root, port 8080        |
| Memory creation           | Reached `ACTIVE`                                                                                      |
| Memory ownership          | Exact name, run-specific description, expected CMK and seven-day event expiry matched                 |
| Runtime creation          | Reached `READY`                                                                                       |
| Runtime ownership         | Exact name, account/Region ARN scope, image digest, role, description, and five tags matched          |
| Runtime invocation        | Exact deterministic handshake returned the expected marker, ping fingerprint, and `runtimeReady=true` |
| Memory event write        | `CreateEvent` returned an event identifier                                                            |
| Memory event read         | `GetEvent` reproduced the exact event, actor, session, memory, and text-union marker                  |
| Evidence status           | `passed`; no unknown or failure event                                                                 |
| Runtime cleanup           | Deleted first and polled absent                                                                       |
| Memory cleanup            | Deleted second and polled absent                                                                      |
| Runner residual inventory | Runtime `false`; Memory `false`                                                                       |
| KMS service grants        | Zero after Memory deletion                                                                            |

Observed lifecycle duration was approximately 2 minutes 36 seconds for Memory
to become active, 21 seconds for Runtime readiness, 9 seconds for the first
Runtime invocation, less than 1 second for the Memory event round trip, 12
seconds for Runtime deletion, and 2 minutes 37 seconds for Memory deletion.
These are single-run observations, not SLOs or capacity measurements.

## Cleanup and independent inventory

The runner deleted Runtime before Memory in `finally`, then wrote a terminal
completed-run marker so AgentCore idempotency tokens cannot be reused after
deletion. A separate AWS CLI inventory, independent of the runner facade,
confirmed:

- zero exact-name Runtimes and zero Memories;
- zero AgentCore-created KMS grants;
- the one service-created, zero-byte Runtime log group was removed by exact name;
- the prerequisite CloudFormation stack reached `DELETE_COMPLETE`;
- the ECR repository and all original/rejected/final images were absent;
- the CodeBuild project and both IAM roles were absent;
- the encrypted CodeBuild log group and Runtime log group were absent;
- the KMS alias was absent; and
- the prerequisite CMK was disabled in its intentional seven-day
  `PendingDeletion` window, with no grants, for deletion on 2026-09-28.

No active campaign resource remained.

## Audit observation

The bounded CloudTrail lookup returned Runtime and Memory control-plane deletion
activity but was not used as proof for Runtime invocation or Memory data-plane
events. End-to-end pipeline audit correlation for `InvokeAgentRuntime`, Memory
events, Gateway calls, and generated-agent traces remains a release gate. This
record does not infer audit coverage from absent lookup rows.

## Bounded conclusion and remaining gates

This proves the pinned SDK request/response shapes, native Runtime container
handshake, short-term Memory event round trip, exact live ownership checks,
cleanup ordering, grant retirement, and zero-active-residue contract in one
`us-west-2` nonproduction Platform account.

It does **not** prove:

- pipeline-owned Runtime or Memory deployment in a Workstream account;
- generated-agent `LiteLLMModel` inference through the Platform inference
  Gateway;
- generated-agent `MCPClient` tool calls through the Workstream Tool Gateway;
- Runtime-to-Memory integration inside the agent process;
- long-term Memory strategies or retrieval;
- VPC/private-network Runtime mode;
- cross-account trust negative twins;
- production, EMEA, load, concurrency, quota, soak, chaos, upgrade, or
  interrupted-deployment behavior;
- complete OTEL/CloudTrail correlation; or
- a measured 24-hour cost baseline.

Those remain release gates. This compatibility result authorizes the next
pipeline-owned Runtime/Memory implementation slice; it is not a broad-adoption
readiness claim.
