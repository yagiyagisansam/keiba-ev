"""結果ページ (race.sp.netkeiba.com ?pid=race_result) のパーサ。

- All_Result_Table   → 着順 (entries)
- Payout_Detail_Table → 払戻 (payouts)
- Race_Name / Race_Data → レースメタ (レース名・コース・距離・頭数)

同着(複数払戻行)・出走取消/除外/中止・特払/返還に対応する。
"""

import re

from . import config
from .db import canonical_combo

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s):
    return _TAG_RE.sub("", s).strip()


def _int_or_none(s):
    try:
        return int(re.sub(r"[^\d]", "", s)) if re.search(r"\d", s) else None
    except ValueError:
        return None


def _time_to_sec(text):
    """'1:23.6' -> 83.6、'59.8' -> 59.8。パースできなければ None。"""
    text = text.strip()
    m = re.match(r"(?:(\d+):)?(\d+(?:\.\d+)?)$", text)
    if not m:
        return None
    minutes = int(m.group(1)) if m.group(1) else 0
    return round(minutes * 60 + float(m.group(2)), 1)


def parse_entry_features(row):
    """結果テーブルの1行から、市場オッズに依存しないファンダメンタル特徴量を抽出する。

    返り値 dict(すべて best-effort、取れないものは None):
      horse_id, sex, age, kinryo, horse_weight, weight_diff,
      jockey, trainer, affiliation, finish_time_sec, agari3f
    これらは Stage1(独立勝率モデル)の入力になる。当該レースの
    タイム・上がりは「過去走の特徴量」としてのみ使い、当該レースのラベル側では使わない。
    """
    f = {
        "horse_id": None, "sex": None, "age": None, "kinryo": None,
        "horse_weight": None, "weight_diff": None, "jockey": None,
        "trainer": None, "affiliation": None, "finish_time_sec": None, "agari3f": None,
    }

    info_m = re.search(r'<td class="Horse_Info">([\s\S]*?)</td>', row)
    if info_m:
        info = info_m.group(1)
        hid = re.search(r"/horse/(\d+)", info)
        if hid:
            f["horse_id"] = hid.group(1)
        left_m = re.search(r'class="Detail_Left">([\s\S]*?)</span>\s*</span>', info) \
            or re.search(r'class="Detail_Left">([\s\S]*?)</span>', info)
        if left_m:
            left = _strip_tags(left_m.group(1))
            sa = re.search(r"([牡牝セ騸せん])\s*(\d+)", left)
            if sa:
                f["sex"], f["age"] = sa.group(1), int(sa.group(2))
            wt = re.search(r"(\d+)\s*kg\s*\(([-+]?\d+)\)", left)
            if wt:
                f["horse_weight"], f["weight_diff"] = int(wt.group(1)), int(wt.group(2))
            else:  # 新馬など増減なし '468kg' / '計不'
                wt2 = re.search(r"(\d+)\s*kg", left)
                if wt2:
                    f["horse_weight"] = int(wt2.group(1))
        right_m = re.search(r'class="Detail_Right">([\s\S]*?)</span>', info)
        if right_m:
            # '戸崎圭 57.0<br />美浦・金成'
            parts = [_strip_tags(p) for p in re.split(r"<br\s*/?>", right_m.group(1))]
            parts = [p for p in parts if p]
            if parts:
                jm = re.match(r"([^\d]+?)\s*(\d+\.\d+)?$", parts[0])
                if jm:
                    f["jockey"] = jm.group(1).strip() or None
                    if jm.group(2):
                        f["kinryo"] = float(jm.group(2))
            if len(parts) > 1 and "・" in parts[1]:
                aff, tr = parts[1].split("・", 1)
                f["affiliation"], f["trainer"] = aff.strip() or None, tr.strip() or None

    time_m = re.search(r'<td class="Time">([\s\S]*?)</td>', row)
    if time_m:
        tcell = time_m.group(1)
        dt_m = re.search(r"<dt>([\s\S]*?)</dt>", tcell)
        if dt_m:
            f["finish_time_sec"] = _time_to_sec(_strip_tags(dt_m.group(1)))
        ag_m = re.search(r"\((\d+\.\d+)\)", _strip_tags(tcell))
        if ag_m:
            f["agari3f"] = float(ag_m.group(1))
    return f


class ResultNotAvailable(Exception):
    """結果テーブルがまだ無い(未実施・中止など)。"""


def parse_race_meta(html):
    """レース名・コース・距離・頭数を best-effort で抽出する。"""
    meta = {"race_name": None, "course": None, "distance": None, "n_horses": None}
    m = re.search(r'<h1 class="Race_Name[^"]*">([^<]+)</h1>', html)
    if m:
        meta["race_name"] = m.group(1).strip()
    m = re.search(r'<div class="Race_Data">([\s\S]*?)</div>', html)
    if m:
        data = _strip_tags(m.group(1))
        cm = re.search(r"(芝|ダ|障)\s*(\d{3,4})m", data)
        if cm:
            meta["course"] = cm.group(1)
            meta["distance"] = int(cm.group(2))
        hm = re.search(r"(\d+)頭", data)
        if hm:
            meta["n_horses"] = int(hm.group(1))
    return meta


