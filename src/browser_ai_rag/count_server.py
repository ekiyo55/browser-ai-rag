"""数える道具の MCP サーバー（第26章）。売上と案件の数字を、データベースから正確に返す。

    uv run python -m browser_ai_rag.count_server

URL は {RAG_BASE_URL}/count/mcp。ログインはほかのサーバーと共通。

AI に SQL を書かせない。AI が選ぶのは「何を・どの期間・何ごとに・どの条件で」だけで、
SQL はサーバーが、社内の数え方の決まりどおりに組み立てる。
比べるための実験として、COUNT_MODE=sql で「読むだけの SQL を AI に書かせる」道具に切り替えられる。
"""

from __future__ import annotations

import calendar
import difflib
import logging
import os
import sqlite3
import threading
import time
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .auth import AuthDB, current_user
from .config import Settings
from .mail_server import SharedTokenVerifier
from .reqlog import RequestLog
from .sales_data import build as build_sales_db

RULES = """社内の数え方の決まり：
- 売上は「計上日」で数える（受注日ではない）。金額は税抜。キャンセルした行は数えない。返品（マイナスの金額）は差し引く。
- 会計年度は4月始まり。上期は4〜9月、下期は10月〜翌年3月。
- 受注率は、期間内に結果が決まった案件のうちの受注の割合（受注 ÷（受注＋失注））。
- 受注金額は、期間内に受注した案件の金額の合計。"""

