# browser-ai-rag

『ブラウザのAIが社内で働きだす ― MCPで作る文書検索・メール・勤怠』（江藤 潔）のサンプルです。

claude.ai や chatgpt.com のような、ブラウザで使う AI から呼ばれるリモート MCP サーバーを作ります。本の章が進むごとにこのリポジトリも育っていき、社内文書の検索、メールの確認と返信、タイムカードなど、いくつもの MCP サーバーがそろいます。特定の AI サービスには寄せていないので、リモート MCP に対応したサービスならどれからでもつながります。

本の全30章ぶんがそろっています。章ごとの対応とタグは [docs/chapters.md](docs/chapters.md) を見てください。

| サーバー | 起動 | URL | 章 |
|---|---|---|---|
| 社内文書検索（ログインもここ） | `python -m browser_ai_rag` | `/mcp` | 第3部 |
| メール | `python -m browser_ai_rag.mail_server` | `/mail/mcp` | 第20〜22章 |
| タイムカード | `python -m browser_ai_rag.timecard_server` | `/timecard/mcp` | 第23〜24章 |
| 売上・案件 | `python -m browser_ai_rag.count_server` | `/count/mcp` | 第26章 |
| 予定とタスク | `python -m browser_ai_rag.schedule_server` | `/schedule/mcp` | 第27章 |
| 運用の窓口 | `python -m browser_ai_rag.ops_server` | `/ops/mcp` | 第28章 |
| 六本をまとめた一本（実験） | `python -m browser_ai_rag.combined_server` | `/all/mcp` | 第29章 |

## 動かしてみる

Python 3.11 以上と [uv](https://docs.astral.sh/uv/) を使います。

```bash
uv sync
uv run python -m browser_ai_rag
```

`http://127.0.0.1:8000/mcp` で待ち受けます。テストは `uv run pytest` で走ります。

## ブラウザの AI からつなぐ

ブラウザの AI は、あなたのパソコンに直接つなぎに来るわけではありません。AI の会社のクラウドが、インターネット越しにこのサーバーを呼びに来ます。なので、手元で動かしているうちはトンネルで外から届くようにします。

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

表示された `https://xxxx.trycloudflare.com` のホスト名を `.env` に書いて、サーバーを起動し直します。

```
RAG_PUBLIC_HOSTS=xxxx.trycloudflare.com
```

これを忘れると、AI 側からは「つながらない」とだけ出て、サーバーのログには 421（Invalid Host header）が残ります。いちばんよく踏む落とし穴です。

あとは `https://xxxx.trycloudflare.com/mcp` を、使っている AI のコネクタ設定に登録します。サービスごとの手順は [docs/connect.md](docs/connect.md) にまとめています。登録できたら、AI に「whoami ツールを使って」と頼んでみてください。Claude からなら Claude の、ChatGPT からなら ChatGPT のクライアント名が返ってきます。同じサーバーに、別々の AI がつながっているのがわかります。

## 設定

`.env.example` をコピーして `.env` を作ります。

| 変数 | 既定値 | 意味 |
|---|---|---|
| `RAG_HOST` | `127.0.0.1` | 待ち受けるアドレス |
| `RAG_PORT` | `8000` | 待ち受けるポート |
| `RAG_PUBLIC_HOSTS` | なし | 外から呼ばれるときのホスト名（カンマ区切り） |
| `RAG_LOG_HANDSHAKE` | なし | `1` で AI の自己紹介の本文をログに残す（道具の引数は残さない） |
| `RAG_BASE_URL` | なし | 公開する URL。設定するとログイン（OAuth）が有効になる |
| `RAG_DATA_DIR` | `data` | 文書・索引・各サーバーのデータベースの置き場所 |

メール・タイムカード・売上・予定・運用の窓口の設定は、`.env.example` に章ごとにまとめてあります。

## ライセンス

Apache License 2.0
