# 期待値 > 1.0 を狙うための計算手法（市場調査に基づく再設計）

本書は、現行 EV 計算が構造的に回収率 100% を超えられない理由を明らかにし、
パリミュチュエル競馬市場で **正の期待値（EV > 1.0）が学術・実運用の双方で実証されてきた手法**
を先行研究から整理し、netkeiba データで再現するための具体的な計算式・パイプライン・検証方法を示す。

> 調査は先行例調査（deep-research）で実施。26 の一次/二次資料から 118 の主張を抽出し、
> 3 票の敵対的検証（2/3 で棄却）を通過した 23 主張に基づく。末尾に出典を明記する。

---

## 0. 結論（先に要点）

1. **現行手法は定義上 EV < 1.0 にしかならない。** 勝率を「単勝オッズの逆数の正規化」で作り、
   その勝率に**同じ市場オッズ**を掛けているため、期待値は控除率の分だけ必ず 1.0 未満に固定される。
   これはバグではなく、**市場オッズ以外の情報がゼロ**であることの数学的帰結。
2. **EV > 1.0 の唯一実証された道は「市場オッズから独立したファンダメンタル勝率推定」**でエッジを作ること。
   原点は Bolton & Chapman (1986) の条件付きロジット、実運用の到達点は Benter (1994) の二段階モデル。
3. ただし **欧米で有効な「大穴除外・人気馬寄せ（favorite-longshot bias 利用）」戦略は日本ではそのまま使えない。**
   日本・香港市場では FLB が弱い/逆転することが実証されている（Busche & Hall 1988; Busche 1994）。
   → JRA では「大穴を切れば勝てる」式の欧米レシピを移植してはならない。独自のキャリブレーションが必須。
4. モデル選択の基準は **精度ではなく確率キャリブレーション**。資金管理は **分数 Kelly + 不確実性収縮**。
   検証は **時系列 split / walk-forward** で、ランダム分割は偽の 100% 超を生む（実例あり）。

---

## 1. なぜ現行手法は 1.0 を超えないのか（構造的証明）

現行の計算（`backtest_example.py:83,100`、`index.html:573-576,675`）：

```
prob_i = (1/odds_i) / Σ_j (1/odds_j)      # 市場オッズの逆数を正規化
EV_i   = prob_i × odds_i                   # 同じ市場オッズを掛ける
```

`odds_i` はブックメーカーではなくパリミュチュエルの確定オッズなので、控除率 t を用いて

```
odds_i ≈ (1 - t) / q_i      （q_i = 市場が付けた真の含意確率、Σ q_i = 1）
```

と書ける。`prob_i` は `1/odds_i ∝ q_i/(1-t)` を正規化したものなので **prob_i = q_i**。したがって

```
EV_i = prob_i × odds_i = q_i × (1 - t)/q_i = (1 - t)  < 1.0
```

**どの馬・どの券種を選んでも期待値は一律 (1 − 控除率)。** JRA の控除率は単勝 20%・馬連 22.5%・
3連単 27.5% 等なので、EV はそれぞれ 0.80 / 0.775 / 0.725 に張り付く。閾値でいくら選別しても、
選別基準自体が市場オッズ由来である限りこの上限は動かない。**現行の検証で EV > 1.0 が出ないのは正しい挙動。**

→ EV > 1.0 に必要なのは「`prob_i` を市場 `q_i` と**別の情報源**から、かつ**より正確に**推定すること」。
それができれば、`prob_i > q_i` となる過小評価馬で `EV_i = prob_i × odds_i > 1` が成立する。

---

## 2. 先行研究のエビデンス（EV > 1.0 は実証済みか）

