#!/usr/bin/env python3
"""On-device receiver controls, inheritance, cleanup and initialization failure."""
import json
import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import fixture_runtime as fixture


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="fixture-controls-")
        cls.directory = Path(cls.temporary.name)
        cls.probe = cls.directory / "probe"
        subprocess.run(["clang", "-O2", "-Wall", "-Wextra", "-Werror", str(fixture.HERE / "fixture-guard-probe.c"),
                        "-o", str(cls.probe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_environment_has_no_ambient_credentials_or_hooks(self):
        values = {name: "fixture-canary" for name in ("SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "BASH_ENV", "BUN_OPTIONS",
                   "NODE_OPTIONS", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND", "HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY")}
        with patch.dict(os.environ, values):
            env = fixture.environment(self.directory / "clean")
        self.assertTrue(set(values).isdisjoint(env))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotEqual(env["HOME"], os.environ["HOME"])
        self.assertEqual(env.get("TERMUX_VERSION"), os.environ.get("TERMUX_VERSION"))

    def test_receiver_positive_and_raw_syscall_negative_controls(self):
        for host, protected, denied in (("127.0.0.1", True, False), ("127.0.0.2", False, False), ("127.0.0.2", True, True)):
            with self.subTest(host=host, protected=protected), socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                tcp.bind((host, 0)); tcp.listen(1); tcp.settimeout(.3)
                udp.bind((host, 0)); udp.settimeout(.3)
                command = [str(self.probe), "deny" if denied else "allow", host, str(tcp.getsockname()[1]), str(udp.getsockname()[1])]
                result = (fixture.run if protected else subprocess.run)(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if denied:
                    with self.assertRaises(TimeoutError): tcp.accept()
                    with self.assertRaises(TimeoutError): udp.recvfrom(10)
                    self.assertEqual({row["syscall"] for row in result.fixture["denied"]}, {203, 206, 211, 269})
                else:
                    with tcp.accept()[0] as connection: self.assertEqual(connection.recv(10), b"TCP")
                    self.assertEqual([udp.recvfrom(10)[0] for _ in range(3)], [b"A", b"B", b"C"])

    def test_real_ssh_entry_blocked_before_helper_runs(self):
        helper = self.directory / "ssh"
        # Real OpenSSH is an ELF. A script named ssh is translated by
        # termux-exec to its interpreter before the exec syscall is reached.
        subprocess.run(["clang", "-x", "c", "-", "-o", str(helper)],
                       input='#include <stdio.h>\nint main(void){puts("SSH_HELPER_ENTERED");return 0;}\n', text=True, check=True)
        baseline = subprocess.run([str(helper)], check=True, capture_output=True, text=True)
        self.assertIn("SSH_HELPER_ENTERED", baseline.stdout)
        result = fixture.run([str(helper)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SSH_HELPER_ENTERED", result.stdout)
        self.assertTrue(any(row["reason"] == "real-ssh-and-default-identity-disabled-in-fixture" for row in result.fixture["denied"]))

    def test_detached_descendants_reaped_on_exit_and_timeout(self):
        for linger in (False, True):
            marker = self.directory / ("detached-" + str(linger))
            source = ("import os,signal,time\nfrom pathlib import Path\np=Path(" + repr(str(marker)) + ")\n"
                      "pid=os.fork()\nif pid==0:\n os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);p.write_text(str(os.getpid()));time.sleep(60);os._exit(0)\n"
                      "while not p.exists():time.sleep(.01)\n" + ("time.sleep(60)\n" if linger else ""))
            result = fixture.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=1 if linger else 10)
            self.assertEqual(result.returncode, 124 if linger else 0, result.stderr)
            self.assertTrue(result.fixture["all_descendants_reaped"])
            self.assertGreater(result.fixture["cleanup_signals"], 0)
            self.assertFalse(Path("/proc/" + marker.read_text()).exists())

    def test_inherited_socket_rejected_before_target(self):
        with socket.socket() as network:
            result = fixture.run([sys.executable, "-c", 'print("TARGET_RAN")'], capture_output=True, text=True,
                                 pass_fds=(network.fileno(),))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TARGET_RAN", result.stdout)
        self.assertFalse(result.fixture["ready"])

    def test_broker_cleans_up_when_its_python_owner_is_killed(self):
        marker = self.directory / "owner-death"
        source = ("import os,signal,time\nfrom pathlib import Path\np=Path(" + repr(str(marker)) + ")\n"
                  "if os.fork()==0:\n os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);p.write_text(str(os.getpid()));time.sleep(30);os._exit(0)\n"
                  "time.sleep(30)\n")
        driver = "import sys;sys.path.insert(0," + repr(str(fixture.HERE)) + ");import fixture_runtime as f;f.run(" + repr([sys.executable, "-c", source]) + ")"
        env = fixture.environment(self.directory / "owner-state")
        owner = subprocess.Popen([sys.executable, "-c", driver], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and owner.poll() is None and time.monotonic() < deadline: time.sleep(.05)
            self.assertTrue(marker.exists(), "Guarded descendant did not start")
            child = Path("/proc/" + marker.read_text())
            owner.kill(); owner.wait(timeout=3)
            deadline = time.monotonic() + 8
            while child.exists() and time.monotonic() < deadline: time.sleep(.05)
            self.assertFalse(child.exists(), "Descendant survived its owner")
        finally:
            if owner.poll() is None: owner.terminate()
            owner.communicate(timeout=10)

    def test_forged_marker_cannot_bypass_guard_entry(self):
        with patch.dict(os.environ, {"TERMUX_FIXTURE_BROKER": str(os.getppid())}):
            self.assertFalse(fixture.inside_guard())
            with self.assertRaisesRegex(RuntimeError, "refusing unguarded"):
                fixture.enter()

    def test_research_anchor_waits_for_detached_work(self):
        script = fixture.HERE.parent / "research/egress-2.1.296/trace-study.py"
        spec = importlib.util.spec_from_file_location("trace_study", script)
        trace = importlib.util.module_from_spec(spec); spec.loader.exec_module(trace)
        directory = self.directory / "anchor"
        directory.mkdir()
        # A pass-through driver tests process lifetime only, not syscall tracing.
        tracer = directory / "driver"
        tracer.write_text("#!" + sys.executable + "\nimport os,sys\ni=sys.argv.index('-o')+2\nos.execv(sys.argv[i],sys.argv[i:])\n")
        tracer.chmod(0o700)
        guard = fixture.build_guard(directory)
        marker = directory / "work-completed"
        code = ("import os,time\nfrom pathlib import Path\n"
                "if os.fork()==0:\n os.setsid();time.sleep(.3);Path(" + repr(str(marker)) + ").touch();os._exit(0)\n")
        result = trace.trace_run(tracer, None, directory / "trace", [sys.executable, "-c", code],
                                 env=fixture.environment(directory / "state"), cwd=directory,
                                 poll_hook=marker.exists, guard=guard)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(fixture.summary(directory / "trace/guard.jsonl", guard)["cleanup_signals"], 0)


if __name__ == "__main__":
    unittest.main()
