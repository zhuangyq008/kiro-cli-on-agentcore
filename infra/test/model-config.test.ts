import { globMatch, parseList, validateModelConfig } from '../lib/model-config';

test('parseList accepts arrays and comma-separated strings from -c', () => {
  expect(parseList(['a', 'b'], 'k')).toEqual(['a', 'b']);
  expect(parseList(' gpt-5.6-sol , auto ,', 'k')).toEqual(['gpt-5.6-sol', 'auto']);
  expect(parseList('claude-sonnet-5.5', 'k')).toEqual(['claude-sonnet-5.5']);
  expect(() => parseList(42, 'k')).toThrow(/k/);
  expect(() => parseList([1], 'k')).toThrow(/k/);
});

test('globMatch supports * and ? and is anchored', () => {
  expect(globMatch('gpt-5.6-sol', 'gpt-*')).toBe(true);
  expect(globMatch('gpt-5.6-sol', '*')).toBe(true);
  expect(globMatch('gpt-5.6-sol', 'gpt-5.6-?ol')).toBe(true);
  expect(globMatch('xgpt-5', 'gpt-*')).toBe(false);
  expect(globMatch('gpt.5', 'gpt-*')).toBe(false);
  expect(globMatch('auto', 'auto')).toBe(true);
  expect(globMatch('autox', 'auto')).toBe(false);
});

test('validateModelConfig accepts the shipped defaults and GPT-only setups', () => {
  expect(() =>
    validateModelConfig(['claude-sonnet-5.5', 'gpt-5.6-terra', 'auto'], ['auto', 'claude-*', 'gpt-*']),
  ).not.toThrow();
  expect(() => validateModelConfig(['gpt-5.6-sol'], ['gpt-*'])).not.toThrow();
  expect(() => validateModelConfig(['deepseek-3.2'], ['*'])).not.toThrow();
});

test.each([
  [['deepseek-3.2'], ['auto', 'gpt-*'], /not permitted/],
  [['-h'], ['*'], /not a valid model ID/],
  [['auto\n'], ['*'], /not a valid model ID/],
  [['auto'], ['auto\n'], /invalid/],
  [['gpt-*'], ['gpt-*'], /not a valid model ID/],
  [[], ['*'], /at least one/],
  [['auto'], [], /allowedModels/],
  [['auto'], ['auto', 'gpt 5'], /invalid/],
  [['auto'], ['[a-z]*'], /invalid/],
])('validateModelConfig rejects %j / %j', (defaults, allowed, error) => {
  expect(() => validateModelConfig(defaults as string[], allowed as string[])).toThrow(error as RegExp);
});
