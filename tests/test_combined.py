"""まとめたサーバー（第29章）。道具の名前がぶつからず、六本分がそろっていること。"""

import pytest
from mcp import Client

from browser_ai_rag import combined_server as cs
from browser_ai_rag.config import Settings

pytestmark = pytest.mark.anyio


async def test_all_tools_unique():
    async with Client(cs.create_combined_server(Settings())) as c:
        names = [t.name for t in (await c.list_tools()).tools]
    assert len(names) == len(set(names)) == 37


def test_name_collision_is_refused(monkeypatch):
    dup = [(cs.server.TOOLS[0][0], "重複", cs.server.TOOLS[0][2])]
    monkeypatch.setattr(cs.mail_server, "TOOLS", cs.mail_server.TOOLS + dup)
    with pytest.raises(RuntimeError, match="ぶつかっています"):
        cs.create_combined_server(Settings())
