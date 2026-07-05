"""取り込みオーケストレータ CLI。

使い方:
  python -m scraper.ingest --db keiba_2025.db --year 2025 [--month 1]
  python -m scraper.ingest --db test.db --date 20250105
  python -m scraper.ingest --db test.db --race-id 202506010101
  共通: [--max-minutes 150] [--sleep 0.8] [--progress-json progress_2025.json]

リジューム設計:
- kaisai_days.status / races.status_result / races.status_odds で進捗管理
- 1レース = 1トランザクション。中断しても次回は未完了レースから再開
- --max-minutes 超過で正常終了(exit 0)
- 連続失敗でブロック疑いなら exit 2
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def today_jst():
    return datetime.now(JST).strftime("%Y%m%d")

from . import config, db
from .enumerate_races import fetch_kaisai_days, fetch_race_list
from .fetch_odds import fetch_odds_for_type, place_odds_map, win_odds_map
from .http_client import BlockSuspectedError, FetchError, PoliteSession
from .parse_result import ResultNotAvailable, parse_result_page

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_BLOCK_SUSPECTED = 2


class TimeBudget:
    def __init__(self, max_minutes):
        self.started = time.monotonic()
        self.max_sec = max_minutes * 60 if max_minutes else None

    def exceeded(self):
        return self.max_sec is not None and (time.monotonic() - self.started) > self.max_sec

    def elapsed_min(self):
        return (time.monotonic() - self.started) / 60


def enumerate_days(conn, session, year, months):
    """未列挙月のカレンダーを取得して kaisai_days に登録する。"""
    for month in months:
        prefix = f"{year:04d}{month:02d}"
        known = conn.execute(
            "SELECT COUNT(*) AS n FROM kaisai_days WHERE kaisai_date LIKE ? AND status = 1",
            (prefix + "%",),
        ).fetchone()["n"]
        if known > 0:
            continue  # この月は列挙済み
        # 未来月はスキップ(カレンダーに開催がまだ載らない月を毎回叩かない)
        if prefix > today_jst()[:6]:
            continue
        days = fetch_kaisai_days(session, year, month)
        print(f"[calendar] {year}-{month:02d}: {len(days)}開催日")
        for d in days:
            db.upsert_kaisai_day(conn, d, status=db.STATUS_PENDING)
        conn.commit()


def enumerate_races_for_days(conn, session, date_filter=None):
    """開催日ごとに race_list を取得して races に登録する。"""
    q = "SELECT kaisai_date FROM kaisai_days WHERE status = 0"
    params = ()
    if date_filter:
        q += " AND kaisai_date = ?"
        params = (date_filter,)
    for row in conn.execute(q + " ORDER BY kaisai_date", params).fetchall():
        d = row["kaisai_date"]
        # 未来の開催日はまだレース一覧が確定しないためスキップ
        if d > today_jst():
            continue
        try:
            races = fetch_race_list(session, d)
        except BlockSuspectedError:
            raise
        except Exception as e:
            print(f"[race_list] {d}: 取得失敗 {e}", file=sys.stderr)
            db.upsert_kaisai_day(conn, d, status=db.STATUS_ERROR)
            conn.commit()
            continue
        for r in races:
            db.upsert_race_stub(conn, r)
        db.upsert_kaisai_day(conn, d, n_races=len(races), status=db.STATUS_DONE)
        conn.commit()
        print(f"[race_list] {d}: {len(races)}レース登録")


def process_race(conn, session, race_id):
    """1レース分の結果+全券種オッズを取り込む。1トランザクションで commit。"""
    race = conn.execute(
        "SELECT status_result, status_odds, n_horses FROM races WHERE race_id = ?",
        (race_id,),
    ).fetchone()

    # --- 結果・払戻 ---
    if race["status_result"] != db.STATUS_DONE:
        url = config.RACE_RESULT_URL.format(race_id=race_id)
        try:
            html = session.get_text(url)
            meta, entries, payouts, warnings = parse_result_page(html)
        except ResultNotAvailable:
            kaisai_date = conn.execute(
                "SELECT kaisai_date FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()["kaisai_date"]
            if kaisai_date >= today_jst():
                # 当日はまだ結果確定前の可能性がある。pending のまま次回に回す
                return "pending"
            # 過去日で結果が無い = 中止等。オッズも存在しないのでスキップ確定
            db.set_race_status(conn, race_id,
                               status_result=db.STATUS_SKIPPED, status_odds=db.STATUS_SKIPPED)
            conn.commit()
            return "skipped"
        except FetchError as e:
            db.set_race_status(conn, race_id, status_result=db.STATUS_ERROR, error_msg=str(e))
            conn.commit()
            return "error"
        db.update_race_result(conn, race_id, meta, entries, payouts, db.STATUS_DONE)
        if warnings:
            db.set_race_status(conn, race_id, error_msg="; ".join(warnings)[:500])
        conn.commit()  # オッズ取得失敗時の rollback で結果まで失わないよう先に確定

    # --- 全券種オッズ ---
    if race["status_odds"] != db.STATUS_DONE:
        n_horses = conn.execute(
            "SELECT n_horses FROM races WHERE race_id = ?", (race_id,)
        ).fetchone()["n_horses"]
        missing = []
        try:
            for bet_type in sorted(config.BET_TYPES):
                odds_dict, official_dt = fetch_odds_for_type(session, race_id, bet_type)
                if not odds_dict:
                    # 少頭数の枠連欠如は正常。それ以外の欠損も記録して続行
                    missing.append(bet_type)
                    continue
                db.upsert_odds_blob(conn, race_id, bet_type, official_dt, odds_dict)
                if bet_type == 1:
                    db.update_entry_odds(conn, race_id, win_odds_map(odds_dict), {})
                elif bet_type == 2:
                    db.update_entry_odds(conn, race_id, {}, place_odds_map(odds_dict))
        except FetchError as e:
            conn.rollback()
            db.set_race_status(conn, race_id, status_odds=db.STATUS_ERROR, error_msg=str(e))
            conn.commit()
            return "error"
        # 単勝すら無いのは異常(結果はあるのにオッズAPIが空)
        if 1 in missing:
            db.set_race_status(conn, race_id, status_odds=db.STATUS_ERROR,
                               odds_types_missing=missing, error_msg="単勝オッズが取得できない")
            conn.commit()
            return "error"
        unexpected = [t for t in missing
                      if not (t == 3 and (n_horses or 0) < config.WAKUREN_MIN_HORSES)]
        db.set_race_status(conn, race_id, status_odds=db.STATUS_DONE,
                           odds_types_missing=missing,
                           error_msg="想定外のオッズ欠損: " + str(unexpected) if unexpected else None)

    conn.commit()
    return "done"


def pending_races(conn, where_extra="", params=()):
    q = (
        "SELECT race_id FROM races "
        "WHERE (status_result IN (0, 2) OR status_odds IN (0, 2)) "
        "AND status_result != 3 " + where_extra + " ORDER BY kaisai_date, race_id"
    )
    return [r["race_id"] for r in conn.execute(q, params).fetchall()]


def write_progress(conn, path, extra=None):
    summary = db.progress_summary(conn)
    if extra:
        summary.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[progress] {json.dumps(summary, ensure_ascii=False)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="netkeiba レースデータ取り込み")
    ap.add_argument("--db", required=True, help="SQLite ファイルパス (例 keiba_2025.db)")
    ap.add_argument("--year", type=int, help="対象年 (カレンダーから開催日を列挙)")
    ap.add_argument("--month", type=int, help="対象月 (--year と併用)")
    ap.add_argument("--date", help="単日指定 YYYYMMDD (スモークテスト用)")
    ap.add_argument("--race-id", help="単レース指定 (スモークテスト用)")
    ap.add_argument("--max-minutes", type=float, default=None, help="時間予算(分)。超過で正常終了")
    ap.add_argument("--sleep", type=float, default=None, help="リクエスト間ウェイト秒")
    ap.add_argument("--progress-json", help="進捗サマリの出力先 JSON")
    args = ap.parse_args(argv)

    if not (args.year or args.date or args.race_id):
        ap.error("--year / --date / --race-id のいずれかが必要です")

    year = args.year or int((args.date or args.race_id)[:4])
    conn = db.open_db(args.db, year=year)
    session = PoliteSession(sleep_sec=args.sleep)
    budget = TimeBudget(args.max_minutes)
    progress_path = args.progress_json or f"progress_{year}.json"

    exit_code = EXIT_OK
    counts = {"done": 0, "skipped": 0, "error": 0, "pending": 0}
    try:
        # 1) 開催日列挙
        if args.race_id:
            race_id = args.race_id
            d = None
            row = conn.execute("SELECT kaisai_date FROM races WHERE race_id = ?", (race_id,)).fetchone()
            if row is None:
                # 単レース試験: 日付不明のままスタブ登録(kaisai_date は年+00…で代用)
                from .enumerate_races import _race_from_id
                stub = _race_from_id(race_id, race_id[:4] + "0000", None)
                db.upsert_race_stub(conn, stub)
                conn.commit()
            targets = [race_id]
        else:
            if args.date:
                # 既に列挙済みなら status を戻さない(再実行での race_list 再取得を防ぐ)
                conn.execute(
                    "INSERT OR IGNORE INTO kaisai_days (kaisai_date, status) VALUES (?, 0)",
                    (args.date,),
                )
                conn.commit()
                enumerate_races_for_days(conn, session, date_filter=args.date)
                targets = pending_races(conn, "AND kaisai_date = ?", (args.date,))
            else:
                months = [args.month] if args.month else list(range(1, 13))
                enumerate_days(conn, session, args.year, months)
                if args.month:
                    prefix = f"{args.year:04d}{args.month:02d}"
                    enumerate_races_for_days(conn, session)
                    targets = pending_races(conn, "AND kaisai_date LIKE ?", (prefix + "%",))
                else:
                    enumerate_races_for_days(conn, session)
                    targets = pending_races(conn)

        # 2) レース処理ループ
        total = len(targets)
        print(f"[ingest] 対象 {total} レース (時間予算: {args.max_minutes or 'なし'}分)")
        for i, race_id in enumerate(targets, 1):
            if budget.exceeded():
                print(f"[ingest] 時間予算超過 ({budget.elapsed_min():.1f}分)。正常終了して次回再開")
                break
            status = process_race(conn, session, race_id)
            counts[status] += 1
            if i % 10 == 0 or status != "done":
                print(f"[ingest] {i}/{total} {race_id}: {status} "
                      f"(経過{budget.elapsed_min():.1f}分, req={session.request_count})")
    except BlockSuspectedError as e:
        print(f"[ingest] 中断: {e}", file=sys.stderr)
        exit_code = EXIT_BLOCK_SUSPECTED
    finally:
        db.set_meta(conn, "last_run_at", db.now_utc())
        conn.commit()
        write_progress(conn, progress_path, extra={"last_run_counts": counts})
        conn.close()

    print(f"[ingest] 終了: {counts}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
