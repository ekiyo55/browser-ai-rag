"""ログインの仕組み（第17章）。MCP サーバー自身が OAuth 2.1 の認可サーバーを兼ねる。

流れ：
  AI のクラウドが /register で自分を登録（動的クライアント登録）
  → 利用者のブラウザで /authorize を開く → このサーバーの /login でユーザー名とパスワード
  → 認可コードを AI の戻り先へ → AI が /token で合鍵（アクセストークン）に引き換える

どの AI サービスから来ても同じ手順で扱う。戻り先（redirect_uri）や Origin を、特定の会社のものに決め打ちしない。
合鍵は「誰の合鍵か」（ユーザー名）を持つので、道具の中で利用者を確かめられる（第24章のタイムカードで使う）。
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ACCESS_TTL = 3600            # 合鍵は1時間。期限が切れたら AI が控え（リフレッシュトークン）で作り直す
REFRESH_TTL = 30 * 24 * 3600  # 控えは30日。使うたびに新しいものと取り替える
PENDING_TTL = 600            # ログイン画面を開いてから10分以内にログインする
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60  # 5回間違えたら15分ロック

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, display_name TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients(client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending(key TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes(code_hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens(
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, username TEXT NOT NULL,
  scopes TEXT, resource TEXT, expires_at INTEGER NOT NULL, pair TEXT
);
CREATE TABLE IF NOT EXISTS login_fail(ip TEXT PRIMARY KEY, count INTEGER NOT NULL, until INTEGER NOT NULL);
"""


def _h(s: str) -> str:
    """合鍵やコードは、そのままではなくハッシュにして保存する。DB が漏れても合鍵は使えない。"""
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc, digest)
    except ValueError:
        return False


def current_user() -> str | None:
    """道具の中から、いま呼んでいる利用者のユーザー名を得る。ログインなしで動かしているときは None。"""
    token = get_access_token()
    return token.subject if token else None


class AuthDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock, self.db:
            return self.db.execute(sql, args).rowcount

    def purge(self) -> None:
        now = int(time.time())
        for table in ("oauth_pending", "oauth_codes", "oauth_tokens"):
            self.x(f"DELETE FROM {table} WHERE expires_at < ?", (now,))

    # 利用者

    def add_user(self, username: str, password: str, display_name: str | None = None) -> None:
        self.x("INSERT OR REPLACE INTO users VALUES(?,?,?,?)",
               (username, hash_password(password), display_name or username, int(time.time())))

    def verify_user(self, username: str, password: str) -> bool:
        rows = self.q("SELECT pw_hash FROM users WHERE username=?", (username,))
        # 存在しない利用者でも同じだけ時間をかけ、利用者名の有無を応答時間から悟らせない
        return check_password(password, rows[0]["pw_hash"] if rows else hash_password("x", 600_000)) and bool(rows)


