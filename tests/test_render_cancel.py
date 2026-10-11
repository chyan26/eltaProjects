import shutil
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from highlight_builder import REPO_ROOT, editor

OUT_DIR = "out/_test_render_cancel"

# Stands in for ``python -m highlight_editor render``: makes its temp folder, reports a segment, then waits.
CHILD = """
import os, sys, time
os.mkdir(os.path.join(sys.argv[1], ".highlight_editor_work"))
print('{"event": "segment_start", "segment_id": "1", "index": 1, "total": 1}', file=sys.stderr, flush=True)
time.sleep(60)
"""


def wait_for(condition, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class TempDirsTest(unittest.TestCase):
    def test_no_output_means_nothing_to_look_at(self):
        self.assertIsNone(editor.render_temp_parent({}))
        self.assertEqual(editor.temp_dirs(None), set())
        self.assertEqual(editor.temp_dirs(REPO_ROOT / OUT_DIR / "missing"), set())


class RenderCancelCleanupTest(unittest.TestCase):
    def setUp(self):
        self.out_made = not (REPO_ROOT / "out").exists()
        self.folder = REPO_ROOT / OUT_DIR
        self.folder.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        if self.out_made:
            try:
                (REPO_ROOT / "out").rmdir()
            except OSError:
                pass

    def test_cancel_removes_the_temp_folder_this_render_left_and_keeps_others(self):
        old = self.folder / ".highlight_editor_old"
        old.mkdir()
        keep = self.folder / "notes"  # not a temp folder
        keep.mkdir()
        real_popen = subprocess.Popen

        def fake_popen(argv, **kwargs):
            return real_popen([sys.executable, "-c", CHILD, str(self.folder)], **kwargs)

        fake_subprocess = types.SimpleNamespace(PIPE=subprocess.PIPE, Popen=fake_popen)
        manager = editor.RenderManager(lambda: "now")
        with mock.patch.object(editor, "subprocess", fake_subprocess):
            render = manager.create({"output": f"{OUT_DIR}/reel.mp4", "segments": [{"id": "1"}]})
            work = self.folder / ".highlight_editor_work"
            self.assertTrue(wait_for(lambda: work.is_dir() and render.phase == "segments"), "child didn't start")
            manager.cancel(render)
            self.assertEqual(render.status, "cancelled")
            self.assertTrue(wait_for(lambda: not work.exists()), "temp folder was left behind")
        self.assertTrue(wait_for(lambda: render.proc.poll() is not None), "child is still running")
        self.assertTrue(old.is_dir())
        self.assertTrue(keep.is_dir())
        self.assertEqual(render.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
