import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';

/**
 * OrgSetupStack - Optional stack deployed in the AWS Organizations management account.
 *
 * Creates cross-account EventBridge rules and IAM roles that allow member accounts
 * to forward operational events (CloudWatch, SecurityHub, Health, GuardDuty) to the
 * central Pulse account's default event bus.
 *
 * NOT deployed by `cdk deploy --all`. Must be explicitly deployed:
 *   cdk deploy Pulse-OrgSetup-dev --context stage=dev --context pulseAccountId=123456789012
 */
export interface OrgSetupStackProps extends cdk.StackProps {
  stage: string;
  pulseAccountId: string;
  organizationId?: string;
}

export class OrgSetupStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OrgSetupStackProps) {
    super(scope, id, props);

    const pulseAccountId = props.pulseAccountId;
    const orgId = props.organizationId || '';

    // --- Cross-account EventBridge rule: forward operational events to Pulse account ---
    const forwardingRule = new events.Rule(this, 'ForwardToPulseRule', {
      ruleName: `pulse-org-forward-${props.stage}`,
      description: 'Forwards operational events from this account to the central Pulse account',
      eventPattern: {
        source: [
          'aws.cloudwatch',
          'aws.securityhub',
          'aws.health',
          'aws.guardduty',
          'aws.config',
        ],
      },
    });

    // Target: Pulse account's default event bus
    forwardingRule.addTarget(new targets.EventBus(
      events.EventBus.fromEventBusArn(
        this, 'PulseEventBus',
        `arn:aws:events:${cdk.Aws.REGION}:${pulseAccountId}:event-bus/default`,
      ),
    ));

    // --- IAM Role for member account forwarding ---
    // This role can be assumed by EventBridge in member accounts to put events
    // on the Pulse account's event bus
    const forwarderRole = new iam.Role(this, 'OrgForwarderRole', {
      roleName: `pulse-org-forwarder-${props.stage}`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('events.amazonaws.com'),
        // Allow any account in the organization to assume this role
        ...(orgId ? [new iam.OrganizationPrincipal(orgId)] : []),
      ),
      description: 'Allows member accounts to forward events to Pulse central account',
    });

    forwarderRole.addToPolicy(new iam.PolicyStatement({
      actions: ['events:PutEvents'],
      resources: [`arn:aws:events:*:${pulseAccountId}:event-bus/default`],
    }));

    // --- Outputs ---
    new cdk.CfnOutput(this, 'ForwarderRoleArn', {
      value: forwarderRole.roleArn,
      description: 'Role ARN for member accounts to assume for event forwarding',
    });

    new cdk.CfnOutput(this, 'PulseTargetBus', {
      value: `arn:aws:events:${cdk.Aws.REGION}:${pulseAccountId}:event-bus/default`,
      description: 'Target event bus ARN in the Pulse account',
    });

    // --- StackSet-ready CloudFormation template instructions ---
    // The member account deployment template is documented in docs/deployment.md
    // It creates an EventBridge rule + IAM role in each member account
    new cdk.CfnOutput(this, 'MemberAccountSetup', {
      value: 'Deploy the forwarding rule in member accounts via StackSets or manual CDK deploy',
      description: 'See docs/deployment.md for member account setup instructions',
    });
  }
}
