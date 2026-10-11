import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from highlight_builder.job import JobValidationError, build_argv, is_project_path, parse_job, preview

REPO = Path(__file__).resolve().parent.parent

REQUEST = {
    "request_id": "6f1c2a9e-1b7d-4c1e-9a52-0d3f7b8e2c41",
    "display": {"summary": "Reds batting", "expected_clip_count": 9},
    "steps": [
        {"id": "find_plays", "tool": "mlb_highlight_groundtruth", "depends_on": [],
         "args": {"game_pk": 823734, "video": "video/game.mp4", "game_offset_seconds": 150.5,
                  "audio_calibrate": True, "out": "sport_hightlight/csv_data/823734_ground_truth.csv"}},
        {"id": "cut_clips", "tool": "make_clip", "depends_on": ["find_plays"],
         "args": {"ground_truth": "sport_hightlight/csv_data/823734_ground_truth.csv", "video": "video/game.mp4",
                  "out_dir": "sport_hightlight/dataset/x", "pre_seconds": 8, "post_seconds": 12, "overwrite": False,
                  "event_types": ["Strikeout", "Home Run"], "batting_team": ["away"], "innings": {"from": 7, "to": 9}}},
        {"id": "blur_flags", "tool": "mask_flags", "depends_on": ["cut_clips"],
         "for_each": {"clips_from": "cut_clips", "output_suffix": "_masked"}, "args": {"mode": "blur"}},
    ],
    "on_failure": "stop",
    "keep_finished_clips": True,
}


def errors_of(request):
    try:
        parse_job(request)
    except JobValidationError as exc:
        return exc
    return None


class ParseJobTest(unittest.TestCase):
    def test_valid_request(self):
        job = parse_job(copy.deepcopy(REQUEST))
        self.assertEqual([s["id"] for s in job.steps], ["find_plays", "cut_clips", "blur_flags"])

    def test_unknown_argument_is_rejected(self):
        bad = copy.deepcopy(REQUEST)
        bad["steps"][0]["args"]["shell"] = "rm -rf /"
        exc = errors_of(bad)
        self.assertIsNotNone(exc)
        body = exc.to_response()
        self.assertEqual(body["code"], 50035)
        self.assertIn("_errors", body["errors"]["steps"]["0"]["args"]["shell"])

    def test_unknown_tool_is_rejected(self):
        bad = copy.deepcopy(REQUEST)
        bad["steps"][0]["tool"] = "bash"
        self.assertIsNotNone(errors_of(bad))

    def test_path_escape_is_rejected_with_its_own_code(self):
        for path in ("../../etc/passwd", "/etc/passwd", "a/../../b", "C:\\x"):
            bad = copy.deepcopy(REQUEST)
            bad["steps"][1]["args"]["out_dir"] = path
            exc = errors_of(bad)
            self.assertIsNotNone(exc, path)
            self.assertEqual(exc.errors[0].code, 50036)

    def test_required_arguments(self):
        bad = copy.deepcopy(REQUEST)
        del bad["steps"][1]["args"]["out_dir"]
        self.assertIsNotNone(errors_of(bad))

    def test_depends_on_must_point_back(self):
        bad = copy.deepcopy(REQUEST)
        bad["steps"][0]["depends_on"] = ["cut_clips"]
        self.assertIsNotNone(errors_of(bad))

    def test_mask_needs_for_each(self):
        bad = copy.deepcopy(REQUEST)
        del bad["steps"][2]["for_each"]
        self.assertIsNotNone(errors_of(bad))

    def test_bad_types(self):
        for step, name, value in ((1, "pre_seconds", 99), (1, "batting_team", ["visitors"]), (0, "game_pk", "x"),
                                  (1, "innings", {"from": 9, "to": 7}), (2, "mode", "smudge")):
            bad = copy.deepcopy(REQUEST)
            bad["steps"][step]["args"][name] = value
            self.assertIsNotNone(errors_of(bad), name)

    def test_mask_device(self):
        for device in ("auto", "cuda", "mps", "cpu"):
            ok = copy.deepcopy(REQUEST)
            ok["steps"][2]["args"]["device"] = device
            self.assertIsNone(errors_of(ok), device)
        bad = copy.deepcopy(REQUEST)
        bad["steps"][2]["args"]["device"] = "tpu"
        self.assertIsNotNone(errors_of(bad))

    def test_project_path(self):
        self.assertTrue(is_project_path("video/a b.mp4"))
        self.assertFalse(is_project_path(""))
        self.assertFalse(is_project_path("x/../../y"))


class ArgvTest(unittest.TestCase):
    def test_build_argv_maps_args_to_options(self):
        job = parse_job(copy.deepcopy(REQUEST))
        argv = build_argv(job.steps[1], "py")
        self.assertEqual(argv[:2], ["py", "sport_hightlight/make_clip.py"])
        joined = " ".join(argv)
        self.assertIn("--event-types Strikeout Home Run", joined)
        self.assertIn("--batting-team away", joined)
        self.assertIn("--innings 7 9", joined)
        self.assertIn("--pre-seconds 8", joined)
        self.assertNotIn("--overwrite", joined)  # false leaves the option out

    def test_true_adds_flag_only(self):
        argv = build_argv(parse_job(copy.deepcopy(REQUEST)).steps[0], "py")
        i = argv.index("--audio-calibrate")
        self.assertTrue(argv[i + 1].startswith("--"))

    def test_preview_quotes_for_the_shell(self):
        cmds = preview(parse_job(copy.deepcopy(REQUEST)))
        self.assertIn("'Home Run'", cmds[1]["shell"])
        self.assertIn("<clip>.mp4", cmds[2]["shell"])


class CliTest(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        p = subprocess.run([sys.executable, "-m", "highlight_builder", *args], input=stdin, capture_output=True,
                           text=True, encoding="utf-8", cwd=REPO)
        return p.returncode, json.loads(p.stdout), p.stderr

    def test_commands(self):
        code, out, _ = self.run_cli("commands", "-", stdin=json.dumps(REQUEST))
        self.assertEqual(code, 0)
        self.assertEqual(len(out["commands"]), 3)

    def test_validation_error_exit_code(self):
        bad = copy.deepcopy(REQUEST)
        bad["steps"][1]["args"]["out_dir"] = "../x"
        code, out, _ = self.run_cli("validate", "-", stdin=json.dumps(bad))
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])

    def test_demo_run_streams_progress_and_returns_clips(self):
        req = copy.deepcopy(REQUEST)
        req["steps"][1]["args"].pop("innings")
        code, out, err = self.run_cli("run", "-", "--demo", "--demo-delay", "0", stdin=json.dumps(req))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["outputs"]["clips"]), 9)
        events = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
        self.assertEqual(events[0]["event"], "step_start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(all(c["masked_path"].endswith("_masked.mp4") for c in out["outputs"]["clips"]))

    def test_demo_run_with_no_matches_fails_with_code(self):
        req = copy.deepcopy(REQUEST)
        req["steps"][1]["args"]["event_types"] = ["Triple Play"]
        code, out, _ = self.run_cli("run", "-", "--demo", "--demo-delay", "0", stdin=json.dumps(req))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], 42201)


if __name__ == "__main__":
    unittest.main()
