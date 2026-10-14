#!/usr/bin/env python3
"""
mlb_events.py

【資料端】MLB 逐球資料 → 事件 ground truth。這個模組完全不碰影片（不 import cv2 / numpy）。

負責三件事：
    1. 抓取 MLB Stats API 的 /feed/live 與 /content，並把原始 JSON 存成快取（快照）。
       之後都從快照離線處理，結果可重現；要新增欄位也不必重新抓取。
    2. 把快照整理成「所有打席事件」表（UTC 時間），並配對官方精華影片。
    3. 提供事件篩選（投打對決、某投手三振集錦、得分／全壘打事件…），
       供本檔 CLI 與 mlb_highlight_groundtruth.py（影片對齊）共用。

單獨使用（不需要影片）：
  pip install requests

  # 全場所有打席事件
  python mlb_events.py --game-pk 823734 --out sport_hightlight/csv_data/823734_events.csv

  # 某投手的三振集錦
  python mlb_events.py --game-pk 823734 --pitcher "Skenes" --strikeouts --out skenes_k.csv

  # 某投打對決（名字用子字詞比對；也可以直接給 MLB player id）
  python mlb_events.py --game-pk 823734 --pitcher Skenes --batter Alonso --out matchup.csv

快取：預設存在 ./mlb_cache/{game_pk}.json；比賽進行中或想更新資料請加 --refresh。
"""

import argparse
import csv
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests


MLB_API_TEMPLATE = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
MLB_CONTENT_API_TEMPLATE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/content"
DEFAULT_CACHE_DIR = "mlb_cache"


# --------------------------------------------------------------------------
# 0. 資料結構
# --------------------------------------------------------------------------

@dataclass
class HalfInningMarker:
    inning: int
    half: str  # "top" or "bottom"
    start_time: datetime  # UTC


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
    batter_id: Optional[int] = None
    pitcher_id: Optional[int] = None
    at_bat_index: Optional[int] = None
    # "pitch"：最後一球的 startTime；"play_end"：沒有逐球資料，退回打席 endTime（誤差可能較大）
    time_source: str = "pitch"
    highlight: Optional[OfficialHighlight] = None
    # matched：配對到官方精華；ambiguous：有候選但同分無法判定；
    # none：content 有取得，但沒有配對；unknown：content API 取不到，無法判斷
    highlight_status: str = "unknown"


@dataclass
class GameData:
    game_pk: int
    fetched_at: str
    game_state: str
    markers: List[HalfInningMarker]
    events: List[GameEvent]  # 全部打席事件（未篩選），依時間排序
    highlights: Optional[List[OfficialHighlight]]  # None 表示 content API 取不到


# --------------------------------------------------------------------------
# 1. 抓取與快照
# --------------------------------------------------------------------------

