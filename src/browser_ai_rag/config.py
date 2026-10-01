"""設定は環境変数（または .env）から読む。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    public_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(Path.cwd() / ".env")
        hosts = [h.strip() for h in os.environ.get("RAG_PUBLIC_HOSTS", "").split(",") if h.strip()]
        return cls(
            host=os.environ.get("RAG_HOST", "127.0.0.1"),
            port=int(os.environ.get("RAG_PORT", "8000")),
            public_hosts=hosts,
        )
