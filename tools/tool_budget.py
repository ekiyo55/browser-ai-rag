"""サーバーごとの道具の一覧が、AI にとって何トークンになるかを数える（第29章）。

    uv run python tools/tool_budget.py

AI のアプリは、つないだサーバーの道具の一覧（名前・題名・説明・引数の形・答えの形・注釈）を、
会話のたびにモデルへ渡す。その分量を、tools/list の答えの JSON をそのまま数えて見積もる。
数え方は o200k_base（第4章と同じ）。モデルによって数え方は違うので、目安として使う。
"""

import asyncio
import json
import sys
from pathlib import Path

import tiktoken
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from browser_ai_rag import combined_server as cs  # noqa: E402

ENC = tiktoken.get_encoding("o200k_base")


async def measure(tools_server) -> tuple[int, int, int]:
    async with Client(tools_server) as c:
        tools = (await c.list_tools()).tools
    full = json.dumps([t.model_dump(exclude_none=True) for t in tools], ensure_ascii=False)
    lean = json.dumps([t.model_dump(exclude_none=True, exclude={"output_schema"}) for t in tools], ensure_ascii=False)
    return len(tools), len(ENC.encode(full)), len(ENC.encode(lean))


async def main() -> None:
    from mcp.server.mcpserver import MCPServer

    rows = []
    for label, module in cs.PARTS:
        m = MCPServer(name=label, instructions=module.INSTRUCTIONS)
        for fn, title, ann in cs.tools_of(module):
            m.tool(title=title, annotations=ann)(fn)
        n, full, lean = await measure(m)
        rows.append((label, n, full, lean, len(ENC.encode(module.INSTRUCTIONS))))
    print(f"{'サーバー':<10} {'道具':>4} {'一覧のトークン':>10} {'答えの形を除く':>10} {'説明文':>6}")
    for label, n, full, lean, ins in rows:
        print(f"{label:<10} {n:>4} {full:>10} {lean:>10} {ins:>6}")
    print(f"{'合計':<10} {sum(r[1] for r in rows):>4} {sum(r[2] for r in rows):>10} {sum(r[3] for r in rows):>10} "
          f"{sum(r[4] for r in rows):>6}")


if __name__ == "__main__":
    asyncio.run(main())
