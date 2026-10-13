import unittest
from unittest.mock import patch

from sport_hightlight.mlb_game_lookup import find_game_pks


class FindGamePksTests(unittest.TestCase):
    def test_accepts_common_white_sox_and_royals_abbreviations(self):
        game = {
            "gamePk": 824059,
            "officialDate": "2026-09-24",
            "teams": {
                "away": {
                    "team": {
                        "name": "Chicago White Sox",
                        "abbreviation": "CWS",
                    }
                },
                "home": {
                    "team": {
                        "name": "Kansas City Royals",
                        "abbreviation": "KC",
                    }
                },
            },
        }

        with patch("sport_hightlight.mlb_game_lookup._schedule_games", return_value=[game]):
            results = find_game_pks("2026-09-24", ["CHW", "KCR"])

        self.assertEqual([result["game_pk"] for result in results], [824059])

    def test_accepts_aliases_when_team_order_is_reversed(self):
        game = {
            "gamePk": 824059,
            "teams": {
                "away": {
                    "team": {
                        "name": "Chicago White Sox",
                        "abbreviation": "CWS",
                    }
                },
                "home": {
                    "team": {
                        "name": "Kansas City Royals",
                        "abbreviation": "KC",
                    }
                },
            },
        }

        with patch("sport_hightlight.mlb_game_lookup._schedule_games", return_value=[game]):
            results = find_game_pks("2026-09-24", ["KCR", "CHW"])

        self.assertEqual([result["game_pk"] for result in results], [824059])


if __name__ == "__main__":
    unittest.main()
