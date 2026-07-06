"""Benter 二段階モデル: 独立ファンダメンタル勝率(Stage1) × 市場オッズ結合(Stage2)。

現行ツールとの決定的な違い:
  現行 : prob = 市場オッズの逆数を正規化  → EV = prob × odds = (1 - 控除率) < 1 で固定
  本手法: prob を市場から独立に推定 → 市場が過小評価した馬で p > q となり EV = p × odds > 1

Stage1  : x_i(斤量・馬体重・過去走指標…市場非使用)の条件付きロジット → π_i
較正    : isotonic で π を実現勝率に合わせる(賭博では精度より較正が効く)
Stage2  : [log π_i, log q_i] の条件付きロジット → p_i ∝ π_i^α q_i^β
判定    : EV_i = p_i × odds_i、EV > 1 + margin で購入、分数 Kelly で配分
"""

import math

from .condlogit import (
    ConditionalLogit, isotonic_apply, isotonic_fit, mcfadden_r2, null_nll,
)


class TwoStageModel:
    def __init__(self, l2=1e-3, lr=0.05, iters=400, calibrate=True):
        self.stage1 = ConditionalLogit(l2=l2, lr=lr, iters=iters)
        self.stage2 = ConditionalLogit(l2=1e-4, lr=0.05, iters=400)
        self.scaler = None
        self.iso = None
        self.calibrate = calibrate

    def fit(self, races, verbose=False):
        """races: [ {X:[[..]], q:[..], win:int} ] （X=Stage1特徴、q=市場勝率、win=勝ち馬idx）。"""
        from .condlogit import Standardizer
        # --- Stage1 ---
        all_rows = [x for r in races for x in r["X"]]
        self.scaler = Standardizer().fit(all_rows)
        s1_races = [(self.scaler.transform(r["X"]), r["win"]) for r in races]
        self.stage1.fit(s1_races, verbose=verbose)

        # Stage1 の生予測(較正・Stage2 用)
        pi_per_race = [self.stage1.predict_race(Xs) for Xs, _ in s1_races]

        # --- 較正(isotonic) ---
        if self.calibrate:
            pairs = []
            for (Xs, win), pi in zip(s1_races, pi_per_race):
                for i, p in enumerate(pi):
                    pairs.append((p, 1 if i == win else 0))
            self.iso = isotonic_fit(pairs)
            pi_per_race = [self._calib_norm(pi) for pi in pi_per_race]

        # --- Stage2: [log π, log q] の条件付きロジット ---
        s2_races = []
        for r, pi in zip(races, pi_per_race):
            X2 = [[math.log(max(pi[i], 1e-9)), math.log(max(r["q"][i], 1e-9))]
                  for i in range(len(pi))]
            s2_races.append((X2, r["win"]))
        self.stage2.fit(s2_races, verbose=verbose)
        return self

    def _calib_norm(self, pi):
        c = [max(isotonic_apply(self.iso, p), 1e-9) for p in pi]
        z = sum(c)
        return [x / z for x in c]

    def stage1_probs(self, X):
        pi = self.stage1.predict_race(self.scaler.transform(X))
        return self._calib_norm(pi) if self.calibrate else pi

    def predict(self, X, q):
        """Stage1特徴 X と市場勝率 q → 結合勝率 p(合計1)。"""
        pi = self.stage1_probs(X)
        X2 = [[math.log(max(pi[i], 1e-9)), math.log(max(q[i], 1e-9))] for i in range(len(pi))]
        return self.stage2.predict_race(X2)

    # -- 評価 --
    def edge_r2(self, races):
        """市場のみ vs Stage2 の McFadden R²、その差 ΔR²(市場超過エッジ)を返す。"""
        n0 = null_nll([(r["X"], r["win"]) for r in races])
        # 市場のみ(q をそのまま)
        nll_mkt = 0.0
        nll_two = 0.0
        for r in races:
            q = r["q"]
            nll_mkt += -math.log(max(q[r["win"]], 1e-12))
            p = self.predict(r["X"], q)
            nll_two += -math.log(max(p[r["win"]], 1e-12))
        nll_mkt /= len(races)
        nll_two /= len(races)
        return {
            "r2_market": mcfadden_r2(nll_mkt, n0),
            "r2_two_stage": mcfadden_r2(nll_two, n0),
            "delta_r2": mcfadden_r2(nll_two, n0) - mcfadden_r2(nll_mkt, n0),
            "nll_market": nll_mkt,
            "nll_two_stage": nll_two,
            "alpha": self.stage2.beta[0] if self.stage2.beta else None,
            "beta": self.stage2.beta[1] if self.stage2.beta else None,
        }
