"""Game, video and environment info for the page (get_game, list_videos)."""

import csv
import importlib.util
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import REPO_ROOT

FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
LFS_PLACEHOLDER_MAX_BYTES = 1024


class GameNotFound(Exception):
    pass


def side_of_batter(half: str) -> str:
    return "away" if half == "top" else "home"


def other_side(side: str) -> str:
    return "home" if side == "away" else "away"


def game_from_feed(feed: Dict[str, Any], game_pk: int) -> Dict[str, Any]:
    teams_raw = feed.get("gameData", {}).get("teams", {})
    teams = [
        {"side": side, "name": teams_raw.get(side, {}).get("name", side.title()),
         "abbreviation": teams_raw.get(side, {}).get("abbreviation", "")}
        for side in ("away", "home")
    ]
    players: Dict[int, Dict[str, Any]] = {}
    types: Counter = Counter()
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    pa = 0
    play_list: List[Dict[str, Any]] = []
    for play in plays:
        event = play.get("result", {}).get("event")
        if not event:
            continue
        pa += 1
        types[event] += 1
        half = play.get("about", {}).get("halfInning", "top")
        matchup = play.get("matchup", {})
        play_list.append({
            "inning": play.get("about", {}).get("inning"), "half": half, "event_type": event,
            "batter": matchup.get("batter", {}).get("fullName", ""),
            "pitcher": matchup.get("pitcher", {}).get("fullName", ""),
            "is_scoring_play": bool(play.get("about", {}).get("isScoringPlay")),
            "official_highlight": None,
        })
        for role, side in (("batter", side_of_batter(half)), ("pitcher", other_side(side_of_batter(half)))):
            person = matchup.get(role, {})
            pid = person.get("id")
            if pid is None:
                continue
            entry = players.setdefault(pid, {"id": pid, "full_name": person.get("fullName", ""), "side": side, "roles": []})
            if role not in entry["roles"]:
                entry["roles"].append(role)
    return {
        "game_pk": game_pk,
        "date": feed.get("gameData", {}).get("datetime", {}).get("officialDate"),
        "teams": teams,
        "players": sorted(players.values(), key=lambda p: (p["side"], p["full_name"])),
        "play_types": [{"event_type": k, "count": v} for k, v in types.most_common()],
        "plate_appearances": pa,
        "plays": play_list,
        "source": "mlb_api",
    }


def game_from_csv(game_pk: int) -> Dict[str, Any]:
    from .runner import demo_source_csv, with_batters, StepFailed

    try:
        path = demo_source_csv(game_pk)
    except StepFailed as exc:
        raise GameNotFound(str(exc)) from exc
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = with_batters([r for r in csv.DictReader(f) if str(r.get("game_pk")) == str(game_pk)])
    away, home = "Away", "Home"
    m = re.search(r"_([A-Z][a-z]+)_([A-Z][a-z]+)_\d{4}", rows[0].get("video", "")) if rows else None
    if m:
        away, home = m.group(1), m.group(2)
    players: Dict[str, Dict[str, Any]] = {}
    for i, r in enumerate(rows):
        name = r.get("batter")
        if name:
            players.setdefault(name, {"id": i, "full_name": name, "side": side_of_batter(r.get("half", "")), "roles": ["batter"]})
    types = Counter(r.get("event_type", "") for r in rows)
    return {
        "game_pk": game_pk,
        "date": None,
        "teams": [{"side": "away", "name": away, "abbreviation": ""}, {"side": "home", "name": home, "abbreviation": ""}],
        "players": sorted(players.values(), key=lambda p: (p["side"], p["full_name"])),
        "play_types": [{"event_type": k, "count": v} for k, v in types.most_common()],
        "plate_appearances": len(rows),
        "plays": [{
            "inning": int(r.get("inning") or 0), "half": r.get("half", ""), "event_type": r.get("event_type", ""),
            "batter": r.get("batter", ""), "pitcher": r.get("pitcher", ""),
            "is_scoring_play": str(r.get("is_scoring_play", "")).lower() == "true",
            "official_highlight": str(r.get("is_official_highlight", "")).lower() == "true",
        } for r in rows],
        "source": "local_csv",
        "source_file": path.relative_to(REPO_ROOT).as_posix(),
    }


def merge_saved_highlights(game: Dict[str, Any]) -> Dict[str, Any]:
    """The live feed doesn't say which plays MLB published as highlights; a saved play list does."""
    try:
        saved = game_from_csv(game["game_pk"])["plays"]
    except GameNotFound:
        return game
    plays = game["plays"]
    if len(saved) == len(plays) and all(
        a["inning"] == b["inning"] and a["half"] == b["half"] and a["event_type"] == b["event_type"]
        for a, b in zip(plays, saved)
    ):
        for a, b in zip(plays, saved):
            a["official_highlight"] = b["official_highlight"]
    return game


def get_game(game_pk: int, offline: bool = False, timeout: float = 8.0) -> Dict[str, Any]:
    if not offline:
        try:
            req = urllib.request.Request(FEED_URL.format(game_pk=game_pk), headers={"User-Agent": "highlight-builder"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return merge_saved_highlights(game_from_feed(json.load(resp), game_pk))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise GameNotFound(f"The MLB API has no game with number {game_pk}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass  # offline: fall back to a saved play list
    return game_from_csv(game_pk)


def probe(path: Path) -> Dict[str, Optional[float]]:
    if not shutil.which("ffprobe"):
        return {"duration_seconds": None, "width": None, "height": None}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        return {"duration_seconds": float(data.get("format", {}).get("duration") or 0) or None,
                "width": stream.get("width"), "height": stream.get("height")}
    except (subprocess.SubprocessError, ValueError, OSError):
        return {"duration_seconds": None, "width": None, "height": None}


def list_videos() -> List[Dict[str, Any]]:
    out = []
    for path in sorted((REPO_ROOT / "video").glob("**/*.mp4")):
        size = path.stat().st_size
        placeholder = size < LFS_PLACEHOLDER_MAX_BYTES
        info = {"duration_seconds": None, "width": None, "height": None} if placeholder else probe(path)
        out.append({"path": path.relative_to(REPO_ROOT).as_posix(), "size_bytes": size, "lfs_placeholder": placeholder, **info})
    return out


def environment(tools_python: str) -> Dict[str, Any]:
    deps = {}
    for mod in ("requests", "cv2", "numpy"):
        try:
            r = subprocess.run([tools_python, "-c", f"import {mod}"], capture_output=True, timeout=20)
            deps[mod] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            deps[mod] = False
    videos = list_videos()
    return {
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "tools_python": tools_python,
        "python_packages": deps,
        "real_videos": [v["path"] for v in videos if not v["lfs_placeholder"]],
        "lfs_placeholders": [v["path"] for v in videos if v["lfs_placeholder"]],
    }
