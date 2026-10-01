"""文書を取り込んで、検索できるようにする（第12章）。

    uv run python -m browser_ai_rag.ingest

data/docs と data/notes の下のファイルを読み、中身が変わったものだけを切り分け直して保存する。
消えたファイルは、検索の保存先からも消す。
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

from .config import Settings
from .embed import Embedder
from .readers import SUPPORTED, read_pages
from .store import Store

_analyzer = None


def split(text: str) -> list[tuple[str, str]]:
    """KotobaCore で、見出しを引き継いだ断片に切る。(見出しの道筋, 本文) の並びを返す。"""
    global _analyzer
    if _analyzer is None:
        from kotobacore import Analyzer

        _analyzer = Analyzer()
    out = []
    for c in _analyzer.chunk(text):
        heading = " > ".join(c.heading_path[1:]) or (c.heading_path[0] if c.heading_path else "")
        out.append((heading, c.text.strip()))
    return [(h, t) for h, t in out if t]


def title_of(path: Path, pages: list[tuple[int | None, str]]) -> str:
    for _, text in pages:
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return path.stem


def ingest_file(store: Store, embedder: Embedder, base: Path, path: Path, kind: str,
                owner: str | None = None) -> int | None:
    rel = path.relative_to(base).as_posix()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if store.document_sha(rel) == sha:
        return None  # 変わっていない
    pages = read_pages(path)
    chunks = [(heading, page, text) for page, page_text in pages for heading, text in split(page_text)]
    if not chunks:
        return None
    vectors = embedder.passages([f"{h}\n{t}" for h, _, t in chunks])
    if owner is None and kind == "note":
        owner = store.owner_of(rel)  # 取り込み直しても書いた人を引き継ぐ
    return store.upsert_document(rel, title_of(path, pages), kind, sha, chunks, vectors, owner)


def build(settings: Settings, store: Store | None = None, embedder: Embedder | None = None) -> dict:
    store = store or Store(settings.db_path)
    embedder = embedder or Embedder()
    base = settings.data_dir
    seen: set[str] = set()
    stats = {"updated": 0, "unchanged": 0, "removed": 0}
    for folder, kind in ((settings.docs_dir, "doc"), (settings.notes_dir, "note")):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if path.suffix.lower() not in SUPPORTED or path.name == "README.md" or path.name.startswith("~$"):
                continue
            seen.add(path.relative_to(base).as_posix())
            stats["updated" if ingest_file(store, embedder, base, path, kind) else "unchanged"] += 1
    for row in store.documents():
        if row["path"] not in seen:
            store.delete_document(row["id"])
            stats["removed"] += 1
    return stats


def main() -> None:
    settings = Settings.from_env()
    t = time.perf_counter()
    stats = build(settings)
    store = Store(settings.db_path)
    docs = store.documents()
    print(f"更新 {stats['updated']} / 変更なし {stats['unchanged']} / 削除 {stats['removed']}"
          f"（{len(docs)} 文書・{sum(d['chunks'] for d in docs)} 断片、{time.perf_counter() - t:.1f}秒）")
    for d in docs:
        print(f"  [{d['id']}] {d['title']}（{d['kind']}・{d['chunks']} 断片）")


if __name__ == "__main__":
    sys.exit(main())
