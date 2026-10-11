import assert from 'node:assert/strict';
import { roleModule } from '../libexec/termux-privacy-profile.mjs';

export async function requestProbe(profile, protectedMode) {
  const api = await roleModule(profile, 'betaState');
  const features = await roleModule(profile, 'betaDefinitions');
  const state = api.create(), diagnosis = features.cacheDiagnosis;
  const controls = [features.contextManagement, features.thread].filter(Boolean);
  assert.ok(controls.length >= 1);
  for (const feature of controls) api.enable(state, feature);
  api.enable(state, diagnosis);
  assert.equal(api.isEnabled(state, diagnosis), !protectedMode);
  assert.equal(state.sent.has(diagnosis), !protectedMode);
  for (const feature of controls) assert.equal(api.isEnabled(state, feature), true);
  state.sent.add(diagnosis); // A state carried into this query from an earlier consumer.
  const clone = api.clone(state);
  assert.equal(api.isEnabled(clone, diagnosis), !protectedMode);
  api.enable(clone, diagnosis);
  assert.equal(clone.sent.has(diagnosis), !protectedMode);
  api.reject(clone, controls[0]); api.enable(clone, controls[0]);
  assert.equal(api.isEnabled(clone, controls[0]), false);
  assert.equal(api.isEnabled(state, controls[0]), true);
  const names = ['CLAUDE_CODE_EXTRA_BODY', 'ANTHROPIC_CUSTOM_HEADERS'];
  const saved = names.map(name => process.env[name]);
  try {
    process.env.CLAUDE_CODE_EXTRA_BODY = JSON.stringify({ diagnostics: { previous_message_id: 'fixture' },
      thread: { previous_message_id: 'continuation' }, temperature: 0.37 });
    process.env.ANTHROPIC_CUSTOM_HEADERS = 'X-Claude-Code-Prev-Tool-Durations: Fixture=17\nAnthropic-Beta: cache-diagnosis-2026-04-07,fixture-beta\nX-Fixture-Control: preserved';
    const custom = (await roleModule(profile, 'requestHeaders')).custom();
    const normalized = Object.fromEntries(Object.entries(custom).map(([key, value]) => [key.toLowerCase(), value]));
    assert.equal(normalized['x-claude-code-prev-tool-durations'], 'Fixture=17');
    assert.equal(normalized['anthropic-beta'].includes('cache-diagnosis-2026-04-07'), !protectedMode);
    assert.ok(normalized['anthropic-beta'].includes('fixture-beta'));
    assert.equal(normalized['x-fixture-control'], 'preserved');
    const extra = JSON.parse(process.env.CLAUDE_CODE_EXTRA_BODY);
    assert.equal(Object.hasOwn(extra, 'diagnostics'), !protectedMode);
    assert.equal(extra.thread.previous_message_id, 'continuation'); assert.equal(extra.temperature, 0.37);
  } finally {
    names.forEach((name, index) => { if (saved[index] === undefined) delete process.env[name]; else process.env[name] = saved[index]; });
  }
  return { passed: true, diagnosisEnabled: !protectedMode, carriedDiagnosisEnabled: !protectedMode,
    independentClone: true, otherFeatures: controls.map(f => f.name), originalRejectionPreserved: true,
    directCustomHeaders: true, customContinuationPreserved: true, customToolTimingPreserved: true };
}
