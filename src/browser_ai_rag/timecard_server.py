"""タイムカードの MCP サーバー（第23〜24章）。

    uv run python -m browser_ai_rag.timecard_server

文書検索・メールとは別のサーバーとして動かす（URL は {RAG_BASE_URL}/timecard/mcp）。ログインは共通。

守っていること（第24章）：
- 誰の打刻かは、ログインの情報だけで決める。道具に「誰の分か」を渡す口を作らない
- 打刻の時刻は、サーバーの時計で決める。AI に時刻を書かせない
- 過去の打刻を直したいときは、理由をつけて申請し、管理者（申請した本人以外）が承認する
- どの AI から打刻したかを、打刻と一緒に残す。打刻は消さず、直したときは古いものに印をつける
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
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

INSTRUCTIONS = """利用者本人の出勤・退勤・休憩を打刻し、本人の勤怠を照会するサーバーです。
打刻（punch）は、利用者がはっきり頼んだときだけ行ってください。あいさつや雑談から推測して打刻しないこと。
打刻の時刻はサーバーの時計で決まります。「9時に出勤したことにして」のように過去の時刻を頼まれたら、punch は使わず、
request_correction で理由を添えて修正を申請してください（管理者が承認すると反映されます）。
ほかの人の分は打刻できません。"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DECIDE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

Kind = Literal["in", "out", "break_start", "break_end"]
KIND_JA = {"in": "出勤", "out": "退勤", "break_start": "休憩開始", "break_end": "休憩終了"}
STANDARD_MINUTES = 8 * 60   # 1日8時間を超えた分を残業として数える
OPEN_SHIFT_LIMIT = 20 * 3600  # 出勤から20時間たっても退勤がなければ「退勤漏れ」とみなす

SCHEMA = """
CREATE TABLE IF NOT EXISTS punches(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL,
  via TEXT NOT NULL, note TEXT, created_at INTEGER NOT NULL,
  correction_id INTEGER, voided_by INTEGER
);
CREATE INDEX IF NOT EXISTS punches_user_at ON punches(username, at);
CREATE TABLE IF NOT EXISTS corrections(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL,
  replaces INTEGER, reason TEXT NOT NULL, requested_via TEXT NOT NULL, requested_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at INTEGER, comment TEXT
);
"""


class TimecardSettings(BaseModel):
    admins: list[str] = []         # 修正の申請を承認できる人
    utc_offset: int = 9            # 勤務日の区切りに使う時差（日本は +9、夏時間なし）
    dev_user: str = "local"        # ログインなしで動かすとき（手元の開発用）の利用者名
    port: int = 8702

    @classmethod
    def from_env(cls) -> TimecardSettings:
        admins = [x.strip() for x in os.environ.get("TIMECARD_ADMINS", "").split(",") if x.strip()]
        return cls(admins=admins, utc_offset=int(os.environ.get("TIMECARD_UTC_OFFSET", "9")),
                   port=int(os.environ.get("TIMECARD_PORT", "8702")))


class _State:
    settings: Settings
    tc: TimecardSettings
    db: sqlite3.Connection
    auth: AuthDB | None = None
    clock = staticmethod(time.time)   # テストでは差し替える
    lock = threading.RLock()


state = _State()


def configure(settings: Settings, tc: TimecardSettings, clock=None) -> None:
    state.settings, state.tc = settings, tc
    state.clock = staticmethod(clock or time.time)
    path = settings.data_dir / "timecard.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    state.db = sqlite3.connect(path, check_same_thread=False)
    state.db.row_factory = sqlite3.Row
    state.db.executescript(SCHEMA)
    state.auth = AuthDB(settings.auth_db_path) if settings.base_url else None


# ---------------------------------------------------------------- 本人と時刻


def _me() -> str:
    """打刻するのは誰か。ログインの情報だけで決める。"""
    user = current_user()
    if user:
        return user
    if state.settings.base_url:   # 公開しているのにログインの情報がない：通さない
        raise ToolError("ログインしていないので、打刻できません。")
    return state.tc.dev_user


