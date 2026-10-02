#!/usr/bin/env python3
"""Create fixed-length training clips from a ground-truth CSV."""

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


TIME_FIELDS: Tuple[str, ...] = (
    "audio_calibrated_video_seconds",
    "predicted_video_seconds",
)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def parse_seconds(row: Dict[str, str]) -> Tuple[Optional[float], Optional[str]]:
    for field in TIME_FIELDS:
        value = (row.get(field) or "").strip()
        if not value:
            continue
        try:
            return float(value), field
        except ValueError:
            continue
    return None, None


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"找不到 {command[0]}；請先安裝 ffmpeg 並確認它在 PATH 中"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"命令執行失敗：{' '.join(command)}\n{detail}") from exc


def probe_duration(video_path: Path) -> float:
    result = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ])
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"無法取得影片長度：{video_path}") from exc


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "event"


def extract_clip(
    video_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration_seconds:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    ])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="從 ground truth CSV 擷取固定長度的 MLB 事件短片"
    )
    parser.add_argument("--ground-truth", type=Path, required=True, help="ground truth CSV")
    parser.add_argument("--video", type=Path, required=True, help="原始影片")
    parser.add_argument("--out-dir", type=Path, required=True, help="輸出資料集目錄")
    parser.add_argument("--pre-seconds", type=float, default=8.0, help="事件前秒數")
    parser.add_argument("--post-seconds", type=float, default=12.0, help="事件後秒數")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="manifest CSV 路徑；預設為 out-dir/manifest.csv",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允許覆寫已存在的短片",
    )
    return parser


def make_clips(args: argparse.Namespace) -> int:
    if args.pre_seconds < 0 or args.post_seconds < 0:
        raise ValueError("--pre-seconds 和 --post-seconds 不可小於 0")
    if not args.video.is_file():
        raise FileNotFoundError(f"找不到影片：{args.video}")
    if not args.ground_truth.is_file():
        raise FileNotFoundError(f"找不到 ground truth：{args.ground_truth}")

    video_duration = probe_duration(args.video)
    output_dir = args.out_dir
    manifest_path = args.manifest or output_dir / "manifest.csv"
    output_dir.mkdir(parents=True, exist_ok=True)

    with args.ground_truth.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))

    manifest_rows: List[Dict[str, str]] = []
    skipped = 0
    created = 0
    for row_number, row in enumerate(rows, start=1):
        event_seconds, time_source = parse_seconds(row)
        if event_seconds is None:
            skipped += 1
            continue

        requested_start = event_seconds - args.pre_seconds
        requested_end = event_seconds + args.post_seconds
        clip_start = max(requested_start, 0.0)
        clip_end = min(requested_end, video_duration)
        clip_duration = clip_end - clip_start
        if clip_duration <= 0:
            skipped += 1
            continue

        is_official = parse_bool(row.get("is_official_highlight", ""))
        label = "positive" if is_official else "hard_negative"
        inning = safe_name(row.get("inning", "unknown"))
        half = safe_name(row.get("half", "unknown"))
        event_type = safe_name(row.get("event_type", "event"))
        clip_name = f"event_{row_number:04d}_{inning}{half}_{event_type}.mp4"
        clip_path = output_dir / label / clip_name

        if not clip_path.exists() or args.overwrite:
            extract_clip(args.video, clip_path, clip_start, clip_duration)
            created += 1

        manifest_rows.append({
            "clip_path": str(clip_path),
            "label": label,
            "game_pk": row.get("game_pk", ""),
            "inning": row.get("inning", ""),
            "half": row.get("half", ""),
            "event_type": row.get("event_type", ""),
            "description": row.get("description", ""),
            "event_seconds": f"{event_seconds:.3f}",
            "event_time_source": time_source or "",
            "clip_start_seconds": f"{clip_start:.3f}",
            "clip_end_seconds": f"{clip_end:.3f}",
            "clip_duration_seconds": f"{clip_duration:.3f}",
            "is_official_highlight": str(is_official),
            "official_highlight_title": row.get("official_highlight_title", ""),
            "official_highlight_url": row.get("official_highlight_url", ""),
        })

    manifest_fields = list(manifest_rows[0].keys()) if manifest_rows else [
        "clip_path", "label", "game_pk", "inning", "half", "event_type",
        "description", "event_seconds", "event_time_source", "clip_start_seconds",
        "clip_end_seconds", "clip_duration_seconds", "is_official_highlight",
        "official_highlight_title", "official_highlight_url",
    ]
    with manifest_path.open("w", encoding="utf-8", newline="") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=manifest_fields)
        writer.writeheader()
        writer.writerows(manifest_rows)

    print(f"完成：建立/保留 {len(manifest_rows)} 個事件短片，新增 {created} 個。")
    print(f"略過 {skipped} 筆沒有有效影片時間的事件。")
    print(f"manifest：{manifest_path}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return make_clips(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