| 研究 | 市場 | 手法 | 実証された結果 | 強度 |
|---|---|---|---|---|
| **Bolton & Chapman 1986** (Manag. Sci. 32:1040) | 米 | 条件付き/多項ロジット（馬・騎手・レース固有変数のみ、**市場オッズ不使用**）＋大穴除外の側制約＋市場を動かさない少額投票 | 50 レースのホールドアウトで**平均 +3.1%**、26 制約値中 19 が正、範囲 +3.1%〜+38.7% | 高（査読・一次）。ただし小標本 |
| **Chapman 2000**（"Still Searching…"） | 香港 | 20 変数の純ファンダメンタル多項ロジット | ホールドアウトで正の単勝収益。勝率 < 0.04 の極端な大穴を除外すると**期待収益 +20% 超** | 高（一次）。in-sample fit の holdout |
| **Benter 1994** | 香港 | 約120変数の多項ロジット**二段階**（ファンダメンタル＋公衆オッズ結合） | **実運用 5 シーズン中 4 シーズン黒字**（損失年で開始資本の約 20%） | 高（一次）だが自己申告・独立監査不能 |
| **Deza et al. 2015** (arXiv:1503.06535) | 加 | 多項ロジット＋Harville/Lo-Bacon-Shone 順位モデル＋分数 Kelly(f=0.5) | 控除率 24.7% のトラックで 350 レース、**総リターン +7.8%**（制約なし版）。市場オッズ結合で公衆に対し ΔR²=0.0036 のエッジ | 中（バックテスト、CI/有意検定なし） |
| **Walsh & Joshi 2024** (Mach. Learn. Appl. 16) | NBA | **キャリブレーション**を最適化基準にしたモデル選択 | キャリブレーション選択で ROI +34.7%、精度選択で −35.2%（同一モデル群） | 高だが競馬外 |
| **Snowberg & Wolfers 2010** (JPE 118) | 米 | FLB の定量化 | 100/1 以上 −61%、ランダム −23%、本命固定 −5.5%。**オッズだけを見るどのセグメントも +EV にならない** | 高（一次） |

**読み取り：** EV > 1.0 は「市場から独立したファンダメンタルモデル」で一貫して達成されており、
逆に「市場オッズを加工しただけ」の戦略（＝現行手法や単純 FLB 利用）はどれも控除率の壁を破れていない。

---

## 3. 提案手法：Benter 型 二段階モデル

### Stage 1 — 独立ファンダメンタル勝率モデル

市場オッズ・人気を**入力に一切使わず**、馬の基礎能力から強さ `s_i` を推定する。

**選択肢A：条件付きロジット（Conditional / Multinomial Logit）** — 先行研究の標準。
レースを 1 つの選択集合とし、馬 i が勝つ確率を

```
π_i = exp(s_i) / Σ_j exp(s_j) ,   s_i = β·x_i     （x_i = 馬 i の特徴量ベクトル）
```

でモデル化。レース内の頭数差を自然に吸収し、β はレース単位の最尤法（クロスエントロピー最小化）で推定。
McFadden R² でモデルの説明力を測る。

**選択肢B：勾配ブースティング（LightGBM 等）＋ softmax 正規化** — 非線形・交互作用を捕捉。
各馬に「勝ち度スコア」を回帰/ランキング学習させ、レース内で softmax（またはランク → 確率変換）して π_i を得る。
**重要：学習は必ずレース単位のグルーピング（LightGBM の `lambdarank` / `group`）で行い、
π を softmax 正規化してから較正する。** 実務では GBDT が条件付きロジットを上回りやすい。

### Stage 2 — 市場オッズとの結合（Benter の核心）

Stage 1 の π_i を、市場の含意確率 q_i（= 正規化した 1/odds）と**もう一段の条件付きロジット**で結合する：

```
combined_i ∝ π_i^α × q_i^β
⇔  log strength_i = α·log π_i + β·log q_i     をレース内 softmax
```

α, β は**過去データで最尤推定**する（β は市場が持つ情報の重み、α は自モデルの重み）。
市場は極めて効率的なので通常 β > 0 が有意に立つ — つまり「市場を無視する」のではなく
**市場を 1 つの特徴量として取り込みつつ、自モデルの独立情報で上回る**のが Benter の要点。

- **市場に対する増分エッジ**は ΔR²（McFadden）で測る：`R²(π,q) − R²(q のみ)`。
  Deza らの実測では ΔR² ≈ 0.0036 と**極小**。JRA の高控除率を破るには、これを控除率相当以上に押し上げる
  特徴量エンジニアリングが要る（§7）。
- α ≈ 0（自モデルが市場に何も足せない）なら、その特徴量セットでは EV > 1.0 は不可能と判断して撤退する。

### 最終 EV とベット則

結合確率 `p_i = combined_i` を使い、券種の実オッズ `o` に対して

```
EV = p × o           （単勝。100円基準なら EV100 = p × o × 100、閾値 100 で損益分岐）
賭ける条件:  EV > 1 + margin      （margin は推定誤差・控除変動へのマージン。例 0.05〜0.15）
```

**現行コードとの差分はただ一点** — `backtest_example.py:83` の
`pm = win_probs(horses)`（市場逆数）を **`pm = combined_probs(model.predict(features), market_q)`** に差し替える。
Harville による連勝式（馬連・3連単等）への展開ロジック（`harville()`, `calc_bets()`）はそのまま再利用できる。
つまり**エンジンは流用可能で、置き換えるのは「確率の作り方」だけ**。

---

## 4. 【最重要】日本市場では欧米の FLB レシピを使うな

