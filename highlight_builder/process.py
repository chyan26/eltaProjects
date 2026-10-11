"""Child processes that behave the same on POSIX and Windows.

Each job or render runs in its own process group so cancelling it also stops the tool
scripts and ffmpeg it started. POSIX signals the group with os.killpg. Windows has no
os.killpg: a graceful stop sends CTRL_BREAK_EVENT to the group (highlight_editor turns it
into KeyboardInterrupt so its temp folder is removed), and a forced stop ends the whole
tree with taskkill.
"""

import os
import signal
import subprocess
import sys
from typing import Any, Dict

WINDOWS = sys.platform == "win32"


def utf8_env() -> Dict[str, str]:
    """Environment for Python children whose output we parse.

    On Windows a pipe defaults to the ANSI code page (cp950, cp1252, ...), which can't
    encode names like "Héctor" or the Chinese messages the tools print.
    """
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def group_kwargs() -> Dict[str, Any]:
    """Popen arguments that start the child in a new process group."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_group(proc: subprocess.Popen, graceful: bool = False) -> None:
    """Stop proc and everything it started.

    graceful=True lets the child run its cleanup (SIGINT on POSIX, CTRL_BREAK_EVENT on
    Windows); otherwise SIGTERM on POSIX and taskkill /F on Windows.
    """
    if proc.poll() is not None:
        return
    if WINDOWS:
        if graceful:
            try:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                return
            except OSError:
                pass  # no shared console (e.g. started by pythonw); fall back to taskkill
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGINT if graceful else signal.SIGTERM)
    except ProcessLookupError:
        pass