def ensure_parent_dir(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def write_json_atomic(path: str, obj) -> None:
    ensure_parent_dir(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_csv_atomic(path: str, fieldnames: List[str], rows: List[dict]) -> None:
    """先寫暫存檔再替換，避免中途失敗時蓋掉舊的 CSV。"""
    ensure_parent_dir(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def _get_json(url: str, retries: int = 3, timeout: int = 30) -> dict:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:  # 用戶端錯誤（例如 game_pk 不存在）重試沒有意義
                break
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"無法取得 {url}：{last_exc}") from last_exc


def fetch_game_feed(game_pk: int) -> dict:
    return _get_json(MLB_API_TEMPLATE.format(game_pk=game_pk))


def fetch_game_content(game_pk: int) -> dict:
    return _get_json(MLB_CONTENT_API_TEMPLATE.format(game_pk=game_pk))


def load_snapshot(game_pk: int, cache_dir: str = DEFAULT_CACHE_DIR, refresh: bool = False) -> dict:
    """讀取（或建立）原始 API 快照：{game_pk, fetched_at, feed, content}。"""
    path = os.path.join(cache_dir, f"{game_pk}.json")
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            snapshot = json.load(f)
        print(f"      使用快取 {path}（API 抓取時間 {snapshot.get('fetched_at', '未知')}）")
        return snapshot

    print(f"      向 MLB Stats API 撈取 game_pk={game_pk} ...")
    feed = fetch_game_feed(game_pk)
    try:
        content = fetch_game_content(game_pk)
    except RuntimeError as exc:
        # content 只用來標記官方精華，取不到不應該讓整個流程失敗
        print(f"      警告：content API 失敗，官方精華狀態將標為 unknown（{exc}）")
        content = None
    snapshot = {
        "game_pk": game_pk,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feed": feed,
        "content": content,
    }
    write_json_atomic(path, snapshot)
    print(f"      已存快照 {path}")
    return snapshot


# --------------------------------------------------------------------------
# 2. 從快照整理出事件、半局標記、官方精華
# --------------------------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    # MLB API 的時間格式類似 "2026-09-13T19:12:03.000Z"；偶爾沒有小數秒
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"無法解析時間戳：{ts!r}")


def format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
            # 不改用該半局的第二個打席：那會讓錨點無聲無息地差上好幾分鐘
            print(f"      警告：第 {inning} 局 {half} 的第一個打席缺少 startTime，沒有此半局標記")
            continue
        markers.append(HalfInningMarker(
            inning=inning,
            half=half,
            start_time=parse_iso(start_time_str),
        ))
    markers.sort(key=lambda m: m.start_time)
    return markers


def extract_events(feed: dict) -> List[GameEvent]:
    """抓出「所有」打席事件，時間用該球的 playEvents.startTime，而非整個打席的時間。

    這裡不做任何篩選；要挑哪些事件請用 select_events()。這樣官方精華的配對永遠
    在完整事件集合上進行，不會因為先篩選過而把別的事件的精華誤配給入選事件。
    """
    events: List[GameEvent] = []
    all_plays = feed["liveData"]["plays"]["allPlays"]
    for play in all_plays:
        about = play["about"]
        inning = about["inning"]
        half = about.get("halfInning", "top")
        result = play.get("result", {})
        event_name = result.get("event", "")
        is_scoring = bool(about.get("isScoringPlay", False))

        # 找這個打席裡「造成結果」的那一球精確時間：優先用最後一個 isPitch 的 playEvent
        event_time = None
        time_source = "pitch"
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
            time_source = "play_end"

        matchup = play.get("matchup", {})
        batter = matchup.get("batter", {})
        pitcher = matchup.get("pitcher", {})
        events.append(GameEvent(
            inning=inning,
            half=half,
            description=result.get("description", event_name),
            event_type=event_name or ("Scoring Play" if is_scoring else "Unknown"),
            is_scoring_play=is_scoring,
            event_time=event_time,
            batter=batter.get("fullName", ""),
            pitcher=pitcher.get("fullName", ""),
            batter_id=batter.get("id"),
            pitcher_id=pitcher.get("id"),
            at_bat_index=about.get("atBatIndex"),
            time_source=time_source,
        ))
    events.sort(key=lambda e: e.event_time)
    return events


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
) -> Tuple[List[Optional[OfficialHighlight]], set]:
    """每支官方精華只指派給分數最高的事件；同分視為無法判定。

    回傳 (assigned, ambiguous)：assigned 與 events 一一對應；
    ambiguous 是曾經因同分而無法判定的事件 index。
    """
    assigned: List[Optional[OfficialHighlight]] = [None] * len(events)
    assigned_scores = [0] * len(events)
    ambiguous = set()
    for highlight in highlights:
        scores = [_match_score(event, highlight) for event in events]
        best_score = max(scores, default=0)
        if best_score == 0:
            continue
        if scores.count(best_score) > 1:
            ambiguous.update(i for i, s in enumerate(scores) if s == best_score)
            continue
        best_index = scores.index(best_score)
        if assigned_scores[best_index] >= best_score:
            continue
        assigned[best_index] = highlight
        assigned_scores[best_index] = best_score
    return assigned, ambiguous


