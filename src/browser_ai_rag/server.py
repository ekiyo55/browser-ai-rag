"""ブラウザの AI から呼ばれる、社内文書の検索サーバー（第3部）。

どの AI サービスから呼ばれても同じように動くよう、接続元を見分けて処理を変えることはしない。
道具は五つだけ：検索、本文の読み出し、一覧、メモの保存、メモの削除。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Annotated

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .embed import Embedder
from .auth import AuthDB, Provider, add_login_routes, current_user
from .ingest import ingest_file
from .reqlog import RequestLog
from .store import Store

INSTRUCTIONS = """サンプル商事の社内文書（規程・マニュアル・議事録）と、利用者が残したメモを検索できるサーバーです。
社内のルールや手続き、過去の会議の内容を聞かれたら、推測で答えずに search_knowledge で調べてください。
答えには、根拠にした文書名と見出し（条番号など）を必ず添えてください。
検索結果の断片だけでは足りないときは、read_document で前後を読んでください。
利用者に「覚えておいて」「メモして」と頼まれたら save_note で保存します。"""

TOOLS: list[tuple] = []  # (関数, title, 注釈)。create_server でサーバーに登録する


def tool(title: str, annotations: ToolAnnotations):
    """道具の印。第16章までは @mcp.tool を直接使っていたが、ログインの有無でサーバーの作り方が
    変わるので（第17章）、ここでは印だけ付けておき、サーバーを作るときにまとめて登録する。"""
    def mark(fn):
        TOOLS.append((fn, title, annotations))
        return fn
    return mark

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

READ_LIMIT = 6000  # read_document が一度に返す文字数の上限


class _State:
    settings: Settings
    store: Store
    embedder: Embedder


state = _State()


def configure(settings: Settings, store: Store | None = None, embedder: Embedder | None = None) -> None:
    state.settings = settings
    state.store = store or Store(settings.db_path)
    state.embedder = embedder or Embedder()


# ---------------------------------------------------------------- 答えの形


class SearchHit(BaseModel):
    document_id: int
    title: str = Field(description="文書名")
    heading: str = Field(description="見出し（条番号など）")
    page: int | None = Field(default=None, description="PDF のページ番号")
    text: str = Field(description="見つかった断片の本文")
    score: float = Field(description="0〜1。大きいほど質問に近い")
    cite: str = Field(description="回答に添える出典の書き方")
    kind: str = Field(description="doc（社内文書）か note（利用者が残したメモ）。メモは社内の正式な文書ではない")
    owner: str | None = Field(default=None, description="メモを書いた利用者")


class DocumentText(BaseModel):
    document_id: int
    title: str
    text: str
    next_offset: int | None = Field(default=None, description="続きがあるときに offset に渡す値")


class DocumentInfo(BaseModel):
    document_id: int
    title: str
    kind: str = Field(description="doc（社内文書）または note（メモ）")
    owner: str | None = Field(default=None, description="メモを書いた利用者")
    chunks: int
    updated: str


# ---------------------------------------------------------------- 道具


@tool(title="社内文書を検索", annotations=READ_ONLY)
def search_knowledge(
    query: Annotated[str, Field(description="調べたいこと。質問文のままでも、重要な語を並べてもよい。言い換え（例：残業→時間外労働）を足すと見つかりやすい")],
    limit: Annotated[int, Field(description="返す断片の数", ge=1, le=10)] = 5,
    document_id: Annotated[int | None, Field(description="この文書の中だけを探すときの番号（list_documents で確かめられる）")] = None,
) -> list[SearchHit]:
    """社内文書とメモを検索し、質問に近い断片を、出典（文書名・見出し・ページ）付きで返す。"""
    hits = state.store.search(query, state.embedder.query(query), limit=limit,
                              alpha=state.settings.hybrid_alpha, doc_id=document_id)
    return [
        SearchHit(document_id=h.doc_id, title=h.title, heading=h.heading, page=h.page, text=h.text, score=h.score,
                  kind=h.kind, owner=h.owner,
                  cite=(f"メモ「{h.title}」（書き手 {h.owner or '不明'}）" if h.kind == "note"
                        else f"{h.title} {h.heading}" + (f"（p.{h.page}）" if h.page else "")))
        for h in hits
    ]


@tool(title="文書を読む", annotations=READ_ONLY)
def read_document(
    document_id: Annotated[int, Field(description="search_knowledge か list_documents で得た文書の番号")],
    heading: Annotated[str | None, Field(description="この見出しを含む部分だけを読む（例：第5条）。省略すると全文")] = None,
    offset: Annotated[int, Field(description="続きを読むときに、前回の next_offset を渡す", ge=0)] = 0,
) -> DocumentText:
    """文書の本文を読む。検索で見つかった断片の前後や、条文の全体を確かめるときに使う。"""
    doc = state.store.document(document_id)
    if doc is None:
        raise ToolError(f"文書番号 {document_id} は見つかりません。list_documents で番号を確かめてください。")
    parts = [c for c in state.store.chunks_of(document_id) if heading is None or heading in c["heading"]]
    if not parts:
        raise ToolError(f"「{heading}」を含む見出しは、この文書にありません。")
    text = "\n\n".join(f"【{c['heading']}{'・p.' + str(c['page']) if c['page'] else ''}】\n{c['text']}" for c in parts)
    end = offset + READ_LIMIT
    return DocumentText(document_id=document_id, title=doc["title"], text=text[offset:end],
                        next_offset=end if end < len(text) else None)


@tool(title="文書の一覧", annotations=READ_ONLY)
def list_documents() -> list[DocumentInfo]:
    """検索できる文書とメモの一覧を返す。"""
    return [
        DocumentInfo(document_id=d["id"], title=d["title"], kind=d["kind"], owner=d["owner"], chunks=d["chunks"],
                     updated=datetime.fromtimestamp(d["updated_at"]).strftime("%Y-%m-%d %H:%M"))
        for d in state.store.documents()
    ]


@tool(title="メモを保存", annotations=WRITE)
def save_note(
    title: Annotated[str, Field(description="メモの題名", min_length=1, max_length=100)],
    body: Annotated[str, Field(description="メモの本文（Markdown 可）", min_length=1, max_length=20000)],
) -> DocumentInfo:
    """利用者に頼まれた内容をメモとして保存し、以後の検索の対象にする。社内文書は書き換えない。"""
    folder = state.settings.notes_dir
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S-") + (re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:40] or "note")
    path = folder / f"{stem}.md"
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    doc_id = ingest_file(state.store, state.embedder, state.settings.data_dir, path, "note", owner=current_user())
    d = next(x for x in state.store.documents() if x["id"] == doc_id)
    return DocumentInfo(document_id=doc_id, title=d["title"], kind="note", owner=d["owner"], chunks=d["chunks"],
                        updated=datetime.fromtimestamp(d["updated_at"]).strftime("%Y-%m-%d %H:%M"))


@tool(title="メモを削除", annotations=DELETE)
def delete_note(
    document_id: Annotated[int, Field(description="削除するメモの番号。社内文書（kind=doc）は削除できない")],
) -> str:
    """save_note で保存したメモを削除する。社内文書は対象外。"""
    doc = state.store.document(document_id)
    if doc is None:
        raise ToolError(f"文書番号 {document_id} は見つかりません。")
    if doc["kind"] != "note":
        raise ToolError("社内文書は削除できません。削除できるのは save_note で保存したメモだけです。")
    if doc["owner"] and doc["owner"] != current_user():
        raise ToolError("ほかの人が書いたメモは削除できません。")
    Path(state.settings.data_dir, doc["path"]).unlink(missing_ok=True)
    state.store.delete_document(document_id)
    return f"メモ「{doc['title']}」を削除しました。"


# ---------------------------------------------------------------- サーバーを作る


def create_server(settings: Settings) -> MCPServer:
    """MCP サーバーを作る。settings.base_url があればログイン（OAuth）を付ける（第17章）。"""
    auth_kwargs = {}
    db = None
    if settings.base_url:
        db = AuthDB(settings.auth_db_path)
        auth_kwargs = dict(
            auth_server_provider=Provider(db, settings.base_url),
            auth=AuthSettings(
                issuer_url=settings.base_url,
                resource_server_url=f"{settings.base_url}/mcp",
                validate_token_resource=True,  # ほかのサーバー宛てに発行された合鍵は受け付けない
                client_registration_options=ClientRegistrationOptions(enabled=True),  # 動的クライアント登録
                revocation_options=RevocationOptions(enabled=True),
            ),
        )
    mcp = MCPServer(name="browser-ai-rag", title="サンプル商事 社内文書検索", instructions=INSTRUCTIONS,
                    version=__version__, **auth_kwargs)
    for fn, title, annotations in TOOLS:
        mcp.tool(title=title, annotations=annotations)(fn)
    if db is not None:
        add_login_routes(mcp, db)
    return mcp


# ---------------------------------------------------------------- HTTP


def build_app(settings: Settings, store: Store | None = None, embedder: Embedder | None = None):
    """Streamable HTTP の ASGI アプリを作る。

    127.0.0.1 で待ち受けると SDK は DNS リバインディング対策を自動で有効にし、
    Host が localhost 以外のリクエストを 421 で弾く。トンネルや本番の公開ホスト名で
    受けるには、そのホスト名を許可リストに足す必要がある（第6章の落とし穴）。
    """
    configure(settings, store, embedder)
    mcp = create_server(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = mcp.streamable_http_app(transport_security=security, host=settings.host)
    return RequestLog(app)  # /mcp へのリクエストを1行ずつ記録する（第9章）
