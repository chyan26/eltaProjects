"""Local HTTP server for the Highlight Builder page.

    python -m highlight_builder.server            # http://localhost:8787
    python -m highlight_builder.server --demo     # force demo mode

A browser page can't start Python by itself, so this small server sits in between:
it serves the page and turns each API call into a ``python -m highlight_builder ...``
subprocess, reading JSON from stdout and progress JSON Lines from stderr.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import REPO_ROOT
from .editor import RenderManager, editor_cli, map_errors
from .process import group_kwargs, stop_group, utf8_env

WEB_ROOT = REPO_ROOT / "web"
# Folders whose videos the page may play (clips and source videos). Nothing else is served.
MEDIA_DIRS = ("sport_hightlight/dataset", "video", "out")
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
API = "/api/v1"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TERMINAL = {"succeeded", "failed", "cancelled"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
                 ".yaml": "text/yaml; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon"}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_job_id() -> str:
    ms = int(time.time() * 1000)
    head = "".join(CROCKFORD[(ms >> (5 * i)) & 31] for i in reversed(range(10)))
    tail = "".join(secrets.choice(CROCKFORD) for _ in range(16))
    return f"job_{head}{tail}"


def cli(args: List[str], stdin: Optional[str] = None, timeout: float = 60) -> Tuple[int, Dict[str, Any]]:
    """Run ``python -m highlight_builder <args>`` and parse its stdout JSON."""
    proc = subprocess.run([sys.executable, "-m", "highlight_builder", *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", cwd=REPO_ROOT, timeout=timeout, env=utf8_env())
    try:
        return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 2, {"ok": False, "error": {"code": 50001, "message": "The backend command failed.", "details": proc.stderr[-2000:]}}


class JobRecord:
    def __init__(self, job_id: str, request: Dict[str, Any], demo: bool):
        self.id = job_id
        self.request = request
        self.demo = demo
        self.status = "queued"
        self.created_at = now()
        self.finished_at: Optional[str] = None
        self.steps = [{"id": s["id"], "status": "waiting", "started_at": None, "finished_at": None, "progress": None}
                      for s in request["steps"]]
        self.error: Optional[Dict[str, Any]] = None
        self.outputs: Dict[str, Any] = {"clips": [], "manifest": None}
        self.proc: Optional[subprocess.Popen] = None
        self.version = 0
        self.cond = threading.Condition()

    def step(self, sid: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.steps if s["id"] == sid), None)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "request_id": self.request["request_id"], "status": self.status,
                "created_at": self.created_at, "finished_at": self.finished_at,
                "display": self.request.get("display") or {}, "steps": self.steps, "error": self.error,
                "outputs": self.outputs, "demo": self.demo}

    def touch(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def apply_event(self, ev: Dict[str, Any]) -> None:
        kind, step = ev.get("event"), self.step(ev.get("step_id", ""))
        if kind == "step_start" and step:
            self.status = "running"
            step.update(status="running", started_at=now())
        elif kind == "progress" and step:
            step["progress"] = {"done": ev.get("done", 0), "total": ev.get("total", 0)}
        elif kind == "step_done" and step:
            step.update(status="succeeded", finished_at=now())
        elif kind == "failed" and step:
            step.update(status="failed", finished_at=now())
        self.touch()

    def finish(self, status: str, result: Optional[Dict[str, Any]]) -> None:
        self.status = status
        self.finished_at = now()
        if result:
            self.error = result.get("error")
            if result.get("outputs"):
                self.outputs = result["outputs"]
        for s in self.steps:
            if s["status"] == "waiting":
                s["status"] = "cancelled" if status == "cancelled" else "skipped"
            elif s["status"] == "running":
                s["status"] = "cancelled" if status == "cancelled" else "failed"
                s["finished_at"] = self.finished_at
        if status == "cancelled" and not self.error:
            done = sum((s.get("progress") or {}).get("done", 0) for s in self.steps if s["id"] and self._tool(s["id"]) == "make_clip")
            self.error = None
            self.outputs.setdefault("cancelled_after_clips", done)
        self.touch()

    def _tool(self, sid: str) -> Optional[str]:
        return next((s["tool"] for s in self.request["steps"] if s["id"] == sid), None)


class JobManager:
    """mode is "auto", "demo" or "real". Auto re-checks the machine before every run,
    so installing ffmpeg or pulling the videos takes effect without a restart."""

    def __init__(self, demo: Optional[bool] = None, demo_delay: float = 1.0, mode: Optional[str] = None):
        self.mode = mode or ("demo" if demo else "real" if demo is False else "auto")
        self.demo_delay = demo_delay
        self.jobs: Dict[str, JobRecord] = {}
        self.by_request: Dict[str, str] = {}
        self.lock = threading.Lock()

    @property
    def demo(self) -> bool:
        return self.demo_status()[0]

    def demo_status(self) -> Tuple[bool, List[str]]:
        if self.mode == "demo":
            return True, ["--demo was given"]
        if self.mode == "real":
            return False, []
        return auto_demo()

    def create(self, request: Dict[str, Any]) -> Tuple[bool, JobRecord]:
        with self.lock:
            existing = self.by_request.get(request["request_id"])
            if existing:
                return False, self.jobs[existing]
            job = JobRecord(new_job_id(), request, self.demo)
            self.jobs[job.id] = job
            self.by_request[request["request_id"]] = job.id
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return True, job

    def _run(self, job: JobRecord) -> None:
        args = [sys.executable, "-m", "highlight_builder", "run", "-"]
        if job.demo:
            args += ["--demo", "--demo-delay", str(self.demo_delay)]
        job.proc = subprocess.Popen(args, cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                    env=utf8_env(), **group_kwargs())
        job.proc.stdin.write(json.dumps(job.request))
        job.proc.stdin.close()
        for line in job.proc.stderr:
            line = line.strip()
            if line.startswith("{"):
                try:
                    job.apply_event(json.loads(line))
                except ValueError:
                    pass
        stdout = job.proc.stdout.read()
        code = job.proc.wait()
        job.proc.stdout.close()
        job.proc.stderr.close()
        if job.status == "cancelled":
            return
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {"ok": False, "error": {"code": 50001, "message": "The run stopped unexpectedly.",
                                             "step_id": None, "details": None, "finished_clips": 0}}
        job.finish("succeeded" if code == 0 and result.get("ok") else "failed", result)

    def cancel(self, job: JobRecord) -> None:
        if job.status in TERMINAL:
            return
        if job.proc:
            stop_group(job.proc)
        job.finish("cancelled", None)


GAME_CACHE_SECONDS = 600


class Handler(BaseHTTPRequestHandler):
    server_version = "HighlightBuilder/1"
    manager: JobManager
    renders: "RenderManager"
    game_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/events" not in str(args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ------------------------------------------------------------ helpers
    def send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, code: int, message: str, errors: Optional[Dict[str, Any]] = None) -> None:
        body: Dict[str, Any] = {"code": code, "message": message}
        if errors is not None:
            body["errors"] = errors
        self.send_json(status, body)

    def read_body(self) -> Optional[str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return None
        return self.rfile.read(length).decode("utf-8") if length else ""

    def job_or_404(self, job_id: str) -> Optional[JobRecord]:
        job = self.manager.jobs.get(job_id)
        if job is None:
            self.error(404, 10001, "Unknown job")
        return job

    def serve_media(self, rel: str, head: bool = False) -> None:
        """Stream a clip with HTTP Range support so the player can seek."""
        from .job import is_project_path

        rel = unquote(rel)
        target = (REPO_ROOT / rel).resolve() if is_project_path(rel) else None
        allowed = target is not None and target.suffix.lower() in MEDIA_TYPES and any(
            (REPO_ROOT / d).resolve() in target.parents for d in MEDIA_DIRS)
        if not allowed:
            return self.error(403, 50036, "Only videos inside sport_hightlight/dataset or video can be played.")
        if not target.is_file():
            return self.error(404, 40401, "This clip file doesn't exist yet.")
        if target.stat().st_size < 1024:
            return self.error(404, 40401, "This file is a Git LFS placeholder, not a video.")
        size = target.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = __import__("re").match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", MEDIA_TYPES[target.suffix.lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if head:
            return
        try:
            with target.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/media/"):
            return self.serve_media(path[len("/media/"):], head=True)
        self.send_error(405)

    def serve_static(self, rel: str) -> None:
        target = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve() or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # local tool: always serve the latest page code
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        parts = path.split("/")
        if path == "/":
            return self.serve_static("index.html")
        if path == "/app":
            return self.serve_static("app.html")
        if path.startswith("/static/"):
            return self.serve_static(path[len("/static/"):])
        if path.startswith("/media/"):
            return self.serve_media(path[len("/media/"):])
        if path == "/docs/highlight-builder.openapi.yaml":
            data = (REPO_ROOT / "docs/api/highlight-builder.openapi.yaml").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == f"{API}/environment":
            code, res = cli(["env"])
            env = res.get("environment", {})
            env["demo"], env["demo_reasons"] = self.manager.demo_status()
            return self.send_json(200, env)
        if path == f"{API}/videos":
            code, res = cli(["videos"])
            return self.send_json(200, res.get("videos", []))
        if len(parts) == 5 and path.startswith(f"{API}/games/"):
            if not parts[4].isdigit():
                return self.error(400, 50035, "Invalid Form Body", {"game_pk": {"_errors": [{"code": 50035, "message": "Must be a whole number."}]}})
            cached = self.game_cache.get(parts[4])
            if cached and time.time() - cached[0] < GAME_CACHE_SECONDS:
                return self.send_json(200, cached[1])
            code, res = cli(["game", parts[4]] + (["--offline"] if os.environ.get("HIGHLIGHT_BUILDER_OFFLINE") else []))
            if res.get("ok"):
                self.game_cache[parts[4]] = (time.time(), res["game"])
                return self.send_json(200, res["game"])
            return self.error(404, 40402, res.get("error", {}).get("message", "Game not found"))
        if path == f"{API}/jobs":
            q = parse_qs(url.query)
            limit = max(1, min(100, int((q.get("limit") or ["25"])[0])))
            jobs = sorted(self.manager.jobs.values(), key=lambda j: j.id, reverse=True)
            before = (q.get("before") or [None])[0]
            if before:
                jobs = [j for j in jobs if j.id < before]
            return self.send_json(200, [j.to_json() for j in jobs[:limit]])
        if len(parts) == 5 and path.startswith(f"{API}/jobs/"):
            job = self.job_or_404(parts[4])
            return job and self.send_json(200, job.to_json())
        if len(parts) == 6 and parts[5] == "events" and path.startswith(f"{API}/jobs/"):
            job = self.job_or_404(parts[4])
            return job and self.stream(job)
        if len(parts) in (5, 6) and path.startswith(f"{API}/renders/"):
            render = self.renders.renders.get(parts[4])
            if render is None:
                return self.error(404, 10002, "Unknown render")
            if len(parts) == 6 and parts[5] == "events":
                return self.stream(render)
            if len(parts) == 5:
                return self.send_json(200, render.to_json())
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if path in (f"{API}/jobs", f"{API}/jobs/preview"):
            body = self.read_body()
            if body is None:
                return self.error(413, 50035, "Request too large")
            try:
                request = json.loads(body or "null")
            except ValueError:
                return self.error(400, 50035, "Invalid Form Body", {"_errors": [{"code": 50035, "message": "Not valid JSON."}]})
            code, res = cli(["commands", "-"], stdin=json.dumps(request))
            if not res.get("ok"):
                err = res.get("error", {})
                return self.error(400, err.get("code", 50035), err.get("message", "Invalid Form Body"), err.get("errors"))
            if path.endswith("/preview"):
                return self.send_json(200, {"commands": res["commands"], "demo": self.manager.demo})
            _, job = self.manager.create(request)
            return self.send_json(200, job.to_json())
        if path in (f"{API}/edit/clips", f"{API}/edit/validate", f"{API}/renders"):
            body = self.read_body()
            if body is None:
                return self.error(413, 50035, "Request too large")
            try:
                data = json.loads(body or "null")
            except ValueError:
                return self.error(400, 50035, "Invalid Form Body", {"_errors": [{"code": 50035, "message": "Not valid JSON."}]})
            if path.endswith("/edit/clips"):
                manifests = data.get("manifests") if isinstance(data, dict) else None
                if not isinstance(manifests, list) or not manifests or not all(isinstance(m, str) and m for m in manifests):
                    return self.error(400, 50035, "Invalid Form Body", {"manifests": {"_errors": [{"code": 50035, "message": "Must be a list of manifest paths."}]}})
                code, res = editor_cli(["list", "--manifest", *manifests])
            elif not self.output_allowed(data):
                return self.error(400, 50035, "Invalid Form Body", {"output": {"_errors": [{"code": 50036, "message": "The final video must be saved under out/ and end in .mp4."}]}})
            elif path.endswith("/edit/validate"):
                code, res = editor_cli(["validate", "-"], stdin=json.dumps(data))
            else:
                # Validate first so bad projects fail fast with field-level errors.
                code, res = editor_cli(["validate", "-"], stdin=json.dumps(data))
                if res.get("ok"):
                    return self.send_json(200, self.renders.create(data).to_json())
            if res.get("ok"):
                return self.send_json(200, res)
            status, err = map_errors(res)
            return self.send_json(status, err)
        if len(parts) == 6 and parts[5] == "cancel" and path.startswith(f"{API}/renders/"):
            render = self.renders.renders.get(parts[4])
            if render is None:
                return self.error(404, 10002, "Unknown render")
            self.renders.cancel(render)
            return self.send_json(200, render.to_json())
        if len(parts) == 6 and parts[5] == "cancel" and path.startswith(f"{API}/jobs/"):
            job = self.job_or_404(parts[4])
            if job:
                self.manager.cancel(job)
                self.send_json(200, job.to_json())
            return
        self.send_error(404)

    @staticmethod
    def output_allowed(project: Any) -> bool:
        """Renders may only write under out/, so a project can never overwrite source videos or clips."""
        from .job import is_project_path

        output = project.get("output") if isinstance(project, dict) else None
        if not is_project_path(output) or not output.lower().endswith(".mp4"):
            return False
        target = (REPO_ROOT / output).resolve()
        return (REPO_ROOT / "out").resolve() in target.parents

    def stream(self, job: JobRecord) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True  # the stream ends after JOB_DONE
        seen = -1
        try:
            while True:
                with job.cond:
                    if job.version == seen and job.status not in TERMINAL:
                        job.cond.wait(timeout=15)
                    version = job.version
                if version == seen and job.status not in TERMINAL:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                seen = version
                done = job.status in TERMINAL
                event = "JOB_DONE" if done else "JOB_UPDATE"
                payload = json.dumps(job.to_json(), ensure_ascii=False)
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if done:
                    return
        except (BrokenPipeError, ConnectionResetError):
            return


def auto_demo() -> Tuple[bool, List[str]]:
    reasons = []
    from .game import list_videos
    import shutil

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        reasons.append("ffmpeg isn't installed")
    if not any(not v["lfs_placeholder"] for v in list_videos()):
        reasons.append("the videos are Git LFS placeholders (run git lfs pull)")
    return bool(reasons), reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m highlight_builder.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="always use demo mode")
    mode.add_argument("--real", action="store_true", help="always run the real tools")
    parser.add_argument("--demo-delay", type=float, default=1.0)
    args = parser.parse_args(argv)

    mode = "demo" if args.demo else "real" if args.real else "auto"
    Handler.manager = JobManager(mode=mode, demo_delay=args.demo_delay)
    Handler.renders = RenderManager(now)
    demo, reasons = Handler.manager.demo_status()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"Highlight Builder running at http://localhost:{args.port}")
    print(f"  tool: http://localhost:{args.port}/app")
    print("  mode: " + (f"DEMO ({'; '.join(reasons)})" if demo else "real") + (" · re-checked before each run" if mode == "auto" else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
