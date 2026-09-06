"""Keiba EV 期待値計算の精度検証ハーネス。

収集済み DB(keiba_YYYY.db)を用いて、index.html の EV 計算(Harville モデル)が
実際のレース結果とどれだけ一致するかを 2軸で検証する:

  1. 的中率の一致率 = 実的中率 ÷ 予測平均的中率
  2. 期待値の一致率 = 実払戻総額 ÷ 予測期待値総額(= ROI ÷ 予測平均EV)

使い方:
  python -m analysis.verify_ev --db keiba_2025.db \
      --bet-type 馬連 --min-hit-pct 20 --min-ev 105 \
      [--csv picks.csv] [--json summary.json]

「対象」= 予測的中率 >= min-hit-pct かつ 予測期待値 >= min-ev の買い目。
各対象を 100円ずつ購入したと仮定して収支・払戻を突合する。
"""

import argparse
import csv
import json
import sqlite3
import sys

# 既存エンジンを再利用(重複実装しない)
from backtest_example import BET_TYPE_NUM, calc_bets, load_odds, win_probs


def collect_picks(conn, bet_type, min_hit_pct, min_ev):
    """条件を満たす買い目を全レースから収集して返す。"""
    bt_num = BET_TYPE_NUM[bet_type]
    races = conn.execute(
        "SELECT race_id, kaisai_date, venue_name, race_no, race_name "
        "FROM races WHERE status_result = 1 AND status_odds = 1 "
        "ORDER BY race_id"
    ).fetchall()

    picks = []
    for race_id, kaisai_date, venue, race_no, race_name in races:
        horses = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT horse_num, win_odds FROM entries "
                "WHERE race_id = ? AND win_odds >= 1.0", (race_id,)
            )
        }
        if len(horses) < 2:
            continue
        pm = win_probs(horses)
        names = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT horse_num, horse_name FROM entries WHERE race_id = ?", (race_id,)
            )
        }
        actual = load_odds(conn, race_id, bt_num)
        if not actual:
            continue
        # 実際の的中組み合わせ(払戻)。同着は複数行
        payout_map = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT combo, payout_yen FROM payouts WHERE race_id = ? AND bet_type = ?",
                (race_id, bt_num),
            )
        }

        for combo, prob in calc_bets(pm, bet_type):
            vals = actual.get(combo)
            if vals is None:
                continue
            odds = vals[0]
            hit_pct = prob * 100
            ev = prob * odds * 100
            if hit_pct < min_hit_pct or ev < min_ev:
                continue
            hit = combo in payout_map
            picks.append({
                "race_id": race_id,
                "date": kaisai_date,
                "venue": venue or "",
                "race_no": race_no,
                "race_name": race_name or "",
                "combo": combo,
                "names": "-".join(names.get(int(x), "?") for x in combo.split("-")),
                "hit_pct": round(hit_pct, 2),
                "odds": odds,
                "ev": round(ev, 1),          # 予測期待値(=100円あたり予測払戻)
                "hit": hit,
                "payout": payout_map.get(combo, 0),  # 実払戻(100円あたり)
            })
    return picks


def summarize(picks, bet_type, min_hit_pct, min_ev):
    """2軸の一致率を集計する。"""
    n = len(picks)
    if n == 0:
        return {"n_picks": 0, "bet_type": bet_type,
                "min_hit_pct": min_hit_pct, "min_ev": min_ev}

    hits = sum(1 for p in picks if p["hit"])
    bet_total = n * 100
    payout_total = sum(p["payout"] for p in picks)       # 実払戻総額
    pred_return_total = sum(p["ev"] for p in picks)       # 予測期待値総額(100円賭けの見込み払戻)

    actual_hit_rate = hits / n * 100
    pred_hit_rate = sum(p["hit_pct"] for p in picks) / n
    pred_ev_avg = pred_return_total / n
    roi = payout_total / bet_total * 100

    return {
        "bet_type": bet_type,
        "min_hit_pct": min_hit_pct,
        "min_ev": min_ev,
        "n_races_selected": len({p["race_id"] for p in picks}),
        "n_picks": n,
        "n_hits": hits,
        # 的中率軸
        "actual_hit_rate": round(actual_hit_rate, 2),
        "pred_hit_rate": round(pred_hit_rate, 2),
        "hit_rate_agreement": round(actual_hit_rate / pred_hit_rate * 100, 1),
        # 期待値軸
        "pred_return_total": round(pred_return_total),
        "payout_total": payout_total,
        "bet_total": bet_total,
        "pred_ev_avg": round(pred_ev_avg, 1),
        "roi": round(roi, 1),
        "ev_agreement": round(payout_total / pred_return_total * 100, 1),
        # 参考
        "odds_min": min(p["odds"] for p in picks),
        "odds_max": max(p["odds"] for p in picks),
    }


