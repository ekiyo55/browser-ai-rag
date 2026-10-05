"""運用の窓口（第28章）。サーバーを触る部分は偽物に差し替えて確かめる。"""

import pytest
from mcp import Client

from browser_ai_rag import ops_server as ops
from browser_ai_rag.config import Settings
from browser_ai_rag.ops_server import OpsSettings, configure, create_ops_server, redact

pytestmark = pytest.mark.anyio


class FakeBackend:
    def __init__(self):
        self.down = {"mooma-book-count"}
        self.restarted = []

    def unit_state(self, unit):
        if unit in self.down:
            return {"ActiveState": "inactive", "SubState": "dead", "NRestarts": "0", "Result": "success",
                    "InactiveEnterTimestamp": "Mon 2026-10-05 10:29:07 JST"}
        return {"ActiveState": "active", "SubState": "running", "ActiveEnterTimestamp": "Mon 2026-10-05 09:00:00 JST",
                "MemoryCurrent": str(80 * 1024 * 1024), "NRestarts": "0"}

    def probe(self, port, path):
        return (None, 3) if port == 8703 and "mooma-book-count" in self.down else (200, 2)

    def logs(self, unit, minutes):
        return ("09:58 systemd[1]: Stopped mooma-book-count.service - Book sample counting MCP server.\n"
                "10:00 POST /count/mcp 200 3ms from=1.33.51.128\n"
                "10:01 Tool 'sales_summary' failed: token=abcd1234 from 9.129.57.3\n"
                "10:02 Traceback (most recent call last):\n")

    def restart(self, unit):
        self.restarted.append(unit)
        self.down.discard(unit)
        return ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeBackend()
    configure(Settings(data_dir=tmp_path), OpsSettings(admins=["kanri"]), backend=fake)
    ops.state.settle = 0
    who = {"name": "eto"}
    monkeypatch.setattr(ops, "current_user", lambda: who["name"])
    return fake, who


async def call(name, args=None):
    async with Client(create_ops_server(Settings())) as c:
        return await c.call_tool(name, args or {})


async def test_status_finds_the_stopped_one(env):
    ans = (await call("service_status")).structured_content
    assert ans["all_ok"] is False
    count = next(s for s in ans["services"] if s["key"] == "count")
    assert count["running"] is False and count["answering"] is False
    assert count["stopped_at"].endswith("10:29:07 JST") and count["stop_reason"].startswith("正常に止められた")  # 落ちたのではない


async def test_errors_and_restart_are_admin_only(env):
    r = await call("recent_errors", {"service": "count"})
    assert r.is_error and "管理者だけ" in r.content[0].text
    r = await call("restart_service", {"service": "count", "reason": "止まっているため"})
    assert r.is_error and "管理者だけ" in r.content[0].text


async def test_errors_are_redacted(env):
    _, who = env
    who["name"] = "kanri"
    ans = (await call("recent_errors", {"service": "売上・案件"})).structured_content
    text = "\n".join(ans["lines"])
    assert "1.33.51.128" not in text and "9.129.57.3" not in text and "abcd1234" not in text
    assert len(ans["lines"]) == 3 and "Stopped" in ans["lines"][0]  # 停止の記録は拾い、200 の行は拾わない


async def test_restart_once_per_ten_minutes(env):
    fake, who = env
    who["name"] = "kanri"
    ok = (await call("restart_service", {"service": "count", "reason": "止まっているため"})).structured_content
    assert ok["after"]["running"] and fake.restarted == ["mooma-book-count"]
    again = await call("restart_service", {"service": "count", "reason": "念のため"})
    assert again.is_error and "再起動したばかり" in again.content[0].text
    hist = (await call("restart_history")).structured_content["result"]
    assert hist[0]["who"] == "kanri" and hist[0]["reason"] == "止まっているため" and hist[0]["result"] == "ok"


async def test_ops_cannot_restart_itself(env):
    _, who = env
    who["name"] = "kanri"
    r = await call("restart_service", {"service": "ops", "reason": "動作の確認のため"})
    assert r.is_error and "再起動できません" in r.content[0].text


def test_redact():
    assert redact("from=192.168.0.1 Bearer xyz password=secret") == "from=x.x.x.x Bearer *** password=***"
