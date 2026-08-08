#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { IngestionStack } from '../lib/ingestion-stack';
import { IntelligenceStack } from '../lib/intelligence-stack';
import { PersonaStack } from '../lib/persona-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { LearningStack } from '../lib/learning-stack';
import { AnalyticsStack } from '../lib/analytics-stack';

const app = new cdk.App();
const stage = app.node.tryGetContext('stage') || 'dev';
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION || 'ap-southeast-1' };

const ingestion = new IngestionStack(app, `Pulse-Ingestion-${stage}`, { env, stage });
const intelligence = new IntelligenceStack(app, `Pulse-Intelligence-${stage}`, { env, stage, signalStream: ingestion.signalStream, signalTable: ingestion.signalTable });
const persona = new PersonaStack(app, `Pulse-Persona-${stage}`, { env, stage });
const delivery = new DeliveryStack(app, `Pulse-Delivery-${stage}`, { env, stage });
const learning = new LearningStack(app, `Pulse-Learning-${stage}`, { env, stage });
const analytics = new AnalyticsStack(app, `Pulse-Analytics-${stage}`, { env, stage });
