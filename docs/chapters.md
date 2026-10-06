# 章とリポジトリの対応

区切りの章が終わるたびに `chNN` のタグを打ってあります。タグは、その章を終えた時点のコードです。途中の章から始めたいときは、その前の章のタグを checkout してください。

```bash
git checkout ch05
```

サーバーは仕事ごとに分け、`src/browser_ai_rag/` の中にモジュールとして並べています。ログインの仕組み（`auth.py`）だけを共通にしています。

| 章 | タグ | このリポジトリでやること |
|---|---|---|
| 5 Hello World | `ch05` | MCPServer と Streamable HTTP。`hello` と `whoami`、リクエストの記録（`reqlog.py`） |
| 6〜10 トンネル・AI につなぐ・ログ・ツール定義 | （`ch05` のまま） | `RAG_PUBLIC_HOSTS`（421 の落とし穴）、`RAG_LOG_HANDSHAKE`。コードの変更は小さいのでタグはない |
| 12〜16 文書検索 | `ch16` | `readers.py`・`ingest.py`（KotobaCore で切り分け、変わったものだけ取り込む）・`embed.py`（e5-small）・`store.py`（FTS5＋ベクトル、点数合成）・道具5つ・`tools/eval_search.py` |
| 17 認証 | `ch17` | `auth.py`：OAuth 2.1・動的クライアント登録・利用者ごとのログイン。`RAG_BASE_URL` を設定したときだけ有効 |
| 18〜19 公開・評価 | （`ch17` のまま） | VPS への配置（本文の手順）と、複数の AI での評価 |
| 20〜22 メール | `ch22` | `mailbox.py`（IMAP/SMTP とフォルダの偽物）・`mail_server.py`（道具7つ、`/mail/mcp`）。下書き→送信の二段階、宛先の許可リスト、送信は宛先と件名の一致が必要、宛先はメールアドレスに限る |
| 23〜24 タイムカード | `ch24` | `timecard_server.py`（道具7つ、`/timecard/mcp`）。打刻する人はログインで、時刻はサーバーの時計で決める。修正は申請→本人以外の管理者が承認、古い打刻は消さずに印。管理用のアカウントは打刻しない。`auth` の `sessions` と `logout` |
| 25 既存の勤怠サービスを包む | — | 解説のみ（サンプルなし） |
| 26 数える道具 | `ch26` | `count_server.py`（`/count/mcp`）・`sales_data.py`（架空の売上と案件）。決まった道具4つ（`COUNT_MODE=tools`）と、比べるための SQL を書かせる実験（`COUNT_MODE=sql`、読むだけ）。記録のない期間は0円と区別 |
| 27 予定とタスク | `ch27` | `schedule_server.py`（`/schedule/mcp`、道具9つ）。曜日つきの日時と曜日の照合、重なりの確認、ほかの人は空きだけ、動かすのは主催者だけ、「今日の予定とタスク」 |
| 28 運用の窓口 | `ch28` | `ops_server.py`（`/ops/mcp`、道具5つ）・`deploy/book-ops`（root で動く手伝い、sudoers で www-data に許す）。稼働状況と具合は誰でも、エラーと再起動は管理者だけ、再起動は10分に1回・理由を記録、ログは IP と合鍵を伏せる、止まった時刻と止まり方を返す |
| 29 サーバーを並べる | `ch29` | `combined_server.py`（`/all/mcp`、六本の道具37個を一本に。名前がぶつかったら止める）・`tools/tool_budget.py`（道具の一覧のトークン数） |
| 30 社内で使い始める前に | — | 解説のみ |

第1〜4章と第11章は考え方の章なので、コードはありません。
