"""Start child processes as their own group and stop the whole tree (POSIX and Windows)."""

import os
import signal
import subprocess
from typing import Any, Dict


def group_kwargs() -> Dict[str, Any]:
    """Popen keyword arguments that put the child in its own process group / session."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_tree(proc: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
    """Stop ``proc`` and everything it started.

    POSIX sends ``sig`` to the process group. Windows has no such signals for a group, and a
    venv ``python.exe`` is only a launcher for the real interpreter, so the tree is killed
    with ``taskkill /T /F``; ``sig`` isn't used there.
    """
    if os.name == "nt":
        # A non-zero exit just means the process was already gone.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
