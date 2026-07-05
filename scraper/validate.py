"""整合性チェック CLI。

  python -m scraper.validate --db keiba_2025.db

チェック内容(取り込み済みレースごと):
1. 払戻×オッズ突合 — 的中 combo の payout_yen ≈ round(odds×100)。
   複勝/ワイドは [min,max]×100 の範囲内か(±10円の丸め許容)
2. 頭数整合 — 取消等を除く出走頭数 = 単勝オッズのキー数
3. 着順整合 — finish_pos=1..3 の馬が 3連単払戻の combo と一致
結果は races.check_status (1=ok, 2=warning) と check_notes に書き込む。
"""

import argparse
import json
import sys
from collections import Counter

from . import db

PAYOUT_TOLERANCE_YEN = 10


def check_race(conn, race_id):
    """1レースを検証して警告リストを返す(空=OK)。"""
    notes = []
    entries = conn.execute(
        "SELECT * FROM entries WHERE race_id = ? ORDER BY horse_num", (race_id,)
    ).fetchall()
    payouts = conn.execute(
        "SELECT * FROM payouts WHERE race_id = ?", (race_id,)
    ).fetchall()

    # --- 1. 払戻×オッズ突合 ---
    odds_cache = {}
    for p in payouts:
        bt = p["bet_type"]
        if bt not in odds_cache:
            odds_cache[bt] = db.load_odds_blob(conn, race_id, bt) or {}
        vals = odds_cache[bt].get(p["combo"])
        if vals is None:
            notes.append(f"払戻{bt}:{p['combo']} がオッズに存在しない")
            continue
        odds, odds_max = vals[0], vals[1]
        if odds_max is not None:  # 複勝・ワイドはレンジ
            lo = odds * 100 - PAYOUT_TOLERANCE_YEN
            hi = odds_max * 100 + PAYOUT_TOLERANCE_YEN
            if not (lo <= p["payout_yen"] <= hi):
                notes.append(
                    f"払戻{bt}:{p['combo']} {p['payout_yen']}円がオッズ範囲"
                    f"[{odds}-{odds_max}]×100 と不一致"
                )
        else:
            expected = round(odds * 100)
            if abs(p["payout_yen"] - expected) > PAYOUT_TOLERANCE_YEN:
                notes.append(
                    f"払戻{bt}:{p['combo']} {p['payout_yen']}円 ≠ オッズ{odds}×100={expected}円"
                )

    # --- 2. 頭数整合 ---
    starters = [e for e in entries if e["finish_status"] not in ("取消", "除外")]
    win_odds = db.load_odds_blob(conn, race_id, 1)
    if win_odds is not None and len(win_odds) != len(starters):
        notes.append(f"出走頭数{len(starters)} ≠ 単勝オッズ{len(win_odds)}件")

    # --- 3. 着順×3連単払戻の整合 ---
    top3 = sorted(
        (e for e in entries if e["finish_pos"] in (1, 2, 3)),
        key=lambda e: e["finish_pos"],
    )
    tan3 = [p for p in payouts if p["bet_type"] == 8]
    if len(top3) == 3 and tan3:
        expected_combo = db.canonical_combo(8, [e["horse_num"] for e in top3])
        if all(p["combo"] != expected_combo for p in tan3):
            notes.append(
                f"3連単払戻 {[p['combo'] for p in tan3]} が着順 {expected_combo} と不一致"
            )

    return notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="DB 整合性チェック")
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, help="チェックするレース数上限(先頭から)")
    ap.add_argument("--recheck", action="store_true", help="検証済みレースも再チェック")
    args = ap.parse_args(argv)

    conn = db.open_db(args.db)
    q = "SELECT race_id FROM races WHERE status_result = 1 AND status_odds = 1"
    if not args.recheck:
        q += " AND check_status IS NULL"
    q += " ORDER BY kaisai_date, race_id"
    if args.limit:
        q += f" LIMIT {args.limit}"

    race_ids = [r["race_id"] for r in conn.execute(q).fetchall()]
    stats = Counter()
    for race_id in race_ids:
        notes = check_race(conn, race_id)
        conn.execute(
            "UPDATE races SET check_status = ?, check_notes = ? WHERE race_id = ?",
            (2 if notes else 1, json.dumps(notes, ensure_ascii=False) if notes else None, race_id),
        )
        stats["warning" if notes else "ok"] += 1
        if notes:
            print(f"[validate] {race_id}: {notes}")
    conn.commit()

    # 欠損レポート
    missing_dist = Counter()
    for row in conn.execute(
        "SELECT odds_types_missing, n_horses FROM races WHERE odds_types_missing IS NOT NULL"
    ).fetchall():
        for t in json.loads(row["odds_types_missing"] or "[]"):
            missing_dist[f"type{t}(n_horses={row['n_horses']})"] += 1
    errors = conn.execute(
        "SELECT COUNT(*) AS n FROM races WHERE status_result = 2 OR status_odds = 2"
    ).fetchone()["n"]

    print(f"[validate] チェック: {dict(stats)} / エラーレース: {errors}件")
    if missing_dist:
        print(f"[validate] オッズ欠損分布: {dict(missing_dist)}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
