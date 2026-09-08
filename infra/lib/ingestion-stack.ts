import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface IngestionStackProps extends cdk.StackProps {
  stage: string;
  organizationId?: string;
}

export class IngestionStack extends cdk.Stack {
  public readonly signalStream: kinesis.Stream;
  public readonly signalTable: dynamodb.Table;
  public readonly apiUrl: string;

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

    // --- API Gateway with rate limiting ---
    const api = new apigateway.RestApi(this, 'PublishApi', {
      restApiName: `pulse-api-${props.stage}`,
      description: 'AWS Pulse Publish API',
      deployOptions: { stageName: props.stage },
    });

    this.apiUrl = api.url;

    // API Key + Usage Plan for rate limiting
    const apiKey = new apigateway.ApiKey(this, 'PulseApiKey', {
      apiKeyName: `pulse-key-${props.stage}`,
      description: 'API Key for Pulse Publish API',
    });

    const usagePlan = new apigateway.UsagePlan(this, 'PulseUsagePlan', {
      name: `pulse-usage-plan-${props.stage}`,
      description: 'Rate limiting: 1000 req/sec',
      throttle: {
        rateLimit: 1000,
        burstLimit: 2000,
      },
      quota: {
        limit: 10000000,
        period: apigateway.Period.MONTH,
      },
    });

    usagePlan.addApiKey(apiKey);
    usagePlan.addApiStage({ stage: api.deploymentStage });

    const v1 = api.root.addResource('v1');
    const signals = v1.addResource('signals');
    signals.addMethod('POST', new apigateway.LambdaIntegration(publishHandler), {
      apiKeyRequired: true,
      authorizationType: apigateway.AuthorizationType.IAM,
    });

    // ===== Webhook Adapters =====

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
        PAGERDUTY_WEBHOOK_SECRET: '',
        STAGE: props.stage,
      },
    });
    this.signalStream.grantWrite(pagerdutyAdapter);
    this.signalTable.grantWriteData(pagerdutyAdapter);

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
        DATADOG_WEBHOOK_API_KEY: '',
        STAGE: props.stage,
      },
    });
    this.signalStream.grantWrite(datadogAdapter);
    this.signalTable.grantWriteData(datadogAdapter);

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
        SERVICENOW_WEBHOOK_USER: '',
        SERVICENOW_WEBHOOK_PASS: '',
        STAGE: props.stage,
      },
    });
    this.signalStream.grantWrite(servicenowAdapter);
    this.signalTable.grantWriteData(servicenowAdapter);

    // --- Org Forwarder Lambda (processes cross-account events) ---
    const orgForwarder = new lambda.Function(this, 'OrgForwarder', {
      functionName: `pulse-org-forwarder-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'org_forwarder.handler',
      code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        AWS_ACCOUNT_ID: this.account,
        SIGNAL_STREAM_NAME: this.signalStream.streamName,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        STAGE: props.stage,
      },
    });
    this.signalStream.grantWrite(orgForwarder);
    this.signalTable.grantWriteData(orgForwarder);

    // One route for local and forwarded events; prevents duplicate raw/normalized records.
    new events.Rule(this, 'NativeEventRule', {
      eventPattern: {
        source: ['aws.cloudwatch', 'aws.securityhub', 'aws.health', 'aws.guardduty', 'aws.config'],
      },
      targets: [new targets.LambdaFunction(orgForwarder)],
    });

    // Cross-account ingestion is opt-in and limited to an explicit organization.
    if (props.organizationId) {
      if (!/^o-[a-z0-9]{10,32}$/.test(props.organizationId)) {
        throw new Error('organizationId must be an AWS Organizations ID');
      }
      new events.CfnEventBusPolicy(this, 'CrossAccountBusPolicy', {
        statementId: `pulse-cross-account-allow-${props.stage}`,
        action: 'events:PutEvents',
        principal: '*',
        condition: { type: 'StringEquals', key: 'aws:PrincipalOrgID', value: props.organizationId },
      });
    }

    // Webhook API endpoints
    const webhooks = v1.addResource('webhooks');
    webhooks.addResource('pagerduty').addMethod('POST', new apigateway.LambdaIntegration(pagerdutyAdapter));
    webhooks.addResource('datadog').addMethod('POST', new apigateway.LambdaIntegration(datadogAdapter));
    webhooks.addResource('servicenow').addMethod('POST', new apigateway.LambdaIntegration(servicenowAdapter));

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', { value: api.url });
    new cdk.CfnOutput(this, 'ApiKeyId', { value: apiKey.keyId });
    new cdk.CfnOutput(this, 'StreamArn', { value: this.signalStream.streamArn });
    new cdk.CfnOutput(this, 'WebhookUrl', { value: `${api.url}v1/webhooks/` });
  }
}
