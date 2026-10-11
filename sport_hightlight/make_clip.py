#!/usr/bin/env python3
"""Create fixed-length training clips from a ground-truth CSV."""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


TIME_FIELDS: Tuple[str, ...] = (
    "audio_calibrated_video_seconds",
    "predicted_video_seconds",
)

X264_PRESETS: Tuple[str, ...] = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

ENCODERS: Tuple[str, ...] = ("libx264", "nvenc", "qsv")


def default_jobs() -> int:
    # libx264 本身已用多執行緒，每 4 個邏輯核心配 1 個 ffmpeg 就足以吃滿 CPU
    return max(1, min(8, (os.cpu_count() or 4) // 4))


def video_codec_args(encoder: str, preset: str) -> List[str]:
    # 硬體編碼器的品質設定已調到與 libx264 veryfast CRF 20 的檔案大小、SSIM 相近。
    # 解碼一律用 CPU：Intel 硬體解碼（-hwaccel qsv）會讓片段固定提早 4 格。
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq",
                "-rc", "vbr", "-cq", "26", "-b:v", "0"]
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "22"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", "20"]


def check_encoder(encoder: str, video_args: Sequence[str]) -> None:
    """硬體編碼器先試編 1 格，沒有對應硬體時直接給出明確錯誤。"""
    if encoder == "libx264":
        return
    try:
        run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=320x240:rate=30",
            "-frames:v", "1", *video_args, "-f", "null", "-",
        ])
    except RuntimeError as exc:
        raise RuntimeError(f"這台電腦無法使用 --encoder {encoder}：{exc}") from exc


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


TEAM_SIDES = ("away", "home")


def batting_side(half: str) -> str:
    """Away team bats in the top half, home team in the bottom half."""
    return "away" if half.strip().lower() == "top" else "home"


def pitching_side(half: str) -> str:
    return "home" if batting_side(half) == "away" else "away"


def filter_rows(rows: List[Dict[str, str]], args: argparse.Namespace) -> List[Tuple[int, Dict[str, str]]]:
    """Keep rows matching every given filter. Returns (original row number, row) pairs.

    Row numbers stay those of the full CSV so clip names don't change when filters change.
    """
    event_types = set(getattr(args, "event_types", None) or [])
    batting_team = set(getattr(args, "batting_team", None) or [])
    pitching_team = set(getattr(args, "pitching_team", None) or [])
    batters = set(getattr(args, "batters", None) or [])
    pitchers = set(getattr(args, "pitchers", None) or [])
    innings = getattr(args, "innings", None)
    scoring_only = bool(getattr(args, "scoring_only", False))
    official_only = bool(getattr(args, "official_only", False))

    if rows and (batters or pitchers):
        missing = [c for c, wanted in (("batter", batters), ("pitcher", pitchers)) if wanted and c not in rows[0]]
        if missing:
            raise ValueError(
                f"ground truth 沒有 {', '.join(missing)} 欄位；請用新版 mlb_highlight_groundtruth.py 重新產生"
            )

    kept: List[Tuple[int, Dict[str, str]]] = []
    for row_number, row in enumerate(rows, start=1):
        half = row.get("half", "")
        if event_types and row.get("event_type", "") not in event_types:
            continue
        if batting_team and batting_side(half) not in batting_team:
            continue
        if pitching_team and pitching_side(half) not in pitching_team:
            continue
        if batters and row.get("batter", "") not in batters:
            continue
        if pitchers and row.get("pitcher", "") not in pitchers:
            continue
        if innings:
            try:
                inning = int(row.get("inning", ""))
            except ValueError:
                continue
            if not innings[0] <= inning <= innings[1]:
                continue
        if scoring_only and not parse_bool(row.get("is_scoring_play", "")):
            continue
        if official_only and not parse_bool(row.get("is_official_highlight", "")):
            continue
        kept.append((row_number, row))
    return kept


def emit_progress(enabled: bool, **event) -> None:
    if enabled:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "event"


def extract_clip(
    video_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
    video_args: Sequence[str],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 先寫到暫存檔，成功才改名；中斷或失敗時不會留下會被當成已完成而略過的殘缺短片
    temp_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    try:
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
            *video_args,
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_path),
        ])
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def extract_clips(
    video_path: Path,
    clips: Sequence[Tuple[Path, float, float]],
    video_args: Sequence[str],
    jobs: int,
    on_done: Optional[Callable[[Path], None]] = None,
) -> None:
    """平行擷取 (輸出路徑, 起點秒數, 長度秒數)；任一失敗即取消尚未開始的工作。

    on_done 在主執行緒中、每完成一個短片就呼叫一次（完成順序不一定是輸入順序）。
    """
    if not clips:
        return
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(extract_clip, video_path, path, start, duration, video_args): path
            for path, start, duration in clips
        }
        try:
            for future in as_completed(futures):
                future.result()
                if on_done is not None:
                    on_done(futures[future])
        except BaseException:
            for future in futures:
                future.cancel()
            raise


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
    parser.add_argument(
        "--jobs",
        type=int,
        default=default_jobs(),
        help="同時執行的 ffmpeg 數量；預設為邏輯核心數 / 4，介於 1 到 8（本機為 %(default)s）",
    )
    parser.add_argument(
        "--encoder",
        choices=ENCODERS,
        default="libx264",
        help="影片編碼器：libx264（CPU，預設）、nvenc（NVIDIA 顯卡）、qsv（Intel 內顯）；"
        "硬體編碼較快，檔案大小與畫質與 libx264 veryfast 相近",
    )
    parser.add_argument(
        "--preset",
        choices=X264_PRESETS,
        default="veryfast",
        help="libx264 編碼速度，只對 --encoder libx264 有效；越快檔案越大（預設 veryfast）",
    )
    filters = parser.add_argument_group("篩選（可組合，不同條件需同時符合）")
    filters.add_argument("--event-types", nargs="+", metavar="TYPE", help="只保留這些 event_type，例如 Strikeout \"Home Run\"")
    filters.add_argument("--batting-team", nargs="+", choices=TEAM_SIDES, help="打擊方：away（上半局）或 home（下半局）")
    filters.add_argument("--pitching-team", nargs="+", choices=TEAM_SIDES, help="投球方：away 或 home")
    filters.add_argument("--batters", nargs="+", metavar="NAME", help="只保留這些打者（需要 batter 欄位）")
    filters.add_argument("--pitchers", nargs="+", metavar="NAME", help="只保留這些投手（需要 pitcher 欄位）")
    filters.add_argument("--innings", nargs=2, type=int, metavar=("FROM", "TO"), help="局數範圍，例如 7 9")
    filters.add_argument("--scoring-only", action="store_true", help="只保留得分事件")
    filters.add_argument("--official-only", action="store_true", help="只保留 MLB 官方精華")
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="每完成一個事件就在 stderr 輸出一行 JSON 進度（供後端讀取）",
    )
    return parser


