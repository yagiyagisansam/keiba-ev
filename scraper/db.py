"""SQLite スキーマとアクセス層。

年別 DB ファイル (keiba_YYYY.db)。オッズは (race_id, bet_type) 単位の
zlib 圧縮 JSON BLOB、結果・払戻・単勝/複勝オッズは正規化テーブルに格納する。
"""

import json
import sqlite3
import zlib
from datetime import datetime, timezone

SCHEMA_VERSION = "3"

# v2 で entries に追加した、市場オッズに依存しないファンダメンタル特徴量。
# 既存 v1 DB は open_db() が ALTER TABLE で加算マイグレーションする(データは保持)。
ENTRY_FEATURE_COLUMNS = [
    ("horse_id", "TEXT"), ("sex", "TEXT"), ("age", "INTEGER"),
    ("kinryo", "REAL"), ("horse_weight", "INTEGER"), ("weight_diff", "INTEGER"),
    ("jockey", "TEXT"), ("trainer", "TEXT"), ("affiliation", "TEXT"),
    ("finish_time_sec", "REAL"), ("agari3f", "REAL"),
    ("corner_pos", "REAL"),  # 最終コーナー通過位置 0..1(脚質)。過去走のみ特徴量化
]

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS kaisai_days (
  kaisai_date TEXT PRIMARY KEY,
  n_races     INTEGER,
  status      INTEGER NOT NULL DEFAULT 0,
  fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS races (
  race_id       TEXT PRIMARY KEY,
  kaisai_date   TEXT NOT NULL,
  venue_code    TEXT NOT NULL,
  venue_name    TEXT,
  kai           INTEGER,
  day           INTEGER,
  race_no       INTEGER,
  race_name     TEXT,
  course        TEXT,
  distance      INTEGER,
  n_horses      INTEGER,
  status_result INTEGER NOT NULL DEFAULT 0,
  status_odds   INTEGER NOT NULL DEFAULT 0,
  odds_types_missing TEXT,
  check_status  INTEGER,
  check_notes   TEXT,
  error_msg     TEXT,
  fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_races_date    ON races(kaisai_date);
CREATE INDEX IF NOT EXISTS idx_races_pending ON races(status_result, status_odds);

CREATE TABLE IF NOT EXISTS entries (
  race_id        TEXT    NOT NULL REFERENCES races(race_id),
  horse_num      INTEGER NOT NULL,
  waku           INTEGER,
  horse_name     TEXT,
  finish_pos     INTEGER,
  finish_status  TEXT,
  win_odds       REAL,
  place_odds_min REAL,
  place_odds_max REAL,
  popularity     INTEGER,
  horse_id       TEXT,
  sex            TEXT,
  age            INTEGER,
  kinryo         REAL,
  horse_weight   INTEGER,
  weight_diff    INTEGER,
  jockey         TEXT,
  trainer        TEXT,
  affiliation    TEXT,
  finish_time_sec REAL,
  agari3f        REAL,
  corner_pos     REAL,
  PRIMARY KEY (race_id, horse_num)
);

CREATE TABLE IF NOT EXISTS payouts (
  race_id    TEXT    NOT NULL REFERENCES races(race_id),
  bet_type   INTEGER NOT NULL,
  combo      TEXT    NOT NULL,
  payout_yen INTEGER NOT NULL,
  popularity INTEGER,
  PRIMARY KEY (race_id, bet_type, combo)
);

CREATE TABLE IF NOT EXISTS odds (
  race_id           TEXT    NOT NULL REFERENCES races(race_id),
  bet_type          INTEGER NOT NULL,
  official_datetime TEXT,
  n_combos          INTEGER,
  payload           BLOB    NOT NULL,
  PRIMARY KEY (race_id, bet_type)
);
"""

# races.status_result / status_odds / kaisai_days.status の値
STATUS_PENDING = 0
STATUS_DONE = 1
STATUS_ERROR = 2
STATUS_SKIPPED = 3  # レース中止・結果なし等


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_combo(bet_type, nums):
    """組番リストを正規化キーにする。例: (4, [2,1]) -> '01-02'、(6, [7,11]) -> '07-11'。

    非順序系(枠連/馬連/ワイド/3連複)は昇順ソート、
    順序系(馬単/3連単)と単一系(単勝/複勝)は並びを保持する。
    オッズ API 側と払戻ページ側の両方をこの関数に通して突合キーを揃える。
    """
    nums = [int(n) for n in nums]
    if bet_type not in (6, 8) and len(nums) > 1:
        nums = sorted(nums)
    return "-".join(f"{n:02d}" for n in nums)


def combo_from_api_key(bet_type, key):
    """オッズ API の組番キー('0102' 等)を正規化 combo にする。"""
    nums = [int(key[i:i + 2]) for i in range(0, len(key), 2)]
    return canonical_combo(bet_type, nums)


def open_db(path, year=None):
    """DB を開き、必要なら初期化する。schema_version 不一致は即エラー。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
        )
        if year is not None:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('year', ?)", (str(year),)
            )
        conn.commit()
    elif row["value"] != SCHEMA_VERSION:
        migrate_schema(conn, row["value"])
    # horse_id 列が存在してから索引を作る(新規=DDL 済 / 移行=ALTER 済)
    if any(r["name"] == "horse_id" for r in conn.execute("PRAGMA table_info(entries)")):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_horse ON entries(horse_id)")
        conn.commit()
    return conn


