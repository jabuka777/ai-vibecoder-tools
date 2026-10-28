"""Real, synthetic process trees; no Zoom, model, credentials or network."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process-tree integration evidence')
class ProcessTreeTests(unittest.TestCase):
    def parent_death_case(self, *, detached):
        source = (
            'import subprocess,sys,time; '
            f'sys.path.insert(0,{str(runtime.HERE)!r}); import runtime; '
            'p=runtime.start_process([sys.executable,"-c",'
            '\'import time; print("ready",flush=True); time.sleep(60)\'], '
            f'survive_parent={detached!r},stdout=subprocess.PIPE,text=True); '
            'assert p.stdout.readline().strip()=="ready"; print(p.pid,flush=True); time.sleep(60)'
        )
        parent = subprocess.Popen([sys.executable, '-c', source],
                                  stdout=subprocess.PIPE, text=True)
        child = None
        birth = None
        try:
            child = int(parent.stdout.readline())
            birth = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[19]
            parent.kill()
            parent.wait(timeout=3)
            if detached:
                time.sleep(.15)
                state = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[0]
                self.assertNotEqual(state, 'Z')
            else:
                self.assert_stopped(child)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            parent.stdout.close()
            if child is not None:
                try:
                    current = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()
                    if current[19] == birth and current[0] != 'Z':
                        os.kill(child, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass

    def test_owned_child_dies_when_parent_is_killed(self):
        self.parent_death_case(detached=False)

    def test_explicitly_detached_worker_survives_parent_death(self):
        self.parent_death_case(detached=True)

    def assert_stopped(self, pid):
        # Grandchildren are reaped by the OS's reaper, not this Python process.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state == 'Z':
                return
            time.sleep(.02)
        self.fail(f'Synthetic descendant {pid} is still running')

    def tree(self, *, nested_group=False, parent_exits=False, parent_ignores_term=True):
        grandchild = ('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                      'print("ready",flush=True); time.sleep(60)')
        command = (
            'import signal,subprocess,sys,time; '
            f'signal.signal(signal.SIGTERM,signal.SIG_IGN if {parent_ignores_term!r} else signal.SIG_DFL); '
            f'p=subprocess.Popen([sys.executable,"-c",{grandchild!r}], '
            f'start_new_session={nested_group!r}, stdout=subprocess.PIPE, text=True); '
            'p.stdout.readline(); print(p.pid,flush=True); '
            + ('sys.exit(0)' if parent_exits else 'time.sleep(60)')
        )
        process = runtime.start_process([sys.executable, '-c', command],
                                        stdout=subprocess.PIPE, text=True)
        descendant = int(process.stdout.readline())
        self.addCleanup(process.stdout.close)
        self.addCleanup(self.cleanup, process, descendant, nested_group)
        return process, descendant

    @staticmethod
    def cleanup(process, descendant, nested_group):
        # Fixed synthetic handles created above; no session-file PID is trusted.
        try:
            runtime.stop_process(process, timeout=.05)
        finally:
            if nested_group:
                try:
                    os.killpg(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_kills_stubborn_grandchild_without_touching_unrelated_worker(self):
        other = runtime.start_process([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(runtime.stop_process, other, .05)
        process, descendant = self.tree()
        runtime.stop_process(process, timeout=.05)
        self.assertIsNotNone(process.returncode)
        self.assert_stopped(descendant)
        self.assertIsNone(other.poll())

    def test_kills_descendant_that_created_its_own_process_group(self):
        process, descendant = self.tree(nested_group=True)
        self.assertNotEqual(os.getpgid(descendant), process.pid)
        runtime.stop_process(process, timeout=.05)
        self.assert_stopped(descendant)

    def test_stubborn_grandchild_is_killed_after_parent_exits_on_term(self):
        process, descendant = self.tree(parent_ignores_term=False)
        runtime.stop_process(process, timeout=.3)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        self.assert_stopped(descendant)

    def test_reaped_parent_cannot_authorize_signalling_unknown_processes(self):
        process, descendant = self.tree(parent_exits=True)
        process.wait(timeout=3)
        try:
            with patch.object(runtime.os, 'killpg') as kill:
                runtime.stop_process(process, timeout=.05)
            kill.assert_not_called()
        finally:
            # The test has a live handle-independent descendant identity; product
            # cleanup deliberately does not infer one from an already reaped PID.
            os.kill(descendant, signal.SIGKILL)
        self.assert_stopped(descendant)

    def test_timeout_cleans_descendant_and_preserves_captured_output(self):
        command = (
            'import subprocess,sys,time; '
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"], '
            'start_new_session=True); print(p.pid,flush=True); time.sleep(60)'
        )
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            runtime.run_process([sys.executable, '-c', command], timeout=.3,
                                capture_output=True, text=True)
        descendant = int(caught.exception.output.strip())
        self.assert_stopped(descendant)

    def test_recycled_identity_cannot_authorize_signalling(self):
        process, _ = self.tree()
        fake_table = {process.pid: (os.getpid(), process.pid, 'different-birth-identity')}
        with patch.object(runtime, '_process_table', return_value=fake_table), \
                patch.object(runtime.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'identity_changed'):
                runtime.stop_process(process, timeout=.05)
        kill.assert_not_called()
        self.assertIsNone(process.poll())

    def test_repeated_stop_never_signals_a_reused_group(self):
        process, descendant = self.tree()
        runtime.stop_process(process, timeout=.05)
        self.assert_stopped(descendant)
        with patch.object(runtime.os, 'killpg') as kill:
            runtime.stop_process(process, timeout=.05)
        kill.assert_not_called()

    def test_run_preserves_input_output_and_nonzero_exit(self):
        result = runtime.run_process([sys.executable, '-c',
            'import sys; print(sys.stdin.read().upper()); print("diagnostic",file=sys.stderr)'],
            input='synthetic', capture_output=True, text=True, check=True, timeout=3)
        self.assertEqual(result.stdout, 'SYNTHETIC\n')
        self.assertEqual(result.stderr, 'diagnostic\n')
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            runtime.run_process([sys.executable, '-c', 'import sys; print("failed"); sys.exit(4)'],
                                capture_output=True, text=True, check=True, timeout=3)
        self.assertEqual(caught.exception.returncode, 4)
        self.assertEqual(caught.exception.output, 'failed\n')


if __name__ == '__main__':
    unittest.main()