def bin_calibration(picks, edges=(20, 25, 30, 40, 100)):
    """予測的中率帯ごとに 予測 vs 実測(的中率・払戻)を集計する。"""
    bins = []
    lo = edges[0]
    for hi in edges[1:]:
        grp = [p for p in picks if lo <= p["hit_pct"] < hi]
        if grp:
            n = len(grp)
            bins.append({
                "range": f"{lo}-{hi}%",
                "n": n,
                "pred_hit": round(sum(p["hit_pct"] for p in grp) / n, 1),
                "actual_hit": round(sum(1 for p in grp if p["hit"]) / n * 100, 1),
                "pred_ev": round(sum(p["ev"] for p in grp) / n, 1),
                "roi": round(sum(p["payout"] for p in grp) / (n * 100) * 100, 1),
            })
        lo = hi
    return bins


def print_report(summary, bins):
    s = summary
    print("=" * 60)
    print(f"  Keiba EV 精度検証: {s['bet_type']} "
          f"(予測的中率>={s['min_hit_pct']}% かつ 期待値>={s['min_ev']})")
    print("=" * 60)
    if s["n_picks"] == 0:
        print("  条件を満たす買い目がありません")
        return
    print(f"  対象: {s['n_picks']}点 / {s['n_races_selected']}レース "
          f"(実オッズ {s['odds_min']}〜{s['odds_max']}倍)")
    print()
    print("  【的中率の一致率】")
    print(f"    予測平均的中率 : {s['pred_hit_rate']:.2f}%")
    print(f"    実的中率       : {s['actual_hit_rate']:.2f}%  ({s['n_hits']}/{s['n_picks']}的中)")
    print(f"    → 一致率       : {s['hit_rate_agreement']:.1f}%")
    print()
    print("  【期待値の一致率(予測払戻 vs 実払戻)】")
    print(f"    予測期待値総額 : {s['pred_return_total']:,}円 (平均EV {s['pred_ev_avg']})")
    print(f"    実払戻総額     : {s['payout_total']:,}円 (投資 {s['bet_total']:,}円)")
    print(f"    → 一致率       : {s['ev_agreement']:.1f}%   (回収率 {s['roi']:.1f}%)")
    print()
    print("  【予測的中率帯別キャリブレーション】")
    print(f"    {'帯':<10}{'件数':>5}{'予測的中':>9}{'実的中':>8}{'予測EV':>8}{'回収率':>8}")
    for b in bins:
        print(f"    {b['range']:<10}{b['n']:>5}{b['pred_hit']:>8.1f}%{b['actual_hit']:>7.1f}%"
              f"{b['pred_ev']:>8.1f}{b['roi']:>7.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keiba EV 期待値計算の精度検証")
    ap.add_argument("--db", required=True)
    ap.add_argument("--bet-type", default="馬連", choices=list(BET_TYPE_NUM))
    ap.add_argument("--min-hit-pct", type=float, default=20.0)
    ap.add_argument("--min-ev", type=float, default=105.0)
    ap.add_argument("--csv", help="対象買い目を CSV 出力")
    ap.add_argument("--json", help="サマリを JSON 出力")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    picks = collect_picks(conn, args.bet_type, args.min_hit_pct, args.min_ev)
    summary = summarize(picks, args.bet_type, args.min_hit_pct, args.min_ev)
    bins = bin_calibration(picks)
    print_report(summary, bins)

    if args.csv and picks:
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(picks[0].keys()))
            w.writeheader()
            w.writerows(picks)
        print(f"\n  CSV: {args.csv} ({len(picks)}行)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "bins": bins}, f, ensure_ascii=False, indent=2)
        print(f"  JSON: {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
