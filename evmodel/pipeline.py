"""実データ(scraper 収集 DB)で二段階モデルを学習・walk-forward 検証し、
新タブ ev2.html 用の予測 JSON を書き出す CLI。

  python -m evmodel.pipeline --db keiba_2025.db [--out ev2_predictions.json] [--min-train 300]

データが一部しか集まっていなくても動く(min-train 未満なら前半学習・後半検証にフォールバック)。
特徴量カラムが未収集(horse_id 等 NULL)の場合は、先に
  python -m scraper.ingest --db keiba_YYYY.db --year YYYY --refresh-features
で埋めること。
"""

import argparse
import json

from .features import load_races
from .model import TwoStageModel
from .backtest import walk_forward, simulate_win_bets, bootstrap_ci


def main(argv=None):
    ap = argparse.ArgumentParser(description="二段階EVモデルの学習・検証・予測エクスポート")
    ap.add_argument("--db", required=True)
    ap.add_argument("--year", help="対象年で絞る(kaisai_date 前方一致)")
    ap.add_argument("--out", default="ev2_predictions.json", help="ev2.html 用予測JSON")
    ap.add_argument("--min-train", type=int, default=300, help="walk-forward 最小学習レース数")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--margin", type=float, default=0.0, help="EV 閾値 (EV>1+margin で購入)")
    ap.add_argument("--kelly", type=float, default=0.5, help="分数 Kelly 係数")
    ap.add_argument("--export-races", type=int, default=300,
                    help="ev2_predictions.json に載せる直近レース数の上限(統計は全OOSで計算)")
    args = ap.parse_args(argv)

    races = load_races(args.db, limit_year=args.year)
    labeled = [r for r in races if r["win"] is not None]
    print(f"[pipeline] レース {len(races)} 件 (勝ち馬確定 {len(labeled)} 件) を読み込み")
    if len(labeled) < 30:
        print("[pipeline] データが少なすぎます。--refresh-features で特徴量を埋め、"
              "収集を進めてから再実行してください。")
        if not labeled:
            return 1

    preds, folds = walk_forward(
        races, lambda: TwoStageModel(l2=1e-3, lr=0.08, iters=300),
        n_folds=args.folds, min_train=args.min_train)

    print("\n[fold 別]")
    for i, f in enumerate(folds, 1):
        print(f"  fold{i}: 学習{f['train_n']:>5} 検証{f['test_n']:>5}  "
              f"ΔR²={f['delta_r2']:+.4f}  α={f['alpha']:.3f} β={f['beta']:.3f}  "
              f"均等ROI={f['flat_roi']*100:.1f}% ({f['n_bets']}点)")

    sim_flat = simulate_win_bets(preds, flat=True)
    sim_kelly = simulate_win_bets(preds, margin=args.margin, kelly=args.kelly, cap=0.05)
    ci = bootstrap_ci(sim_flat["picks"], flat=True)
    dr2 = sum(f["delta_r2"] for f in folds) / len(folds) if folds else 0.0

    print("\n[OOS 総合]")
    print(f"  平均ΔR²(市場超過エッジ) : {dr2:+.4f}")
    print(f"  均等買い ROI            : {sim_flat['roi']*100:.1f}%  "
          f"(賭け {sim_flat['n_bets']}点, 95%CI {ci['lo']*100:.1f}〜{ci['hi']*100:.1f}%)")
    print(f"  1/2 Kelly ROI           : {sim_kelly['roi']*100:.1f}%  "
          f"(投下 {sim_kelly['stake_total']:.1f}, 純益 "
          f"{sim_kelly['return_total']-sim_kelly['stake_total']:+.1f})")
    verdict = "エッジ検出(要さらなる検証)" if (dr2 > 0 and sim_flat["roi"] > 1.0
                                              and ci["lo"] > 1.0) else \
              "有意なエッジ無し(控除率の壁を破れず)"
    print(f"  判定: {verdict}")

    export_predictions(args.out, preds_races(races, preds, limit=args.export_races))
    print(f"\n[pipeline] 予測(直近{args.export_races}レース上限)を {args.out} に書き出しました")
    return 0


def preds_races(all_races, oos_preds, limit=None):
    """OOS 予測が付いたレースだけを ev2.html 用の構造にまとめる(直近 limit 件に制限可)。"""
    # oos_preds は per-horse フラット。race_id ごとに束ね直す。
    by_race = {}
    for p in oos_preds:
        by_race.setdefault(p["race_id"], []).append(p)
    keep = set(by_race)
    if limit and len(keep) > limit:
        keep = set(sorted(by_race)[-limit:])  # race_id 昇順=時系列、直近を残す
    races_by_id = {r["race_id"]: r for r in all_races}
    result = {}
    for rid, phorses in by_race.items():
        if rid not in keep:
            continue
        r = races_by_id.get(rid)
        if not r:
            continue
        horses = []
        for i, ph in enumerate(phorses):
            idx = i  # walk_forward は per-race 昇順で push している
            horses.append({
                "num": r["nums"][idx] if idx < len(r["nums"]) else idx + 1,
                "name": r["names"][idx] if idx < len(r["names"]) else "",
                "p": round(ph["p"], 5),
                "q": round(r["q"][idx], 5) if idx < len(r["q"]) else None,
                "odds": round(ph["odds"], 1),
                "won": ph["won"],
            })
        result[rid] = {"date": r["date"], "horses": horses}
    return result


def export_predictions(path, races_map):
    payload = {
        "schema": "ev2-predictions/1",
        "note": "p=二段階モデルの結合勝率, q=市場勝率, EV=p*odds。詳細 docs/EV_METHODOLOGY.md",
        "races": races_map,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
