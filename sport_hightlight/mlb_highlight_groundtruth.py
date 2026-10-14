#!/usr/bin/env python3
"""
mlb_highlight_groundtruth.py

【影片端 + 串接】把 mlb_events.py 整理好的事件（UTC 時間）對到轉播影片的秒數，輸出最終 CSV。

分工：
    mlb_events.py                  資料端。抓 API、存快照、整理事件、配對官方精華、篩選事件。
                                   不依賴 cv2 / numpy，本身就能輸出一份不含影片資訊的事件表。
    mlb_highlight_groundtruth.py   影片端（本檔）。scorebug anchor 偵測、音訊校準、
                                   時間偏移量計算，再把事件換算成影片秒數。

流程：
    1. 載入 MLB 資料（優先用快取快照，結果可重現）。
    2. 取得 anchor：完整轉播用 --game-offset-seconds；單一半局用 --anchor-video-seconds 或 --auto-anchor。
    3. 以半局 anchor 將事件時間換算成影片秒數；--audio-calibrate 可再用音訊上升沿提供額外校準值。
    4. 套用事件篩選（--pitcher / --batter / --strikeouts / --key-events-only），輸出 CSV，
       並在旁邊存一份 <out>.alignment.json 記錄這次對齊用的 offset、anchor 來源與影片資訊。

需求套件：
  pip install requests opencv-python numpy

完整轉播使用方式：
  python mlb_highlight_groundtruth.py \\
      --game-pk 823734 \\
      --video video/eltaMax10_Reds_Brewers_0913.mp4 \\
      --game-offset-seconds 150.5 \\
      --out sport_hightlight/csv_data/823734_ground_truth.csv

單一半局片段使用方式：
  python mlb_highlight_groundtruth.py \\
      --game-pk 823734 \\
      --video bottom2.mp4 \\
      --clip-inning 2 \\
      --clip-half bottom \\
      --auto-anchor \\
      --out sport_hightlight/csv_data/bottom2_ground_truth.csv

只要某投手的三振、或某個投打對決（可搭配上面任一種模式）：
  ... --pitcher Skenes --strikeouts
  ... --pitcher Skenes --batter Alonso

若轉播版型不同，可用 --anchor-video-seconds 手動指定半局起始秒數。
"""

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

import cv2
import numpy as np

from mlb_events import (
    DEFAULT_CACHE_DIR,
    GameData,
    GameEvent,
    HalfInningMarker,
    add_selection_args,
    ensure_parent_dir,
    format_iso,
    load_game_data,
    select_from_args,
    write_csv_atomic,
    write_json_atomic,
)


# --------------------------------------------------------------------------
# 1. 在影片中偵測 live scorebug 出現的時間點
# --------------------------------------------------------------------------

def _has_live_scorebug(frame: np.ndarray) -> bool:
    small = cv2.resize(frame, (160, 90))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    green_ratio = float(
        ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95) & (hsv[:, :, 1] >= 45)).mean()
    )

    scorebug = small[68:88, 125:159]
    gray = cv2.cvtColor(scorebug, cv2.COLOR_BGR2GRAY)
    edge_density = float(cv2.Canny(gray, 50, 150).mean() / 255.0)
    bright_ratio = float((gray > 180).mean())
    dark_ratio = float((gray < 60).mean())
    return (
        green_ratio >= 0.25
        and edge_density >= 0.10
        and bright_ratio >= 0.04
        and dark_ratio >= 0.15
    )


