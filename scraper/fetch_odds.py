"""確定オッズ API クライアント (netkeiba api_get_jra_odds)。

type 1..8 の全券種を取得し、正規化 combo をキーにした dict に変換する。
index.html fetchOddsRaw / fetchWinOdds の移植。
"""

from . import config
from .db import combo_from_api_key


def parse_odds_payload(data, bet_type):
    """API レスポンス dict → ({combo: [odds, odds_max|None, popularity|None]}, official_datetime)。

    - オッズ文字列のカンマ ("1,666.7") を除去
    - 出走取消などで "0.0"・非数値の馬(組)は除外
    - 複勝/ワイドは [min, max] のレンジを保持
    """
    odds_raw = ((data or {}).get("data") or {}).get("odds") or {}
    type_data = odds_raw.get(str(bet_type)) or {}
    official_dt = ((data or {}).get("data") or {}).get("official_datetime")

    result = {}
    for key, vals in type_data.items():
        odds = _to_float(vals[0] if len(vals) > 0 else None)
        if odds is None or odds < 1.0:
            continue  # 取消・発売なし
        odds_max = None
        if bet_type in config.RANGE_ODDS_BET_TYPES:
            odds_max = _to_float(vals[1] if len(vals) > 1 else None)
        popularity = _to_int(vals[2] if len(vals) > 2 else None)
        result[combo_from_api_key(bet_type, key)] = [odds, odds_max, popularity]
    return result, official_dt


def _to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_odds_for_type(session, race_id, bet_type):
    """1券種の確定オッズを取得する。({combo: [...]}, official_datetime)"""
    url = config.ODDS_API_URL.format(race_id=race_id, bet_type=bet_type)
    data = session.get_jsonp(url)
    return parse_odds_payload(data, bet_type)


def win_odds_map(odds_dict):
    """type1 の odds_dict → {horse_num: (odds, popularity)} (entries 反映用)。"""
    return {
        int(combo): (vals[0], vals[2])
        for combo, vals in odds_dict.items()
    }


def place_odds_map(odds_dict):
    """type2 の odds_dict → {horse_num: (min, max)} (entries 反映用)。"""
    return {
        int(combo): (vals[0], vals[1])
        for combo, vals in odds_dict.items()
    }
