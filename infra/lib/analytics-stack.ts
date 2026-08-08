import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface AnalyticsStackProps extends cdk.StackProps {
  stage: string;
}

export class AnalyticsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AnalyticsStackProps) {
    super(scope, id, props);
    // TODO: Implement AnalyticsStack
  }
}
