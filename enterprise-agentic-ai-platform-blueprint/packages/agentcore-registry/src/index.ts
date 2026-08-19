/**
 * @agenticai/agentcore-registry
 *
 * Platform-account Agent + Tool Registry (spec §2.1.3 L336-338 / R-ARCH-010).
 * Stored as two DynamoDB tables (CMK, PITR, streams) so registration is
 * declarative IaC — the spec's R7 concern about imperative-notebook
 * registration is addressed here.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { AgentCoreRegistryConstruct, type AgentCoreRegistryConstructProps } from './registry-construct';
