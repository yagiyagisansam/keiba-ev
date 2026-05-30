#!/usr/bin/env python3
"""
StockScan JP - 日本株テクニカル分析スクリプト
GitHub Actions により毎日18:00 JST に実行される
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ─── データ取得 ────────────────────────────────────────────────────

def get_stock_data(code: str) -> pd.DataFrame | None:
    ticker = yf.Ticker(f"{code}.T")
    df = ticker.history(period="1y")
    if df is None or len(df) < 30:
        return None
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df = df[df['close'] > 0].copy()
    return df


def get_stock_info(code: str) -> dict:
    try:
        t = yf.Ticker(f"{code}.T")
        info = t.info
        return {
            'name_en': info.get('shortName', ''),
            'name_ja': info.get('longName', ''),
        }
    except Exception:
        return {}


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['ma5']  = df['close'].rolling(5).mean()
    df['ma25'] = df['close'].rolling(25).mean()
    df['ma75'] = df['close'].rolling(75).mean()
    df['vol_ma25'] = df['volume'].rolling(25).mean()

    # RSI(14)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    # Bollinger Bands (20, 2σ)
    df['bb_mid']   = df['close'].rolling(20).mean()
    df['bb_std']   = df['close'].rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid'].replace(0, np.nan)

    return df


# ─── テクニカル手法 30種 ──────────────────────────────────────────

def chk_bullish_engulfing(df: pd.DataFrame) -> bool:
    """陽の包み足"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    p_bear = p['close'] < p['open']
    c_bull = c['close'] > c['open']
    engulf = c['open'] <= p['close'] and c['close'] >= p['open']
    c_body = c['close'] - c['open']
    p_body = p['open'] - p['close']
    return bool(p_bear and c_bull and engulf and c_body > p_body > 0)


def chk_hammer(df: pd.DataFrame) -> bool:
    """下ひげ陽線（ハンマー）"""
    if len(df) < 2:
        return False
    c = df.iloc[-1]
    body = abs(c['close'] - c['open'])
    lo_shadow = min(c['open'], c['close']) - c['low']
    hi_shadow = c['high'] - max(c['open'], c['close'])
    total = c['high'] - c['low']
    if total <= 0 or body <= 0:
        return False
    return bool(
        c['close'] > c['open'] and
        lo_shadow >= 2 * body and
        hi_shadow <= body * 0.5
    )


def chk_morning_star(df: pd.DataFrame) -> bool:
    """朝の明星"""
    if len(df) < 3:
        return False
    d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    d1_body = abs(d1['close'] - d1['open'])
    d2_body = abs(d2['close'] - d2['open'])
    d3_body = abs(d3['close'] - d3['open'])
    if d1_body <= 0:
        return False
    d1_mid = (d1['open'] + d1['close']) / 2
    return bool(
        d1['close'] < d1['open'] and              # day1: 陰線
        d2_body < d1_body * 0.5 and               # day2: 小さい実体
        d3['close'] > d3['open'] and              # day3: 陽線
        d3['close'] > d1_mid                      # day3: day1中値超え
    )


def chk_three_white_soldiers(df: pd.DataFrame) -> bool:
    """陽の三兵"""
    if len(df) < 3:
        return False
    c = [df.iloc[-3], df.iloc[-2], df.iloc[-1]]
    for i, candle in enumerate(c):
        if candle['close'] <= candle['open']:
            return False
        if i > 0:
            if candle['close'] <= c[i-1]['close']:
                return False
            if not (c[i-1]['open'] <= candle['open'] <= c[i-1]['close']):
                return False
    return True


def chk_gap_up(df: pd.DataFrame) -> bool:
    """窓開け陽線（真空ギャップ）"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return bool(c['open'] > p['high'] and c['close'] > c['open'])


def chk_large_bullish(df: pd.DataFrame) -> bool:
    """大陽線（実体3%以上）"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['close'] <= 0:
        return False
    body = c['close'] - c['open']
    body_pct = body / p['close']
    return bool(body_pct >= 0.03)


