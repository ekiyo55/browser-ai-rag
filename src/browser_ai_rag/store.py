"""検索の保存先（第12〜13章）。SQLite 一つに、文書・断片・全文検索・ベクトルを入れる。

検索は「全文検索（FTS5 の trigram）」と「ベクトル（コサイン類似度）」の点数を、
それぞれ 0〜1 にならしてから alpha : (1 - alpha) で足し合わせる。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,     -- data/ からの相対パス
  title TEXT NOT NULL,
  kind TEXT NOT NULL,            -- doc（data/docs）か note（save_note で書いたもの）
  owner TEXT,                    -- メモを書いた利用者（ログインありのとき）
  sha256 TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,          -- 文書の中での順番
  heading TEXT NOT NULL,         -- 見出しの道筋（「第5条（交通機関）」など）
  page INTEGER,                  -- PDF のページ。ページのない形式は NULL
  text TEXT NOT NULL,
  embedding BLOB NOT NULL        -- float32 のベクトル
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(heading, text, tokenize='trigram');
"""


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    title: str
    heading: str
    page: int | None
    text: str
    score: float


def normalize(s: str) -> str:
    """全角・半角のゆれをそろえる（「１３，０００円」と「13,000円」を同じにする）。"""
    return unicodedata.normalize("NFKC", s)


def _minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    return (x - lo) / (hi - lo) if hi > lo else np.zeros_like(x)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "owner" not in [r[1] for r in self.db.execute("PRAGMA table_info(documents)")]:
            self.db.execute("ALTER TABLE documents ADD COLUMN owner TEXT")  # 第16章までの保存先を引き継ぐ
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None  # ベクトルをまとめた行列（変更があれば作り直す）
        self._ids: list[int] = []

    # ---------------------------------------------------------------- 書き込み

    def document_sha(self, rel_path: str) -> str | None:
        row = self.db.execute("SELECT sha256 FROM documents WHERE path=?", (rel_path,)).fetchone()
        return row["sha256"] if row else None

    def owner_of(self, rel_path: str) -> str | None:
        row = self.db.execute("SELECT owner FROM documents WHERE path=?", (rel_path,)).fetchone()
        return row["owner"] if row else None

    def upsert_document(self, rel_path: str, title: str, kind: str, sha: str,
                        chunks: list[tuple[str, int | None, str]], vectors: np.ndarray,
                        owner: str | None = None) -> int:
        with self._lock, self.db:
            old = self.db.execute("SELECT id FROM documents WHERE path=?", (rel_path,)).fetchone()
            if old:
                self._delete(old["id"])
            cur = self.db.execute(
                "INSERT INTO documents(path, title, kind, owner, sha256, updated_at) VALUES(?,?,?,?,?,?)",
                (rel_path, title, kind, owner, sha, int(time.time())),
            )
            doc_id = cur.lastrowid
            for ord_, ((heading, page, text), vec) in enumerate(zip(chunks, vectors)):
                c = self.db.execute(
                    "INSERT INTO chunks(doc_id, ord, heading, page, text, embedding) VALUES(?,?,?,?,?,?)",
                    (doc_id, ord_, heading, page, text, vec.astype(np.float32).tobytes()),
                )
                self.db.execute("INSERT INTO chunks_fts(rowid, heading, text) VALUES(?,?,?)",
                                (c.lastrowid, normalize(heading), normalize(text)))
            self._matrix = None
            return doc_id

    def delete_document(self, doc_id: int) -> bool:
        with self._lock, self.db:
            found = self._delete(doc_id)
            self._matrix = None
            return found

    def _delete(self, doc_id: int) -> bool:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        self.db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        return self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount > 0

    # ---------------------------------------------------------------- 読み出し

    def documents(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT d.id, d.path, d.title, d.kind, d.owner, d.updated_at, COUNT(c.id) AS chunks "
            "FROM documents d LEFT JOIN chunks c ON c.doc_id=d.id GROUP BY d.id ORDER BY d.kind, d.title"
        ).fetchall()

    def document(self, doc_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def chunks_of(self, doc_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT id, heading, page, text FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,)
        ).fetchall()

    # ---------------------------------------------------------------- 検索

    def _vectors(self) -> tuple[np.ndarray, list[int]]:
        with self._lock:
            if self._matrix is None:
                rows = self.db.execute("SELECT id, embedding FROM chunks ORDER BY id").fetchall()
                self._ids = [r["id"] for r in rows]
                self._matrix = (np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                                if rows else np.zeros((0, 1), dtype=np.float32))
            return self._matrix, self._ids

    def _lexical(self, query: str, ids: list[int]) -> np.ndarray:
        """FTS5 trigram の bm25 を、ベクトルと同じ並び（ids の順）の配列にして返す。"""
        q = normalize(query)
        grams = {q[i:i + 3] for i in range(len(q) - 2)}
        grams = [g for g in grams if not re.search(r"[\s、。？！?!「」]", g)]
        scores = np.zeros(len(ids), dtype=np.float32)
        if not grams:
            return scores  # 2文字以下の語だけの検索は、全文検索に引っかからない
        expr = " OR ".join('"' + g.replace('"', '""') + '"' for g in grams)
        pos = {cid: i for i, cid in enumerate(ids)}
        for rowid, bm in self.db.execute("SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?", (expr,)):
            if rowid in pos:
                scores[pos[rowid]] = -bm  # bm25() は小さいほど良いので符号を返す
        return scores

    def search(self, query: str, query_vec: np.ndarray, limit: int = 5, alpha: float = 0.3,
               doc_id: int | None = None) -> list[Hit]:
        matrix, ids = self._vectors()
        if not ids:
            return []
        dense = matrix @ query_vec
        lexical = self._lexical(query, ids)
        score = alpha * _minmax(lexical) + (1 - alpha) * _minmax(dense)
        order = np.argsort(score)[::-1]
        hits: list[Hit] = []
        for i in order:
            row = self.db.execute(
                "SELECT c.id, c.doc_id, d.title, c.heading, c.page, c.text FROM chunks c "
                "JOIN documents d ON d.id=c.doc_id WHERE c.id=?", (ids[i],)
            ).fetchone()
            if doc_id is not None and row["doc_id"] != doc_id:
                continue
            hits.append(Hit(row["id"], row["doc_id"], row["title"], row["heading"], row["page"], row["text"],
                            round(float(score[i]), 3)))
            if len(hits) >= limit:
                break
        return hits
