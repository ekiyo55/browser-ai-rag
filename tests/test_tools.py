"""五つの道具を、プロセス内のクライアントから呼んで確かめる。"""

import json

import pytest
from mcp import Client

from browser_ai_rag.server import configure, mcp

from conftest import FakeEmbedder, make

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(data_dir):
    embedder = FakeEmbedder()
    settings, store = make(data_dir, embedder)
    configure(settings, store, embedder)
    return settings, store


async def test_five_tools_with_annotations(server):
    async with Client(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"search_knowledge", "read_document", "list_documents", "save_note", "delete_note"}
    assert tools["search_knowledge"].annotations.read_only_hint is True
    assert tools["delete_note"].annotations.destructive_hint is True
    assert "description" in tools["search_knowledge"].input_schema["properties"]["query"]


async def test_search_returns_citation(server):
    async with Client(mcp) as c:
        r = await c.call_tool("search_knowledge", {"query": "新幹線 グリーン車", "limit": 3})
    hits = r.structured_content["result"]
    assert hits[0]["title"] == "出張旅費規程"
    assert "第5条" in hits[0]["heading"]
    assert hits[0]["cite"].startswith("出張旅費規程 第5条")


async def test_read_document_by_heading(server):
    async with Client(mcp) as c:
        docs = (await c.call_tool("list_documents", {})).structured_content["result"]
        doc_id = next(d["document_id"] for d in docs if d["title"] == "出張旅費規程")
        r = await c.call_tool("read_document", {"document_id": doc_id, "heading": "第6条"})
    text = r.structured_content["text"]
    assert "宿泊費" in text and "第5条" not in text


async def test_save_search_and_delete_note(server):
    settings, _ = server
    async with Client(mcp) as c:
        saved = (await c.call_tool("save_note", {"title": "来週の訪問", "body": "北関東の物流会社に10月14日に訪問する"})).structured_content
        assert saved["kind"] == "note"
        assert list(settings.notes_dir.glob("*.md"))
        hits = (await c.call_tool("search_knowledge", {"query": "物流会社 訪問 10月14日"})).structured_content["result"]
        assert any(h["document_id"] == saved["document_id"] for h in hits)
        msg = await c.call_tool("delete_note", {"document_id": saved["document_id"]})
        assert "削除しました" in msg.content[0].text
    assert not list(settings.notes_dir.glob("*.md"))


async def test_cannot_delete_company_document(server):
    async with Client(mcp) as c:
        r = await c.call_tool("delete_note", {"document_id": 1})
    assert r.is_error
    assert "社内文書は削除できません" in r.content[0].text


async def test_limit_is_validated(server):
    async with Client(mcp) as c:
        r = await c.call_tool("search_knowledge", {"query": "出張", "limit": 50})
    assert r.is_error
