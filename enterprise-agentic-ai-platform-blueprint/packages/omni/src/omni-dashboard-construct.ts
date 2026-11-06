/**
 * OmniDashboardConstruct
 *
 * Thin, validated wrapper over AWS::CloudWatch::OmniDashboard (no L1 in the
 * pinned aws-cdk-lib). The dashboard body schema is owned by the Omni web UI:
 * author a dashboard there, export its JSON, and commit it under
 * `assets/omni-dashboards/`. This construct refuses to invent a body.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnResource } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface OmniDashboardConstructProps {
  /** `^[a-zA-Z0-9_.@~()-]+$`, 1-256 characters. */
  readonly name: string;
  /** Space id (`SpaceId` attribute of the space). */
  readonly spaceId: string;
  /** Dashboard body as exported from the Omni web UI. Serialised to JSON (max 1 MiB). */
  readonly body: Record<string, unknown>;
  readonly description?: string;
  readonly tags?: Record<string, string>;
}

export class OmniDashboardConstruct extends Construct {
  readonly dashboard: CfnResource;

  constructor(scope: Construct, id: string, props: OmniDashboardConstructProps) {
    super(scope, id);

    if (!/^[a-zA-Z0-9_.@~()-]{1,256}$/.test(props.name)) {
      throw new Error(`OmniDashboardConstruct name '${props.name}' must match ^[a-zA-Z0-9_.@~()-]{1,256}$.`);
    }
    if (Object.keys(props.body).length === 0) {
      throw new Error('OmniDashboardConstruct refuses an empty dashboard body; export one from the Omni web UI.');
    }
    const body = JSON.stringify(props.body);
    if (body.length > 1_048_576) {
      throw new Error('OmniDashboardConstruct body exceeds the 1 MiB CloudFormation limit.');
    }
    if (props.description && (props.description.length < 1 || props.description.length > 1024)) {
      throw new Error('OmniDashboardConstruct description must be 1-1024 characters.');
    }

    const properties: Record<string, unknown> = {
      Name: props.name,
      SpaceId: props.spaceId,
      Body: body,
    };
    if (props.description) properties.Description = props.description;
    if (props.tags) properties.Tags = Object.entries(props.tags).map(([Key, Value]) => ({ Key, Value }));

    this.dashboard = new CfnResource(this, 'Dashboard', {
      type: 'AWS::CloudWatch::OmniDashboard',
      properties,
    });
  }
}
