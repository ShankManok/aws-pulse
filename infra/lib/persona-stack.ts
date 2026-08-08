import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export interface PersonaStackProps extends cdk.StackProps {
  stage: string;
  deliveryTableName: string;
  sesDomain: string;
}

export class PersonaStack extends cdk.Stack {
  public readonly personaTable: dynamodb.Table;
  public readonly personaWorkflow: sfn.StateMachine;

  constructor(scope: Construct, id: string, props: PersonaStackProps) {
    super(scope, id, props);

    // --- Persona DynamoDB Table ---
    this.personaTable = new dynamodb.Table(this, 'PersonaTable', {
      tableName: `pulse-personas-${props.stage}`,
      partitionKey: { name: 'personaId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI for lookups by role template
    this.personaTable.addGlobalSecondaryIndex({
      indexName: 'by-role-template',
      partitionKey: { name: 'roleTemplate', type: dynamodb.AttributeType.STRING },
    });

    // --- Shared Lambda layer for src/shared ---
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
      description: 'Shared utilities layer (models, config, bedrock_client)',
    });

    // --- Audience Router Lambda ---
    const audienceRouter = new lambda.Function(this, 'AudienceRouter', {
      functionName: `pulse-audience-router-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'audience_router.handler',
      code: lambda.Code.fromAsset('../src/persona'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        PERSONA_TABLE_NAME: this.personaTable.tableName,
        STAGE: props.stage,
      },
    });
    this.personaTable.grantReadData(audienceRouter);

    // --- Content Transformer Lambda ---
    const contentTransformer = new lambda.Function(this, 'ContentTransformer', {
      functionName: `pulse-content-transformer-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'content_transformer.handler',
      code: lambda.Code.fromAsset('../src/persona'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        PERSONA_TABLE_NAME: this.personaTable.tableName,
        STAGE: props.stage,
      },
    });
    this.personaTable.grantReadData(contentTransformer);

    // Bedrock invoke permission
    contentTransformer.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['*'],
    }));

    // --- Email Sender Lambda ---
    const emailSender = new lambda.Function(this, 'EmailSender', {
      functionName: `pulse-email-sender-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'email_sender.handler',
      code: lambda.Code.fromAsset('../src/delivery'),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        DELIVERY_TABLE_NAME: props.deliveryTableName,
        SES_DOMAIN: props.sesDomain,
        STAGE: props.stage,
      },
    });

    emailSender.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ses:SendEmail', 'ses:SendRawEmail'],
      resources: ['*'],
    }));

    emailSender.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'],
      resources: [`arn:aws:dynamodb:*:*:table/${props.deliveryTableName}`],
    }));

    // --- Step Functions Workflow ---
    // Step 1: Route signal to matching personas
    const routeStep = new tasks.LambdaInvoke(this, 'RouteToPersonas', {
      lambdaFunction: audienceRouter,
      outputPath: '$.Payload',
    });

    // Step 2: Transform content for each persona
    const transformStep = new tasks.LambdaInvoke(this, 'TransformContent', {
      lambdaFunction: contentTransformer,
      outputPath: '$.Payload',
    });

    // Step 3: Deliver to each recipient via email
    const deliverStep = new tasks.LambdaInvoke(this, 'DeliverEmail', {
      lambdaFunction: emailSender,
      inputPath: '$',
      outputPath: '$.Payload',
    });

    // Map state to iterate over transformations and deliver each
    const deliverMap = new sfn.Map(this, 'DeliverToRecipients', {
      itemsPath: '$.transformations',
      parameters: {
        'delivery.$': '$$.Map.Item.Value',
        'signal.$': '$.signal',
      },
      maxConcurrency: 5,
    });
    deliverMap.itemProcessor(deliverStep);

    // Wire: Route → Transform → Deliver (map)
    const definition = routeStep
      .next(transformStep)
      .next(deliverMap);

    this.personaWorkflow = new sfn.StateMachine(this, 'PersonaWorkflow', {
      stateMachineName: `pulse-persona-workflow-${props.stage}`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.minutes(5),
    });

    // --- Seed MVP personas via custom resource ---
    const seedPersonas = new cr.AwsCustomResource(this, 'SeedPersonas', {
      onCreate: {
        service: 'DynamoDB',
        action: 'batchWriteItem',
        parameters: {
          RequestItems: {
            [this.personaTable.tableName]: [
              {
                PutRequest: {
                  Item: {
                    personaId: { S: 'persona-ciso' },
                    orgId: { S: 'default' },
                    name: { S: 'CISO' },
                    roleTemplate: { S: 'ciso' },
                    languageLevel: { S: 'executive' },
                    members: { L: [{ M: { principalId: { S: 'ciso@example.com' }, channels: { L: [{ S: 'email' }] } } }] },
                    deliveryPreferences: { M: {
                      channels: { L: [{ S: 'email' }] },
                      cadence: { S: 'realtime' },
                      escalationAfterMinutes: { N: '15' },
                    }},
                    subscriptions: { L: [] },
                    suppressionRules: { L: [] },
                  },
                },
              },
              {
                PutRequest: {
                  Item: {
                    personaId: { S: 'persona-sre' },
                    orgId: { S: 'default' },
                    name: { S: 'SRE' },
                    roleTemplate: { S: 'sre' },
                    languageLevel: { S: 'detailed_technical' },
                    members: { L: [{ M: { principalId: { S: 'sre@example.com' }, channels: { L: [{ S: 'email' }] } } }] },
                    deliveryPreferences: { M: {
                      channels: { L: [{ S: 'email' }] },
                      cadence: { S: 'realtime' },
                      escalationAfterMinutes: { N: '10' },
                    }},
                    subscriptions: { L: [] },
                    suppressionRules: { L: [] },
                  },
                },
              },
              {
                PutRequest: {
                  Item: {
                    personaId: { S: 'persona-cto' },
                    orgId: { S: 'default' },
                    name: { S: 'CTO' },
                    roleTemplate: { S: 'cto' },
                    languageLevel: { S: 'technical_summary' },
                    members: { L: [{ M: { principalId: { S: 'cto@example.com' }, channels: { L: [{ S: 'email' }] } } }] },
                    deliveryPreferences: { M: {
                      channels: { L: [{ S: 'email' }] },
                      cadence: { S: 'realtime' },
                      escalationAfterMinutes: { N: '30' },
                    }},
                    subscriptions: { L: [] },
                    suppressionRules: { L: [] },
                  },
                },
              },
            ],
          },
        },
        physicalResourceId: cr.PhysicalResourceId.of('seed-personas-v1'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.personaTable.tableArn],
      }),
    });

    seedPersonas.node.addDependency(this.personaTable);

    // --- Outputs ---
    new cdk.CfnOutput(this, 'PersonaTableName', { value: this.personaTable.tableName });
    new cdk.CfnOutput(this, 'WorkflowArn', { value: this.personaWorkflow.stateMachineArn });
  }
}
