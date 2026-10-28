"""Linux launcher: owned workers die if their creating parent disappears.

This runs before exec, rather than using preexec_fn in a threaded Python
process. Deliberately detached MCP recording workers do not use this launcher.
"""
import ctypes
import os
import signal
import sys


def main():
    try:
        expected = int(sys.argv[1])
        command = sys.argv[2:]
        if not sys.platform.startswith('linux') or expected < 1 or not command:
            raise ValueError('Invalid guarded launch')
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                         ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError('Cannot establish parent-death guard')
        # The parent may have died before the kernel guard was installed.
        if os.getppid() != expected:
            return 128 + signal.SIGKILL
        os.execvpe(command[0], command, os.environ)
    except Exception as exc:
        # The command and environment may contain private data.
        print('Child launch failed: ' + type(exc).__name__, file=sys.stderr, flush=True)
        return 127


if __name__ == '__main__':
    raise SystemExit(main())
