/**
 * Model configuration: list parsing for CDK context values and synth-time validation.
 *
 * The same rules are enforced by the runtime (runtime/src/kiro_runtime/models.py); checking them
 * here makes a bad setting fail `cdk deploy` instead of crashing the container at start-up.
 */

// Must match MODEL_ID_RE / PATTERN_RE in runtime/src/kiro_runtime/models.py.
const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/;
const PATTERN_RE = /^[A-Za-z0-9*?][A-Za-z0-9._:*?-]{0,99}$/;

/** A context value given as a JSON array (cdk.json) or a comma-separated string (`-c key=a,b`). */
export function parseList(value: unknown, key: string): string[] {
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((v) => v.trim())
      .filter((v) => v.length > 0);
  }
  if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
    return value.map((v) => v.trim()).filter((v) => v.length > 0);
  }
  throw new Error(`CDK context ${key} must be a list of strings or a comma-separated string`);
}

/** Shell-style match with `*` and `?`, anchored and case-sensitive (like Python's fnmatchcase). */
export function globMatch(value: string, pattern: string): boolean {
  const source = pattern
    .split('')
    .map((ch) => (ch === '*' ? '.*' : ch === '?' ? '.' : ch.replace(/[.+^${}()|[\]\\-]/g, '\\$&')))
    .join('');
  return new RegExp(`^${source}$`).test(value);
}

export function validateModelConfig(defaultModels: readonly string[], allowedModels: readonly string[]): void {
  if (allowedModels.length === 0) {
    throw new Error('allowedModels must not be empty');
  }
  const badPatterns = allowedModels.filter((p) => !PATTERN_RE.test(p));
  if (badPatterns.length > 0) {
    throw new Error(`allowedModels has invalid entries: ${badPatterns.join(', ')}`);
  }
  if (defaultModels.length === 0) {
    throw new Error('defaultModel must list at least one model');
  }
  for (const model of defaultModels) {
    if (!MODEL_ID_RE.test(model)) {
      throw new Error(`defaultModel entry "${model}" is not a valid model ID`);
    }
    if (!allowedModels.some((p) => globMatch(model, p))) {
      throw new Error(`defaultModel entry "${model}" is not permitted by allowedModels (${allowedModels.join(', ')})`);
    }
  }
}
