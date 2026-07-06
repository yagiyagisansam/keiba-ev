"""SQLite DB(scraper 収集分)から、時系列リークの無いレース特徴量を組む。

原則:
- Stage1 特徴量に「当該レースの結果由来」(タイム・着順・上がり・確定オッズ・人気)は使わない。
- 過去走由来の集計(馬・騎手・調教師の成績)は、そのレースより前の開催のみで作る。
  → レースを開催日昇順に処理し、統計を「使ってから更新」する(strictly out-of-time)。
- 市場勝率 q は win_odds の逆数正規化(Stage2 の入力としてのみ使用)。

返り値の各レース: {race_id, date, X:[[..]], feat_names:[..], q:[..], odds:[..], win:int|None}
"""

import sqlite3


FEAT_NAMES = [
    "kinryo", "horse_weight", "weight_diff", "age", "is_female",
    "field_size", "post_rel",
    "is_debut", "n_past", "win_rate", "place_rate", "avg_finish_rel",
    "best_agari", "avg_speed", "days_since_last",
    "jockey_win_rate", "trainer_win_rate",
    # 近走リカレンシー(第2ラウンドで追加): 直近の勢い・状態・トレンド
    "recent_finish_rel", "recent_win_rate3", "best_speed", "form_trend",
    # 脚質(Option B): 過去走の平均コーナー通過位置 0=先行/1=追込
    "avg_corner_pos",
]


def _f(v, default=0.0):
    return float(v) if v is not None else default


def _rate(d, key):
    return (d[key] / d["n"]) if d and d["n"] > 0 else None


