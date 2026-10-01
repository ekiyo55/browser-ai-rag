# 章とリポジトリの対応

章が終わるたびに `chNN` のタグを打ちます。途中の章から始めたいときは、その前の章のタグを checkout してください。

```bash
git checkout ch05
```

第3部までは `src/browser_ai_rag/` の一本のサーバーを育てます。第4部からは仕事ごとにサーバーを分け、`servers/` の下に並べていく予定です（文書検索・メール・タイムカード…）。

| 章 | タグ | このリポジトリでやること | 状態 |
|---|---|---|---|
| 5 Hello World | `ch05` | MCPServer と Streamable HTTP。`hello` と `whoami` | できた |
| 6 トンネル | `ch06` | cloudflared / ngrok、`RAG_PUBLIC_HOSTS`（421 の落とし穴） | 設定だけ入っている |
| 7 Claude と ChatGPT につなぐ | `ch07` | `whoami` で両方から呼ばれていることを確かめる | |
| 8 そのほかの AI | `ch08` | 対応表の更新（docs/connect.md） | |
| 9 Inspector とログ | `ch09` | リクエストのログ（`reqlog.py`）、クライアントごとの違いの見分け方 | ログはできた |
| 10 型ヒントとツール定義 | `ch10` | 引数の説明と型 | |
| 12 読み込みとチャンク分割 | `ch16` | `readers.py`・`ingest.py`（KotobaCore で切り分け、変わったものだけ取り込む） | できた |
| 13 埋め込みと検索 | `ch16` | `embed.py`（e5-small）・`store.py`（FTS5＋ベクトル、点数合成）・`tools/eval_search.py` | できた |
| 14〜16 文書検索の道具 | `ch16` | 道具5つ（search_knowledge / read_document / list_documents / save_note / delete_note） | できた |
| 17 認証 | `ch17` | `auth.py`：OAuth 2.1・動的クライアント登録・利用者ごとのログイン。`RAG_BASE_URL` を設定したときだけ有効 | できた |
| 18 公開 | `ch18` | Azure Container Apps、VPS | |
| 19 テストと評価 | `ch19` | 複数の AI で同じ質問を流す回帰テスト | |
| 20〜22 メール | `ch20`〜`ch22` | `servers/mail`：受信の確認、返信（下書きと送信を分ける）、プロンプトインジェクション対策 | |
| 23〜25 タイムカード | `ch23`〜`ch25` | `servers/timecard`：打刻・照会・月の集計、本人の確定、既存 SaaS を包む | |
| 26 数える道具 | `ch26` | 集計をデータベースから正確に返す | |
| 27 予定とタスク | `ch27` | カレンダーとタスク管理 | |
| 28 運用の窓口 | `ch28` | サーバーの稼働状況を答える | |
| 29 サーバーを並べる | `ch29` | 認証の共通化（`common/`）、道具の数の予算 | |
| 30〜33 チームで使う | `ch30`〜`ch33` | SharePoint、組織プラン、Entra ID、運用と安全 | |

第1〜4章と第11章は考え方の章なので、コードはありません。第12〜16章は一つの作業としてまとめて作ったので、タグは `ch16` だけです。
