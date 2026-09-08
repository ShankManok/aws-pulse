import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
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
      stream: dynamodb.StreamViewType.NEW_IMAGE,
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


    this.signalTable.grantReadWriteData(publishHandler);

    publishHandler.addEnvironment('API_ACCOUNT_IDS', this.account);
    publishHandler.addEnvironment('SOURCE_ACCOUNT_IDS', this.account);
    const outbox = new lambda.Function(this, 'SignalOutbox', {
      functionName: `pulse-outbox-${props.stage}`, runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'outbox.handler', code: lambda.Code.fromAsset('../src/ingestion'),
      layers: [sharedLayer], timeout: cdk.Duration.seconds(30),
      environment: { SIGNAL_STREAM_NAME: this.signalStream.streamName },
    });
    const outboxFailures = new sqs.Queue(this, 'OutboxFailures', {
      retentionPeriod: cdk.Duration.days(14), encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    this.signalStream.grantWrite(outbox);
    outbox.addEventSource(new sources.DynamoEventSource(this.signalTable, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      batchSize: 100, reportBatchItemFailures: true, bisectBatchOnError: true,
      retryAttempts: 10, onFailure: new sources.SqsDlq(outboxFailures),
    }));

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

    const management = new lambda.Function(this, 'ManagementApi', {
      functionName: `pulse-management-${props.stage}`, runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'management.handler', code: lambda.Code.fromAsset('../src/api'),
      layers: [sharedLayer], timeout: cdk.Duration.seconds(30),
      environment: {
        API_ACCOUNT_IDS: this.account,
        SIGNAL_TABLE_NAME: this.signalTable.tableName,
        PERSONA_TABLE_NAME: `pulse-personas-${props.stage}`,
        DELIVERY_TABLE_NAME: `pulse-delivery-${props.stage}`,
        ANALYTICS_TABLE_NAME: `pulse-analytics-${props.stage}`,
        ACTION_FUNCTION_NAME: `pulse-action-callback-${props.stage}`,
        SUBSCRIPTION_FUNCTION_NAME: `pulse-subscription-agent-${props.stage}`,
      },
    });
    this.signalTable.grantReadData(management);
    for (const name of ['personas', 'delivery', 'analytics']) {
      const arn = this.formatArn({ service: 'dynamodb', resource: 'table', resourceName: `pulse-${name}-${props.stage}` });
      management.addToRolePolicy(new iam.PolicyStatement({
        actions: name === 'personas' ? ['dynamodb:GetItem', 'dynamodb:PutItem'] : ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:Scan'],
        resources: [arn, `${arn}/index/*`],
      }));
    }
    management.addToRolePolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'],
      resources: ['action-callback', 'subscription-agent'].map(name => this.formatArn({ service: 'lambda', resource: 'function', resourceName: `pulse-${name}-${props.stage}`, arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME })),
    }));
    const managementIntegration = new apigateway.LambdaIntegration(management);
    const protectedMethod = { authorizationType: apigateway.AuthorizationType.IAM, apiKeyRequired: true };
    const personas = v1.addResource('personas');
    personas.addMethod('POST', managementIntegration, protectedMethod);
    const personaId = personas.addResource('{personaId}');
    personaId.addMethod('PUT', managementIntegration, protectedMethod);
    personaId.addResource('subscribe').addMethod('POST', managementIntegration, protectedMethod);
    signals.addResource('{signalId}').addMethod('GET', managementIntegration, protectedMethod);
    v1.addResource('deliveries').addMethod('GET', managementIntegration, protectedMethod);
    v1.addResource('feedback').addMethod('POST', managementIntegration, protectedMethod);
    v1.addResource('analytics').addResource('nrs').addMethod('GET', managementIntegration, protectedMethod);

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

    this.signalTable.grantReadWriteData(pagerdutyAdapter);

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

    this.signalTable.grantReadWriteData(datadogAdapter);

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

    this.signalTable.grantReadWriteData(servicenowAdapter);

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

    this.signalTable.grantReadWriteData(orgForwarder);

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

    const webhookSecret = new secretsmanager.Secret(this, 'WebhookCredentials', {
      secretStringValue: cdk.SecretValue.unsafePlainText('{}'),
      description: 'Set provider credentials before enabling webhook senders',
    });
    for (const adapter of [pagerdutyAdapter, datadogAdapter, servicenowAdapter]) {
      adapter.addEnvironment('WEBHOOK_SECRET_ARN', webhookSecret.secretArn);
      webhookSecret.grantRead(adapter);
    }
    new cdk.CfnOutput(this, 'WebhookSecretArn', { value: webhookSecret.secretArn });

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
