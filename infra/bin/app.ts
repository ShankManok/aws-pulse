#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { IngestionStack } from '../lib/ingestion-stack';
import { IntelligenceStack } from '../lib/intelligence-stack';
import { PersonaStack } from '../lib/persona-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { LearningStack } from '../lib/learning-stack';
import { AnalyticsStack } from '../lib/analytics-stack';
import { OrgSetupStack } from '../lib/org-setup-stack';

const app = new cdk.App();
const stage = app.node.tryGetContext('stage') || 'dev';
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION || 'ap-southeast-1' };

// --- Layer 1: Ingestion (no dependencies) ---
const ingestion = new IngestionStack(app, `Pulse-Ingestion-${stage}`, { env, stage });

// --- Layer 2: Delivery (no dependencies on other Pulse stacks) ---
const delivery = new DeliveryStack(app, `Pulse-Delivery-${stage}`, { env, stage });

// --- Layer 3: Persona (depends on Delivery for table name + SES domain) ---
const persona = new PersonaStack(app, `Pulse-Persona-${stage}`, {
  env,
  stage,
  deliveryTableName: delivery.deliveryTable.tableName,
  sesDomain: delivery.sesDomain,
  callbackApiUrl: delivery.callbackApiUrl,
});
persona.addDependency(delivery);

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

// --- Layer 5: Analytics (depends on Delivery for table name) ---
const analytics = new AnalyticsStack(app, `Pulse-Analytics-${stage}`, {
  env,
  stage,
  deliveryTableName: delivery.deliveryTable.tableName,
});
analytics.addDependency(delivery);

// --- Layer 6: Learning (depends on Delivery stream, Persona table, Signal table, Analytics table) ---
const learning = new LearningStack(app, `Pulse-Learning-${stage}`, {
  env,
  stage,
  deliveryTableName: delivery.deliveryTable.tableName,
  deliveryTableStreamArn: delivery.deliveryTable.tableStreamArn!,
  personaTableName: persona.personaTable.tableName,
  personaTableArn: persona.personaTable.tableArn,
  signalTableName: ingestion.signalTable.tableName,
  analyticsTableName: analytics.analyticsTable.tableName,
});
learning.addDependency(delivery);
learning.addDependency(persona);
learning.addDependency(ingestion);
learning.addDependency(analytics);

// --- Optional: OrgSetupStack (only when pulseAccountId context is provided) ---
// Deploy with: cdk deploy Pulse-OrgSetup-dev --context stage=dev --context pulseAccountId=123456789012
const pulseAccountId = app.node.tryGetContext('pulseAccountId');
if (pulseAccountId) {
  new OrgSetupStack(app, `Pulse-OrgSetup-${stage}`, {
    env,
    stage,
    pulseAccountId,
    organizationId: app.node.tryGetContext('organizationId'),
  });
}
