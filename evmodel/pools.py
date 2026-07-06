"""プール間裁定(Dr. Z 方式)による別系統の EV 手法。

これまでの手法は全て「勝率を市場より上手く当てる」路線で、効率的な単勝市場を
超えられず 1 年データで EV>1.0 未達 → 却下。ここは根本的に別の発想:

  効率的な【単勝プール】から各馬の公正な複勝/連系確率を Harville で導き、
  相対的に非効率な【複勝・ワイド・馬連プール】の歪み(実オッズ > 公正オッズ)を突く。

モデル学習を一切しないので過剰適合が無く、全レース検証がそのまま妥当。
文献: Hausch-Ziemba-Rubinstein の Dr. Z システム(place/show の +EV を実証)。

  python -m evmodel.pools --db keiba_all.db --bet place --margin 0.05 --odds min
"""

import argparse
import sqlite3
from itertools import combinations


def win_probs(win_odds):
    """{馬番: 単勝オッズ} → {馬番: 正規化勝率}(単勝プールの含意確率)。"""
    raw = {n: 1.0 / o for n, o in win_odds.items() if o and o > 0}
    s = sum(raw.values())
    return {n: v / s for n, v in raw.items()} if s > 0 else {}


def place_probs_top_k(p, k):
    """Harville(Plackett-Luce)で各馬が上位 k 着以内に入る確率を返す。"""
    nums = list(p)
    res = {n: p[n] for n in nums}  # 1着確率
    if k >= 2:
        for i in nums:
            s = 0.0
            for a in nums:
                if a == i:
                    continue
                denom = 1.0 - p[a]
                if denom > 1e-9:
                    s += p[a] * p[i] / denom
            res[i] += s
    if k >= 3:
        for i in nums:
            s = 0.0
            for a in nums:
                if a == i:
                    continue
                d1 = 1.0 - p[a]
                if d1 <= 1e-9:
                    continue
                for b in nums:
                    if b == i or b == a:
                        continue
                    d2 = 1.0 - p[a] - p[b]
                    if d2 > 1e-9:
                        s += p[a] * (p[b] / d1) * (p[i] / d2)
            res[i] += s
    return res


def place_k(n_horses):
    """JRA 複勝の的中着順: 8頭以上=3着、5-7頭=2着、4頭以下は発売なし。"""
    if n_horses >= 8:
        return 3
    if n_horses >= 5:
        return 2
    return 0


def backtest_place(conn, margin=0.05, odds_field="min"):
    """複勝プール裁定の全レース検証。単勝プール由来の公正確率 × 実複勝オッズ > 1 で購入。"""
    races = conn.execute(
        "SELECT race_id, n_horses FROM races WHERE status_result=1").fetchall()
    stake = ret = 0.0
    nbet = hit = 0
    picks = []
    for race_id, n_horses in races:
        k = place_k(n_horses or 0)
        if k == 0:
            continue
        ents = conn.execute(
            "SELECT horse_num, win_odds, place_odds_min, place_odds_max, finish_pos "
            "FROM entries WHERE race_id=? AND win_odds >= 1.0", (race_id,)).fetchall()
        win_odds = {e[0]: e[1] for e in ents}
        p = win_probs(win_odds)
        if len(p) < 3:
            continue
        pk = place_probs_top_k(p, k)
        # 複勝の確定払戻(bet_type=2)。的中馬は payouts に載る
        pay = {int(c): v for c, v in conn.execute(
            "SELECT combo, payout_yen FROM payouts WHERE race_id=? AND bet_type=2",
            (race_id,)).fetchall()}
        for e in ents:
            num, _wo, pmin, pmax, fin = e
            if pmin is None:
                continue
            odds_dec = pmin if odds_field == "min" else (
                pmax if odds_field == "max" else (pmin + (pmax or pmin)) / 2)
            ev = pk.get(num, 0.0) * odds_dec
            if ev <= 1.0 + margin:
                continue
            nbet += 1
            stake += 1.0
            payout = pay.get(num)
            if payout:
                ret += payout / 100.0
                hit += 1
            picks.append({"race_id": race_id, "num": num, "p": pk[num],
                          "odds": odds_dec, "ev": ev, "won": bool(payout)})
    roi = ret / stake if stake else 0.0
    return {"n_bets": nbet, "hit": hit, "stake": stake, "return": ret,
            "roi": roi, "picks": picks}


