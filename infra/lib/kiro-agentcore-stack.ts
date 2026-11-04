import * as path from 'path';
import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpUserPoolAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface KiroAgentCoreConfig {
  readonly defaultModel: string;
  readonly allowedModels: string[];
  readonly defaultEffort: string;
  readonly allowedEfforts: string[];
  readonly defaultMaxIterations: number;
  readonly maxIterationsLimit: number;
  readonly defaultTimeoutSeconds: number;
  readonly maxTimeoutSeconds: number;
  readonly kiroApiKeySecretName: string;
  /** Empty string or "NONE" disables Git credentials. */
  readonly gitCredentialsSecretName: string;
  readonly kiroCliVersion: string;
  /** Globally unique prefix for the Cognito managed login domain. */
  readonly cognitoDomainPrefix: string;
  /** Names of additional confidential app clients for system callers (client_credentials). */
  readonly machineClientNames: string[];
}

export interface KiroAgentCoreStackProps extends StackProps {
  readonly config: KiroAgentCoreConfig;
}

const LOG_RETENTION = logs.RetentionDays.ONE_MONTH;
const RUNTIME_NAME = 'kiro_agentcore';
const SCOPE = 'kiro-agent/invoke';
// Keep user data when the stack is updated or deleted, but clean up if the very first create rolls back,
// so a failed first install does not leave orphaned resources behind.
const KEEP_DATA = RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE;

