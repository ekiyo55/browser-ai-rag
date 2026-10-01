"""ログイン（OAuth 2.1・動的クライアント登録）を、AI のクラウドと同じ手順でたどる（第17章）。"""

import base64
import hashlib
import json
import secrets
import urllib.parse

import pytest
from starlette.testclient import TestClient

from browser_ai_rag.auth import AuthDB
from browser_ai_rag.config import Settings
from browser_ai_rag.server import build_app

from conftest import FakeEmbedder, make

BASE = "https://book.example.com"
CALLBACK = "https://ai.example.net/oauth/callback"  # どの AI サービスの戻り先でも受け付ける
H = {"Host": "book.example.com", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def client(data_dir):
    settings = Settings(data_dir=data_dir, base_url=BASE)
    _, store = make(data_dir, FakeEmbedder())
    db = AuthDB(settings.auth_db_path)
    db.add_user("yamada", "yamada-password-123")
    db.add_user("sato", "sato-password-456")
    with TestClient(build_app(settings, store, FakeEmbedder()), base_url=BASE) as c:
        yield c


def login(c: TestClient, username: str, password: str) -> str:
    reg = c.post("/register", json={"client_name": "テスト用AI", "redirect_uris": [CALLBACK],
                                    "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                                    "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256",
                                    "resource": f"{BASE}/mcp"}, follow_redirects=False)
    assert r.status_code == 302 and "/login?req=" in r.headers["location"]
    key = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["req"][0]
    page = c.get(f"/login?req={key}")
    assert "テスト用AI" in page.text  # 誰に鍵を渡すのかがログイン画面に出る
    r = c.post("/login", data={"req": key, "username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(CALLBACK)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
    t = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                               "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert t.status_code == 200
    return t.json()["access_token"]


def call(c: TestClient, token: str, name: str, args: dict) -> dict:
    h = {**H, "Authorization": f"Bearer {token}"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    r = c.post("/mcp", json=init, headers=h)
    sid = r.headers["mcp-session-id"]
    h2 = {**h, "Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"}
    c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, headers=h2)
    data = [line[5:] for line in r.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1] if data else r.text)["result"]


def test_without_token_points_to_metadata(client):
    r = client.post("/mcp", json={}, headers=H)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": H["Host"]}).json()
    assert meta["authorization_servers"] == [BASE]


def test_wrong_password_is_rejected(client):
    with pytest.raises(AssertionError):
        login(client, "yamada", "wrong-password")


def test_login_and_owned_notes(client):
    yamada = login(client, "yamada", "yamada-password-123")
    sato = login(client, "sato", "sato-password-456")
    saved = call(client, yamada, "save_note", {"title": "山田のメモ", "body": "来週の会議の準備"})["structuredContent"]
    assert saved["owner"] == "yamada"
    denied = call(client, sato, "delete_note", {"document_id": saved["document_id"]})
    assert denied["isError"] and "ほかの人が書いたメモ" in denied["content"][0]["text"]
    ok = call(client, yamada, "delete_note", {"document_id": saved["document_id"]})
    assert not ok["isError"]
