# 章とリポジトリの対応

章が終わるたびに `chNN` のタグを打ちます。途中の章から始めたいときは、その前の章のタグを checkout してください。

```bash
git checkout ch05
```

| 章 | タグ | このリポジトリでやること | 状態 |
|---|---|---|---|
| 5 Hello World | `ch05` | MCPServer と Streamable HTTP。`hello` と `whoami` | できた |
| 6 トンネル | `ch06` | cloudflared / ngrok、`RAG_PUBLIC_HOSTS`（421 の落とし穴） | 設定だけ入っている |
| 7 Claude と ChatGPT につなぐ | `ch07` | `whoami` で両方から呼ばれていることを確かめる | |
| 8 そのほかの AI | `ch08` | 対応表の更新（docs/connect.md） | |
| 9 Inspector とログ | `ch09` | リクエストのログ、クライアントごとの違いの見分け方 | |
| 10 型ヒントとツール定義 | `ch10` | 引数の説明と型 | |
| 12 読み込みとチャンク分割 | `ch12` | `data/docs/` の Markdown を読む | |
| 13 埋め込みとベクトルDB | `ch13` | Chroma（のちに pgvector） | |
| 14 ハイブリッド検索 | `ch14` | BM25＋ベクトル、リランキング | |
| 15〜19 RAG MCP サーバー | `ch15`〜`ch19` | search_knowledge / read_document / save_note / フィルタ | |
| 20 認証 | `ch20` | OAuth 2.1・動的クライアント登録。特定のクライアントを決め打ちしない | |
| 21 デプロイ | `ch21` | Azure Container Apps、VPS | |
| 22 テストと評価 | `ch22` | 複数クライアントでの回帰テスト | |
| 23〜27 チームで使う | `ch23`〜`ch27` | SharePoint、組織プラン、Entra ID | |

第1〜4章と第11章は考え方の章なので、コードはありません。