def attach_highlights(events: List[GameEvent], highlights: Optional[List[OfficialHighlight]]) -> None:
    """把配對結果直接寫回每個事件的 highlight / highlight_status。"""
    if highlights is None:
        for event in events:
            event.highlight, event.highlight_status = None, "unknown"
        return
    assigned, ambiguous = assign_official_highlights(events, highlights)
    for i, (event, highlight) in enumerate(zip(events, assigned)):
        event.highlight = highlight
        if highlight is not None:
            event.highlight_status = "matched"
        elif i in ambiguous:
            event.highlight_status = "ambiguous"
        else:
            event.highlight_status = "none"


def load_game_data(
    game_pk: int,
    cache_dir: str = DEFAULT_CACHE_DIR,
    refresh: bool = False,
) -> GameData:
    """快照 → GameData（所有打席事件 + 半局標記 + 官方精華配對）。"""
    snapshot = load_snapshot(game_pk, cache_dir, refresh)
    feed = snapshot["feed"]
    game_state = feed.get("gameData", {}).get("status", {}).get("abstractGameState", "")
    if game_state and game_state != "Final":
        print(f"      警告：比賽狀態為 {game_state}（非 Final），快取資料可能不完整；可加 --refresh 更新")

    markers = extract_half_inning_markers(feed)
    events = extract_events(feed)
    content = snapshot.get("content")
    highlights = extract_official_highlights(content, game_pk) if content is not None else None
    attach_highlights(events, highlights)

    print(f"      取得 {len(markers)} 個半局起始時間、{len(events)} 個打席事件")
    if highlights is not None:
        matched = sum(1 for e in events if e.highlight_status == "matched")
        ambiguous = sum(1 for e in events if e.highlight_status == "ambiguous")
        print(
            f"      官方精華 {len(highlights)} 支：配對成功 {matched} 筆、"
            f"同分無法判定 {ambiguous} 筆"
        )
    return GameData(
        game_pk=game_pk,
        fetched_at=str(snapshot.get("fetched_at", "")),
        game_state=game_state,
        markers=markers,
        events=events,
        highlights=highlights,
    )


# --------------------------------------------------------------------------
# 3. 事件篩選（投打對決、某投手三振集錦、得分／全壘打…）
# --------------------------------------------------------------------------

def _name_matches(query: str, full_name: str, player_id: Optional[int]) -> bool:
    """query 為數字 → 比對 MLB player id；否則 query 的每個字都要出現在姓名裡。"""
    query = query.strip()
    if query.isdigit():
        return player_id is not None and int(query) == player_id
    wanted = _tokens(query)
    have = _tokens(full_name)
    return bool(wanted) and all(token in have for token in wanted)


def select_events(
    events: List[GameEvent],
    *,
    pitcher: Optional[str] = None,
    batter: Optional[str] = None,
    strikeouts: bool = False,
    key_events_only: bool = False,
    keywords: Tuple[str, ...] = ("home run",),
) -> List[GameEvent]:
    """所有條件取 AND；全部不指定就原樣回傳。

    常見組合：
        投打對決        pitcher=..., batter=...
        某投手三振集錦  pitcher=..., strikeouts=True
        得分／全壘打    key_events_only=True

    要新增篩選條件（某一局、某種結果…），在下面多加一個 `if ...: continue` 即可。
    """
    selected = []
    for event in events:
        if pitcher and not _name_matches(pitcher, event.pitcher, event.pitcher_id):
            continue
        if batter and not _name_matches(batter, event.batter, event.batter_id):
            continue
        if strikeouts and "strikeout" not in event.event_type.lower():
            continue
        if key_events_only and not (
            event.is_scoring_play
            or any(k.lower() in event.event_type.lower() for k in keywords)
        ):
            continue
        selected.append(event)
    return selected


