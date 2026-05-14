#!/usr/bin/env python3
"""Debug launcher for MComix.

Enables debug logging, prints the PID, and installs a SIGUSR1 handler
that dumps all live thread names and stack traces to stderr on demand.

Usage:
    ./mcomixstarterdebug.py

When the process hangs after closing the window, open a second terminal and:
    kill -USR1 <pid>

That will print every thread's current stack trace to stderr so you can
see exactly what is blocking exit.
"""

import faulthandler
import os
import signal
import sys
import threading
import traceback

# Enable C-level fault handler (prints tracebacks on SIGSEGV etc.)
faulthandler.enable()

# Force debug log level without requiring the user to type it.
if '-W' not in sys.argv:
    sys.argv.insert(1, 'debug')
    sys.argv.insert(1, '-W')


def _dump_threads(signum=None, frame=None):
    frames = sys._current_frames()
    lines = ['\n=== thread dump ===']
    for t in threading.enumerate():
        lines.append(
            f'\n--- {t.name}  id={t.ident}  daemon={t.daemon}  alive={t.is_alive()} ---'
        )
        f = frames.get(t.ident)
        if f:
            lines.append(''.join(traceback.format_stack(f)).rstrip())
        else:
            lines.append('  (no frame)')
    lines.append('\n=== end thread dump ===\n')
    print('\n'.join(lines), file=sys.stderr, flush=True)


signal.signal(signal.SIGUSR1, _dump_threads)

print(f'[debug] PID {os.getpid()} — dump threads with:  kill -USR1 {os.getpid()}',
      file=sys.stderr, flush=True)

from mcomix.__main__ import main  # noqa: E402

if __name__ == '__main__':
    main()
