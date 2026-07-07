"""late money 検証: odds_snapshots の時系列からオッズ変動特徴を作り、
「締切直前に売れた馬(steamed)」戦略の回収率を検証する。

前進収集(scraper.poll_odds)で貯めた odds_snapshots と、確定後の結果(entries)を
突合できるレースだけを対象にする。データが溜まってから走らせる。

  python -m evmodel.latemoney --db keiba_2026.db --strategy steam --margin 0.0
"""

import argparse
import json
import math
import sqlite3
import zlib

from .backtest import bootstrap_ci


def load_snapshots(conn, bet_type=1):
    """{race_id: [(minutes_to_post, {horse_num: odds}), ...]} を返す(単勝=1)。"""
    out = {}
    for rid, mtp, payload in conn.execute(
            "SELECT race_id, minutes_to_post, payload FROM odds_snapshots "
            "WHERE bet_type=? ORDER BY race_id, minutes_to_post DESC", (bet_type,)):
        d = json.loads(zlib.decompress(payload).decode("utf-8"))
        odds = {int(k): v[0] for k, v in d.items() if v and v[0]}
        out.setdefault(rid, []).append((mtp, odds))
    return out


def race_late_features(snaps):
    """1レースのスナップショット列 → {horse: {early, late, drift, late_drop}}。

    early = 最も発走から遠い時点、late = 最も近い時点。
    drift = log(late/early)（負=オッズ短縮=売れた）。late_drop = 直近区間の短縮。
    """
    if len(snaps) < 2:
        return {}
    # minutes_to_post 降順で来る(遠い→近い)。early=最初, late=最後
    ordered = sorted(snaps, key=lambda t: -(t[0] if t[0] is not None else -999))
    early = ordered[0][1]
    late = ordered[-1][1]
    mid = ordered[len(ordered) // 2][1]
    feats = {}
    for h in late:
        eo, lo, mo = early.get(h), late.get(h), mid.get(h)
        if not lo or lo <= 0:
            continue
        drift = math.log(lo / eo) if (eo and eo > 0) else 0.0
        late_drop = math.log(lo / mo) if (mo and mo > 0) else 0.0
        feats[h] = {"early": eo, "late": lo, "drift": drift, "late_drop": late_drop}
    return feats


def backtest_steam(conn, *, threshold=-0.05, margin=0.0, min_late_odds=1.5):
    """「締切直前に有意に売れた馬」を単勝で買う戦略の回収率。

    threshold: drift がこの値以下(=これ以上短縮)なら steamed とみなす。
    確定後の結果(entries.finish_pos / win_odds)と突合して実回収を測る。
    """
    snaps = load_snapshots(conn, bet_type=1)
    stake = ret = 0.0
    picks = []
    for rid, s in snaps.items():
        feats = race_late_features(s)
        if not feats:
            continue
        res = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT horse_num, finish_pos, win_odds FROM entries WHERE race_id=?", (rid,))}
        if not res:
            continue  # まだ結果なし
        for h, f in feats.items():
            if f["drift"] > threshold or f["late"] < min_late_odds:
                continue
            fin, wodds = res.get(h, (None, None))
            if wodds is None:
                continue
            won = (fin == 1)
            payoff = wodds if won else 0.0  # 単勝払戻=確定オッズ
            stake += 1.0
            ret += payoff
            picks.append({"race_id": rid, "num": h, "odds": wodds,
                          "drift": f["drift"], "won": won})
    roi = ret / stake if stake else 0.0
    ci = bootstrap_ci([{"odds": p["odds"], "won": p["won"], "stake": 1.0}
                       for p in picks], flat=True) if picks else {"lo": 0, "hi": 0}
    return {"n_bets": len(picks), "roi": roi, "ci_lo": ci["lo"], "ci_hi": ci["hi"],
            "picks": picks}


def main(argv=None):
    ap = argparse.ArgumentParser(description="late money(オッズ時系列)戦略の検証")
    ap.add_argument("--db", required=True)
    ap.add_argument("--threshold", type=float, default=-0.05,
                    help="steamed 判定の drift 閾値(負=短縮)")
    ap.add_argument("--margin", type=float, default=0.0)
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    n_snap_races = conn.execute(
        "SELECT COUNT(DISTINCT race_id) FROM odds_snapshots").fetchone()[0]
    print(f"[latemoney] スナップショットのあるレース: {n_snap_races}")
    if n_snap_races == 0:
        print("  まだ odds_snapshots がありません。poll-odds を開催日に回して蓄積してください。")
        return 0
    r = backtest_steam(conn, threshold=args.threshold, margin=args.margin)
    print(f"  steam戦略: 賭け{r['n_bets']}点  ROI {r['roi']*100:.1f}%  "
          f"95%CI[{r['ci_lo']*100:.1f}, {r['ci_hi']*100:.1f}]  "
          f"{'★EV>1(CI下限>100%)' if r['ci_lo']>1.0 and r['n_bets']>=100 else '未達/データ不足'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
