# セッション引き継ぎ: 過去10年レースDBの構築

このセッションの主目的は **「期待値プラスを狙う買い目計算ツールのバックテスト用に、
過去約10年の競馬レースデータをDB化すること」**。目的は達成済み。

## 完了事項

- **収集パイプライン `scraper/`**: netkeiba から開催日→レース→結果/払戻+全8券種確定オッズを取得。
  レース単位のリジューム(status フラグ + 1レース1トランザクション)、4〜8ワーカー並列取得、
  時間予算(`--max-minutes`)での自主終了、連続失敗時のブロック検知(`BlockGuard`)。
- **GitHub Actions `collect-data.yml`**: cron + 手動 dispatch。GitHub Release を実行間の
  永続ストレージにして時間予算内でバックフィルを継続。
- **2016〜2026年を収集完了**(下表)。整合性チェック(`scraper/validate.py`)も各実行で自動化。
- **検証ハーネス `analysis/verify_ev.py`**: EV計算(Harville モデル)の精度を2軸の一致率で検証。
  2025馬連(的中率≥20% ∧ 期待値≥105、321点)で **回収率96.6% / 的中率を約17%過大評価**。
  これは過去に手動で行った検証と同傾向であり、**DB取得が正しく行えていることの裏付け**となった。

## データ現状(GitHub Releases)

`data-2016`〜`data-2026` に `keiba_YYYY.db.gz` として格納。

| 年 | done | 備考 |
|---|---|---|
| 2016–2017 | 3,454 / 3,455 | |
| 2018 | 3,454 | 中止1レースを skip(正常) |
| 2019–2024 | 3,452〜3,456 | |
| 2025 | 3,239 | 通年 |
| 2026 | 1,774 | 当日開催の20件は結果確定後に cron が自動補完(pending) |

収集エラーは全年 0 件。以降の開催は cron が自動追加する。

## ブランチ / マージ状態

- **`scraper/` 一式・ワークフロー・`backtest_example.py`・`docs/DATABASE.md`** は
  PR #3 で **main にマージ済み**。
- **`analysis/verify_ev.py`・`README.md`・本ファイル** は作業ブランチ
  `claude/horse-racing-db-ev-1xn9i7`(main から再作成)にあり、**PR で main へ提出**。
- DB ファイル(`keiba_*.db`)は `.gitignore` 済み。リポジトリには含めず Release で配布。

## 既知の注意点

- **validate の警告**: 「払戻 ≠ オッズ×100」の警告はほぼ全て**同着レースの按分払戻**で、データは正常
  (チェック側が同着を未考慮なだけ)。エラー(status=2)とは別物で、収集は成功している。
- **EVモデルの過大評価**: Harville モデルは人気馬の連対率を高く見積もる傾向。的中率を相対約17%過大評価。
  改善余地 → 控除率補正、収集した10年データでのキャリブレーション係数の導入。
- **netkeiba ブロック対策**: ランナーIPが弾かれたら Actions secret `PROXY_URL` に
  Cloudflare Worker(`?url=` 透過形式、index.html と同じ)を設定するとフォールバックする。
- **cron の対象年**: リポジトリ変数 `COLLECT_YEARS` 未設定時は現在年のみが対象。
  継続収集したい年をカンマ区切りで設定すること(例 `2026,2025`)。

## 次の候補(未着手)

- 買い目計算ツール本体(期待値プラスの自動選定)の設計・実装。
- `scraper/validate.py` の同着対応(警告の解消)。
- 他券種(馬単・3連複・3連単)・複数年での精度検証(`analysis/verify_ev.py --bet-type ...`)。
- モデルのキャリブレーション(過大評価の補正)。

## 参照

- スキーマ・収集運用の詳細: [`docs/DATABASE.md`](docs/DATABASE.md)
- リポジトリ全体像・使い方: [`README.md`](README.md)
- 検証ハーネス: [`analysis/verify_ev.py`](analysis/verify_ev.py) / DB消費例: [`backtest_example.py`](backtest_example.py)
