"""`python -m browser_ai_rag` で起動する。"""

import logging
import time

import uvicorn

from .config import Settings
from .embed import Embedder
from .ingest import build
from .server import build_app
from .store import Store


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings = Settings.from_env()
    store, embedder = Store(settings.db_path), Embedder()

    # 起動のときに、変わった文書だけ取り込み直し、埋め込みモデルも読み込んでおく。
    # こうしないと、最初の検索だけモデルの読み込みで2秒ほど待たされる（第15章）。
    t = time.perf_counter()
    stats = build(settings, store, embedder)
    embedder.query("準備")
    docs = store.documents()
    print(f"文書 {len(docs)} 件（更新 {stats['updated']}・削除 {stats['removed']}）、準備 {time.perf_counter() - t:.1f}秒")

    print(f"MCP エンドポイント: http://{settings.host}:{settings.port}/mcp")
    if settings.public_hosts:
        print("公開ホスト名として受け付ける:", ", ".join(settings.public_hosts))
    uvicorn.run(build_app(settings, store, embedder), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
