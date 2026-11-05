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

test('runtime role is read-only with explicit denies and scoped writes', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    ManagedPolicyArns: Match.arrayWith([
      Match.objectLike({ 'Fn::Join': Match.arrayWith([Match.arrayWith([':iam::aws:policy/ReadOnlyAccess'])]) }),
    ]),
  });
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
