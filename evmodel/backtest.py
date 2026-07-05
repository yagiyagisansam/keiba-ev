"""EV>1 判定・分数 Kelly・walk-forward 検証のロジック(純Python)。

- simulate_win_bets: 単勝 EV>1+margin を買い、分数 Kelly で配分した収支
- bootstrap_ci      : レース単位ブートストラップで ROI の信頼区間
- walk_forward      : 時系列に fold を切り、過去で学習→未来で検証(リーク防止)
"""

import math


def kelly_fraction(p, odds):
    """単勝の Kelly 比。b=odds-1、f* = (p*odds - 1)/(odds - 1)。負なら賭けない。"""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    f = (p * odds - 1.0) / b
    return max(0.0, f)


def simulate_win_bets(preds, *, margin=0.0, kelly=0.5, cap=0.05, flat=False):
    """preds: [ {p, odds, won} ] を EV 順に賭けた収支を返す。

    p    : 結合勝率(本手法の予測)
    odds : 確定単勝オッズ(実オッズ)
    won  : この馬が1着なら True
    margin: EV 閾値 (EV > 1 + margin で購入)
    kelly : 分数 Kelly の係数(0.5 = ハーフ Kelly)
    cap   : 1点あたり資金比率の上限
    flat  : True なら均等買い(100円固定)で ROI を見る
    """
    bet_races = 0
    stake_total = 0.0
    ret_total = 0.0
    picks = []
    for r in preds:
        ev = r["p"] * r["odds"]
        if ev <= 1.0 + margin:
            continue
        if flat:
            stake = 1.0
        else:
            stake = min(cap, kelly * kelly_fraction(r["p"], r["odds"]))
            if stake <= 0:
                continue
        payoff = stake * r["odds"] if r["won"] else 0.0
        stake_total += stake
        ret_total += payoff
        bet_races += 1
        picks.append({**r, "ev": ev, "stake": stake, "payoff": payoff})
    roi = (ret_total / stake_total) if stake_total > 0 else 0.0
    return {
        "n_bets": bet_races,
        "stake_total": stake_total,
        "return_total": ret_total,
        "roi": roi,
        "profit_rate": roi - 1.0,
        "picks": picks,
    }


def bootstrap_ci(picks, n_boot=1000, seed=12345, flat=True):
    """賭けた各点の収支をリサンプルして ROI の 95% 信頼区間を返す。

    seed 固定の線形合同法(外部依存なし)。flat=True は均等買い ROI。
    """
    if not picks:
        return {"lo": 0.0, "hi": 0.0, "mean": 0.0}
    units = []
    for p in picks:
        stake = 1.0 if flat else p["stake"]
        ret = (stake * p["odds"]) if p["won"] else 0.0
        units.append((stake, ret))
    n = len(units)
    rng = seed
    rois = []
    for _ in range(n_boot):
        s = 0.0
        r = 0.0
        for _ in range(n):
            rng = (1103515245 * rng + 12345) & 0x7FFFFFFF
            idx = rng % n
            s += units[idx][0]
            r += units[idx][1]
        rois.append(r / s if s > 0 else 0.0)
    rois.sort()
    return {
        "lo": rois[int(0.025 * n_boot)],
        "hi": rois[int(0.975 * n_boot)],
        "mean": sum(rois) / len(rois),
    }


def walk_forward(races_sorted, model_factory, *, n_folds=4, min_train=200):
    """時系列 walk-forward 検証。

    races_sorted: 日付昇順の [ {X, q, win, odds:[..], date, race_id} ]
    model_factory: () -> TwoStageModel（毎 fold 新規に学習）
    返り値: (all_preds, fold_reports)
      all_preds: [ {p, odds, won, race_id} ] 全 OOS 予測(検証区間のみ)
    """
    N = len(races_sorted)
    if N < min_train + n_folds:
        # データが少なければ単純ホールドアウト(前半学習・後半検証)
        cut = max(1, int(N * 0.7))
        folds = [(0, cut, cut, N)]
    else:
        test_size = (N - min_train) // n_folds
        folds = []
        start_test = min_train
        while start_test < N:
            end_test = min(start_test + test_size, N)
            folds.append((0, start_test, start_test, end_test))
            start_test = end_test

    all_preds = []
    reports = []
    for (tr0, tr1, te0, te1) in folds:
        train = races_sorted[tr0:tr1]
        test = races_sorted[te0:te1]
        train = [r for r in train if r["win"] is not None]
        if not train or not test:
            continue
        model = model_factory()
        model.fit(train)
        fold_preds = []
        for r in test:
            if r["win"] is None:
                continue
            p = model.predict(r["X"], r["q"])
            for i in range(len(p)):
                fold_preds.append({
                    "p": p[i], "odds": r["odds"][i],
                    "won": (i == r["win"]), "race_id": r.get("race_id"),
                })
        edge = model.edge_r2(test)
        sim = simulate_win_bets(fold_preds, flat=True)
        reports.append({
            "train_n": len(train), "test_n": len(test),
            "delta_r2": edge["delta_r2"], "alpha": edge["alpha"], "beta": edge["beta"],
            "flat_roi": sim["roi"], "n_bets": sim["n_bets"],
        })
        all_preds.extend(fold_preds)
    return all_preds, reports