- Snowberg & Wolfers, Green ら米国研究の「大穴は過剰人気・本命に寄せろ」という補正
  （Prelec 加重 `ln(π) = −[−ln(p)]^a, a=.928` 等）は**米国代表ベッターに較正した値**であり、
  均衡オッズに既に埋め込まれている。この式単体では日本市場のエッジ（EV > 1）は生まれない。
- **Busche & Hall (1988) は香港 2,653 レース、Busche (1994) は香港＋日本 1,738 レースで FLB の不在/逆転を実証。**
  FLB の方向は市場構造で決まり、多結果市場（競馬）でも情報/ノイズ比によって標準 FLB にも逆 FLB にもなる
  （Ottaviani & Sørensen 2010; Risks 2021）。
- **実装方針：** 欧米の補正式を写経しない。**自前データで含意確率 q → 実現勝率の較正曲線を推定**し
  （§6 の isotonic/Platt）、日本市場に固有のバイアス方向・強度をデータから測って補正する。

---

## 5. モデルの最適化基準は「精度」でなく「キャリブレーション」

- Walsh & Joshi (2024)：同じモデル群でも、**確率が実現頻度と一致するよう選ぶ**と ROI が劇的に改善
  （+34.7% vs −35.2%）。的中率（accuracy）で選ぶと賭博では負ける。
- 学習損失は**クロスエントロピー / log-loss**、モデル選択・早期打ち切りは **log-loss + キャリブレーション誤差**で行う。
- 出力確率は必ず**較正**する：
  - **Isotonic regression**（単調・ノンパラ、データ多いとき）または **Platt scaling / temperature scaling**（少データ）。
  - 較正の評価は **reliability diagram** と **ECE（Expected Calibration Error）**、および
    「予測確率帯ごとの実現勝率と平均払戻」のバケット表（現行 backtest の EV バケットを流用可能）。

---

## 6. 資金管理：分数 Kelly ＋ 不確実性収縮

エッジが出ても、賭け方を誤ると破産する。

- **Kelly 基準**（単勝、実オッズ o、推定勝率 p、b = o − 1）：
  ```
  f* = (b·p − (1 − p)) / b = (p·o − 1) / (o − 1) = EV − 1 の符号付き比率
  ```
- **フル Kelly は推定勝率が不確実なとき系統的に賭けすぎ**、分散と破産確率を上げる（arXiv:1701.02814）。
  → **分数 Kelly（f = 0.25〜0.5）** を使う。Deza らも f = 0.5 で運用。
- 推定の不確実性を織り込むなら **`f_bet = f_kelly × 0.5 − (推定分散に比例した収縮)`**、
  かつ **1 点あたり上限**（例：資金の 1〜2%）と **1 レースあたり合計上限**を設ける。
- 複数買い目の同時ベットは**プール影響（自分の投票がオッズを動かす）**を考慮（Isaacs の解）。
  少額なら無視可、まとまった額なら発券後オッズ変動を織り込む。

---

## 7. netkeiba 再現パイプライン

### 7.1 現状データの限界（最初にやること）

現行 DB（`docs/DATABASE.md`）の `entries` は **`win_odds` / `popularity` / `place_odds` / `finish_pos` しか持たない**。
これは全部「市場 or 結果」由来で、**Stage 1 に使えるファンダメンタル特徴量が皆無**。
→ **まず結果ページ/出馬表から独立特徴量を追加スクレイピングする**のが実装の第一関門。

### 7.2 追加すべき特徴量（Stage 1 の x_i）

netkeiba の結果・出馬表・過去走から取得可能で、**発走前に確定している**もののみ使う（リーク厳禁）：

| カテゴリ | 特徴量 | 備考 |
|---|---|---|
| 馬の負担 | 斤量、斤量-前走差 | 出馬表 |
| 馬体 | 馬体重、増減 | 発走前確定 |
| 人・厩 | 騎手（勝率・複勝率）、調教師、乗り替わり | 集計は**過去データのみ**で作る |
| 適性 | コース（芝/ダ・距離）別成績、枠番、頭数 | race テーブルの course/distance/n_horses と結合 |
| 脚質・展開 | 脚質（逃げ/先行/差し/追込）、想定隊列、上がり3F（**過去走**の平均・最速） | 当該レースの上がり3F は結果なので**過去走のみ**特徴量化 |
| 血統 | 父・母父の距離/馬場適性 | ワンホット or 埋め込み |
| 近走 | 前走着順・着差・タイム指数、休養明け週数、斤量/クラス変化 | 過去 race_id を時系列で結合 |
| 調教 | 調教評価・追い切りタイム | 取得できれば |
| 場・馬場 | 天候、馬場状態、開催回・日 | race テーブル拡張 |

