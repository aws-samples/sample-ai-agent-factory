/**
 * @agenticai/eu-ai-act-compliance
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-2.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  BLUEPRINT_RISK_CLASS,
  RISK_CLASS_ARTICLES,
  riskClassForBlueprint,
  validateRiskClass,
  type RiskClass,
} from './risk-classification';
export {
  technicalDocumentation,
  riskAssessment,
  humanOversightProtocol,
  type ConformityInputs,
} from './conformity-templates';
export {
  ConformityAssessmentConstruct,
  type ConformityAssessmentConstructProps,
} from './conformity-assessment-construct';
