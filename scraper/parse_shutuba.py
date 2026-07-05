"""出馬表ページ (race/shutuba.html) のパーサ。

未来レース(結果前)の出走馬リストを取得し、Stage1 の特徴量に必要な発走前確定情報
(馬番・horse_id・性齢・斤量・馬体重・騎手)を抽出する。ライブ予測 (evmodel.serve) 用。
"""

import re

_TAG = re.compile(r"<[^>]+>")


class ShutubaNotAvailable(Exception):
    """出馬表テーブルがまだ無い。"""


def parse_shutuba(html):
    """返り値: [{horse_num, horse_id, horse_name, sex, age, kinryo, horse_weight, weight_diff, jockey}]"""
    tm = re.search(r'<table class="Shutuba_Table[^"]*">([\s\S]*?)</table>', html)
    if not tm:
        raise ShutubaNotAvailable("Shutuba_Table が見つかりません")
    rows = re.findall(r'<tr\s+class="HorseList"[^>]*>([\s\S]*?)</tr>', tm.group(1))
    if not rows:
        raise ShutubaNotAvailable("出走馬行が抽出できません")

    out = []
    for row in rows:
        num_m = re.search(r'id="odds-\d+_(\d+)"', row)
        hid_m = re.search(r"/horse/(\d+)", row)
        name_m = re.search(r'class="Horse HorseLink">[\s\S]*?<a[^>]*>\s*([^<]+?)\s*</a>', row)
        age_m = re.search(r'class="Age">\s*([牡牝セ騸せん])(\d+)', row)
        jk_m = re.search(r'class="Jockey">[\s\S]*?<em>\s*([^<]+?)\s*</em>\s*([\d.]+)?', row)
        wt_m = re.search(r'class="Weight">\s*(\d+)\s*<br\s*/?>\s*<span>\(([-+]?\d+)\)', row)

        horse_num = int(num_m.group(1)) if num_m else None
        out.append({
            "horse_num": horse_num,
            "horse_id": hid_m.group(1) if hid_m else None,
            "horse_name": (name_m.group(1).replace(" ", "").strip() if name_m else None),
            "sex": age_m.group(1) if age_m else None,
            "age": int(age_m.group(2)) if age_m else None,
            "kinryo": float(jk_m.group(2)) if (jk_m and jk_m.group(2)) else None,
            "horse_weight": int(wt_m.group(1)) if wt_m else None,
            "weight_diff": int(wt_m.group(2)) if wt_m else None,
            "jockey": jk_m.group(1).strip() if jk_m else None,
        })
    out.sort(key=lambda e: (e["horse_num"] is None, e["horse_num"] or 0))
    return out