def _via() -> str:
    """どの AI からの呼び出しか（動的クライアント登録で AI が名乗った名前）。"""
    token = get_access_token()
    if token is None:
        return "local"
    if state.auth:
        rows = state.auth.q("SELECT info FROM oauth_clients WHERE client_id=?", (token.client_id,))
        if rows:
            name = json.loads(rows[0]["info"]).get("client_name")
            if name:
                return name
    return token.client_id


def _tz() -> timezone:
    return timezone(timedelta(hours=state.tc.utc_offset))


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%Y-%m-%d %H:%M")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}時間{minutes % 60:02d}分"


def _month_range(month: str | None) -> tuple[int, int, str]:
    now = datetime.fromtimestamp(state.clock(), _tz())
    try:
        y, m = (int(x) for x in month.split("-")) if month else (now.year, now.month)
        start = datetime(y, m, 1, tzinfo=_tz())
    except ValueError:
        raise ToolError(f"月は「2026-10」の形で指定してください（受け取った値: {month}）。")
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=_tz())
    return int(start.timestamp()), int(end.timestamp()), f"{y}-{m:02d}"


# ---------------------------------------------------------------- 勤務の組み立て


class Shift(BaseModel):
    work_date: str = Field(description="勤務日（出勤した日）")
    start: str
    end: str | None = Field(description="退勤の時刻。なければ勤務中か退勤漏れ")
    break_minutes: int
    worked_minutes: int = Field(description="退勤−出勤−休憩（分）")
    overtime_minutes: int = Field(description="1日8時間を超えた分")
    problem: str | None = Field(default=None, description="退勤漏れなど、直す必要のあること")


def _punches(user: str, since: int, until: int) -> list[sqlite3.Row]:
    return state.db.execute(
        "SELECT * FROM punches WHERE username=? AND voided_by IS NULL AND at>=? AND at<? ORDER BY at, id",
        (user, since, until)).fetchall()


