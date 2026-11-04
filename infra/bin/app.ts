#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { KiroAgentCoreConfig, KiroAgentCoreStack } from '../lib/kiro-agentcore-stack';

const app = new App();

function ctx<T>(key: string): T {
  const value = app.node.tryGetContext(key);
  if (value === undefined) {
    throw new Error(`missing CDK context value: ${key}`);
  }
  return value as T;
}

const account = process.env.CDK_DEFAULT_ACCOUNT;
const config: KiroAgentCoreConfig = {
  defaultModel: ctx('defaultModel'),
  allowedModels: ctx('allowedModels'),
  defaultEffort: ctx('defaultEffort'),
  allowedEfforts: ctx('allowedEfforts'),
  defaultMaxIterations: Number(ctx('defaultMaxIterations')),
  maxIterationsLimit: Number(ctx('maxIterationsLimit')),
  defaultTimeoutSeconds: Number(ctx('defaultTimeoutSeconds')),
  maxTimeoutSeconds: Number(ctx('maxTimeoutSeconds')),
  kiroApiKeySecretName: ctx('kiroApiKeySecretName'),
  gitCredentialsSecretName: ctx('gitCredentialsSecretName'),
  kiroCliVersion: ctx('kiroCliVersion'),
  cognitoDomainPrefix: app.node.tryGetContext('cognitoDomainPrefix') || `kiro-agentcore-${account ?? 'dev'}`,
  machineClientNames: app.node.tryGetContext('machineClientNames') ?? [],
};

new KiroAgentCoreStack(app, 'KiroAgentCoreStack', {
  env: { account, region: 'us-east-1' },
  description: 'kiro-cli on Amazon Bedrock AgentCore Runtime (closed-loop agent)',
  config,
});