def make_clips(args: argparse.Namespace) -> int:
    if args.pre_seconds < 0 or args.post_seconds < 0:
        raise ValueError("--pre-seconds 和 --post-seconds 不可小於 0")
    if args.jobs < 1:
        raise ValueError("--jobs 至少為 1")
    if not args.video.is_file():
        raise FileNotFoundError(f"找不到影片：{args.video}")
    if not args.ground_truth.is_file():
        raise FileNotFoundError(f"找不到 ground truth：{args.ground_truth}")
    if getattr(args, "innings", None) and args.innings[0] > args.innings[1]:
        raise ValueError("--innings 的 FROM 不可大於 TO")

    video_duration = probe_duration(args.video)
    output_dir = args.out_dir
    manifest_path = args.manifest or output_dir / "manifest.csv"
    output_dir.mkdir(parents=True, exist_ok=True)

    with args.ground_truth.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))

    selected = filter_rows(rows, args)
    progress = bool(getattr(args, "progress_json", False))
    total = len(selected)
    finished = 0  # clips done or skipped, in completion order
    emit_progress(progress, event="clips_start", total=total)

    def report(event: str, clip: Optional[Path] = None) -> None:
        nonlocal finished
        finished += 1
        extra = {"clip": clip.as_posix()} if clip is not None else {}
        emit_progress(progress, event=event, index=finished, total=total, **extra)

    manifest_rows: List[Dict[str, str]] = []
    pending: List[Tuple[Path, float, float]] = []
    skipped = 0
    for row_number, row in selected:
        event_seconds, time_source = parse_seconds(row)
        if event_seconds is None:
            skipped += 1
            report("clip_skipped")
            continue

        requested_start = event_seconds - args.pre_seconds
        requested_end = event_seconds + args.post_seconds
        clip_start = max(requested_start, 0.0)
        clip_end = min(requested_end, video_duration)
        clip_duration = clip_end - clip_start
        if clip_duration <= 0:
            skipped += 1
            report("clip_skipped")
            continue

        is_official = parse_bool(row.get("is_official_highlight", ""))
        label = "positive" if is_official else "hard_negative"
        inning = safe_name(row.get("inning", "unknown"))
        half = safe_name(row.get("half", "unknown"))
        event_type = safe_name(row.get("event_type", "event"))
        clip_name = f"event_{row_number:04d}_{inning}{half}_{event_type}.mp4"
        clip_path = output_dir / label / clip_name

        if not clip_path.exists() or args.overwrite:
            pending.append((clip_path, clip_start, clip_duration))
        else:
            report("clip_done", clip_path)  # already cut earlier; kept as is

        # manifest 會進版控，一律用 / 分隔，Windows 產生的檔案在 macOS／Linux 也能用
        manifest_rows.append({
            "clip_path": clip_path.as_posix(),
            "label": label,
            "game_pk": row.get("game_pk", ""),
            "source_video": args.video.as_posix(),
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
            "batter": row.get("batter", ""),
            "pitcher": row.get("pitcher", ""),
        })

    video_args = video_codec_args(args.encoder, args.preset)
    if pending:
        check_encoder(args.encoder, video_args)
        settings = f"--encoder {args.encoder}，--jobs {args.jobs}"
        if args.encoder == "libx264":
            settings += f"，--preset {args.preset}"
        print(f"擷取 {len(pending)} 個短片（{settings}）...")
    extract_clips(args.video, pending, video_args, args.jobs, on_done=lambda path: report("clip_done", path))
    created = len(pending)

    manifest_fields = list(manifest_rows[0].keys()) if manifest_rows else [
        "clip_path", "label", "game_pk", "source_video", "inning", "half", "event_type",
        "description", "event_seconds", "event_time_source", "clip_start_seconds",
        "clip_end_seconds", "clip_duration_seconds", "is_official_highlight",
        "official_highlight_title", "official_highlight_url", "batter", "pitcher",
    ]
    with manifest_path.open("w", encoding="utf-8", newline="") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=manifest_fields)
        writer.writeheader()
        writer.writerows(manifest_rows)

    if len(selected) != len(rows):
        print(f"篩選：{len(rows)} 筆事件中保留 {len(selected)} 筆。")
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
