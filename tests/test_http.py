"""HTTP の入口（Host の許可リスト）を確かめる。トンネル越しに 421 が出る落とし穴の再現。"""

from starlette.testclient import TestClient

import pytest

from browser_ai_rag.config import Settings
from browser_ai_rag.server import build_app

from conftest import FakeEmbedder

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def _tmp_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 既定の data/ を一時フォルダに作らせる


def _post(settings: Settings, host: str) -> int:
    with TestClient(build_app(settings, embedder=FakeEmbedder())) as client:
        return client.post("/mcp", json=INIT, headers={**HEADERS, "Host": host}).status_code


def test_localhost_is_accepted():
    assert _post(Settings(), "127.0.0.1:8000") == 200


def test_unknown_public_host_is_rejected():
    assert _post(Settings(), "example.trycloudflare.com") == 421


def test_configured_public_host_is_accepted():
    settings = Settings(public_hosts=["example.trycloudflare.com"])
    assert _post(settings, "example.trycloudflare.com") == 200
