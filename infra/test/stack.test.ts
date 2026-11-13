import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { KiroAgentCoreStack } from '../lib/kiro-agentcore-stack';

const config = {
  defaultModels: ['claude-sonnet-5.5', 'gpt-5.6-terra', 'auto'],
  allowedModels: ['auto', 'claude-*', 'gpt-*'],
  defaultEffort: 'high',
  allowedEfforts: ['low', 'high'],
  defaultMaxIterations: 3,
  maxIterationsLimit: 5,
  defaultTimeoutSeconds: 7200,
  maxTimeoutSeconds: 27000,
  kiroApiKeySecretName: 'prod/kiro/api-key',
  gitCredentialsSecretName: 'prod/kiro/git-credentials',
  kiroCliVersion: '2.35.2',
  cognitoDomainPrefix: 'kiro-agentcore-test',
  serviceVersion: '9.9.9',
  machineClientNames: [],
};

let template: Template;

beforeAll(() => {
  const app = new App();
  const stack = new KiroAgentCoreStack(app, 'TestStack', {
    env: { account: '111111111111', region: 'us-east-1' },
    config,
  });
  template = Template.fromStack(stack);
});

function statements(): any[] {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

test('artifacts bucket blocks public access, enforces TLS and expires objects after 90 days', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
    BucketEncryption: { ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }] },
    LifecycleConfiguration: {
      Rules: Match.arrayWith([Match.objectLike({ ExpirationInDays: 90, Status: 'Enabled' })]),
    },
  });
  template.hasResourceProperties('AWS::S3::BucketPolicy', {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
      ]),
    },
  });
});

test('tasks table has TTL, PITR and both GSIs', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({ IndexName: 'owner-created_at' }),
      Match.objectLike({ IndexName: 'running-index', Projection: { ProjectionType: 'KEYS_ONLY' } }),
    ]),
  });
});

test('all log groups keep logs for 30 days, including the AgentCore-created runtime log group', () => {
  const groups = template.findResources('AWS::Logs::LogGroup');
  expect(Object.keys(groups).length).toBeGreaterThanOrEqual(3);
  for (const g of Object.values(groups) as any[]) {
    expect(g.Properties.RetentionInDays).toBe(30);
  }
  // The runtime log group is created by AgentCore, so retention is applied by a custom resource.
  template.hasResourceProperties('Custom::LogRetention', {
    RetentionInDays: 30,
    LogGroupName: Match.objectLike({ 'Fn::Join': Match.arrayWith([Match.arrayWith(['/aws/bedrock-agentcore/runtimes/'])]) }),
  });
});

test('data resources survive updates and deletes but not a failed first create', () => {
  for (const type of ['AWS::DynamoDB::Table', 'AWS::S3::Bucket', 'AWS::Cognito::UserPool']) {
    const resources = template.findResources(type);
    for (const r of Object.values(resources) as any[]) {
      expect(r.UpdateReplacePolicy).toBe('Retain');
      expect(r.DeletionPolicy).toBe('RetainExceptOnCreate');
    }
  }
});

test('runtime uses public network, JWT auth with scope, header allowlist and lifecycle', () => {
  template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
    NetworkConfiguration: { NetworkMode: 'PUBLIC' },
    AuthorizerConfiguration: {
      CustomJWTAuthorizer: Match.objectLike({
        DiscoveryUrl: Match.anyValue(),
        AllowedClients: Match.anyValue(),
        AllowedScopes: ['kiro-agent/invoke'],
      }),
    },
    RequestHeaderConfiguration: { RequestHeaderAllowlist: ['Authorization'] },
    LifecycleConfiguration: { IdleRuntimeSessionTimeout: 300, MaxLifetime: 28800 },
    EnvironmentVariables: Match.objectLike({
      DEFAULT_MODEL: 'claude-sonnet-5.5,gpt-5.6-terra,auto',
      ALLOWED_MODELS: 'auto,claude-*,gpt-*',
      DEFAULT_LOOP_MODE: 'none',
      KIRO_API_KEY_SECRET_NAME: 'prod/kiro/api-key',
      DEFAULT_MAX_ITERATIONS: '3',
    }),
  });
});

test('runtime starts sessions from a snapshot (platform version V2)', () => {
  template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', { PlatformVersion: 'V2' });
});

