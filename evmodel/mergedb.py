"""複数年の keiba_YYYY.db を1つの DB に統合する(学習用)。

各ソースを db.open_db で開いて v2 にマイグレーションしてから、
races/entries/payouts/odds を INSERT OR IGNORE でコピーする。

  python -m evmodel.mergedb --out keiba_all.db keiba_2016.db keiba_2017.db ...
"""

import argparse
import sqlite3

from scraper import db as dbmod

TABLES = ["races", "entries", "payouts", "odds"]


def merge(sources, dest):
    # まず各ソースを開いて v2 スキーマへ移行(古い v1 DB を安全に扱う)
    for s in sources:
        dbmod.open_db(s).close()
    conn = dbmod.open_db(dest)
    total = {t: 0 for t in TABLES}
    for s in sources:
        conn.execute("ATTACH DATABASE ? AS src", (s,))
        for t in TABLES:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
            collist = ",".join(cols)
            before = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO main.%s (%s) SELECT %s FROM src.%s"
                % (t, collist, collist, t))
            after = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            total[t] += after - before
        conn.commit()
        conn.execute("DETACH DATABASE src")
        print(f"[merge] {s} を統合")
    conn.close()
    print(f"[merge] 完了 → {dest}: " + ", ".join(f"{t}+{n}" for t, n in total.items()))
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description="複数年DBを統合")
    ap.add_argument("--out", required=True)
    ap.add_argument("sources", nargs="+")
    args = ap.parse_args(argv)
    merge(args.sources, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
