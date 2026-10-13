#!/usr/bin/env python3
"""
MLB game_pk 查詢工具：日期 + 對戰組合 <-> game_pk。

用法：
  # 由日期（與球隊）查 game_pk；球隊不分主客場，可用名稱片段或縮寫
  python mlb_game_lookup.py --date 2026-09-13 --teams reds brewers
  python mlb_game_lookup.py --date 2026-09-13            # 列出當天所有比賽

  # 由 game_pk 反查日期與對戰組合
  python mlb_game_lookup.py --game-pk 823734

日期為 MLB 的 officialDate（美國當地比賽日）。
"""

import argparse
from typing import List, Optional

import requests


MLB_SCHEDULE_API = "https://statsapi.mlb.com/api/v1/schedule"


def _schedule_games(params: dict) -> List[dict]:
    resp = requests.get(
        MLB_SCHEDULE_API,
        params={"sportId": 1, "hydrate": "team", **params},
        timeout=30,
    )
    resp.raise_for_status()
    return [g for d in resp.json().get("dates", []) for g in d.get("games", [])]


def _game_summary(game: dict) -> dict:
    away = game["teams"]["away"]["team"]
    home = game["teams"]["home"]["team"]
    return {
        "game_pk": game["gamePk"],
        "date": game.get("officialDate", ""),
        "start_time_utc": game.get("gameDate", ""),
        "away": away.get("name", ""),
        "home": home.get("name", ""),
        "away_abbr": away.get("abbreviation", ""),
        "home_abbr": home.get("abbreviation", ""),
        "status": game.get("status", {}).get("detailedState", ""),
        "game_number": game.get("gameNumber", 1),
    }


def _team_matches(query: str, summary: dict, side: str) -> bool:
    q = query.lower()
    return q == summary[f"{side}_abbr"].lower() or q in summary[side].lower()


def find_game_pks(date: str, teams: Optional[List[str]] = None) -> List[dict]:
    """以日期 (YYYY-MM-DD) 與球隊名稱/縮寫查詢 game_pk。

    teams 可給 0~2 個；球隊不分主客場。雙重賽會回傳多筆。
    """
    teams = teams or []
    results = []
    for game in _schedule_games({"date": date}):
        summary = _game_summary(game)
        if len(teams) == 2:
            ok = (
                _team_matches(teams[0], summary, "away") and _team_matches(teams[1], summary, "home")
            ) or (
                _team_matches(teams[0], summary, "home") and _team_matches(teams[1], summary, "away")
            )
        else:
            ok = all(
                _team_matches(t, summary, "away") or _team_matches(t, summary, "home") for t in teams
            )
        if ok:
            results.append(summary)
    return results


def lookup_game_by_pk(game_pk: int) -> Optional[dict]:
    games = _schedule_games({"gamePk": game_pk})
    return _game_summary(games[0]) if games else None


def _print_game(summary: dict) -> None:
    print(
        f"game_pk={summary['game_pk']}  {summary['date']}  "
        f"{summary['away']} @ {summary['home']}  "
        f"(game {summary['game_number']}, {summary['status']}, {summary['start_time_utc']})"
    )


def main():
    parser = argparse.ArgumentParser(description="MLB game_pk 與 日期/對戰組合 互相查詢")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="比賽日期（美國當地）")
    parser.add_argument("--teams", type=str, nargs="*", default=[], help="球隊名稱或縮寫，最多兩個，例如 reds brewers 或 CIN MIL")
    parser.add_argument("--game-pk", type=int, help="由 game_pk 反查日期與對戰組合")
    args = parser.parse_args()

    if (args.game_pk is None) == (args.date is None):
        parser.error("請擇一提供 --date 或 --game-pk")
    if len(args.teams) > 2:
        parser.error("--teams 最多兩個")

    if args.game_pk is not None:
        summary = lookup_game_by_pk(args.game_pk)
        if summary is None:
            parser.exit(1, f"找不到 game_pk={args.game_pk}\n")
        _print_game(summary)
        return

    found = find_game_pks(args.date, args.teams)
    if not found:
        parser.exit(1, "查無符合的比賽\n")
    for summary in found:
        _print_game(summary)


if __name__ == "__main__":
    main()
