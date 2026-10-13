"""CLI：python -m highlight_editor <list|validate|render>。

stdout 一律是單一 JSON 物件；render 的進度以 JSON Lines 輸出到 stderr。
"""

import argparse
import json
import signal
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

from .catalog import list_clips
from .project import EditorError, error_result, exit_code_for
from .render import render_project, validate_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m highlight_editor",
        description="精華剪輯後台：列出片段、驗證專案、渲染合併影片",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="列出 manifest 中可選的片段")
    list_parser.add_argument("--manifest", nargs="+", required=True, help="manifest.csv 路徑，可給多個")

    validate_parser = commands.add_parser("validate", help="驗證專案 JSON，不編碼")
    validate_parser.add_argument("project", help="專案 JSON 路徑；- 代表從 stdin 讀取")

    render_parser = commands.add_parser("render", help="依專案 JSON 渲染合併影片")
    render_parser.add_argument("project", help="專案 JSON 路徑；- 代表從 stdin 讀取")
    render_parser.add_argument("--overwrite", action="store_true", help="允許覆寫已存在的輸出檔")

    for subparser in (list_parser, validate_parser, render_parser):
        subparser.add_argument(
            "--base-dir", default=".", help="專案內容與 manifest 中相對路徑的基準目錄（預設為目前目錄）",
        )
    return parser


def load_project_json(source: str) -> Tuple[Any, Optional[EditorError]]:
    """讀取專案 JSON（檔案或 stdin），接受 UTF-8 BOM。"""
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            with open(source, encoding="utf-8-sig") as handle:
                text = handle.read()
    except OSError as exc:
        return None, EditorError("invalid_json", f"無法讀取專案檔：{source}（{exc.strerror or exc}）")
    except UnicodeDecodeError:
        return None, EditorError("invalid_json", f"專案檔不是 UTF-8 編碼：{source}")
    try:
        return json.loads(text.lstrip("\ufeff")), None
    except json.JSONDecodeError as exc:
        return None, EditorError(
            "invalid_json", f"專案檔不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 欄，{exc.msg}",
        )


def _configure_stdio() -> None:
    """Windows 主控台預設不是 UTF-8；統一改成 UTF-8，讓 UI 端可以直接解析。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _print_progress(event: Dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def _configure_signals() -> None:
    """Windows 無法送 SIGINT 給其他程序；後端取消時改送 CTRL_BREAK，同樣以 KeyboardInterrupt 結束，讓暫存目錄被清除。"""
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "list":
        result = list_clips(args.manifest, base_dir=args.base_dir)
    else:
        data, error = load_project_json(args.project)
        if error is not None:
            result = error_result([error])
        elif args.command == "validate":
            result = validate_project(data, base_dir=args.base_dir)
        else:
            result = render_project(
                data, base_dir=args.base_dir, overwrite=args.overwrite, on_progress=_print_progress,
            )
    print(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return exit_code_for(result)


if __name__ == "__main__":
    _configure_signals()
    raise SystemExit(main())
