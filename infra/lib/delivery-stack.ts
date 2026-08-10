import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import { Construct } from 'constructs';

export interface DeliveryStackProps extends cdk.StackProps {
  stage: string;
  sesDomain?: string;
}

export class DeliveryStack extends cdk.Stack {
  public readonly deliveryTable: dynamodb.Table;
  public readonly sesDomain: string;
  public readonly escalationHandlerFn: lambda.Function;
  public readonly escalationHandlerArn: string;
  public readonly escalationSchedulerArn: string;
  public readonly schedulerRoleArn: string;
  public readonly schedulerGroupName: string;

  constructor(scope: Construct, id: string, props: DeliveryStackProps) {
    super(scope, id, props);

    this.sesDomain = props.sesDomain || `pulse-${props.stage}.example.com`;
    this.schedulerGroupName = `pulse-escalations-${props.stage}`;

    // --- DeliveryRecords DynamoDB Table ---
    this.deliveryTable = new dynamodb.Table(this, 'DeliveryTable', {
      tableName: `pulse-delivery-${props.stage}`,
      partitionKey: { name: 'deliveryId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: lookup by signalId to find all deliveries for a signal
    this.deliveryTable.addGlobalSecondaryIndex({
      indexName: 'by-signal',
      partitionKey: { name: 'signalId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'deliveredAt', type: dynamodb.AttributeType.STRING },
    });

    // GSI: lookup by personaId for persona delivery history
    this.deliveryTable.addGlobalSecondaryIndex({
      indexName: 'by-persona',
      partitionKey: { name: 'personaId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'deliveredAt', type: dynamodb.AttributeType.STRING },
    });

    // --- Shared Lambda layer ---
    const sharedLayer = new lambda.LayerVersion(this, 'SharedLayer', {
      code: lambda.Code.fromAsset('../src/shared', {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: [
            'bash', '-c',
            'mkdir -p /asset-output/python/shared && cp -r . /asset-output/python/shared/ && pip3 install pydantic ulid-py structlog boto3 -t /asset-output/python --quiet',
          ],
          local: {
            tryBundle(outputDir: string) {
              const { execSync } = require('child_process');
              execSync(`mkdir -p ${outputDir}/python/shared && cp -r ../src/shared/* ${outputDir}/python/shared/ && pip3 install pydantic ulid-py structlog boto3 -t ${outputDir}/python --quiet`);
              return true;
            },
          },
        },
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: 'Shared utilities layer (delivery)',
    });

    // --- Action Callback Lambda ---
    const actionCallback = new lambda.Function(this, 'ActionCallback', {
      functionName: `pulse-action-callback-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'action_callback.handler',
      code: lambda.Code.fromAsset('../src/delivery'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      environment: {
        DELIVERY_TABLE_NAME: this.deliveryTable.tableName,
        STAGE: props.stage,
      },
    });
    this.deliveryTable.grantReadWriteData(actionCallback);

    // --- Action Callback API Gateway ---
    const callbackApi = new apigateway.RestApi(this, 'CallbackApi', {
      restApiName: `pulse-callback-${props.stage}`,
      description: 'AWS Pulse action callback endpoints for notification buttons',
      deployOptions: { stageName: props.stage },
    });

    const actions = callbackApi.root.addResource('v1').addResource('actions');
    const actionProxy = actions.addResource('{deliveryId}').addResource('{action}');
    actionProxy.addMethod('POST', new apigateway.LambdaIntegration(actionCallback));
    // Also support GET for email link clicks
    actionProxy.addMethod('GET', new apigateway.LambdaIntegration(actionCallback));

    // --- EventBridge Scheduler Group for escalations ---
    const schedulerGroup = new scheduler.CfnScheduleGroup(this, 'EscalationSchedulerGroup', {
      name: this.schedulerGroupName,
    });

    // --- Escalation Handler Lambda ---
    const escalationHandler = new lambda.Function(this, 'EscalationHandler', {
      functionName: `pulse-escalation-handler-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'escalation_handler.handler',
      code: lambda.Code.fromAsset('../src/delivery'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        DELIVERY_TABLE_NAME: this.deliveryTable.tableName,
        PERSONA_WORKFLOW_ARN: '', // Wired post-construction from app.ts
        SCHEDULER_GROUP_NAME: this.schedulerGroupName,
        STAGE: props.stage,
      },
    });

    this.deliveryTable.grantReadWriteData(escalationHandler);
    this.escalationHandlerFn = escalationHandler;
    this.escalationHandlerArn = escalationHandler.functionArn;

    // Permission to delete EventBridge Scheduler schedules (cleanup)
    escalationHandler.addToRolePolicy(new iam.PolicyStatement({
      actions: ['scheduler:DeleteSchedule'],
      resources: [`arn:aws:scheduler:*:*:schedule/${this.schedulerGroupName}/*`],
    }));

    // --- IAM Role for EventBridge Scheduler to invoke escalation handler ---
    const schedulerRole = new iam.Role(this, 'EscalationSchedulerRole', {
      roleName: `pulse-scheduler-role-${props.stage}`,
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      description: 'Allows EventBridge Scheduler to invoke escalation handler Lambda',
    });

    schedulerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [escalationHandler.functionArn],
    }));

    this.schedulerRoleArn = schedulerRole.roleArn;

    // --- Schedule Escalation Lambda ---
    const scheduleEscalation = new lambda.Function(this, 'ScheduleEscalation', {
      functionName: `pulse-schedule-escalation-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'schedule_escalation.handler',
      code: lambda.Code.fromAsset('../src/delivery'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        DELIVERY_TABLE_NAME: this.deliveryTable.tableName,
        ESCALATION_FUNCTION_ARN: escalationHandler.functionArn,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
        SCHEDULER_GROUP_NAME: this.schedulerGroupName,
        STAGE: props.stage,
      },
    });

    this.escalationSchedulerArn = scheduleEscalation.functionArn;

    // Permission to create EventBridge Scheduler schedules
    scheduleEscalation.addToRolePolicy(new iam.PolicyStatement({
      actions: ['scheduler:CreateSchedule', 'scheduler:GetSchedule'],
      resources: [`arn:aws:scheduler:*:*:schedule/${this.schedulerGroupName}/*`],
    }));

    // Permission to pass the scheduler role
    scheduleEscalation.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [schedulerRole.roleArn],
    }));

    // --- Outputs ---
    new cdk.CfnOutput(this, 'DeliveryTableName', { value: this.deliveryTable.tableName });
    new cdk.CfnOutput(this, 'CallbackApiUrl', { value: callbackApi.url });
    new cdk.CfnOutput(this, 'EscalationHandlerArn', { value: escalationHandler.functionArn });
    new cdk.CfnOutput(this, 'ScheduleEscalationArn', { value: scheduleEscalation.functionArn });
    new cdk.CfnOutput(this, 'SchedulerRoleArn', { value: schedulerRole.roleArn });
  }
}
