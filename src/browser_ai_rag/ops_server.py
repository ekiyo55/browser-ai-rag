"""運用の窓口の MCP サーバー（第28章）。本書のサーバーたちが動いているかを、AI から確かめる。

    uv run python -m browser_ai_rag.ops_server

URL は {RAG_BASE_URL}/ops/mcp。ログインはほかのサーバーと共通。

このサーバー自身は、ふつうの利用者（www-data）の権限で動く。
ログを読むことと再起動だけは root の権限が要るので、決まった操作しかできない小さな手伝いのスクリプト
（deploy/book-ops）を、sudo で呼ぶ。スクリプトの側でも、相手にしてよいサービスを確かめる。

- 稼働状況・サーバーの具合（負荷・メモリ・ディスク・証明書の期限）は、ログインした人なら誰でも見られる
- 最近のエラーと再起動は、管理者だけ。再起動は同じサービスにつき10分に1回まで、理由を残す
- ログは、IP アドレスや合鍵を伏せてから返す
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .auth import AuthDB, current_user
from .config import Settings
from .mail_server import SharedTokenVerifier
from .reqlog import RequestLog
from .timecard_server import _via_from

INSTRUCTIONS = """本書のサンプルの MCP サーバーたちが動いているかを確かめるサーバーです。
稼働状況とサーバーの具合は誰でも見られます。最近のエラーと再起動は管理者だけです。
再起動は、利用者がはっきり頼み、理由がわかっているときだけ行ってください。止まっていないサービスを、念のために再起動しないこと。
「全部再起動して」と頼まれても、まず service_status で様子を確かめ、必要なものだけを、利用者に確かめてから再起動してください。"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RESTART = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

COOLDOWN = 10 * 60
LOG_LINES = 30
ERROR_WORDS = re.compile(r"failed|error|traceback|exception|warning|critical| 5\d\d ", re.I)


class Service(BaseModel):
    key: str
    label: str
    unit: str
    port: int
    path: str
    restartable: bool = True


SERVICES = [
    Service(key="docs", label="文書検索（ログインもここ）", unit="mooma-book", port=8700, path="/mcp"),
    Service(key="mail", label="メール", unit="mooma-book-mail", port=8701, path="/mail/mcp"),
    Service(key="timecard", label="タイムカード", unit="mooma-book-timecard", port=8702, path="/timecard/mcp"),
    Service(key="count", label="売上・案件", unit="mooma-book-count", port=8703, path="/count/mcp"),
    Service(key="schedule", label="予定とタスク", unit="mooma-book-schedule", port=8704, path="/schedule/mcp"),
    Service(key="ops", label="運用の窓口（このサーバー）", unit="mooma-book-ops", port=8705, path="/ops/mcp",
            restartable=False),   # 自分自身は再起動させない（呼び出しの途中で止まってしまう）
]


class OpsSettings(BaseModel):
    admins: list[str] = []
    tls_host: str = ""             # 証明書の期限を確かめる名前（例 book.mooma.style）
    helper: str = "/usr/local/sbin/book-ops"
    port: int = 8705

    @classmethod
    def from_env(cls) -> OpsSettings:
        admins = [x.strip() for x in os.environ.get("OPS_ADMINS", "").split(",") if x.strip()]
        return cls(admins=admins, tls_host=os.environ.get("OPS_TLS_HOST", ""),
                   helper=os.environ.get("OPS_HELPER", "/usr/local/sbin/book-ops"),
                   port=int(os.environ.get("OPS_PORT", "8705")))


# ---------------------------------------------------------------- サーバーを触る部分（テストでは偽物に差し替える）


