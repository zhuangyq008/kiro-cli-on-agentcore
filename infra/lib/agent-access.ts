/**
 * AWS access granted to tasks (CDK context `agentAwsAccess`).
 *
 * kiro-cli runs with --trust-all-tools and shares the runtime's execution role, so whatever is
 * attached here is reachable by any task, including one steered by a prompt injection. The default
 * is therefore no AWS access beyond what the service itself needs.
 */

export type AgentAwsAccessMode = 'none' | 'viewonly' | 'readonly' | 'custom';

export interface AgentAwsAccess {
  readonly mode: AgentAwsAccessMode;
  /** Managed policy ARNs for `custom`; empty for the presets. */
  readonly policyArns: string[];
  /** Normalized value, exported as a stack output so install.sh keeps it on upgrade. */
  readonly label: string;
}

/** AWS managed policy names behind the presets. */
export const PRESET_POLICY_NAMES: Readonly<Record<'viewonly' | 'readonly', string>> = {
  // Lists and describes resources without reading their content (no S3 objects, no table items).
  viewonly: 'job-function/ViewOnlyAccess',
  // Also reads data: S3 objects, DynamoDB items, logs, Lambda code and environment variables.
  readonly: 'ReadOnlyAccess',
};

// A role holds 10 managed policies by default; keep headroom.
const MAX_CUSTOM_POLICIES = 8;
// The stack is deployed to us-east-1 only, so only the `aws` partition is accepted.
const POLICY_ARN_RE = /^arn:aws:iam::(?:aws|\d{12}):policy\/[A-Za-z0-9+=.@_/-]{1,256}$/;
const AWS_MANAGED_RE = /:iam::aws:policy\//;
// AWS managed policies are accepted only when their name says they are read-only. Anything else
// (including read-write policies with harmless-looking names) needs a customer managed policy, whose
// content the deployer owns.
const READ_ONLY_AWS_POLICY_RE = /:iam::aws:policy\/(?:[A-Za-z0-9_-]+\/)*(?:[A-Za-z0-9_-]*(?:ReadOnlyAccess|ReadOnly|ViewOnlyAccess)|SecurityAudit)$/;
// A CloudFormation output value holds at most 1024 characters.
const MAX_LABEL_LENGTH = 1000;
const PRESET_RE = /^(?:none|viewonly|readonly)$/i;
const USAGE = 'CDK context agentAwsAccess must be "none", "viewonly", "readonly" or a comma-separated list of managed policy ARNs';

function toItems(value: unknown): string[] {
  if (value === undefined || value === null) {
    return [];
  }
  if (typeof value === 'string') {
    return toItems(value.split(','));
  }
  if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
    return value
      .map((v) => v.trim())
      .filter((v) => v.length > 0)
      .map((v) => (PRESET_RE.test(v) ? v.toLowerCase() : v));
  }
  throw new Error(USAGE);
}

export function parseAgentAwsAccess(value: unknown): AgentAwsAccess {
  const items = [...new Set(toItems(value))];
  if (items.length === 0) {
    return { mode: 'none', policyArns: [], label: 'none' };
  }
  if (items.length === 1) {
    const preset = items[0];
    if (preset === 'none' || preset === 'viewonly' || preset === 'readonly') {
      return { mode: preset, policyArns: [], label: preset };
    }
  }
  const malformed = items.filter((arn) => !POLICY_ARN_RE.test(arn));
  if (malformed.length > 0) {
    throw new Error(`${USAGE}; got: ${malformed.join(', ')}`);
  }
  const notReadOnly = items.filter((arn) => AWS_MANAGED_RE.test(arn) && !READ_ONLY_AWS_POLICY_RE.test(arn));
  if (notReadOnly.length > 0) {
    throw new Error(
      `agentAwsAccess must not grant write access to tasks: ${notReadOnly.join(', ')}. ` +
        'Only read-only AWS managed policies are accepted (name ending in ReadOnlyAccess, ReadOnly or ' +
        'ViewOnlyAccess, or SecurityAudit); otherwise use a customer managed policy scoped to what tasks need to read.',
    );
  }
  if (items.length > MAX_CUSTOM_POLICIES) {
    throw new Error(`agentAwsAccess accepts at most ${MAX_CUSTOM_POLICIES} managed policy ARNs, got ${items.length}`);
  }
  const label = items.join(',');
  if (label.length > MAX_LABEL_LENGTH) {
    throw new Error(`agentAwsAccess is too long (${label.length} characters, at most ${MAX_LABEL_LENGTH})`);
  }
  return { mode: 'custom', policyArns: items, label };
}
