"""本物の埋め込みモデルで、言い換えと番号の質問がどれだけ当たるかを確かめる（第13章）。

    RAG_TEST_REAL=1 uv run pytest tests/test_search_quality.py -s
"""

import json
from pathlib import Path

from conftest import make

QUESTIONS = json.loads((Path(__file__).parents[1] / "tools" / "eval_questions.json").read_text(encoding="utf-8"))


def _hit_at(store, emb, q, alpha, k):
    import re
    hits = store.search(q["q"], emb.query(q["q"]), limit=k, alpha=alpha)
    return any(re.search(q["doc"], h.title) and re.search(q["heading"], h.heading) for h in hits)


def test_hybrid_beats_each_side(data_dir, real_model):
    _, store = make(data_dir, real_model)
    score = {a: sum(_hit_at(store, real_model, q, a, 3) for q in QUESTIONS) for a in (0.0, 0.3, 1.0)}
    print(score)
    assert score[0.3] >= score[0.0] and score[0.3] > score[1.0]
