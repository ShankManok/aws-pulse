import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface PersonaStackProps extends cdk.StackProps {
  stage: string;
}

export class PersonaStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PersonaStackProps) {
    super(scope, id, props);
    // TODO: Implement PersonaStack
  }
}
