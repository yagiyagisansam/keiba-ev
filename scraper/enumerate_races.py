"""開催日・レースの列挙。

- カレンダーページ (race.netkeiba.com/top/calendar.html) から開催日を列挙
- race_list ページから race_id を列挙 (index.html getRaceList の移植)
"""

import re

from . import config


def parse_calendar(html, year, month):
    """カレンダー HTML から対象月の開催日 (YYYYMMDD) を昇順で返す。

    月末月初はカレンダーの端に他月の日が載ることがあるため接頭辞で絞る。
    """
    prefix = f"{year:04d}{month:02d}"
    dates = set(re.findall(r"kaisai_date=(\d{8})", html))
    return sorted(d for d in dates if d.startswith(prefix))


def fetch_kaisai_days(session, year, month):
    url = config.CALENDAR_URL.format(year=year, month=month)
    return parse_calendar(session.get_text(url), year, month)


def parse_race_list(html, kaisai_date):
    """race_list HTML からレース一覧を返す。

    返り値: [{race_id, venue_code, venue_name, kai, day, race_no, race_name}]
    """
    # 対象日付のセクションを抽出(複数日付ページ対策)
    sec_m = re.search(
        rf'data-kaisaidate="{kaisai_date}"[\s\S]+?(?=<div class="RaceListDayWrap"|$)', html
    )
    target = sec_m.group(0) if sec_m else html

    races = []
    seen = set()
    # 会場単位のブロックごとに分割
    for item in target.split('<div class="RaceList_SlideBoxItem">')[1:]:
        for box in item.split('<div class="RaceList_Main_Box">')[1:]:
            close_idx = box.find("<!-- /")
            if close_idx > 0:
                box = box[:close_idx]
            rid_m = re.search(r"(?:race_result&(?:amp;)?race_id|shutuba\.html\?race_id)=(\d{12})", box)
            if not rid_m:
                continue
            race_id = rid_m.group(1)
            if race_id in seen:
                continue
            seen.add(race_id)
            rname_m = re.search(r'<dt class="Race_Name">\s*([^\n\r<]+)', box)
            races.append(_race_from_id(race_id, kaisai_date, rname_m.group(1).strip() if rname_m else None))
    races.sort(key=lambda r: r["race_id"])
    return races


def _race_from_id(race_id, kaisai_date, race_name):
    """12桁 race_id (YYYY+場2+回2+日2+R2) をレコードに展開する。"""
    venue_code = race_id[4:6]
    return {
        "race_id": race_id,
        "kaisai_date": kaisai_date,
        "venue_code": venue_code,
        "venue_name": config.VENUE_CODE.get(venue_code),
        "kai": int(race_id[6:8]),
        "day": int(race_id[8:10]),
        "race_no": int(race_id[10:12]),
        "race_name": race_name,
    }


def fetch_race_list(session, kaisai_date):
    url = config.RACE_LIST_URL.format(date=kaisai_date)
    return parse_race_list(session.get_text(url), kaisai_date)


def parse_race_times(html, kaisai_date):
    """race_list HTML から {race_id: '発走HH:MM'} を返す(late money ポーリング用)。"""
    sec_m = re.search(
        rf'data-kaisaidate="{kaisai_date}"[\s\S]+?(?=<div class="RaceListDayWrap"|$)', html
    )
    target = sec_m.group(0) if sec_m else html
    times = {}
    # Main_Box 単位に分割(切り詰めない: 発走時刻は Race_Data=Item02 にあるため)
    for box in target.split('<div class="RaceList_Main_Box">')[1:]:
        rid_m = re.search(
            r"(?:race_result&(?:amp;)?race_id|shutuba\.html\?race_id)=(\d{12})", box)
        tm = re.search(r'Race_Data">\s*(\d{1,2}:\d{2})', box)
        if rid_m and tm and rid_m.group(1) not in times:
            times[rid_m.group(1)] = tm.group(1)
    return times


def fetch_race_times(session, kaisai_date):
    url = config.RACE_LIST_URL.format(date=kaisai_date)
    return parse_race_times(session.get_text(url), kaisai_date)
