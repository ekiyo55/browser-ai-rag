"""タイムカードのサーバー（第23〜24章）。時計は差し替えて、決まった時刻で確かめる。"""

from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client

from browser_ai_rag import timecard_server as tc
from browser_ai_rag.config import Settings
from browser_ai_rag.timecard_server import TimecardSettings, configure, create_timecard_server

pytestmark = pytest.mark.anyio
JST = timezone(timedelta(hours=9))


class Clock:
    def __init__(self, s: str) -> None:
        self.t = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, s: str) -> None:
        self.t = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = Clock("2026-10-01 09:00")
    configure(Settings(data_dir=tmp_path), TimecardSettings(admins=["boss"]), clock)
    who = {"name": "yamada"}
    monkeypatch.setattr(tc, "current_user", lambda: who["name"])   # ログインした人の代わり
    return clock, who


async def call(name, args=None):
    async with Client(create_timecard_server(Settings())) as c:
        return await c.call_tool(name, args or {})


async def test_punch_uses_server_clock_and_login(env):
    clock, _ = env
    r = (await call("punch", {"kind": "in"})).structured_content
    assert r["username"] == "yamada" and r["at"] == "2026-10-01 09:00" and r["status"] == "勤務中"
    clock.set("2026-10-01 12:00"); await call("punch", {"kind": "break_start"})
    clock.set("2026-10-01 13:00"); await call("punch", {"kind": "break_end"})
    clock.set("2026-10-01 19:30"); await call("punch", {"kind": "out"})
    s = (await call("monthly_summary", {"month": "2026-10"})).structured_content
    assert s["worked_minutes"] == 9 * 60 + 30 and s["overtime_minutes"] == 90 and s["days_worked"] == 1


async def test_no_time_or_user_argument():
    """打刻の道具には「いつ」「誰の」を渡す口がない。"""
    async with Client(create_timecard_server(Settings())) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools["punch"].input_schema["properties"]) == {"kind", "note"}


async def test_wrong_order_is_rejected(env):
    r = await call("punch", {"kind": "out"})
    assert r.is_error and "勤務外" in r.content[0].text
    await call("punch", {"kind": "in"})
    again = await call("punch", {"kind": "in"})
    assert again.is_error and "勤務中" in again.content[0].text


async def test_others_summary_is_admin_only(env):
    _, who = env
    await call("punch", {"kind": "in"})
    who["name"] = "sato"
    r = await call("monthly_summary", {"username": "yamada"})
    assert r.is_error and "管理者" in r.content[0].text
    who["name"] = "boss"
    ok = (await call("monthly_summary", {"username": "yamada"})).structured_content
    assert ok["username"] == "yamada"


async def test_overnight_and_forgotten_out(env):
    clock, _ = env
    clock.set("2026-10-01 22:00"); await call("punch", {"kind": "in"})
    clock.set("2026-10-02 06:00"); await call("punch", {"kind": "out"})      # 日をまたいだ勤務は出勤した日の分
    clock.set("2026-10-03 09:00"); await call("punch", {"kind": "in"})       # 退勤を打ち忘れた
    clock.set("2026-10-04 09:00")
    assert (await call("my_today")).structured_content["status"] == "勤務外（前回の退勤漏れ）"
    await call("punch", {"kind": "in"})
    rec = (await call("my_records", {"month": "2026-10"})).structured_content["result"]
    assert rec[0]["work_date"] == "2026-10-01" and rec[0]["worked_minutes"] == 480
    assert rec[1]["problem"] == "退勤の打刻がありません"


async def test_correction_flow(env):
    clock, who = env
    first = (await call("punch", {"kind": "in"})).structured_content          # 本当は 8:30 に来ていた
    clock.set("2026-10-01 18:00"); await call("punch", {"kind": "out"})
    future = await call("request_correction", {"kind": "in", "at": "2026-10-02 09:00", "reason": "打ち間違い"})
    assert future.is_error
    req = (await call("request_correction", {"kind": "in", "at": "2026-10-01 08:30", "reason": "打刻を忘れて9時に打った",
                                             "replaces_punch_id": first["punch_id"]})).structured_content
    assert req["status"] == "pending"
    assert (await call("my_records")).structured_content["result"][0]["start"] == "09:00"   # 承認までは変わらない
    self_ok = await call("decide_correction", {"correction_id": req["correction_id"], "approve": True})
    assert self_ok.is_error and "管理者だけ" in self_ok.content[0].text
    who["name"] = "boss"
    done = (await call("decide_correction", {"correction_id": req["correction_id"], "approve": True})).structured_content
    assert done["status"] == "approved" and done["decided_by"] == "boss"
    who["name"] = "yamada"
    rec = (await call("my_records")).structured_content["result"]
    assert rec[0]["start"] == "08:30" and rec[0]["worked_minutes"] == 570
    voided = tc.state.db.execute("SELECT voided_by FROM punches WHERE id=?", (first["punch_id"],)).fetchone()[0]
    assert voided is not None                                                   # 古い打刻は消さずに印をつける


async def test_admin_cannot_approve_own(env):
    _, who = env
    who["name"] = "boss"
    await call("punch", {"kind": "in"})
    req = (await call("request_correction", {"kind": "in", "at": "2026-10-01 08:00", "reason": "打ち忘れ"})).structured_content
    r = await call("decide_correction", {"correction_id": req["correction_id"], "approve": True})
    assert r.is_error and "自分の申請" in r.content[0].text


async def test_minutes_match_what_is_shown(env):
    """15:34:48 出勤・15:37:03 退勤は、表示どおり 15:34〜15:37 の3分と数える（秒で数えると2分になる）。"""
    clock, _ = env
    clock.t += 15 * 3600 + 34 * 60 + 48 - 9 * 3600; await call("punch", {"kind": "in"})
    clock.t += 135; await call("punch", {"kind": "out"})
    rec = (await call("my_records")).structured_content["result"][0]
    assert (rec["start"], rec["end"], rec["worked_minutes"]) == ("15:34", "15:37", 3)


async def test_correction_list_tells_who_is_looking(env):
    """一覧の答えに、見ている人・管理者かどうか・承認できる人を入れる（AI が推測で補わないように。実機で起きた）。"""
    _, who = env
    await call("punch", {"kind": "in"})
    await call("request_correction", {"kind": "in", "at": "2026-10-01 08:00", "reason": "打ち忘れ"})
    mine = (await call("list_corrections")).structured_content
    assert mine["viewer"] == "yamada" and not mine["viewer_is_admin"] and mine["approvers"] == ["boss"]
    who["name"] = "boss"
    boss = (await call("list_corrections")).structured_content
    assert boss["viewer_is_admin"] and len(boss["corrections"]) == 1