function synth(overrides: Record<string, unknown>): Template {
  const stack = new KiroAgentCoreStack(new App(), 'AccessStack', {
    env: { account: '111111111111', region: 'us-east-1' },
    config: { ...config, ...overrides },
  });
  return Template.fromStack(stack);
}

function runtimeRole(t: Template): any {
  const roles = Object.values(t.findResources('AWS::IAM::Role')) as any[];
  const role = roles.find((r) =>
    JSON.stringify(r.Properties.AssumeRolePolicyDocument).includes('bedrock-agentcore.amazonaws.com'),
  );
  expect(role).toBeDefined();
  return role;
}

test('by default tasks get no AWS access: the runtime role has no managed policy', () => {
  expect(runtimeRole(template).Properties.ManagedPolicyArns).toBeUndefined();
  template.hasOutput('AgentAwsAccess', { Value: 'none' });
});

test.each([
  ['readonly', ':iam::aws:policy/ReadOnlyAccess'],
  ['viewonly', ':iam::aws:policy/job-function/ViewOnlyAccess'],
])('agentAwsAccess %s attaches only the matching AWS managed policy', (access, suffix) => {
  const t = synth({ agentAwsAccess: access });
  const arns = runtimeRole(t).Properties.ManagedPolicyArns;
  expect(arns).toHaveLength(1);
  expect(JSON.stringify(arns[0])).toContain(suffix);
  t.hasOutput('AgentAwsAccess', { Value: access });
});

test('agentAwsAccess accepts customer managed policy ARNs', () => {
  const arn = 'arn:aws:iam::111111111111:policy/kiro-agent-read';
  const t = synth({ agentAwsAccess: arn });
  expect(runtimeRole(t).Properties.ManagedPolicyArns).toEqual([arn]);
  t.hasOutput('AgentAwsAccess', { Value: arn });
});

test('synth rejects a write-capable or malformed agentAwsAccess', () => {
  expect(() => synth({ agentAwsAccess: 'arn:aws:iam::aws:policy/AdministratorAccess' })).toThrow(/write access/);
  expect(() => synth({ agentAwsAccess: 'everything' })).toThrow(/agentAwsAccess/);
});

test('runtime role has explicit denies and scoped writes', () => {
  const deny = statements().find((s) => s.Sid === 'DenyPrivilegedAndDestructive');
  expect(deny).toBeDefined();
  expect(deny.Effect).toBe('Deny');
  for (const a of ['iam:*', 'sts:AssumeRole', 's3:DeleteObject*', 'secretsmanager:PutSecretValue']) {
    expect(deny.Action).toContain(a);
  }
  const writes = statements().find((s) => s.Sid === 'ArtifactsWrite');
  expect(writes.Action).toBe('s3:PutObject');
  // No statement grants a wildcard action on all resources.
  for (const s of statements()) {
    if (s.Effect === 'Allow') {
      expect(s.Action === '*' && s.Resource === '*').toBe(false);
    }
  }
});

