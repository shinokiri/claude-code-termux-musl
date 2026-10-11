import assert from 'node:assert/strict';
import test from 'node:test';
import { withoutToolTiming, withoutCacheDiagnosis, requestSettingsFilter } from '../libexec/termux-privacy-requests.mjs';

test('timing payload is never read; frozen inputs, lazy options and references survive', () => {
  let reads = 0, counts = 0;
  const context = {}, messages = [], signal = new AbortController().signal;
  const options = Object.freeze({ model: 'fixture', signal,
    get unrelated() { reads++; return 7; },
    get prevToolDurations() { throw Error('optional timing payload read'); } });
  const request = Object.freeze({ messages, options });
  const result = {};
  const wrapped = withoutToolTiming(function (received, extra) {
    assert.equal(this, context); assert.equal(extra, 19);
    assert.equal(received.messages, messages); assert.equal(received.options.signal, signal);
    assert.equal(reads, 0); assert.equal(received.options.unrelated, 7);
    assert.equal(received.options.prevToolDurations, undefined);
    assert.equal(Object.hasOwn({ ...received.options }, 'prevToolDurations'), false);
    return result;
  }, () => counts++);
  assert.equal(wrapped.call(context, request, 19), result);
  assert.equal(counts, 1);
  assert.equal(typeof Object.getOwnPropertyDescriptor(options, 'prevToolDurations').get, 'function');
});

test('inherited timing is masked without dropping other inherited options', () => {
  const prototype = { temperature: 0, get prevToolDurations() { throw Error('inherited payload read'); } };
  const request = { options: Object.assign(Object.create(prototype), { model: 'fixture' }) };
  withoutToolTiming(received => {
    assert.equal(received.options.prevToolDurations, undefined);
    assert.equal(received.options.temperature, 0); assert.equal(received.options.model, 'fixture');
    assert.equal(Object.getPrototypeOf(received.options), prototype);
  }, () => {})(request);
});

test('unaffected input and returned Promise preserve identity', async () => {
  const request = { options: { model: 'fixture' } }, result = Promise.resolve('done');
  const wrapped = withoutToolTiming(received => { assert.equal(received, request); return result; }, () => { throw Error('unexpected change'); });
  assert.equal(wrapped(request), result); assert.equal(await result, 'done');
});

test('async iterator identity, cancellation and cleanup pass through', async () => {
  let cleaned = false;
  const iterator = (async function* () { try { yield 1; yield 2; } finally { cleaned = true; } })();
  const wrapped = withoutToolTiming(() => iterator, () => {});
  const received = wrapped({ options: { prevToolDurations: [] } });
  assert.equal(received, iterator); assert.deepEqual(await received.next(), { value: 1, done: false });
  assert.deepEqual(await received.return('cancelled'), { value: 'cancelled', done: true });
  assert.equal(cleaned, true);
});

test('original synchronous errors propagate unchanged', () => {
  const error = new Error('fixture');
  const wrapped = withoutToolTiming(() => { throw error; }, () => {});
  assert.throws(() => wrapped({ options: { prevToolDurations: [] } }), value => value === error);
});

test('unknown diagnosis descriptor fails closed', () => {
  assert.throws(() => withoutCacheDiagnosis({}, { name: 'cache_diagnosis', header: 'changed' }, () => {}), /Unsupported/);
});

test('custom diagnostics are removed without changing continuation, cache or unrelated headers', () => {
  const beta = 'cache-diagnosis-2026-04-07', counts = [];
  const parsed = { diagnostics: { previous_message_id: 'diagnostic' }, thread: { previous_message_id: 'conversation' },
    cache_control: { type: 'ephemeral' }, betas: ['first', beta, 'last'], anthropic_beta: beta + ',other' };
  const env = { CLAUDE_CODE_EXTRA_BODY: 'fixture',
    ANTHROPIC_CUSTOM_HEADERS: 'Authorization: synthetic test only\n X-Claude-Code-Prev-Tool-Durations : secret\nAnthropic-Beta: first,' + beta + '\nX-Custom:  spaced value  \nignored-line' };
  const clean = requestSettingsFilter(env, text => text === 'fixture' ? parsed : JSON.parse(text), key => counts.push(key));
  clean();
  assert.deepEqual(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY), { thread: parsed.thread, cache_control: parsed.cache_control,
    betas: ['first', 'last'], anthropic_beta: 'other' });
  assert.equal(env.ANTHROPIC_CUSTOM_HEADERS, 'Authorization: synthetic test only\nAnthropic-Beta: first\nX-Custom:  spaced value  \nignored-line');
  assert.equal(parsed.diagnostics.previous_message_id, 'diagnostic'); // Do not mutate the original parser's cache.
  clean(); assert.equal(counts.length, 2);
});

test('settings loaded after startup are filtered again at query entry', () => {
  const env = {}, clean = requestSettingsFilter(env, JSON.parse, () => {});
  clean();
  env.CLAUDE_CODE_EXTRA_BODY = '{"diagnostics":null,"betas":"cache-diagnosis-2026-04-07","temperature":0.25}';
  env.ANTHROPIC_CUSTOM_HEADERS = 'anthropic-beta: cache-diagnosis-2026-04-07';
  const input = { options: { model: 'fixture' } };
  withoutToolTiming(request => {
    assert.equal(request, input); assert.equal(env.CLAUDE_CODE_EXTRA_BODY, '{"temperature":0.25}');
    assert.equal(env.ANTHROPIC_CUSTOM_HEADERS, '');
  }, () => {}, clean)(input);
});

test('unrelated and invalid extra-body settings keep their original bytes', () => {
  for (const raw of [' { "temperature" : 0.5 } ', '["diagnostics"]', 'null', 'invalid']) {
    const env = { CLAUDE_CODE_EXTRA_BODY: raw, ANTHROPIC_CUSTOM_HEADERS: 'X-Fixture: diagnostics\nAuthorization: fixture' };
    const parse = value => { try { return JSON.parse(value); } catch { return null; } };
    requestSettingsFilter(env, parse, () => { throw Error('unexpected change'); })();
    assert.equal(env.CLAUDE_CODE_EXTRA_BODY, raw);
    assert.equal(env.ANTHROPIC_CUSTOM_HEADERS, 'X-Fixture: diagnostics\nAuthorization: fixture');
  }
});
