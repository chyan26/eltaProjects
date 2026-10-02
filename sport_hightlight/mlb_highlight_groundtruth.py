#!/usr/bin/env python3
"""
mlb_highlight_groundtruth.py

產生棒球轉播影片的事件 ground truth 時間軸。

流程：
    1. 從 MLB Stats API 的 /feed/live 取得所有逐球事件與官方時間。
    2. 從 /content 取得官方精華影片，將其配對到逐球事件並標記。
    3. 以半局 anchor 將事件時間換算成影片秒數。
    4. 輸出包含所有事件與官方精華標籤的 CSV。

單一半局模式使用 --auto-anchor 自動找影片起始時間；完整轉播模式則使用比分板 ROI
偵測半局切換。--audio-calibrate 可在事件附近用音訊上升沿提供額外校準值。

需求套件：
  pip install requests opencv-python numpy

使用方式：
  python mlb_highlight_groundtruth.py \
      --game-pk 823734 \
      --video full_broadcast.mp4 \
      --roi 1700 50 220 80 \
      --sample-fps 1 \
      --out ground_truth.csv

單一半局片段使用方式：
    python mlb_highlight_groundtruth.py \
            --game-pk 823734 \
            --video bottom2.mp4 \
            --clip-inning 2 \
            --clip-half bottom \
            --auto-anchor \
            --out bottom2_ground_truth.csv

若轉播版型不同，可用 --anchor-video-seconds 手動指定半局起始秒數。

ROI 格式：x y width height（比分板在畫面上的像素範圍，需自行用看圖工具框出一次）
"""

import argparse
import csv
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import cv2
import numpy as np
import requests


MLB_API_TEMPLATE = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
MLB_CONTENT_API_TEMPLATE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/content"


# --------------------------------------------------------------------------
# 1. 從 MLB Stats API 撈取官方逐球資料
# --------------------------------------------------------------------------

@dataclass
class HalfInningMarker:
    inning: int
    half: str  # "top" or "bottom"
    start_time: datetime  # UTC


@dataclass
class GameEvent:
    inning: int
    half: str
    description: str
    event_type: str
    is_scoring_play: bool
    event_time: datetime  # UTC，該球（play event）發生的精確時間


@dataclass
class OfficialHighlight:
    highlight_id: str
    title: str
    description: str
    duration_seconds: Optional[float]
    playback_url: str
    inning: Optional[int]
    half: Optional[str]


def fetch_game_feed(game_pk: int) -> dict:
    url = MLB_API_TEMPLATE.format(game_pk=game_pk)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_game_content(game_pk: int) -> dict:
    url = MLB_CONTENT_API_TEMPLATE.format(game_pk=game_pk)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_duration(duration: Optional[str]) -> Optional[float]:
    if not duration:
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", duration)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_inning(text: str) -> Tuple[Optional[int], Optional[str]]:
    match = re.search(
        r"\b(top|bottom) of the (\d+)(?:st|nd|rd|th)\b|"
        r"\b(top|bottom) (\d+)(?:st|nd|rd|th)\b|"
        r"\bthe (\d+)(?:st|nd|rd|th) inning\b",
        text.lower(),
    )
    if not match:
        return None, None
    half = match.group(1) or match.group(3)
    inning = match.group(2) or match.group(4) or match.group(5)
    return int(inning), half


def _playback_url(playbacks: list) -> str:
    preferred = ("mp4Avc", "hlsCloud", "HTTP_CLOUD_WIRED")
    for name in preferred:
        for playback in playbacks or []:
            if playback.get("name") == name and playback.get("url"):
                return playback["url"]
    return ""


