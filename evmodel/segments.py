"""セグメント発掘: 市場が系統的に間違える「条件」を探す別系統の手法。

勝率モデル/FLB/プール裁定が全滅したため、大域モデルではなく局所的な
市場非効率(ポケット)を探す。単変数・二変数のセグメントごとに、単勝/複勝を
「その条件の馬を全部買う」ROI で評価する。

過剰適合を避けるため time-split: 前半(train)で ROI>100% を探し、後半(holdout)でも
ROI>100% かつ holdout の 95%CI 下限>100% を満たすものだけ「候補」とする。
それでも生き残れば実運用検証へ、無ければこの系統も却下。

  python -m evmodel.segments --db keiba_all.db --min-n 150
"""

import argparse
import sqlite3


def _bucket_odds(o):
    for hi, lab in [(2, "~2.0"), (4, "2-4"), (7, "4-7"), (15, "7-15"),
                    (50, "15-50")]:
        if o < hi:
            return lab
    return "50+"


def _bucket(v, edges, labels, default="?"):
    if v is None:
        return default
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1] if len(labels) > len(edges) else default


def collect(db_path):
    """各出走馬の (セグメント特徴, 単勝/複勝リターン, split) を集める。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT r.race_id, r.kaisai_date, r.n_horses, "
        "  e.horse_num, e.horse_id, e.win_odds, e.popularity, e.finish_pos, "
        "  e.weight_diff, e.age, e.horse_weight, e.waku "
        "FROM races r JOIN entries e ON e.race_id=r.race_id "
        "WHERE r.status_result=1 AND e.win_odds>=1.0 "
        "ORDER BY r.kaisai_date, r.race_id, e.horse_num").fetchall()
    place_pay = {}
    for rid, combo, v in conn.execute(
            "SELECT race_id, combo, payout_yen FROM payouts WHERE bet_type=2"):
        place_pay[(rid, int(combo))] = v
    conn.close()

    # 開催日昇順で horse の過去走数・前走日を追跡(休養明け・初出走)
    horse_runs, horse_last = {}, {}
    race_order = []
    seen = set()
    for r in rows:
        if r["race_id"] not in seen:
            seen.add(r["race_id"]); race_order.append(r["race_id"])
    split_idx = int(len(race_order) * 0.6)
    train_races = set(race_order[:split_idx])

    recs = []
    for r in rows:
        hid = r["horse_id"]
        n_prior = horse_runs.get(hid, 0)
        last = horse_last.get(hid)
        layoff = _date_diff(last, r["kaisai_date"]) if last else None
        seg = {
            "odds": _bucket_odds(r["win_odds"]),
            "pop": _bucket(r["popularity"], [2, 4, 7, 11], ["1-2", "3-4", "5-7", "8-11", "12+"]),
            "wdiff": _bucket(r["weight_diff"], [-8, -3, 4, 9],
                             ["<-8", "-8..-4", "-3..3", "4..8", "9+"]),
            "age": _bucket(r["age"], [3, 4, 5, 6], ["<3", "3", "4", "5", "6+"]),
            "layoff": _bucket(layoff, [21, 57, 121, 301],
                              ["~20", "21-56", "57-120", "121-300", "301+"]) if layoff is not None
                      else ("debut" if n_prior == 0 else "?"),
            "post": _bucket(r["waku"], [3, 5, 7], ["1-2", "3-4", "5-6", "7-8"]),
            "field": _bucket(r["n_horses"], [10, 14], ["~9", "10-13", "14+"]),
        }
        won = (r["finish_pos"] == 1)
        win_ret = r["win_odds"] if won else 0.0
        pp = place_pay.get((r["race_id"], r["horse_num"]))
        place_ret = (pp / 100.0) if pp else 0.0
        recs.append((r["race_id"] in train_races, seg, win_ret, place_ret))
        # 更新(使ってから)
        horse_runs[hid] = n_prior + 1
        horse_last[hid] = r["kaisai_date"]
    return recs


def _date_diff(d1, d2):
    try:
        y1, m1, dd1 = int(d1[:4]), int(d1[4:6]), int(d1[6:8])
        y2, m2, dd2 = int(d2[:4]), int(d2[4:6]), int(d2[6:8])
        return (y2 - y1) * 365 + (m2 - m1) * 30 + (dd2 - dd1)
    except (ValueError, TypeError):
        return None


def ci_low(returns, n_boot=2000, seed=7):
    n = len(returns)
    if n == 0:
        return 0.0
    rng = seed; rois = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            rng = (1103515245 * rng + 12345) & 0x7FFFFFFF
            s += returns[rng % n]
        rois.append(s / n)
    rois.sort()
    return rois[int(0.025 * n_boot)]


def scan(recs, variables, bet, min_n):
    """単変数セグメントごとに train/holdout ROI を集計し候補を返す。"""
    idx = 2 if bet == "win" else 3
    agg = {}  # (var, bucket) -> {"tr":[..],"ho":[..]}
    for is_train, seg, wret, pret in recs:
        ret = wret if bet == "win" else pret
        for var in variables:
            key = (var, seg[var])
            d = agg.setdefault(key, {"tr": [], "ho": []})
            d["tr" if is_train else "ho"].append(ret)
    out = []
    for (var, bucket), d in agg.items():
        if len(d["tr"]) < min_n or len(d["ho"]) < min_n:
            continue
        tr = sum(d["tr"]) / len(d["tr"])
        ho = sum(d["ho"]) / len(d["ho"])
        if tr > 1.0 and ho > 1.0:
            lo = ci_low(d["ho"])
            out.append((var, bucket, tr, ho, lo, len(d["ho"])))
    return sorted(out, key=lambda t: -t[4])


def scan_pairs(recs, variables, bet, min_n):
    """二変数の交互作用も探索(候補が局所的なほど強い非効率になりうる)。"""
    idx = 2 if bet == "win" else 3
    agg = {}
    for is_train, seg, wret, pret in recs:
        ret = wret if bet == "win" else pret
        for i in range(len(variables)):
            for j in range(i + 1, len(variables)):
                v1, v2 = variables[i], variables[j]
                key = (v1, seg[v1], v2, seg[v2])
                d = agg.setdefault(key, {"tr": [], "ho": []})
                d["tr" if is_train else "ho"].append(ret)
    out = []
    for (v1, b1, v2, b2), d in agg.items():
        if len(d["tr"]) < min_n or len(d["ho"]) < min_n:
            continue
        tr = sum(d["tr"]) / len(d["tr"]); ho = sum(d["ho"]) / len(d["ho"])
        if tr > 1.05 and ho > 1.0:
            lo = ci_low(d["ho"])
            out.append((f"{v1}={b1} & {v2}={b2}", tr, ho, lo, len(d["ho"])))
    return sorted(out, key=lambda t: -t[3])


def main(argv=None):
    ap = argparse.ArgumentParser(description="セグメント発掘(市場の系統的誤りを探す)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--min-n", type=int, default=150, help="train/holdout 各の最小サンプル")
    ap.add_argument("--pairs", action="store_true", help="二変数交互作用も探索")
    args = ap.parse_args(argv)

    recs = collect(args.db)
    nt = sum(1 for r in recs if r[0])
    print(f"[segments] {len(recs)} 出走(train {nt} / holdout {len(recs)-nt})\n")
    variables = ["odds", "pop", "wdiff", "age", "layoff", "post", "field"]

    winners = []
    for bet in ("win", "place"):
        print(f"=== {bet} 単変数セグメント (train>100% かつ holdout>100%) ===")
        res = scan(recs, variables, bet, args.min_n)
        if not res:
            print("  該当なし")
        for var, bucket, tr, ho, lo, n in res:
            flag = " ★holdout CI下限>100%" if lo > 1.0 else ""
            if lo > 1.0:
                winners.append((bet, f"{var}={bucket}", ho, lo, n))
            print(f"  {var:7s}={bucket:8s}  train {tr*100:5.0f}%  holdout {ho*100:5.0f}%  "
                  f"CI下限 {lo*100:5.0f}%  n={n}{flag}")
        if args.pairs:
            print(f"--- {bet} 二変数 ---")
            for label, tr, ho, lo, n in scan_pairs(recs, variables, bet, args.min_n)[:10]:
                flag = " ★" if lo > 1.0 else ""
                if lo > 1.0:
                    winners.append((bet, label, ho, lo, n))
                print(f"  {label:32s} train {tr*100:4.0f}% holdout {ho*100:4.0f}% "
                      f"CI下限 {lo*100:4.0f}% n={n}{flag}")
        print()

    print("=== 判定 ===")
    if winners:
        print("holdout CI下限>100% を満たす候補(=市場の系統的誤りの可能性):")
        for bet, label, ho, lo, n in winners:
            print(f"  ★ [{bet}] {label}  holdout ROI {ho*100:.0f}%  CI下限 {lo*100:.0f}%  n={n}")
        print("→ 実運用検証(別年・walk-forward)へ進む価値あり。")
    else:
        print("holdout で有意(CI下限>100%)なセグメントなし → セグメント発掘も1年基準で却下。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
