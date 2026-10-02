"""数える道具（第26章）。答えは、SQL を使わずに Python で数えた値と突き合わせる。"""

import sqlite3
from datetime import date

import pytest
from mcp import Client

from browser_ai_rag import count_server as cs
from browser_ai_rag.config import Settings
from browser_ai_rag.count_server import CountSettings, configure, create_count_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def raw(tmp_path):
    configure(Settings(data_dir=tmp_path), CountSettings(), today=lambda: date(2026, 10, 2))
    db = sqlite3.connect(tmp_path / "sales" / "sales.db")
    db.row_factory = sqlite3.Row
    return db


async def call(name, args, mode="tools"):
    async with Client(create_count_server(Settings(), mode)) as c:
        return await c.call_tool(name, args)


async def test_september_sales_follow_the_rules(raw):
    """9月の売上＝計上日が9月・キャンセル除外・返品込み・税抜。"""
    expect = sum(r["amount"] for r in raw.execute("SELECT * FROM sales")
                 if r["booked_on"].startswith("2026-09") and r["status"] == "計上")
    naive = sum(r["amount"] + r["tax"] for r in raw.execute("SELECT * FROM sales") if r["ordered_on"].startswith("2026-09"))
    ans = (await call("sales_summary", {"date_from": "2026-09", "date_to": "2026-09"})).structured_content
    assert ans["total"] == expect and expect != naive          # 決まりを外すと、数字が変わるデータになっている


async def test_group_by_month_adds_up(raw):
    ans = (await call("sales_summary", {"date_from": "2026-04", "date_to": "2026-09", "group_by": "month"})).structured_content
    assert [r["key"] for r in ans["rows"]] == [f"2026-{m:02d}" for m in range(4, 10)]
    assert sum(r["amount"] for r in ans["rows"]) == ans["total"]


async def test_customer_by_short_name_and_ambiguous(raw):
    ok = (await call("sales_summary", {"date_from": "2025-10", "date_to": "2026-09", "customer": "アルファ"})).structured_content
    assert ok["conditions"]["顧客"] == "株式会社アルファ商会"
    bad = await call("sales_summary", {"date_from": "2025-10", "date_to": "2026-09", "customer": "株式会社"})
    assert bad.is_error and "一つに決められません" in bad.content[0].text


async def test_win_rate(raw):
    rows = [r for r in raw.execute("SELECT * FROM deals") if r["closed_on"] and "2026-04-01" <= r["closed_on"] <= "2026-09-30"]
    won = sum(r["stage"] == "受注" for r in rows)
    lost = sum(r["stage"] == "失注" for r in rows)
    ans = (await call("deal_summary", {"date_from": "2026-04", "date_to": "2026-09"})).structured_content
    assert (ans["won"], ans["lost"]) == (won, lost) and ans["win_rate"] == f"{won / (won + lost):.0%}"


async def test_sql_mode_reads_only(raw):
    n = raw.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    ok = (await call("run_sql", {"sql": "SELECT COUNT(*) FROM sales"}, "sql")).structured_content
    assert ok["rows"][0][0] == n
    for bad in ("DELETE FROM sales", "UPDATE sales SET amount = 0", "ATTACH DATABASE 'x.db' AS x",
                "PRAGMA writable_schema = 1", "DROP TABLE sales"):
        r = await call("run_sql", {"sql": bad}, "sql")
        assert r.is_error, bad
    assert raw.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == n


async def test_modes_expose_different_tools():
    async with Client(create_count_server(Settings(), "tools")) as c:
        assert {t.name for t in (await c.list_tools()).tools} == {"sales_summary", "deal_summary", "list_deals", "list_values"}
    async with Client(create_count_server(Settings(), "sql")) as c:
        assert {t.name for t in (await c.list_tools()).tools} == {"run_sql"}


async def test_no_records_is_not_zero(raw):
    """記録のない期間を「0円」と答えさせない（実機で「前年同月比 9,990,000円の増加」と答えた）。"""
    before = (await call("sales_summary", {"date_from": "2025-09", "date_to": "2025-09"})).structured_content
    assert before["total"] == 0 and before["coverage"].startswith("記録なし")
    part = (await call("sales_summary", {"date_from": "2025-09", "date_to": "2025-10"})).structured_content
    assert part["coverage"].startswith("一部だけ")
    full = (await call("sales_summary", {"date_from": "2026-09", "date_to": "2026-09"})).structured_content
    assert full["coverage"] == "期間のすべてに記録あり"
    month = (await call("sales_summary", {"date_from": "2026-10", "date_to": "2026-10"})).structured_content
    assert month["coverage"].startswith("一部だけ")              # 今日（10/2）より先は、まだ起きていない