class SystemdBackend:
    """本物：systemctl で様子を見て、ログと再起動は手伝いのスクリプトを sudo で呼ぶ。"""

    def __init__(self, helper: str) -> None:
        self.helper = helper

    def unit_state(self, unit: str) -> dict:
        out = subprocess.run(["systemctl", "show", unit, "--property=ActiveState,SubState,ActiveEnterTimestamp,"
                              "MemoryCurrent,NRestarts"], capture_output=True, text=True, timeout=5).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def probe(self, port: int, path: str) -> tuple[int | None, int]:
        """MCP の入口の案内（保護リソースのメタデータ）に、手元から問い合わせる。200 なら動いている。"""
        t = time.monotonic()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource{path}",
                                        timeout=3) as r:
                return r.status, int((time.monotonic() - t) * 1000)
        except urllib.error.HTTPError as e:
            return e.code, int((time.monotonic() - t) * 1000)
        except OSError:
            return None, int((time.monotonic() - t) * 1000)

    def logs(self, unit: str, minutes: int) -> str:
        return self._helper("logs", unit, str(minutes))

    def restart(self, unit: str) -> str:
        return self._helper("restart", unit)

    def _helper(self, *args: str) -> str:
        r = subprocess.run(["sudo", "-n", self.helper, *args], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ToolError(f"手伝いのスクリプトが断りました: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout


class _State:
    settings: Settings
    ops: OpsSettings
    backend: object
    db: sqlite3.Connection
    auth: AuthDB | None = None
    clock = staticmethod(time.time)
    settle = 3.0                    # 再起動のあと、様子を見るまで待つ秒数（テストでは0）
    lock = threading.RLock()


state = _State()


def configure(settings: Settings, ops: OpsSettings, backend=None, clock=None) -> None:
    state.settings, state.ops = settings, ops
    state.backend = backend or SystemdBackend(ops.helper)
    state.clock = staticmethod(clock or time.time)
    path = settings.data_dir / "ops.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    state.db = sqlite3.connect(path, check_same_thread=False)
    state.db.row_factory = sqlite3.Row
    state.db.executescript("CREATE TABLE IF NOT EXISTS restarts(id INTEGER PRIMARY KEY, unit TEXT NOT NULL, "
                           "who TEXT NOT NULL, reason TEXT NOT NULL, via TEXT, at INTEGER NOT NULL, result TEXT)")
    state.auth = AuthDB(settings.auth_db_path) if settings.base_url else None


def _me() -> str:
    user = current_user()
    if user:
        return user
    if state.settings.base_url:
        raise ToolError("ログインしていないので、使えません。")
    return "local"


def _admin() -> str:
    me = _me()
    if me not in state.ops.admins:
        raise ToolError("最近のエラーと再起動は、管理者だけが使えます。")
    return me


def _service(key: str) -> Service:
    for s in SERVICES:
        if key in (s.key, s.unit, s.label) or key in s.label:
            return s
    raise ToolError(f"「{key}」というサービスはありません。使える名前: " + "、".join(f"{s.key}（{s.label}）" for s in SERVICES))


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def redact(text: str) -> str:
    """ログを AI に渡す前に、IP アドレスと合鍵らしきものを伏せる。"""
    text = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "x.x.x.x", text)
    text = re.sub(r"(?i)(bearer\s+|token[=:]\s*|password[=:]\s*)\S+", r"\1***", text)
    return text


# ---------------------------------------------------------------- 答えの形


class ServiceState(BaseModel):
    key: str
    label: str
    running: bool = Field(description="systemd で動いているか")
    answering: bool = Field(description="入口に問い合わせて、答えが返ったか")
    since: str | None = Field(description="今の起動の時刻")
    memory_mb: int | None
    restarts: int | None = Field(description="systemd が自動で起こし直した回数")
    response_ms: int


class StatusAnswer(BaseModel):
    checked_at: str
    all_ok: bool
    services: list[ServiceState]


class Health(BaseModel):
    checked_at: str
    load_1min: float
    cpus: int
    memory_used_percent: int
    disk_used_percent: int
    disk_free_gb: float
    certificate: str = Field(description="公開している証明書の期限と残り日数")


class ErrorLines(BaseModel):
    service: str
    minutes: int
    lines: list[str] = Field(description="エラーらしい行（新しいものが下）。IP アドレスなどは伏せてある")
    note: str


class RestartResult(BaseModel):
    service: str
    before: ServiceState
    after: ServiceState
    note: str


TOOLS: list[tuple] = []


def tool(title: str, annotations: ToolAnnotations):
    def mark(fn):
        TOOLS.append((fn, title, annotations))
        return fn
    return mark


def _state_of(s: Service) -> ServiceState:
    u = state.backend.unit_state(s.unit)
    code, ms = state.backend.probe(s.port, s.path)
    mem = u.get("MemoryCurrent", "")
    since = u.get("ActiveEnterTimestamp") or None
    return ServiceState(key=s.key, label=s.label, running=u.get("ActiveState") == "active",
                        answering=code == 200, since=since, response_ms=ms,
                        memory_mb=int(mem) // (1024 * 1024) if mem.isdigit() else None,
                        restarts=int(u["NRestarts"]) if u.get("NRestarts", "").isdigit() else None)


# ---------------------------------------------------------------- 道具


@tool("稼働状況を見る", READ_ONLY)
def service_status() -> StatusAnswer:
    """本書のサーバーそれぞれが、動いているか・入口が答えるか・いつから動いているか・メモリをどれだけ使っているかを返す。"""
    _me()
    states = [_state_of(s) for s in SERVICES]
    return StatusAnswer(checked_at=_when(state.clock()), all_ok=all(x.running and x.answering for x in states),
                        services=states)


@tool("サーバーの具合を見る", READ_ONLY)
def server_health() -> Health:
    """サーバー機の負荷・メモリ・ディスクの空き・公開している証明書の期限を返す。"""
    _me()
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    mem = 0
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo", encoding="utf-8"))
        total, avail = int(info["MemTotal"].split()[0]), int(info["MemAvailable"].split()[0])
        mem = round((total - avail) * 100 / total)
    except (OSError, KeyError, ValueError):
        pass
    du = shutil.disk_usage("/")
    return Health(checked_at=_when(state.clock()), load_1min=round(load, 2), cpus=os.cpu_count() or 1,
                  memory_used_percent=mem, disk_used_percent=round(du.used * 100 / du.total),
                  disk_free_gb=round(du.free / 1024 ** 3, 1), certificate=_certificate())


def _certificate() -> str:
    host = state.ops.tls_host
    if not host:
        return "確かめていません（OPS_TLS_HOST が未設定）"
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock, ctx.wrap_socket(sock, server_hostname=host) as s:
            not_after = ssl.cert_time_to_seconds(s.getpeercert()["notAfter"])
    except (OSError, ssl.SSLError) as e:
        return f"確かめられませんでした（{type(e).__name__}）"
    days = int((not_after - state.clock()) // 86400)
    return f"{host} の証明書は {_when(not_after)} まで（残り {days} 日）"


@tool("最近のエラーを見る", READ_ONLY)
def recent_errors(
    service: Annotated[str, Field(description="docs・mail・timecard・count・schedule・ops のどれか（日本語の名前でもよい）")],
    minutes: Annotated[int, Field(ge=5, le=1440, description="何分前までさかのぼるか")] = 60,
) -> ErrorLines:
    """（管理者だけ）サービスのログから、エラーらしい行を新しいほうから最大30行返す。IP アドレスや合鍵は伏せる。"""
    _admin()
    s = _service(service)
    raw = state.backend.logs(s.unit, minutes)
    hits = [redact(line) for line in raw.splitlines() if ERROR_WORDS.search(line)][-LOG_LINES:]
    note = "エラーらしい行はありません。" if not hits else f"{len(hits)} 行。道具の失敗（Tool '…' failed）は、多くは利用者の入力が断られた記録です。"
    return ErrorLines(service=s.label, minutes=minutes, lines=hits, note=note)


@tool("サービスを再起動する", RESTART)
def restart_service(
    service: Annotated[str, Field(description="再起動するサービス（docs・mail・timecard・count・schedule）")],
    reason: Annotated[str, Field(min_length=4, max_length=200, description="再起動する理由（記録に残る）")],
) -> RestartResult:
    """（管理者だけ）サービスを一つ再起動する。同じサービスは10分に1回まで。理由と、誰がどの AI から行ったかを記録する。
    止まっていないサービスを念のために再起動しないこと。"""
    me = _admin()
    s = _service(service)
    if not s.restartable:
        raise ToolError(f"{s.label} は、この道具では再起動できません。サーバーに入って、手で行ってください。")
    with state.lock:
        last = state.db.execute("SELECT at FROM restarts WHERE unit=? AND result='ok' ORDER BY at DESC LIMIT 1",
                                (s.unit,)).fetchone()
        if last and state.clock() - last["at"] < COOLDOWN:
            wait = int((COOLDOWN - (state.clock() - last["at"])) // 60) + 1
            raise ToolError(f"{s.label} は {_when(last['at'])} に再起動したばかりです。あと {wait} 分は再起動できません。"
                            "続けて止まるなら、recent_errors で原因を確かめてください。")
        before = _state_of(s)
        cur = state.db.execute("INSERT INTO restarts(unit, who, reason, via, at) VALUES(?,?,?,?,?)",
                               (s.unit, me, reason, _via_from(state.auth), int(state.clock())))
        state.db.commit()
        try:
            state.backend.restart(s.unit)
        except ToolError:
            state.db.execute("UPDATE restarts SET result='failed' WHERE id=?", (cur.lastrowid,))
            state.db.commit()
            raise
        time.sleep(state.settle)
        after = _state_of(s)
        state.db.execute("UPDATE restarts SET result=? WHERE id=?", ("ok" if after.running else "not running",
                                                                      cur.lastrowid))
        state.db.commit()
    note = "再起動しました。" if after.running and after.answering else "再起動しましたが、まだ答えが返りません。少し待って稼働状況を見てください。"
    return RestartResult(service=s.label, before=before, after=after, note=note)


@tool("再起動の記録を見る", READ_ONLY)
def restart_history(
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> list[dict]:
    """（管理者だけ）再起動の記録（いつ・誰が・どの AI から・なぜ・結果）を新しい順に返す。"""
    _admin()
    return [{"when": _when(r["at"]), "service": next((s.label for s in SERVICES if s.unit == r["unit"]), r["unit"]),
             "who": r["who"], "via": r["via"], "reason": r["reason"], "result": r["result"]}
            for r in state.db.execute("SELECT * FROM restarts ORDER BY at DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- サーバー


def create_ops_server(settings: Settings) -> MCPServer:
    kwargs = {}
    if settings.base_url:
        kwargs = dict(
            token_verifier=SharedTokenVerifier(AuthDB(settings.auth_db_path), settings.base_url),
            auth=AuthSettings(issuer_url=settings.base_url, resource_server_url=f"{settings.base_url}/ops/mcp",
                              validate_token_resource=True),
        )
    mcp = MCPServer(name="browser-ai-ops", title="サンプル商事 運用の窓口", instructions=INSTRUCTIONS,
                    version=__version__, **kwargs)
    for fn, title, annotations in TOOLS:
        mcp.tool(title=title, annotations=annotations)(fn)
    return mcp


def build_ops_app(settings: Settings, ops: OpsSettings, backend=None):
    configure(settings, ops, backend)
    mcp = create_ops_server(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    return RequestLog(mcp.streamable_http_app(transport_security=security, host=settings.host,
                                              streamable_http_path="/ops/mcp"), path="/ops/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings, ops = Settings.from_env(), OpsSettings.from_env()
    print(f"運用の窓口 MCP: http://{settings.host}:{ops.port}/ops/mcp（管理者: {', '.join(ops.admins) or 'なし'}）")
    uvicorn.run(build_ops_app(settings, ops), host=settings.host, port=ops.port)


if __name__ == "__main__":
    main()
