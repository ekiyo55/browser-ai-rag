"""文章をベクトルにする（第13章）。

使うのは multilingual-e5-small を ONNX で動かしたもの。torch が要らず、メモリは 0.7GB ほど。
e5 は、検索語には "query: "、文書には "passage: " を頭に付けて使う決まりになっている。
"""

from __future__ import annotations

import numpy as np

MODEL = "intfloat/multilingual-e5-small"
DIM = 384


class Embedder:
    def __init__(self, model: str = MODEL) -> None:
        self.model_name = model
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType

            if self.model_name == MODEL:
                try:
                    TextEmbedding.add_custom_model(
                        model=MODEL, pooling=PoolingType.MEAN, normalization=True,
                        sources=ModelSource(hf=MODEL), dim=DIM, model_file="onnx/model.onnx",
                    )
                except ValueError:
                    pass  # 登録済み
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def passages(self, texts: list[str]) -> np.ndarray:
        return self._norm(np.array(list(self._load().embed(["passage: " + t for t in texts])), dtype=np.float32))

    def query(self, text: str) -> np.ndarray:
        return self._norm(np.array(list(self._load().embed(["query: " + text])), dtype=np.float32))[0]

    @staticmethod
    def _norm(m: np.ndarray) -> np.ndarray:
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
