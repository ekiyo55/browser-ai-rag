"""ブラウザの AI から呼ばれるリモート MCP サーバー。

第5章（Hello World）の段階。ここから章ごとに育てていく。
どの AI サービスから呼ばれても同じように動くよう、接続元を見分けて処理を変えることはしない。
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import Settings
from .reqlog import RequestLog

INSTRUCTIONS = """これは本書のサンプル用 MCP サーバーです。
いまは動作確認用のツールだけを持っています。"""

mcp = MCPServer(
    name="browser-ai-rag",
    title="ブラウザのAIが社内で働きだす（サンプル）",
    instructions=INSTRUCTIONS,
    version=__version__,
)


@mcp.tool(title="あいさつ")
def hello(name: str = "読者") -> str:
    """接続の確認用。名前を受け取って、あいさつを返す。"""
    return f"こんにちは、{name}さん。サーバーは動いています。"


@mcp.tool(title="接続元の確認")
def whoami(ctx: Context) -> str:
    """このサーバーを呼んでいる AI クライアントの名前とバージョンを返す。

    同じサーバーに Claude からも ChatGPT からもつながっていることを確かめるのに使う。
    """
    params = ctx.session.client_params
    if params is None:
        return "クライアント情報は送られてきませんでした。"
    info = params.client_info
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"クライアント: {info.name} {info.version}（{now}）"


def build_app(settings: Settings):
    """Streamable HTTP の ASGI アプリを作る。

    127.0.0.1 で待ち受けると SDK は DNS リバインディング対策を自動で有効にし、
    Host が localhost 以外のリクエストを 421 で弾く。トンネルや本番の公開ホスト名で
    受けるには、そのホスト名を許可リストに足す必要がある（第6章の落とし穴）。
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = mcp.streamable_http_app(transport_security=security, host=settings.host)
    return RequestLog(app)  # /mcp へのリクエストを1行ずつ記録する（第9章）