**絶対に特徴量にしないもの（リーク源）：** 当該レースの走破タイム・着順・確定オッズ・人気・上がり3F。
これらは §5 のラベル（勝敗）や market q（Stage 2 の別入力）としてのみ扱う。

### 7.3 パイプライン

```
[拡張スクレイピング] entries に斤量/馬体重/騎手/脚質/血統… を追加
        ↓
[特徴量生成] 過去 race_id を時系列結合し、騎手勝率等の集計は "そのレース以前" のみで算出（リーク防止）
        ↓
[Stage 1] LightGBM(lambdarank, group=race_id) or 条件付きロジット → π_i、レース内 softmax
        ↓
[較正] isotonic/temperature scaling で π を実現勝率に合わせる
        ↓
[Stage 2] combined ∝ π^α × q^β（q = 正規化 1/win_odds）、α,β を過去データで最尤
        ↓
[EV] 単勝 EV=p·o、連勝式は既存 harville()/calc_bets() で展開
        ↓
[選別] EV > 1 + margin、かつ較正済みバケットで実測 EV>1 の帯のみ
        ↓
[資金] 分数 Kelly(0.25〜0.5) + 上限、[検証] walk-forward
```

---

## 8. 検証方法論（過剰適合を絶対に避ける）

**実例警告：** ある公開実装は「回収率 100% 超」を達成したが、原因は
①「直近3走」のつもりが**スクレイピング実行時刻基準**で未来走を混入、
②`train_test_split` の**ランダム分割**で同一 race_id からタイムが漏れた、という**データリーク**だった
（出典：Qiita gara_gara）。**ランダム分割の 100% 超は基本的に偽陽性と疑え。**

- **時系列分割が必須。** 学習=過去、検証=未来。**race_id / 日付でグループ化**し、同一レースが
  train と test にまたがらないようにする。
- **Walk-forward（前進検証）：** 例）2020–2023 学習 → 2024 検証、窓をずらして反復。
  ハイパラ調整も各窓の学習期間内で完結させる（検証期間を覗かない）。
- **完全なホールドアウト年**を最後まで触らず、最終判断のみに 1 回使う。
- 評価指標：log-loss / ECE（較正）、**out-of-sample の実測回収率と信頼区間**（ブートストラップ）、
  EV 帯別の実測 EV。**単一トラック・単一シーズンの好成績は CI 付きで疑う**（Deza 論文の留保と同じ）。
- コスト前提を正しく：控除率、端数（10円単位）、**自分の投票によるオッズ低下**を織り込む。

---

## 9. 実装ロードマップ（既存コードへの接続）

| 段階 | 作業 | 実装 | 状態 |
|---|---|---|---|
| 1 | `entries` に斤量・馬体重・騎手・血統ID・上がり等を追加取得（結果ページから、追加リクエスト不要）＋旧DBへ加算収集 | `scraper/parse_result.py`・`db.py`・`ingest.py --refresh-features` | ✅ 実装・テスト済 |
| 2 | 時系列リーク防止の特徴量生成（過去走のみ集計） | `evmodel/features.py` | ✅ |
| 3 | Stage 1 モデル（条件付きロジット。将来 LightGBM 差し替え可） | `evmodel/condlogit.py` | ✅ |
| 4 | isotonic 較正 | `evmodel/condlogit.isotonic_*` | ✅ |
| 5 | Stage 2 結合（π^α·q^β の α,β 最尤）と ΔR² 計測 | `evmodel/model.py` | ✅ |
| 6 | 結合確率での walk-forward バックテスト | `evmodel/backtest.py`・`evmodel/pipeline.py` | ✅ |
| 7 | 分数 Kelly ステーキング + 上限、OOS 回収率と CI | `evmodel/backtest.py`（`kelly_fraction`/`bootstrap_ci`） | ✅ |
| 8 | 別タブ UI（予測JSONを読み EV>1 とKelly配分を表示） | `ev2.html`（🧮 独立モデル法） | ✅ |
| — | 手法の妥当性検証（効率市場で偽陽性なし・非効率市場でEV>1） | `evmodel/selftest.py` | ✅ 合成データで確認済 |

実行手順・出力の読み方は `evmodel/README.md` を参照。**実データでの本番検証は10年分の収集・
`--refresh-features` 完了後**（データが一部でも揃えば `pipeline` は順次実行可能）。

### スマートフォン完結アーキテクチャ

端末では重い学習を走らせない。**GitHub Actions が学習し、GitHub Pages が配信し、ブラウザは軽い計算だけ**行う：

