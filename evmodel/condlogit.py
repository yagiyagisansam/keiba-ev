"""条件付きロジット(多項ロジット)の純Python実装。

依存ライブラリ無し(numpy 不要)で、この環境だけで学習・検証が回せることを優先。
将来 LightGBM 等へ差し替える場合も、Stage1 の出力(レース内で正規化した勝率)と
インターフェースを揃えれば pipeline はそのまま使える。

Benter 二段階の両方をこの1関数で表現できる:
  Stage1: 特徴量ベクトル x_i(斤量・馬体重・過去走指標…市場オッズ非使用) → 勝率 π_i
  Stage2: 2特徴 [log π_i, log q_i] の条件付きロジット → 結合勝率 p_i ∝ π_i^α q_i^β

モデル: レース r で馬 i が勝つ確率 P_i = softmax_i(β·x_i)。
損失  : 平均負の対数尤度 (勝ち馬の -log P) + L2。Adam で最適化。
勾配  : ∂NLL/∂β_k = Σ_i (P_i - y_i) x_{i,k} をレース平均。
"""

import math


def softmax(scores):
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    z = sum(exps)
    return [e / z for e in exps]


class Standardizer:
    """特徴量を平均0・分散1に標準化する(列ごと)。条件付きロジットの収束安定用。"""

    def __init__(self):
        self.mean = None
        self.std = None

    def fit(self, rows):
        n = len(rows)
        d = len(rows[0]) if n else 0
        self.mean = [0.0] * d
        self.std = [1.0] * d
        for j in range(d):
            col = [r[j] for r in rows]
            mu = sum(col) / n
            var = sum((v - mu) ** 2 for v in col) / n
            self.mean[j] = mu
            self.std[j] = math.sqrt(var) if var > 1e-12 else 1.0
        return self

    def transform(self, rows):
        return [[(r[j] - self.mean[j]) / self.std[j] for j in range(len(r))] for r in rows]


class ConditionalLogit:
    """レース単位の条件付きロジット。

    races: [ (X, winner_idx) ] で、X は 1レースの [特徴量ベクトル,...]、
    winner_idx は勝ち馬(1着)の X 内インデックス。中止等で勝ち馬不明のレースは学習前に除外。
    """

    def __init__(self, l2=1e-3, lr=0.05, iters=400, fit_intercept=False):
        self.l2 = l2
        self.lr = lr
        self.iters = iters
        # 条件付きロジットはレース内 softmax なので定数項(全馬共通)は打ち消える。
        # fit_intercept は API 互換のため残すが常に無効。
        self.fit_intercept = False
        self.beta = None
        self.dim = None

    def fit(self, races, verbose=False):
        self.dim = len(races[0][0][0])
        beta = [0.0] * self.dim
        # Adam
        m = [0.0] * self.dim
        v = [0.0] * self.dim
        b1, b2, eps = 0.9, 0.999, 1e-8
        n_races = len(races)
        for t in range(1, self.iters + 1):
            grad = [0.0] * self.dim
            for X, win in races:
                scores = [sum(beta[k] * x[k] for k in range(self.dim)) for x in X]
                P = softmax(scores)
                for i, x in enumerate(X):
                    coef = P[i] - (1.0 if i == win else 0.0)
                    for k in range(self.dim):
                        grad[k] += coef * x[k]
            for k in range(self.dim):
                grad[k] = grad[k] / n_races + 2 * self.l2 * beta[k]
                m[k] = b1 * m[k] + (1 - b1) * grad[k]
                v[k] = b2 * v[k] + (1 - b2) * grad[k] * grad[k]
                mhat = m[k] / (1 - b1 ** t)
                vhat = v[k] / (1 - b2 ** t)
                beta[k] -= self.lr * mhat / (math.sqrt(vhat) + eps)
            if verbose and (t % 50 == 0 or t == 1):
                print(f"  iter {t:4d}  NLL={self.nll(races, beta):.5f}")
        self.beta = beta
        return self

    def nll(self, races, beta=None):
        beta = beta if beta is not None else self.beta
        total = 0.0
        for X, win in races:
            scores = [sum(beta[k] * x[k] for k in range(self.dim)) for x in X]
            P = softmax(scores)
            total += -math.log(max(P[win], 1e-12))
        return total / len(races)

    def predict_race(self, X):
        """1レースの特徴量行列 → 各馬の勝率(合計1)。"""
        scores = [sum(self.beta[k] * x[k] for k in range(self.dim)) for x in X]
        return softmax(scores)


def mcfadden_r2(nll_model, nll_null):
    """McFadden の擬似決定係数 1 - LL_model/LL_null。null は一様(1/頭数)モデル。"""
    return 1.0 - (nll_model / nll_null) if nll_null > 0 else 0.0


def null_nll(races):
    """一様モデル(各馬 1/頭数)の平均 NLL。McFadden R² の分母。"""
    total = 0.0
    for X, _win in races:
        total += math.log(len(X))
    return total / len(races)


# ---------------------------------------------------------------------------
# 較正: Pool-Adjacent-Violators による isotonic regression(単調・ノンパラ)
# ---------------------------------------------------------------------------

def isotonic_fit(pairs):
    """pairs = [(p_pred, y)] を p 昇順に PAV で単調回帰し、階段関数の節点を返す。

    返り値: (xs, ys) 昇順の x に対する較正後 y。lookup は isotonic_apply。
    """
    pts = sorted(pairs, key=lambda t: t[0])
    xs = [p for p, _ in pts]
    ys = [float(y) for _, y in pts]
    w = [1.0] * len(ys)
    # PAV
    i = 0
    while i < len(ys) - 1:
        if ys[i] > ys[i + 1] + 1e-15:
            # マージ
            new_y = (ys[i] * w[i] + ys[i + 1] * w[i + 1]) / (w[i] + w[i + 1])
            new_w = w[i] + w[i + 1]
            ys[i] = new_y
            w[i] = new_w
            del ys[i + 1]
            del w[i + 1]
            del xs[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return xs, ys


def isotonic_apply(model, p):
    xs, ys = model
    if not xs:
        return p
    # xs 上の最近傍(以下)の節点値を返す
    lo, hi = 0, len(xs) - 1
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if xs[mid] <= p:
            lo = mid
        else:
            hi = mid - 1
    return ys[lo]