def add_selection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("事件篩選（條件取 AND；都不指定則輸出所有打席事件）")
    group.add_argument("--pitcher", type=str, help="只要這位投手的打席；姓名子字詞（如 Skenes）或 MLB player id")
    group.add_argument("--batter", type=str, help="只要這位打者的打席；姓名子字詞或 MLB player id")
    group.add_argument("--strikeouts", action="store_true", help="只要三振事件")
    group.add_argument(
        "--key-events-only",
        action="store_true",
        help="只要得分事件，或事件名稱含 --keywords 的事件",
    )
    group.add_argument(
        "--keywords", type=str, nargs="*", default=["home run"],
        help="搭配 --key-events-only：比對事件名稱（result.event）的關鍵字（預設 home run）",
    )


def select_from_args(events: List[GameEvent], args: argparse.Namespace) -> List[GameEvent]:
    selected = select_events(
        events,
        pitcher=args.pitcher,
        batter=args.batter,
        strikeouts=args.strikeouts,
        key_events_only=args.key_events_only,
        keywords=tuple(args.keywords),
    )
    if not selected and (args.pitcher or args.batter):
        pitchers = sorted({e.pitcher for e in events if e.pitcher})
        batters = sorted({e.batter for e in events if e.batter})
        print("      警告：沒有任何事件符合篩選條件。這場比賽出現過的投手："
              + "、".join(pitchers[:30]) + (" ..." if len(pitchers) > 30 else ""))
        if args.batter:
            print("      打者：" + "、".join(batters[:40]) + (" ..." if len(batters) > 40 else ""))
    return selected


# --------------------------------------------------------------------------
# 4. 單獨輸出事件表（UTC，不含任何影片資訊）
# --------------------------------------------------------------------------

EVENT_FIELDS = [
    "game_pk", "at_bat_index", "inning", "half", "event_type", "description",
    "is_scoring_play", "event_time_utc", "time_source",
    "batter", "batter_id", "pitcher", "pitcher_id",
    "highlight_status", "official_highlight_title",
    "official_highlight_duration", "official_highlight_url",
]


def event_to_row(game_pk: int, event: GameEvent) -> dict:
    highlight = event.highlight
    return {
        "game_pk": game_pk,
        "at_bat_index": "" if event.at_bat_index is None else event.at_bat_index,
        "inning": event.inning,
        "half": event.half,
        "event_type": event.event_type,
        "description": event.description,
        "is_scoring_play": event.is_scoring_play,
        "event_time_utc": format_iso(event.event_time),
        "time_source": event.time_source,
        "batter": event.batter,
        "batter_id": "" if event.batter_id is None else event.batter_id,
        "pitcher": event.pitcher,
        "pitcher_id": "" if event.pitcher_id is None else event.pitcher_id,
        "highlight_status": event.highlight_status,
        "official_highlight_title": highlight.title if highlight else "",
        "official_highlight_duration": (
            f"{highlight.duration_seconds:.1f}"
            if highlight is not None and highlight.duration_seconds is not None else ""
        ),
        "official_highlight_url": highlight.playback_url if highlight else "",
    }


def main():
    parser = argparse.ArgumentParser(description="從 MLB Stats API 產生事件 ground truth（不需要影片）")
    parser.add_argument("--game-pk", type=int, required=True, help="MLB Stats API 的 game_pk（例如 823734）")
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR, help="原始 API 快照目錄（預設 mlb_cache）")
    parser.add_argument("--refresh", action="store_true", help="忽略快取，重新向 API 抓取並覆寫快照")
    add_selection_args(parser)
    parser.add_argument("--out", type=str, default="events.csv", help="輸出 CSV 路徑")
    args = parser.parse_args()

    ensure_parent_dir(args.out)
    print(f"[1/2] 載入 game_pk={args.game_pk} 的 MLB 資料...")
    data = load_game_data(args.game_pk, args.cache_dir, args.refresh)
    selected = select_from_args(data.events, args)

    print(f"[2/2] 輸出 {len(selected)} / {len(data.events)} 筆事件至 {args.out} ...")
    write_csv_atomic(args.out, EVENT_FIELDS, [event_to_row(args.game_pk, e) for e in selected])
    print("完成！")


if __name__ == "__main__":
    main()