def compute_feat(pre, hs, js, ts, field, race_date):
    """1頭の Stage1 特徴量 dict を組む(学習・ライブ採点で共通)。

    pre: {kinryo, horse_weight, weight_diff, age, sex, horse_num} の発走前確定情報
    hs/js/ts: そのレース時点までの馬・騎手・調教師の累積成績(無ければ None)
    """
    n_past = hs["n"] if hs else 0
    days_since = _date_diff(hs["last_date"], race_date) if hs and hs.get("last_date") else None
    recent = hs.get("recent") if hs else None  # [(finish_rel, won, speed), ...] 新しい順
    if recent:
        last3 = recent[:3]
        recent_finish_rel = sum(t[0] for t in last3) / len(last3)
        recent_win_rate3 = sum(t[1] for t in last3) / len(last3)
        best_speed = max(t[2] for t in recent if t[2] is not None) if any(
            t[2] is not None for t in recent) else None
        # フォームトレンド: 古い着順相対 − 直近着順相対(正=改善)
        if len(recent) >= 2:
            half = max(1, len(recent) // 2)
            new_avg = sum(t[0] for t in recent[:half]) / half
            old_avg = sum(t[0] for t in recent[half:]) / max(1, len(recent) - half)
            form_trend = old_avg - new_avg
        else:
            form_trend = 0.0
    else:
        recent_finish_rel, recent_win_rate3, best_speed, form_trend = None, None, None, 0.0
    return {
        "kinryo": _f(pre.get("kinryo"), 55.0),
        "horse_weight": _f(pre.get("horse_weight"), 470.0),
        "weight_diff": _f(pre.get("weight_diff"), 0.0),
        "age": _f(pre.get("age"), 4.0),
        "is_female": 1.0 if (pre.get("sex") in ("牝",)) else 0.0,
        "field_size": float(field),
        "post_rel": (pre.get("horse_num") or 1) / max(field, 1),
        "is_debut": 1.0 if n_past == 0 else 0.0,
        "n_past": float(n_past),
        "win_rate": _f(_rate(hs, "win"), 0.08),
        "place_rate": _f(_rate(hs, "place"), 0.25),
        "avg_finish_rel": _f((hs["fin_sum"] / hs["n"]) if hs and hs["n"] else None, 8.0),
        "best_agari": _f(hs.get("best_agari") if hs else None, 36.0),
        "avg_speed": _f(hs.get("speed_sum") / hs["n"] if hs and hs["n"] else None, 16.0),
        "days_since_last": _f(days_since, 60.0),
        "jockey_win_rate": _f(_rate(js, "win"), 0.06),
        "trainer_win_rate": _f(_rate(ts, "win"), 0.06),
        "recent_finish_rel": _f(recent_finish_rel, 0.6),
        "recent_win_rate3": _f(recent_win_rate3, 0.08),
        "best_speed": _f(best_speed, 16.0),
        "form_trend": _f(form_trend, 0.0),
        "avg_corner_pos": _f(
            (hs["corner_sum"] / hs["corner_n"]) if hs and hs.get("corner_n") else None, 0.5),
    }


def load_races(db_path, limit_year=None, return_stats=False):
    """DB から (race meta + entries) を開催日昇順で読み、特徴量付きレース列を返す。

    return_stats=True のとき、全履歴を反映し切った後の累積成績
    (horse_stats, jockey_stats, trainer_stats) も返す(ライブ採点 evmodel.serve 用)。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = (
        "SELECT r.race_id, r.kaisai_date, r.distance, r.n_horses, "
        "       e.horse_num, e.horse_name, e.horse_id, e.finish_pos, e.finish_status, "
        "       e.win_odds, e.kinryo, e.horse_weight, e.weight_diff, e.age, e.sex, "
        "       e.jockey, e.trainer, e.agari3f, e.finish_time_sec, e.corner_pos "
        "FROM races r JOIN entries e ON e.race_id = r.race_id "
        "WHERE r.status_result = 1 "
    )
    params = ()
    if limit_year:
        q += "AND r.kaisai_date LIKE ? "
        params = (f"{limit_year}%",)
    q += "ORDER BY r.kaisai_date, r.race_id, e.horse_num"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    # レース単位にまとめる
    races = {}
    order = []
    for row in rows:
        rid = row["race_id"]
        if rid not in races:
            races[rid] = {"date": row["kaisai_date"], "distance": row["distance"], "rows": []}
            order.append(rid)
        races[rid]["rows"].append(row)

    # 累積統計(その時点までの成績)。使ってから更新する。
    horse_stats = {}   # horse_id -> {"n","win","place","fin_sum","last_date"}
    jockey_stats = {}  # jockey -> {"n","win"}
    trainer_stats = {}

    out = []
    for rid in order:
        rc = races[rid]
        ents = rc["rows"]
        field = len(ents)
        dist = rc["distance"] or 0
        X, q_raw, odds, feats = [], [], [], []
        nums, names = [], []
        win_idx = None
        for idx, e in enumerate(ents):
            if e["finish_pos"] == 1:
                win_idx = idx
            hs = horse_stats.get(e["horse_id"])
            js = jockey_stats.get(e["jockey"])
            ts = trainer_stats.get(e["trainer"])
            pre = {k: e[k] for k in ("kinryo", "horse_weight", "weight_diff",
                                     "age", "sex", "horse_num")}
            feat = compute_feat(pre, hs, js, ts, field, rc["date"])
            feats.append(feat)
            X.append([feat[k] for k in FEAT_NAMES])
            nums.append(e["horse_num"])
            names.append(e["horse_name"])
            o = e["win_odds"]
            odds.append(_f(o, 0.0))
            q_raw.append((1.0 / o) if (o and o > 0) else 0.0)

        # 市場勝率 q(逆数正規化)。オッズ欠損レースは学習に使わない。
        s = sum(q_raw)
        if s <= 0:
            _update_stats(ents, horse_stats, jockey_stats, trainer_stats, rc, dist)
            continue
        qn = [v / s for v in q_raw]
        out.append({
            "race_id": rid, "date": rc["date"], "X": X, "feat_names": FEAT_NAMES,
            "q": qn, "odds": odds, "win": win_idx, "nums": nums, "names": names,
        })
        # レース後に累積統計を更新(このレースは以後のレースの過去走になる)
        _update_stats(ents, horse_stats, jockey_stats, trainer_stats, rc, dist)
    if return_stats:
        return out, (horse_stats, jockey_stats, trainer_stats)
    return out


def _update_stats(ents, horse_stats, jockey_stats, trainer_stats, rc, dist):
    for e in ents:
        pos = e["finish_pos"]
        hid = e["horse_id"]
        if hid is not None:
            hs = horse_stats.setdefault(
                hid, {"n": 0, "win": 0, "place": 0, "fin_sum": 0.0,
                      "last_date": None, "best_agari": None, "speed_sum": 0.0,
                      "recent": [], "corner_sum": 0.0, "corner_n": 0})
            hs["n"] += 1
            if pos == 1:
                hs["win"] += 1
            if pos is not None and pos <= 3:
                hs["place"] += 1
            hs["fin_sum"] += pos if pos is not None else 18
            hs["last_date"] = rc["date"]
            if e["agari3f"] is not None:
                hs["best_agari"] = e["agari3f"] if hs["best_agari"] is None \
                    else min(hs["best_agari"], e["agari3f"])
            speed = (dist / e["finish_time_sec"]) if (e["finish_time_sec"] and dist) else None
            if speed is not None:
                hs["speed_sum"] += speed  # m/s 相当
            if e["corner_pos"] is not None:
                hs["corner_sum"] += e["corner_pos"]
                hs["corner_n"] += 1
            # 近走履歴(新しい順、最大6走): (着順相対, 勝ち, スピード)
            field = len(ents)
            fin_rel = (pos / field) if pos is not None else 1.0
            hs["recent"].insert(0, (fin_rel, 1 if pos == 1 else 0, speed))
            del hs["recent"][6:]
        for stats, key in ((jockey_stats, e["jockey"]), (trainer_stats, e["trainer"])):
            if key is None:
                continue
            d = stats.setdefault(key, {"n": 0, "win": 0})
            d["n"] += 1
            if pos == 1:
                d["win"] += 1


def _date_diff(d1, d2):
    """'YYYYMMDD' 文字列同士の日数差(概算・月30日)。"""
    try:
        y1, m1, day1 = int(d1[:4]), int(d1[4:6]), int(d1[6:8])
        y2, m2, day2 = int(d2[:4]), int(d2[4:6]), int(d2[6:8])
        return (y2 - y1) * 365 + (m2 - m1) * 30 + (day2 - day1)
    except (ValueError, TypeError):
        return None
