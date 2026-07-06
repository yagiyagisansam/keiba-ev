"""二段階EVモデル(evmodel)の単体テスト。高速・決定的(ネットワーク/重依存なし)。"""

import random
import unittest

from evmodel.condlogit import (
    ConditionalLogit, Standardizer, softmax, isotonic_fit, isotonic_apply,
)
from evmodel.model import TwoStageModel
from evmodel.backtest import kelly_fraction, simulate_win_bets, bootstrap_ci


def make_synth(n=200, n_feat=4, market_noise=0.0, seed=1):
    rng = random.Random(seed)
    w = [rng.gauss(0, 1) for _ in range(n_feat)]
    races = []
    for _ in range(n):
        k = rng.randint(6, 10)
        X = [[rng.gauss(0, 1) for _ in range(n_feat)] for _ in range(k)]
        strength = [sum(w[j] * x[j] for j in range(n_feat)) for x in X]
        tp = softmax(strength)
        r = rng.random(); acc = 0.0; win = k - 1
        for i, p in enumerate(tp):
            acc += p
            if r <= acc:
                win = i; break
        q = softmax([strength[i] + rng.gauss(0, market_noise) for i in range(k)])
        odds = [(0.8 / max(q[i], 1e-6)) for i in range(k)]
        races.append({"X": X, "q": q, "win": win, "odds": odds})
    return races


class TestCondLogit(unittest.TestCase):
    def test_softmax_sums_to_one(self):
        s = softmax([1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(s), 1.0, places=9)
        self.assertTrue(s[2] > s[1] > s[0])

    def test_recovers_signal(self):
        # 特徴量0が勝敗を決める → beta[0] が明確に正になるはず
        rng = random.Random(3)
        races = []
        for _ in range(150):
            k = 8
            X = [[rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(k)]
            win = max(range(k), key=lambda i: X[i][0])  # 特徴0最大が必ず勝つ
            races.append(([list(x) for x in X], win))
        sc = Standardizer().fit([x for X, _ in races for x in X])
        races_s = [(sc.transform(X), w) for X, w in races]
        m = ConditionalLogit(iters=200, lr=0.1).fit(races_s)
        self.assertGreater(m.beta[0], 0.5)
        self.assertLess(m.nll(races_s), 1.5)

    def test_isotonic_monotone(self):
        pairs = [(0.1, 0), (0.2, 1), (0.15, 0), (0.9, 1), (0.8, 1), (0.05, 0)]
        model = isotonic_fit(pairs)
        ys = [isotonic_apply(model, x) for x in (0.05, 0.2, 0.5, 0.9)]
        self.assertTrue(all(ys[i] <= ys[i + 1] + 1e-9 for i in range(len(ys) - 1)))


class TestTwoStage(unittest.TestCase):
    def test_predict_normalized(self):
        races = make_synth(120, market_noise=0.5, seed=2)
        m = TwoStageModel(iters=120).fit(races)
        p = m.predict(races[0]["X"], races[0]["q"])
        self.assertAlmostEqual(sum(p), 1.0, places=6)
        self.assertEqual(len(p), len(races[0]["X"]))

    def test_edge_metrics_present(self):
        races = make_synth(150, market_noise=0.6, seed=4)
        m = TwoStageModel(iters=150).fit(races)
        e = m.edge_r2(races)
        for k in ("r2_market", "r2_two_stage", "delta_r2", "alpha", "beta"):
            self.assertIn(k, e)


class TestBacktest(unittest.TestCase):
    def test_kelly_fraction(self):
        self.assertEqual(kelly_fraction(0.1, 5.0), 0.0)          # EV=0.5<1 → 賭けない
        self.assertAlmostEqual(kelly_fraction(0.5, 3.0), 0.25)   # (1.5-1)/2

    def test_simulate_only_positive_ev(self):
        preds = [
            {"p": 0.5, "odds": 3.0, "won": True},    # EV1.5 → 買う, 的中
            {"p": 0.1, "odds": 2.0, "won": False},   # EV0.2 → 買わない
        ]
        r = simulate_win_bets(preds, margin=0.0, flat=True)
        self.assertEqual(r["n_bets"], 1)
        self.assertGreater(r["roi"], 1.0)

    def test_bootstrap_ci_bounds(self):
        picks = [{"odds": 3.0, "won": i % 3 == 0, "stake": 1.0} for i in range(60)]
        ci = bootstrap_ci(picks, n_boot=200, flat=True)
        self.assertLessEqual(ci["lo"], ci["mean"] + 1e-9)
        self.assertLessEqual(ci["mean"], ci["hi"] + 1e-9)


class TestMergeDB(unittest.TestCase):
    def test_merge_counts(self):
        import os, tempfile
        from scraper import db as dbmod
        from evmodel.mergedb import merge
        paths = []
        for k in range(2):
            p = tempfile.mktemp(suffix=f"_{k}.db")
            c = dbmod.open_db(p, year=2016 + k)
            c.execute("INSERT INTO races(race_id,kaisai_date,venue_code,status_result) "
                      "VALUES(?,?,?,1)", (f"20160000000{k}", "20160101", "05"))
            c.execute("INSERT INTO entries(race_id,horse_num,finish_pos,horse_id) "
                      "VALUES(?,1,1,?)", (f"20160000000{k}", f"H{k}"))
            c.commit(); c.close(); paths.append(p)
        dest = tempfile.mktemp(suffix="_all.db")
        merge(paths, dest)
        import sqlite3
        c = sqlite3.connect(dest)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM races").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        c.close()
        for p in paths + [dest]:
            os.path.exists(p) and os.unlink(p)


if __name__ == "__main__":
    unittest.main()
