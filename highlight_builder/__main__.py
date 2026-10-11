"""``python -m highlight_builder <command>``

| command | input | stdout |
|---|---|---|
| ``validate <job.json or ->`` | JobCreateRequest | ``{"ok": true}`` or ``{"ok": false, "error": ErrorResponse}`` |
| ``commands <job.json or ->`` | JobCreateRequest | ``{"ok": true, "commands": [...]}`` (US-6) |
| ``run <job.json or -> [--demo]`` | JobCreateRequest | ``{"ok": true, "outputs": ...}`` or ``{"ok": false, "error": ...}`` (US-7) |
| ``game <game_pk> [--offline]`` | | ``{"ok": true, "game": GameResponse}`` (US-3) |
| ``videos`` | | ``{"ok": true, "videos": [...]}`` (US-1) |
| ``env`` | | ``{"ok": true, "environment": ...}`` |

Progress for ``run`` goes to stderr, one JSON object per line. Exit codes: 0 ok, 1 input or
validation error, 2 tool or filesystem error.
"""

import argparse
import json
import sys
from typing import Any, Dict

from .game import GameNotFound, environment, get_game, list_videos
from .job import JobValidationError, parse_job, preview
from .runner import EXIT_INPUT, EXIT_OK, python_executable, run_job


def out(obj: Dict[str, Any], code: int = EXIT_OK) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return code


def read_job(source: str):
    try:
        text = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise JobValidationError([]) from exc
    return parse_job(data)


def configure_stdio() -> None:
    """Windows pipes default to the ANSI code page, not UTF-8; the server reads UTF-8."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv=None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="python -m highlight_builder")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "commands", "run"):
        p = sub.add_parser(name)
        p.add_argument("job", help="job JSON file, or - for stdin")
        if name == "run":
            p.add_argument("--demo", action="store_true", help="walk through the steps without running the tools")
            p.add_argument("--demo-delay", type=float, default=1.0, help="speed factor for demo pauses")
    g = sub.add_parser("game")
    g.add_argument("game_pk", type=int)
    g.add_argument("--offline", action="store_true", help="use saved play lists only")
    sub.add_parser("videos")
    sub.add_parser("env")
    args = parser.parse_args(argv)

    if args.command in ("validate", "commands", "run"):
        try:
            job = read_job(args.job)
        except JobValidationError as exc:
            body = exc.to_response() if exc.errors else {"code": 50035, "message": "The request is not valid JSON.", "errors": {}}
            return out({"ok": False, "error": body}, EXIT_INPUT)
        if args.command == "validate":
            return out({"ok": True})
        if args.command == "commands":
            return out({"ok": True, "commands": preview(job)})

        def emit(event: Dict[str, Any]) -> None:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

        code, result = run_job(job, emit, demo=args.demo, demo_delay=args.demo_delay)
        return out(result, code)

    if args.command == "game":
        try:
            return out({"ok": True, "game": get_game(args.game_pk, offline=args.offline)})
        except GameNotFound as exc:
            return out({"ok": False, "error": {"code": 40402, "message": str(exc)}}, EXIT_INPUT)
    if args.command == "videos":
        return out({"ok": True, "videos": list_videos()})
    if args.command == "env":
        return out({"ok": True, "environment": environment(python_executable())})
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
