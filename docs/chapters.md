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
| 20〜22 メール | `ch22` | `mailbox.py`（IMAP/SMTP とフォルダの偽物）・`mail_server.py`（道具7つ、`/mail/mcp`、ログインは共通）。下書き→送信の二段階、宛先の許可リスト、送信は宛先と件名の一致が必要 | できた |
| 23〜24 タイムカード | `ch24` | `timecard_server.py`（道具7つ、`/timecard/mcp`、ログインは共通）。打刻する人はログインで、時刻はサーバーの時計で決める。過去の修正は申請→管理者（本人以外）が承認、古い打刻は消さずに印 | できた |
| 25 既存の勤怠 SaaS を包む | — | 解説のみ（サンプルなし）。鍵の型・時刻の口・申請の作り直し禁止・回数制限・社員番号の対応表 | 解説のみ |
| 26 数える道具 | `ch26` | `count_server.py`（`/count/mcp`）・`sales_data.py`（架空の売上と案件）。決まった道具4つ（COUNT_MODE=tools）と、比べるための SQL を書かせる実験（COUNT_MODE=sql、読むだけ）。記録のない期間は0円と区別 | できた |
| 27 予定とタスク | `ch27` | `schedule_server.py`（`/schedule/mcp`、道具8つ）。曜日つきの日時と曜日の照合、重なりの確認、ほかの人は空きだけ、動かすのは主催者だけ | できた |
| 28 運用の窓口 | `ch28` | `ops_server.py`（`/ops/mcp`、道具5つ）・`deploy/book-ops`（root で動く手伝い、sudoers で www-data に許す）。稼働状況と具合は誰でも、エラーと再起動は管理者だけ、再起動は10分に1回・理由を記録、ログは IP と合鍵を伏せる | できた |
| 29 サーバーを並べる | `ch29` | 認証の共通化（`common/`）、道具の数の予算 | |
| 30〜33 チームで使う | `ch30`〜`ch33` | SharePoint、組織プラン、Entra ID、運用と安全 | |

第1〜4章と第11章は考え方の章なので、コードはありません。第12〜16章は一つの作業としてまとめて作ったので、タグは `ch16` だけです。
