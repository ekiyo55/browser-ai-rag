"""取り込みと読み込みの確かめ。"""

from browser_ai_rag.config import Settings
from browser_ai_rag.ingest import build, split
from browser_ai_rag.store import Store

from conftest import FakeEmbedder


def test_split_keeps_article_heading():
    chunks = split("# 規程\n\n## 第5条（交通機関）\n\n新幹線は普通車指定席を原則とする。\n")
    assert chunks[-1][0] == "第5条（交通機関）"
    assert "普通車指定席" in chunks[-1][1]


def test_unchanged_files_are_skipped_and_removed_files_are_dropped(data_dir):
    settings = Settings(data_dir=data_dir)
    store, emb = Store(settings.db_path), FakeEmbedder()
    n = len([p for p in (data_dir / "docs").iterdir() if p.name != "README.md"])
    first = build(settings, store, emb)
    assert first["updated"] == n
    assert build(settings, store, emb) == {"updated": 0, "unchanged": n, "removed": 0}
    (data_dir / "docs" / "情報セキュリティ規程.md").unlink()
    assert build(settings, store, emb)["removed"] == 1
    assert len(store.documents()) == n - 1


def test_pdf_pages_and_headings(data_dir):
    from browser_ai_rag.readers import read_pages
    pages = read_pages(data_dir / "docs" / "社内システム利用ガイド.pdf")
    assert [p for p, _ in pages] == [1, 2, 3, 4]
    assert pages[0][1].startswith("# 社内システム利用ガイド")
    assert "## 4. パスワードを忘れたとき" in pages[3][1]
    assert "生成AI" in pages[2][1]           # 「生成 AI」の空白が取れている
    assert "（内線2400、平日9時〜18時）に連絡する。" in pages[3][1]  # 折り返しがつながっている


def test_docx_table_stays_together(data_dir):
    from browser_ai_rag.readers import read_pages
    text = read_pages(data_dir / "docs" / "慶弔見舞金規程.docx")[0][1]
    assert "| 事由 | 金額 |\n|---|---|\n| 本人の結婚 | 30,000円 |" in text


def test_fullwidth_digits_match(data_dir):
    settings = Settings(data_dir=data_dir)
    store, emb = Store(settings.db_path), FakeEmbedder()
    build(settings, store, emb)
    hits = store.search("１３，０００円", emb.query("１３，０００円"), limit=1, alpha=1.0)
    assert "第6条" in hits[0].heading
