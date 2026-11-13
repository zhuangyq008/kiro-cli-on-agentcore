import { parseAgentAwsAccess } from '../lib/agent-access';

test('no value, an empty value and "none" all mean no AWS access for tasks', () => {
  for (const value of [undefined, '', '  ', 'none', 'NONE']) {
    expect(parseAgentAwsAccess(value)).toEqual({ mode: 'none', policyArns: [], label: 'none' });
  }
});

test('presets are case-insensitive', () => {
  expect(parseAgentAwsAccess('viewonly')).toEqual({ mode: 'viewonly', policyArns: [], label: 'viewonly' });
  expect(parseAgentAwsAccess('ReadOnly')).toEqual({ mode: 'readonly', policyArns: [], label: 'readonly' });
});

test('managed policy ARNs are accepted as a comma-separated string or an array', () => {
  const a = 'arn:aws:iam::111111111111:policy/kiro-agent-read';
  const b = 'arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess';
  expect(parseAgentAwsAccess(`${a}, ${b}`)).toEqual({ mode: 'custom', policyArns: [a, b], label: `${a},${b}` });
  expect(parseAgentAwsAccess([a, a])).toEqual({ mode: 'custom', policyArns: [a], label: a });
});

test.each([
  ['admin', /agentAwsAccess/],
  ['readonly,viewonly', /agentAwsAccess/],
  ['arn:aws:iam::111111111111:role/some-role', /agentAwsAccess/],
  ['arn:aws:iam::aws:policy/AdministratorAccess', /write access/],
  ['arn:aws:iam::aws:policy/PowerUserAccess', /write access/],
  ['arn:aws:iam::aws:policy/AmazonS3FullAccess', /write access/],
  ['arn:aws:iam::aws:policy/IAMFullAccess', /write access/],
  // Write-capable AWS managed policies whose names do not say so.
  ['arn:aws:iam::aws:policy/AdministratorAccess-Amplify', /write access/],
  ['arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess_v2', /write access/],
  ['arn:aws:iam::aws:policy/job-function/SystemAdministrator', /write access/],
  ['arn:aws:iam::aws:policy/SecretsManagerReadWrite', /write access/],
  ['arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser', /write access/],
  ['none,arn:aws:iam::111111111111:policy/p', /agentAwsAccess/],
  ['arn:aws-cn:iam::aws:policy/AmazonS3ReadOnlyAccess', /agentAwsAccess/],
  [42, /agentAwsAccess/],
])('rejects %p', (value, message) => {
  expect(() => parseAgentAwsAccess(value)).toThrow(message);
});

test.each([
  'arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess',
  'arn:aws:iam::aws:policy/AWSLambda_ReadOnlyAccess',
  'arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly',
  'arn:aws:iam::aws:policy/job-function/ViewOnlyAccess',
  'arn:aws:iam::aws:policy/SecurityAudit',
  'arn:aws:iam::111111111111:policy/team/anything-goes',
])('accepts read-only AWS managed policies and any customer managed policy: %s', (arn) => {
  expect(parseAgentAwsAccess(arn).policyArns).toEqual([arn]);
});

test('presets are de-duplicated regardless of case', () => {
  expect(parseAgentAwsAccess('readonly,READONLY').mode).toBe('readonly');
});

test('rejects a value too long for a stack output', () => {
  const arns = Array.from({ length: 5 }, (_, i) => `arn:aws:iam::111111111111:policy/${'p'.repeat(230)}${i}`);
  expect(() => parseAgentAwsAccess(arns)).toThrow(/too long/);
});

test('rejects more policies than a role can hold alongside the service policy', () => {
  const arns = Array.from({ length: 9 }, (_, i) => `arn:aws:iam::111111111111:policy/p${i}`);
  expect(() => parseAgentAwsAccess(arns)).toThrow(/at most 8/);
});
