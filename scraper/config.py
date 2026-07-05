"""定数定義: URL・会場コード・券種マップ・HTTP設定。

index.html / umatan.html のスクレイパー(netkeiba_to_csv.py 由来)と
同じエンドポイントを使う。
"""

# --- エンドポイント ---
CALENDAR_URL = "https://race.netkeiba.com/top/calendar.html?year={year}&month={month}"
RACE_LIST_URL = "https://race.sp.netkeiba.com/?pid=race_list&kaisai_date={date}"
RACE_RESULT_URL = "https://race.sp.netkeiba.com/?pid=race_result&race_id={race_id}"
ODDS_API_URL = (
    "https://race.netkeiba.com/api/api_get_jra_odds.html"
    "?pid=api_get_jra_odds&input=UTF-8&output=jsonp"
    "&race_id={race_id}&type={bet_type}&action=init&sort=odds&compress=0&callback=jQuery"
)

# --- 会場コード (race_id の 5-6 桁目) ---
VENUE_CODE = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟",
    "05": "東京", "06": "中山", "07": "中京", "08": "京都",
    "09": "阪神", "10": "小倉",
}

# --- 券種 (netkeiba オッズ API の type 番号) ---
BET_TYPES = {
    1: "単勝",
    2: "複勝",
    3: "枠連",
    4: "馬連",
    5: "ワイド",
    6: "馬単",
    7: "3連複",
    8: "3連単",
}
# 組み合わせが着順に依存する券種(組番を並べ替えない)
ORDERED_BET_TYPES = {6, 8}
# min/max のレンジオッズを持つ券種(複勝・ワイド)
RANGE_ODDS_BET_TYPES = {2, 5}
# 枠連は 9 頭以上でしか発売されない
WAKUREN_MIN_HORSES = 9

# 払戻テーブルの行クラス名 → 券種
PAYOUT_ROW_CLASS = {
    "Tansho": 1, "Fukusho": 2, "Wakuren": 3, "Umaren": 4,
    "Wide": 5, "Umatan": 6, "Fuku3": 7, "Tan3": 8,
}

# --- HTTP ---
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
SLEEP_SEC = 0.8          # リクエスト間の基本ウェイト
SLEEP_JITTER = 0.4       # 0〜この秒数のジッターを加算
RETRY_MAX = 3            # リトライ回数(指数バックオフ)
RETRY_BACKOFF_BASE = 2.0  # バックオフ: base**attempt 秒
TIMEOUT_SEC = 30
CONSECUTIVE_FAILURE_LIMIT = 10  # 連続失敗でブロック疑い → 中断