def migrate_schema(conn, from_version):
    """旧 DB を破壊せず加算マイグレーションする。

    v1 -> v2: entries にファンダメンタル特徴量カラムを ALTER で追加(既存データ保持)。
    追加カラムは NULL で入るので、ingest --refresh-features で結果ページを
    再取得して埋める(旧データベースへの追加入力)。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
    added = []
    for col, typ in ENTRY_FEATURE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {typ}")
            added.append(col)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    print(f"[migrate] schema {from_version} -> {SCHEMA_VERSION}: "
          f"entries に {len(added)} カラム追加 {added}")


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def upsert_kaisai_day(conn, kaisai_date, n_races=None, status=STATUS_PENDING):
    conn.execute(
        """INSERT INTO kaisai_days (kaisai_date, n_races, status, fetched_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(kaisai_date) DO UPDATE SET
             n_races = COALESCE(excluded.n_races, n_races),
             status = excluded.status,
             fetched_at = excluded.fetched_at""",
        (kaisai_date, n_races, status, now_utc()),
    )


def upsert_race_stub(conn, race):
    """レース列挙時の登録。既存行の取得済みステータスは維持する。"""
    conn.execute(
        """INSERT INTO races (race_id, kaisai_date, venue_code, venue_name,
                              kai, day, race_no, race_name)
           VALUES (:race_id, :kaisai_date, :venue_code, :venue_name,
                   :kai, :day, :race_no, :race_name)
           ON CONFLICT(race_id) DO UPDATE SET
             race_name = COALESCE(excluded.race_name, race_name)""",
        race,
    )


def update_race_result(conn, race_id, meta, entries, payouts, status):
    """結果ページ取り込み結果を反映する(1トランザクション内で呼ぶこと)。"""
    conn.execute(
        """UPDATE races SET race_name = COALESCE(?, race_name),
                            course = ?, distance = ?, n_horses = ?,
                            status_result = ?, error_msg = NULL, fetched_at = ?
           WHERE race_id = ?""",
        (
            meta.get("race_name"), meta.get("course"), meta.get("distance"),
            meta.get("n_horses"), status, now_utc(), race_id,
        ),
    )
    for e in entries:
        conn.execute(
            """INSERT INTO entries (race_id, horse_num, waku, horse_name,
                                    finish_pos, finish_status,
                                    horse_id, sex, age, kinryo, horse_weight, weight_diff,
                                    jockey, trainer, affiliation, finish_time_sec, agari3f,
                                    corner_pos)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(race_id, horse_num) DO UPDATE SET
                 waku = COALESCE(excluded.waku, waku),
                 horse_name = COALESCE(excluded.horse_name, horse_name),
                 finish_pos = excluded.finish_pos,
                 finish_status = excluded.finish_status,
                 horse_id = COALESCE(excluded.horse_id, horse_id),
                 sex = COALESCE(excluded.sex, sex),
                 age = COALESCE(excluded.age, age),
                 kinryo = COALESCE(excluded.kinryo, kinryo),
                 horse_weight = COALESCE(excluded.horse_weight, horse_weight),
                 weight_diff = COALESCE(excluded.weight_diff, weight_diff),
                 jockey = COALESCE(excluded.jockey, jockey),
                 trainer = COALESCE(excluded.trainer, trainer),
                 affiliation = COALESCE(excluded.affiliation, affiliation),
                 finish_time_sec = COALESCE(excluded.finish_time_sec, finish_time_sec),
                 agari3f = COALESCE(excluded.agari3f, agari3f),
                 corner_pos = COALESCE(excluded.corner_pos, corner_pos)""",
            (
                race_id, e["horse_num"], e.get("waku"), e.get("horse_name"),
                e.get("finish_pos"), e.get("finish_status"),
                e.get("horse_id"), e.get("sex"), e.get("age"), e.get("kinryo"),
                e.get("horse_weight"), e.get("weight_diff"), e.get("jockey"),
                e.get("trainer"), e.get("affiliation"), e.get("finish_time_sec"),
                e.get("agari3f"), e.get("corner_pos"),
            ),
        )
    for p in payouts:
        conn.execute(
            """INSERT OR REPLACE INTO payouts
               (race_id, bet_type, combo, payout_yen, popularity)
               VALUES (?, ?, ?, ?, ?)""",
            (race_id, p["bet_type"], p["combo"], p["payout_yen"], p.get("popularity")),
        )


def upsert_odds_blob(conn, race_id, bet_type, official_datetime, odds_dict):
    """odds_dict: {combo: [odds, odds_max|None, popularity|None]} を圧縮保存。"""
    payload = zlib.compress(
        json.dumps(odds_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6
    )
    conn.execute(
        """INSERT OR REPLACE INTO odds
           (race_id, bet_type, official_datetime, n_combos, payload)
           VALUES (?, ?, ?, ?, ?)""",
        (race_id, bet_type, official_datetime, len(odds_dict), payload),
    )


def load_odds_blob(conn, race_id, bet_type):
    """保存済みオッズを {combo: [odds, odds_max|None, popularity|None]} で返す。"""
    row = conn.execute(
        "SELECT payload FROM odds WHERE race_id = ? AND bet_type = ?", (race_id, bet_type)
    ).fetchone()
    if row is None:
        return None
    return json.loads(zlib.decompress(row["payload"]).decode("utf-8"))


def update_entry_odds(conn, race_id, win_odds_map, place_odds_map):
    """type1/2 のオッズを entries に正規化コピーする。

    win_odds_map:   {horse_num: (odds, popularity)}
    place_odds_map: {horse_num: (min, max)}
    """
    for num, (odds, pop) in win_odds_map.items():
        conn.execute(
            """UPDATE entries SET win_odds = ?, popularity = ?
               WHERE race_id = ? AND horse_num = ?""",
            (odds, pop, race_id, num),
        )
    for num, (mn, mx) in place_odds_map.items():
        conn.execute(
            """UPDATE entries SET place_odds_min = ?, place_odds_max = ?
               WHERE race_id = ? AND horse_num = ?""",
            (mn, mx, race_id, num),
        )


def set_race_status(conn, race_id, *, status_result=None, status_odds=None,
                    odds_types_missing=None, error_msg=None):
    sets, params = ["fetched_at = ?"], [now_utc()]
    if status_result is not None:
        sets.append("status_result = ?")
        params.append(status_result)
    if status_odds is not None:
        sets.append("status_odds = ?")
        params.append(status_odds)
    if odds_types_missing is not None:
        sets.append("odds_types_missing = ?")
        params.append(json.dumps(odds_types_missing))
    if error_msg is not None:
        sets.append("error_msg = ?")
        params.append(error_msg)
    params.append(race_id)
    conn.execute(f"UPDATE races SET {', '.join(sets)} WHERE race_id = ?", params)


def progress_summary(conn):
    """進捗サマリを dict で返す。"""
    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             SUM(status_result = 1 AND status_odds = 1) AS done,
             SUM(status_result = 3) AS skipped,
             SUM(status_result = 2 OR status_odds = 2) AS error,
             SUM((status_result = 0 OR status_odds = 0)
                 AND status_result != 3 AND status_result != 2 AND status_odds != 2) AS pending
           FROM races"""
    ).fetchone()
    days = conn.execute(
        "SELECT COUNT(*) AS n, SUM(status = 1) AS done FROM kaisai_days"
    ).fetchone()
    return {
        "races_total": row["total"] or 0,
        "races_done": row["done"] or 0,
        "races_skipped": row["skipped"] or 0,
        "races_error": row["error"] or 0,
        "races_pending": row["pending"] or 0,
        "kaisai_days_total": days["n"] or 0,
        "kaisai_days_enumerated": days["done"] or 0,
        "updated_at": now_utc(),
    }
