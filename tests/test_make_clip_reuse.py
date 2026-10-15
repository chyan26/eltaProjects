import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAKE_CLIP = REPO / "sport_hightlight" / "make_clip.py"

EVENTS = [  # (inning, half, event_type, seconds)
    ("2", "bottom", "Strikeout", "3.0"),
    ("2", "bottom", "Home Run", "5.0"),
    ("2", "bottom", "Walk", "7.0"),
]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class MakeClipReuseTest(unittest.TestCase):
    """Re-running with other filters into the same folder reuses clips instead of cutting them again."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hb_reuse_"))
        cls.video = cls.tmp / "game.mp4"
        cls.other_video = cls.tmp / "other.mp4"
        for path, freq in ((cls.video, 440), (cls.other_video, 660)):
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=10",
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=10",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
            ], check=True)
        cls.gt = cls.tmp / "gt.csv"
        with cls.gt.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["game_pk", "inning", "half", "event_type", "description",
                                              "is_scoring_play", "predicted_video_seconds", "is_official_highlight"])
            w.writeheader()
            for inning, half, event, secs in EVENTS:
                w.writerow({"game_pk": "1", "inning": inning, "half": half, "event_type": event, "description": event,
                            "is_scoring_play": "False", "predicted_video_seconds": secs, "is_official_highlight": "False"})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.out = self.tmp / f"out_{self._testMethodName}"
        shutil.rmtree(self.out, ignore_errors=True)

    def run_clip(self, *extra, video=None):
        args = [sys.executable, str(MAKE_CLIP), "--ground-truth", str(self.gt), "--video", str(video or self.video),
                "--out-dir", str(self.out), "--pre-seconds", "1", "--post-seconds", "1", "--preset", "ultrafast",
                "--progress-json", *extra]
        p = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(l) for l in p.stderr.splitlines() if l.startswith("{")]
        return p.stdout, events

    def manifest(self):
        with (self.out / "manifest.csv").open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def clip(self, name):
        return next(self.out.rglob(name))

    def test_second_filter_reuses_and_manifest_keeps_earlier_rows(self):
        self.run_clip("--event-types", "Strikeout")
        k = self.clip("event_0001_2bottom_Strikeout.mp4")
        k_mtime = k.stat().st_mtime_ns
        time.sleep(0.05)

        out, events = self.run_clip("--event-types", "Strikeout", "Home Run")
        self.assertEqual(k.stat().st_mtime_ns, k_mtime, "existing clip was cut again")
        done = [e for e in events if e["event"] == "clip_done"]
        self.assertEqual(len(done), 2)
        self.assertEqual(sorted(e.get("reused", False) for e in done), [False, True])
        self.assertIn("沿用 1", out)

        _, _ = self.run_clip("--event-types", "Walk")
        rows = self.manifest()
        self.assertEqual(sorted(r["event_type"] for r in rows), ["Home Run", "Strikeout", "Walk"])
        self.assertEqual(len({r["clip_path"] for r in rows}), 3, "manifest has duplicate rows")
        self.assertEqual(len(list(self.out.rglob("*.mp4"))), 3, "duplicate clip files")

    def test_same_filters_again_cuts_nothing(self):
        self.run_clip()
        mtimes = {p: p.stat().st_mtime_ns for p in self.out.rglob("*.mp4")}
        out, events = self.run_clip()
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.out.rglob("*.mp4")}, mtimes)
        self.assertTrue(all(e.get("reused") for e in events if e["event"] == "clip_done"))
        self.assertIn("新增 0", out)

    def test_changed_timing_recuts_only_that_clip(self):
        self.run_clip("--event-types", "Strikeout")
        k = self.clip("event_0001_2bottom_Strikeout.mp4")
        k_mtime = k.stat().st_mtime_ns
        time.sleep(0.05)
        args = [sys.executable, str(MAKE_CLIP), "--ground-truth", str(self.gt), "--video", str(self.video),
                "--out-dir", str(self.out), "--pre-seconds", "2", "--post-seconds", "1", "--preset", "ultrafast",
                "--event-types", "Strikeout"]
        subprocess.run(args, check=True, capture_output=True)
        self.assertNotEqual(k.stat().st_mtime_ns, k_mtime, "clip with new timing was not re-cut")
        row = self.manifest()[0]
        self.assertEqual(row["clip_start_seconds"], "1.000")
        self.assertEqual(len(self.manifest()), 1)

    def test_other_source_video_recuts(self):
        self.run_clip("--event-types", "Strikeout")
        k = self.clip("event_0001_2bottom_Strikeout.mp4")
        k_mtime = k.stat().st_mtime_ns
        time.sleep(0.05)
        self.run_clip("--event-types", "Strikeout", video=self.other_video)
        self.assertNotEqual(k.stat().st_mtime_ns, k_mtime, "clip from another video was reused")
        self.assertEqual(self.manifest()[0]["source_video"], str(self.other_video))

    def test_missing_file_with_manifest_row_is_cut_again(self):
        self.run_clip("--event-types", "Strikeout")
        k = self.clip("event_0001_2bottom_Strikeout.mp4")
        os.remove(k)
        self.run_clip("--event-types", "Strikeout")
        self.assertTrue(k.exists())


if __name__ == "__main__":
    unittest.main()