- **収集**: Actions「レースデータ収集」（スマホの GitHub から起動）→ `data-YYYY` Release にDB蓄積
- **学習**: Actions「モデル学習・π生成」（`.github/workflows/train-model.yml`）→ 年別DB統合→`pipeline`→`serve`→
  `ev2_predictions.json`・`ev2_upcoming.json` を main にコミット→Pages 再配信
- **利用**: `ev2.html`（🧮 独立モデル法）を開くだけ。
  - **📡 ライブ**: 発走前にブラウザが単勝オッズ q だけ取得し、CI が用意した独立勝率 π と
    `p ∝ π^α·q^β`、`EV = p × odds` を計算（π はオッズ非依存なので事前計算できる）
  - **📊 検証**: walk-forward 予測（ROI・CI）を閲覧

これで PC 不要。ブラウザ（GitHub と Pages）だけで収集起動→学習起動→ライブ判定まで完結する。

**判定基準（撤退ライン）：** Stage 2 の α が有意に 0 でなく、walk-forward の OOS で
較正済み EV 帯が安定して控除率 + margin を超える帯を持つこと。満たさなければ、その特徴量セットでは
EV > 1.0 は成立しないと結論し、特徴量を強化して再挑戦する（過剰適合で無理に閾値を下げない）。

---

## 10. 出典（検証通過分）

- **Bolton & Chapman (1986)** "Searching for Positive Returns at the Track", Management Science 32(8):1040-1060.
  https://gwern.net/doc/statistics/decision/1986-bolton.pdf
- **Chapman (2000)** "Still Searching for Positive Returns…", in *Efficiency of Racetrack Betting Markets*, World Scientific.
  https://www.worldscientific.com/doi/10.1142/9789812819192_0018
- **Benter (1994)** "Computer Based Horse Race Handicapping and Wagering Systems".
  https://gwern.net/doc/statistics/decision/1994-benter.pdf ／解説 https://actamachina.com/posts/annotated-benter-paper
- **Deza et al. (2015)** "Wagering in Pari-mutuel Horse Racing…", arXiv:1503.06535. https://arxiv.org/pdf/1503.06535
- **Walsh & Joshi (2024)** "Machine learning for sports betting: calibration vs accuracy", Machine Learning with Applications 16:100539. https://arxiv.org/pdf/2410.21484
- **Snowberg & Wolfers (2010)** "Explaining the Favorite-Longshot Bias", JPE 118(4). https://www.nber.org/system/files/working_papers/w15923/w15923.pdf
- **Busche & Hall (1988)** "An Exception to the Risk Preference Anomaly", J. Business（香港で FLB 不在）／**Busche (1994)**（香港・日本で FLB 不在）。
  Wharton まとめ: https://faculty.wharton.upenn.edu/wp-content/uploads/2012/04/Racetrack-Betting-and-Consensus-of-Subjective-Probabilities.pdf
- **Green, Lee & Rothschild** "The Favorite-Longshot Midas". https://www.stat.berkeley.edu/~aldous/157/Papers/Green.pdf
- **Ottaviani & Sørensen / FLB 方向の理論** Risks 2021, 9(1):22. https://doi.org/10.3390/risks9010022
- **Kelly の不確実性収縮** arXiv:1701.02814. https://arxiv.org/pdf/1701.02814
- **Uhrin et al. (2021)** 適応的分数 Kelly, IMA J. Manag. Math. 32(4). https://arxiv.org/pdf/2107.08827
- **Ziemba (2023)** "Pari-Mutuel Betting Markets Revisited", Annual Review of Financial Economics.
  https://researchonline.lse.ac.uk/id/eprint/120846/1/Ziemba_Pari_mutuel_betting_markets_published.pdf
- **リーク実例（回収率100%超の偽陽性）** https://qiita.com/gara_gara/items/3e099c24280d772e7086

---

## 付録：現行 vs 提案 の一行差分

```python
# 現行（EV は常に 1 - 控除率 < 1.0）
pm = win_probs(horses)                       # market 逆数の正規化 → prob = q
ev = pm[combo] * actual_odds                 # = (1 - t)

# 提案（市場から独立した推定でエッジを作る）
pi  = model.predict(features)                # Stage1: fundamental（市場を使わない）
pi  = calibrate(pi)                          # isotonic / temperature
p   = combine(pi, market_q, alpha, beta)     # Stage2: p ∝ pi^α · q^β
ev  = p[combo] * actual_odds                 # p > q の過小評価馬で EV > 1 が成立しうる
bet = ev > 1 + margin                        # 分数 Kelly で配分
```
