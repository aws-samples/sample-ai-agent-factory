# ADR-0002 — Infrastructure as code authored in AWS CDK, not Terraform

- **Status:** Accepted. Relocated from deviation D-02 to this ADR on 2026-09-18; the decision itself
  is unchanged.
- **Date:** Original decision predates this file; relocated 2026-09-18.
- **Related:** [RFC-0001 §3](../architecture/target-state-architecture.md#3-d-01-and-d-03-as-historical-inputs),
  [`README.md`](../../README.md) §3.2
- **Why this file exists:** D-02 was recorded in the README beside two *topology* deviations, D-01 and
  D-03. That placement invited readers to treat "CDK versus Terraform" as a deployment-pattern choice
  on a par with "distributed versus centralised". It is not. It is an implementation-technology
  decision that is orthogonal to topology and applies identically to every topology. Relocating it
  removes a false choice from the customer's decision surface.

---

## Context

The source specification this blueprint derives from illustrates its controls with Terraform. The
blueprint had to choose an authoring technology for a multi-account AWS Organization with service
control policies, VPC endpoint policies, resource-based policies, Cedar policies, customer-managed
key wiring, guardrail attachment, and CI/CD pipelines — and had to preserve the *control semantics* of
the specification whatever it chose.

Three properties drove the choice:

1. **The controls are policy documents, and policy documents must be diffable and testable.** The
   artifact that matters for review is the synthesized IAM policy, resource policy, and SCP body.
2. **Type-level composition.** A model allow-list that flows into a service control policy, a VPC
   endpoint policy, a router configuration, and an execution role should be one constant with one
   type, not four copies in four files.
3. **Policy-as-code scanning in the build.** cdk-nag's `AwsSolutionsChecks` and `NIST80053R5Checks`
   Aspects run over the construct tree at synth, so a control regression fails the build rather than
   the deployment.

---

## Decision

1. **Infrastructure is authored in AWS CDK — TypeScript, with Python where the surrounding tooling is
   Python — synthesizing CloudFormation.**
2. **The specification's Terraform examples are re-implemented as CDK constructs with equivalent
   control semantics.** Equivalence is asserted, not assumed: same SCP bodies, same VPC endpoint
   policies, same resource-based policies, same Cedar policies, same customer-managed key wiring, same
   guardrail attachment.
3. **Every deviating construct cites the specification section it implements**, in a source comment,
   so a reviewer can trace a control to its requirement.
4. **cdk-nag `AwsSolutionsChecks` and `NIST80053R5Checks` Aspects are mandatory**, and every
   suppression carries an inline `SEC-0NN` marker with an owner, a rationale, and a compensating
   control.
5. **A build-time SCP size check runs**, because service control policies have a hard size limit that
   is easy to exceed when they are generated.
6. **The synthesized template is the review artifact, not the source.** Source-level intent and the
   effective policy can differ, particularly with IAM policy minimization enabled. Diffs, adversarial
   tests, and evidence all key on the synthesized output.
7. **AWS Account Factory for Terraform is explicitly rejected** for account vending in this blueprint.
   Introducing a second IaC toolchain solely for account provisioning would split the control surface
   across two state models and two review workflows.

This decision is **topology-independent**. It applied to the distributed pattern, it applied to the
centralised pattern, and it applies unchanged to the converged target topology in RFC-0001.

---

## Consequences

### Positive

- One language and one type system for controls, so shared invariants such as the model allow-list are
  single constants with compile-time checking.
- Policy-as-code scanning runs at synth, inside the pull request.
- No separate state backend to secure, and no state-locking failure mode; CloudFormation holds state.
- CDK Pipelines gives a self-mutating deployment path in the same codebase as the infrastructure.

### Negative, and accepted

- **CDK abstracts CloudFormation, and CloudFormation abstracts the API.** Two layers between intent and
  effect. Mitigated by consequence 6 above: review the synthesized template.
- **Construct renames and stack moves cause resource replacement.** This is a CloudFormation logical-id
  property, not a CDK bug, and it is the single largest hazard in any refactor of this repository. See
  [RFC-0001 §15](../architecture/target-state-architecture.md#15-cdk-migration-and-replacement-hazards).
- **Customers standardised on Terraform must either adopt CDK for this blueprint or port it.** Real
  adoption friction, accepted because the specification's controls, not its Terraform, are what this
  blueprint reproduces.
- **CloudFormation coverage can lag new service APIs.** Custom resources close the gap and are a
  maintenance cost.

### Neutral

- The choice does not affect the account model, the Gateway topology, the deployment boundary, or any
  control semantics. That orthogonality is exactly why this belongs in an ADR and not beside the
  topology deviations.

---

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Terraform, matching the specification's examples** | Introduces a state backend to secure and a second review workflow; loses cdk-nag's NIST and Solutions Aspects at synth; loses type-level sharing of invariants such as the model allow-list. |
| **AWS Account Factory for Terraform for account vending, CDK for everything else** | Two IaC toolchains, two state models, one control surface. Explicitly rejected. |
| **Raw CloudFormation templates** | No type checking, no composition, and the shared-invariant duplication this decision exists to avoid. |
| **CDK for TypeScript only, no Python** | The evaluation gate, teardown, and integration suites are Python for good reasons. Forcing one language would rewrite working, tested code for uniformity alone. |
