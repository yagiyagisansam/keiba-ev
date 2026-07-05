"""ライブ採点: 過去DBで学習したモデルで「未来レース」の Stage1 勝率 π を書き出す。

π はオッズに依存しない(ファンダメンタル)ので、CI(GitHub Actions)で先に計算しておける。
スマホのブラウザ(ev2.html)は発走前にライブ単勝オッズだけ取得し、
  q_i = 正規化(1/odds),  p_i ∝ π_i^α · q_i^β,  EV_i = p_i × odds_i
を計算する。重い学習はサーバ、軽い結合はブラウザ、という分担でスマホ完結にする。

  python -m evmodel.serve --db keiba_2025.db --date 20250105 --out ev2_upcoming.json
  python -m evmodel.serve --db keiba_2025.db --race-id 202506010101 --out ev2_upcoming.json

出力(ev2_upcoming.json):
  {"schema":"ev2-upcoming/1","model":{"alpha":..,"beta":..},
   "races":{race_id:{"name":..,"date":..,"horses":[{"num":..,"name":..,"pi":..}]}}}
"""

import argparse
import json

from scraper import config
from scraper.enumerate_races import fetch_race_list
from scraper.http_client import BlockGuard, PoliteSession
from scraper.parse_shutuba import ShutubaNotAvailable, parse_shutuba

from .features import compute_feat, FEAT_NAMES, load_races
from .model import TwoStageModel


def score_race(model, stats, entries, race_date):
    """出馬表の entries に Stage1 勝率 π を付ける。"""
    horse_stats, jockey_stats, trainer_stats = stats
    field = len(entries)
    X = []
    for e in entries:
        hs = horse_stats.get(e["horse_id"])
        js = jockey_stats.get(e["jockey"])
        ts = None  # 出馬表に調教師は無い → 既定値(標準化後は定数で無害)
        feat = compute_feat(e, hs, js, ts, field, race_date)
        X.append([feat[k] for k in FEAT_NAMES])
    pi = model.stage1_probs(X)
    return pi


def build(db_path, race_ids=None, date=None, out_path="ev2_upcoming.json", limit_year=None):
    races, stats = load_races(db_path, limit_year=limit_year, return_stats=True)
    labeled = [r for r in races if r["win"] is not None]
    print(f"[serve] 学習用 {len(labeled)} レースでモデル学習")
    model = TwoStageModel(l2=1e-3, lr=0.08, iters=300).fit(labeled)
    alpha, beta = model.stage2.beta

    guard = BlockGuard()
    session = PoliteSession(sleep_sec=0.5, guard=guard)
    if not race_ids:
        stubs = fetch_race_list(session, date)
        race_ids = [s["race_id"] for s in stubs]
        names = {s["race_id"]: s.get("race_name") for s in stubs}
    else:
        names = {}

    out = {"schema": "ev2-upcoming/1", "model": {"alpha": alpha, "beta": beta}, "races": {}}
    for rid in race_ids:
        try:
            html = session.get_text(
                f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={rid}")
            entries = parse_shutuba(html)
        except ShutubaNotAvailable:
            print(f"[serve] {rid}: 出馬表なし、スキップ")
            continue
        if not entries:
            continue
        date_str = rid[:4] + "0000" if date is None else date
        pi = score_race(model, stats, entries, date or "20991231")
        out["races"][rid] = {
            "name": names.get(rid), "date": date,
            "horses": [{"num": e["horse_num"], "name": e["horse_name"],
                        "pi": round(pi[i], 5)} for i, e in enumerate(entries)],
        }
        print(f"[serve] {rid}: {len(entries)}頭 π算出")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[serve] {len(out['races'])}レースを {out_path} に出力 "
          f"(α={alpha:.3f}, β={beta:.3f}) → ev2.html ライブモードで使用")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="未来レースの Stage1 π を書き出す(ライブ採点)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", help="対象開催日 YYYYMMDD(その日の全レースを採点)")
    ap.add_argument("--race-id", action="append", help="対象レースID(複数可)")
    ap.add_argument("--year", help="学習に使う年で絞る(kaisai_date 前方一致)")
    ap.add_argument("--out", default="ev2_upcoming.json")
    args = ap.parse_args(argv)
    if not (args.date or args.race_id):
        ap.error("--date か --race-id が必要です")
    build(args.db, race_ids=args.race_id, date=args.date, out_path=args.out,
          limit_year=args.year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
