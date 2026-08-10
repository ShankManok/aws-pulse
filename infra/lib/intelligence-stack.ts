import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as eventsources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
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
  public readonly predictorsTable: dynamodb.Table;
  public readonly alarmTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: IntelligenceStackProps) {
    super(scope, id, props);

    // --- Shared alarm SNS topic ---
    this.alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `pulse-alarms-${props.stage}`,
      displayName: 'AWS Pulse Operational Alarms',
    });

    // --- Correlation Groups DynamoDB Table ---
    this.correlationTable = new dynamodb.Table(this, 'CorrelationTable', {
      tableName: `pulse-correlations-${props.stage}`,
      partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.correlationTable.addGlobalSecondaryIndex({
      indexName: 'by-status',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
    });

    // --- Predictors Configuration Table ---
    this.predictorsTable = new dynamodb.Table(this, 'PredictorsTable', {
      tableName: `pulse-predictors-${props.stage}`,
      partitionKey: { name: 'predictorId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // --- DLQ for failed Kinesis records ---
    const correlatorDlq = new sqs.Queue(this, 'CorrelatorDLQ', {
      queueName: `pulse-correlator-dlq-${props.stage}`,
      retentionPeriod: cdk.Duration.days(14),
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

    props.signalStream.grantRead(correlator);
    this.correlationTable.grantReadWriteData(correlator);
    props.signalTable.grantReadData(correlator);

    correlator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:StartExecution'],
      resources: [props.personaWorkflowArn],
    }));

    // Kinesis Event Source with DLQ
    correlator.addEventSource(new eventsources.KinesisEventSource(props.signalStream, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      batchSize: 25,
      maxBatchingWindow: cdk.Duration.seconds(5),
      retryAttempts: 3,
      bisectBatchOnError: true,
      reportBatchItemFailures: true,
      onFailure: new eventsources.SqsDlq(correlatorDlq),
    }));

    // --- Predictor Lambda (scheduled every 6 hours) ---
    const predictor = new lambda.Function(this, 'Predictor', {
      functionName: `pulse-predictor-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'predictor.handler',
      code: lambda.Code.fromAsset('../src/intelligence'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
      memorySize: 512,
      environment: {
        PREDICTORS_TABLE_NAME: this.predictorsTable.tableName,
        SIGNAL_STREAM_NAME: props.signalStream.streamName,
        SIGNAL_TABLE_NAME: props.signalTable.tableName,
        STAGE: props.stage,
      },
    });

    this.predictorsTable.grantReadData(predictor);
    props.signalStream.grantWrite(predictor);
    props.signalTable.grantWriteData(predictor);

    // CloudWatch read permissions for metric queries
    predictor.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:GetMetricStatistics', 'cloudwatch:GetMetricData', 'cloudwatch:ListMetrics'],
      resources: ['*'],
    }));

    // Schedule: every 6 hours
    new events.Rule(this, 'PredictorSchedule', {
      ruleName: `pulse-predictor-6h-${props.stage}`,
      schedule: events.Schedule.rate(cdk.Duration.hours(6)),
      targets: [new targets.LambdaFunction(predictor)],
    });

    // ===== CloudWatch Alarms =====

    // Alarm: Correlator errors > 10 in 5 min
    const correlatorErrors = correlator.metricErrors({
      period: cdk.Duration.minutes(5),
      statistic: 'Sum',
    });
    const correlatorErrorAlarm = new cloudwatch.Alarm(this, 'CorrelatorErrorAlarm', {
      alarmName: `pulse-correlator-errors-${props.stage}`,
      metric: correlatorErrors,
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: 'Correlator Lambda errors exceeded 10 in 5 minutes',
    });
    correlatorErrorAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(this.alarmTopic));

    // Alarm: Kinesis iterator age > 60 seconds
    const iteratorAge = new cloudwatch.Metric({
      namespace: 'AWS/Kinesis',
      metricName: 'GetRecords.IteratorAgeMilliseconds',
      dimensionsMap: { StreamName: props.signalStream.streamName },
      period: cdk.Duration.minutes(1),
      statistic: 'Maximum',
    });
    const iteratorAgeAlarm = new cloudwatch.Alarm(this, 'IteratorAgeAlarm', {
      alarmName: `pulse-kinesis-iterator-age-${props.stage}`,
      metric: iteratorAge,
      threshold: 60000, // 60 seconds in milliseconds
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: 'Kinesis consumer is falling behind (iterator age > 60s)',
    });
    iteratorAgeAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(this.alarmTopic));

    // Alarm: Step Functions execution failures > 5 in 5 min
    const sfnFailures = new cloudwatch.Metric({
      namespace: 'AWS/States',
      metricName: 'ExecutionsFailed',
      dimensionsMap: { StateMachineArn: props.personaWorkflowArn },
      period: cdk.Duration.minutes(5),
      statistic: 'Sum',
    });
    const sfnFailureAlarm = new cloudwatch.Alarm(this, 'SfnFailureAlarm', {
      alarmName: `pulse-workflow-failures-${props.stage}`,
      metric: sfnFailures,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: 'Persona workflow execution failures exceeded 5 in 5 minutes',
    });
    sfnFailureAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(this.alarmTopic));

    // --- Outputs ---
    new cdk.CfnOutput(this, 'CorrelationTableName', { value: this.correlationTable.tableName });
    new cdk.CfnOutput(this, 'PredictorsTableName', { value: this.predictorsTable.tableName });
    new cdk.CfnOutput(this, 'CorrelatorFunctionArn', { value: correlator.functionArn });
    new cdk.CfnOutput(this, 'PredictorFunctionArn', { value: predictor.functionArn });
    new cdk.CfnOutput(this, 'AlarmTopicArn', { value: this.alarmTopic.topicArn });
    new cdk.CfnOutput(this, 'DlqUrl', { value: correlatorDlq.queueUrl });
  }
}
