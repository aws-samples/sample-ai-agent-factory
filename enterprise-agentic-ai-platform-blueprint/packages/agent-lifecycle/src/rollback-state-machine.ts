/**
 * RollbackStateMachineConstruct — Step Functions state machine that flips
 * the agent alias from a misbehaving CANARY back to its previous PROD
 * pointer when the online evaluation watchdog (Phase B) fires the
 * `Regressed` composite alarm during the canary soak window.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-3 (rollback side).
 *
 * Flow:
 *   1. EnsureSoakWindow — check the current canary record is within soak.
 *   2. ReadPreviousAlias — DDB GetItem on the by-alias GSI ('PROD').
 *   3. UpdateAliasToPrevious — DDB UpdateItem flipping the LIVE row.
 *   4. WriteAuditRecord — DDB PutItem to the version-history table with
 *      status=ROLLED_BACK.
 *   5. NotifySuccess — SNS publish to the eval-failures topic.
 *
 * The state machine is invoked by an EventBridge rule keyed on the Phase-B
 * `Regressed` alarm transitioning to ALARM during the canary soak.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration } from 'aws-cdk-lib';
import { CompositeAlarm, IAlarm } from 'aws-cdk-lib/aws-cloudwatch';
import { Rule, RuleTargetInput } from 'aws-cdk-lib/aws-events';
import { SfnStateMachine } from 'aws-cdk-lib/aws-events-targets';
import { Effect, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import {
  Choice,
  Condition,
  DefinitionBody,
  LogLevel,
  Pass,
  StateMachine,
  StateMachineType,
  Succeed,
  Fail,
  TaskInput,
} from 'aws-cdk-lib/aws-stepfunctions';
import {
  CallAwsService,
  SnsPublish,
} from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';

export interface RollbackStateMachineConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly versionTable: ITable;
  readonly failuresTopic: ITopic;
}

export class RollbackStateMachineConstruct extends Construct {
  readonly stateMachine: StateMachine;
  readonly role: Role;
  readonly logGroup: LogGroup;

  constructor(scope: Construct, id: string, props: RollbackStateMachineConstructProps) {
    super(scope, id);

    this.role = new Role(this, 'Role', {
      roleName: `AgenticAI-Rollback-${props.envName}-${props.tenantId}-${props.agentId}`,
      assumedBy: new ServicePrincipal('states.amazonaws.com'),
      description: 'Rollback state machine - flips alias on canary regression.',
    });
    props.versionTable.grantReadWriteData(this.role);
    props.failuresTopic.grantPublish(this.role);
    this.role.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['logs:CreateLogDelivery', 'logs:GetLogDelivery', 'logs:UpdateLogDelivery', 'logs:DeleteLogDelivery', 'logs:ListLogDeliveries', 'logs:PutResourcePolicy', 'logs:DescribeResourcePolicies', 'logs:DescribeLogGroups'],
        resources: ['*'],
      }),
    );

    this.logGroup = new LogGroup(this, 'Logs', {
      logGroupName: `/agenticai/agent-rollback/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.ONE_YEAR,
    });

    // H-E fix: input validation upfront. The previous version blew up with
    // States.Runtime when `$.canaryStatus` was missing because Choice tried
    // to evaluate `stringMatches` on a non-existent path.
    const ensureSoak = new Choice(this, 'EnsureSoakWindow')
      .when(
        Condition.and(
          Condition.isPresent('$.canaryStatus'),
          Condition.stringMatches('$.canaryStatus', 'PROMOTING'),
        ),
        new Pass(this, 'InsideSoak'),
      )
      .otherwise(
        new Fail(this, 'NoActiveCanary', {
          error: 'NoActiveCanary',
          cause: 'Rollback invoked outside an active canary soak window. Input requires {"canaryStatus":"PROMOTING"}.',
        }),
      );

    const readPrevious = new CallAwsService(this, 'ReadPreviousAlias', {
      service: 'dynamodb',
      action: 'query',
      iamResources: [props.versionTable.tableArn, `${props.versionTable.tableArn}/index/by-alias`],
      parameters: {
        TableName: props.versionTable.tableName,
        IndexName: 'by-alias',
        KeyConditionExpression: 'aliasName = :a AND tenantAgent = :ta',
        ExpressionAttributeValues: {
          ':a': { S: 'PREVIOUS' },
          ':ta': { S: `${props.tenantId}#${props.agentId}` },
        },
        Limit: 1,
      },
      resultPath: '$.previous',
    });

    const flipAlias = new CallAwsService(this, 'FlipAliasToPrevious', {
      service: 'dynamodb',
      action: 'updateItem',
      iamResources: [props.versionTable.tableArn],
      parameters: {
        TableName: props.versionTable.tableName,
        Key: {
          tenantId: { S: props.tenantId },
          // CRIT-B fix: DDB Key requires AttributeValue envelope `{S: ...}`,
          // not a raw string. The `'sk.$'` substitution returns a string;
          // wrap it in `{S: ...}` via the nested `'S.$'` path syntax.
          sk: { 'S.$': '$.previous.Items[0].sk.S' },
        },
        UpdateExpression: 'SET aliasName = :a, #s = :s',
        ExpressionAttributeNames: { '#s': 'status' },
        ExpressionAttributeValues: {
          ':a': { S: 'PROD' },
          ':s': { S: 'LIVE' },
        },
      },
      resultPath: '$.flip',
    });

    const writeAudit = new CallAwsService(this, 'WriteAuditRecord', {
      service: 'dynamodb',
      action: 'putItem',
      iamResources: [props.versionTable.tableArn],
      parameters: {
        TableName: props.versionTable.tableName,
        Item: {
          tenantId: { S: props.tenantId },
          sk: { 'S.$': "States.Format('{}#audit#rollback#{}', $$.Execution.Name, $$.State.EnteredTime)" },
          eventType: { S: 'ROLLBACK' },
          status: { S: 'ROLLED_BACK' },
          emittedAt: { 'S.$': '$$.State.EnteredTime' },
        },
      },
      resultPath: '$.audit',
    });

    const notify = new SnsPublish(this, 'NotifyOps', {
      topic: props.failuresTopic,
      subject: `AgenticAI rollback executed (${props.envName} ${props.tenantId}/${props.agentId})`,
      message: TaskInput.fromText(
        `Rollback completed — agent ${props.tenantId}/${props.agentId} (${props.envName}) reverted to previous PROD version.`,
      ),
    });

    const definition = ensureSoak.afterwards()
      .next(readPrevious)
      .next(flipAlias)
      .next(writeAudit)
      .next(notify)
      .next(new Succeed(this, 'Done'));

    this.stateMachine = new StateMachine(this, 'StateMachine', {
      stateMachineName: `AgenticAI-Rollback-${props.envName}-${props.tenantId}-${props.agentId}`,
      stateMachineType: StateMachineType.STANDARD,
      definitionBody: DefinitionBody.fromChainable(definition),
      role: this.role,
      timeout: Duration.minutes(15),
      logs: { destination: this.logGroup, level: LogLevel.ALL },
      tracingEnabled: true,
    });
  }

  /**
   * Z7-B: wire the OnlineEval `Regressed` composite alarm to auto-trigger
   * this rollback state machine when the alarm transitions to ALARM during
   * a canary soak window. Caller passes the composite alarm; the SF stays
   * usable from manual SSM triggers too.
   */
  wireToRegressionAlarm(alarm: IAlarm | CompositeAlarm): Rule {
    const rule = new Rule(this, 'RegressionAutoRollback', {
      description: 'OnlineEval Regressed alarm transitions to ALARM ⇒ start RollbackStateMachine.',
      eventPattern: {
        source: ['aws.cloudwatch'],
        detailType: ['CloudWatch Alarm State Change'],
        detail: {
          alarmName: [alarm.alarmName],
          state: { value: ['ALARM'] },
        },
      },
    });
    rule.addTarget(
      new SfnStateMachine(this.stateMachine, {
        input: RuleTargetInput.fromObject({ canaryStatus: 'PROMOTING', source: 'auto-rollback' }),
      }),
    );
    return rule;
  }
}