def extract_official_highlights(
    content: dict,
    game_pk: int,
) -> List[OfficialHighlight]:
    highlights: List[OfficialHighlight] = []
    seen = set()
    for item in _walk_dicts(content):
        if item.get("type") != "video" or not item.get("id"):
            continue
        keywords = item.get("keywordsAll") or []
        keyword_values = {str(k.get("value", "")).lower() for k in keywords}
        if f"gamepk-{game_pk}" not in keyword_values:
            continue
        if not {"highlight", "in-game-highlight"}.intersection(keyword_values):
            continue
        highlight_id = str(item["id"])
        if highlight_id in seen:
            continue
        seen.add(highlight_id)
        title = item.get("headline") or item.get("title") or ""
        description = item.get("description") or item.get("blurb") or ""
        inning, half = _parse_inning(f"{title} {description}")
        highlights.append(OfficialHighlight(
            highlight_id=highlight_id,
            title=title,
            description=description,
            duration_seconds=_parse_duration(item.get("duration")),
            playback_url=_playback_url(item.get("playbacks", [])),
            inning=inning,
            half=half,
        ))
    return highlights


def _normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def match_official_highlight(
    event: GameEvent,
    highlights: List[OfficialHighlight],
) -> Optional[OfficialHighlight]:
    event_text = _normalise_text(f"{event.event_type} {event.description}")
    event_tokens = set(re.findall(r"[a-z]{3,}", event_text))
    event_kind = _normalise_text(event.event_type)
    kind_patterns = {
        "home run": ("home run", "homer"),
        "strikeout": ("strikeout", "strikes out", "fans"),
        "pop out": ("pop out", "pops out"),
        "groundout": ("ground out", "grounds out"),
        "flyout": ("fly out", "flies out"),
        "lineout": ("line out", "lines out"),
        "double": ("double",),
        "single": ("single",),
        "walk": ("walk",),
        "sac fly": ("sacrifice fly", "sac fly"),
    }
    patterns = kind_patterns.get(event_kind, (event_kind,))

    best: Optional[OfficialHighlight] = None
    best_score = 0
    for highlight in highlights:
        if highlight.inning is None:
            continue
        if highlight.inning != event.inning:
            continue
        if highlight.half is not None and highlight.half != event.half:
            continue
        highlight_text = _normalise_text(f"{highlight.title} {highlight.description}")
        if not any(pattern in highlight_text for pattern in patterns):
            continue
        highlight_tokens = set(re.findall(r"[a-z]{3,}", highlight_text))
        score = len(event_tokens.intersection(highlight_tokens))
        if score >= 2 and score > best_score:
            best = highlight
            best_score = score
    return best


def parse_iso(ts: str) -> datetime:
    # MLB API 的時間格式類似 "2026-09-13T19:12:03.000Z"
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def extract_half_inning_markers(feed: dict) -> List[HalfInningMarker]:
    """從 allPlays 裡抓出每個半局第一個打席的開始時間，當作該半局的錨點時間。"""
    markers: List[HalfInningMarker] = []
    seen = set()
    all_plays = feed["liveData"]["plays"]["allPlays"]
    for play in all_plays:
        about = play["about"]
        inning = about["inning"]
        half = about.get("halfInning", "top")  # "top" 或 "bottom"
        key = (inning, half)
        if key in seen:
            continue
        seen.add(key)
        start_time_str = about.get("startTime")
        if not start_time_str:
            continue
        markers.append(HalfInningMarker(
            inning=inning,
            half=half,
            start_time=parse_iso(start_time_str),
        ))
    markers.sort(key=lambda m: m.start_time)
    return markers


def extract_key_events(
    feed: dict,
    keywords: Tuple[str, ...] = ("home run",),
    include_all_events: bool = True,
) -> List[GameEvent]:
    """抓出全壘打／得分事件的精確時間（用該球的 playEvents.startTime，而非整個打席的時間）。"""
    events: List[GameEvent] = []
    all_plays = feed["liveData"]["plays"]["allPlays"]
    for play in all_plays:
        about = play["about"]
        inning = about["inning"]
        half = about.get("halfInning", "top")
        result = play.get("result", {})
        event_name = result.get("event", "")
        is_scoring = bool(about.get("isScoringPlay", False))

        matched = (
            include_all_events
            or is_scoring
            or any(k.lower() in event_name.lower() for k in keywords)
        )
        if not matched:
            continue

        # 找這個打席裡「造成結果」的那一球精確時間：優先用最後一個 isPitch 的 playEvent
        event_time = None
        for pe in reversed(play.get("playEvents", [])):
            if pe.get("isPitch") and pe.get("startTime"):
                event_time = parse_iso(pe["startTime"])
                break
        if event_time is None:
            # 退而求其次，用打席的 about.endTime
            end_time_str = about.get("endTime")
            if not end_time_str:
                continue
            event_time = parse_iso(end_time_str)

        events.append(GameEvent(
            inning=inning,
            half=half,
            description=result.get("description", event_name),
            event_type=event_name or ("Scoring Play" if is_scoring else "Unknown"),
            is_scoring_play=is_scoring,
            event_time=event_time,
        ))
    events.sort(key=lambda e: e.event_time)
    return events


