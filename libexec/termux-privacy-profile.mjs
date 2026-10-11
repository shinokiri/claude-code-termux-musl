// Version-specific symbols live in a reviewed profile, not the policy logic.
// This does not infer semantics or approve new versions automatically.
export async function roleModule(profile, key) {
  const module = await import(profile.modules[key].path);
  const bindings = profile.bindings[key] || {};
  return new Proxy(module, { get(target, role) { return target[bindings[role] || role]; } });
}

export function exportOverrides(profile, key, overrides) {
  const bindings = profile.bindings[key] || {};
  return Object.fromEntries(Object.entries(overrides).map(([role, value]) => [bindings[role] || role, value]));
}
