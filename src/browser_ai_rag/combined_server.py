"""六本のサーバーの道具を、一本にまとめたサーバー（第29章の比べるための実験）。

    uv run python -m browser_ai_rag.combined_server

URL は {RAG_BASE_URL}/all/mcp。中身は、それぞれのサーバーの道具をそのまま並べただけ。
分けたサーバーと同じデータを使うので、どちらから使っても同じ結果になる。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from . import count_server, mail_server, ops_server, schedule_server, server, timecard_server
from .auth import AuthDB
from .config import Settings
from .mail_server import SharedTokenVerifier
from .reqlog import RequestLog

PARTS = [  # (見出し, モジュール)
    ("社内文書検索", server),
    ("メール", mail_server),
    ("タイムカード", timecard_server),
    ("売上・案件", count_server),
    ("予定とタスク", schedule_server),
    ("運用の窓口", ops_server),
]


def configure_all(settings: Settings, mail=None, ops_backend=None) -> None:
    server.configure(settings)
    mail_server.configure(settings, mail or mail_server.MailSettings.from_env())
    timecard_server.configure(settings, timecard_server.TimecardSettings.from_env())
    count_server.configure(settings, count_server.CountSettings(mode="tools"))
    schedule_server.configure(settings, schedule_server.ScheduleSettings.from_env())
    ops_server.configure(settings, ops_server.OpsSettings.from_env(), backend=ops_backend)


def tools_of(module) -> list[tuple]:
    """それぞれのサーバーの道具の一覧（関数, 題名, 注釈）。数える道具は本番の四つだけ。"""
    if module is count_server:
        return [(fn, title, count_server.READ_ONLY) for fn, title, mode in module.TOOLS if mode == "tools"]
    return list(module.TOOLS)


def create_combined_server(settings: Settings) -> MCPServer:
    kwargs = {}
    if settings.base_url:
        kwargs = dict(
            token_verifier=SharedTokenVerifier(AuthDB(settings.auth_db_path), settings.base_url),
            auth=AuthSettings(issuer_url=settings.base_url, resource_server_url=f"{settings.base_url}/all/mcp",
                              validate_token_resource=True),
        )
    instructions = "サンプル商事の社内の仕事をまとめて扱うサーバーです。\n\n" + "\n\n".join(
        f"【{label}】\n{module.INSTRUCTIONS}" for label, module in PARTS)
    mcp = MCPServer(name="browser-ai-all", title="サンプル商事 まとめ", instructions=instructions,
                    version=__version__, **kwargs)
    seen: dict[str, str] = {}
    for label, module in PARTS:
        for fn, title, annotations in tools_of(module):
            if fn.__name__ in seen:   # 名前がぶつかったら、黙って上書きせずに止める
                raise RuntimeError(f"道具の名前 {fn.__name__} が「{seen[fn.__name__]}」と「{label}」でぶつかっています")
            seen[fn.__name__] = label
            mcp.tool(title=title, annotations=annotations)(fn)
    return mcp


def build_combined_app(settings: Settings):
    configure_all(settings)
    mcp = create_combined_server(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    return RequestLog(mcp.streamable_http_app(transport_security=security, host=settings.host,
                                              streamable_http_path="/all/mcp"), path="/all/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings = Settings.from_env()
    port = int(os.environ.get("ALL_PORT", "8706"))
    print(f"まとめた MCP: http://{settings.host}:{port}/all/mcp")
    uvicorn.run(build_combined_app(settings), host=settings.host, port=port)


if __name__ == "__main__":
    main()