# --------------------------------------------------------------------------
# 2. 在影片的比分板 ROI 上偵測半局切換的候選時間點
# --------------------------------------------------------------------------

def detect_half_inning_changes_in_video(
    video_path: str,
    roi: Tuple[int, int, int, int],
    sample_fps: float = 1.0,
    diff_threshold: float = 18.0,
    min_gap_seconds: float = 20.0,
) -> List[float]:
    """
    粗取樣掃描整支影片，在比分板 ROI 上做 frame differencing，
    回傳「疑似局數變化」的影片秒數清單。

    這是半局切換偵測，不是逐球偵測，取樣率不需要很高（預設每秒一張）。
    min_gap_seconds 用來避免同一次切換因為疊圖動畫被重複偵測成好幾個候選點，
    實務上建議先用小範圍測試影片調好 diff_threshold 再跑整場。
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(int(round(native_fps / sample_fps)), 1)

    x, y, w, h = roi
    prev_gray: Optional[np.ndarray] = None
    candidates: List[Tuple[float, float]] = []  # (timestamp_sec, diff_score)

    frame_idx = 0
    while True:
        ret = cap.grab()
        if not ret:
            break
        if frame_idx % frame_interval != 0:
            frame_idx += 1
            continue

        ret, frame = cap.retrieve()
        if not ret:
            break

        timestamp_sec = frame_idx / native_fps
        patch = frame[y:y + h, x:x + w]
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            score = float(np.mean(diff))
            if score > diff_threshold:
                candidates.append((timestamp_sec, score))

        prev_gray = gray
        frame_idx += 1

    cap.release()

    # 把太接近的候選點（同一次切換動畫觸發多次）合併，只留第一個
    candidates.sort(key=lambda c: c[0])
    merged: List[float] = []
    for ts, _score in candidates:
        if not merged or ts - merged[-1] > min_gap_seconds:
            merged.append(ts)
    return merged


# --------------------------------------------------------------------------
# 3. 配對官方半局時間與影片偵測時間點，計算偏移量
# --------------------------------------------------------------------------

@dataclass
class InningOffset:
    inning: int
    half: str
    offset_seconds: float  # video_time = api_time_epoch_seconds + offset


def align_markers_to_video(
    markers: List[HalfInningMarker],
    video_timestamps: List[float],
) -> List[InningOffset]:
    """
    假設兩份清單筆數、順序一一對應（都是照比賽進行順序排列的半局切換點），
    直接按順序配對。如果筆數不一致，只配對較短清單的長度，並印出警告，
    這種情況通常代表 diff_threshold 設太敏感（誤判太多）或太遲鈍（漏掉切換），
    需要回頭調整參數。
    """
    n = min(len(markers), len(video_timestamps))
    if len(markers) != len(video_timestamps):
        print(
            f"[警告] API 半局數量（{len(markers)}）與影片偵測到的切換點數量"
            f"（{len(video_timestamps)}）不一致，只配對前 {n} 筆，"
            f"請檢查 --diff-threshold / --min-gap-seconds 或影片是否涵蓋整場比賽。",
            file=sys.stderr,
        )

    offsets: List[InningOffset] = []
    for marker, video_ts in zip(markers[:n], video_timestamps[:n]):
        api_epoch = marker.start_time.timestamp()
        offset = video_ts - api_epoch
        offsets.append(InningOffset(
            inning=marker.inning,
            half=marker.half,
            offset_seconds=offset,
        ))
    return offsets


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


def detect_clip_anchor_in_video(
    video_path: str,
    search_seconds: float = 180.0,
    sample_fps: float = 2.0,
) -> float:
    """以連續球場畫面與右下角 live scorebug 估計片段的比賽開始時間。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(int(round(native_fps / sample_fps)), 1)
    max_frames = int(search_seconds * native_fps)
    previous_live_scorebug = False

    for frame_idx in range(max_frames):
        if not cap.grab():
            break
        if frame_idx % frame_interval != 0:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break

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
        has_live_scorebug = (
            green_ratio >= 0.25
            and edge_density >= 0.10
            and bright_ratio >= 0.04
            and dark_ratio >= 0.15
        )

        if has_live_scorebug and not previous_live_scorebug:
            cap.release()
            return frame_idx / native_fps + (1.0 / sample_fps)
        previous_live_scorebug = has_live_scorebug

    cap.release()
    raise RuntimeError(
        "無法自動找到片段 anchor；請改用 --anchor-video-seconds，"
        "或調整影片前段搜尋範圍/轉播版型偵測規則"
    )


