import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as eventsources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface LearningStackProps extends cdk.StackProps {
  stage: string;
  deliveryTableName: string;
  deliveryTableStreamArn: string;
  personaTableName: string;
  personaTableArn: string;
  signalTableName: string;
  analyticsTableName: string;
}

export class LearningStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: LearningStackProps) {
    super(scope, id, props);

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
      description: 'Shared utilities layer (learning)',
    });

    // --- Learning code layer (includes learning module for imports) ---
    const learningLayer = new lambda.LayerVersion(this, 'LearningLayer', {
      code: lambda.Code.fromAsset('../src/learning', {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: [
            'bash', '-c',
            'mkdir -p /asset-output/python/learning && cp -r . /asset-output/python/learning/',
          ],
          local: {
            tryBundle(outputDir: string) {
              const { execSync } = require('child_process');
              execSync(`mkdir -p ${outputDir}/python/learning && cp -r ../src/learning/* ${outputDir}/python/learning/`);
              return true;
            },
          },
        },
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: 'Learning module layer',
    });

    // ===== Feedback Processor (DynamoDB Streams consumer) =====

    const feedbackProcessor = new lambda.Function(this, 'FeedbackProcessor', {
      functionName: `pulse-feedback-processor-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'feedback_processor.handler',
      code: lambda.Code.fromAsset('../src/learning'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        DELIVERY_TABLE_NAME: props.deliveryTableName,
        PERSONA_TABLE_NAME: props.personaTableName,
        NOISE_THRESHOLD: '3',
        WINDOW_DAYS: '30',
        STAGE: props.stage,
      },
    });

    // DynamoDB Streams event source from DeliveryRecords table
    feedbackProcessor.addEventSource(new eventsources.DynamoEventSource(
      dynamodb.Table.fromTableAttributes(this, 'DeliveryTableImport', {
        tableName: props.deliveryTableName,
        tableStreamArn: props.deliveryTableStreamArn,
      }),
      {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 25,
        maxBatchingWindow: cdk.Duration.seconds(5),
        retryAttempts: 3,
        bisectBatchOnError: true,
        reportBatchItemFailures: true,
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual('MODIFY'),
          }),
        ],
      },
    ));

    // Permissions: read/write persona table (to update suppression rules)
    feedbackProcessor.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
      resources: [
        props.personaTableArn,
        `${props.personaTableArn}/index/*`,
      ],
    }));

    // Permissions: query delivery table (for noise counting)
    feedbackProcessor.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}`,
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}/index/*`,
      ],
    }));

    // Permissions: CloudWatch metrics
    feedbackProcessor.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));

    // ===== NRS Calculator (daily scheduled) =====

    const nrsCalculator = new lambda.Function(this, 'NrsCalculator', {
      functionName: `pulse-nrs-calculator-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'nrs_calculator.handler',
      code: lambda.Code.fromAsset('../src/learning'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
      memorySize: 512,
      environment: {
        SIGNAL_TABLE_NAME: props.signalTableName,
        DELIVERY_TABLE_NAME: props.deliveryTableName,
        ANALYTICS_TABLE_NAME: props.analyticsTableName,
        ORG_ID: 'default',
        STAGE: props.stage,
      },
    });

    // Permissions: scan signal and delivery tables
    nrsCalculator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Scan', 'dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:*:*:table/${props.signalTableName}`,
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}`,
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}/index/*`,
      ],
    }));

    // Permissions: write to analytics table
    nrsCalculator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'],
      resources: [`arn:aws:dynamodb:*:*:table/${props.analyticsTableName}`],
    }));

    // Permissions: CloudWatch metrics
    nrsCalculator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));

    // Schedule: daily at 02:30 UTC
    new events.Rule(this, 'NrsScheduleRule', {
      ruleName: `pulse-nrs-daily-${props.stage}`,
      schedule: events.Schedule.cron({ minute: '30', hour: '2' }),
      targets: [new targets.LambdaFunction(nrsCalculator)],
    });

    // ===== Suppression Recalculator (nightly scheduled) =====

    const suppressionRecalculator = new lambda.Function(this, 'SuppressionRecalculator', {
      functionName: `pulse-suppression-recalc-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'suppression_model.handler',
      code: lambda.Code.fromAsset('../src/learning'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
      memorySize: 256,
      environment: {
        PERSONA_TABLE_NAME: props.personaTableName,
        DELIVERY_TABLE_NAME: props.deliveryTableName,
        STAGE: props.stage,
      },
    });

    // Permissions: read/write persona table
    suppressionRecalculator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Scan', 'dynamodb:GetItem', 'dynamodb:UpdateItem'],
      resources: [props.personaTableArn],
    }));

    // Permissions: query delivery table
    suppressionRecalculator.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}`,
        `arn:aws:dynamodb:*:*:table/${props.deliveryTableName}/index/*`,
      ],
    }));

    // Schedule: daily at 02:00 UTC
    new events.Rule(this, 'SuppressionScheduleRule', {
      ruleName: `pulse-suppression-daily-${props.stage}`,
      schedule: events.Schedule.cron({ minute: '0', hour: '2' }),
      targets: [new targets.LambdaFunction(suppressionRecalculator)],
    });

    // --- Outputs ---
    new cdk.CfnOutput(this, 'FeedbackProcessorArn', { value: feedbackProcessor.functionArn });
    new cdk.CfnOutput(this, 'NrsCalculatorArn', { value: nrsCalculator.functionArn });
    new cdk.CfnOutput(this, 'SuppressionRecalcArn', { value: suppressionRecalculator.functionArn });
  }
}
