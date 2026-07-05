"""バックテスト消費サンプル。

index.html のエンジン(_nb_engine_v6.py 由来の Harville モデル)を Python に移植し、
DB だけで「理論確率 → 実オッズ結合 → EV → 払戻突合」が完結することを実証する。

  python backtest_example.py --db keiba_2025.db --date 20250105 [--ev-min 100]

各レースについて:
1. entries の確定単勝オッズから Harville で各買い目の的中確率を計算
2. odds BLOB を展開して実オッズと結合、EV100 = prob × 実オッズ × 100
3. EV100 >= 閾値の買い目を「購入」したと仮定し、payouts と突合して収支を集計
"""

import argparse
import json
import sqlite3
import zlib
from itertools import combinations, permutations

# index.html:568-569 と同値
WIN_RR = 0.80
RETURN_RATES = {"馬連": 0.775, "馬単": 0.750, "3連複": 0.750, "3連単": 0.725}
BET_TYPE_NUM = {"馬連": 4, "馬単": 6, "3連複": 7, "3連単": 8}
ORDERED = {"馬単", "3連単"}


def harville(prob_map, order):
    p, rem = 1.0, 1.0
    for n in order:
        if rem < 1e-12:
            return 0.0
        p *= prob_map[n] / rem
        rem -= prob_map[n]
    return p


def win_probs(horses):
    """{馬番: 単勝オッズ} → {馬番: 正規化勝率} (index.html normalize と同じ)"""
    raw = {n: 1.0 / o for n, o in horses.items()}
    s = sum(raw.values())
    return {n: v / s for n, v in raw.items()}


def calc_bets(pm, bet_type):
    """券種ごとの全買い目と的中確率を yield する。combo は DB の正規化キーと同形式。"""
    nums = sorted(pm)
    if bet_type == "馬連":
        for a, b in combinations(nums, 2):
            yield f"{a:02d}-{b:02d}", harville(pm, [a, b]) + harville(pm, [b, a])
    elif bet_type == "馬単":
        for a, b in permutations(nums, 2):
            yield f"{a:02d}-{b:02d}", harville(pm, [a, b])
    elif bet_type == "3連複":
        for c in combinations(nums, 3):
            p = sum(harville(pm, perm) for perm in permutations(c))
            yield "-".join(f"{n:02d}" for n in c), p
    elif bet_type == "3連単":
        for perm in permutations(nums, 3):
            yield "-".join(f"{n:02d}" for n in perm), harville(pm, perm)


def load_odds(conn, race_id, bet_type_num):
    row = conn.execute(
        "SELECT payload FROM odds WHERE race_id = ? AND bet_type = ?",
        (race_id, bet_type_num),
    ).fetchone()
    if row is None:
        return {}
    return json.loads(zlib.decompress(row[0]).decode("utf-8"))


def backtest_race(conn, race_id, ev_min):
    """1レース分: EV>=閾値の買い目を100円ずつ購入したと仮定した収支を返す。"""
    horses = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT horse_num, win_odds FROM entries "
            "WHERE race_id = ? AND win_odds >= 1.0", (race_id,),
        )
    }
    if len(horses) < 3:
        return None
    pm = win_probs(horses)
    payout_map = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT bet_type, combo, payout_yen FROM payouts WHERE race_id = ?", (race_id,)
        )
    }

    picks, bet_total, return_total = [], 0, 0
    for bt_name, bt_num in BET_TYPE_NUM.items():
        actual = load_odds(conn, race_id, bt_num)
        if not actual:
            continue
        for combo, prob in calc_bets(pm, bt_name):
            vals = actual.get(combo)
            if vals is None:
                continue
            ev100 = prob * vals[0] * 100
            if ev100 < ev_min:
                continue
            hit = payout_map.get((bt_num, combo), 0)
            bet_total += 100
            return_total += hit
            picks.append({
                "type": bt_name, "combo": combo, "prob": prob,
                "odds": vals[0], "ev100": round(ev100), "payout": hit,
            })
    return {"picks": picks, "bet": bet_total, "return": return_total}


def main():
    ap = argparse.ArgumentParser(description="DB からの EV バックテスト実演")
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", help="対象日 YYYYMMDD (省略時は全レース)")
    ap.add_argument("--ev-min", type=float, default=100, help="購入する EV100 の下限")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    q = ("SELECT race_id, venue_name, race_no, race_name FROM races "
         "WHERE status_result = 1 AND status_odds = 1")
    params = ()
    if args.date:
        q += " AND kaisai_date = ?"
        params = (args.date,)
    races = conn.execute(q + " ORDER BY race_id", params).fetchall()

    total_bet = total_return = total_picks = 0
    for race_id, venue, race_no, race_name in races:
        r = backtest_race(conn, race_id, args.ev_min)
        if r is None:
            continue
        total_bet += r["bet"]
        total_return += r["return"]
        total_picks += len(r["picks"])
        hits = [p for p in r["picks"] if p["payout"] > 0]
        print(f"{race_id} {venue}{race_no}R {race_name or ''}: "
              f"EV>={args.ev_min:.0f} が {len(r['picks'])}点 "
              f"購入{r['bet']}円 → 払戻{r['return']}円"
              + (f"  的中: {[(h['type'], h['combo'], h['payout']) for h in hits]}" if hits else ""))

    if total_bet:
        roi = total_return / total_bet * 100
        print(f"\n合計: {total_picks}点 購入{total_bet:,}円 → 払戻{total_return:,}円 "
              f"(回収率 {roi:.1f}%)")
    else:
        print("対象レースまたは EV 条件を満たす買い目がありません")


if __name__ == "__main__":
    main()
