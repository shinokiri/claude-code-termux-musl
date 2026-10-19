// Recheck cache-diagnosis settings before queries. Forward every argument and
// the original return value unchanged; upstream owns tool timing and its gate.
export function withRequestPreparation(original, prepare) {
  return function (...args) {
    prepare();
    return Reflect.apply(original, this, args);
  };
}

// The reviewed environment appliers are synchronous, including their writes;
// their asynchronous certificate refresh does not write request settings. Clean
// before control returns to a suspended query, including after partial failure.
export function withSettingsCleanup(original, clean) {
  return function (...args) {
    try { return Reflect.apply(original, this, args); }
    finally { clean(); }
  };
}

// Settings can reload while a query is suspended. Reapply this after each
// environment application as well as at the existing request entry points,
// using the same JSON parser as the reviewed request builder.
// Only cache diagnostics and their beta are removed. Tool timing, including
// explicit custom headers, follows the original program's behavior.
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
