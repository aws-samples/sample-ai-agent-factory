/**
 * @agenticai/landing-zone
 *
 * Constructs deployed into the Log Archive and Audit accounts:
 *   - LogArchiveConstruct  — Org CloudTrail destination bucket, CWL cross-account
 *                            destination, CUR bucket (spec §2.1.3 L348-351).
 *   - AuditConstruct       — CloudWatch cross-account-observability OAM sink
 *                            (spec §5 observability — derived R-OBS-002).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { LogArchiveConstruct, type LogArchiveConstructProps } from './log-archive-construct';
export { AuditConstruct, type AuditConstructProps } from './audit-construct';
