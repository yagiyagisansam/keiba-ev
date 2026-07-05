# レースデータベース仕様

期待値ベースの買い目計算ツールのバックテスト用に、JRA 全レースの
**確定オッズ(全8券種・全組み合わせ)** と **レース結果・払戻** を年別 SQLite に収集する。

- データソース: netkeiba(結果ページ + 確定オッズ API)。index.html のライブ取得と同じエンドポイント
- 保存先: GitHub Release `data-YYYY` の `keiba_YYYY.db.gz`
- 収集: `.github/workflows/collect-data.yml`(cron 自動 + workflow_dispatch 手動)

## 取得方法

```bash
gh release download data-2025 --pattern 'keiba_2025.db.gz'
gunzip keiba_2025.db.gz
python backtest_example.py --db keiba_2025.db --date 20250105
```

進捗は同じ Release の `progress_YYYY.json` で確認できる。

## テーブル

### meta
`schema_version` / `year` / `last_run_at`。schema_version が変わったら再収集かマイグレーションが必要。

### kaisai_days — 開催日(列挙状態の管理)
| 列 | 説明 |
|---|---|
| kaisai_date | 'YYYYMMDD' (PK) |
| n_races | race_list で見つけたレース数 |
| status | 0=未列挙 1=列挙済 2=エラー |

### races — レース(1行=1レース)
| 列 | 説明 |
|---|---|
| race_id | 12桁 `YYYY+場2+回2+日2+R2` (PK)。netkeiba の race_id と同一 |
| kaisai_date / venue_code / venue_name / kai / day / race_no / race_name | 開催情報。venue_code は '01'札幌〜'10'小倉 |
| course / distance / n_horses | '芝'/'ダ'/'障'、距離m、頭数 |
| status_result / status_odds | 0=未取得 1=取得済 2=エラー 3=スキップ(中止等) |
| odds_types_missing | 取得できなかった券種の JSON 配列。8頭以下の `[3]`(枠連なし)は正常 |
| check_status / check_notes | validate.py の結果。1=OK 2=警告(内容は notes) |

### entries — 出走馬(1行=1頭)
| 列 | 説明 |
|---|---|
| race_id, horse_num | PK |
| waku / horse_name | 枠番・馬名 |
| finish_pos | 確定着順。完走していない馬は NULL |
| finish_status | '1','2',… または '中止'/'除外'/'取消'/'失格' |
| win_odds / popularity | 確定単勝オッズ・人気(オッズ API type1 より) |
| place_odds_min / place_odds_max | 確定複勝オッズの下限/上限(type2 より) |

### payouts — 払戻(1行=1的中)
| 列 | 説明 |
|---|---|
| race_id, bet_type, combo | PK。同着は複数行になる |
| payout_yen | 100円あたりの払戻円 |
| popularity | 的中組み合わせの人気順位 |

### odds — 確定オッズ(1行=1レース×1券種)
| 列 | 説明 |
|---|---|
| race_id, bet_type | PK |
| official_datetime | netkeiba API の確定時刻 |
| n_combos | 組み合わせ数 |
| payload | **zlib 圧縮 JSON**: `{"01-02": [オッズ, 上限オッズ|null, 人気|null], ...}` |

payload の展開:

```python
import json, zlib
odds = json.loads(zlib.decompress(payload).decode("utf-8"))
```

上限オッズは複勝(2)・ワイド(5)のみ(確定値のレンジ)。他券種は null。

## 券種コード (bet_type)

| 番号 | 券種 | combo 例 | 順序 |
|---|---|---|---|
| 1 | 単勝 | `07` | - |
| 2 | 複勝 | `07` | - |
| 3 | 枠連 | `04-06`(枠番) | 昇順 |
| 4 | 馬連 | `07-11` | 昇順 |
| 5 | ワイド | `07-11` | 昇順 |
| 6 | 馬単 | `11-07`(1着-2着) | 着順 |
| 7 | 3連複 | `07-11-14` | 昇順 |
| 8 | 3連単 | `07-11-14`(1-2-3着) | 着順 |

combo は常に **2桁ゼロ埋め+`-`連結**。オッズと払戻で同じ正規化
(`scraper/db.py: canonical_combo`)を通しているため、そのまま JOIN できる。

## 収集の運用

- **単日スモーク**: Actions → レースデータ収集 → Run workflow で `date=20250105`
- **月指定**: `year=2025, month=1`
- **年の自動バックフィル**: リポジトリ変数 `COLLECT_YEARS` に対象年をカンマ区切りで設定
  (例 `2026,2025`)。cron が毎日、時間予算内で未収集分を進める(完了済みなら即終了)
- **所要時間**: 並列4ワーカー(既定)で 1レース≈1.8秒 → **1年≈2時間**。
  2年分のパイロットは cron 1回で完了する
- **過去年への遡及**: `COLLECT_YEARS` に年を足す(cron 任せ、1回の実行で最大2年分)か、
  年別に workflow_dispatch する。異なる年の dispatch は並列実行できるが、
  netkeiba への集約レートが上がるため同時2〜3本まで
- **速度調整**: dispatch 入力 `workers`(既定4)と `sleep`(既定0.5秒)で調整可能。
  ブロック兆候(exit 2)が出たら `workers=2, sleep=1.0` などに緩めて再実行
- netkeiba がランナー IP を弾く場合は、Actions secret `PROXY_URL` に
  Cloudflare Worker(`?url=` 透過形式。index.html と同じもの)を設定するとフォールバックする

## 既知の欠損・特殊ケース

- 枠連(type3)は 8頭以下では発売されない → `odds_types_missing=[3]` で正常
- 出走取消・除外馬はオッズ API 上 0.0 になるため odds payload に含まれない。
  entries には finish_status='取消' 等で残る
- レース中止は `status_result=3`(結果・オッズなし)
- 同着は payouts に複数行。3連単で1着同着なら combo が2通り存在する
- 払戻の「特払・返還」はパースできた的中行のみ格納し、警告を races.error_msg / check_notes に残す

## 整合性チェック

```bash
python -m scraper.validate --db keiba_2025.db
```

的中 combo の `payout_yen ≈ 確定オッズ×100`(複勝/ワイドはレンジ内)、
出走頭数=単勝オッズ件数、着順と3連単払戻の一致を全レースで検査し、
`races.check_status` に書き込む。ワークフローが毎回実行する。
