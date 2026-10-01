"""サーバーをプロセス内でつないで、ツールが見えて呼べることを確かめる。"""

import pytest
from mcp import Client

from browser_ai_rag.server import mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_tools_are_listed():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"hello", "whoami"} <= names


async def test_hello():
    async with Client(mcp) as client:
        result = await client.call_tool("hello", {"name": "テスト"})
    assert "テストさん" in result.content[0].text


async def test_whoami_reports_client_name():
    async with Client(mcp) as client:
        result = await client.call_tool("whoami", {})
    assert "クライアント:" in result.content[0].text