def _shifts(user: str, since: int, until: int) -> list[Shift]:
    """打刻を時刻の順に並べ、出勤から退勤までを1回の勤務にまとめる。日をまたいでも出勤した日の勤務にする。
    時間は、画面に出す時刻（分まで）どうしの差で数える。秒で数えると、表示と合わなくなる（第23章）。"""
    rows = _punches(user, since - 24 * 3600, until + 24 * 3600)
    shifts: list[dict] = []
    cur: dict | None = None
    now = int(state.clock())

    def close(problem: str | None = None):
        nonlocal cur
        if cur is not None:
            cur["problem"] = cur.get("problem") or problem
            shifts.append(cur)
        cur = None

    for r in rows:
        k, at = r["kind"], r["at"] - r["at"] % 60   # 秒は切り捨てて、分の単位で数える
        if k == "in":
            close("退勤の打刻がありません")
            cur = {"start": at, "end": None, "breaks": 0, "break_from": None, "problem": None}
        elif cur is None:
            shifts.append({"start": at, "end": at if k == "out" else None, "breaks": 0, "break_from": None,
                           "problem": f"出勤の打刻がないまま{KIND_JA[k]}が打たれています"})
        elif k == "break_start":
            cur["break_from"] = at
        elif k == "break_end":
            if cur["break_from"] is not None:
                cur["breaks"] += at - cur["break_from"]
                cur["break_from"] = None
        elif k == "out":
            cur["end"] = at
            if cur["break_from"] is not None:
                cur["problem"] = "休憩終了の打刻がありません"
            close()
    if cur is not None:
        close("退勤の打刻がありません" if now - cur["start"] > OPEN_SHIFT_LIMIT else None)

    out = []
    for s in shifts:
        if not (since <= s["start"] < until):
            continue
        worked = max(0, (s["end"] - s["start"] - s["breaks"]) // 60) if s["end"] else 0
        out.append(Shift(work_date=_fmt(s["start"])[:10], start=_fmt(s["start"])[11:],
                         end=_fmt(s["end"])[11:] if s["end"] else None, break_minutes=s["breaks"] // 60,
                         worked_minutes=worked, overtime_minutes=max(0, worked - STANDARD_MINUTES),
                         problem=s["problem"]))
    return out


def _status(user: str) -> tuple[str, sqlite3.Row | None]:
    """いまの状態（勤務外・勤務中・休憩中）と、最後の打刻。"""
    last = state.db.execute("SELECT * FROM punches WHERE username=? AND voided_by IS NULL ORDER BY at DESC, id DESC LIMIT 1",
                            (user,)).fetchone()
    if last is None or last["kind"] == "out":
        return "勤務外", last
    if int(state.clock()) - _shift_start(user, last) > OPEN_SHIFT_LIMIT:
        return "勤務外（前回の退勤漏れ）", last
    return ("休憩中" if last["kind"] == "break_start" else "勤務中"), last


def _shift_start(user: str, last: sqlite3.Row) -> int:
    row = state.db.execute("SELECT at FROM punches WHERE username=? AND voided_by IS NULL AND kind='in' AND at<=? "
                           "ORDER BY at DESC LIMIT 1", (user, last["at"])).fetchone()
    return row["at"] if row else last["at"]


# ---------------------------------------------------------------- 答えの形


class PunchResult(BaseModel):
    punch_id: int
    username: str = Field(description="打刻した人（ログインの情報から決めた）")
    kind: str
    at: str = Field(description="打刻の時刻（サーバーの時計）")
    via: str = Field(description="どの AI から打刻したか")
    status: str = Field(description="打刻した後の状態")


class Today(BaseModel):
    username: str
    now: str
    status: str
    punches: list[str]


class MonthSummary(BaseModel):
    username: str
    month: str
    days_worked: int
    worked: str
    worked_minutes: int
    overtime: str
    overtime_minutes: int
    problems: list[str] = Field(description="退勤漏れなど、修正の申請が要る日")
    pending_corrections: int


class Correction(BaseModel):
    correction_id: int
    username: str
    kind: str
    at: str
    replaces_punch_id: int | None
    reason: str
    status: str = Field(description="pending（承認待ち）・approved・rejected")
    requested_via: str
    decided_by: str | None = None
    comment: str | None = None


class CorrectionList(BaseModel):
    viewer: str = Field(description="いま見ている人（ログインの情報から）")
    viewer_is_admin: bool = Field(description="見ている人が管理者か。管理者なら全員分、そうでなければ本人分だけが入る")
    approvers: list[str] = Field(description="申請を承認できる人（管理者）。自分の申請は自分以外の管理者が承認する")
    corrections: list[Correction]


def _correction(r: sqlite3.Row) -> Correction:
    return Correction(correction_id=r["id"], username=r["username"], kind=KIND_JA[r["kind"]], at=_fmt(r["at"]),
                      replaces_punch_id=r["replaces"], reason=r["reason"], status=r["status"],
                      requested_via=r["requested_via"], decided_by=r["decided_by"], comment=r["comment"])


TOOLS: list[tuple] = []


def tool(title: str, annotations: ToolAnnotations):
    def mark(fn):
        TOOLS.append((fn, title, annotations))
        return fn
    return mark


# ---------------------------------------------------------------- 道具

ALLOWED_NEXT = {
    "勤務外": {"in"}, "勤務外（前回の退勤漏れ）": {"in"},
    "勤務中": {"out", "break_start"}, "休憩中": {"break_end"},
}


@tool("打刻する", WRITE)
def punch(
    kind: Annotated[Kind, Field(description="in=出勤、out=退勤、break_start=休憩開始、break_end=休憩終了")],
    note: Annotated[str | None, Field(description="メモ（直行・在宅など）", max_length=200)] = None,
) -> PunchResult:
    """ログインしている本人の打刻をする。時刻はサーバーの時計で決まり、指定できない。
    利用者がはっきり頼んだときだけ呼ぶこと。過去の時刻で打ちたいときは request_correction を使う。"""
    user = _me()
    with state.lock:
        status, last = _status(user)
        if kind not in ALLOWED_NEXT[status]:
            can = "・".join(KIND_JA[k] for k in sorted(ALLOWED_NEXT[status]))
            last_txt = f"（最後の打刻: {KIND_JA[last['kind']]} {_fmt(last['at'])}）" if last else ""
            raise ToolError(f"いまは「{status}」なので{KIND_JA[kind]}は打てません{last_txt}。"
                            f"打てるのは {can} です。時刻を直したいなら request_correction で申請してください。")
        now = int(state.clock())
        with state.db:
            cur = state.db.execute("INSERT INTO punches(username, kind, at, via, note, created_at) VALUES(?,?,?,?,?,?)",
                                   (user, kind, now, _via(), note, now))
        after, _ = _status(user)
    return PunchResult(punch_id=cur.lastrowid, username=user, kind=KIND_JA[kind], at=_fmt(now), via=_via(), status=after)


@tool("今日の勤怠を見る", READ_ONLY)
def my_today() -> Today:
    """本人のいまの状態（勤務外・勤務中・休憩中）と、今日の打刻を返す。"""
    user = _me()
    now = int(state.clock())
    day0 = int(datetime.fromtimestamp(now, _tz()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = _punches(user, day0, day0 + 24 * 3600)
    status, _ = _status(user)
    return Today(username=user, now=_fmt(now), status=status,
                 punches=[f"{KIND_JA[r['kind']]} {_fmt(r['at'])[11:]}（番号 {r['id']}・{r['via']}）" for r in rows])


@tool("月の勤務を見る", READ_ONLY)
def my_records(
    month: Annotated[str | None, Field(description="「2026-10」の形。省略すると今月")] = None,
) -> list[Shift]:
    """本人の、指定した月の勤務を1回ずつ返す（出勤・退勤・休憩・働いた時間・残業・問題点）。"""
    since, until, _ = _month_range(month)
    return _shifts(_me(), since, until)


@tool("月の集計", READ_ONLY)
def monthly_summary(
    month: Annotated[str | None, Field(description="「2026-10」の形。省略すると今月")] = None,
    username: Annotated[str | None, Field(description="ほかの人の集計を見るとき（管理者だけ）。省略すると本人")] = None,
) -> MonthSummary:
    """指定した月の、出勤日数・働いた時間・残業・直す必要のある日を数える。数えるのはサーバーで、AI に計算させない。"""
    me = _me()
    who = username or me
    if who != me and me not in state.tc.admins:
        raise ToolError("ほかの人の勤怠は、管理者しか見られません。")
    since, until, label = _month_range(month)
    shifts = _shifts(who, since, until)
    worked = sum(s.worked_minutes for s in shifts)
    over = sum(s.overtime_minutes for s in shifts)
    pending = state.db.execute("SELECT COUNT(*) FROM corrections WHERE username=? AND status='pending'", (who,)).fetchone()[0]
    return MonthSummary(username=who, month=label, days_worked=len({s.work_date for s in shifts if s.end}),
                        worked=_hm(worked), worked_minutes=worked, overtime=_hm(over), overtime_minutes=over,
                        problems=[f"{s.work_date} {s.start}〜: {s.problem}" for s in shifts if s.problem],
                        pending_corrections=pending)


@tool("打刻の修正を申請する", WRITE)
def request_correction(
    kind: Annotated[Kind, Field(description="in=出勤、out=退勤、break_start=休憩開始、break_end=休憩終了")],
    at: Annotated[str, Field(description="正しい日時。「2026-10-02 09:00」の形")],
    reason: Annotated[str, Field(description="修正の理由（打ち忘れ、など）", min_length=2, max_length=300)],
    replaces_punch_id: Annotated[int | None, Field(description="間違った打刻を置き換えるときは、その番号。打ち忘れの追加なら省略")] = None,
) -> Correction:
    """打ち忘れや打ち間違いの修正を申請する。すぐには反映されず、管理者が承認したときに反映される。"""
    user = _me()
    try:
        ts = int(datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=_tz()).timestamp())
    except ValueError:
        raise ToolError(f"日時は「2026-10-02 09:00」の形で指定してください（受け取った値: {at}）。")
    if ts > state.clock():
        raise ToolError("これから先の時刻は申請できません。")
    if replaces_punch_id is not None:
        row = state.db.execute("SELECT * FROM punches WHERE id=? AND username=? AND voided_by IS NULL",
                               (replaces_punch_id, user)).fetchone()
        if row is None:
            raise ToolError(f"番号 {replaces_punch_id} の打刻は、あなたの有効な打刻の中に見つかりません。")
    now = int(state.clock())
    with state.lock, state.db:
        cur = state.db.execute("INSERT INTO corrections(username, kind, at, replaces, reason, requested_via, requested_at) "
                               "VALUES(?,?,?,?,?,?,?)", (user, kind, ts, replaces_punch_id, reason, _via(), now))
    return _correction(state.db.execute("SELECT * FROM corrections WHERE id=?", (cur.lastrowid,)).fetchone())


@tool("修正の申請を見る", READ_ONLY)
def list_corrections(
    status: Annotated[Literal["pending", "approved", "rejected", "all"], Field(description="pending=承認待ち")] = "pending",
) -> CorrectionList:
    """修正の申請を返す。管理者には全員の分、それ以外の人には本人の分だけ。
    見ている人・管理者かどうか・承認できる人も返すので、推測で答えないこと。"""
    me = _me()
    sql, args = "SELECT * FROM corrections WHERE 1=1", []
    if me not in state.tc.admins:
        sql += " AND username=?"
        args.append(me)
    if status != "all":
        sql += " AND status=?"
        args.append(status)
    return CorrectionList(viewer=me, viewer_is_admin=me in state.tc.admins, approvers=list(state.tc.admins),
                          corrections=[_correction(r) for r in state.db.execute(sql + " ORDER BY id DESC LIMIT 50", args)])


@tool("修正の申請を承認・却下する", DECIDE)
def decide_correction(
    correction_id: Annotated[int, Field(description="list_corrections で得た申請の番号")],
    approve: Annotated[bool, Field(description="true=承認して打刻に反映、false=却下")],
    comment: Annotated[str | None, Field(description="承認・却下の理由", max_length=300)] = None,
) -> Correction:
    """管理者が修正の申請を承認・却下する。承認すると打刻に反映され、置き換えられた打刻には印がつく（消さない）。
    自分の申請は承認できない。"""
    me = _me()
    if me not in state.tc.admins:
        raise ToolError("修正の申請を承認・却下できるのは、管理者だけです。")
    with state.lock:
        r = state.db.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
        if r is None:
            raise ToolError(f"申請 {correction_id} は見つかりません。")
        if r["status"] != "pending":
            return _correction(r)
        if r["username"] == me:
            raise ToolError("自分の申請は承認・却下できません。ほかの管理者に頼んでください。")
        now = int(state.clock())
        with state.db:
            if approve:
                cur = state.db.execute("INSERT INTO punches(username, kind, at, via, note, created_at, correction_id) "
                                       "VALUES(?,?,?,?,?,?,?)",
                                       (r["username"], r["kind"], r["at"], f"修正申請 {correction_id}（承認: {me}）",
                                        r["reason"], now, correction_id))
                if r["replaces"]:
                    state.db.execute("UPDATE punches SET voided_by=? WHERE id=?", (cur.lastrowid, r["replaces"]))
            state.db.execute("UPDATE corrections SET status=?, decided_by=?, decided_at=?, comment=? WHERE id=?",
                             ("approved" if approve else "rejected", me, now, comment, correction_id))
    return _correction(state.db.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone())


# ---------------------------------------------------------------- サーバー


def create_timecard_server(settings: Settings) -> MCPServer:
    kwargs = {}
    if settings.base_url:
        kwargs = dict(
            token_verifier=SharedTokenVerifier(AuthDB(settings.auth_db_path), settings.base_url),
            auth=AuthSettings(issuer_url=settings.base_url, resource_server_url=f"{settings.base_url}/timecard/mcp",
                              validate_token_resource=True),
        )
    mcp = MCPServer(name="browser-ai-timecard", title="サンプル商事 タイムカード", instructions=INSTRUCTIONS,
                    version=__version__, **kwargs)
    for fn, title, annotations in TOOLS:
        mcp.tool(title=title, annotations=annotations)(fn)
    return mcp


def build_timecard_app(settings: Settings, tc: TimecardSettings, clock=None):
    configure(settings, tc, clock)
    mcp = create_timecard_server(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    return RequestLog(mcp.streamable_http_app(transport_security=security, host=settings.host,
                                              streamable_http_path="/timecard/mcp"), path="/timecard/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings, tc = Settings.from_env(), TimecardSettings.from_env()
    print(f"タイムカード MCP: http://{settings.host}:{tc.port}/timecard/mcp（管理者: {', '.join(tc.admins) or 'なし'}）")
    uvicorn.run(build_timecard_app(settings, tc), host=settings.host, port=tc.port)


if __name__ == "__main__":
    main()
