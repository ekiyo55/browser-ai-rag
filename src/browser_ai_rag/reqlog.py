"""/mcp に届いたリクエストを1行ずつ記録する（第9章）。

どの AI から、どの世代の通信（握手あり／握手なし）で、何を頼まれたのかを見分けるためのもの。
本文は JSON-RPC のメソッド名と道具の名前だけを拾い、引数の中身は記録しない。
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("browser_ai_rag.requests")


def _summary(body: bytes) -> dict:
    try:
        msg = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(msg, dict):
        return {"method": "(batch)"}
    params = msg.get("params") or {}
    meta = params.get("_meta") or {}
    client = params.get("clientInfo") or meta.get("io.modelcontextprotocol/clientInfo") or {}
    return {
        "method": msg.get("method", "(response)"),
        "tool": params.get("name") if msg.get("method") == "tools/call" else None,
        "client": f"{client.get('name', '')} {client.get('version', '')}".strip() or None,
        "proto": params.get("protocolVersion") or meta.get("io.modelcontextprotocol/protocolVersion"),
    }


class RequestLog:
    """ASGI のミドルウェア。/mcp への POST・GET・DELETE を記録する。"""

    def __init__(self, app, path: str = "/mcp") -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.path):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        chunks: list[bytes] = []
        status = 0
        started = time.perf_counter()

        async def receive_and_keep():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        async def send_and_watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_and_keep, send_and_watch)
        finally:
            body = b"".join(chunks)
            info = _summary(body) if scope["method"] == "POST" else {}
            if os.environ.get("RAG_LOG_HANDSHAKE") and info.get("method") in ("initialize", "server/discover"):
                # 自己紹介（クライアントの名前と機能）だけは本文ごと残す。道具の引数は含まれない
                log.info("handshake body: %s", body.decode("utf-8", "replace")[:3000])
            ms = (time.perf_counter() - started) * 1000
            log.info(
                "%s %s %d %.0fms method=%s tool=%s client=%s proto=%s session=%s from=%s ua=%s",
                scope["method"],
                scope["path"],
                status,
                ms,
                info.get("method"),
                info.get("tool"),
                info.get("client"),
                info.get("proto") or headers.get("mcp-protocol-version"),
                "yes" if headers.get("mcp-session-id") else "no",
                headers.get("cf-connecting-ip") or headers.get("x-forwarded-for") or (scope.get("client") or ("?",))[0],
                headers.get("user-agent", "")[:60],
            )
