"""Run a validated job, one step after another, as subprocesses without a shell.

Progress goes to ``emit`` as dicts (the CLI writes them to stderr as JSON Lines):

    {"event": "step_start", "step_id": ..., "index": 1, "total": 3}
    {"event": "progress", "step_id": ..., "done": 4, "total": 9}
    {"event": "step_done", "step_id": ...}
    {"event": "done"}

The return value is the final result the CLI prints on stdout.
Demo mode walks through the same steps using an existing ground-truth CSV and makes no files.
"""

import argparse
import csv
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import REPO_ROOT
from .job import Job, build_argv, mask_output_path
from .process import utf8_env

Emit = Callable[[Dict[str, Any]], None]

# Job error codes (components/schemas/JobErrorCodes)
VIDEO_NOT_FOUND = 40401
GAME_NOT_FOUND = 40402
NO_PLAYS_MATCHED = 42201
TOOL_FAILED = 50001
FFMPEG_MISSING = 50002

EXIT_OK, EXIT_INPUT, EXIT_TOOL = 0, 1, 2
INPUT_ERRORS = {VIDEO_NOT_FOUND, GAME_NOT_FOUND, NO_PLAYS_MATCHED}


def load_make_clip():
    spec = importlib.util.spec_from_file_location("make_clip", REPO_ROOT / "sport_hightlight" / "make_clip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StepFailed(Exception):
    def __init__(self, code: int, message: str, step_id: str, details: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.step_id, self.details = code, message, step_id, details


def python_executable() -> str:
    """Python used for the tool scripts: $HIGHLIGHT_BUILDER_PYTHON, else the project's .venv, else this one."""
    if os.environ.get("HIGHLIGHT_BUILDER_PYTHON"):
        return os.environ["HIGHLIGHT_BUILDER_PYTHON"]
    for candidate in (REPO_ROOT / ".venv" / "bin" / "python", REPO_ROOT / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def tools_python_display() -> str:
    return "python"


# --------------------------------------------------------------------------- outputs

def read_manifest(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [clip_from_row(r) for r in rows]


def clip_from_row(r: Dict[str, str], path: Optional[str] = None, duration: Optional[float] = None) -> Dict[str, Any]:
    try:
        inning = int(r.get("inning") or 0)
    except ValueError:
        inning = 0
    if duration is None:
        try:
            duration = float(r.get("clip_duration_seconds") or 0)
        except ValueError:
            duration = 0.0
    return {
        "path": path or r.get("clip_path", ""),
        "event_type": r.get("event_type", ""),
        "inning": inning,
        "half": r.get("half", ""),
        "batter": r.get("batter") or None,
        "description": r.get("description", ""),
        "duration_seconds": round(duration, 3),
        "official_highlight": str(r.get("is_official_highlight", "")).strip().lower() in ("true", "1", "yes"),
        "official_highlight_title": r.get("official_highlight_title", ""),
        "masked_path": None,
    }


# --------------------------------------------------------------------------- real run

def check_prerequisites(job: Job) -> None:
    for step in job.steps:
        video = step.get("args", {}).get("video")
        if video and not (REPO_ROOT / video).is_file():
            raise StepFailed(VIDEO_NOT_FOUND, f"The video file can't be found at {video}.", step["id"])
        if video and (REPO_ROOT / video).stat().st_size < 1024:
            raise StepFailed(
                VIDEO_NOT_FOUND,
                f"{video} is a Git LFS placeholder, not the video. Run git lfs pull first.",
                step["id"],
            )
    needs_ffmpeg = any(s["tool"] in ("make_clip", "mask_flags") or s.get("args", {}).get("audio_calibrate") or s.get("args", {}).get("auto_anchor") for s in job.steps)
    if needs_ffmpeg and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        first = next(s["id"] for s in job.steps)
        raise StepFailed(FFMPEG_MISSING, "ffmpeg or ffprobe isn't installed on this computer.", first)


def run_process(argv: List[str], step_id: str, on_line: Callable[[str], None]) -> None:
    tail: deque = deque(maxlen=20)
    try:
        proc = subprocess.Popen(
            argv, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=utf8_env(),
        )
    except FileNotFoundError as exc:
        raise StepFailed(TOOL_FAILED, f"Couldn't start {argv[0]}.", step_id, str(exc)) from exc
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail.append(line)
        on_line(line)
    code = proc.wait()
    if code != 0:
        details = "\n".join(tail)
        if step_id and re.search(r"\b404\b", details) and "statsapi" in details.lower():
            raise StepFailed(GAME_NOT_FOUND, "The MLB API has no game with this number.", step_id, details)
        raise StepFailed(TOOL_FAILED, f"Step {step_id} stopped with an error.", step_id, details)


def run_real(job: Job, emit: Emit) -> Dict[str, Any]:
    import json

    check_prerequisites(job)
    py = python_executable()
    outputs: Dict[str, Any] = {"clips": [], "manifest": None}
    clips_by_step: Dict[str, List[Dict[str, Any]]] = {}
    total_steps = len(job.steps)
    for index, step in enumerate(job.steps, start=1):
        sid = step["id"]
        emit({"event": "step_start", "step_id": sid, "index": index, "total": total_steps})
        if step["tool"] == "make_clip":
            seen_total = {"n": None}

            def on_line(line: str, sid=sid, seen_total=seen_total) -> None:
                if not line.startswith("{"):
                    return
                try:
                    ev = json.loads(line)
                except ValueError:
                    return
                if ev.get("event") == "clips_start":
                    seen_total["n"] = ev.get("total", 0)
                    emit({"event": "progress", "step_id": sid, "done": 0, "total": seen_total["n"]})
                elif ev.get("event") in ("clip_done", "clip_skipped"):
                    emit({"event": "progress", "step_id": sid, "done": ev["index"], "total": ev["total"]})

            run_process(build_argv(step, py, ["--progress-json"]), sid, on_line)
            if seen_total["n"] == 0:
                raise StepFailed(NO_PLAYS_MATCHED, "Your filters matched no plays.", sid)
            manifest = REPO_ROOT / step["args"]["out_dir"] / "manifest.csv"
            clips = read_manifest(manifest)
            clips_by_step[sid] = clips
            outputs["clips"] = clips
            outputs["manifest"] = manifest.relative_to(REPO_ROOT).as_posix()
        elif step["tool"] == "mask_flags":
            source = clips_by_step.get(step["for_each"]["clips_from"], [])
            suffix = step["for_each"].get("output_suffix", "_masked")
            for n, clip in enumerate(source, start=1):
                out = mask_output_path(clip["path"], suffix)
                argv = build_argv(step, py)
                argv[2:2] = [clip["path"], out]
                run_process(argv, sid, lambda line: None)
                clip["masked_path"] = out
                emit({"event": "progress", "step_id": sid, "done": n, "total": len(source)})
        else:
            out = step.get("args", {}).get("out")
            if out:  # mlb_highlight_groundtruth.py doesn't create the folder for --out
                (REPO_ROOT / out).parent.mkdir(parents=True, exist_ok=True)
            run_process(build_argv(step, py), sid, lambda line: None)
        emit({"event": "step_done", "step_id": sid})
    return outputs


# --------------------------------------------------------------------------- demo run

NAME_RE = re.compile(r"^(.+?) (?:grounds|lines|strikes|walks|singles|flies|homers|pops|doubles|triples|grounded|hits|called|reaches|caught|lays|bunts|is caught|out at|steals|challenged|was)\b")


def demo_source_csv(game_pk: int) -> Path:
    best: Tuple[int, Optional[Path]] = (-1, None)
    for path in sorted((REPO_ROOT / "sport_hightlight" / "csv_data").glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if str(r.get("game_pk")) == str(game_pk)]
        if len(rows) > best[0]:
            best = (len(rows), path)
    if best[1] is None or best[0] <= 0:
        raise StepFailed(GAME_NOT_FOUND, f"Demo mode has no saved play list for game {game_pk}.", "find_plays")
    return best[1]


def with_batters(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Old CSVs have no batter column; demo mode reads the name from the description."""
    out = []
    for r in rows:
        r = dict(r)
        if not r.get("batter"):
            m = NAME_RE.match(r.get("description", ""))
            r["batter"] = m.group(1) if m else ""
        r.setdefault("pitcher", "")
        out.append(r)
    return out


def make_clip_namespace(args: Dict[str, Any]) -> argparse.Namespace:
    innings = args.get("innings")
    return argparse.Namespace(
        event_types=args.get("event_types"), batting_team=args.get("batting_team"),
        pitching_team=args.get("pitching_team"), batters=args.get("batters"),
        pitchers=args.get("pitchers"), innings=(innings["from"], innings["to"]) if innings else None,
        scoring_only=args.get("scoring_only"), official_only=args.get("official_only"),
    )


def run_demo(job: Job, emit: Emit, delay: float = 1.0) -> Dict[str, Any]:
    mc = load_make_clip()
    outputs: Dict[str, Any] = {"clips": [], "manifest": None}
    rows: List[Dict[str, str]] = []
    clips_by_step: Dict[str, List[Dict[str, Any]]] = {}
    total_steps = len(job.steps)
    for index, step in enumerate(job.steps, start=1):
        sid, args = step["id"], step.get("args", {})
        emit({"event": "step_start", "step_id": sid, "index": index, "total": total_steps})
        if step["tool"] == "mlb_highlight_groundtruth":
            source = demo_source_csv(args["game_pk"])
            with source.open(encoding="utf-8-sig", newline="") as f:
                rows = with_batters(list(csv.DictReader(f)))
            time.sleep(1.2 * delay)
        elif step["tool"] == "make_clip":
            if not rows:
                gt = REPO_ROOT / args["ground_truth"]
                if gt.is_file():
                    with gt.open(encoding="utf-8-sig", newline="") as f:
                        rows = with_batters(list(csv.DictReader(f)))
            selected = mc.filter_rows(rows, make_clip_namespace(args))
            if not selected:
                raise StepFailed(NO_PLAYS_MATCHED, "Your filters matched no plays.", sid)
            pre, post = args.get("pre_seconds", 8.0), args.get("post_seconds", 12.0)
            clips = []
            emit({"event": "progress", "step_id": sid, "done": 0, "total": len(selected)})
            for n, (row_number, row) in enumerate(selected, start=1):
                label = "positive" if mc.parse_bool(row.get("is_official_highlight", "")) else "hard_negative"
                name = f"event_{row_number:04d}_{mc.safe_name(row.get('inning', ''))}{mc.safe_name(row.get('half', ''))}_{mc.safe_name(row.get('event_type', 'event'))}.mp4"
                path = f"{args['out_dir'].rstrip('/')}/{label}/{name}"
                clips.append(clip_from_row(row, path=path, duration=float(pre) + float(post)))
                time.sleep(0.35 * delay)
                emit({"event": "progress", "step_id": sid, "done": n, "total": len(selected)})
            clips_by_step[sid] = clips
            outputs["clips"] = clips
            outputs["manifest"] = f"{args['out_dir'].rstrip('/')}/manifest.csv"
        elif step["tool"] == "mask_flags":
            source_clips = clips_by_step.get(step["for_each"]["clips_from"], [])
            suffix = step["for_each"].get("output_suffix", "_masked")
            for n, clip in enumerate(source_clips, start=1):
                clip["masked_path"] = mask_output_path(clip["path"], suffix)
                time.sleep(0.2 * delay)
                emit({"event": "progress", "step_id": sid, "done": n, "total": len(source_clips)})
        emit({"event": "step_done", "step_id": sid})
    return outputs


# --------------------------------------------------------------------------- entry

def run_job(job: Job, emit: Emit, demo: bool = False, demo_delay: float = 1.0) -> Tuple[int, Dict[str, Any]]:
    """Returns (exit code, stdout result)."""
    current = {"step": None, "clips": 0}

    def tracking_emit(ev: Dict[str, Any]) -> None:
        if ev.get("event") == "step_start":
            current["step"] = ev["step_id"]
        if ev.get("event") == "progress" and job.step(ev["step_id"]) and job.step(ev["step_id"])["tool"] == "make_clip":
            current["clips"] = ev["done"]
        emit(ev)

    try:
        outputs = run_demo(job, tracking_emit, demo_delay) if demo else run_real(job, tracking_emit)
    except StepFailed as exc:
        emit({"event": "failed", "step_id": exc.step_id})
        return (EXIT_INPUT if exc.code in INPUT_ERRORS else EXIT_TOOL), {
            "ok": False,
            "demo": demo,
            "error": {
                "code": exc.code, "message": exc.message, "step_id": exc.step_id,
                "details": exc.details, "finished_clips": current["clips"],
            },
        }
    emit({"event": "done"})
    return EXIT_OK, {"ok": True, "demo": demo, "outputs": outputs}
