"""Validate a JobCreateRequest and turn its steps into argument lists.

The request never carries shell text. Each step names an allowed tool and its arguments;
this module checks every argument against the tool's table and builds the argv itself.
See components/schemas/JobCreateRequest in docs/api/highlight-builder.openapi.yaml.
"""

import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence

# Error codes shared with the HTTP API (see "Error codes" in the requirements doc).
INVALID_FORM_BODY = 50035
PATH_OUTSIDE_PROJECT = 50036

STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SUFFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class Tool:
    script: str
    args: Dict[str, str]  # argument name -> kind
    required: Sequence[str] = ()


TOOLS: Dict[str, Tool] = {
    "mlb_highlight_groundtruth": Tool(
        script="sport_hightlight/mlb_highlight_groundtruth.py",
        args={
            "game_pk": "positive_int",
            "video": "path",
            "game_offset_seconds": "number",
            "clip_inning": "positive_int",
            "clip_half": "half",
            "anchor_video_seconds": "number",
            "auto_anchor": "bool",
            "audio_calibrate": "bool",
            "audio_search_before": "number",
            "audio_search_after": "number",
            "out": "path",
        },
        required=("game_pk", "video", "out"),
    ),
    "make_clip": Tool(
        script="sport_hightlight/make_clip.py",
        args={
            "ground_truth": "path",
            "video": "path",
            "out_dir": "path",
            "pre_seconds": "seconds",
            "post_seconds": "seconds",
            "overwrite": "bool",
            "event_types": "str_list",
            "batting_team": "side_list",
            "pitching_team": "side_list",
            "batters": "str_list",
            "pitchers": "str_list",
            "innings": "inning_range",
            "scoring_only": "bool",
            "official_only": "bool",
        },
        required=("ground_truth", "video", "out_dir"),
    ),
    "mask_flags": Tool(
        script="target_mask/mask_flags.py",
        args={"mode": "mask_mode", "device": "device"},
    ),
}

KIND_MESSAGES = {
    "positive_int": "Must be a whole number of 1 or more.",
    "number": "Must be a number of 0 or more.",
    "seconds": "Must be a number from 0 to 60.",
    "bool": "Must be true or false.",
    "path": "Must be a path inside the project folder.",
    "half": "Must be top or bottom.",
    "str_list": "Must be a list of names.",
    "side_list": "Must be a list of away or home.",
    "inning_range": "Must be an object with from and to, where from is not after to.",
    "mask_mode": "Must be blur or pixelate.",
    "device": "Must be auto, cuda, mps or cpu.",
}


@dataclass
class FieldError:
    path: List[Any]
    code: int
    message: str


@dataclass
class Job:
    request_id: str
    steps: List[Dict[str, Any]]
    display: Dict[str, Any] = field(default_factory=dict)
    on_failure: str = "stop"
    keep_finished_clips: bool = True

    def step(self, step_id: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.steps if s["id"] == step_id), None)


class JobValidationError(Exception):
    def __init__(self, errors: List[FieldError]):
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors

    def to_response(self) -> Dict[str, Any]:
        """Discord-style "Invalid Form Body": errors mirrors the request shape."""
        tree: Dict[str, Any] = {}
        for err in self.errors:
            node = tree
            for key in err.path:
                node = node.setdefault(str(key), {})
            node.setdefault("_errors", []).append({"code": err.code, "message": err.message})
        return {"code": INVALID_FORM_BODY, "message": "Invalid Form Body", "errors": tree}


def is_project_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512 or "\0" in value:
        return False
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    return ".." not in PurePosixPath(value.replace("\\", "/")).parts


def _check_kind(kind: str, value: Any) -> bool:
    if value is None:
        return True  # null means "leave the option out"
    if kind == "positive_int":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if kind == "seconds":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 60
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "path":
        return is_project_path(value)
    if kind == "half":
        return value in ("top", "bottom")
    if kind == "str_list":
        return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value) and len(set(value)) == len(value)
    if kind == "side_list":
        return isinstance(value, list) and all(v in ("away", "home") for v in value) and len(set(value)) == len(value)
    if kind == "inning_range":
        if not isinstance(value, dict) or set(value) != {"from", "to"}:
            return False
        lo, hi = value["from"], value["to"]
        return all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in (lo, hi)) and lo <= hi
    if kind == "mask_mode":
        return value in ("blur", "pixelate")
    if kind == "device":
        return value in ("auto", "cuda", "mps", "cpu")
    raise ValueError(f"unknown kind {kind}")


