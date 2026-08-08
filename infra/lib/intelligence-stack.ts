import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface IntelligenceStackProps extends cdk.StackProps {
  stage: string;
  signalStream: any; signalTable: any;
}

export class IntelligenceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IntelligenceStackProps) {
    super(scope, id, props);
    // TODO: Implement IntelligenceStack
  }
}