def chk_dragonfly_doji(df: pd.DataFrame) -> bool:
    """たくり線（下ひげ十字）"""
    if len(df) < 1:
        return False
    c = df.iloc[-1]
    total = c['high'] - c['low']
    if total <= 0:
        return False
    body = abs(c['close'] - c['open'])
    lo_shadow = min(c['open'], c['close']) - c['low']
    hi_shadow = c['high'] - max(c['open'], c['close'])
    return bool(
        body / total < 0.1 and
        lo_shadow > total * 0.6 and
        hi_shadow < total * 0.2
    )


def chk_piercing(df: pd.DataFrame) -> bool:
    """切り込み線"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    p_body = p['open'] - p['close']
    if p_body <= 0:
        return False
    mid = p['close'] + p_body * 0.5
    return bool(
        p['close'] < p['open'] and           # 前日: 陰線
        c['close'] > c['open'] and           # 当日: 陽線
        c['open'] < p['close'] and           # 当日始値 < 前日終値
        c['close'] > mid and                  # 当日終値 > 前日中値
        c['close'] < p['open']               # 当日終値 < 前日始値（包み足にはならない）
    )


def chk_perfect_order(df: pd.DataFrame) -> bool:
    """パーフェクトオーダー（株価 > 5日 > 25日 > 75日 MA、全MA上向き）"""
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    price_order = c['close'] > c['ma5'] > c['ma25'] > c['ma75']
    ma5_up  = df['ma5'].iloc[-1]  > df['ma5'].iloc[-5]
    ma25_up = df['ma25'].iloc[-1] > df['ma25'].iloc[-5]
    return bool(price_order and ma5_up and ma25_up)


def chk_gc_5_25(df: pd.DataFrame) -> bool:
    """ゴールデンクロス（5日/25日線）"""
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if any(pd.isna([p['ma5'], p['ma25'], c['ma5'], c['ma25']])):
        return False
    return bool(p['ma5'] < p['ma25'] and c['ma5'] >= c['ma25'])


def chk_gc_25_75(df: pd.DataFrame) -> bool:
    """ゴールデンクロス（25日/75日線）"""
    if len(df) < 77:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if any(pd.isna([p['ma25'], p['ma75'], c['ma25'], c['ma75']])):
        return False
    return bool(p['ma25'] < p['ma75'] and c['ma25'] >= c['ma75'])


def chk_ma25_debut(df: pd.DataFrame) -> bool:
    """25日線デビュー買い（下向きから上向きへ転換）"""
    if len(df) < 35:
        return False
    ma25 = df['ma25'].dropna()
    if len(ma25) < 10:
        return False
    was_declining = ma25.iloc[-8] >= ma25.iloc[-5]
    now_rising    = ma25.iloc[-1] > ma25.iloc[-3]
    price_above   = df.iloc[-1]['close'] > df.iloc[-1]['ma25']
    return bool(was_declining and now_rising and price_above)


def chk_ma75_recovery(df: pd.DataFrame) -> bool:
    """75日線回復（下から上へ突破）"""
    if len(df) < 78:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma75']):
        return False
    closes  = df['close'].iloc[-12:-1]
    ma75s   = df['ma75'].iloc[-12:-1]
    was_below = (closes < ma75s).any()
    return bool(was_below and c['close'] > c['ma75'])


def chk_ma_squeeze_breakout(df: pd.DataFrame) -> bool:
    """MA収束後ブレイク（3本線が収束し価格が上抜け）"""
    if len(df) < 85:
        return False
    recent = df.iloc[-12:-1]
    if recent[['ma5','ma75']].isna().any().any():
        return False
    spread = (recent['ma5'] - recent['ma75']).abs() / recent['close']
    c = df.iloc[-1]
    was_tight = (spread < 0.03).all()
    breaking  = c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']
    return bool(was_tight and breaking)


def chk_price_above_all_ma(df: pd.DataFrame) -> bool:
    """株価が全MA上（5・25・75日線の上に位置）"""
    if len(df) < 76:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    return bool(c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75'])


def chk_vol_surge_150(df: pd.DataFrame) -> bool:
    """出来高急増（前日比150%以上 + 上昇）"""
    if len(df) < 2:
        return False
    c, p = df.iloc[-1], df.iloc[-2]
    if p['volume'] <= 0:
        return False
    return bool(c['volume'] >= p['volume'] * 1.5 and c['close'] > p['close'])


def chk_new_high_vol(df: pd.DataFrame) -> bool:
    """新高値 + 出来高急増（年初来高値更新 + 出来高25日平均の150%超）"""
    if len(df) < 100:
        return False
    c = df.iloc[-1]
    year_high = df['close'].iloc[-252:-1].max() if len(df) >= 253 else df['close'].iloc[:-1].max()
    new_high  = c['close'] >= year_high
    vol_ma    = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(new_high and c['volume'] >= vol_ma * 1.5)


def chk_vol_dry_surge(df: pd.DataFrame) -> bool:
    """出来高枯れ→急増（4日縮小後に2倍超）"""
    if len(df) < 7:
        return False
    vols = df['volume'].iloc[-6:].tolist()
    drying = all(vols[i] >= vols[i+1] for i in range(0, 4))
    surge  = vols[-1] >= vols[-2] * 2.0
    return bool(drying and surge)


def chk_vol_above_ma25(df: pd.DataFrame) -> bool:
    """出来高25日平均超え + 上昇"""
    if len(df) < 27:
        return False
    c, p = df.iloc[-1], df.iloc[-2]
    if pd.isna(c['vol_ma25']) or c['vol_ma25'] <= 0:
        return False
    return bool(c['volume'] > c['vol_ma25'] and c['close'] > p['close'])


def chk_vcp(df: pd.DataFrame) -> bool:
    """VCP（ボラティリティ収縮パターン）段階的値幅縮小"""
    if len(df) < 65:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False

    def range_pct(start: int, end: int) -> float:
        seg = df.iloc[start:end]
        if len(seg) == 0 or seg['close'].mean() == 0:
            return 0.0
        return float((seg['high'].max() - seg['low'].min()) / seg['close'].mean())

    r1 = range_pct(-60, -40)
    r2 = range_pct(-40, -20)
    r3 = range_pct(-20, -5)
    contracting = r1 > r2 > r3 > 0
    tight_now   = range_pct(-10, -1) < 0.08
    vol_dec     = df['volume'].iloc[-6:-1].mean() < df['volume'].iloc[-25:-6].mean()
    return bool(contracting and tight_now and vol_dec)


def chk_cup_with_handle(df: pd.DataFrame) -> bool:
    """カップウィズハンドル（U字底 + 小さなハンドル + ブレイク）"""
    if len(df) < 65:
        return False
    cup    = df['close'].iloc[-55:-10]
    handle = df['close'].iloc[-10:]
    c      = df.iloc[-1]

    cup_left_high  = cup.iloc[:5].max()
    cup_bottom     = cup.min()
    cup_right_high = cup.iloc[-5:].max()
    if cup_left_high <= 0 or (cup_left_high - cup_bottom) <= 0:
        return False

    depth    = (cup_left_high - cup_bottom) / cup_left_high
    recovery = (cup_right_high - cup_bottom) / (cup_left_high - cup_bottom)
    rounded  = 0.1 < depth < 0.5 and recovery > 0.8

    handle_pull = (cup_right_high - handle.min()) / cup_right_high if cup_right_high > 0 else 1
    breakout    = c['close'] > cup_right_high
    vol_confirm = c['volume'] > c['vol_ma25'] * 1.3 if not pd.isna(c['vol_ma25']) else False

    return bool(rounded and handle_pull < 0.15 and breakout and vol_confirm)


def chk_tight_area(df: pd.DataFrame) -> bool:
    """タイト保ち合い（上昇トレンド中の超狭レンジ・出来高縮小）"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False
    rec = df.iloc[-8:]
    avg = rec['close'].mean()
    if avg <= 0:
        return False
    tight   = (rec['close'].max() - rec['close'].min()) / avg < 0.05
    vol_dec = rec['volume'].iloc[:4].mean() > rec['volume'].iloc[4:].mean()
    return bool(tight and vol_dec)


