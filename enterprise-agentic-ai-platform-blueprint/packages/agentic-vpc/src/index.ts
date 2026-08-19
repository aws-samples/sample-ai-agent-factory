/**
 * @agenticai/agentic-vpc
 *
 * AgenticVpcConstruct — the per-workload-account VPC per spec §2.3.2.
 *   - 3 AZs, private subnets only (no IGW, no NAT)
 *   - 9 required VPC endpoints per §2.3.4
 *   - VPC endpoint policies scoping principals + Bedrock model allow-list (§2.3.5)
 *   - Security-group pair (workload-ENI ⇄ VPCE-ENI) per §2.3.6
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { AgenticVpcConstruct, type AgenticVpcConstructProps } from './agentic-vpc-construct';
