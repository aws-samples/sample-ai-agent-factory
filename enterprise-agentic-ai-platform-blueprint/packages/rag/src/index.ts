/**
 * @agenticai/rag
 *
 * RAG on S3 per spec §4.2:
 *   - CMK-encrypted source + vector storage
 *   - VPCE-only access (bucket policy denies calls not from the workload VPCE)
 *   - Scoped-prefix authorisation (per-app prefix inside a shared bucket)
 *
 * S3 Vectors is the spec target; current CFN resource type is `AWS::S3::Bucket`
 * with vector-addon configuration via CDK custom resource in a Phase 5 follow-on.
 * For now we deliver the spec-conformant source bucket + prefix + scoped IAM role.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { RagKnowledgeBaseConstruct, type RagKnowledgeBaseConstructProps } from './kb-construct';
