# @agenticai/platform-inference-gateway

D-03 centralised-platform **PrivateLink primitive**. Stands up the *network*
half of the workload-to-platform inference path described in
README §3.3, residual-risks row "Cross-account PrivateLink →
LiteLLM attack surface".

## What it emits

1. An internal `NetworkLoadBalancer` in the platform-account VPC (multi-AZ,
   private-isolated subnets, deletion-protection on, S3 access logging on).
2. A listener on TCP:443 (or TLS:443 if you pass an ACM `certificate`).
3. A `NetworkTargetGroup` of type `ALB`. When `targetAlb` is passed it
   registers that ALB via `AlbArnTarget`. When omitted the target group is
   empty — so the D-03 *current* shape (`AssumeRole → Bedrock direct`) still
   synths cleanly; wire the LiteLLM ALB in later by re-running CDK with
   `targetAlb` set.
4. A `VpcEndpointService` wrapping the NLB with
   `acceptanceRequired: false` and `AllowedPrincipals` locked to the
   `arn:aws:iam::<acct>:root` of each workload account id you supply.
5. `CfnOutput` of the endpoint service name — pass it to each workload
   account via context / SSM / pipeline parameter.

## When to pass `targetAlb`

- **Don't pass** while D-03 runs its current `AssumeRole → Bedrock`
  shape. The construct still provisions the NLB + endpoint service so
  cross-account consumers can wire VPCE scaffolding today.
- **Pass** when a platform-account LiteLLM deployment exists and you want
  workload agents to hit it cross-account. The ALB must already expose a
  listener on `targetAlbPort` (default 443); the NLB forwards at L4.

## Consumer wiring

In a workload account:

```ts
const vpce = new InterfaceVpcEndpoint(this, 'PlatformInferenceVpce', {
  vpc,
  service: new InterfaceVpcEndpointService(platformInferenceServiceName, 443),
  subnets: { subnetGroupName: 'vpce' },
  securityGroups: [vpceSg],
  privateDnsEnabled: false,
});
```

See `apps/workload-account/lib/d03-workload-agent-stack.ts` for the
production wiring.
