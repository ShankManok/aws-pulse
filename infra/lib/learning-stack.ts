import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface LearningStackProps extends cdk.StackProps {
  stage: string;
}

export class LearningStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: LearningStackProps) {
    super(scope, id, props);
    // TODO: Implement LearningStack
  }
}