def chk_double_bottom(df: pd.DataFrame) -> bool:
    """ダブルボトム（W底ネックライン突破）"""
    if len(df) < 45:
        return False
    prices = df['close'].iloc[-45:]
    lows: list[tuple[int, float]] = []
    for i in range(3, len(prices) - 3):
        if prices.iloc[i] == prices.iloc[i-3:i+4].min():
            lows.append((i, float(prices.iloc[i])))
    if len(lows) < 2:
        return False
    (i1, v1), (i2, v2) = lows[-2], lows[-1]
    similar = abs(v1 - v2) / v1 < 0.05 if v1 > 0 else False
    neckline = float(prices.iloc[i1:i2+1].max())
    between_recovery = neckline > max(v1, v2) * 1.05
    breaking = df.iloc[-1]['close'] > neckline
    return bool(similar and between_recovery and breaking)


def chk_flag(df: pd.DataFrame) -> bool:
    """フラッグ・ペナント（急騰後の保ち合いから上抜け）"""
    if len(df) < 25:
        return False
    pole   = df.iloc[-20:-10]
    flag   = df.iloc[-10:-1]
    c      = df.iloc[-1]
    pole_c = pole['close']
    if pole_c.iloc[0] <= 0:
        return False
    pole_ret   = (pole_c.iloc[-1] - pole_c.iloc[0]) / pole_c.iloc[0]
    flag_avg   = flag['close'].mean()
    flag_range = (flag['close'].max() - flag['close'].min()) / flag_avg if flag_avg > 0 else 1
    breakout   = c['close'] > flag['close'].max()
    vol_surge  = c['volume'] > flag['volume'].mean() * 1.3 if flag['volume'].mean() > 0 else False
    return bool(pole_ret > 0.05 and flag_range < 0.06 and breakout and vol_surge)


