# browser-ai-rag

『ブラウザのAIに社内文書を読ませる ― MCPで作るRAGサーバー入門』（江藤 潔）のサンプルです。

claude.ai や chatgpt.com のような、ブラウザで使う AI から呼ばれるリモート MCP サーバーを作ります。本の章が進むごとにこのリポジトリも育っていき、最後は社内文書を検索する RAG サーバーになります。特定の AI サービスには寄せていないので、リモート MCP に対応したサービスならどれからでもつながります。

> いまは第5章（Hello World）の段階です。章ごとの対応は [docs/chapters.md](docs/chapters.md) を見てください。

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

## ライセンス

Apache License 2.0
