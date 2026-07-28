# keiba-ev

競馬の**期待値(EV)プラス**を狙う買い目計算ツールと、その戦略を過去データで
検証するためのデータ基盤。

3つの構成要素からなる:

1. **EV計算ツール(GitHub Pages)** — `index.html`(全4券種)/ `umatan.html`(馬単特化)。
   netkeiba のオッズをライブ取得し、Harville モデルで各買い目の的中率・理論オッズ・期待値を計算する。
2. **過去データ収集基盤(`scraper/` + GitHub Actions + Releases)** — JRA 全レースの
   確定オッズ(全8券種・全組み合わせ)・結果・払戻を年別 SQLite に収集する。バックテスト用。
3. **検証ツール(`backtest_example.py` / `analysis/verify_ev.py`)** — 収集した過去データで
   EV計算の精度やバックテスト回収率を検証する。

## ディレクトリ構成

| パス | 内容 |
|---|---|
| `index.html` / `umatan.html` | EV計算ツール(GitHub Pages で公開) |
| `scraper/` | 収集パイプライン(列挙・パーサ・取り込み・整合性チェック) |
| `analysis/verify_ev.py` | EV精度検証ハーネス(2軸の一致率を集計) |
| `backtest_example.py` | DB消費サンプル(Harville EV でバックテスト実演) |
| `.github/workflows/collect-data.yml` | 収集ワークフロー(cron + 手動) |
| `docs/DATABASE.md` | **DBスキーマ・収集運用の詳細(最重要リファレンス)** |
| `tests/` | パーサ単体テスト(fixtures ベース、ネット不要) |

## 収集済みデータ

確定オッズ・結果・払戻は GitHub の **Releases**(`data-2016`〜`data-2026`)に
`keiba_YYYY.db.gz` として置いてある(リポジトリ本体には含めない)。

| 年 | レース数 | 年 | レース数 |
|---|---|---|---|
| 2016 | 3,454 | 2021 | 3,456 |
| 2017 | 3,455 | 2022 | 3,456 |
| 2018 | 3,454 | 2023 | 3,456 |
| 2019 | 3,452 | 2024 | 3,454 |
| 2020 | 3,456 | 2025 | 3,239 |
| | | 2026 | 収集継続中 |

合計 約36,000レース。以後の開催は cron が自動追加する。

## クイックスタート

### データを取得して使う

```bash
pip install -r requirements.txt   # requests のみ

# 対象年の DB を Release から取得
gh release download data-2025 --pattern 'keiba_2025.db.gz'
gunzip keiba_2025.db.gz

# バックテスト例(指定日の EV>=閾値 の買い目を100円ずつ買った収支)
python backtest_example.py --db keiba_2025.db --date 20250105 --ev-min 105
```

スキーマ(テーブル・オッズBLOBの展開方法・combo正規化)は
[`docs/DATABASE.md`](docs/DATABASE.md) を参照。

### EV計算の精度を検証する

```bash
# 馬連・的中率>=20% かつ 期待値>=105 の買い目で、予測と実測の一致率を集計
python -m analysis.verify_ev --db keiba_2025.db \
    --bet-type 馬連 --min-hit-pct 20 --min-ev 105 \
    --csv picks.csv --json summary.json
```

- `--bet-type`: 馬連 / 馬単 / 3連複 / 3連単
- 出力: ①的中率の一致率(実的中率÷予測)②期待値の一致率(実払戻÷予測期待値)+ 帯別キャリブレーション
- 2025馬連の検証では回収率96.6%(モデルは的中率を約17%過大評価)。過去の手動検証と同傾向

### 収集を回す

GitHub Actions → 「レースデータ収集」→ Run workflow。

- 単日スモーク: `date=20250105`
- 年指定: `year=2025`(`workers=8, sleep=0.3` で1年約35分)
- cron 自動収集: リポジトリ変数 `COLLECT_YEARS`(例 `2026,2025`)を対象に毎日実行
- netkeiba がランナーIPを弾く場合は Actions secret `PROXY_URL`(Cloudflare Worker)でフォールバック

詳細は [`docs/DATABASE.md`](docs/DATABASE.md)、収集完了までの経緯は
[`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) を参照。