def chk_inv_head_shoulders(df: pd.DataFrame) -> bool:
    """逆ヘッド&ショルダー（三点底からネックライン突破）"""
    if len(df) < 60:
        return False
    prices = df['close'].iloc[-60:]
    lows: list[tuple[int, float]] = []
    for i in range(4, len(prices) - 4):
        if prices.iloc[i] == prices.iloc[i-4:i+5].min():
            lows.append((i, float(prices.iloc[i])))
    if len(lows) < 3:
        return False
    ls, hd, rs = lows[-3], lows[-2], lows[-1]
    head_lowest   = hd[1] < ls[1] and hd[1] < rs[1]
    shoulders_sim = abs(ls[1] - rs[1]) / ls[1] < 0.06 if ls[1] > 0 else False
    neckline      = float(prices.iloc[ls[0]:rs[0]+1].max())
    breaking      = df.iloc[-1]['close'] > neckline
    return bool(head_lowest and shoulders_sim and breaking)


def chk_rsi_50_cross(df: pd.DataFrame) -> bool:
    """RSI 50超え回復（50割れから復帰）"""
    if len(df) < 20:
        return False
    rsi = df['rsi'].dropna()
    if len(rsi) < 6:
        return False
    was_below = (rsi.iloc[-6:-1] < 50).any()
    return bool(was_below and rsi.iloc[-1] >= 50)


def chk_macd_gc(df: pd.DataFrame) -> bool:
    """MACDゴールデンクロス（MACDラインがシグナルを上抜け）"""
    if len(df) < 30:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if any(pd.isna([p['macd'], p['macd_signal'], c['macd'], c['macd_signal']])):
        return False
    return bool(p['macd'] < p['macd_signal'] and c['macd'] >= c['macd_signal'])


def chk_bb_squeeze(df: pd.DataFrame) -> bool:
    """BBスクイーズ→拡張（バンド収縮後の急拡大）"""
    if len(df) < 30:
        return False
    bw = df['bb_width'].dropna()
    if len(bw) < 10:
        return False
    avg_bw     = float(bw.iloc[-25:].mean())
    was_squeezed = (bw.iloc[-7:-1] < avg_bw * 0.75).all()
    expanding    = float(bw.iloc[-1]) > float(bw.iloc[-2]) * 1.1
    return bool(was_squeezed and expanding)


