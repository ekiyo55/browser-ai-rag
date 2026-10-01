"""リクエストの記録が、二つの世代の通信をどちらも読み分けられることを確かめる。"""

import json

from browser_ai_rag.reqlog import _summary


def test_handshake_era():
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "inspector-cli", "version": "2.9.0"}}}
    s = _summary(json.dumps(body).encode())
    assert s["method"] == "initialize"
    assert s["client"] == "inspector-cli 2.9.0"
    assert s["proto"] == "2025-11-25"


def test_stateless_era():
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.1.286"}}
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "whoami", "arguments": {}, "_meta": meta}}
    s = _summary(json.dumps(body).encode())
    assert (s["method"], s["tool"], s["client"], s["proto"]) == ("tools/call", "whoami", "claude-code 2.1.286", "2026-07-28")


def test_not_json():
    assert _summary(b"not json") == {}
