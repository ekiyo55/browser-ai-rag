"""`python -m browser_ai_rag` で起動する。"""

import logging

import uvicorn

from .config import Settings
from .server import build_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings = Settings.from_env()
    print(f"MCP エンドポイント: http://{settings.host}:{settings.port}/mcp")
    if settings.public_hosts:
        print("公開ホスト名として受け付ける:", ", ".join(settings.public_hosts))
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
