"""合成データによる手法の妥当性検証(実データ不要・この環境で完結)。

2つのシナリオでパイプラインの正しさを確認する:
  (A) 効率的市場: 市場オッズが真の勝率どおり → 独立モデルにエッジは無いはず。
      ROI ≈ (1 - 控除率)、ΔR² ≈ 0。ここで EV>1 が出るなら過剰適合/リークの疑い。
  (B) 非効率市場: 市場が真の勝率を誤って価格付け(ノイズ大)、かつ我々のモデルは
      真の能力特徴量を持つ → 過小評価馬で p>q となり ROI>1・ΔR²>0 になるはず。

(A) で偽の勝ちが出ず、(B) で勝てることが同時に示せて初めて、
「EV>1 は正しい条件下でのみ出る」ことの担保になる。

実行: python -m evmodel.selftest
"""

import math
import random

from .model import TwoStageModel
from .backtest import walk_forward, simulate_win_bets, bootstrap_ci

TAKEOUT = 0.20  # 単勝控除率(JRA ≈ 20%)


def softmax(xs):
    m = max(xs)
    e = [math.exp(x - m) for x in xs]
    z = sum(e)
    return [v / z for v in e]


def make_races(n_races, n_feat=6, market_noise=0.0, seed=1):
    """真の能力 s=w·x のレースを生成。市場は s をノイズ付きで観測してオッズを付ける。"""
    rng = random.Random(seed)
    w = [rng.gauss(0, 1) for _ in range(n_feat)]
    races = []
    for _ in range(n_races):
        k = rng.randint(8, 14)
        X = [[rng.gauss(0, 1) for _ in range(n_feat)] for _ in range(k)]
        strength = [sum(w[j] * x[j] for j in range(n_feat)) for x in X]
        true_p = softmax(strength)
        # 勝ち馬を真の確率からサンプリング
        r = rng.random()
        acc = 0.0
        win = k - 1
        for i, p in enumerate(true_p):
            acc += p
            if r <= acc:
                win = i
                break
        # 市場: 真の強さをノイズ付きで観測 → 市場勝率 q → オッズ=(1-控除)/q
        mkt_strength = [strength[i] + rng.gauss(0, market_noise) for i in range(k)]
        q = softmax(mkt_strength)
        odds = [(1.0 - TAKEOUT) / max(q[i], 1e-6) for i in range(k)]
        races.append({"X": X, "q": q, "win": win, "odds": odds, "date": _})
    return races


def run_scenario(name, market_noise):
    races = make_races(1600, market_noise=market_noise, seed=42)
    preds, folds = walk_forward(
        races, lambda: TwoStageModel(l2=1e-3, lr=0.08, iters=300), n_folds=4, min_train=400)
    sim_flat = simulate_win_bets(preds, flat=True)
    sim_kelly = simulate_win_bets(preds, margin=0.0, kelly=0.5, cap=0.05)
    ci = bootstrap_ci(sim_flat["picks"], flat=True)
    dr2 = sum(f["delta_r2"] for f in folds) / len(folds)
    a = sum(f["alpha"] for f in folds) / len(folds)
    b = sum(f["beta"] for f in folds) / len(folds)
    print(f"\n=== シナリオ {name} (market_noise={market_noise}) ===")
    print(f"  OOS レース数={len(races)}  平均ΔR²={dr2:+.4f}  α(自モデル)={a:.3f}  β(市場)={b:.3f}")
    print(f"  均等買い ROI     : {sim_flat['roi']*100:5.1f}%  "
          f"(賭け {sim_flat['n_bets']}点, 95%CI {ci['lo']*100:.1f}〜{ci['hi']*100:.1f}%)")
    print(f"  1/2 Kelly 収支   : 資金比 {(sim_kelly['return_total']-sim_kelly['stake_total']):+.2f} "
          f"(投下 {sim_kelly['stake_total']:.1f}, ROI {sim_kelly['roi']*100:.1f}%)")
    return {"roi": sim_flat["roi"], "dr2": dr2, "ci": ci, "n": sim_flat["n_bets"]}


def main():
    print("控除率 =", TAKEOUT, " → 情報ゼロなら ROI は理論上 %.0f%% に張り付く" % ((1 - TAKEOUT) * 100))
    eff = run_scenario("A:効率的市場(市場=真の勝率)", market_noise=0.0)
    ineff = run_scenario("B:非効率市場(市場が誤価格)", market_noise=0.6)

    print("\n--- 判定 ---")
    # (A) 効率市場では控除率の壁を破れない(ROI < 100% 近辺、ΔR² 小)
    ok_a = eff["roi"] < 1.02
    # (B) 非効率市場では独立モデルが控除率を超える(ROI > 100%、ΔR²>0)
    ok_b = ineff["roi"] > 1.03 and ineff["dr2"] > 0
    print(f"  (A) 効率市場で EV>1 が出ない  : {'OK' if ok_a else 'NG'} "
          f"(ROI {eff['roi']*100:.1f}%)")
    print(f"  (B) 非効率市場で EV>1 を達成  : {'OK' if ok_b else 'NG'} "
          f"(ROI {ineff['roi']*100:.1f}%, ΔR² {ineff['dr2']:+.4f})")
    print("\n結論: 独立ファンダメンタル推定が市場を上回るときのみ回収率100%超が出る。"
          "\n      現行手法(prob=市場逆数)は定義上シナリオAと同じで、控除率の壁を破れない。")
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    raise SystemExit(main())
