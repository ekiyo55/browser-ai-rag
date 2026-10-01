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
    data_dir: Path = Path("data")
    hybrid_alpha: float = 0.3  # 全文検索の点数の重み（残りが埋め込み）。第13章の実測で決めた値
    base_url: str = ""  # 公開する URL（例 https://book.mooma.style）。空ならログインなし（手元での開発用）

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index" / "rag.db"

    @property
    def auth_db_path(self) -> Path:
        return self.data_dir / "auth.db"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(Path.cwd() / ".env")
        hosts = [h.strip() for h in os.environ.get("RAG_PUBLIC_HOSTS", "").split(",") if h.strip()]
        return cls(
            host=os.environ.get("RAG_HOST", "127.0.0.1"),
            port=int(os.environ.get("RAG_PORT", "8000")),
            public_hosts=hosts,
            data_dir=Path(os.environ.get("RAG_DATA_DIR", "data")),
            hybrid_alpha=float(os.environ.get("RAG_HYBRID_ALPHA", "0.3")),
            base_url=os.environ.get("RAG_BASE_URL", "").rstrip("/"),
        )
