"""第26章で使う、サンプル商事の売上と案件のデータベースを作る（架空のデータ）。

    uv run python -m browser_ai_rag.sales_data [出力先 data/sales/sales.db]

数える道具のサーバーは、データベースがなければ起動のときにこれで作る。

乱数の種を固定しているので、何度作っても同じ中身になる。
数え方を間違えやすいところを、わざと入れてある：
  - 売上は「計上日」で数える。受注日と計上日の月がずれる行がある
  - キャンセルされた行（status='キャンセル'）は数えない
  - 返品はマイナスの金額で入っている
  - 金額は税抜（amount）。税額（tax）は別の列
  - 顧客の正式名称は「株式会社〜」などで、ふだんの呼び名と違う
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, short_name TEXT NOT NULL);
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit_price INTEGER NOT NULL);
CREATE TABLE sales(
  id INTEGER PRIMARY KEY,
  ordered_on TEXT NOT NULL,     -- 受注日
  booked_on TEXT NOT NULL,      -- 計上日（売上はこの日で数える）
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  owner TEXT NOT NULL,          -- 担当者
  quantity INTEGER NOT NULL,
  amount INTEGER NOT NULL,      -- 税抜金額（円）。返品はマイナス
  tax INTEGER NOT NULL,         -- 消費税額（円）
  status TEXT NOT NULL          -- 計上 / キャンセル
);
CREATE TABLE deals(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  owner TEXT NOT NULL,
  stage TEXT NOT NULL,          -- 見込み / 提案 / 受注 / 失注
  amount INTEGER NOT NULL,      -- 見込み金額（税抜、円）
  created_on TEXT NOT NULL,
  expected_close TEXT NOT NULL, -- 受注予定日
  closed_on TEXT                -- 受注・失注が決まった日
);
"""

CUSTOMERS = [("株式会社アルファ商会", "アルファ"), ("ベータ工業株式会社", "ベータ"), ("ガンマ物産株式会社", "ガンマ"),
             ("デルタ電機株式会社", "デルタ"), ("イプシロン食品株式会社", "イプシロン"), ("ゼータ建設株式会社", "ゼータ"),
             ("エータ薬品株式会社", "エータ"), ("シータ運輸株式会社", "シータ")]
PRODUCTS = [("業務パッケージA", 480_000), ("保守サービス", 60_000), ("導入支援", 250_000),
            ("クラウド利用料", 30_000), ("研修", 120_000)]
OWNERS = ["江藤", "山田", "佐藤", "鈴木", "田中"]
START, END = date(2025, 10, 1), date(2026, 9, 30)


def build(path: Path) -> None:
    rnd = random.Random(2026)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO customers(name, short_name) VALUES(?,?)", CUSTOMERS)
    db.executemany("INSERT INTO products(name, unit_price) VALUES(?,?)", PRODUCTS)

    days = (END - START).days
    made = 0
    while made < 320:
        ordered = START + timedelta(days=rnd.randrange(days))
        booked = ordered + timedelta(days=rnd.choice([0, 3, 10, 20, 35]))   # 月をまたぐ計上がある
        if booked > END:
            continue                                                          # まだ計上されていない受注は載らない
        made += 1
        pid = rnd.randrange(len(PRODUCTS)) + 1
        qty = rnd.choice([1, 1, 1, 2, 3, 5])
        amount = PRODUCTS[pid - 1][1] * qty
        status = "キャンセル" if rnd.random() < 0.05 else "計上"
        if status == "計上" and rnd.random() < 0.04:
            amount = -amount                                                  # 返品
        db.execute("INSERT INTO sales(ordered_on, booked_on, customer_id, product_id, owner, quantity, amount, tax, status)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (ordered.isoformat(), booked.isoformat(), rnd.randrange(len(CUSTOMERS)) + 1, pid,
                    rnd.choice(OWNERS), qty, amount, amount // 10, status))

    # 9月に計上予定だったが、キャンセルになった大口の注文（数え方の罠をはっきりさせるため、一行だけ決め打ちで入れる）
    db.execute("INSERT INTO sales(ordered_on, booked_on, customer_id, product_id, owner, quantity, amount, tax, status)"
               " VALUES('2026-09-10', '2026-09-25', 1, 1, '山田', 2, 960000, 96000, 'キャンセル')")

    for n in range(1, 81):
        created = START + timedelta(days=rnd.randrange(days - 30))
        expected = created + timedelta(days=rnd.choice([30, 45, 60, 90]))
        stage = rnd.choices(["見込み", "提案", "受注", "失注"], weights=[2, 2, 3, 2])[0]
        closed = None
        if stage in ("受注", "失注"):
            closed = min(created + timedelta(days=rnd.randrange(20, 80)), END).isoformat()
        cid = rnd.randrange(len(CUSTOMERS)) + 1
        db.execute("INSERT INTO deals(title, customer_id, owner, stage, amount, created_on, expected_close, closed_on)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (f"{CUSTOMERS[cid - 1][1]} {rnd.choice(['基幹刷新', '保守更新', '追加導入', '研修', 'クラウド移行'])} {n:02d}",
                    cid, rnd.choice(OWNERS), stage, rnd.choice([300_000, 800_000, 1_200_000, 2_500_000, 4_000_000]),
                    created.isoformat(), expected.isoformat(), closed))
    db.commit()
    db.close()


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sales/sales.db")
    build(out)
    print(f"作りました: {out}")