def parse_job(data: Any) -> Job:
    """Validate a decoded JobCreateRequest. Raises JobValidationError with every problem found."""
    errors: List[FieldError] = []

    def err(path: List[Any], message: str, code: int = INVALID_FORM_BODY) -> None:
        errors.append(FieldError(path, code, message))

    if not isinstance(data, dict):
        raise JobValidationError([FieldError([], INVALID_FORM_BODY, "The request body must be a JSON object.")])

    allowed_top = {"request_id", "display", "steps", "on_failure", "keep_finished_clips"}
    for key in data:
        if key not in allowed_top:
            err([key], "Unknown field.")

    request_id = data.get("request_id")
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        err(["request_id"], "Must be a UUID.")

    display = data.get("display") or {}
    if not isinstance(display, dict):
        err(["display"], "Must be an object.")
        display = {}

    if data.get("on_failure", "stop") != "stop":
        err(["on_failure"], "Only stop is supported.")
    keep = data.get("keep_finished_clips", True)
    if not isinstance(keep, bool):
        err(["keep_finished_clips"], "Must be true or false.")

    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 3:
        err(["steps"], "Must be a list of 1 to 3 steps.")
        steps = []

    seen: List[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            err(["steps", i], "Must be an object.")
            continue
        for key in step:
            if key not in ("id", "tool", "depends_on", "args", "for_each"):
                err(["steps", i, key], "Unknown field.")
        sid = step.get("id")
        if not isinstance(sid, str) or not STEP_ID_RE.match(sid):
            err(["steps", i, "id"], "Must be lowercase letters, digits or _, starting with a letter.")
        elif sid in seen:
            err(["steps", i, "id"], "Must be unique within the request.")
        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or any(d not in seen for d in deps):
            err(["steps", i, "depends_on"], "Must list earlier step ids only.")
        tool = TOOLS.get(step.get("tool"))
        if tool is None:
            err(["steps", i, "tool"], f"Must be one of: {', '.join(TOOLS)}.")
        else:
            args = step.get("args")
            if not isinstance(args, dict):
                err(["steps", i, "args"], "Must be an object.")
            else:
                for name, value in args.items():
                    kind = tool.args.get(name)
                    if kind is None:
                        err(["steps", i, "args", name], "Unknown argument for this tool.")
                    elif not _check_kind(kind, value):
                        code = PATH_OUTSIDE_PROJECT if kind == "path" and isinstance(value, str) else INVALID_FORM_BODY
                        message = "Path must stay inside the project folder." if code == PATH_OUTSIDE_PROJECT else KIND_MESSAGES[kind]
                        err(["steps", i, "args", name], message, code)
                for name in tool.required:
                    if args.get(name) is None:
                        err(["steps", i, "args", name], "This argument is required.")
            for_each = step.get("for_each")
            if step.get("tool") == "mask_flags":
                if not isinstance(for_each, dict) or for_each.get("clips_from") not in seen:
                    err(["steps", i, "for_each"], "mask_flags needs for_each.clips_from naming an earlier step.")
                elif not SUFFIX_RE.match(str(for_each.get("output_suffix", "_masked"))):
                    err(["steps", i, "for_each", "output_suffix"], "Use 1 to 32 letters, digits, _ or -.")
            elif for_each is not None:
                err(["steps", i, "for_each"], "Only mask_flags supports for_each.")
        if isinstance(sid, str):
            seen.append(sid)

    if errors:
        raise JobValidationError(errors)
    return Job(request_id=str(request_id), steps=steps, display=display, keep_finished_clips=keep)


def option(name: str) -> str:
    return "--" + name.replace("_", "-")


def build_argv(step: Dict[str, Any], python: str = "python", extra: Sequence[str] = ()) -> List[str]:
    """argv for one step (mask_flags positional files are added by the runner)."""
    tool = TOOLS[step["tool"]]
    argv = [python, tool.script]
    for name, value in step.get("args", {}).items():
        if value is None or value is False:
            continue
        if value is True:
            argv.append(option(name))
        elif isinstance(value, list):
            if value:
                argv.append(option(name))
                argv.extend(str(v) for v in value)
        elif isinstance(value, dict):  # inning_range
            argv.extend([option(name), str(value["from"]), str(value["to"])])
        else:
            argv.extend([option(name), str(value)])
    argv.extend(extra)
    return argv


def mask_output_path(clip_path: str, suffix: str) -> str:
    p = PurePosixPath(clip_path)
    return str(p.with_name(p.stem + suffix + p.suffix))


def preview(job: Job) -> List[Dict[str, Any]]:
    """The commands US-6 shows. Built by the same code the runner uses."""
    out = []
    for step in job.steps:
        if step["tool"] == "mask_flags":
            suffix = step["for_each"].get("output_suffix", "_masked")
            argv = build_argv(step)
            argv[2:2] = ["<clip>.mp4", f"<clip>{suffix}.mp4"]
            note = f"Runs once for each clip made by {step['for_each']['clips_from']}."
        else:
            argv = build_argv(step)
            note = None
        out.append({"step_id": step["id"], "tool": step["tool"], "argv": argv, "shell": shlex.join(argv), "note": note})
    return out
