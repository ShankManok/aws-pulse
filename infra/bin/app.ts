#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { IngestionStack } from '../lib/ingestion-stack';
import { IntelligenceStack } from '../lib/intelligence-stack';
import { PersonaStack } from '../lib/persona-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { LearningStack } from '../lib/learning-stack';
import { AnalyticsStack } from '../lib/analytics-stack';

const app = new cdk.App();
const stage = app.node.tryGetContext('stage') || 'dev';
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION || 'ap-southeast-1' };

// --- Layer 1: Ingestion (no dependencies) ---
const ingestion = new IngestionStack(app, `Pulse-Ingestion-${stage}`, { env, stage });

// --- Layer 2: Delivery (independent, exports escalation infra) ---
// personaWorkflowArn is wired after PersonaStack creation via addEnvironment
const delivery = new DeliveryStack(app, `Pulse-Delivery-${stage}`, { env, stage });

// --- Layer 3: Persona (depends on Delivery for table name + SES domain + escalation) ---
const persona = new PersonaStack(app, `Pulse-Persona-${stage}`, {
  env,
  stage,
  deliveryTableName: delivery.deliveryTable.tableName,
  sesDomain: delivery.sesDomain,
  escalationSchedulerArn: delivery.escalationSchedulerArn,
  schedulerRoleArn: delivery.schedulerRoleArn,
  schedulerGroupName: delivery.schedulerGroupName,
  escalationHandlerArn: delivery.escalationHandlerArn,
});
persona.addDependency(delivery);

// Wire persona workflow ARN back into escalation handler (breaks circular dep)
delivery.escalationHandlerFn.addEnvironment('PERSONA_WORKFLOW_ARN', persona.personaWorkflow.stateMachineArn);
delivery.escalationHandlerFn.addToRolePolicy(new iam.PolicyStatement({
  actions: ['states:StartExecution'],
  resources: [persona.personaWorkflow.stateMachineArn],
}));

// --- Layer 4: Intelligence (depends on Ingestion stream + Persona workflow) ---
const intelligence = new IntelligenceStack(app, `Pulse-Intelligence-${stage}`, {
  env,
  stage,
  signalStream: ingestion.signalStream,
  signalTable: ingestion.signalTable,
  personaWorkflowArn: persona.personaWorkflow.stateMachineArn,
});
intelligence.addDependency(ingestion);
intelligence.addDependency(persona);

// --- Layer 5: Learning & Analytics (future phases, placeholder stacks) ---
const learning = new LearningStack(app, `Pulse-Learning-${stage}`, { env, stage });
const analytics = new AnalyticsStack(app, `Pulse-Analytics-${stage}`, { env, stage });
