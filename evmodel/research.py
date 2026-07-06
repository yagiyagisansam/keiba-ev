"""EV>1.0 を狙う「方式」を過去データで横並び検証する研究ハーネス。

目的: ①方式を作る → ②過去データで walk-forward 検証 → ③ダメなら別方式、のループを回す。
各方式は fit(races) と predict(X, q)->勝率 を実装するだけ。市場のみ/二段階/独立のみ/
市場キャリブレーション等を同一データ・同一検証で比較し、ROI・95%CI・ΔR² をランキングする。

  python -m evmodel.research --db keiba_all.db --min-train 400 --margin 0.05

判定: OOS 均等 ROI の 95%CI 下限 > 100% を安定して満たす方式が出れば「EV>1.0 達成」。
出なければ方式を足して再実行する(このファイルに Method を追加)。
"""

import argparse
import math

from .condlogit import isotonic_fit, isotonic_apply, null_nll, mcfadden_r2
from .model import TwoStageModel
from .features import load_races
from .backtest import simulate_win_bets, bootstrap_ci


# ---------------------------------------------------------------------------
# 方式(Method): fit(races) と predict(X, q) を実装する
# ---------------------------------------------------------------------------

class MarketOnly:
    """基準線: 市場勝率 q をそのまま使う(=現行ツールと同じ。EV=1-控除率に張り付く)。"""
    name = "market_only"
    def fit(self, races): return self
    def predict(self, X, q): return list(q)


class Stage1Only:
    """独立ファンダメンタルモデルの勝率のみ(市場 q を使わない)。"""
    name = "stage1_only"
    def __init__(self, **kw): self.m = TwoStageModel(**kw)
    def fit(self, races): self.m.fit(races); return self
    def predict(self, X, q): return self.m.stage1_probs(X)


class TwoStage:
    """Benter 二段階: p ∝ π^α · q^β。"""
    name = "two_stage"
    def __init__(self, **kw): self.m = TwoStageModel(**kw)
    def fit(self, races): self.m.fit(races); return self
    def predict(self, X, q): return self.m.predict(X, q)


class MarketCalibrated:
    """市場キャリブレーション(FLB 利用): 学習データで q→実現勝率の単調写像を推定し、
    その補正確率で EV を測る。JRA の含意確率に系統的な歪みがあれば正EVになりうる。
    market_only と違い、補正はホールドアウトで学習するので循環ではない。"""
    name = "market_calibrated"
    def __init__(self): self.iso = None
    def fit(self, races):
        pairs = []
        for r in races:
            for i, qi in enumerate(r["q"]):
                pairs.append((qi, 1 if i == r["win"] else 0))
        self.iso = isotonic_fit(pairs)
        return self
    def predict(self, X, q):
        c = [max(isotonic_apply(self.iso, qi), 1e-9) for qi in q]
        z = sum(c)
        return [x / z for x in c]


# ---------------------------------------------------------------------------
# walk-forward 検証(方式非依存。fit/predict だけ要求)
# ---------------------------------------------------------------------------

def evaluate(method_factory, races_sorted, *, n_folds=4, min_train=400, margin=0.05):
    N = len(races_sorted)
    if N < min_train + n_folds:
        folds = [(0, max(1, int(N * 0.7)), max(1, int(N * 0.7)), N)]
    else:
        step = (N - min_train) // n_folds
        folds, s = [], min_train
        while s < N:
            e = min(s + step, N)
            folds.append((0, s, s, e))
            s = e

    preds, nll_m, nll_model, n0_sum, n_races = [], 0.0, 0.0, 0.0, 0
    for (tr0, tr1, te0, te1) in folds:
        train = [r for r in races_sorted[tr0:tr1] if r["win"] is not None]
        test = [r for r in races_sorted[te0:te1] if r["win"] is not None]
        if not train or not test:
            continue
        m = method_factory().fit(train)
        for r in test:
            p = m.predict(r["X"], r["q"])
            w = r["win"]
            nll_m += -math.log(max(r["q"][w], 1e-12))
            nll_model += -math.log(max(p[w], 1e-12))
            n0_sum += math.log(len(r["X"]))
            n_races += 1
            for i in range(len(p)):
                preds.append({"p": p[i], "odds": r["odds"][i], "won": (i == w)})
    if n_races == 0:
        return None
    null = n0_sum / n_races
    r2_mkt = mcfadden_r2(nll_m / n_races, null)
    r2_mdl = mcfadden_r2(nll_model / n_races, null)
    sim = simulate_win_bets(preds, margin=margin, flat=True)
    ci = bootstrap_ci(sim["picks"], flat=True)
    return {
        "roi": sim["roi"], "n_bets": sim["n_bets"],
        "ci_lo": ci["lo"], "ci_hi": ci["hi"],
        "delta_r2": r2_mdl - r2_mkt, "r2_model": r2_mdl,
    }


METHODS = [
    ("market_only", lambda: MarketOnly()),
    ("market_calibrated", lambda: MarketCalibrated()),
    ("stage1_only", lambda: Stage1Only(l2=1e-3, lr=0.08, iters=300)),
    ("two_stage", lambda: TwoStage(l2=1e-3, lr=0.08, iters=300)),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="EV>1.0 を狙う方式の横並び検証")
    ap.add_argument("--db", required=True)
    ap.add_argument("--year", help="対象年で絞る")
    ap.add_argument("--min-train", type=int, default=400)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--margin", type=float, default=0.05)
    args = ap.parse_args(argv)

    races = load_races(args.db, limit_year=args.year)
    labeled = [r for r in races if r["win"] is not None]
    print(f"[research] {len(labeled)} レース(勝ち馬確定)で検証\n")

    rows = []
    for name, factory in METHODS:
        res = evaluate(factory, races, n_folds=args.folds,
                       min_train=args.min_train, margin=args.margin)
        if res:
            rows.append((name, res))
            print(f"  {name:20s} ROI={res['roi']*100:6.1f}%  "
                  f"CI[{res['ci_lo']*100:5.1f},{res['ci_hi']*100:6.1f}]  "
                  f"ΔR²={res['delta_r2']:+.4f}  賭け{res['n_bets']}点")

    print("\n=== ランキング(ROI降順) ===")
    rows.sort(key=lambda t: -t[1]["roi"])
    win = None
    for name, r in rows:
        flag = ""
        if r["ci_lo"] > 1.0 and r["n_bets"] >= 30:
            flag = " ★EV>1.0達成(CI下限>100%)"
            win = win or name
        print(f"  {name:20s} ROI={r['roi']*100:6.1f}%  CI下限={r['ci_lo']*100:.1f}%{flag}")
    if win:
        print(f"\n判定: 『{win}』が控除率の壁を破った。")
    else:
        print("\n判定: どの方式も EV>1.0 に未達。方式を追加(このファイルに Method 追記)し再実行、"
              "または特徴量拡充/データ増で再挑戦。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
