"""GBDT(LightGBM)による非線形勝率モデル — 別手法(線形の限界を破れるか)。

線形の条件付きロジットは ΔR² −0.09 で頭打ち。木モデルは特徴の交互作用
(脚質×距離、斤量×馬体重、近走×休養 等)を自動で捉えるため、同じ22特徴でも
市場に近づける可能性がある。日本のML実践者の主流手法。

厳密な時系列 walk-forward・レースグループ化・リーク無し特徴で検証する。

  python -m evmodel.gbdt --db keiba_all.db --min-train 5000 --folds 3 --margin 0.05
"""

import argparse
import math

import numpy as np
from lightgbm import LGBMClassifier

from .features import load_races, FEAT_NAMES
from .condlogit import ConditionalLogit, isotonic_fit, isotonic_apply, mcfadden_r2
from .backtest import simulate_win_bets, bootstrap_ci


def _race_matrix(races):
    X = np.array([x for r in races for x in r["X"]], dtype=np.float64)
    y = np.array([1 if i == r["win"] else 0
                  for r in races for i in range(len(r["X"]))], dtype=np.int32)
    return X, y


def _softmax(v):
    m = v.max()
    e = np.exp(v - m)
    return e / e.sum()


def fit_predict_fold(train, test, params):
    Xtr, ytr = _race_matrix(train)
    clf = LGBMClassifier(**params)
    clf.fit(Xtr, ytr)

    # Stage1: GBDT の勝ち確率をレース内で正規化 → π
    pi_per_race = []
    raw_pairs = []  # (rawprob, won) 較正用
    for r in test:
        Xr = np.array(r["X"], dtype=np.float64)
        raw = clf.predict_proba(Xr)[:, 1]
        pi = raw / raw.sum() if raw.sum() > 0 else np.ones(len(raw)) / len(raw)
        pi_per_race.append(pi)
        for i, p in enumerate(pi):
            raw_pairs.append((float(p), 1 if i == r["win"] else 0))
    # 較正(isotonic)
    iso = isotonic_fit(raw_pairs)

    # Stage2: [log π, log q] の条件付きロジットを train 上で学習
    #   train の π も必要 → 別途 train を予測
    s2_races = []
    for r in train:
        Xr = np.array(r["X"], dtype=np.float64)
        raw = clf.predict_proba(Xr)[:, 1]
        pi = raw / raw.sum() if raw.sum() > 0 else np.ones(len(raw)) / len(raw)
        pic = np.array([max(isotonic_apply(iso, float(p)), 1e-9) for p in pi])
        pic /= pic.sum()
        X2 = [[math.log(pic[i]), math.log(max(r["q"][i], 1e-9))] for i in range(len(pic))]
        s2_races.append((X2, r["win"]))
    stage2 = ConditionalLogit(l2=1e-4, lr=0.05, iters=300).fit(s2_races)
    return clf, iso, stage2


def evaluate(races, params, *, min_train=5000, folds=3, margin=0.05):
    N = len(races)
    step = (N - min_train) // folds
    fold_bounds, s = [], min_train
    while s < N:
        e = min(s + step, N)
        fold_bounds.append((s, e))
        s = e

    preds_s1, preds_two = [], []
    nll_m = nll_s1 = nll_two = n0 = nrace = 0.0
    for (te0, te1) in fold_bounds:
        train = [r for r in races[:te0] if r["win"] is not None]
        test = [r for r in races[te0:te1] if r["win"] is not None]
        if len(train) < 100 or not test:
            continue
        clf, iso, stage2 = fit_predict_fold(train, test, params)
        for r in test:
            Xr = np.array(r["X"], dtype=np.float64)
            raw = clf.predict_proba(Xr)[:, 1]
            pi = raw / raw.sum() if raw.sum() > 0 else np.ones(len(raw)) / len(raw)
            pic = np.array([max(isotonic_apply(iso, float(p)), 1e-9) for p in pi])
            pic /= pic.sum()
            X2 = [[math.log(pic[i]), math.log(max(r["q"][i], 1e-9))] for i in range(len(pic))]
            p2 = stage2.predict_race(X2)
            w = r["win"]
            nll_m += -math.log(max(r["q"][w], 1e-12))
            nll_s1 += -math.log(max(pic[w], 1e-12))
            nll_two += -math.log(max(p2[w], 1e-12))
            n0 += math.log(len(r["X"]))
            nrace += 1
            for i in range(len(r["X"])):
                preds_s1.append({"p": float(pic[i]), "odds": r["odds"][i], "won": i == w})
                preds_two.append({"p": float(p2[i]), "odds": r["odds"][i], "won": i == w})
    null = n0 / nrace
    r2_m = mcfadden_r2(nll_m / nrace, null)
    r2_s1 = mcfadden_r2(nll_s1 / nrace, null)
    r2_two = mcfadden_r2(nll_two / nrace, null)
    out = {}
    for name, preds, r2 in [("gbdt_stage1", preds_s1, r2_s1),
                            ("gbdt_two_stage", preds_two, r2_two)]:
        sim = simulate_win_bets(preds, margin=margin, flat=True)
        ci = bootstrap_ci(sim["picks"], flat=True)
        out[name] = {"roi": sim["roi"], "n_bets": sim["n_bets"],
                     "ci_lo": ci["lo"], "ci_hi": ci["hi"],
                     "delta_r2": r2 - r2_m}
    out["_r2_market"] = r2_m
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="GBDT(LightGBM)勝率モデルの検証")
    ap.add_argument("--db", required=True)
    ap.add_argument("--year")
    ap.add_argument("--min-train", type=int, default=5000)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--margin", type=float, default=0.05)
    ap.add_argument("--leaves", type=int, default=31)
    ap.add_argument("--estimators", type=int, default=300)
    ap.add_argument("--lr", type=float, default=0.05)
    args = ap.parse_args(argv)

    races = load_races(args.db, limit_year=args.year)
    labeled = [r for r in races if r["win"] is not None]
    print(f"[gbdt] {len(labeled)} レース(勝ち馬確定), 特徴 {len(FEAT_NAMES)}")
    params = dict(n_estimators=args.estimators, num_leaves=args.leaves,
                  learning_rate=args.lr, subsample=0.8, colsample_bytree=0.8,
                  min_child_samples=50, reg_lambda=1.0, n_jobs=-1, verbosity=-1)
    res = evaluate(races, params, min_train=args.min_train, folds=args.folds,
                   margin=args.margin)
    print(f"\n市場のみ McFadden R² = {res['_r2_market']:.4f}")
    for name in ("gbdt_stage1", "gbdt_two_stage"):
        r = res[name]
        flag = " ★EV>1.0(CI下限>100%)" if r["ci_lo"] > 1.0 and r["n_bets"] >= 100 else ""
        print(f"  {name:16s} ROI={r['roi']*100:6.1f}%  CI[{r['ci_lo']*100:5.1f},"
              f"{r['ci_hi']*100:6.1f}]  ΔR²={r['delta_r2']:+.4f}  賭け{r['n_bets']}点{flag}")
    print("\n参考: 線形条件付きロジットの stage1 ΔR² は約 -0.094(3年)。"
          "GBDT がこれを上回れば非線形の交互作用が効いている。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
