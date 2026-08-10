import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';

export interface IngestionStackProps extends cdk.StackProps {
  stage: string;
}

export class IngestionStack extends cdk.Stack {
  public readonly signalStream: kinesis.Stream;
  public readonly signalTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, props);

    // Signal buffer stream
    this.signalStream = new kinesis.Stream(this, 'SignalStream', {
      streamName: `pulse-signals-${props.stage}`,
      shardCount: 2,
      retentionPeriod: cdk.Duration.hours(24),
    });

    // Signal events table
    this.signalTable = new dynamodb.Table(this, 'SignalTable', {
      tableName: `pulse-events-${props.stage}`,
      partitionKey: { name: 'signalId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'ingestedAt', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI for correlation lookups
    this.signalTable.addGlobalSecondaryIndex({
      indexName: 'by-correlation-group',
      partitionKey: { name: 'correlationGroupId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'ingestedAt', type: dynamodb.AttributeType.STRING },
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
      description: 'Shared utilities layer (ingestion)',
    });

    // Publish API Lambda
    const publishHandler = new lambda.Function(this, 'PublishHandler', {
      functionName: `pulse-publish-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'publish_handler.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      environment: {
        SIGNAL_STREAM_NAME: this.signalStream.streamName,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        STAGE: props.stage,
      },
    });

    this.signalStream.grantWrite(publishHandler);
    this.signalTable.grantWriteData(publishHandler);

    // API Gateway
    const api = new apigateway.RestApi(this, 'PublishApi', {
      restApiName: `pulse-api-${props.stage}`,
      description: 'AWS Pulse Publish API',
      deployOptions: { stageName: props.stage },
    });

    const v1 = api.root.addResource('v1');
    const signals = v1.addResource('signals');
    signals.addMethod('POST', new apigateway.LambdaIntegration(publishHandler), {
      apiKeyRequired: true,
    });

    // ===== Webhook Adapters (Phase 3) =====

    // --- PagerDuty Adapter Lambda ---
    const pagerdutyAdapter = new lambda.Function(this, 'PagerDutyAdapter', {
      functionName: `pulse-webhook-pagerduty-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'webhook_adapters.pagerduty.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        SIGNAL_STREAM_NAME: this.signalStream.streamName,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        PAGERDUTY_WEBHOOK_SECRET: '', // Set via parameter store in production
        STAGE: props.stage,
      },
    });

    this.signalStream.grantWrite(pagerdutyAdapter);
    this.signalTable.grantWriteData(pagerdutyAdapter);

    // --- Datadog Adapter Lambda ---
    const datadogAdapter = new lambda.Function(this, 'DatadogAdapter', {
      functionName: `pulse-webhook-datadog-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'webhook_adapters.datadog.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        SIGNAL_STREAM_NAME: this.signalStream.streamName,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        DATADOG_WEBHOOK_API_KEY: '', // Set via parameter store in production
        STAGE: props.stage,
      },
    });

    this.signalStream.grantWrite(datadogAdapter);
    this.signalTable.grantWriteData(datadogAdapter);

    // --- ServiceNow Adapter Lambda ---
    const servicenowAdapter = new lambda.Function(this, 'ServiceNowAdapter', {
      functionName: `pulse-webhook-servicenow-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'webhook_adapters.servicenow.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        SIGNAL_STREAM_NAME: this.signalStream.streamName,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        SERVICENOW_WEBHOOK_USER: '', // Set via parameter store in production
        SERVICENOW_WEBHOOK_PASS: '', // Set via parameter store in production
        STAGE: props.stage,
      },
    });

    this.signalStream.grantWrite(servicenowAdapter);
    this.signalTable.grantWriteData(servicenowAdapter);

    // --- Webhook API endpoint: POST /v1/webhooks/{provider} ---
    const webhooks = v1.addResource('webhooks');

    const pagerdutyResource = webhooks.addResource('pagerduty');
    pagerdutyResource.addMethod('POST', new apigateway.LambdaIntegration(pagerdutyAdapter));

    const datadogResource = webhooks.addResource('datadog');
    datadogResource.addMethod('POST', new apigateway.LambdaIntegration(datadogAdapter));

    const servicenowResource = webhooks.addResource('servicenow');
    servicenowResource.addMethod('POST', new apigateway.LambdaIntegration(servicenowAdapter));

    // ===== EventBridge rules for AWS service signals =====

    const cloudwatchRule = new events.Rule(this, 'CloudWatchAlarmRule', {
      eventPattern: {
        source: ['aws.cloudwatch'],
        detailType: ['CloudWatch Alarm State Change'],
      },
    });
    cloudwatchRule.addTarget(new targets.KinesisStream(this.signalStream));

    const securityHubRule = new events.Rule(this, 'SecurityHubRule', {
      eventPattern: {
        source: ['aws.securityhub'],
        detailType: ['Security Hub Findings - Imported'],
      },
    });
    securityHubRule.addTarget(new targets.KinesisStream(this.signalStream));

    const healthRule = new events.Rule(this, 'HealthRule', {
      eventPattern: {
        source: ['aws.health'],
      },
    });
    healthRule.addTarget(new targets.KinesisStream(this.signalStream));

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', { value: api.url });
    new cdk.CfnOutput(this, 'StreamArn', { value: this.signalStream.streamArn });
    new cdk.CfnOutput(this, 'WebhookUrl', { value: `${api.url}v1/webhooks/` });
  }
}