def find_live_scorebug_edges(
    video_path: str,
    start_seconds: float,
    end_seconds: float,
    sample_fps: float = 2.0,
    first_sample_is_edge: bool = False,
    max_edges: Optional[int] = None,
) -> List[float]:
    """回傳視窗內 live scorebug 由無到有的影片秒數（已加一個取樣間隔）。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(int(round(native_fps / sample_fps)), 1)
    start_frame = int(max(start_seconds, 0.0) * native_fps)
    end_frame = int(end_seconds * native_fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    edges: List[float] = []
    previous_live = not first_sample_is_edge
    for frame_idx in range(start_frame, end_frame):
        if not cap.grab():
            break
        if (frame_idx - start_frame) % frame_interval != 0:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break

        live = _has_live_scorebug(frame)
        if live and not previous_live:
            edges.append(frame_idx / native_fps + 1.0 / sample_fps)
            if max_edges is not None and len(edges) >= max_edges:
                break
        previous_live = live

    cap.release()
    return edges


def detect_clip_anchor_in_video(
    video_path: str,
    search_seconds: float = 180.0,
    sample_fps: float = 2.0,
) -> float:
    """以連續球場畫面與右下角 live scorebug 估計片段的比賽開始時間。"""
    edges = find_live_scorebug_edges(
        video_path,
        0.0,
        search_seconds,
        sample_fps,
        first_sample_is_edge=True,
        max_edges=1,
    )
    if not edges:
        raise RuntimeError(
            "無法自動找到片段 anchor；請改用 --anchor-video-seconds，"
            "或調整影片前段搜尋範圍/轉播版型偵測規則"
        )
    return edges[0]


# --------------------------------------------------------------------------
# 2. 音訊校準
# --------------------------------------------------------------------------

@dataclass
class AudioCalibration:
    seconds: float         # 校準後的影片秒數
    strength: float        # 上升沿強度（log-RMS 增量）；越大越明顯，太小代表可能只是雜訊
    at_window_edge: bool   # 峰值落在搜尋視窗邊緣：真正的事件可能在視窗之外，請存疑


def calibrate_event_with_audio(
    video_path: str,
    predicted_seconds: float,
    search_before: float = 2.0,
    search_after: float = 2.0,
    sample_rate: int = 16000,
) -> Optional[AudioCalibration]:
    """在 API 預測時間附近尋找音訊能量上升沿；找不到任何上升沿時回傳 None。"""
    start_seconds = max(predicted_seconds - search_before, 0.0)
    duration_seconds = search_before + search_after
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        video_path,
        "-t",
        f"{duration_seconds:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("音訊校準需要可執行的 ffmpeg") from exc

    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
    if samples.size < sample_rate // 10:
        return None

    frame_size = max(sample_rate // 20, 1)
    hop_size = max(sample_rate // 100, 1)
    frame_count = 1 + (samples.size - frame_size) // hop_size
    frames = np.lib.stride_tricks.as_strided(
        samples,
        shape=(frame_count, frame_size),
        strides=(samples.strides[0] * hop_size, samples.strides[0]),
        writeable=False,
    )
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    log_rms = np.log(rms + 1e-6)
    onset = np.maximum(np.diff(log_rms, prepend=log_rms[0]), 0.0)
    peak_index = int(np.argmax(onset))
    strength = float(onset[peak_index])
    if strength <= 0.0:
        return None  # 視窗內完全沒有上升沿（例如靜音）：不要回傳一個假的峰值

    peak_seconds = start_seconds + (peak_index * hop_size + frame_size / 2) / sample_rate
    window_end = start_seconds + samples.size / sample_rate
    edge_margin = 0.1
    at_edge = (peak_seconds - start_seconds) < edge_margin or (window_end - peak_seconds) < edge_margin
    return AudioCalibration(seconds=peak_seconds, strength=strength, at_window_edge=at_edge)


# --------------------------------------------------------------------------
# 3. 對齊：配對官方半局時間與影片時間點，計算偏移量
# --------------------------------------------------------------------------

@dataclass
class InningOffset:
    inning: int
    half: str
    offset_seconds: float  # video_time = api_time_epoch_seconds + offset


@dataclass
class VideoInfo:
    path: str
    size_bytes: int
    fps: float
    frame_count: int
    duration_seconds: Optional[float]


@dataclass
class Alignment:
    method: str  # manual-full / manual-clip / auto-clip
    anchor_video_seconds: float
    anchor_inning: int
    anchor_half: str
    anchor_api_time: datetime
    offsets: List[InningOffset]


def probe_video(video_path: str) -> VideoInfo:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = frame_count / fps if fps > 0 and frame_count > 0 else None
    return VideoInfo(
        path=video_path,
        size_bytes=os.path.getsize(video_path),
        fps=fps,
        frame_count=frame_count,
        duration_seconds=duration,
    )


def offset_for_event(offsets: List[InningOffset], event: GameEvent) -> Optional[float]:
    for o in offsets:
        if o.inning == event.inning and o.half == event.half:
            return o.offset_seconds
    return None


def marker_for_half(
    markers: List[HalfInningMarker],
    inning: int,
    half: str,
) -> HalfInningMarker:
    for marker in markers:
        if marker.inning == inning and marker.half == half:
            return marker
    raise ValueError(f"找不到第 {inning} 局 {half} 的 MLB API 半局標記")


def compute_alignment(args: argparse.Namespace, data: GameData) -> Alignment:
    if args.clip_inning is not None:
        marker = marker_for_half(data.markers, args.clip_inning, args.clip_half)
        if args.auto_anchor:
            anchor = detect_clip_anchor_in_video(args.video)
            method = "auto-clip"
            print(f"      自動偵測到片段 anchor：影片 {anchor:.1f} 秒")
        else:
            anchor = args.anchor_video_seconds
            method = "manual-clip"
        anchor_source = "自動偵測" if args.auto_anchor else "人工"
        print(
            f"[2/4] 使用第 {args.clip_inning} 局 {args.clip_half} 的{anchor_source} anchor "
            f"（影片 {anchor:.1f} 秒）"
        )
        print("[3/4] 依指定半局計算時間偏移量...")
        offsets = [InningOffset(
            inning=marker.inning,
            half=marker.half,
            offset_seconds=anchor - marker.start_time.timestamp(),
        )]
        return Alignment(method, anchor, marker.inning, marker.half, marker.start_time, offsets)

    print(f"[2/4] 完整影片模式：第一個打席位於影片 {args.game_offset_seconds:.1f} 秒")
    if not data.markers:
        raise RuntimeError("API 資料中沒有任何半局標記，無法對齊（比賽是否已開打？）")
    first = data.markers[0]
    if (first.inning, first.half) != (1, "top"):
        raise RuntimeError(
            f"最早的半局標記是第 {first.inning} 局 {first.half}，不是第 1 局上半；"
            "第 1 局上半的第一個打席可能缺少 startTime，無法用它當全場 anchor"
        )
    print("[3/4] 全場共用同一個時間偏移量...")
    game_offset = args.game_offset_seconds - first.start_time.timestamp()
    # 偏移量全場共用，所以涵蓋事件出現過的每個半局（不受個別半局標記缺漏影響）
    halves = sorted({(e.inning, e.half) for e in data.events}, key=lambda ih: (ih[0], ih[1] != "top"))
    offsets = [InningOffset(inning=i, half=h, offset_seconds=game_offset) for i, h in halves]
    return Alignment(
        "manual-full", args.game_offset_seconds, first.inning, first.half, first.start_time, offsets
    )


# --------------------------------------------------------------------------
# 4. 輸出
# --------------------------------------------------------------------------

# 前 16 欄與原版相同（順序不變），其餘為新增欄位
OUTPUT_FIELDS = [
    "game_pk", "video", "inning", "half", "event_type", "description",
    "is_scoring_play", "predicted_video_seconds",
    "audio_calibrated_video_seconds", "audio_offset_seconds",
    "is_official_highlight", "official_highlight_title",
    "official_highlight_duration", "official_highlight_url",
    "batter", "pitcher",
    # 新增
    "at_bat_index", "batter_id", "pitcher_id",
    "event_time_utc", "time_source",
    "offset_seconds", "alignment_method", "in_video_range",
    "audio_onset_strength", "audio_at_window_edge",
    "highlight_status",
]


def build_rows(
    args: argparse.Namespace,
    events: List[GameEvent],
    alignment: Alignment,
    video: VideoInfo,
) -> List[dict]:
    rows = []
    for event in events:
        offset = offset_for_event(alignment.offsets, event)
        predicted = event.event_time.timestamp() + offset if offset is not None else None

        in_range = ""
        if predicted is not None and video.duration_seconds is not None:
            in_range = 0.0 <= predicted <= video.duration_seconds

        audio = None
        if args.audio_calibrate and predicted is not None and in_range is not False:
            audio = calibrate_event_with_audio(
                args.video,
                predicted,
                search_before=args.audio_search_before,
                search_after=args.audio_search_after,
            )

        highlight = event.highlight
        rows.append({
            "game_pk": args.game_pk,
            "video": args.video,
            "inning": event.inning,
            "half": event.half,
            "event_type": event.event_type,
            "description": event.description,
            "is_scoring_play": event.is_scoring_play,
            "predicted_video_seconds": f"{predicted:.2f}" if predicted is not None else "",
            "audio_calibrated_video_seconds": f"{audio.seconds:.2f}" if audio is not None else "",
            "audio_offset_seconds": (
                f"{audio.seconds - predicted:.2f}" if audio is not None and predicted is not None else ""
            ),
            "is_official_highlight": event.highlight_status == "matched",
            "official_highlight_title": highlight.title if highlight is not None else "",
            "official_highlight_duration": (
                f"{highlight.duration_seconds:.1f}"
                if highlight is not None and highlight.duration_seconds is not None
                else ""
            ),
            "official_highlight_url": highlight.playback_url if highlight is not None else "",
            "batter": event.batter,
            "pitcher": event.pitcher,
            "at_bat_index": "" if event.at_bat_index is None else event.at_bat_index,
            "batter_id": "" if event.batter_id is None else event.batter_id,
            "pitcher_id": "" if event.pitcher_id is None else event.pitcher_id,
            "event_time_utc": format_iso(event.event_time),
            "time_source": event.time_source,
            "offset_seconds": f"{offset:.3f}" if offset is not None else "",
            "alignment_method": alignment.method,
            "in_video_range": in_range,
            "audio_onset_strength": f"{audio.strength:.3f}" if audio is not None else "",
            "audio_at_window_edge": audio.at_window_edge if audio is not None else "",
            "highlight_status": event.highlight_status,
        })
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自動產生棒球轉播影片的精華事件 ground truth 時間軸")
    parser.add_argument("--game-pk", type=int, required=True, help="MLB Stats API 的 game_pk（例如 823734）；可用 mlb_game_lookup.py 由日期與球隊查詢")
    parser.add_argument("--video", type=str, required=True, help="轉播影片檔案路徑")
    parser.add_argument(
        "--game-offset-seconds",
        type=float,
        help="完整影片模式必填：第一個打席開始在影片中的秒數（連續錄影時全場共用）",
    )
    parser.add_argument("--clip-inning", type=int, help="單一半局片段的局數；需和 --clip-half、--anchor-video-seconds 一起使用")
    parser.add_argument("--clip-half", choices=("top", "bottom"), help="單一半局片段的上半局或下半局")
    parser.add_argument(
        "--anchor-video-seconds",
        type=float,
        help="指定半局第一個打席開始在影片中的秒數；可用 --auto-anchor 取代",
    )
    parser.add_argument(
        "--auto-anchor",
        action="store_true",
        help="自動偵測片段的第一個 live scorebug anchor",
    )
    parser.add_argument(
        "--audio-calibrate",
        action="store_true",
        help="在 API 預測事件附近用音訊能量上升沿校準時間；需要 ffmpeg",
    )
    parser.add_argument(
        "--audio-search-before",
        type=float,
        default=2.0,
        help="音訊校準搜尋事件前幾秒（預設 2）",
    )
    parser.add_argument(
        "--audio-search-after",
        type=float,
        default=2.0,
        help="音訊校準搜尋事件後幾秒（預設 2）",
    )
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR, help="原始 API 快照目錄（預設 mlb_cache）")
    parser.add_argument("--refresh", action="store_true", help="忽略快取，重新向 API 抓取並覆寫快照")
    add_selection_args(parser)
    parser.add_argument("--out", type=str, default="ground_truth.csv", help="輸出 CSV 路徑")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.clip_inning is None and (args.clip_half is not None or args.anchor_video_seconds is not None or args.auto_anchor):
        parser.error("片段參數必須搭配 --clip-inning")
    if args.clip_inning is not None and args.clip_half is None:
        parser.error("--clip-inning 必須搭配 --clip-half")
    if args.clip_inning is not None and args.auto_anchor and args.anchor_video_seconds is not None:
        parser.error("--auto-anchor 不可和 --anchor-video-seconds 同時使用")
    if args.clip_inning is not None and not args.auto_anchor and args.anchor_video_seconds is None:
        parser.error("片段模式必須提供 --anchor-video-seconds 或 --auto-anchor")
    if args.clip_inning is None and args.game_offset_seconds is None:
        parser.error("完整影片模式必須提供 --game-offset-seconds")
    if args.audio_calibrate and shutil.which("ffmpeg") is None:
        parser.error("--audio-calibrate 需要 ffmpeg，但在 PATH 中找不到")


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)

    # 先做便宜的檢查：影片打不開、輸出路徑建不起來，都不要等到跑完才發現
    ensure_parent_dir(args.out)
    video = probe_video(args.video)

    print(f"[1/4] 載入 game_pk={args.game_pk} 的 MLB 資料...")
    data = load_game_data(args.game_pk, args.cache_dir, args.refresh)

    alignment = compute_alignment(args, data)

    events = data.events
    if args.clip_inning is not None:
        events = [e for e in events if e.inning == args.clip_inning and e.half == args.clip_half]
    events = select_from_args(events, args)

    print(f"[4/4] 套用偏移量，換算 {len(events)} 筆事件的影片秒數，輸出至 {args.out} ...")
    rows = build_rows(args, events, alignment, video)
    write_csv_atomic(args.out, OUTPUT_FIELDS, rows)

    sidecar = os.path.splitext(args.out)[0] + ".alignment.json"
    write_json_atomic(sidecar, {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "argv": sys.argv[1:],
        "game_pk": args.game_pk,
        "api_snapshot_fetched_at": data.fetched_at,
        "api_game_state": data.game_state,
        "video": asdict(video),
        "alignment": {
            "method": alignment.method,
            "anchor_video_seconds": alignment.anchor_video_seconds,
            "anchor_inning": alignment.anchor_inning,
            "anchor_half": alignment.anchor_half,
            "anchor_api_time_utc": format_iso(alignment.anchor_api_time),
            "offsets": [asdict(o) for o in alignment.offsets],
        },
        "audio_calibrate": {
            "enabled": args.audio_calibrate,
            "search_before": args.audio_search_before,
            "search_after": args.audio_search_after,
        },
        "selection": {
            "pitcher": args.pitcher,
            "batter": args.batter,
            "strikeouts": args.strikeouts,
            "key_events_only": args.key_events_only,
            "keywords": args.keywords,
        },
        "rows": len(rows),
    })

    matched = sum(1 for r in rows if r["predicted_video_seconds"])
    out_of_range = sum(1 for r in rows if r["in_video_range"] is False)
    print(f"完成！共輸出 {len(rows)} 筆事件，{matched} 筆成功換算出影片秒數。")
    if out_of_range:
        print(f"      警告：{out_of_range} 筆事件的預測秒數落在影片長度之外，請檢查 offset 是否正確。")
    if args.audio_calibrate:
        calibrated = sum(1 for r in rows if r["audio_calibrated_video_seconds"])
        edge = sum(1 for r in rows if r["audio_at_window_edge"] is True)
        print(f"      音訊校準完成 {calibrated} 筆事件（其中 {edge} 筆峰值落在搜尋視窗邊緣，建議存疑）。")
    print(f"      對齊資訊已存至 {sidecar}")


if __name__ == "__main__":
    main()
