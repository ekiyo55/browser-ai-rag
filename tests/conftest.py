"""テストの共通部品。

ふだんのテストは、本物の埋め込みモデルの代わりに、文字の並びから作る軽い偽物を使う。
本物のモデルで検索の当たり方を確かめるテストは、RAG_TEST_REAL=1 のときだけ走る。
"""

import hashlib
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from browser_ai_rag.config import Settings
from browser_ai_rag.embed import DIM, Embedder
from browser_ai_rag.ingest import build
from browser_ai_rag.store import Store, normalize

REPO = Path(__file__).resolve().parents[1]


class FakeEmbedder(Embedder):
    """文字の2文字ずつの並びを、決まった次元に散らしただけのベクトル。速くて結果が毎回同じ。"""

    def __init__(self) -> None:
        super().__init__()

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        t = normalize(text)
        for i in range(len(t) - 1):
            h = int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        return v / max(np.linalg.norm(v), 1e-12)

    def passages(self, texts):
        return np.vstack([self._vec(t) for t in texts])

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def data_dir(tmp_path):
    shutil.copytree(REPO / "data" / "docs", tmp_path / "docs")
    return tmp_path


@pytest.fixture
def real_model():
    if not os.environ.get("RAG_TEST_REAL"):
        pytest.skip("本物の埋め込みモデルでのテストは RAG_TEST_REAL=1 のときだけ走らせる")
    return Embedder()


def make(data_dir: Path, embedder: Embedder):
    settings = Settings(data_dir=data_dir)
    store = Store(settings.db_path)
    build(settings, store, embedder)
    return settings, store
