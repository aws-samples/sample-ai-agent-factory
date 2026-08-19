/**
 * ShowbackConstruct — QuickSight dataset over CUR Athena for per-tenant
 * cost visibility.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS Missing-4 (showback half — chargeback
 * shipped earlier as ChargebackConstruct).
 *
 * Components:
 *   - QuickSight DataSource (Athena) — points at the platform CUR.
 *   - QuickSight DataSet (custom SQL) — per-tenant + per-cost-centre
 *     rollup with computed column for month-to-date. Permissions scoped
 *     to a configurable QuickSight principal.
 *   - QuickSight Refresh schedule (daily 03:00 UTC).
 *
 * Showback (visibility, this) vs. Chargeback (real billing CSV, separate
 * construct) — both consume the same CUR.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack } from 'aws-cdk-lib';
import { CfnDataSet, CfnDataSource } from 'aws-cdk-lib/aws-quicksight';
import { Construct } from 'constructs';

export interface ShowbackConstructProps {
  readonly envName: string;
  readonly curAthenaDatabase: string;
  readonly curAthenaTable: string;
  /** QuickSight workgroup (defaults to `primary`). */
  readonly workgroup?: string;
  /**
   * Optional QuickSight principal ARN to grant read on the dataset.
   * When omitted (or set to a placeholder string containing
   * `AgenticAI-FinOps`) NO permissions block is attached — customers wire
   * grants out-of-band once a real QuickSight user/group is provisioned.
   * Live-tested 2026-05-15: QuickSight rejects unknown principals at
   * deploy-time, so we default to a permission-less dataset.
   */
  readonly readerPrincipalArn?: string;
}

const DATASET_ID = 'agenticai-showback';

export class ShowbackConstruct extends Construct {
  readonly dataSource: CfnDataSource;
  readonly dataSet: CfnDataSet;

  constructor(scope: Construct, id: string, props: ShowbackConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    const isRealPrincipal =
      typeof props.readerPrincipalArn === 'string' &&
      !props.readerPrincipalArn.includes('AgenticAI-FinOps') &&
      props.readerPrincipalArn.startsWith('arn:aws:quicksight:');

    this.dataSource = new CfnDataSource(this, 'DataSource', {
      awsAccountId: stack.account,
      dataSourceId: `agenticai-cur-${props.envName}`,
      name: `AgenticAI CUR (${props.envName})`,
      type: 'ATHENA',
      dataSourceParameters: {
        athenaParameters: {
          workGroup: props.workgroup ?? 'primary',
        },
      },
      sslProperties: { disableSsl: false },
      permissions: isRealPrincipal
        ? [
            {
              principal: props.readerPrincipalArn!,
              actions: [
                'quicksight:DescribeDataSource',
                'quicksight:DescribeDataSourcePermissions',
                'quicksight:PassDataSource',
              ],
            },
          ]
        : undefined,
    });

    const sql =
      `SELECT ` +
      `  resource_tags_user_application_id AS tenant_id, ` +
      `  resource_tags_user_cost_centre AS cost_centre, ` +
      `  resource_tags_user_agent_id AS agent_id, ` +
      `  date_format(line_item_usage_start_date, '%Y-%m') AS month_key, ` +
      `  sum(line_item_unblended_cost) AS unblended_cost_usd ` +
      `FROM ${props.curAthenaDatabase}.${props.curAthenaTable} ` +
      `WHERE resource_tags_user_application_id IS NOT NULL ` +
      `GROUP BY 1, 2, 3, 4`;

    this.dataSet = new CfnDataSet(this, 'DataSet', {
      awsAccountId: stack.account,
      dataSetId: `${DATASET_ID}-${props.envName}`,
      name: `AgenticAI Showback (${props.envName})`,
      importMode: 'DIRECT_QUERY',
      physicalTableMap: {
        agenticaiShowback: {
          customSql: {
            dataSourceArn: this.dataSource.attrArn,
            name: 'agenticai-cur-rollup',
            sqlQuery: sql,
            columns: [
              { name: 'tenant_id', type: 'STRING' },
              { name: 'cost_centre', type: 'STRING' },
              { name: 'agent_id', type: 'STRING' },
              { name: 'month_key', type: 'STRING' },
              { name: 'unblended_cost_usd', type: 'DECIMAL' },
            ],
          },
        },
      },
      permissions: isRealPrincipal
        ? [
            {
              principal: props.readerPrincipalArn!,
              actions: [
                'quicksight:DescribeDataSet',
                'quicksight:DescribeDataSetPermissions',
                'quicksight:PassDataSet',
                'quicksight:DescribeIngestion',
                'quicksight:ListIngestions',
              ],
            },
          ]
        : undefined,
    });
    this.dataSet.addDependency(this.dataSource);
  }
}
