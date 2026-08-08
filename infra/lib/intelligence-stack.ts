import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as eventsources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface IntelligenceStackProps extends cdk.StackProps {
  stage: string;
  signalStream: kinesis.IStream;
  signalTable: dynamodb.ITable;
  personaWorkflowArn: string;
}

export class IntelligenceStack extends cdk.Stack {
  public readonly correlationTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: IntelligenceStackProps) {
    super(scope, id, props);

    // --- Correlation Groups DynamoDB Table ---
    this.correlationTable = new dynamodb.Table(this, 'CorrelationTable', {
      tableName: `pulse-correlations-${props.stage}`,
      partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: lookup by resource ARN for correlation matching
    this.correlationTable.addGlobalSecondaryIndex({
      indexName: 'by-status',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
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
      description: 'Shared utilities layer (intelligence)',
    });

    // --- Correlator Lambda (Kinesis consumer) ---
    const correlator = new lambda.Function(this, 'Correlator', {
      functionName: `pulse-correlator-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'correlator.handler',
      code: lambda.Code.fromAsset('../src/intelligence'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        CORRELATION_TABLE_NAME: this.correlationTable.tableName,
        SIGNAL_TABLE_NAME: props.signalTable.tableName,
        PERSONA_WORKFLOW_ARN: props.personaWorkflowArn,
        STAGE: props.stage,
      },
    });

    // Grant Kinesis read access
    props.signalStream.grantRead(correlator);

    // Grant DynamoDB access
    this.correlationTable.grantReadWriteData(correlator);
    props.signalTable.grantReadData(correlator);

    // Grant Step Functions start execution
    correlator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:StartExecution'],
      resources: [props.personaWorkflowArn],
    }));

    // --- Kinesis Event Source: consume from signal stream ---
    correlator.addEventSource(new eventsources.KinesisEventSource(props.signalStream, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      batchSize: 25,
      maxBatchingWindow: cdk.Duration.seconds(5),
      retryAttempts: 3,
      bisectBatchOnError: true,
      reportBatchItemFailures: true,
    }));

    // --- Outputs ---
    new cdk.CfnOutput(this, 'CorrelationTableName', { value: this.correlationTable.tableName });
    new cdk.CfnOutput(this, 'CorrelatorFunctionArn', { value: correlator.functionArn });
  }
}
