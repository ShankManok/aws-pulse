import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface DeliveryStackProps extends cdk.StackProps {
  stage: string;
  sesDomain?: string;
}

export class DeliveryStack extends cdk.Stack {
  public readonly deliveryTable: dynamodb.Table;
  public readonly sesDomain: string;

  constructor(scope: Construct, id: string, props: DeliveryStackProps) {
    super(scope, id, props);

    this.sesDomain = props.sesDomain || `pulse-${props.stage}.example.com`;

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
            'mkdir -p /asset-output/python/shared && cp -r . /asset-output/python/shared/ && pip install pydantic ulid-py structlog boto3 -t /asset-output/python --quiet',
          ],
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

    // --- Outputs ---
    new cdk.CfnOutput(this, 'DeliveryTableName', { value: this.deliveryTable.tableName });
    new cdk.CfnOutput(this, 'CallbackApiUrl', { value: callbackApi.url });
  }
}
