# evmodel — 期待値 > 1.0 を狙う二段階モデル

`docs/EV_METHODOLOGY.md` の手法（Benter 二段階）の実装。**標準ライブラリのみ**で学習・
walk-forward 検証まで完結する（numpy 等は不要。将来 GBDT へ差し替える場合のみ任意依存）。

## なぜこれで EV>1.0 を狙えるか

現行ツール（`index.html` / `backtest_example.py`）は勝率を**単勝オッズの逆数**から作るため、
`EV = 勝率 × オッズ = (1 − 控除率) < 1.0` に構造的に固定される。本パッケージは勝率を
**市場オッズから独立**に推定し、市場が過小評価した馬（`p > q`）で `EV = p × オッズ > 1` を拾う。

- **Stage1** `condlogit.ConditionalLogit`: 斤量・馬体重・過去走指標などファンダメンタル特徴量の
  条件付きロジット（市場オッズ非使用）→ 勝率 π
- **較正** `condlogit.isotonic_*`: π を実現勝率に合わせる（賭博では精度より較正が効く）
- **Stage2** 同じ条件付きロジットで `[log π, log q]` を結合 → `p ∝ π^α · q^β`
- **判定/資金** `backtest`: `EV = p × odds`、`EV > 1 + margin` を分数 Kelly で配分
- **検証** `backtest.walk_forward`: 時系列 fold（過去で学習→未来で検証）＋ブートストラップ CI

## スマホ完結の仕組み（重い処理はCI、軽い計算はブラウザ）

学習は端末で走らせない。**GitHub Actions（スマホの GitHub アプリ/Webから起動）**が回し、
成果物を main にコミット → GitHub Pages が配信 → **`ev2.html` が自動読込**する。PC は不要。

```
[スマホ] Actions「レースデータ収集」を起動        … data-YYYY Release にDB蓄積
[スマホ] Actions「モデル学習・π生成」を起動        … 下記をCIが実行
   └ mergedb → pipeline(ev2_predictions.json) → serve(ev2_upcoming.json) → main へ公開
[スマホ] ev2.html を開く（GitHub Pages）
   ├ 📡 ライブ：発走前にブラウザが単勝オッズだけ取得し p∝π^α·q^β・EV=p×odds を計算
   └ 📊 検証：walk-forward の予測を閲覧（ROI・CI）
```

**なぜライブがブラウザだけで済むか**: Stage1 の独立勝率 π はオッズに依存しないので CI が
先に計算して `ev2_upcoming.json` に入れておける。ブラウザは閲覧時にライブオッズ q を取って
Stage2 の結合 `p ∝ π^α·q^β` と `EV=p×odds` を計算するだけ（軽量・常に最新オッズ）。

## CLI（サーバ/CI 側。ローカル検証にも使える）

```bash
# 0) 特徴量が未収集の旧DBは結果ページ再取得で埋める（オッズは再取得しない）
python -m scraper.ingest --db keiba_2025.db --year 2025 --refresh-features

# 1) 複数年DBを統合
python -m evmodel.mergedb --out keiba_all.db keiba_2016.db keiba_2017.db ...

# 2) 学習・walk-forward 検証 → 検証閲覧用 JSON
python -m evmodel.pipeline --db keiba_all.db --out ev2_predictions.json --min-train 500

# 3) 対象開催日の各馬 独立勝率π → ライブ用 JSON（出馬表を取得して算出）
python -m evmodel.serve --db keiba_all.db --date 20250105 --out ev2_upcoming.json

# 4) 手法の妥当性を合成データで確認（実データ不要）
python -m evmodel.selftest
```

## 出力の読み方（重要）

- **ΔR²**: 市場のみ vs 二段階の McFadden R² 差＝市場超過エッジ。**正で初めて意味がある**。
- **均等 ROI と 95%CI**: CI 下限が 100% を超えて初めて「控除率の壁を破った」と言える。
- 日本市場では favorite-longshot bias が弱い/逆転する（`docs/EV_METHODOLOGY.md` §4）。
  欧米の大穴除外レシピは移植しない。特徴量で素直にエッジを作る。
- **単一年・単一セットの 100% 超は過剰適合を疑う。** walk-forward 全 fold と CI で判断する。

## 現状の限界

- Stage1 の特徴量は結果ページから取れる範囲（斤量・馬体重・騎手・過去走のタイム/上がり/成績）。
  血統・調教・詳細通過順は未収集（別ページが必要、将来拡張）。
- データ量が閾値未満のときは前半学習・後半検証にフォールバックする。10 年分での walk-forward
  が本命。
