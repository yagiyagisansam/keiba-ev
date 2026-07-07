"""late money 収集: 発走前のオッズを相対時刻で時系列スナップショット取得する。

過去レースの途中オッズは取得できないため、これから発走するレースを発走前に
定期ポーリングして odds_snapshots に貯める(前進収集)。1プロセスで当日の全レースを
相対時刻(発走 N 分前)に撃つ。GitHub Actions で開催日に1ジョブとして起動する想定。

  python -m scraper.poll_odds --db keiba_2026.db --date 20260711
  python -m scraper.poll_odds --db test.db --race-id 202606010111 --once   # 即時1発(試験)

設計・背景は docs/RESEARCH_LOG.md / docs/LATE_MONEY.md を参照。
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, db
from .enumerate_races import fetch_race_times
from .fetch_odds import fetch_odds_for_type
from .http_client import BlockGuard, BlockSuspectedError, FetchError, PoliteSession

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

# 発走前 N 分に撃つ(締切直前ほど密に)。締切直前=late money の本命シグナル
DEFAULT_OFFSETS = [60, 30, 15, 10, 5, 3, 1]
POLL_BET_TYPES = [1, 2]  # 単勝・複勝(最も高頻度で更新される)


def post_datetime(date_str, hhmm):
    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=JST)


def build_plan(race_times, date_str, offsets, now):
    """{race_id: 'HH:MM'} → [(target_dt, race_id, post_dt)] を時刻昇順で。過去の撃ち時刻は除外。"""
    plan = []
    for rid, hhmm in race_times.items():
        try:
            post = post_datetime(date_str, hhmm)
        except (ValueError, TypeError):
            continue
        for off in offsets:
            target = post - timedelta(minutes=off)
            if target > now:
                plan.append((target, rid, post))
    plan.sort(key=lambda t: t[0])
    return plan


def snapshot_race(conn, session, rid, post_dt):
    """1レースの現在オッズ(単勝・複勝)をスナップショット保存する。"""
    now = datetime.now(JST)
    mtp = round((post_dt - now).total_seconds() / 60) if post_dt else None
    saved = 0
    for bt in POLL_BET_TYPES:
        try:
            odds, _official = fetch_odds_for_type(session, rid, bt)
        except FetchError:
            continue
        if odds:
            db.insert_odds_snapshot(
                conn, rid, bt, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), mtp, odds)
            saved += 1
    if saved:
        conn.commit()
    return saved


def run(conn, session, plan, budget_end):
    """plan を時刻順に消化。各撃ち時刻まで sleep してスナップショット取得。"""
    done = 0
    for target, rid, post in plan:
        now = datetime.now(JST)
        if now >= budget_end or target >= budget_end:
            print(f"[poll] 時間予算到達。残り{len(plan)-done}件をスキップ")
            break
        wait = (target - now).total_seconds()
        if wait > 0:
            time.sleep(wait)
        mtp = round((post - datetime.now(JST)).total_seconds() / 60)
        n = snapshot_race(conn, session, rid, post)
        done += 1
        print(f"[poll] {rid} 発走{mtp:+d}分 スナップショット{n}件 "
              f"({datetime.now(JST):%H:%M:%S})")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description="発走前オッズの時系列スナップショット収集")
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", help="対象開催日 YYYYMMDD(既定=今日JST)")
    ap.add_argument("--race-id", help="単レース(試験用)")
    ap.add_argument("--once", action="store_true", help="タイミング無視で即時1発だけ撮る(試験)")
    ap.add_argument("--offsets", default=",".join(map(str, DEFAULT_OFFSETS)),
                    help="発走前ポーリング分(カンマ区切り)")
    ap.add_argument("--max-minutes", type=float, default=360, help="時間予算(分, 既定6時間)")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args(argv)

    date_str = args.date or datetime.now(JST).strftime("%Y%m%d")
    year = int(date_str[:4])
    conn = db.open_db(args.db, year=year)
    guard = BlockGuard()
    session = PoliteSession(sleep_sec=args.sleep, guard=guard)
    budget_end = datetime.now(JST) + timedelta(minutes=args.max_minutes)

    try:
        if args.once:
            rid = args.race_id
            if not rid:
                times = fetch_race_times(session, date_str)
                rid = sorted(times)[0] if times else None
            if not rid:
                print("対象レースがありません", file=sys.stderr)
                return 1
            n = snapshot_race(conn, session, rid, None)
            print(f"[poll] {rid} 即時スナップショット{n}件")
            return 0

        offsets = [int(x) for x in args.offsets.split(",") if x.strip()]
        times = fetch_race_times(session, date_str)
        if args.race_id:
            times = {k: v for k, v in times.items() if k == args.race_id}
        if not times:
            print(f"[poll] {date_str}: 発走時刻を取得できるレースがありません")
            return 0
        plan = build_plan(times, date_str, offsets, datetime.now(JST))
        print(f"[poll] {date_str}: {len(times)}レース, 撃ち予定 {len(plan)}回 "
              f"(予算{args.max_minutes:.0f}分)")
        done = run(conn, session, plan, budget_end)
        print(f"[poll] 終了: {done}回撮影")
        return 0
    except BlockSuspectedError as e:
        print(f"[poll] 中断(ブロック疑い): {e}", file=sys.stderr)
        return 2
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
