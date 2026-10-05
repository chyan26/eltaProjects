#!/usr/bin/env python3
"""
mlb_highlight_groundtruth.py

產生棒球轉播影片的事件 ground truth 時間軸。

流程：
    1. 從 MLB Stats API 的 /feed/live 取得所有逐球事件與官方時間。
    2. 從 /content 取得官方精華影片，將其配對到逐球事件並標記。
    3. 以半局 anchor 將事件時間換算成影片秒數。
    4. 輸出包含所有事件與官方精華標籤的 CSV。

單一半局模式使用 --auto-anchor 自動找影片起始時間；完整轉播模式則用 --game-offset-seconds
指定第一個打席在影片中的秒數，連續錄影時全場共用。--audio-calibrate 可在事件
附近用音訊上升沿提供額外校準值。

需求套件：
  pip install requests opencv-python numpy

完整轉播使用方式：
  python mlb_highlight_groundtruth.py \
      --game-pk 823734 \
      --video video/eltaMax10_Reds_Brewers_0913.mp4 \
      --game-offset-seconds 150.5 \
      --out sport_hightlight/csv_data/full_game_ground_truth.csv

單一半局片段使用方式：
    python mlb_highlight_groundtruth.py \
            --game-pk 823734 \
            --video bottom2.mp4 \
            --clip-inning 2 \
            --clip-half bottom \
            --auto-anchor \
            --out bottom2_ground_truth.csv

若轉播版型不同，可用 --anchor-video-seconds 手動指定半局起始秒數。
"""

import argparse
import csv
import re
import subprocess
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
    batter: str = ""
    pitcher: str = ""


@dataclass
class OfficialHighlight:
    highlight_id: str
    title: str
    description: str
    duration_seconds: Optional[float]
    playback_url: str
    inning: Optional[int]
    half: Optional[str]
    slug: str = ""  # content 的 id，多半是 play 描述或「投手-in-play-打者」
    players: Tuple[str, ...] = ()


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
        r"\bin the (\d+)(?:st|nd|rd|th)\b",
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
            slug=highlight_id,
            players=tuple(
                str(k.get("displayName", "")) for k in keywords
                if k.get("type") == "player_id"
            ),
        ))
    return highlights


def _normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", _normalise_text(text))


def _contains(tokens: List[str], sub: List[str]) -> bool:
    n = len(sub)
    return n > 0 and any(tokens[i:i + n] == sub for i in range(len(tokens) - n + 1))


def _slug_matches_description(slug: List[str], desc: List[str]) -> bool:
    """slug 是 play 描述的開頭；結尾字可能被截斷，後面可能多出雜湊碼。"""
    matched = 0
    for s, d in zip(slug, desc):
        if s == d:
            matched += 1
        else:
            matched += d.startswith(s)
            break
    return matched >= 4 and len(slug) - matched <= 2


def _match_score(event: GameEvent, highlight: OfficialHighlight) -> int:
    """依官方精華 id 與球員關鍵字比對 play；0 表示不配對。"""
    if highlight.inning is not None and highlight.inning != event.inning:
        return 0
    if highlight.half is not None and highlight.half != event.half:
        return 0

    slug = _tokens(highlight.slug)
    desc = _tokens(event.description)
    pitcher = _tokens(event.pitcher)
    desc_prefix = _slug_matches_description(slug, desc)
    batter_in_slug = _contains(slug, _tokens(event.batter))
    if not (desc_prefix or batter_in_slug):
        return 0

    score = 4 if desc_prefix else 0
    score += 2 if batter_in_slug else 0
    score += 2 if _contains(slug, pitcher) else 0
    for name in highlight.players:
        name_tokens = _tokens(name)
        if name_tokens == pitcher or _contains(desc, name_tokens):
            score += 1
    if "strike" in slug and "strikeout" in event.event_type.lower():
        score += 1
    return score


def assign_official_highlights(
    events: List[GameEvent],
    highlights: List[OfficialHighlight],
) -> List[Optional[OfficialHighlight]]:
    """每支官方精華只指派給分數最高的事件；同分視為無法判定。"""
    assigned: List[Optional[OfficialHighlight]] = [None] * len(events)
    assigned_scores = [0] * len(events)
    for highlight in highlights:
        scores = [_match_score(event, highlight) for event in events]
        best_score = max(scores, default=0)
        if best_score == 0 or scores.count(best_score) > 1:
            continue
        best_index = scores.index(best_score)
        if assigned_scores[best_index] >= best_score:
            continue
        assigned[best_index] = highlight
        assigned_scores[best_index] = best_score
    return assigned


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
            batter=play.get("matchup", {}).get("batter", {}).get("fullName", ""),
            pitcher=play.get("matchup", {}).get("pitcher", {}).get("fullName", ""),
        ))
    events.sort(key=lambda e: e.event_time)
    return events


# --------------------------------------------------------------------------
# 2. 在影片中偵測 live scorebug 出現的時間點
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


# --------------------------------------------------------------------------
# 3. 配對官方半局時間與影片偵測時間點，計算偏移量
# --------------------------------------------------------------------------

@dataclass
class InningOffset:
    inning: int
    half: str
    offset_seconds: float  # video_time = api_time_epoch_seconds + offset


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
    if args.clip_inning is None and args.game_offset_seconds is None:
        parser.error("完整影片模式必須提供 --game-offset-seconds")

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
        anchor_source = "自動偵測" if args.auto_anchor else "人工"
        print(
            f"[2/4] 使用第 {args.clip_inning} 局 {args.clip_half} 的{anchor_source} anchor "
            f"（影片 {args.anchor_video_seconds:.1f} 秒）"
        )
        print("[3/4] 依指定半局計算時間偏移量...")
        offsets = [InningOffset(
            inning=marker.inning,
            half=marker.half,
            offset_seconds=args.anchor_video_seconds - marker.start_time.timestamp(),
        )]
        events = [
            event for event in events
            if event.inning == args.clip_inning and event.half == args.clip_half
        ]
    else:
        print(f"[2/4] 完整影片模式：第一個打席位於影片 {args.game_offset_seconds:.1f} 秒")
        print("[3/4] 全場共用同一個時間偏移量...")
        game_offset = args.game_offset_seconds - markers[0].start_time.timestamp()
        offsets = [
            InningOffset(inning=m.inning, half=m.half, offset_seconds=game_offset)
            for m in markers
        ]

    print(f"[4/4] 套用偏移量，換算所有精華事件的影片秒數，輸出至 {args.out} ...")
    rows = []
    assigned_highlights = assign_official_highlights(events, official_highlights)
    for event, official_highlight in zip(events, assigned_highlights):
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

        rows.append({
            "game_pk": args.game_pk,
            "video": args.video,
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
            "game_pk", "video", "inning", "half", "event_type", "description",
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