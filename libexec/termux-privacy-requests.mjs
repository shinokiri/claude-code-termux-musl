// Keep model inputs and execution timing intact; omit the optional timing
// attachment before the original query builder can read or serialize it.
// Descriptor copies preserve lazy properties without reading their values.
export function withoutToolTiming(original, count, prepare = () => {}) {
  return function (request, ...rest) {
    prepare();
    const options = request.options;
    if (!options || !('prevToolDurations' in options)) return Reflect.apply(original, this, [request, ...rest]);
    const fields = Object.getOwnPropertyDescriptors(options);
    fields.prevToolDurations = { value: undefined, enumerable: false, configurable: true, writable: true };
    const nextOptions = Object.create(Object.getPrototypeOf(options), fields);
    const outer = Object.getOwnPropertyDescriptors(request);
    outer.options = { value: nextOptions, enumerable: true, configurable: true, writable: true };
    count('model-tool-timing-attachment');
    return Reflect.apply(original, this, [Object.create(Object.getPrototypeOf(request), outer), ...rest]);
  };
}

// Settings may load after the preload. Reapply this at telemetry initialization
// and query entry, using the same JSON parser as the reviewed request builder.
// Only the two diagnosed optional attachments and their beta are removed.
export function requestSettingsFilter(env, parse, count) {
  const beta = 'cache-diagnosis-2026-04-07';
  const filterBetas = value => String(value).split(',').map(s => s.trim()).filter(s => s && s !== beta);
  return () => {
    const raw = env.CLAUDE_CODE_EXTRA_BODY;
    if (raw) {
      const parsed = parse(raw, false);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const body = { ...parsed };
        let changed = Object.hasOwn(body, 'diagnostics');
        delete body.diagnostics;
        for (const key of ['betas', 'anthropic_beta']) {
          if (!Object.hasOwn(body, key) || !String(body[key]).split(',').some(s => s.trim() === beta)) continue;
          const kept = filterBetas(body[key]);
          if (!kept.length) delete body[key];
          else body[key] = Array.isArray(body[key]) ? kept : kept.join(',');
          changed = true;
        }
        if (changed) { env.CLAUDE_CODE_EXTRA_BODY = JSON.stringify(body); count('request-extra-diagnostics'); }
      }
    }
    const headers = env.ANTHROPIC_CUSTOM_HEADERS;
    if (headers) {
      let changed = false;
      const kept = [];
      for (const line of headers.split('\n')) {
        const colon = line.indexOf(':');
        const name = colon < 0 ? '' : line.slice(0, colon).trim().toLowerCase();
        if (name === 'x-claude-code-prev-tool-durations') { changed = true; continue; }
        if (name === 'anthropic-beta' && line.slice(colon + 1).split(',').some(s => s.trim() === beta)) {
          const betas = filterBetas(line.slice(colon + 1));
          if (betas.length) kept.push(line.slice(0, colon + 1) + ' ' + betas.join(','));
          changed = true;
        } else kept.push(line);
      }
      if (changed) { env.ANTHROPIC_CUSTOM_HEADERS = kept.join('\n'); count('request-custom-diagnostics'); }
    }
  };
}

// Only the reviewed cache-diagnosis feature is disabled. Other beta features
// retain their original enable/reject semantics, including prompt caching and
// conversation continuation. A previously latched diagnosis stays ineffective.
export function withoutCacheDiagnosis(original, diagnosis, count) {
  if (diagnosis?.name !== 'cache_diagnosis' || diagnosis.header !== 'cache-diagnosis-2026-04-07') {
    throw Error('Unsupported cache diagnosis feature');
  }
  return {
    enable(state, feature, ...rest) {
      if (feature === diagnosis) {
        count('cache-diagnosis-enable'); state.sent.delete(feature); return;
      }
      return Reflect.apply(original.enable, this, [state, feature, ...rest]);
    },
    isEnabled(state, feature, ...rest) {
      if (feature === diagnosis) { count('cache-diagnosis-check'); return false; }
      return Reflect.apply(original.isEnabled, this, [state, feature, ...rest]);
    },
  };
}
