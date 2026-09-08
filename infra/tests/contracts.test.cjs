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
