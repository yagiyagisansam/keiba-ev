"""取り込みオーケストレータ CLI。

使い方:
  python -m scraper.ingest --db keiba_2025.db --year 2025 [--month 1]
  python -m scraper.ingest --db test.db --date 20250105
  python -m scraper.ingest --db test.db --race-id 202506010101
  共通: [--workers 4] [--max-minutes 150] [--sleep 0.5]
        [--progress-json progress_2025.json]

並列設計:
- ワーカースレッドはネットワーク取得+パースのみ(fetch_race_data)。
  ワーカー毎に専用 PoliteSession を持ち独立にペーシングする
- SQLite への書き込みはメインスレッドのみ(write_race_data)。
  1レース = 1トランザクション
- BlockGuard を全ワーカーで共有し、連続失敗の閾値超えで全停止(exit 2)

リジューム設計:
- kaisai_days.status / races.status_result / races.status_odds で進捗管理
- 中断しても次回は未完了レースから再開
- --max-minutes 超過で新規投入を止め、実行中のみ回収して正常終了(exit 0)
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from . import config, db
from .enumerate_races import fetch_kaisai_days, fetch_race_list
from .fetch_odds import fetch_odds_for_type, place_odds_map, win_odds_map
from .http_client import BlockGuard, BlockSuspectedError, FetchError, PoliteSession
from .parse_result import ResultNotAvailable, parse_result_page

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_BLOCK_SUSPECTED = 2

JST = timezone(timedelta(hours=9))


def today_jst():
    return datetime.now(JST).strftime("%Y%m%d")


class TimeBudget:
    def __init__(self, max_minutes):
        self.started = time.monotonic()
        self.max_sec = max_minutes * 60 if max_minutes else None

    def exceeded(self):
        return self.max_sec is not None and (time.monotonic() - self.started) > self.max_sec

    def elapsed_min(self):
        return (time.monotonic() - self.started) / 60


# ================================================================
# 列挙(直列。リクエスト数が少ないため並列化しない)
# ================================================================

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


# ================================================================
# レース取得(ワーカースレッド側: ネットワーク+パースのみ、DB 触らない)
# ================================================================

def fetch_race_data(session, row, force_result=False):
    """1レース分を取得・パースして純データを返す(DB 書き込みなし)。

    force_result=True のとき、status_result が DONE でも結果ページを再取得して
    特徴量カラム(斤量・馬体重・騎手・血統ID・上がり等)を埋め直す(旧DBへの追加入力)。

    row: {race_id, kaisai_date, status_result, status_odds, n_horses}
    返り値 dict:
      outcome        : 'done' | 'skipped' | 'pending' | 'error'
      result_data    : (meta, entries, payouts, warnings) | None
      result_status  : 更新すべき status_result | None(変更なし)
      odds_blobs     : {bet_type: (official_dt, odds_dict)}
      odds_missing   : 欠損券種リスト
      odds_status    : 更新すべき status_odds | None
      error_msg      : str | None
    """
    race_id = row["race_id"]
    out = {
        "outcome": "done", "result_data": None, "result_status": None,
        "odds_blobs": {}, "odds_missing": [], "odds_status": None, "error_msg": None,
    }
    n_horses = row["n_horses"]

    # --- 結果・払戻 ---
    if force_result or row["status_result"] != db.STATUS_DONE:
        try:
            html = session.get_text(config.RACE_RESULT_URL.format(race_id=race_id))
            out["result_data"] = parse_result_page(html)
            out["result_status"] = db.STATUS_DONE
            n_horses = out["result_data"][0].get("n_horses")
        except ResultNotAvailable:
            if row["kaisai_date"] >= today_jst():
                # 当日はまだ結果確定前の可能性がある。pending のまま次回に回す
                out["outcome"] = "pending"
            else:
                # 過去日で結果が無い = 中止等。オッズも存在しないのでスキップ確定
                out["outcome"] = "skipped"
            return out
        except FetchError as e:
            out.update(outcome="error", result_status=db.STATUS_ERROR, error_msg=str(e))
            return out

    # --- 全券種オッズ ---
    if row["status_odds"] != db.STATUS_DONE:
        try:
            for bet_type in sorted(config.BET_TYPES):
                odds_dict, official_dt = fetch_odds_for_type(session, race_id, bet_type)
                if not odds_dict:
                    out["odds_missing"].append(bet_type)
                    continue
                out["odds_blobs"][bet_type] = (official_dt, odds_dict)
        except FetchError as e:
            out.update(outcome="error", odds_status=db.STATUS_ERROR, error_msg=str(e))
            return out
        # 単勝すら無いのは異常(結果はあるのにオッズ API が空)
        if 1 in out["odds_missing"]:
            out.update(outcome="error", odds_status=db.STATUS_ERROR,
                       error_msg="単勝オッズが取得できない")
            return out
        unexpected = [t for t in out["odds_missing"]
                      if not (t == 3 and (n_horses or 0) < config.WAKUREN_MIN_HORSES)]
        if unexpected:
            out["error_msg"] = f"想定外のオッズ欠損: {unexpected}"
        out["odds_status"] = db.STATUS_DONE

    return out


# ================================================================
# レース書き込み(メインスレッド側: 1レース = 1トランザクション)
# ================================================================

def write_race_data(conn, row, data):
    """fetch_race_data の結果を DB に反映して outcome を返す。"""
    race_id = row["race_id"]

    if data["outcome"] == "pending":
        return "pending"  # 何も書かず次回に回す
    if data["outcome"] == "skipped":
        db.set_race_status(conn, race_id,
                           status_result=db.STATUS_SKIPPED, status_odds=db.STATUS_SKIPPED)
        conn.commit()
        return "skipped"

    if data["result_data"] is not None:
        meta, entries, payouts, warnings = data["result_data"]
        db.update_race_result(conn, race_id, meta, entries, payouts, db.STATUS_DONE)
        if warnings:
            db.set_race_status(conn, race_id, error_msg="; ".join(warnings)[:500])

    for bet_type, (official_dt, odds_dict) in data["odds_blobs"].items():
        db.upsert_odds_blob(conn, race_id, bet_type, official_dt, odds_dict)
        if bet_type == 1:
            db.update_entry_odds(conn, race_id, win_odds_map(odds_dict), {})
        elif bet_type == 2:
            db.update_entry_odds(conn, race_id, {}, place_odds_map(odds_dict))

    db.set_race_status(
        conn, race_id,
        status_result=data["result_status"],
        status_odds=data["odds_status"],
        odds_types_missing=data["odds_missing"] if data["odds_status"] is not None else None,
        error_msg=data["error_msg"],
    )
    conn.commit()
    return data["outcome"]


def pending_races(conn, where_extra="", params=()):
    """未完了レースの行(worker に渡す情報つき)を返す。"""
    q = (
        "SELECT race_id, kaisai_date, status_result, status_odds, n_horses FROM races "
        "WHERE (status_result IN (0, 2) OR status_odds IN (0, 2)) "
        "AND status_result != 3 " + where_extra + " ORDER BY kaisai_date, race_id"
    )
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def write_progress(conn, path, extra=None):
    summary = db.progress_summary(conn)
    if extra:
        summary.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[progress] {json.dumps(summary, ensure_ascii=False)}")


# ================================================================
# メインループ(並列取得 → 逐次書き込み)
# ================================================================

def refresh_feature_targets(conn, where_extra="", params=()):
    """特徴量カラムが未取得(v1 から移行した/旧収集の)完了レースを返す。

    status_result は DONE だが horse_id が NULL の行 = 結果ページ再取得で
    ファンダメンタル特徴量を埋める対象(旧データベースへの追加入力)。
    """
    q = (
        "SELECT r.race_id, r.kaisai_date, r.status_result, r.status_odds, r.n_horses "
        "FROM races r WHERE r.status_result = 1 " + where_extra + " AND EXISTS ("
        "  SELECT 1 FROM entries e WHERE e.race_id = r.race_id "
        "    AND (e.horse_id IS NULL OR e.corner_pos IS NULL)"
        ") ORDER BY r.kaisai_date, r.race_id"
    )
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def run_targets(conn, targets, *, workers, sleep_sec, guard, budget, counts,
                force_result=False):
    """対象レースをワーカー並列で取得し、完了順に書き込む。"""
    total = len(targets)
    print(f"[ingest] 対象 {total} レース (workers={workers}, "
          f"時間予算: {budget.max_sec / 60 if budget.max_sec else 'なし'}分)")
    if not targets:
        return

    tls = threading.local()

    def init_worker():
        tls.session = PoliteSession(sleep_sec=sleep_sec, guard=guard)

    def task(row):
        return fetch_race_data(tls.session, row, force_result=force_result)

    processed = 0
    block_error = None
    with ThreadPoolExecutor(max_workers=workers, initializer=init_worker) as executor:
        it = iter(targets)
        futures = {}
        # ワーカー数ぶんだけ先行投入(深く積みすぎると時間予算で無駄撃ちになる)
        for _ in range(workers):
            row = next(it, None)
            if row is None:
                break
            futures[executor.submit(task, row)] = row

        while futures:
            done_set, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done_set:
                row = futures.pop(fut)
                try:
                    data = fut.result()
                except BlockSuspectedError as e:
                    block_error = block_error or e
                    continue  # 残りの futures も回収(guard 停止済みなので即失敗する)
                except Exception as e:  # 想定外はエラー記録して続行
                    db.set_race_status(conn, row["race_id"],
                                       status_result=db.STATUS_ERROR, error_msg=repr(e)[:500])
                    conn.commit()
                    counts["error"] += 1
                    processed += 1
                    continue
                status = write_race_data(conn, row, data)
                counts[status] += 1
                processed += 1
                if processed % 20 == 0 or status not in ("done", "pending"):
                    print(f"[ingest] {processed}/{total} {row['race_id']}: {status} "
                          f"(経過{budget.elapsed_min():.1f}分, req={guard.request_count})")
                # 予算内かつブロックなしなら次を投入
                if block_error is None and not budget.exceeded():
                    nrow = next(it, None)
                    if nrow is not None:
                        futures[executor.submit(task, nrow)] = nrow
            if block_error is None and budget.exceeded() and futures:
                print(f"[ingest] 時間予算超過 ({budget.elapsed_min():.1f}分)。"
                      f"実行中の{len(futures)}件を回収して終了")
    if block_error is not None:
        raise block_error


def main(argv=None):
    ap = argparse.ArgumentParser(description="netkeiba レースデータ取り込み")
    ap.add_argument("--db", required=True, help="SQLite ファイルパス (例 keiba_2025.db)")
    ap.add_argument("--year", type=int, help="対象年 (カレンダーから開催日を列挙)")
    ap.add_argument("--month", type=int, help="対象月 (--year と併用)")
    ap.add_argument("--date", help="単日指定 YYYYMMDD (スモークテスト用)")
    ap.add_argument("--race-id", help="単レース指定 (スモークテスト用)")
    ap.add_argument("--workers", type=int, default=4, help="並列ワーカー数 (1=直列)")
    ap.add_argument("--max-minutes", type=float, default=None, help="時間予算(分)。超過で正常終了")
    ap.add_argument("--sleep", type=float, default=None,
                    help=f"ワーカー毎のリクエスト間ウェイト秒 (既定 {config.SLEEP_SEC})")
    ap.add_argument("--progress-json", help="進捗サマリの出力先 JSON")
    ap.add_argument("--refresh-features", action="store_true",
                    help="完了済みレースの結果ページを再取得し、特徴量カラム(斤量・馬体重・"
                         "騎手・血統ID・上がり等)が未取得の行を埋め直す(旧DBへの追加入力)")
    args = ap.parse_args(argv)

    if not (args.year or args.date or args.race_id):
        ap.error("--year / --date / --race-id のいずれかが必要です")

    year = args.year or int((args.date or args.race_id)[:4])
    conn = db.open_db(args.db, year=year)
    guard = BlockGuard()
    session = PoliteSession(sleep_sec=args.sleep, guard=guard)  # 列挙用(メインスレッド)
    budget = TimeBudget(args.max_minutes)
    progress_path = args.progress_json or f"progress_{year}.json"

    exit_code = EXIT_OK
    counts = {"done": 0, "skipped": 0, "error": 0, "pending": 0}
    try:
        # 特徴量バックフィル: 収集済みレースの結果ページを再取得して特徴量を埋める
        if args.refresh_features:
            if args.race_id:
                where, params = "AND r.race_id = ?", (args.race_id,)
            elif args.date:
                where, params = "AND r.kaisai_date = ?", (args.date,)
            elif args.month:
                where = "AND r.kaisai_date LIKE ?"
                params = (f"{args.year:04d}{args.month:02d}%",)
            else:
                where, params = "", ()
            targets = refresh_feature_targets(conn, where, params)
            run_targets(conn, targets, workers=max(1, args.workers), sleep_sec=args.sleep,
                        guard=guard, budget=budget, counts=counts, force_result=True)
            db.set_meta(conn, "last_run_at", db.now_utc())
            conn.commit()
            write_progress(conn, progress_path, extra={"last_run_counts": counts,
                                                       "mode": "refresh-features"})
            conn.close()
            print(f"[ingest] 特徴量バックフィル終了: {counts}")
            return exit_code

        # 1) 開催日・レース列挙
        if args.race_id:
            race_id = args.race_id
            if conn.execute("SELECT 1 FROM races WHERE race_id = ?", (race_id,)).fetchone() is None:
                # 単レース試験: 日付不明のままスタブ登録(kaisai_date は年+0000 で代用)
                from .enumerate_races import _race_from_id
                db.upsert_race_stub(conn, _race_from_id(race_id, race_id[:4] + "0000", None))
                conn.commit()
            targets = pending_races(conn, "AND race_id = ?", (race_id,))
        elif args.date:
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
            enumerate_races_for_days(conn, session)
            if args.month:
                prefix = f"{args.year:04d}{args.month:02d}"
                targets = pending_races(conn, "AND kaisai_date LIKE ?", (prefix + "%",))
            else:
                targets = pending_races(conn)

        # 2) レース処理(並列)
        run_targets(conn, targets, workers=max(1, args.workers), sleep_sec=args.sleep,
                    guard=guard, budget=budget, counts=counts)
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