def backtest_wide(conn, margin=0.05):
    """ワイド(2頭が共に3着以内)プール裁定。単勝由来のペア確率 × 実ワイドオッズ。"""
    import zlib
    import json as _json
    races = conn.execute(
        "SELECT race_id, n_horses FROM races WHERE status_result=1").fetchall()
    stake = ret = 0.0
    nbet = hit = 0
    for race_id, n_horses in races:
        k = place_k(n_horses or 0)
        if k < 3:
            continue
        ents = conn.execute(
            "SELECT horse_num, win_odds FROM entries WHERE race_id=? AND win_odds>=1.0",
            (race_id,)).fetchall()
        win_odds = {e[0]: e[1] for e in ents}
        p = win_probs(win_odds)
        if len(p) < 4:
            continue
        pk = place_probs_top_k(p, 3)
        # ワイドのペア確率(近似): P(both in top3) ≈ P(a in top3)*P(b in top3 | a)
        # ここでは独立近似 pk[a]*pk[b] で公正確率を見積もる(下限寄りの保守値)
        row = conn.execute(
            "SELECT payload FROM odds WHERE race_id=? AND bet_type=5", (race_id,)).fetchone()
        if not row:
            continue
        wide = _json.loads(zlib.decompress(row[0]).decode())
        pay = {c: v for c, v in conn.execute(
            "SELECT combo, payout_yen FROM payouts WHERE race_id=? AND bet_type=5",
            (race_id,)).fetchall()}
        for a, b in combinations(sorted(p), 2):
            combo = f"{a:02d}-{b:02d}"
            vals = wide.get(combo)
            if not vals:
                continue
            odds_dec = vals[0]  # ワイド下限オッズ(確定レンジの下限=保守)
            prob = pk[a] * pk[b]  # 独立近似(過小評価寄り)
            ev = prob * odds_dec
            if ev <= 1.0 + margin:
                continue
            nbet += 1
            stake += 1.0
            payout = pay.get(combo)
            if payout:
                ret += payout / 100.0
                hit += 1
    roi = ret / stake if stake else 0.0
    return {"n_bets": nbet, "hit": hit, "stake": stake, "return": ret, "roi": roi}


def main(argv=None):
    ap = argparse.ArgumentParser(description="プール間裁定(Dr. Z方式)のEV検証")
    ap.add_argument("--db", required=True)
    ap.add_argument("--bet", choices=["place", "wide"], default="place")
    ap.add_argument("--margin", type=float, default=0.05)
    ap.add_argument("--odds", choices=["min", "max", "mid"], default="min",
                    help="複勝の判定オッズ(min=保守/mid=中点/max=楽観)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    if args.bet == "place":
        for odds_field in (["min", "mid", "max"] if args.odds == "min" else [args.odds]):
            r = backtest_place(conn, margin=args.margin, odds_field=odds_field)
            print(f"[複勝プール裁定 odds={odds_field:3s}] "
                  f"ROI={r['roi']*100:6.1f}%  賭け{r['n_bets']}点  的中{r['hit']}  "
                  f"({'★EV>1達成' if r['roi']>1.0 and r['n_bets']>=100 else '未達'})")
    else:
        r = backtest_wide(conn, margin=args.margin)
        print(f"[ワイドプール裁定] ROI={r['roi']*100:6.1f}%  賭け{r['n_bets']}点  的中{r['hit']}  "
              f"({'★EV>1達成' if r['roi']>1.0 and r['n_bets']>=100 else '未達'})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