export class KiroAgentCoreStack extends Stack {
  constructor(scope: Construct, id: string, props: KiroAgentCoreStackProps) {
    super(scope, id, props);
    const cfg = props.config;
    const repoRoot = path.join(__dirname, '..', '..');

    // ---------------------------------------------------------------- Cognito
    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: false,
      signInAliases: { username: true, email: true },
      passwordPolicy: { minLength: 12, requireDigits: true, requireLowercase: true, requireUppercase: true, requireSymbols: true },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { otp: true, sms: false },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: KEEP_DATA,
    });
    const domain = userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: cfg.cognitoDomainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,
    });
    const invokeScope = new cognito.ResourceServerScope({ scopeName: 'invoke', scopeDescription: 'Submit and query tasks' });
    const resourceServer = userPool.addResourceServer('ResourceServer', {
      identifier: 'kiro-agent',
      scopes: [invokeScope],
    });
    const customScope = cognito.OAuthScope.resourceServer(resourceServer, invokeScope);

    const cliClient = userPool.addClient('KacCliClient', {
      userPoolClientName: 'kac-cli',
      generateSecret: false,
      authFlows: {},
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, customScope],
        callbackUrls: ['http://localhost:8765/callback'],
        logoutUrls: ['http://localhost:8765/logout'],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
    });
    const machineClients = ['kac-machine', ...cfg.machineClientNames].map((name, i) =>
      userPool.addClient(i === 0 ? 'KacMachineClient' : `MachineClient${i}`, {
        userPoolClientName: name,
        generateSecret: true,
        authFlows: {},
        oAuth: { flows: { clientCredentials: true }, scopes: [customScope] },
        supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
        accessTokenValidity: Duration.hours(1),
        preventUserExistenceErrors: true,
      }),
    );
    for (const c of [cliClient, ...machineClients]) {
      c.node.addDependency(resourceServer);
    }
    const allClients = [cliClient, ...machineClients];

    // ------------------------------------------------------------- Data stores
    const table = new dynamodb.Table(this, 'TasksTable', {
      partitionKey: { name: 'task_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'expires_at',
      removalPolicy: KEEP_DATA,
    });
    table.addGlobalSecondaryIndex({
      indexName: 'owner-created_at',
      partitionKey: { name: 'owner', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    table.addGlobalSecondaryIndex({
      indexName: 'running-index',
      partitionKey: { name: 'running_shard', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'heartbeat_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    const bucket = new s3.Bucket(this, 'ArtifactsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: false,
      lifecycleRules: [{ id: 'expire-after-90-days', expiration: Duration.days(90), abortIncompleteMultipartUploadAfter: Duration.days(1) }],
      removalPolicy: KEEP_DATA,
    });

    const bus = new events.EventBus(this, 'EventBus', { eventBusName: 'kiro-agentcore' });

    const kiroKeySecret = secretsmanager.Secret.fromSecretNameV2(this, 'KiroApiKeySecret', cfg.kiroApiKeySecretName);
    const gitSecretName = cfg.gitCredentialsSecretName.toUpperCase() === 'NONE' ? '' : cfg.gitCredentialsSecretName;
    const gitSecret = gitSecretName
      ? secretsmanager.Secret.fromSecretNameV2(this, 'GitCredentialsSecret', gitSecretName)
      : undefined;

    // ----------------------------------------------------------------- Runtime
    const runtime = new agentcore.Runtime(this, 'Runtime', {
      runtimeName: RUNTIME_NAME,
      description: 'kiro-cli closed-loop agent',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromAsset(path.join(repoRoot, 'runtime'), {
        platform: Platform.LINUX_ARM64,
        buildArgs: { KIRO_CLI_VERSION: cfg.kiroCliVersion },
      }),
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingCognito(userPool, allClients, undefined, [SCOPE]),
      requestHeaderConfiguration: { allowlistedHeaders: ['Authorization'] },
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: Duration.minutes(5),
        maxLifetime: Duration.hours(8),
      },
      tracingEnabled: true,
      environmentVariables: {
        DEFAULT_MODEL: cfg.defaultModel,
        ALLOWED_MODELS: cfg.allowedModels.join(','),
        DEFAULT_EFFORT: cfg.defaultEffort,
        ALLOWED_EFFORTS: cfg.allowedEfforts.join(','),
        DEFAULT_MAX_ITERATIONS: String(cfg.defaultMaxIterations),
        MAX_ITERATIONS_LIMIT: String(cfg.maxIterationsLimit),
        DEFAULT_TIMEOUT_SECONDS: String(cfg.defaultTimeoutSeconds),
        MAX_TIMEOUT_SECONDS: String(cfg.maxTimeoutSeconds),
        KIRO_API_KEY_SECRET_NAME: cfg.kiroApiKeySecretName,
        GIT_CREDENTIALS_SECRET_NAME: gitSecretName || 'NONE',
        TASKS_TABLE: table.tableName,
        ARTIFACTS_BUCKET: bucket.bucketName,
        EVENT_BUS_NAME: bus.eventBusName,
      },
    });

    const role = runtime.role;
    if (!(role instanceof iam.Role)) {
      throw new Error('expected the Runtime construct to create its execution role');
    }
    role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('ReadOnlyAccess'));
    kiroKeySecret.grantRead(role);
    gitSecret?.grantRead(role);
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'TasksTableWrite',
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [table.tableArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'ArtifactsWrite',
      actions: ['s3:PutObject'],
      resources: [bucket.arnForObjects('*')],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'PublishEvents',
      actions: ['events:PutEvents'],
      resources: [bus.eventBusArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'DenyPrivilegedAndDestructive',
      effect: iam.Effect.DENY,
      actions: [
        'iam:*',
        'organizations:*',
        'sts:AssumeRole',
        's3:DeleteObject*',
        's3:PutBucket*',
        's3:DeleteBucket*',
        'secretsmanager:PutSecretValue',
        'secretsmanager:UpdateSecret',
        'secretsmanager:DeleteSecret',
        'secretsmanager:CreateSecret',
        'dynamodb:DeleteTable',
        'dynamodb:DeleteItem',
      ],
      resources: ['*'],
    }));

    // AgentCore creates the runtime log group itself as soon as the runtime exists, so it cannot be
    // pre-created here. LogRetention sets the 30-day retention on it (and creates it if still missing).
    const runtimeLogGroupName = `/aws/bedrock-agentcore/runtimes/${runtime.agentRuntimeId}-DEFAULT`;
    const runtimeLogRetention = new logs.LogRetention(this, 'RuntimeLogRetention', {
      logGroupName: runtimeLogGroupName,
      retention: LOG_RETENTION,
    });
    runtimeLogRetention.node.addDependency(runtime);

    // ---------------------------------------------------------------- Lambdas
    const lambdaCode = lambda.Code.fromAsset(path.join(repoRoot, 'lambdas'), {
      exclude: ['tests', '.venv', '**/__pycache__', '.pytest_cache', '.ruff_cache', 'pyproject.toml', 'uv.lock'],
    });
    const fnDefaults = {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      code: lambdaCode,
      memorySize: 256,
    };

    const queryFn = new lambda.Function(this, 'QueryFunction', {
      ...fnDefaults,
      handler: 'query_api.handler.handler',
      timeout: Duration.seconds(15),
      environment: { TASKS_TABLE: table.tableName, ARTIFACTS_BUCKET: bucket.bucketName },
      logGroup: new logs.LogGroup(this, 'QueryFunctionLogs', { retention: LOG_RETENTION, removalPolicy: RemovalPolicy.DESTROY }),
    });
    queryFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [table.tableArn, `${table.tableArn}/index/owner-created_at`],
    }));
    queryFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [bucket.bucketArn] }));
    queryFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:GetObject'], resources: [bucket.arnForObjects('*')] }));

    const sweeperFn = new lambda.Function(this, 'SweeperFunction', {
      ...fnDefaults,
      handler: 'sweeper.handler.handler',
      timeout: Duration.minutes(2),
      environment: { TASKS_TABLE: table.tableName, EVENT_BUS_NAME: bus.eventBusName, STALE_AFTER_SECONDS: '600' },
      logGroup: new logs.LogGroup(this, 'SweeperFunctionLogs', { retention: LOG_RETENTION, removalPolicy: RemovalPolicy.DESTROY }),
    });
    sweeperFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [`${table.tableArn}/index/running-index`],
    }));
    sweeperFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:UpdateItem'], resources: [table.tableArn] }));
    sweeperFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [bus.eventBusArn] }));

    new scheduler.Schedule(this, 'SweeperSchedule', {
      schedule: scheduler.ScheduleExpression.rate(Duration.minutes(5)),
      target: new schedulerTargets.LambdaInvoke(sweeperFn, {}),
      description: 'Mark tasks whose runtime session was lost as FAILED',
    });

    // --------------------------------------------------------------- Query API
    const authorizer = new HttpUserPoolAuthorizer('CognitoAuthorizer', userPool, { userPoolClients: allClients });
    const accessLogs = new logs.LogGroup(this, 'QueryApiAccessLogs', { retention: LOG_RETENTION, removalPolicy: RemovalPolicy.DESTROY });
    const api = new apigwv2.HttpApi(this, 'QueryApi', {
      apiName: 'kiro-agentcore-query',
      createDefaultStage: false,
    });
    new apigwv2.HttpStage(this, 'QueryApiStage', {
      httpApi: api,
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 20, burstLimit: 40 },
      accessLogSettings: { destination: new apigwv2.LogGroupLogDestination(accessLogs) },
    });
    const integration = new HttpLambdaIntegration('QueryIntegration', queryFn);
    for (const routePath of ['/tasks', '/tasks/{task_id}', '/tasks/{task_id}/artifacts']) {
      api.addRoutes({
        path: routePath,
        methods: [apigwv2.HttpMethod.GET],
        integration,
        authorizer,
        authorizationScopes: [SCOPE],
      });
    }

    // ----------------------------------------------------------------- Outputs
    const out = (name: string, value: string) => new CfnOutput(this, name, { value });
    out('RuntimeArn', runtime.agentRuntimeArn);
    out('RuntimeLogGroupName', runtimeLogGroupName);
    out('QueryApiUrl', api.apiEndpoint);
    out('UserPoolId', userPool.userPoolId);
    out('CognitoDomain', domain.baseUrl());
    out('CliClientId', cliClient.userPoolClientId);
    out('MachineClientId', machineClients[0].userPoolClientId);
    out('OAuthScope', SCOPE);
    out('ArtifactsBucketName', bucket.bucketName);
    out('TasksTableName', table.tableName);
    out('EventBusName', bus.eventBusName);
  }
}
