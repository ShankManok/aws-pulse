import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface DeliveryStackProps extends cdk.StackProps {
  stage: string;
}

export class DeliveryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DeliveryStackProps) {
    super(scope, id, props);
    // TODO: Implement DeliveryStack
  }
}
