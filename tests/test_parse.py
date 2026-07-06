"""パーサ単体テスト(保存済み fixtures ベース、ネットワーク不要)。

実行: python -m pytest tests/ -q  または  python -m unittest discover tests
"""

import json
import re
import unittest
from pathlib import Path

from scraper.db import canonical_combo, combo_from_api_key
from scraper.enumerate_races import parse_calendar, parse_race_list
from scraper.fetch_odds import parse_odds_payload, place_odds_map, win_odds_map
from scraper.parse_result import parse_result_page
from scraper.parse_shutuba import parse_shutuba

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_jsonp(name):
    text = load(name)
    return json.loads(re.match(r"^[^(]+\(([\s\S]+)\)\s*;?\s*$", text.strip()).group(1))


class TestCanonicalCombo(unittest.TestCase):
    def test_unordered_sorted(self):
        self.assertEqual(canonical_combo(4, [11, 7]), "07-11")   # 馬連
        self.assertEqual(canonical_combo(7, [14, 7, 11]), "07-11-14")  # 3連複
        self.assertEqual(canonical_combo(3, [1, 1]), "01-01")    # 枠連ゾロ目

    def test_ordered_preserved(self):
        self.assertEqual(canonical_combo(6, [11, 7]), "11-07")   # 馬単
        self.assertEqual(canonical_combo(8, [7, 11, 14]), "07-11-14")  # 3連単

    def test_api_key(self):
        self.assertEqual(combo_from_api_key(4, "0102"), "01-02")
        self.assertEqual(combo_from_api_key(8, "110714"), "11-07-14")
        self.assertEqual(combo_from_api_key(1, "07"), "07")


class TestCalendar(unittest.TestCase):
    def test_parse_calendar_2025_01(self):
        days = parse_calendar(load("calendar_2025_01.html"), 2025, 1)
        self.assertEqual(days, [
            "20250105", "20250106", "20250111", "20250112", "20250113",
            "20250118", "20250119", "20250125", "20250126",
        ])


class TestRaceList(unittest.TestCase):
    def test_parse_race_list_20250105(self):
        races = parse_race_list(load("race_list_20250105.html"), "20250105")
        # 2025-01-05 は中山・中京の2場 × 12R = 24レース
        self.assertEqual(len(races), 24)
        venues = {r["venue_name"] for r in races}
        self.assertEqual(venues, {"中山", "中京"})
        r1 = races[0]
        self.assertEqual(r1["race_id"], "202506010101")
        self.assertEqual(r1["venue_code"], "06")
        self.assertEqual(r1["kai"], 1)
        self.assertEqual(r1["day"], 1)
        self.assertEqual(r1["race_no"], 1)
        self.assertEqual(r1["kaisai_date"], "20250105")