INSTRUCTIONS = f"""サンプル商事の売上と案件の数字を答えるサーバーです。
{RULES}
数字は必ず道具の答えから伝えてください。自分で足し算をし直したり、推測で補ったりしないこと。
顧客・担当者・商品の名前がはっきりしないときは、list_values で正式な名前を確かめてください。"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

SALES_RULE = "売上＝計上日が期間内・キャンセルを除く・返品を差し引いた税抜金額"
DEAL_RULE = "受注率＝期間内に受注または失注が決まった案件のうちの受注の割合。受注金額＝期間内に受注した案件の金額の合計"


class CountSettings(BaseModel):
    mode: Literal["tools", "sql"] = "tools"   # tools: 決まった道具（本番） / sql: AI に SQL を書かせる（比較の実験）
    port: int = 8703

    @classmethod
    def from_env(cls) -> CountSettings:
        return cls(mode=os.environ.get("COUNT_MODE", "tools"), port=int(os.environ.get("COUNT_PORT", "8703")))


class _State:
    settings: Settings
    cs: CountSettings
    db: sqlite3.Connection
    today = staticmethod(date.today)


state = _State()


def configure(settings: Settings, cs: CountSettings, today=None) -> None:
    state.settings, state.cs = settings, cs
    state.today = staticmethod(today or date.today)
    path = settings.data_dir / "sales" / "sales.db"
    if not path.exists():
        build_sales_db(path)
    # 読むだけで開く。道具の中から書き込むことはない
    state.db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    state.db.row_factory = sqlite3.Row


def _check_login() -> None:
    if state.settings.base_url and not current_user():
        raise ToolError("ログインしていないので、数字は見せられません。")


# ---------------------------------------------------------------- 条件の読み取り


def _period(date_from: str, date_to: str) -> tuple[str, str]:
    """「2026-09」は月の初日・末日として受け取る。"""
    def parse(s: str, end: bool) -> date:
        s = s.strip()
        try:
            if len(s) == 7:
                y, m = int(s[:4]), int(s[5:])
                return date(y, m, calendar.monthrange(y, m)[1] if end else 1)
            return date.fromisoformat(s)
        except ValueError:
            raise ToolError(f"日付は「2026-09-30」か「2026-09」の形で指定してください（受け取った値: {s}）。")
    a, b = parse(date_from, False), parse(date_to, True)
    if a > b:
        raise ToolError(f"期間の始め（{a}）が終わり（{b}）より後になっています。")
    return a.isoformat(), b.isoformat()


def _values(kind: str) -> list[str]:
    sql = {"owner": "SELECT DISTINCT owner FROM sales UNION SELECT DISTINCT owner FROM deals ORDER BY 1",
           "customer": "SELECT name FROM customers ORDER BY id",
           "product": "SELECT name FROM products ORDER BY id",
           "stage": "SELECT DISTINCT stage FROM deals ORDER BY 1"}[kind]
    return [r[0] for r in state.db.execute(sql)]


def _resolve_customer(text: str | None) -> int | None:
    """正式名称でも、ふだんの呼び名でも受け取る。決めきれなければ候補を返して断る（推測で選ばない）。"""
    if not text:
        return None
    t = text.strip()
    rows = state.db.execute("SELECT id, name, short_name FROM customers").fetchall()
    exact = [r for r in rows if t in (r["name"], r["short_name"])]
    if not exact:
        exact = [r for r in rows if t in r["name"]]
    if len(exact) == 1:
        return exact[0]["id"]
    names = [r["name"] for r in rows]
    cands = [r["name"] for r in exact] or difflib.get_close_matches(t, names, n=3, cutoff=0.3)
    raise ToolError(f"顧客「{t}」を一つに決められません。候補: {', '.join(cands) or 'なし'}。"
                    "list_values(kind='customer') で正式な名前を確かめてください。")


def _resolve(kind: str, text: str | None) -> str | None:
    if not text:
        return None
    vals = _values(kind)
    if text.strip() in vals:
        return text.strip()
    hit = [v for v in vals if text.strip() in v or v in text]
    if len(hit) == 1:
        return hit[0]
    label = {"owner": "担当者", "product": "商品", "stage": "段階"}[kind]
    raise ToolError(f"{label}「{text}」は見つかりません。使える値: {', '.join(vals)}")


def _yen(n: int) -> str:
    return f"{n:,}円"


# ---------------------------------------------------------------- 答えの形


class Row(BaseModel):
    key: str = Field(description="まとめた単位（月・担当者・顧客・商品）。まとめないときは「合計」")
    amount: int = Field(description="税抜金額（円）")
    amount_text: str
    count: int = Field(description="数えた行の数")


class SalesAnswer(BaseModel):
    rule: str = Field(description="この数字の数え方。利用者に数字を伝えるときは、必要に応じて添えること")
    conditions: dict[str, str] = Field(description="実際に数えた条件（名前は正式名称に直したもの）")
    rows: list[Row]
    total: int
    total_text: str
    as_of: str = Field(description="サーバーの今日の日付")


class DealAnswer(BaseModel):
    rule: str
    conditions: dict[str, str]
    won: int = Field(description="期間内に受注した件数")
    lost: int = Field(description="期間内に失注した件数")
    win_rate: str = Field(description="受注率（受注÷(受注＋失注)）。決まった案件がなければ「—」")
    won_amount: int
    won_amount_text: str
    by: list[dict] = Field(description="group_by を指定したときの内訳")
    open_deals: int = Field(description="いま進行中（見込み・提案）の案件の数（期間に関係なく、今日の時点）")
    open_amount_text: str
    as_of: str


class Deal(BaseModel):
    deal_id: int
    title: str
    customer: str
    owner: str
    stage: str
    amount_text: str
    expected_close: str
    closed_on: str | None


TOOLS: list[tuple] = []


def tool(title: str, mode: str):
    def mark(fn):
        TOOLS.append((fn, title, mode))
        return fn
    return mark


# ---------------------------------------------------------------- 決まった道具（本番）

GroupBy = Literal["none", "month", "owner", "customer", "product"]


@tool("売上を集計する", "tools")
def sales_summary(
    date_from: Annotated[str, Field(description="期間の始め。「2026-09-01」か「2026-09」")],
    date_to: Annotated[str, Field(description="期間の終わり（その日を含む）。「2026-09-30」か「2026-09」")],
    group_by: Annotated[GroupBy, Field(description="何ごとにまとめるか。none=合計だけ")] = "none",
    owner: Annotated[str | None, Field(description="担当者で絞る")] = None,
    customer: Annotated[str | None, Field(description="顧客で絞る（正式名称でも、ふだんの呼び名でもよい）")] = None,
    product: Annotated[str | None, Field(description="商品で絞る")] = None,
) -> SalesAnswer:
    """売上を、社内の決まり（計上日・税抜・キャンセル除外・返品差し引き）どおりに数える。SQL はサーバーが組み立てる。"""
    _check_login()
    a, b = _period(date_from, date_to)
    cid, own, prod = _resolve_customer(customer), _resolve("owner", owner), _resolve("product", product)
    key = {"none": "'合計'", "month": "substr(s.booked_on, 1, 7)", "owner": "s.owner",
           "customer": "c.name", "product": "p.name"}[group_by]
    where, args = ["s.booked_on BETWEEN ? AND ?", "s.status = '計上'"], [a, b]
    if cid:
        where.append("s.customer_id = ?"); args.append(cid)
    if own:
        where.append("s.owner = ?"); args.append(own)
    if prod:
        where.append("p.name = ?"); args.append(prod)
    sql = (f"SELECT {key} AS k, SUM(s.amount) AS amount, COUNT(*) AS n FROM sales s "
           "JOIN customers c ON c.id = s.customer_id JOIN products p ON p.id = s.product_id "
           f"WHERE {' AND '.join(where)} GROUP BY k ORDER BY {'k' if group_by == 'month' else 'amount DESC'}")
    rows = [Row(key=r["k"], amount=r["amount"], amount_text=_yen(r["amount"]), count=r["n"])
            for r in state.db.execute(sql, args)]
    total = sum(r.amount for r in rows)
    cond = {"期間": f"{a}〜{b}（計上日）", "まとめ方": group_by}
    if cid:
        cond["顧客"] = state.db.execute("SELECT name FROM customers WHERE id=?", (cid,)).fetchone()[0]
    if own:
        cond["担当者"] = own
    if prod:
        cond["商品"] = prod
    return SalesAnswer(rule=SALES_RULE, conditions=cond, rows=rows, total=total, total_text=_yen(total),
                       as_of=state.today().isoformat())


@tool("案件を集計する", "tools")
def deal_summary(
    date_from: Annotated[str, Field(description="期間の始め。「2026-04-01」か「2026-04」")],
    date_to: Annotated[str, Field(description="期間の終わり（その日を含む）")],
    group_by: Annotated[Literal["none", "owner", "customer"], Field(description="受注・失注の内訳を何ごとに出すか")] = "none",
    owner: Annotated[str | None, Field(description="担当者で絞る")] = None,
    customer: Annotated[str | None, Field(description="顧客で絞る")] = None,
) -> DealAnswer:
    """期間内に受注・失注が決まった案件を数え、受注率と受注金額を出す。あわせて、いま進行中の案件の数と金額も返す。"""
    _check_login()
    a, b = _period(date_from, date_to)
    cid, own = _resolve_customer(customer), _resolve("owner", owner)
    where, args = ["1=1"], []
    if cid:
        where.append("d.customer_id = ?"); args.append(cid)
    if own:
        where.append("d.owner = ?"); args.append(own)
    w = " AND ".join(where)
    key = {"none": "'合計'", "owner": "d.owner", "customer": "c.name"}[group_by]
    rows = state.db.execute(
        f"SELECT {key} AS k, SUM(d.stage='受注') AS won, SUM(d.stage='失注') AS lost, "
        "SUM(CASE WHEN d.stage='受注' THEN d.amount ELSE 0 END) AS won_amount "
        f"FROM deals d JOIN customers c ON c.id = d.customer_id WHERE {w} AND d.closed_on BETWEEN ? AND ? "
        "GROUP BY k ORDER BY won_amount DESC", (*args, a, b)).fetchall()
    rate = lambda won, lost: f"{won / (won + lost):.0%}" if won + lost else "—"  # noqa: E731
    won, lost = sum(r["won"] for r in rows), sum(r["lost"] for r in rows)
    won_amount = sum(r["won_amount"] for r in rows)
    op = state.db.execute(f"SELECT COUNT(*), COALESCE(SUM(d.amount), 0) FROM deals d WHERE {w} "
                          "AND d.stage IN ('見込み', '提案')", args).fetchone()
    by = [] if group_by == "none" else [
        {"key": r["k"], "won": r["won"], "lost": r["lost"], "win_rate": rate(r["won"], r["lost"]),
         "won_amount_text": _yen(r["won_amount"])} for r in rows]
    cond = {"期間": f"{a}〜{b}（受注・失注が決まった日）"}
    if cid:
        cond["顧客"] = state.db.execute("SELECT name FROM customers WHERE id=?", (cid,)).fetchone()[0]
    if own:
        cond["担当者"] = own
    return DealAnswer(rule=DEAL_RULE, conditions=cond, won=won, lost=lost, win_rate=rate(won, lost),
                      won_amount=won_amount, won_amount_text=_yen(won_amount), by=by,
                      open_deals=op[0], open_amount_text=_yen(op[1]), as_of=state.today().isoformat())


@tool("案件を一覧する", "tools")
def list_deals(
    stage: Annotated[str | None, Field(description="見込み・提案・受注・失注")] = None,
    owner: Annotated[str | None, Field(description="担当者")] = None,
    customer: Annotated[str | None, Field(description="顧客")] = None,
    expected_close_from: Annotated[str | None, Field(description="受注予定日の始め")] = None,
    expected_close_to: Annotated[str | None, Field(description="受注予定日の終わり")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> list[Deal]:
    """条件に合う案件を、受注予定日の順に返す（「今月受注予定の案件は？」など）。"""
    _check_login()
    where, args = ["1=1"], []
    if (st := _resolve("stage", stage)):
        where.append("d.stage = ?"); args.append(st)
    if (own := _resolve("owner", owner)):
        where.append("d.owner = ?"); args.append(own)
    if (cid := _resolve_customer(customer)):
        where.append("d.customer_id = ?"); args.append(cid)
    if expected_close_from or expected_close_to:
        a, b = _period(expected_close_from or "2000-01-01", expected_close_to or "2100-12-31")
        where.append("d.expected_close BETWEEN ? AND ?"); args += [a, b]
    rows = state.db.execute(f"SELECT d.*, c.name AS cname FROM deals d JOIN customers c ON c.id = d.customer_id "
                            f"WHERE {' AND '.join(where)} ORDER BY d.expected_close LIMIT ?", (*args, limit))
    return [Deal(deal_id=r["id"], title=r["title"], customer=r["cname"], owner=r["owner"], stage=r["stage"],
                 amount_text=_yen(r["amount"]), expected_close=r["expected_close"], closed_on=r["closed_on"])
            for r in rows]


@tool("使える名前を見る", "tools")
def list_values(
    kind: Annotated[Literal["owner", "customer", "product", "stage"], Field(description="担当者・顧客・商品・段階")],
) -> list[str]:
    """条件に使える正式な名前の一覧（顧客の正式名称など）。"""
    _check_login()
    return _values(kind)


# ---------------------------------------------------------------- 比べるための実験：AI に SQL を書かせる

SQL_ROW_LIMIT = 200


class SqlAnswer(BaseModel):
    columns: list[str]
    rows: list[list]
    truncated: bool


def _authorizer(action, arg1, arg2, db_name, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@tool("SQL で調べる（実験）", "sql")
def run_sql(
    sql: Annotated[str, Field(description="SQLite の SELECT 文を一つ", max_length=4000)],
) -> SqlAnswer:
    """売上と案件のデータベースに、読むだけの SQL（SELECT）を一つ実行する。表の形：
    customers(id, name 正式名称, short_name 呼び名) / products(id, name, unit_price) /
    sales(id, ordered_on 受注日, booked_on 計上日, customer_id, product_id, owner 担当者, quantity,
    amount 税抜金額（返品はマイナス）, tax 消費税額, status '計上' または 'キャンセル') /
    deals(id, title, customer_id, owner, stage '見込み'/'提案'/'受注'/'失注', amount 見込み金額（税抜）,
    created_on, expected_close 受注予定日, closed_on 受注・失注が決まった日)。日付は 'YYYY-MM-DD' の文字列。"""
    _check_login()
    with _sql_lock:
        return _run_sql(sql)


_sql_lock = threading.Lock()


def _run_sql(sql: str) -> SqlAnswer:
    deadline = time.monotonic() + 2.0
    state.db.set_authorizer(_authorizer)                                   # 読む以外の操作は断る
    state.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)  # 2秒で打ち切る
    try:
        cur = state.db.execute(sql)
        rows = cur.fetchmany(SQL_ROW_LIMIT + 1)
    except sqlite3.Error as e:
        raise ToolError(f"SQL を実行できませんでした: {e}")
    finally:
        state.db.set_authorizer(None)
        state.db.set_progress_handler(None, 0)
    cols = [d[0] for d in cur.description or []]
    return SqlAnswer(columns=cols, rows=[list(r) for r in rows[:SQL_ROW_LIMIT]], truncated=len(rows) > SQL_ROW_LIMIT)


# ---------------------------------------------------------------- サーバー


def create_count_server(settings: Settings, mode: str = "tools") -> MCPServer:
    kwargs = {}
    if settings.base_url:
        kwargs = dict(
            token_verifier=SharedTokenVerifier(AuthDB(settings.auth_db_path), settings.base_url),
            auth=AuthSettings(issuer_url=settings.base_url, resource_server_url=f"{settings.base_url}/count/mcp",
                              validate_token_resource=True),
        )
    mcp = MCPServer(name="browser-ai-count", title="サンプル商事 売上・案件", instructions=INSTRUCTIONS,
                    version=__version__, **kwargs)
    for fn, title, m in TOOLS:
        if m == mode:
            mcp.tool(title=title, annotations=READ_ONLY)(fn)
    return mcp


def build_count_app(settings: Settings, cs: CountSettings, today=None):
    configure(settings, cs, today)
    mcp = create_count_server(settings, cs.mode)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    return RequestLog(mcp.streamable_http_app(transport_security=security, host=settings.host,
                                              streamable_http_path="/count/mcp"), path="/count/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings, cs = Settings.from_env(), CountSettings.from_env()
    print(f"数える道具 MCP: http://{settings.host}:{cs.port}/count/mcp（{'SQL を書かせる実験' if cs.mode == 'sql' else '決まった道具'}）")
    uvicorn.run(build_count_app(settings, cs), host=settings.host, port=cs.port)


if __name__ == "__main__":
    main()