// Whatever read access is added for tasks, it must not open up this service's own data: one task
// could otherwise read every other task's prompts and results.
test.each(['none', 'readonly'])('service data stays closed to tasks (agentAwsAccess %s)', (access) => {
  const t = synth({ agentAwsAccess: access });
  const all = Object.values(t.findResources('AWS::IAM::Policy')).flatMap(
    (p: any) => p.Properties.PolicyDocument.Statement,
  );
  const table = all.find((s) => s.Sid === 'TasksTableOnlyItemAccess');
  expect(table.Effect).toBe('Deny');
  expect([...table.NotAction].sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem']);
  expect(table.Resource).toHaveLength(2);
  expect(JSON.stringify(table.Resource)).toContain('TasksTable');
  expect(JSON.stringify(table.Resource[1])).toContain('/*');
  // The Deny must leave open exactly what the wrapper is allowed to do, or the runtime breaks.
  const tableAllow = all.find((s) => s.Sid === 'TasksTableWrite');
  expect([...tableAllow.Action].sort()).toEqual([...table.NotAction].sort());
  expect(all.find((s) => s.Sid === 'ArtifactsWrite').Action).toBe('s3:PutObject');

  // Logs carry task IDs, and a task record can be read by ID; the user pool holds client secrets.
  const runtimeLogs = all.find((s) => s.Sid === 'RuntimeLogsWriteOnly');
  expect(runtimeLogs.Effect).toBe('Deny');
  expect(runtimeLogs.NotAction).not.toContain('logs:GetLogEvents');
  expect(runtimeLogs.NotAction).toEqual(expect.arrayContaining(['logs:CreateLogStream', 'logs:PutLogEvents']));
  expect(JSON.stringify(runtimeLogs.Resource)).toContain('log-group:/aws/bedrock-agentcore/runtimes/*');
  expect(JSON.stringify(runtimeLogs.Resource)).toContain('log-group:aws/spans');
  const traces = all.find((s) => s.Sid === 'TracesWriteOnly');
  expect(traces.Effect).toBe('Deny');
  expect(traces.Action).toEqual(expect.arrayContaining(['xray:BatchGetTraces', 'xray:GetTraceSummaries']));
  // The X-Ray daemon / OTEL exporter still needs to send traces and read sampling rules.
  expect(traces.Action).not.toContain('xray:PutTraceSegments');
  expect(traces.Action).not.toContain('xray:GetSamplingRules');
  const closed = all.find((s) => s.Sid === 'ServiceLogsAndUserPoolClosed');
  expect(closed.Effect).toBe('Deny');
  expect([...closed.Action].sort()).toEqual(['cognito-idp:*', 'logs:*']);
  const closedResources = JSON.stringify(closed.Resource);
  for (const id of ['QueryFunctionLogs', 'SweeperFunctionLogs', 'QueryApiAccessLogs', 'UserPool']) {
    expect(closedResources).toContain(id);
  }

  const bucket = all.find((s) => s.Sid === 'ArtifactsWriteOnly');
  expect(bucket.Effect).toBe('Deny');
  expect(bucket.NotAction).toBe('s3:PutObject');
  expect(bucket.Resource).toHaveLength(2);
  expect(JSON.stringify(bucket.Resource)).toContain('ArtifactsBucket');
});

test('secrets are referenced, not created', () => {
  template.resourceCountIs('AWS::SecretsManager::Secret', 0);
});

test('query API routes require the Cognito JWT authorizer and scope', () => {
  template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', { AuthorizerType: 'JWT' });
  const routes = template.findResources('AWS::ApiGatewayV2::Route');
  expect(Object.keys(routes).length).toBe(3);
  for (const r of Object.values(routes) as any[]) {
    expect(r.Properties.AuthorizationType).toBe('JWT');
    expect(r.Properties.AuthorizationScopes).toEqual(['kiro-agent/invoke']);
  }
});

test('sweeper runs every 5 minutes', () => {
  template.hasResourceProperties('AWS::Scheduler::Schedule', { ScheduleExpression: 'rate(5 minutes)' });
});

test('cognito: no self sign-up; CLI client is public, machine client uses client credentials', () => {
  template.hasResourceProperties('AWS::Cognito::UserPool', {
    AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
  });
  template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
    ClientName: 'kac-cli',
    GenerateSecret: false,
    AllowedOAuthFlows: ['code'],
    CallbackURLs: ['http://localhost:8765/callback'],
  });
  template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
    ClientName: 'kac-machine',
    GenerateSecret: true,
    AllowedOAuthFlows: ['client_credentials'],
  });
});

test('synth fails when a default model is not covered by the allow list', () => {
  const app = new App();
  expect(
    () =>
      new KiroAgentCoreStack(app, 'BadStack', {
        env: { account: '111111111111', region: 'us-east-1' },
        config: { ...config, defaultModels: ['gpt-5.6-sol'], allowedModels: ['claude-*'] },
      }),
  ).toThrow(/not permitted/);
});

test('model settings are exported for install.sh upgrades', () => {
  template.hasOutput('DefaultModels', { Value: 'claude-sonnet-5.5,gpt-5.6-terra,auto' });
  template.hasOutput('AllowedModels', { Value: 'auto,claude-*,gpt-*' });
  template.hasOutput('DefaultLoopMode', { Value: 'none' });
  template.hasOutput('ServiceVersion', { Value: '9.9.9' });
});

test('synth rejects an unknown loop mode', () => {
  const app = new App();
  expect(
    () =>
      new KiroAgentCoreStack(app, 'BadLoop', {
        env: { account: '111111111111', region: 'us-east-1' },
        config: { ...config, defaultLoopMode: 'dag' },
      }),
  ).toThrow(/defaultLoopMode/);
});
