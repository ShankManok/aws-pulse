const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const dir = process.env.PULSE_CDK_OUT || path.join(__dirname, '..', 'cdk.out');
const templates = Object.fromEntries(fs.readdirSync(dir).filter(f => f.endsWith('.template.json')).map(f => [f, JSON.parse(fs.readFileSync(path.join(dir, f)))]));
const resources = Object.values(templates).flatMap(t => Object.entries(t.Resources));

test('publish and persona management require IAM authorization', () => {
  const methods = resources.filter(([,r]) => r.Type === 'AWS::ApiGateway::Method').map(([,r]) => r.Properties);
  const publish = methods.find(m => m.HttpMethod === 'POST' && m.ApiKeyRequired === true);
  assert.equal(publish.AuthorizationType, 'AWS_IAM');
  const persona = Object.entries(templates).find(([name]) => name.includes('Persona'))[1];
  const post = Object.values(persona.Resources).find(r => r.Type === 'AWS::ApiGateway::Method' && r.Properties.HttpMethod === 'POST');
  assert.equal(post.Properties.AuthorizationType, 'AWS_IAM');
});

test('native events route exactly once through Lambda normalization', () => {
  const rules = resources.filter(([,r]) => r.Type === 'AWS::Events::Rule' && r.Properties.EventPattern?.source?.includes('aws.cloudwatch'));
  assert.equal(rules.length, 1);
  assert.equal(rules[0][1].Properties.Targets.length, 1);
  assert.match(JSON.stringify(rules[0][1].Properties.Targets), /OrgForwarder/);
});

test('correlator can update the deployed signal table', () => {
  const policy = resources.find(([id,r]) => id.startsWith('CorrelatorServiceRoleDefaultPolicy') && r.Type === 'AWS::IAM::Policy')[1];
  assert.ok(policy.Properties.PolicyDocument.Statement.some(s => [].concat(s.Action).includes('dynamodb:UpdateItem') && JSON.stringify(s.Resource).includes('SignalTable')));
});

test('cross-account bus access is disabled without an organization ID', () => {
  assert.equal(resources.filter(([,r]) => r.Type === 'AWS::Events::EventBusPolicy').length, 0);
});

test('CloudFormation resources have no dependency cycles', () => {
  for (const [filename, template] of Object.entries(templates)) {
    const graph = {};
    for (const [id, resource] of Object.entries(template.Resources)) {
      const refs = new Set([].concat(resource.DependsOn || []));
      function visit(value) {
        if (!value || typeof value !== 'object') return;
        if (value.Ref) refs.add(value.Ref);
        if (value['Fn::GetAtt']) refs.add([].concat(value['Fn::GetAtt'])[0].split('.')[0]);
        if (value['Fn::Sub']) {
          const sub = Array.isArray(value['Fn::Sub']) ? value['Fn::Sub'][0] : value['Fn::Sub'];
          for (const match of sub.matchAll(/\$\{([^}.]+)(?:\.[^}]*)?\}/g)) refs.add(match[1]);
        }
        Object.values(value).forEach(visit);
      }
      visit(resource.Properties);
      graph[id] = [...refs].filter(ref => ref in template.Resources && ref !== id);
    }
    const done = new Set();
    function walk(id, chain = []) {
      assert.ok(!chain.includes(id), `${filename}: dependency cycle ${[...chain,id].join(' -> ')}`);
      if (done.has(id)) return;
      graph[id].forEach(next => walk(next, [...chain,id])); done.add(id);
    }
    Object.keys(graph).forEach(id => walk(id));
  }
});

test('all management methods use IAM and usage keys on the main API', () => {
  const template = Object.entries(templates).find(([name]) => name.includes('Ingestion'))[1];
  const methods = Object.values(template.Resources).filter(r => r.Type === 'AWS::ApiGateway::Method');
  const protectedMethods = methods.filter(r => r.Properties.ApiKeyRequired);
  assert.equal(protectedMethods.length, 8);
  assert.ok(protectedMethods.every(r => r.Properties.AuthorizationType === 'AWS_IAM'));
  const handler = Object.values(template.Resources).find(r => r.Type === 'AWS::Lambda::Function' && r.Properties.Handler === 'management.handler');
  assert.ok(handler.Properties.Environment.Variables.API_ACCOUNT_IDS);
  for (const field of ['PERSONA_TABLE_NAME','DELIVERY_TABLE_NAME','SIGNAL_TABLE_NAME','ANALYTICS_TABLE_NAME','ACTION_FUNCTION_NAME','SUBSCRIPTION_FUNCTION_NAME']) assert.ok(handler.Properties.Environment.Variables[field]);
});

test('signal publication uses a DynamoDB stream outbox with partial failure retries', () => {
  const template = Object.entries(templates).find(([name]) => name.includes('Ingestion'))[1];
  const table = Object.values(template.Resources).find(r => r.Type === 'AWS::DynamoDB::Table');
  assert.equal(table.Properties.StreamSpecification.StreamViewType, 'NEW_IMAGE');
  const mapping = Object.values(template.Resources).find(r => r.Type === 'AWS::Lambda::EventSourceMapping');
  assert.deepEqual(mapping.Properties.FunctionResponseTypes, ['ReportBatchItemFailures']);
  assert.ok(mapping.Properties.DestinationConfig.OnFailure.Destination);
  const producers = Object.entries(template.Resources).filter(([id,r]) => r.Type === 'AWS::IAM::Policy' && /PublishHandler|Adapter|OrgForwarder/.test(id));
  assert.ok(producers.every(([,r]) => !JSON.stringify(r).includes('kinesis:PutRecord')));
});

test('audit history is versioned, retained and includes delivery mutations', () => {
  const template = Object.entries(templates).find(([name]) => name.includes('Analytics'))[1];
  const bucket = Object.values(template.Resources).find(r => r.Type === 'AWS::S3::Bucket');
  assert.equal(bucket.Properties.VersioningConfiguration.Status, 'Enabled');
  assert.equal(bucket.Properties.ObjectLockConfiguration.Rule.DefaultRetention.Mode, 'COMPLIANCE');
  assert.equal(bucket.DeletionPolicy, 'Retain');
  assert.ok(Object.values(template.Resources).some(r => r.Type === 'AWS::Lambda::EventSourceMapping'));
});

test('delivery claims have required permissions and unknown channels fail explicitly', () => {
  const template = Object.entries(templates).find(([name]) => name.includes('Persona'))[1];
  for (const prefix of ['EmailSender','SlackSender']) {
    const policy = Object.entries(template.Resources).find(([id,r]) => id.startsWith(prefix+'ServiceRoleDefaultPolicy') && r.Type === 'AWS::IAM::Policy')[1];
    const actions = policy.Properties.PolicyDocument.Statement.flatMap(s => [].concat(s.Action));
    for (const action of ['dynamodb:PutItem','dynamodb:GetItem','dynamodb:UpdateItem']) assert.ok(actions.includes(action));
  }
  const workflow = Object.values(template.Resources).find(r => r.Type === 'AWS::StepFunctions::StateMachine');
  assert.ok(JSON.stringify(workflow).includes('UnsupportedChannel'));
  const slack = Object.values(template.Resources).find(r => r.Type === 'AWS::Lambda::Function' && r.Properties.Handler === 'slack_sender.handler');
  assert.equal(slack.Properties.Environment.Variables.SLACK_DESTINATIONS, '{}');
});