def calibrate_event_with_audio(
    video_path: str,
    predicted_seconds: float,
    search_before: float = 2.0,
    search_after: float = 2.0,
    sample_rate: int = 16000,
) -> Optional[float]:
    """在 API 預測時間附近尋找音訊能量上升沿，回傳校準後影片秒數。"""
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
    peak_seconds = start_seconds + (peak_index * hop_size + frame_size / 2) / sample_rate
    return peak_seconds


# --------------------------------------------------------------------------
# 4. 主流程
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="自動產生棒球轉播影片的精華事件 ground truth 時間軸")
    parser.add_argument("--game-pk", type=int, required=True, help="MLB Stats API 的 game_pk（例如 823734）")
    parser.add_argument("--video", type=str, required=True, help="完整轉播影片檔案路徑")
    parser.add_argument(
        "--roi", type=int, nargs=4,
        metavar=("X", "Y", "W", "H"),
        help="比分板在畫面上的 ROI（完整影片模式必填；像素座標：x y width height）",
    )
    parser.add_argument("--sample-fps", type=float, default=1.0, help="掃描影片時的取樣頻率（預設每秒 1 張）")
    parser.add_argument("--diff-threshold", type=float, default=18.0, help="frame diff 判定為變化的閾值，需依實際畫面調整")
    parser.add_argument("--min-gap-seconds", type=float, default=20.0, help="同一次切換的最小間隔秒數，避免重複偵測")
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
    parser.add_argument(
        "--keywords", type=str, nargs="*", default=["home run"],
        help="除了 isScoringPlay 之外，額外用事件描述關鍵字篩選（預設只抓全壘打）",
    )
    parser.add_argument(
        "--key-events-only",
        action="store_true",
        help="只輸出得分/關鍵字事件；預設輸出所有打席事件",
    )
    parser.add_argument("--out", type=str, default="ground_truth.csv", help="輸出 CSV 路徑")
    args = parser.parse_args()

    if args.clip_inning is None and (args.clip_half is not None or args.anchor_video_seconds is not None or args.auto_anchor):
        parser.error("片段參數必須搭配 --clip-inning")
    if args.clip_inning is not None and args.clip_half is None:
        parser.error("--clip-inning 必須搭配 --clip-half")
    if args.clip_inning is not None and args.auto_anchor and args.anchor_video_seconds is not None:
        parser.error("--auto-anchor 不可和 --anchor-video-seconds 同時使用")
    if args.clip_inning is not None and not args.auto_anchor and args.anchor_video_seconds is None:
        parser.error("片段模式必須提供 --anchor-video-seconds 或 --auto-anchor")
    if args.clip_inning is None and args.roi is None:
        parser.error("完整影片模式必須提供 --roi；單一半局模式可改用 clip 參數")

    print(f"[1/4] 向 MLB Stats API 撈取 game_pk={args.game_pk} 的逐球資料...")
    feed = fetch_game_feed(args.game_pk)
    markers = extract_half_inning_markers(feed)
    events = extract_key_events(
        feed,
        keywords=tuple(args.keywords),
        include_all_events=not args.key_events_only,
    )
    print(f"      取得 {len(markers)} 個半局起始時間、{len(events)} 個候選精華事件")
    content = fetch_game_content(args.game_pk)
    official_highlights = extract_official_highlights(content, args.game_pk)
    print(f"      content API 取得 {len(official_highlights)} 個官方精華影片")

    if args.clip_inning is not None:
        marker = marker_for_half(markers, args.clip_inning, args.clip_half)
        if args.auto_anchor:
            args.anchor_video_seconds = detect_clip_anchor_in_video(args.video)
            print(
                f"      自動偵測到片段 anchor：影片 {args.anchor_video_seconds:.1f} 秒"
            )
        video_timestamps = [args.anchor_video_seconds]
        anchor_source = "自動偵測" if args.auto_anchor else "人工"
        print(
            f"[2/4] 使用第 {args.clip_inning} 局 {args.clip_half} 的{anchor_source} anchor "
            f"（影片 {args.anchor_video_seconds:.1f} 秒）"
        )
        print("[3/4] 依指定半局計算時間偏移量...")
        offsets = align_markers_to_video([marker], video_timestamps)
        events = [
            event for event in events
            if event.inning == args.clip_inning and event.half == args.clip_half
        ]
    else:
        print(f"[2/4] 掃描影片 {args.video}，偵測比分板 ROI 的半局切換點...")
        video_timestamps = detect_half_inning_changes_in_video(
            args.video,
            roi=tuple(args.roi),
            sample_fps=args.sample_fps,
            diff_threshold=args.diff_threshold,
            min_gap_seconds=args.min_gap_seconds,
        )
        print(f"      偵測到 {len(video_timestamps)} 個候選切換點")

        print("[3/4] 配對官方半局時間與影片切換點，計算每個半局的時間偏移量...")
        offsets = align_markers_to_video(markers, video_timestamps)

    print(f"[4/4] 套用偏移量，換算所有精華事件的影片秒數，輸出至 {args.out} ...")
    rows = []
    for event in events:
        offset = offset_for_event(offsets, event)
        predicted_video_time = (
            event.event_time.timestamp() + offset if offset is not None else None
        )
        audio_calibrated_time = None
        if args.audio_calibrate and predicted_video_time is not None:
            audio_calibrated_time = calibrate_event_with_audio(
                args.video,
                predicted_video_time,
                search_before=args.audio_search_before,
                search_after=args.audio_search_after,
            )
        official_highlight = match_official_highlight(event, official_highlights)

        rows.append({
            "inning": event.inning,
            "half": event.half,
            "event_type": event.event_type,
            "description": event.description,
            "is_scoring_play": event.is_scoring_play,
            "predicted_video_seconds": (
                f"{predicted_video_time:.1f}" if predicted_video_time is not None else ""
            ),
            "audio_calibrated_video_seconds": (
                f"{audio_calibrated_time:.1f}" if audio_calibrated_time is not None else ""
            ),
            "audio_offset_seconds": (
                f"{audio_calibrated_time - predicted_video_time:.1f}"
                if audio_calibrated_time is not None and predicted_video_time is not None
                else ""
            ),
            "is_official_highlight": official_highlight is not None,
            "official_highlight_title": (
                official_highlight.title if official_highlight is not None else ""
            ),
            "official_highlight_duration": (
                f"{official_highlight.duration_seconds:.1f}"
                if official_highlight is not None and official_highlight.duration_seconds is not None
                else ""
            ),
            "official_highlight_url": (
                official_highlight.playback_url if official_highlight is not None else ""
            ),
        })

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "inning", "half", "event_type", "description",
            "is_scoring_play", "predicted_video_seconds",
            "audio_calibrated_video_seconds", "audio_offset_seconds",
            "is_official_highlight", "official_highlight_title",
            "official_highlight_duration", "official_highlight_url",
        ])
        writer.writeheader()
        writer.writerows(rows)

    matched = sum(1 for r in rows if r["predicted_video_seconds"])
    calibrated = sum(1 for r in rows if r["audio_calibrated_video_seconds"])
    print(f"完成！共輸出 {len(rows)} 筆事件，{matched} 筆成功換算出影片秒數。")
    if args.audio_calibrate:
        print(f"      音訊校準完成 {calibrated} 筆事件。")


if __name__ == "__main__":
    main()