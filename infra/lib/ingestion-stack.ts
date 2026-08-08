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

    // Publish API Lambda
    const publishHandler = new lambda.Function(this, 'PublishHandler', {
      functionName: `pulse-publish-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'publish_handler.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
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

    const signals = api.root.addResource('v1').addResource('signals');
    signals.addMethod('POST', new apigateway.LambdaIntegration(publishHandler), {
      apiKeyRequired: true,
    });

    // EventBridge rules for AWS service signals
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
  }
}
