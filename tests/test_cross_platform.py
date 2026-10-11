"""Behaviour that differs between POSIX and Windows: output encoding and stopping child processes."""

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from highlight_builder import process, runner
from tests.test_job import REPO


class Utf8OutputTest(unittest.TestCase):
    """Windows pipes default to the ANSI code page; PYTHONIOENCODING simulates that on any OS."""

    def test_backend_json_survives_a_windows_code_page(self):
        env = {**os.environ, "PYTHONIOENCODING": "cp950"}  # Traditional Chinese Windows can't encode "é"
        p = subprocess.run([sys.executable, "-m", "highlight_builder", "game", "823734", "--offline"],
                           capture_output=True, cwd=REPO, env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        game = json.loads(p.stdout.decode("utf-8"))["game"]
        self.assertIn("Héctor Rodríguez", {player["full_name"] for player in game["players"]})

    def test_tool_scripts_print_utf8(self):
        lines = []
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):  # English Windows can't encode Chinese
            runner.run_process([sys.executable, "-c", "print('擷取 Héctor')"], "s", lines.append)
        self.assertEqual(lines, ["擷取 Héctor"])


class StopGroupTest(unittest.TestCase):
    def start_sleeper(self):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **process.group_kwargs())

    def test_stops_a_running_child(self):
        proc = self.start_sleeper()
        try:
            started = time.monotonic()
            process.stop_group(proc)
            proc.wait(timeout=10)
            self.assertLess(time.monotonic() - started, 10)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_finished_child_is_left_alone(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], **process.group_kwargs())
        proc.wait(timeout=10)
        with mock.patch.object(process.os, "killpg", create=True) as killpg, \
                mock.patch.object(process.subprocess, "run") as run:
            process.stop_group(proc)
        killpg.assert_not_called()
        run.assert_not_called()


@mock.patch.object(process, "WINDOWS", True)
class WindowsBranchTest(unittest.TestCase):
    """The Windows-only calls, checked with mocks so they also run on macOS and Linux."""

    def fake_proc(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        return proc

    def test_new_process_group(self):
        with mock.patch.object(process.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True):
            self.assertEqual(process.group_kwargs(), {"creationflags": 0x200})

    def test_forced_stop_kills_the_tree(self):
        with mock.patch.object(process.subprocess, "run") as run, mock.patch.object(process.os, "kill") as kill:
            process.stop_group(self.fake_proc())
        kill.assert_not_called()
        self.assertEqual(run.call_args[0][0], ["taskkill", "/PID", "4321", "/T", "/F"])

    def test_graceful_stop_sends_ctrl_break(self):
        with mock.patch.object(process.signal, "CTRL_BREAK_EVENT", 1, create=True), \
                mock.patch.object(process.os, "kill") as kill, mock.patch.object(process.subprocess, "run") as run:
            process.stop_group(self.fake_proc(), graceful=True)
        kill.assert_called_once_with(4321, 1)
        run.assert_not_called()

    def test_graceful_stop_without_console_falls_back_to_taskkill(self):
        with mock.patch.object(process.signal, "CTRL_BREAK_EVENT", 1, create=True), \
                mock.patch.object(process.os, "kill", side_effect=OSError), \
                mock.patch.object(process.subprocess, "run") as run:
            process.stop_group(self.fake_proc(), graceful=True)
        self.assertEqual(run.call_args[0][0][0], "taskkill")


if __name__ == "__main__":
    unittest.main()
