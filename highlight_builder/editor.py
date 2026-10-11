"""Bridge to highlight_editor (US-8 export).

highlight_editor has no HTTP endpoints; it's a CLI that prints one JSON object on stdout
and, for render, progress as JSON Lines on stderr (highlight_editor/openapi.yaml). This
module runs it as a subprocess and maps its results to the Highlight Builder API.
"""

import json
import secrets
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import REPO_ROOT
from .process import group_kwargs, stop_group, utf8_env

# highlight_editor error code -> (HTTP status, Highlight Builder error code)
ERROR_MAP: Dict[str, Tuple[int, int]] = {
    "invalid_json": (400, 50035),
    "missing_field": (400, 50035),
    "invalid_value": (400, 50035),
    "out_of_range": (400, 50035),
    "invalid_media": (400, 50035),
    "clip_not_found": (404, 40401),
    "manifest_not_found": (404, 40401),
    "output_exists": (409, 40901),
    "ffmpeg_not_found": (500, 50002),
    "ffmpeg_failed": (500, 50001),
    "io_error": (500, 50001),
}
TERMINAL = {"succeeded", "failed", "cancelled"}


def editor_python() -> str:
    # highlight_editor only needs the standard library plus ffmpeg on PATH.
    return sys.executable


def editor_cli(args: List[str], stdin: Optional[str] = None, timeout: float = 120) -> Tuple[int, Dict[str, Any]]:
    """Run ``python -m highlight_editor <args> --base-dir .`` from the repo root."""
    proc = subprocess.run(
        [editor_python(), "-m", "highlight_editor", *args, "--base-dir", "."], input=stdin,
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO_ROOT, timeout=timeout,
        env=utf8_env(),
    )
    try:
        return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 2, {"ok": False, "errors": [{"code": "io_error", "segment_id": None, "field": None,
                                            "message": "highlight_editor didn't return JSON.",
                                            "detail": proc.stderr[-2000:]}]}


def map_errors(result: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """ErrorResult -> (HTTP status, error body). The original errors are kept for the page."""
    errors = result.get("errors") or [{"code": "io_error", "message": "Unknown error"}]
    status, code = ERROR_MAP.get(errors[0].get("code", ""), (500, 50001))
    return status, {"code": code, "message": errors[0].get("message", ""), "editor_errors": errors}


def new_render_id() -> str:
    return "render_" + secrets.token_hex(10)


class RenderRecord:
    """One highlight_editor render. Shape mirrors JobRecord so the SSE stream can reuse it."""

    def __init__(self, render_id: str, project: Dict[str, Any], created_at: str):
        self.id = render_id
        self.project = project
        self.status = "queued"
        self.created_at = created_at
        self.finished_at: Optional[str] = None
        self.phase = "queued"  # queued | segments | concat | done
        self.progress = {"done": 0, "total": len(project.get("segments") or [])}
        self.current_segment: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.proc: Optional[subprocess.Popen] = None
        self.version = 0
        self.cond = threading.Condition()

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "status": self.status, "phase": self.phase, "created_at": self.created_at,
                "finished_at": self.finished_at, "output": self.project.get("output"),
                "progress": self.progress, "current_segment": self.current_segment,
                "result": self.result, "error": self.error}

    def touch(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def apply_event(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("event")
        self.status = "running"
        if kind == "segment_start":
            self.phase = "segments"
            self.current_segment = ev.get("segment_id")
            self.progress = {"done": ev.get("index", 1) - 1, "total": ev.get("total", 0)}
        elif kind == "segment_done":
            self.progress = {"done": ev.get("index", 0), "total": ev.get("total", 0)}
        elif kind == "concat_start":
            self.phase = "concat"
            self.current_segment = None
        elif kind == "done":
            self.phase = "done"
        self.touch()


class RenderManager:
    def __init__(self, now):
        self.now = now
        self.renders: Dict[str, RenderRecord] = {}
        self.lock = threading.Lock()

    def create(self, project: Dict[str, Any]) -> RenderRecord:
        render = RenderRecord(new_render_id(), project, self.now())
        with self.lock:
            self.renders[render.id] = render
        threading.Thread(target=self._run, args=(render,), daemon=True).start()
        return render

    def _run(self, render: RenderRecord) -> None:
        render.proc = subprocess.Popen(
            [editor_python(), "-m", "highlight_editor", "render", "-", "--base-dir", "."],
            cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=utf8_env(), **group_kwargs(),
        )
        render.proc.stdin.write(json.dumps(render.project))
        render.proc.stdin.close()
        for line in render.proc.stderr:
            line = line.strip()
            if line.startswith("{"):
                try:
                    render.apply_event(json.loads(line))
                except ValueError:
                    pass
        stdout = render.proc.stdout.read()
        code = render.proc.wait()
        render.proc.stdout.close()
        render.proc.stderr.close()
        if render.status == "cancelled":
            return
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {"ok": False, "errors": [{"code": "io_error", "message": "The render stopped unexpectedly."}]}
        render.finished_at = self.now()
        if code == 0 and result.get("ok"):
            render.status, render.phase, render.result = "succeeded", "done", result
            render.progress = {"done": render.progress["total"], "total": render.progress["total"]}
        else:
            render.status = "failed"
            render.error = map_errors(result)[1]
        render.touch()

    def cancel(self, render: RenderRecord) -> None:
        if render.status in TERMINAL:
            return
        if render.proc:
            # Graceful (SIGINT / CTRL_BREAK) so highlight_editor's finally blocks run and its temp folder is removed.
            stop_group(render.proc, graceful=True)
        render.status = "cancelled"
        render.finished_at = self.now()
        render.touch()
