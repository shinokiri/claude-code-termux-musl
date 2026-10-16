#!/usr/bin/env python3
"""A real CLI and its detached Bionic child must protect their release from GC."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libexec'))
from termux_storage import collect


def locked(path):
    with path.open() as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    lease = root / 'release.lease'
    with tempfile.TemporaryDirectory(prefix='claude-lease-') as directory:
        run = Path(directory)
        child = run / 'child.py'
        child.write_text('import os,time\nfrom pathlib import Path\np=Path(os.environ["LEASE_TEST_DIR"])\np.joinpath("ready").write_text(str(os.getpid()))\nend=time.monotonic()+20\nwhile not p.joinpath("stop").exists() and time.monotonic()<end: time.sleep(.02)\n')
        probe = run / 'probe.mjs'
        probe.write_text('''
import fs from 'node:fs';
import {spawn, spawnSync} from 'node:child_process';
const run=process.env.LEASE_TEST_DIR;
const lease=fs.statSync(process.env.LEASE_TEST_FILE);
const held=fs.readdirSync('/proc/self/fd').filter(fd=>{
  try {const s=fs.statSync('/proc/self/fd/'+fd);return s.dev===lease.dev&&s.ino===lease.ino;}catch{return false;}
}).length;
if (held !== 1) throw Error('Expected one release lease, found '+held);
if (process.env.LEASE_TEST_CHILD) {console.log(JSON.stringify({held}));process.exit(0);}
for(let i=0;i<8;i++) {
  const result=spawnSync(process.execPath,['--version'],{env:{...process.env,LEASE_TEST_CHILD:'1'},encoding:'utf8'});
  if(result.status!==0||JSON.parse(result.stdout).held!==1) throw Error('Lease failed across exec: '+result.stderr);
}
const child=spawn(process.env.LEASE_TEST_PYTHON,[run+'/child.py'],{env:process.env,detached:true,stdio:'ignore'});
child.unref();
const end=Date.now()+8000;
while(!fs.existsSync(run+'/ready')) {if(Date.now()>end)throw Error('Child not ready');await Bun.sleep(20);}
console.log(JSON.stringify({held,child:child.pid,self_spawns:8}));process.exit(0);
''')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('CLAUDE_', 'ANTHROPIC_', 'OTEL_'))
               and key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'BUN_OPTIONS', 'NODE_OPTIONS')}
        env.update(BUN_OPTIONS='--preload ' + str(probe), CLAUDE_TERMUX_PRIVACY='standard',
                   CLAUDE_CONFIG_DIR=str(run / 'config'), TMPDIR=str(run),
                   LEASE_TEST_DIR=str(run), LEASE_TEST_FILE=str(lease),
                   LEASE_TEST_PYTHON=os.sys.executable)
        child_pid = None
        try:
            result = subprocess.run([str(root / 'claude'), '--version'], env=env, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(result.stdout[-2000:] + result.stderr[-3000:])
            report = json.loads(result.stdout)
            child_pid = report['child']
            assert locked(lease), 'Detached Bionic child lost its lease'
            report['detached_bionic_child_protected'] = True
            # Redirect only the disposable lease-file open to the real inode;
            # flock, collector decisions and directory deletion remain real.
            # Android Python does not expose os.link for a hard-link fixture.
            gc_data = run / 'gc-installation'
            old = gc_data / 'releases/previous'
            (old / 'runtime').mkdir(parents=True)
            (old / 'installation.json').write_text(json.dumps({'data_dir': str(gc_data)}))
            (old / 'runtime/runtime.json').write_text(json.dumps({'root': str(old / 'runtime'), 'release_lease': 1}))
            disposable_lease = old / 'runtime/release.lease'
            disposable_lease.touch()
            (gc_data / 'previous').symlink_to(old)
            original_open = os.open

            def open_lease(path, *args, **kwargs):
                return original_open(lease if path == disposable_lease else path, *args, **kwargs)

            def collect_fixture():
                with (gc_data / 'install.lock').open('a') as install_lock, patch.object(os, 'open', side_effect=open_lease):
                    fcntl.flock(install_lock, fcntl.LOCK_EX)
                    return collect(gc_data, apply=True)

            active_gc = collect_fixture()
            assert not active_gc['removed'] and old.is_dir(), 'GC removed a live previous release'
            report['gc_preserves_live_previous'] = True
            # Constructor must refuse an exclusively locked release, before
            # running the application. This is the collector/launcher race.
            (run / 'stop').touch()
            deadline = time.monotonic() + 5
            while locked(lease) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not locked(lease), 'Lease leaked after descendant exit'
            child_pid = None
            idle_gc = collect_fixture()
            assert idle_gc['removed'] == ['previous'] and not old.exists()
            assert not (gc_data / 'previous').is_symlink()
            report['gc_removes_previous_after_exit'] = True
            with lease.open() as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                refused = subprocess.run([str(root / 'claude'), '--version'], env=env, capture_output=True, timeout=5)
                assert refused.returncode == 78 and b'release lease unavailable' in refused.stderr
            report.update(released_after_exit=True, exclusive_cleanup_blocks_start=True)
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))
        finally:
            (run / 'stop').touch()
            # The test child also has a 20-second lifetime; no broad pkill.
            if child_pid and (run / 'ready').is_file() and (run / 'ready').read_text() == str(child_pid):
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    from fixture_runtime import enter
    enter()
    main()
