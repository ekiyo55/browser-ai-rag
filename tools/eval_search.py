"""検索の当たり方を測る（第13章の表を再現する）。

    uv run python tools/eval_search.py

data/docs を一時フォルダに取り込み、tools/eval_questions.json の質問について、
全文検索だけ（alpha=1）・埋め込みだけ（alpha=0）・混ぜたもの（alpha=0.3 など）の当たり方を表にする。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from browser_ai_rag.config import Settings  # noqa: E402
from browser_ai_rag.embed import Embedder  # noqa: E402
from browser_ai_rag.ingest import build  # noqa: E402
from browser_ai_rag.store import Store  # noqa: E402


def main() -> None:
    questions = json.loads((ROOT / "tools" / "eval_questions.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(ROOT / "data" / "docs", Path(tmp) / "docs")
        settings = Settings(data_dir=Path(tmp))
        store, emb = Store(settings.db_path), Embedder()
        build(settings, store, emb)
        vecs = {q["q"]: emb.query(q["q"]) for q in questions}
        print(f"{'方式':<22}{'種類':<10}{'1位':>6}{'3位以内':>8}")
        for label, alpha in (("全文検索だけ", 1.0), ("埋め込みだけ", 0.0), ("混ぜる 0.3", 0.3), ("混ぜる 0.5", 0.5)):
            for kind in ("言い換え", "番号・名前"):
                qs = [q for q in questions if q["type"] == kind]
                h1 = h3 = 0
                t = time.perf_counter()
                for q in qs:
                    hits = store.search(q["q"], vecs[q["q"]], limit=3, alpha=alpha)
                    ok = [bool(re.search(q["doc"], h.title) and re.search(q["heading"], h.heading)) for h in hits]
                    h1 += bool(ok and ok[0])
                    h3 += any(ok)
                print(f"{label:<22}{kind:<10}{h1:>3}/{len(qs):<3}{h3:>4}/{len(qs)}")
        store.db.close()


if __name__ == "__main__":
    main()
