/**
 * SCP-08 — Deny ECR Public Repositories.
 *
 * Spec §2.2.9 L885-906. Container images must stay in private ECR.
 * Public ECR repositories are denied across workload accounts.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp08DenyEcrPublic(): ScpDefinition {
  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyEcrPublic',
        Effect: 'Deny',
        Action: ['ecr-public:*'],
        Resource: '*',
      },
    ],
  };

  return toScpDefinition(
    'scp-08',
    'AgenticAI-SCP-08-DenyEcrPublic',
    'Deny all ECR Public actions in workload accounts (spec §2.2.9).',
    body,
  );
}
