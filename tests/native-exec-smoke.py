#!/usr/bin/env python3
"""Exercise real bundled Bun process APIs across both libc boundaries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

APP = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=APP / 'build/runtime')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    with tempfile.TemporaryDirectory(prefix='claude-exec-test-') as directory:
        run = Path(directory)
        (run / 'via-env').write_text('#!/usr/bin/env python3\nprint("SCRIPT_OK")\n')
        (run / 'via-env-split').write_text('#!/usr/bin/env -S python3 -I\nprint("SCRIPT_OK")\n')
        (run / 'not-executable').write_text('#!/usr/bin/env python3\nprint("BAD")\n')
        for name in ('via-env', 'via-env-split'):
            (run / name).chmod(0o700)
        (run / 'script-link').symlink_to('via-env')
        subprocess.run(['clang', '-O2', '-Wall', '-Wextra', '-Werror', str(APP / 'tests/native-exec-driver.c'), '-o', str(run / 'exec-driver')], check=True)
        (run / 'libs').mkdir()
        source = run / 'custom.c'
        source.write_text('#include <stdlib.h>\n#ifdef PRELOAD_MARKER\n__attribute__((constructor)) static void init(void) { setenv("CUSTOM_PRELOAD_LOADED", "1", 1); }\n#endif\nint fixture_value(void) { return 73; }\n')
        for name, definitions in [('custom-preload.so', ['-DPRELOAD_MARKER']), ('libs/libexec-env-test.so', [])]:
            subprocess.run(['clang', '-O2', '-fPIC', '-shared', *definitions, str(source), '-o', str(run / name)], check=True)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('ANTHROPIC_', 'CLAUDE_', 'OTEL_')) or key.lower().endswith('_proxy') or key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'BUN_OPTIONS', 'NODE_OPTIONS'):
                env.pop(key)
        env.update(BUN_OPTIONS='--preload ' + str(APP / 'tests/native-exec-probe.mjs'),
                   EXEC_TEST_RUN=str(run), EXEC_TEST_PREFIX=os.environ.get('PREFIX', '/data/data/com.termux/files/usr'),
                   EXEC_TEST_REPORT=str(run / 'report.json'), CLAUDE_CONFIG_DIR=str(run / 'config'),
                   TMPDIR=str(run), CLAUDE_CODE_TMPDIR=str(run))
        result = subprocess.run([str(root / 'claude'), '--version'], env=env, cwd=run,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout[-2000:] + result.stderr[-5000:])
        report = json.loads((run / 'report.json').read_text())
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'process_cases': len(report['cases']), **{k:v for k,v in report.items() if k != 'cases'}}))


if __name__ == '__main__':
    from fixture_runtime import enter
    enter()
    main()