class Provider:
    """MCP SDK の OAuthAuthorizationServerProvider の約束に沿った実装。"""

    def __init__(self, db: AuthDB, base_url: str) -> None:
        self.db, self.base_url = db, base_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.db.q("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0]["info"]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.db.x("INSERT OR REPLACE INTO oauth_clients VALUES(?,?,?)",
                  (client_info.client_id, client_info.model_dump_json(), int(time.time())))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.db.purge()
        key = secrets.token_urlsafe(24)
        self.db.x("INSERT INTO oauth_pending VALUES(?,?,?,?)",
                  (key, client.client_id, params.model_dump_json(), int(time.time()) + PENDING_TTL))
        return f"{self.base_url}/login?req={key}"

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        rows = self.db.q("SELECT data FROM oauth_codes WHERE code_hash=?", (_h(authorization_code),))
        if not rows:
            return None
        code = AuthorizationCode.model_validate_json(rows[0]["data"])
        return code if code.client_id == client.client_id and code.expires_at >= time.time() else None

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        self.db.x("DELETE FROM oauth_codes WHERE code_hash=?", (_h(authorization_code.code),))  # コードは一度きり
        return self._issue(client.client_id, authorization_code.subject or "", authorization_code.scopes,
                           authorization_code.resource)

    def _issue(self, client_id: str, username: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        now, sc = int(time.time()), " ".join(scopes)
        self.db.x("INSERT INTO oauth_tokens VALUES(?,?,?,?,?,?,?,?)",
                  (_h(access), "access", client_id, username, sc, resource, now + ACCESS_TTL, _h(refresh)))
        self.db.x("INSERT INTO oauth_tokens VALUES(?,?,?,?,?,?,?,?)",
                  (_h(refresh), "refresh", client_id, username, sc, resource, now + REFRESH_TTL, _h(access)))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=sc or None)

    def _token(self, token: str, kind: str):
        rows = self.db.q("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind=?", (_h(token), kind))
        return rows[0] if rows and rows[0]["expires_at"] >= time.time() else None

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        r = self._token(refresh_token, "refresh")
        if r is None or r["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=(r["scopes"] or "").split(),
                            expires_at=r["expires_at"], resource=r["resource"], subject=r["username"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        r = self._token(refresh_token.token, "refresh")
        if r is None:
            raise TokenError("invalid_grant", "refresh token revoked")
        # 控えは使い捨て。古い合鍵と控えを消してから、新しい組を出す
        self.db.x("DELETE FROM oauth_tokens WHERE token_hash IN (?,?)", (_h(refresh_token.token), r["pair"]))
        return self._issue(client.client_id, r["username"], scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        r = self._token(token, "access")
        if r is None:
            return None
        return AccessToken(token=token, client_id=r["client_id"], scopes=(r["scopes"] or "").split(),
                           expires_at=r["expires_at"], resource=r["resource"], subject=r["username"])

    async def revoke_token(self, token) -> None:
        rows = self.db.q("SELECT pair FROM oauth_tokens WHERE token_hash=?", (_h(token.token),))
        if rows:
            self.db.x("DELETE FROM oauth_tokens WHERE token_hash IN (?,?)", (_h(token.token), rows[0]["pair"]))

    async def exchange_identity_assertion(self, client, params):
        raise TokenError("unsupported_grant_type", "not supported")


# ---------------------------------------------------------------- ログイン画面


def _client_ip(req: Request) -> str:
    return (req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (req.client.host if req.client else "?"))


# 合鍵の宛先（resource）ごとに、利用者に見せる「何をさせる鍵か」。ログインを共通にするなら、ここを必ず分ける（第29章）
PURPOSES = {
    "/mcp": ("社内文書検索", "あなたの代わりに、社内文書を検索し、メモを残そうとしています。"),
    "/mail/mcp": ("メール", "あなたの受信箱を読み、返信の下書きを作り、メールを送ろうとしています。"),
    "/timecard/mcp": ("タイムカード", "あなたの名前で出勤・退勤を打刻し、あなたの勤怠を見ようとしています。"),
}


def purpose_of(resource: str | None) -> tuple[str, str]:
    return PURPOSES.get(urlparse(resource or "").path, ("社内ツール", "あなたの代わりに、社内のツールを使おうとしています。"))


def _page(key: str, client_name: str | None, error: str = "", resource: str | None = None) -> HTMLResponse:
    service, does = purpose_of(resource)
    who = f"<p><b>{html.escape(client_name)}</b> が、{html.escape(does)}</p>" if client_name else ""
    err = f"<p style='color:#c0392b'>{html.escape(error)}</p>" if error else ""
    body = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ログイン</title>
<style>body{{font-family:sans-serif;max-width:420px;margin:48px auto;padding:0 16px}}
input,button{{width:100%;padding:10px;margin:6px 0;font-size:16px;box-sizing:border-box}}</style></head><body>
<h1>サンプル商事 {html.escape(service)}</h1>{who}{err}
<form method="post" action="/login"><input type="hidden" name="req" value="{html.escape(key)}">
<label>ユーザー名<input name="username" autocomplete="username" required autofocus></label>
<label>パスワード<input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit">許可してログイン</button></form></body></html>"""
    # 他のサイトの枠の中に表示させない（パスワードを盗む細工を防ぐ）
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def add_login_routes(mcp, db: AuthDB) -> None:
    def pending(key: str):
        rows = db.q("SELECT client_id, params FROM oauth_pending WHERE key=? AND expires_at>?", (key, int(time.time())))
        return rows[0] if rows else None

    def _resource(p) -> str | None:
        return AuthorizationParams.model_validate_json(p["params"]).resource

    def client_name(client_id: str) -> str:
        rows = db.q("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        info = json.loads(rows[0]["info"]) if rows else {}
        return info.get("client_name") or client_id

    @mcp.custom_route("/login", methods=["GET"])
    async def login_get(req: Request) -> Response:
        p = pending(req.query_params.get("req", ""))
        if p is None:
            return HTMLResponse("ログインの期限が切れました。AI サービスの画面から接続をやり直してください。", 400)
        return _page(req.query_params["req"], client_name(p["client_id"]), resource=_resource(p))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_post(req: Request) -> Response:
        form = await req.form()
        key, ip = str(form.get("req", "")), _client_ip(req)
        p = pending(key)
        if p is None:
            return HTMLResponse("ログインの期限が切れました。AI サービスの画面から接続をやり直してください。", 400)
        lock = db.q("SELECT count, until FROM login_fail WHERE ip=?", (ip,))
        if lock and lock[0]["count"] >= LOCK_AFTER and lock[0]["until"] > time.time():
            return _page(key, client_name(p["client_id"]), "失敗が続いたため、15分間ロックしています。", _resource(p))
        username = str(form.get("username", "")).strip()
        if not db.verify_user(username, str(form.get("password", ""))):
            db.x("INSERT INTO login_fail VALUES(?,1,?) ON CONFLICT(ip) DO UPDATE SET count=count+1, until=excluded.until",
                 (ip, int(time.time()) + LOCK_SECONDS))
            return _page(key, client_name(p["client_id"]), "ユーザー名かパスワードが違います。", _resource(p))
        db.x("DELETE FROM login_fail WHERE ip=?", (ip,))
        db.x("DELETE FROM oauth_pending WHERE key=?", (key,))
        params = AuthorizationParams.model_validate_json(p["params"])
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(code=code, scopes=params.scopes or [], expires_at=time.time() + 300,
                               client_id=p["client_id"], code_challenge=params.code_challenge,
                               redirect_uri=params.redirect_uri,
                               redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                               resource=params.resource, subject=username)
        db.x("INSERT INTO oauth_codes VALUES(?,?,?)", (_h(code), ac.model_dump_json(), int(time.time()) + 300))
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)


# ---------------------------------------------------------------- 利用者の登録（コマンド）


def main() -> None:
    """uv run python -m browser_ai_rag.auth add <ユーザー名> [表示名]"""
    from .config import Settings

    if len(sys.argv) < 3 or sys.argv[1] != "add":
        print("使い方: python -m browser_ai_rag.auth add <ユーザー名> [表示名]")
        sys.exit(2)
    username = sys.argv[2]
    password = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("パスワード: ")
    if len(password) < 12:
        print("パスワードは12文字以上にしてください。")
        sys.exit(1)
    AuthDB(Settings.from_env().auth_db_path).add_user(username, password, sys.argv[3] if len(sys.argv) > 3 else None)
    print(f"利用者 {username} を登録しました。")


if __name__ == "__main__":
    main()