def chk_52week_high(df: pd.DataFrame) -> bool:
    """52週新高値（年間最高値を更新）"""
    if len(df) < 100:
        return False
    curr     = df.iloc[-1]['close']
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    return bool(curr >= lookback.max())


def chk_high_level_tight(df: pd.DataFrame) -> bool:
    """高値圏コンソリデーション（年初来高値90%圏内での超タイト保ち合い後ブレイク）"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    lookback = df['close'].iloc[-252:] if len(df) >= 252 else df['close']
    year_high = lookback.max()
    if year_high <= 0:
        return False
    near_high  = c['close'] >= year_high * 0.90
    rec        = df['close'].iloc[-15:]
    avg        = rec.mean()
    tight      = (rec.max() - rec.min()) / avg < 0.05 if avg > 0 else False
    breaking   = c['close'] >= float(rec.iloc[:-1].max())
    return bool(near_high and tight and breaking)


# ─── 手法定義テーブル ─────────────────────────────────────────────

CHECKS: list[tuple[str, str, str]] = [
    # (key, label, func)
    ('bullish_engulfing',    '陽の包み足',                  'chk_bullish_engulfing'),
    ('hammer',               '下ひげ陽線（ハンマー）',       'chk_hammer'),
    ('morning_star',         '朝の明星',                    'chk_morning_star'),
    ('three_white_soldiers', '陽の三兵',                    'chk_three_white_soldiers'),
    ('gap_up',               '窓開け陽線',                  'chk_gap_up'),
    ('large_bullish',        '大陽線（実体3%超）',           'chk_large_bullish'),
    ('dragonfly_doji',       'たくり線',                    'chk_dragonfly_doji'),
    ('piercing',             '切り込み線',                  'chk_piercing'),
    ('perfect_order',        'パーフェクトオーダー',         'chk_perfect_order'),
    ('gc_5_25',              'GC 5/25日線',                 'chk_gc_5_25'),
    ('gc_25_75',             'GC 25/75日線',                'chk_gc_25_75'),
    ('ma25_debut',           '25日線デビュー買い',           'chk_ma25_debut'),
    ('ma75_recovery',        '75日線回復',                  'chk_ma75_recovery'),
    ('ma_squeeze_breakout',  'MA収束後ブレイク',             'chk_ma_squeeze_breakout'),
    ('price_above_all_ma',   '株価が全MA上',                 'chk_price_above_all_ma'),
    ('vol_surge_150',        '出来高急増（前日比150%超）',   'chk_vol_surge_150'),
    ('new_high_vol',         '新高値＋出来高急増',           'chk_new_high_vol'),
    ('vol_dry_surge',        '出来高枯れ→急増',              'chk_vol_dry_surge'),
    ('vol_above_ma25',       '出来高25日平均超え',           'chk_vol_above_ma25'),
    ('vcp',                  'VCP',                         'chk_vcp'),
    ('cup_with_handle',      'カップウィズハンドル',         'chk_cup_with_handle'),
    ('tight_area',           'タイト保ち合い',               'chk_tight_area'),
    ('double_bottom',        'ダブルボトム（W底）',          'chk_double_bottom'),
    ('flag',                 'フラッグ・ペナント',           'chk_flag'),
    ('inv_head_shoulders',   '逆ヘッド&ショルダー',          'chk_inv_head_shoulders'),
    ('rsi_50_cross',         'RSI 50超え回復',               'chk_rsi_50_cross'),
    ('macd_gc',              'MACDゴールデンクロス',         'chk_macd_gc'),
    ('bb_squeeze',           'BBスクイーズ→拡張',            'chk_bb_squeeze'),
    ('52week_high',          '52週新高値',                   'chk_52week_high'),
    ('high_level_tight',     '高値圏コンソリデーション',     'chk_high_level_tight'),
]

_FUNC_MAP = {key: eval(fn) for key, _, fn in CHECKS}


# ─── 単一銘柄分析 ─────────────────────────────────────────────────

def analyze_stock(code: str, name: str = '') -> dict | None:
    df = get_stock_data(code)
    if df is None or len(df) < 30:
        print(f"  ⚠ {code}: データ取得失敗またはデータ不足")
        return None

    df = calc_indicators(df)
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    matches = []
    for key, label, _ in CHECKS:
        try:
            if _FUNC_MAP[key](df):
                matches.append({'key': key, 'label': label})
        except Exception as e:
            pass

    change = (curr['close'] - prev['close']) / prev['close'] * 100 if prev['close'] > 0 else 0.0

    # チャートデータ（直近90日）
    chart = []
    for dt, row in df.tail(90).iterrows():
        chart.append({
            'time':   dt.strftime('%Y-%m-%d'),
            'open':   round(float(row['open']),  2),
            'high':   round(float(row['high']),  2),
            'low':    round(float(row['low']),   2),
            'close':  round(float(row['close']), 2),
            'volume': int(row['volume']),
            'ma5':    round(float(row['ma5']),  2) if not pd.isna(row['ma5'])  else None,
            'ma25':   round(float(row['ma25']), 2) if not pd.isna(row['ma25']) else None,
            'ma75':   round(float(row['ma75']), 2) if not pd.isna(row['ma75']) else None,
        })

    return {
        'code':    code,
        'name':    name,
        'close':   round(float(curr['close']), 2),
        'change':  round(float(change), 2),
        'volume':  int(curr['volume']),
        'matches': matches,
        'chart':   chart,
    }


# ─── メール送信 ───────────────────────────────────────────────────

def send_email(matched: list[dict], date_str: str) -> None:
    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_PASS', '')
    to_email  = os.environ.get('TO_EMAIL', smtp_user)

    if not smtp_user or not smtp_pass:
        print("メール設定なし。スキップ。")
        return

    subject = f"【StockScan JP】テクニカル一致 {date_str}（{len(matched)}銘柄）"

    lines = [
        f"StockScan JP テクニカル分析レポート",
        f"日付: {date_str}",
        f"一致銘柄数: {len(matched)} 件",
        "=" * 50,
    ]
    for s in matched:
        sign = "+" if s['change'] >= 0 else ""
        labels = "、".join(m['label'] for m in s['matches'])
        lines += [
            "",
            f"■ {s['code']}  {s['name']}",
            f"   終値 ¥{s['close']:,.0f}  ({sign}{s['change']:.1f}%)",
            f"   一致手法: {labels}",
        ]
    lines += ["", "─" * 50, "https://yagiyagisansam.github.io/keiba-ev/stocks.html"]

    body = "\n".join(lines)
    msg  = MIMEMultipart()
    msg['From']    = smtp_user
    msg['To']      = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"✉ メール送信完了 → {to_email}")
    except Exception as e:
        print(f"✗ メール送信エラー: {e}")


# ─── メイン ───────────────────────────────────────────────────────

def main() -> None:
    base  = os.path.dirname(os.path.abspath(__file__))
    root  = os.path.join(base, '..')

    stocks_path  = os.path.join(root, 'data', 'stocks.json')
    results_path = os.path.join(root, 'data', 'results.json')

    with open(stocks_path, 'r', encoding='utf-8') as f:
        stocks = json.load(f)

    results: list[dict] = []
    matched: list[dict] = []

    for s in stocks:
        code = str(s['code'])
        name = s.get('name', '')
        print(f"→ {code} {name} を分析中...")
        result = analyze_stock(code, name)
        if result:
            results.append(result)
            if result['matches']:
                matched.append(result)
                print(f"  ✅ 一致: {[m['label'] for m in result['matches']]}")
            else:
                print(f"  　 マッチなし")

    results.sort(key=lambda x: (-len(x['matches']), x['code']))

    now = datetime.now()
    output = {
        'date':      now.strftime('%Y/%m/%d'),
        'timestamp': now.isoformat(),
        'total':     len(results),
        'matched':   len(matched),
        'stocks':    results,
    }

    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n分析完了: {len(matched)}/{len(results)} 銘柄一致")

    if matched:
        send_email(matched, now.strftime('%Y/%m/%d'))


if __name__ == '__main__':
    main()