class TestResultPage(unittest.TestCase):
    def test_parse_result_201605021211(self):
        meta, entries, payouts, warnings = parse_result_page(load("result_201605021211.html"))

        self.assertEqual(meta["race_name"], "薫風S")
        self.assertEqual(meta["course"], "ダ")
        self.assertEqual(meta["distance"], 1400)
        self.assertEqual(meta["n_horses"], 16)

        self.assertEqual(len(entries), 16)
        winner = next(e for e in entries if e["finish_pos"] == 1)
        self.assertEqual(winner["horse_num"], 7)
        self.assertEqual(winner["horse_name"], "マッチレスヒーロー")
        self.assertEqual(winner["waku"], 4)

        pay = {(p["bet_type"], p["combo"]): p for p in payouts}
        self.assertEqual(pay[(1, "07")]["payout_yen"], 1320)       # 単勝
        self.assertEqual(pay[(1, "07")]["popularity"], 8)
        self.assertEqual(pay[(2, "07")]["payout_yen"], 290)        # 複勝 3頭
        self.assertEqual(pay[(2, "11")]["payout_yen"], 340)
        self.assertEqual(pay[(2, "14")]["payout_yen"], 180)
        self.assertEqual(pay[(3, "04-06")]["payout_yen"], 2960)    # 枠連
        self.assertEqual(pay[(4, "07-11")]["payout_yen"], 8920)    # 馬連
        self.assertEqual(pay[(5, "07-11")]["payout_yen"], 2490)    # ワイド 3組
        self.assertEqual(pay[(5, "07-14")]["payout_yen"], 1040)
        self.assertEqual(pay[(5, "11-14")]["payout_yen"], 1130)
        self.assertEqual(pay[(6, "07-11")]["payout_yen"], 18190)   # 馬単
        self.assertEqual(pay[(7, "07-11-14")]["payout_yen"], 11820)  # 3連複
        self.assertEqual(pay[(8, "07-11-14")]["payout_yen"], 118200)  # 3連単
        self.assertEqual(pay[(8, "07-11-14")]["popularity"], 451)

        self.assertEqual(warnings, [])

    def test_parse_entry_features(self):
        _, entries, _, _ = parse_result_page(load("result_201605021211.html"))
        # 市場オッズに依存しないファンダメンタル特徴量が全頭抽出できること
        for key in ("horse_id", "sex", "age", "kinryo", "horse_weight", "weight_diff",
                    "jockey", "trainer", "affiliation", "finish_time_sec", "agari3f"):
            self.assertTrue(all(e[key] is not None for e in entries), f"{key} 欠損あり")
        winner = next(e for e in entries if e["finish_pos"] == 1)
        self.assertEqual(winner["horse_id"], "2011105967")
        self.assertEqual(winner["sex"], "牡")
        self.assertEqual(winner["age"], 5)
        self.assertEqual(winner["kinryo"], 57.0)
        self.assertEqual(winner["horse_weight"], 468)
        self.assertEqual(winner["weight_diff"], -6)
        self.assertEqual(winner["jockey"], "戸崎圭")
        self.assertEqual(winner["affiliation"], "美浦")
        self.assertEqual(winner["trainer"], "金成")
        self.assertEqual(winner["finish_time_sec"], 83.6)  # 1:23.6
        self.assertEqual(winner["agari3f"], 35.4)

    def test_parse_corner_positions(self):
        from scraper.parse_result import parse_corner_positions
        cp = parse_corner_positions(load("result_201605021211.html"))
        self.assertEqual(len(cp), 16)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in cp.values()))
        self.assertEqual(min(cp, key=cp.get), 12)   # 4角先頭は馬12
        self.assertEqual(cp[12], 0.0)
        self.assertEqual(max(cp, key=cp.get), 15)   # 最後方は馬15
        # parse_result_page が entries に corner_pos を付けること
        _, entries, _, _ = parse_result_page(load("result_201605021211.html"))
        self.assertTrue(all(e.get("corner_pos") is not None for e in entries))


class TestShutuba(unittest.TestCase):
    def test_parse_shutuba(self):
        horses = parse_shutuba(load("shutuba_201605021211.html"))
        self.assertEqual(len(horses), 16)
        for key in ("horse_num", "horse_id", "sex", "age", "kinryo",
                    "horse_weight", "weight_diff", "jockey"):
            self.assertTrue(all(h[key] is not None for h in horses), f"{key} 欠損")
        h1 = horses[0]
        self.assertEqual(h1["horse_num"], 1)
        self.assertEqual(h1["horse_id"], "2010102853")
        self.assertEqual(h1["sex"], "牡")
        self.assertEqual(h1["kinryo"], 57.0)
        self.assertEqual(h1["horse_weight"], 480)
        self.assertEqual(h1["weight_diff"], -2)
        self.assertEqual(h1["jockey"], "柴田善")


class TestOddsApi(unittest.TestCase):
    def test_win_odds(self):
        odds, dt = parse_odds_payload(load_jsonp("odds_201605021211_type1.jsonp"), 1)
        self.assertEqual(dt, "2016-05-29 16:37:28")
        self.assertEqual(len(odds), 16)
        self.assertEqual(odds["07"][0], 13.2)  # 勝ち馬の確定単勝
        self.assertEqual(odds["07"][2], 8)     # 人気
        wmap = win_odds_map(odds)
        self.assertEqual(wmap[7], (13.2, 8))

    def test_place_odds_range(self):
        odds, _ = parse_odds_payload(load_jsonp("odds_201605021211_type2.jsonp"), 2)
        self.assertEqual(len(odds), 16)
        mn, mx = place_odds_map(odds)[14]
        self.assertEqual((mn, mx), (1.5, 2.0))

    def test_umaren_comma_and_combo(self):
        odds, _ = parse_odds_payload(load_jsonp("odds_201605021211_type4.jsonp"), 4)
        self.assertEqual(len(odds), 120)  # 16C2
        self.assertEqual(odds["01-16"][0], 1666.7)  # "1,666.7" のカンマ除去
        # 払戻 8,920円 ≒ 馬連オッズ 89.2倍 × 100
        self.assertAlmostEqual(odds["07-11"][0], 89.2, places=1)

    def test_sanrentan_full(self):
        odds, _ = parse_odds_payload(load_jsonp("odds_201605021211_type8.jsonp"), 8)
        self.assertEqual(len(odds), 16 * 15 * 14)  # 3360通り
        # 払戻 118,200円 ≒ 1182.0倍 × 100
        self.assertAlmostEqual(odds["07-11-14"][0], 1182.0, places=1)


if __name__ == "__main__":
    unittest.main()
