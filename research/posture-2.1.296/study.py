#!/usr/bin/env python3
"""Controlled experiments; never edit the installed adapter or contact a real model."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import difflib
import hashlib
import json
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True, help='New directory for synthetic fixtures and reports')
parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime', help='Installed 1.7.2 runtime for Claude 2.1.296')
parser.add_argument('--suite', choices=('all', 'core', 'boundaries'), default='all')
args = parser.parse_args()
out = args.output.resolve()
if any(c.isspace() for c in str(out)) or any(c.isspace() for c in str(HERE)):
    parser.error('The pinned Bun preload parser requires paths without whitespace')
test = REPO / 'tests/privacy-dataflow.py'
original = test.read_text()
if hashlib.sha256(original.encode()).hexdigest() != '4f2f0622adc6a443773c9b90a8917b84acfbe0b936904826b5b4c529038605bd':
    raise SystemExit('Base fixture changed; review this version-bound study before running')
root = args.root.resolve()
runtime = json.loads((root / 'runtime.json').read_text())
adapter = json.loads((root.parent / 'adapter.json').read_text())
if (runtime['version'] != '2.1.296' or adapter['version'] != '1.7.2'
        or runtime['original_sha256'] != 'b0bbc936a82930933d7d5645081050d5f827a946cf6abc6575383b777a0feec0'):
    raise SystemExit('This study requires the reviewed Claude 2.1.296 / adapter 1.7.2')
with (root / 'claude').open('rb') as elf:
    if hashlib.file_digest(elf, 'sha256').hexdigest() != runtime['patched_sha256']:
        raise SystemExit('Installed runtime digest mismatch')
out.mkdir(mode=0o700, parents=True, exist_ok=False)
source = original
changes = [
    ('             request_overrides=False, gateway_hints_off=False):',
     '             request_overrides=False, gateway_hints_off=False, research_mode="observe", origin=True):'),
    ('    if collector_check:\n        subprocess.run(["git", "-C", str(workspace), "config", "remote.origin.url", "https://git.fixture.invalid/group/project.git"], check=True)',
     '    if origin:\n        subprocess.run(["git", "-C", str(workspace), "config", "remote.origin.url", "https://git.fixture.invalid/group/project.git"], check=True)'),
    ('                "posture_fields": sorted(set(re.findall(',
     '                "posture_git_fixture_present": \'jy="git.fixture.invalid/group/project"\' in headers.get("x-claude-code-wiggly-dove", ""),\n'
     '                "posture_without_origin_sha256": hashlib.sha256(re.sub(r\'(?:^|,\\s*)jy="(?:[^"\\\\]|\\\\.)*"\', "", headers.get("x-claude-code-wiggly-dove", "")).encode()).hexdigest(),\n'
     '                "posture_fields": sorted(set(re.findall('),
    ('    env["CLAUDE_TERMUX_ROOT"] = str(root)',
     '    env["CLAUDE_TERMUX_ROOT"] = str(root)\n'
     '    env.update(POSTURE_RESEARCH_FIXTURE="posture-local-fixture-v1", POSTURE_RESEARCH_MODE=research_mode, POSTURE_RESEARCH_REPORT=str(directory / "posture.json"))\n'
     '    env["BUN_OPTIONS"] += " --preload " + ' + repr(str(HERE / 'probe.mjs'))),
    ('    if posture_count != (3 if posture else 0):',
     '    if posture_count != (3 + state["empty_streams"] if posture else 0):'),
]
changes += [
    ('             request_overrides=False, gateway_hints_off=False, research_mode="observe", origin=True):',
     '             request_overrides=False, gateway_hints_off=False, research_mode="observe", origin=True, debug=False, custom=False, getter=False):'),
    ('    if gateway_hints_off:',
     '    if getter:\n        env["POSTURE_RESEARCH_SHARED_GETTER"] = "1"\n'
     '    if custom:\n        env["ANTHROPIC_CUSTOM_HEADERS"] = \'x-claude-code-wiggly-dove: v=1, jy="git.fixture.invalid/group/project", hint="preserve,fixture"\'\n'
     '    if gateway_hints_off:'),
    ('    try:\n        result = subprocess.run(command,',
     '    if debug:\n        command.extend(["--debug-file", str(directory / "debug.log")])\n'
     '    try:\n        result = subprocess.run(command,'),
    ('                "posture_fields": sorted(set(re.findall(',
     '                "custom_posture_sentinel": \'hint="preserve,fixture"\' in headers.get("x-claude-code-wiggly-dove", ""),\n'
     '                "posture_fields": sorted(set(re.findall('),
]
for old, new in changes:
    if source.count(old) != 1:
        raise SystemExit('Research fixture patch no longer matches exactly once')
    source = source.replace(old, new)
(out / 'fixture.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True), source.splitlines(True), fromfile='tests/privacy-dataflow.py', tofile='research-fixture.py')))
(out / 'research-fixture.py').write_text(source)
namespace = {'__file__': str(test), '__name__': 'posture_research_fixture'}
exec(compile(source, str(out / 'research-fixture.py'), 'exec'), namespace)
helper = out / 'tls-cert'
subprocess.run(['clang', '-O2', str(REPO / 'tests/tls-cert.c'), '-lcrypto', '-o', str(helper)], check=True)
cert, key = out / 'cert.pem', out / 'key.pem'
hosts = ('api.anthropic.com', 'api-staging.anthropic.com', 'statsig.anthropic.com', 'claude.ai', 'http-intake.logs.us5.datadoghq.com', 'browser-intake-us5-datadoghq.com')
subprocess.run([str(helper), str(key), str(cert), ','.join('DNS:' + h for h in hosts)], check=True)
key.chmod(0o600)
core_cases = [
    ('original', 'standard', 'observe', True, True, False, False, False, False, False, False),
    ('protected-baseline', 'optional-off', 'observe', True, True, False, False, False, False, False, False),
    ('scoped-producer', 'optional-off', 'producer', True, True, False, False, False, False, False, False),
    ('constructor-filter', 'optional-off', 'constructor', True, True, False, False, False, False, False, False),
    ('producer-feature-off', 'optional-off', 'producer', False, True, False, False, False, False, False, False),
    ('producer-no-origin', 'optional-off', 'producer', True, False, False, False, False, False, False, False),
    ('producer-once', 'optional-off', 'producer', True, True, True, False, False, False, False, False),
    ('producer-fallback', 'optional-off', 'producer', True, True, False, True, False, False, False, False),
    ('producer-hints-off', 'optional-off', 'producer', True, True, False, False, True, False, False, False),
]
boundary_cases = [
    ('debug-observe', 'optional-off', 'observe', True, True, False, False, False, True, False, False),
    ('debug-producer', 'optional-off', 'producer', True, True, False, False, False, True, False, False),
    ('debug-constructor', 'optional-off', 'constructor', True, True, False, False, False, True, False, False),
    ('custom-observe', 'optional-off', 'observe', True, True, False, False, False, False, True, False),
    ('custom-producer', 'optional-off', 'producer', True, True, False, False, False, False, True, False),
    ('custom-constructor', 'optional-off', 'constructor', True, True, False, False, False, False, True, False),
    ('getter-observe', 'optional-off', 'observe', True, True, False, False, False, False, False, True),
    ('getter-producer', 'optional-off', 'producer', True, True, False, False, False, False, False, True),
    ('getter-constructor', 'optional-off', 'constructor', True, True, False, False, False, False, False, True),
    ('official-no-origin', 'optional-off', 'observe', True, False, False, False, False, False, False, False),
]
selected = core_cases if args.suite == 'core' else boundary_cases if args.suite == 'boundaries' else core_cases + boundary_cases
cases = {}
started_at = datetime.now(timezone.utc).isoformat()
for name, mode, experiment, feature, origin, once, fallback, hints_off, debug, custom, getter in selected:
    result = namespace['run_case'](root, out / name, cert, key, mode, feature,
                                  request_check=True, query_once=once, stream_failure=fallback,
                                  gateway_hints_off=hints_off, research_mode=experiment, origin=origin, debug=debug, custom=custom, getter=getter)
    observed = json.loads((out / name / 'posture.json').read_text()) if (out / name / 'posture.json').exists() else {}
    requests = [r for r in result['requests'] if r['path'] == '/v1/messages']
    expected_origin = feature and origin and (experiment == 'observe' or (custom and experiment == 'producer'))
    checks = {'fixture_passed': result['passed'], 'model_requests_observed': bool(requests),
              'expected_origin_field': all(r['posture_git_fixture_present'] == expected_origin for r in requests),
              'outside_lookup_preserved': observed.get('controlRemotePresent') == origin,
              'async_scope_restored': observed.get('scopedStateRestored') is True,
              'factory_observed': observed.get('clientCalls', 0) > 0}
    if feature:
        checks['scoped_lookup_exercised'] = observed.get('scopedRemoteCalls', 0) > 0
    if experiment == 'producer':
        checks['source_lookup_stopped'] = observed.get('originalScopedRemoteCalls') == 0
    details = {}
    if custom:
        checks['custom_sentinel_retained'] = all(r['custom_posture_sentinel'] for r in requests)
        details['custom_origin_control_gap'] = experiment == 'producer' and all(r['posture_git_fixture_present'] for r in requests)
    if getter:
        values = observed.get('sharedGetterOriginPresent', [])
        checks['synthetic_shared_getter_exercised'] = len(values) == 3
        checks['expected_scope_counterexample'] = all(v == (experiment != 'producer') for v in values)
        details['shared_getter_regression_demonstrated'] = experiment == 'producer' and not any(values)
    if debug:
        log = out / name / 'debug.log'
        lines = [line for line in log.read_text().splitlines() if 'x-claude-code-wiggly-dove:' in line] if log.exists() else []
        checks['debug_header_path_exercised'] = bool(lines)
        details['debug_header_lines'] = len(lines)
        details['origin_in_header_debug_log'] = any('git.fixture.invalid/group/project' in line for line in lines)
        checks['expected_debug_origin'] = details['origin_in_header_debug_log'] == (experiment != 'producer')
    cases[name] = {'setup': {'privacy_mode': mode, 'research_mode': experiment, 'feature': feature,
                            'origin': origin, 'complete_response': once, 'stream_failure': fallback,
                            'gateway_hints_off': hints_off, 'debug_file': debug,
                            'custom_header': custom, 'synthetic_parameter_getter': getter},
                   'boundary_observations': details, 'passed': all(checks.values()), 'checks': checks, 'observer': observed,
                   'requests': requests, 'model_requests': len(requests), 'fixture_failures': result['failures'],
                   'tools_completed': result['completed'], 'event_batches': result['event_batches']}
    print(json.dumps({'case': name, 'passed': cases[name]['passed'], 'checks': checks, 'lookup_counts': {
        k: observed.get(k) for k in ('scopedRemoteCalls', 'originalScopedRemoteCalls', 'stoppedRemoteCalls')},
        'fixture_failures': result['failures']}), flush=True)
    if not cases[name]['passed']:
        break
comparisons = {}
for reference, candidates in (
        ('protected-baseline', ('scoped-producer', 'constructor-filter')),
        ('debug-observe', ('debug-producer', 'debug-constructor'))):
    if reference not in cases:
        continue
    baseline = cases[reference]['requests']
    for name in candidates:
        altered = cases.get(name, {}).get('requests', [])
        comparisons[name] = bool(baseline) and len(baseline) == len(altered) and all(
            a['posture_without_origin_sha256'] == b['posture_without_origin_sha256']
            for a, b in zip(baseline, altered))
report = {'schema': 'posture-control-study-v2', 'suite': args.suite, 'claude': runtime['version'], 'original_sha256': runtime['original_sha256'],
          'started_at_utc': started_at, 'finished_at_utc': datetime.now(timezone.utc).isoformat(),
          'runtime_adapter': adapter['version'],
          'base_fixture_sha256': hashlib.sha256(original.encode()).hexdigest(),
          'research_fixture_sha256': hashlib.sha256(source.encode()).hexdigest(),
          'probe_sha256': hashlib.sha256((HERE / 'probe.mjs').read_bytes()).hexdigest(),
          'study_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'cases': cases, 'scenario_count': len(cases), 'model_request_count': sum(c['model_requests'] for c in cases.values()),
          'other_posture_fields_unchanged': comparisons,
          'passed': len(cases) == len(selected) and all(r['passed'] for r in cases.values()) and all(comparisons.values()),
          'production_modified': False, 'passed_means': 'Observed expected controls and expected candidate limitations, including the scoped-producer getter regression and custom-header gap.',
          'limitations': ['Synthetic local receivers do not validate server acceptance or semantic requirements.',
                          'The synthetic parameter getter demonstrates eU scope overreach; no claim that a known production caller uses it.',
                          'Actual --debug-file output retains the synthetic origin with constructor filtering.',
                          'Custom posture header overrides bypass the producer experiment; constructor recognizes only the canonical name.']}
(out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
raise SystemExit(0 if report['passed'] else 1)