def parse_result_table(html):
    """All_Result_Table から着順リストを返す。

    返り値: [{horse_num, waku, horse_name, finish_pos, finish_status}]
    結果テーブルが無い場合は ResultNotAvailable。
    """
    tm = re.search(r'id="All_Result_Table"[^>]*>([\s\S]*?)</table>', html)
    if not tm:
        raise ResultNotAvailable("All_Result_Table が見つかりません")
    table = tm.group(1)

    entries = []
    for rm in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", table):
        row = rm.group(1)
        if "<th" in row:
            continue
        rank_m = re.search(r'<div class="Rank">([\s\S]*?)</div>', row)
        if not rank_m:
            continue
        rank_text = _strip_tags(rank_m.group(1)).replace("着", "")
        nums = re.findall(r'<td class="Num[^"]*">\s*<div>(\d+)</div>', row)
        if len(nums) < 2:
            continue
        waku, horse_num = int(nums[0]), int(nums[1])

        name_m = re.search(r'class="Horse_Name">[\s\S]*?<a[^>]+title="([^"]+)"', row)
        if not name_m:
            name_m = re.search(r'class="Horse_Name">[\s\S]*?<a[^>]+>([^<]+)<', row)
        horse_name = name_m.group(1).strip() if name_m else None

        if rank_text.isdigit():
            finish_pos, finish_status = int(rank_text), rank_text
        else:
            finish_pos, finish_status = None, rank_text  # 中止/除外/取消/失格 等

        entry = {
            "horse_num": horse_num,
            "waku": waku,
            "horse_name": horse_name,
            "finish_pos": finish_pos,
            "finish_status": finish_status,
        }
        entry.update(parse_entry_features(row))
        entries.append(entry)
    if not entries:
        raise ResultNotAvailable("結果行が抽出できません")
    entries.sort(key=lambda e: e["horse_num"])
    return entries


def parse_payout_table(html):
    """Payout_Detail_Table から払戻リストを返す。

    返り値: ([{bet_type, combo, payout_yen, popularity}], warnings)
    的中数と払戻数が合わない行などは warnings に積み、取り込みは止めない。
    """
    tm = re.search(r'Payout_Detail_Table"?[^>]*>([\s\S]*?)</table>', html)
    if not tm:
        return [], ["Payout_Detail_Table が見つかりません"]
    table = tm.group(1)

    payouts, warnings = [], []
    for rm in re.finditer(r'<tr class="(\w+)">([\s\S]*?)</tr>', table):
        cls, row = rm.group(1), rm.group(2)
        bet_type = config.PAYOUT_ROW_CLASS.get(cls)
        if bet_type is None:
            continue

        res_m = re.search(r'<td class="Result">([\s\S]*?)</td>', row)
        pay_m = re.search(r'<td class="Payout">([\s\S]*?)</td>', row)
        if not res_m or not pay_m:
            warnings.append(f"bet_type={bet_type}: Result/Payout セルなし")
            continue

        combos = _parse_result_cell(bet_type, res_m.group(1))
        amounts = _parse_payout_cell(pay_m.group(1))
        ninki_m = re.search(r'<td class="Ninki">([\s\S]*?)</td>', row)
        ninkis = (
            [_int_or_none(s) for s in re.findall(r"<span>([^<]*)</span>", ninki_m.group(1))]
            if ninki_m else []
        )

        if len(combos) != len(amounts):
            warnings.append(
                f"bet_type={bet_type}: 的中{len(combos)}件と払戻{len(amounts)}件が不一致"
            )
        for i, combo in enumerate(combos):
            amount = amounts[i] if i < len(amounts) else None
            if amount is None:
                # 特払・返還などパースできない行は警告のみ
                warnings.append(f"bet_type={bet_type} combo={combo}: 払戻額が不明")
                continue
            payouts.append({
                "bet_type": bet_type,
                "combo": combo,
                "payout_yen": amount,
                "popularity": ninkis[i] if i < len(ninkis) else None,
            })
    return payouts, warnings


def _parse_result_cell(bet_type, cell):
    """的中組番セルを正規化 combo のリストにする。

    組み合わせ券種は <ul>(1的中=1ブロック)、単勝/複勝は <span> の連なり。
    """
    combos = []
    uls = re.findall(r"<ul>([\s\S]*?)</ul>", cell)
    if uls:
        for ul in uls:
            nums = [int(n) for n in re.findall(r"<span>\s*(\d+)", ul)]
            if nums:
                combos.append(canonical_combo(bet_type, nums))
    else:
        for n in re.findall(r"<span>\s*(\d+)", cell):
            combos.append(canonical_combo(bet_type, [int(n)]))
    return combos


def _parse_payout_cell(cell):
    """払戻額セルを円の整数リストにする。'1,320円<br/>340円' → [1320, 340]。"""
    amounts = []
    for part in re.split(r"<br\s*/?>", cell):
        text = _strip_tags(part)
        if not text:
            continue
        m = re.search(r"([\d,]+)円", text)
        amounts.append(int(m.group(1).replace(",", "")) if m else None)
    return amounts


def parse_result_page(html):
    """結果ページ全体をパースする。

    返り値: (meta, entries, payouts, warnings)
    結果が無いページは ResultNotAvailable を送出。
    """
    entries = parse_result_table(html)  # 結果なしならここで例外
    meta = parse_race_meta(html)
    if meta["n_horses"] is None:
        meta["n_horses"] = len(entries)
    payouts, warnings = parse_payout_table(html)
    return meta, entries, payouts, warnings
