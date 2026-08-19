/**
 * AgenticAppBudgetConstruct — per-application AWS Budget.
 *
 * Filtered by the `application-id` cost-allocation tag produced by AgenticApp
 * (R-TEN-013, R-TEN-029). Alerts at 80% of monthly budget via SNS.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnBudget } from 'aws-cdk-lib/aws-budgets';
import { Construct } from 'constructs';

export interface AgenticAppBudgetConstructProps {
  readonly tenantId: string;
  readonly envName: string;
  /** Monthly budget in USD. */
  readonly monthlyBudgetUsd: number;
  /**
   * Email address to notify on threshold breach. For production, swap for
   * an SNS topic consumer or PagerDuty integration.
   */
  readonly notificationEmail: string;
}

export class AgenticAppBudgetConstruct extends Construct {
  readonly budget: CfnBudget;

  constructor(scope: Construct, id: string, props: AgenticAppBudgetConstructProps) {
    super(scope, id);

    this.budget = new CfnBudget(this, 'Budget', {
      budget: {
        budgetName: `agenticai-${props.envName}-${props.tenantId}`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: {
          amount: props.monthlyBudgetUsd,
          unit: 'USD',
        },
        costFilters: {
          TagKeyValue: [`user:application-id$${props.tenantId}`],
        },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            threshold: 80,
            thresholdType: 'PERCENTAGE',
            comparisonOperator: 'GREATER_THAN',
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: props.notificationEmail },
          ],
        },
        {
          notification: {
            notificationType: 'FORECASTED',
            threshold: 100,
            thresholdType: 'PERCENTAGE',
            comparisonOperator: 'GREATER_THAN',
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: props.notificationEmail },
          ],
        },
      ],
    });
  }
}
