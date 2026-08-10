import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as athena from 'aws-cdk-lib/aws-athena';
import { Construct } from 'constructs';

export interface AnalyticsStackProps extends cdk.StackProps {
  stage: string;
  deliveryTableName: string;
}

export class AnalyticsStack extends cdk.Stack {
  public readonly analyticsTable: dynamodb.Table;
  public readonly auditBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: AnalyticsStackProps) {
    super(scope, id, props);

    // --- Analytics DynamoDB Table (NRS daily snapshots) ---
    this.analyticsTable = new dynamodb.Table(this, 'AnalyticsTable', {
      tableName: `pulse-analytics-${props.stage}`,
      partitionKey: { name: 'snapshotId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.analyticsTable.addGlobalSecondaryIndex({
      indexName: 'by-org-date',
      partitionKey: { name: 'orgId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date', type: dynamodb.AttributeType.STRING },
    });

    // --- Audit S3 Bucket ---
    this.auditBucket = new s3.Bucket(this, 'AuditBucket', {
      bucketName: `pulse-audit-${cdk.Aws.ACCOUNT_ID}-${props.stage}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      lifecycleRules: [
        {
          id: 'transition-to-ia',
          transitions: [
            { storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(90) },
            { storageClass: s3.StorageClass.GLACIER, transitionAfter: cdk.Duration.days(365) },
          ],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
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
      description: 'Shared utilities layer (analytics)',
    });

    // --- Audit Exporter Lambda (hourly) ---
    const auditExporter = new lambda.Function(this, 'AuditExporter', {
      functionName: `pulse-audit-exporter-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'audit_exporter.handler',
      code: lambda.Code.fromAsset('../src/learning'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
      memorySize: 512,
      environment: {
        DELIVERY_TABLE_NAME: props.deliveryTableName,
        AUDIT_BUCKET_NAME: this.auditBucket.bucketName,
        STAGE: props.stage,
      },
    });

    // Permissions: scan delivery table
    auditExporter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Scan'],
      resources: [`arn:aws:dynamodb:*:*:table/${props.deliveryTableName}`],
    }));

    // Permissions: write to audit bucket
    this.auditBucket.grantWrite(auditExporter);

    // Schedule: hourly at minute 5
    new events.Rule(this, 'AuditExportSchedule', {
      ruleName: `pulse-audit-export-hourly-${props.stage}`,
      schedule: events.Schedule.cron({ minute: '5' }),
      targets: [new targets.LambdaFunction(auditExporter)],
    });

    // --- Athena Workgroup ---
    new athena.CfnWorkGroup(this, 'AuditAthenaWorkgroup', {
      name: `pulse-audit-${props.stage}`,
      description: 'Athena workgroup for Pulse audit trail queries',
      state: 'ENABLED',
      workGroupConfiguration: {
        resultConfiguration: {
          outputLocation: `s3://${this.auditBucket.bucketName}/athena-results/`,
          encryptionConfiguration: {
            encryptionOption: 'SSE_S3',
          },
        },
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
      },
    });

    // --- Outputs ---
    new cdk.CfnOutput(this, 'AnalyticsTableName', { value: this.analyticsTable.tableName });
    new cdk.CfnOutput(this, 'AuditBucketName', { value: this.auditBucket.bucketName });
    new cdk.CfnOutput(this, 'AuditExporterArn', { value: auditExporter.functionArn });
  }
}
