"""late money 収集・解析の単体テスト(ネットワーク不要)。"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper.enumerate_races import parse_race_times
from scraper.poll_odds import build_plan, JST
from evmodel.latemoney import race_late_features

FIXTURES = Path(__file__).parent / "fixtures"


class TestPostTimes(unittest.TestCase):
    def test_parse_race_times(self):
        html = (FIXTURES / "race_list_20250105.html").read_text(encoding="utf-8")
        times = parse_race_times(html, "20250105")
        self.assertEqual(len(times), 24)
        self.assertRegex(next(iter(times.values())), r"^\d{1,2}:\d{2}$")
        self.assertEqual(times["202506010101"], "10:05")


class TestPollPlan(unittest.TestCase):
    def test_build_plan_orders_and_filters(self):
        times = {"202606010111": "15:40", "202606010112": "16:10"}
        now = datetime(2026, 7, 11, 14, 0, tzinfo=JST)
        plan = build_plan(times, "20260711", [60, 30, 15, 10, 5, 3, 1], now)
        # 15:40 の 60分前=14:40 は未来 → 含む。過去の撃ち時刻は無し
        self.assertTrue(all(t > now for t, _, _ in plan))
        # 時刻昇順
        self.assertEqual(plan, sorted(plan, key=lambda t: t[0]))
        # 2レース×7オフセット=14(全て未来)
        self.assertEqual(len(plan), 14)

    def test_build_plan_excludes_past(self):
        times = {"202606010111": "15:40"}
        now = datetime(2026, 7, 11, 15, 30, tzinfo=JST)  # 発走10分前
        plan = build_plan(times, "20260711", [60, 30, 15, 10, 5, 3, 1], now)
        # 未来の撃ち時刻は 5分前(15:35)・3分前(15:37)・1分前(15:39) の3つ
        self.assertEqual(len(plan), 3)


class TestLateFeatures(unittest.TestCase):
    def test_steamed_horse_negative_drift(self):
        # 遠い時点→近い時点。馬7が 8.0→4.0 に短縮(売れた)、馬1は 2.0→2.2(緩む)
        snaps = [
            (30, {7: 8.0, 1: 2.0}),   # 30分前
            (10, {7: 6.0, 1: 2.1}),   # 10分前
            (1, {7: 4.0, 1: 2.2}),    # 1分前
        ]
        feats = race_late_features(snaps)
        self.assertLess(feats[7]["drift"], -0.5)   # 大きく短縮
        self.assertGreater(feats[1]["drift"], 0)   # 緩んだ
        self.assertLess(feats[7]["late_drop"], 0)  # 直近区間でも短縮

    def test_needs_two_snapshots(self):
        self.assertEqual(race_late_features([(5, {1: 2.0})]), {})


if __name__ == "__main__":
    unittest.main()
