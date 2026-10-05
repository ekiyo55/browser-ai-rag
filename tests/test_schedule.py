"""予定とタスク（第27章）。時計は 2026-10-05（月）9:00 に止めて確かめる。"""

from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client

from browser_ai_rag import schedule_server as sv
from browser_ai_rag.config import Settings
from browser_ai_rag.schedule_server import ScheduleSettings, configure, create_schedule_server

pytestmark = pytest.mark.anyio
JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=JST).timestamp()


@pytest.fixture
def who(tmp_path, monkeypatch):
    configure(Settings(data_dir=tmp_path), ScheduleSettings(), clock=lambda: NOW)
    w = {"name": "eto"}
    monkeypatch.setattr(sv, "current_user", lambda: w["name"])
    return w


async def call(name, args=None):
    async with Client(create_schedule_server(Settings())) as c:
        return await c.call_tool(name, args or {})


async def test_list_has_weekday_and_now(who):
    ans = (await call("list_events", {"date_from": "2026-10-05", "date_to": "2026-10-09"})).structured_content
    assert ans["now"] == "2026-10-05(月) 09:00"
    assert ans["events"][0]["start"] == "2026-10-05(月) 10:00" and ans["events"][0]["can_edit"] is False


async def test_wrong_weekday_is_rejected(who):
    r = await call("create_event", {"title": "打ち合わせ", "start": "2026-10-12 15:00", "weekday": "火"})
    assert r.is_error and "月曜日で、火曜日ではありません" in r.content[0].text
    ok = (await call("create_event", {"title": "打ち合わせ", "start": "2026-10-13 15:00", "weekday": "火曜日"})).structured_content
    assert ok["event"]["start"] == "2026-10-13(火) 15:00"


async def test_conflict_hides_others_titles(who):
    r = await call("create_event", {"title": "相談", "start": "2026-10-06 14:00", "attendees": ["山田さん"]})
    text = r.content[0].text
    assert r.is_error and "山田: 予定あり" in text and "ベータ工業" not in text      # 中身は見せない
    assert "自分: アルファ商会 打ち合わせ" not in text                               # 13〜14時なので重ならない


async def test_free_time_for_two(who):
    ans = (await call("find_free_time", {"attendees": ["山田"], "date_from": "2026-10-06", "date_to": "2026-10-06",
                                         "duration_minutes": 60})).structured_content
    starts = [s["start"] for s in ans["slots"]]
    assert "2026-10-06(火) 13:00" not in starts and "2026-10-06(火) 14:00" not in starts
    assert starts[0] == "2026-10-06(火) 09:00"


async def test_only_organizer_moves(who):
    lst = (await call("list_events", {"date_from": "2026-10-05", "date_to": "2026-10-05"})).structured_content
    teirei = lst["events"][0]["event_id"]
    r = await call("move_event", {"event_id": teirei, "start": "2026-10-05 16:00"})
    assert r.is_error and "主催者は 山田 さん" in r.content[0].text
    who["name"] = "yamada"
    ok = (await call("move_event", {"event_id": teirei, "start": "2026-10-05 16:00"})).structured_content
    assert ok["event"]["start"] == "2026-10-05(月) 16:00"
    who["name"] = "eto"
    lst = (await call("list_events", {"date_from": "2026-10-05", "date_to": "2026-10-05"})).structured_content
    assert lst["events"][0]["start"] == "2026-10-05(月) 16:00"                       # 参加者の予定表も変わる


async def test_cancel_keeps_record(who):
    made = (await call("create_event", {"title": "仮押さえ", "start": "2026-10-07 17:00"})).structured_content
    await call("cancel_event", {"event_id": made["event"]["event_id"]})
    row = sv.state.db.execute("SELECT cancelled_at FROM events WHERE id=?", (made["event"]["event_id"],)).fetchone()
    assert row[0] is not None


async def test_tasks(who):
    ans = (await call("list_tasks")).structured_content
    assert ans["today"] == "2026-10-05(月)"
    over = [t for t in ans["tasks"] if t["overdue"]]
    assert [t["title"] for t in over] == ["9月の営業報告を出す"]
    t = (await call("add_task", {"title": "請求書の確認", "due": "2026-10-08"})).structured_content
    done = (await call("complete_task", {"task_id": t["task_id"]})).structured_content
    assert done["done"] and done["due"] == "2026-10-08(木)"


async def test_past_time_is_rejected(who):
    r = await call("create_event", {"title": "朝会", "start": "2026-10-05 08:00"})
    assert r.is_error and "過ぎた時刻" in r.content[0].text
