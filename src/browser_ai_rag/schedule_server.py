"""予定とタスクの MCP サーバー（第27章）。

    uv run python -m browser_ai_rag.schedule_server

URL は {RAG_BASE_URL}/schedule/mcp。ログインはほかのサーバーと共通。

守っていること：
- 日時は、曜日をつけて返す。AI が曜日も書いてきたら、日付と合っているかを確かめる（「来週の火曜」の計算違いを止める）
- 予定が重なるときは、黙って入れない。誰と重なるかを返す
- ほかの人の予定は「空いているかどうか」だけ。題名や場所は見せない
- 予定を動かす・取り消すのは、主催者だけ。取り消しても記録は消さない
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
from .timecard_server import _via_from

INSTRUCTIONS = """利用者の予定とタスクを扱うサーバーです。
「来週の火曜」のような言い方は、答えにある今日の日付と曜日をもとに、具体的な日付に直してから道具を呼んでください。
利用者が曜日で言ったときは、weekday にその曜日も入れてください。日付と合わなければサーバーが断ります。
予定を入れる・動かす前に、日時と参加者を利用者に確かめてください。ほかの人の予定は、空いているかどうかしか見られません。"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

WEEKDAYS = "月火水木金土日"
WORK_START, WORK_END = 9, 18      # 空き時間を探すのは、平日の9時〜18時
STEP = 30                         # 空き時間は30分きざみで探す

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(username TEXT PRIMARY KEY, display_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, organizer TEXT NOT NULL, title TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
  location TEXT, created_via TEXT, created_at INTEGER NOT NULL, cancelled_at INTEGER
);
CREATE TABLE IF NOT EXISTS attendees(event_id INTEGER NOT NULL, username TEXT NOT NULL, PRIMARY KEY(event_id, username));
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, due TEXT, note TEXT,
  created_via TEXT, created_at INTEGER NOT NULL, done_at INTEGER
);
"""


class ScheduleSettings(BaseModel):
    utc_offset: int = 9
    dev_user: str = "eto"
    port: int = 8704

    @classmethod
    def from_env(cls) -> ScheduleSettings:
        return cls(utc_offset=int(os.environ.get("SCHEDULE_UTC_OFFSET", "9")),
                   port=int(os.environ.get("SCHEDULE_PORT", "8704")))


class _State:
    settings: Settings
    sc: ScheduleSettings
    db: sqlite3.Connection
    auth: AuthDB | None = None
    clock = staticmethod(time.time)
    lock = threading.RLock()


state = _State()


def configure(settings: Settings, sc: ScheduleSettings, clock=None) -> None:
    state.settings, state.sc = settings, sc
    state.clock = staticmethod(clock or time.time)
    path = settings.data_dir / "schedule.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    state.db = sqlite3.connect(path, check_same_thread=False)
    state.db.row_factory = sqlite3.Row
    state.db.executescript(SCHEMA)
    state.auth = AuthDB(settings.auth_db_path) if settings.base_url else None
    if state.db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
        _seed()


# ---------------------------------------------------------------- 日時


def _tz() -> timezone:
    return timezone(timedelta(hours=state.sc.utc_offset))


def _now() -> datetime:
    return datetime.fromtimestamp(state.clock(), _tz())


def _fmt(ts: int) -> str:
    d = datetime.fromtimestamp(ts, _tz())
    return f"{d:%Y-%m-%d}({WEEKDAYS[d.weekday()]}) {d:%H:%M}"


def _day(d: date) -> str:
    return f"{d:%Y-%m-%d}({WEEKDAYS[d.weekday()]})"


def _parse(text: str, weekday: str | None = None) -> int:
    """「2026-10-13 15:00」を受け取る。曜日も渡されたら、日付と合っているか確かめる。"""
    if os.environ.get("SCHEDULE_LOG_ARGS") == "1":   # 実験のときだけ：AI が曜日を渡してきたかを記録する（架空のデータ）
        logging.getLogger("schedule").info("日時: %s 曜日: %s", text, weekday)
    t = text.strip().replace("T", " ")
    try:
        d = datetime.strptime(t[:16], "%Y-%m-%d %H:%M").replace(tzinfo=_tz())
    except ValueError:
        raise ToolError(f"日時は「2026-10-13 15:00」の形で指定してください（受け取った値: {text}）。")
    if weekday:
        w = weekday.strip().rstrip("曜日")[:1]
        if w in WEEKDAYS and WEEKDAYS[d.weekday()] != w:
            raise ToolError(f"{d:%Y-%m-%d} は{WEEKDAYS[d.weekday()]}曜日で、{w}曜日ではありません。"
                            f"今日は {_day(_now().date())} です。日付を計算し直してください。")
    return int(d.timestamp())


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        raise ToolError(f"日付は「2026-10-13」の形で指定してください（受け取った値: {text}）。")


# ---------------------------------------------------------------- 人


def _me() -> str:
    user = current_user()
    if not user:
        if state.settings.base_url:
            raise ToolError("ログインしていないので、予定は見られません。")
        user = state.sc.dev_user
    if not state.db.execute("SELECT 1 FROM people WHERE username=?", (user,)).fetchone():
        with state.lock, state.db:
            state.db.execute("INSERT INTO people VALUES(?,?)", (user, user))
    return user


def _name(username: str) -> str:
    r = state.db.execute("SELECT display_name FROM people WHERE username=?", (username,)).fetchone()
    return r[0] if r else username


def _resolve_person(text: str) -> str:
    """「山田」「山田さん」「yamada」のどれでも受け取る。決めきれなければ候補を返して断る。"""
    t = text.strip().removesuffix("さん")
    rows = state.db.execute("SELECT username, display_name FROM people").fetchall()
    hit = [r["username"] for r in rows if t in (r["username"], r["display_name"])]
    if len(hit) == 1:
        return hit[0]
    raise ToolError(f"「{text}」が誰か決められません。予定表にいる人: "
                    + "、".join(f"{r['display_name']}（{r['username']}）" for r in rows))


def _via() -> str:
    return _via_from(state.auth)


# ---------------------------------------------------------------- 予定の重なり


def _busy(username: str, start: int, end: int, skip_event: int | None = None) -> list[sqlite3.Row]:
    return state.db.execute(
        "SELECT DISTINCT e.* FROM events e LEFT JOIN attendees a ON a.event_id = e.id "
        "WHERE e.cancelled_at IS NULL AND (e.organizer = ? OR a.username = ?) AND e.start < ? AND e.end > ? "
        "AND e.id IS NOT ? ORDER BY e.start", (username, username, end, start, skip_event)).fetchall()


def _conflicts(people: list[str], start: int, end: int, me: str, skip_event: int | None = None) -> list[str]:
    """重なる予定を、本人の分は題名つきで、ほかの人の分は「予定あり」だけで返す。"""
    out = []
    for p in people:
        for e in _busy(p, start, end, skip_event):
            span = f"{_fmt(e['start'])[-5:]}〜{_fmt(e['end'])[-5:]}"
            out.append(f"自分: {e['title']}（{span}）" if p == me else f"{_name(p)}: 予定あり（{span}）")
    return out


# ---------------------------------------------------------------- 答えの形


class Event(BaseModel):
    event_id: int
    title: str
    start: str = Field(description="開始（曜日つき）")
    end: str
    organizer: str
    attendees: list[str]
    location: str | None
    can_edit: bool = Field(description="自分が主催者なら true（動かす・取り消すのは主催者だけ）")


class EventList(BaseModel):
    viewer: str
    now: str = Field(description="サーバーの今の日時（曜日つき）。「来週」などはここから数える")
    events: list[Event]


class Slot(BaseModel):
    start: str
    end: str


class FreeTime(BaseModel):
    now: str
    people: list[str] = Field(description="空きを調べた人（自分を含む）")
    slots: list[Slot] = Field(description="全員が空いている時間の候補（平日9〜18時、30分きざみ）")


class Saved(BaseModel):
    event: Event
    note: str


class Task(BaseModel):
    task_id: int
    title: str
    due: str | None = Field(description="期限（曜日つき）")
    overdue: bool
    done: bool
    note: str | None


class TaskList(BaseModel):
    viewer: str
    today: str
    tasks: list[Task]


def _event(r: sqlite3.Row, me: str) -> Event:
    att = [_name(a[0]) for a in state.db.execute("SELECT username FROM attendees WHERE event_id=? ORDER BY username",
                                                  (r["id"],))]
    return Event(event_id=r["id"], title=r["title"], start=_fmt(r["start"]), end=_fmt(r["end"]),
                 organizer=_name(r["organizer"]), attendees=att, location=r["location"], can_edit=r["organizer"] == me)


def _task(r: sqlite3.Row) -> Task:
    today = _now().date()
    due = date.fromisoformat(r["due"]) if r["due"] else None
    return Task(task_id=r["id"], title=r["title"], due=_day(due) if due else None,
                overdue=bool(due and due < today and not r["done_at"]), done=bool(r["done_at"]), note=r["note"])


TOOLS: list[tuple] = []


def tool(title: str, annotations: ToolAnnotations):
    def mark(fn):
        TOOLS.append((fn, title, annotations))
        return fn
    return mark


# ---------------------------------------------------------------- 予定の道具


@tool("予定を見る", READ_ONLY)
def list_events(
    date_from: Annotated[str | None, Field(description="「2026-10-05」の形。省略すると今日")] = None,
    date_to: Annotated[str | None, Field(description="この日まで（その日を含む）。省略すると7日後まで")] = None,
) -> EventList:
    """自分の予定（主催・参加）を時刻の順に返す。答えには今日の日時と曜日も入る。"""
    me = _me()
    a = _date(date_from) if date_from else _now().date()
    b = _date(date_to) if date_to else a + timedelta(days=7)
    lo = int(datetime.combine(a, datetime.min.time(), _tz()).timestamp())
    hi = int(datetime.combine(b + timedelta(days=1), datetime.min.time(), _tz()).timestamp())
    rows = _busy(me, lo, hi)
    return EventList(viewer=_name(me), now=_fmt(int(state.clock())), events=[_event(r, me) for r in rows])


@tool("空いている時間を探す", READ_ONLY)
def find_free_time(
    attendees: Annotated[list[str], Field(description="一緒に予定を入れたい人（名前でよい）。自分は自動で入る")],
    date_from: Annotated[str, Field(description="探しはじめる日「2026-10-13」")],
    date_to: Annotated[str, Field(description="探し終わる日（その日を含む）")],
    duration_minutes: Annotated[int, Field(ge=15, le=480, description="必要な長さ（分）")] = 60,
) -> FreeTime:
    """自分と相手が全員空いている時間を探す。ほかの人の予定の中身は見せない（空いているかどうかだけ）。"""
    me = _me()
    people = [me] + [p for p in (_resolve_person(x) for x in attendees) if p != me]
    a, b = _date(date_from), _date(date_to)
    if (b - a).days > 31:
        raise ToolError("探す範囲は31日以内にしてください。")
    now, slots, d = int(state.clock()), [], a
    while d <= b and len(slots) < 10:
        if d.weekday() < 5:
            t = datetime.combine(d, datetime.min.time(), _tz()).replace(hour=WORK_START)
            last = t.replace(hour=WORK_END) - timedelta(minutes=duration_minutes)
            while t <= last and len(slots) < 10:
                s, e = int(t.timestamp()), int((t + timedelta(minutes=duration_minutes)).timestamp())
                if s >= now and not any(_busy(p, s, e) for p in people):
                    slots.append(Slot(start=_fmt(s), end=_fmt(e)))
                    t += timedelta(minutes=duration_minutes)    # 候補どうしが重ならないように
                    continue
                t += timedelta(minutes=STEP)
        d += timedelta(days=1)
    return FreeTime(now=_fmt(now), people=[_name(p) for p in people], slots=slots)


@tool("予定を入れる", WRITE)
def create_event(
    title: Annotated[str, Field(min_length=1, max_length=100)],
    start: Annotated[str, Field(description="開始「2026-10-13 15:00」")],
    duration_minutes: Annotated[int, Field(ge=5, le=720)] = 60,
    attendees: Annotated[list[str], Field(description="参加者（名前でよい）。参加者の予定表にも入る")] = [],
    location: Annotated[str | None, Field(max_length=100)] = None,
    weekday: Annotated[str | None, Field(description="利用者が曜日で言ったときの曜日（「火」など）。日付と合わなければ断る")] = None,
    allow_conflict: Annotated[bool, Field(description="重なりを利用者が承知のうえで入れるときだけ true")] = False,
) -> Saved:
    """予定を入れる。重なる予定があれば入れずに、誰とどう重なるかを返す（利用者が承知なら allow_conflict=true）。"""
    me = _me()
    s = _parse(start, weekday)
    e = s + duration_minutes * 60
    if s < int(state.clock()):
        raise ToolError(f"{_fmt(s)} は過ぎた時刻です。今は {_fmt(int(state.clock()))} です。")
    people = [me] + [p for p in (_resolve_person(x) for x in attendees) if p != me]
    clash = _conflicts(people, s, e, me)
    if clash and not allow_conflict:
        raise ToolError("予定が重なるので、入れませんでした。" + "／".join(clash)
                        + "。find_free_time で空き時間を探すか、利用者が承知なら allow_conflict=true で入れてください。")
    with state.lock, state.db:
        cur = state.db.execute("INSERT INTO events(organizer, title, start, end, location, created_via, created_at)"
                               " VALUES(?,?,?,?,?,?,?)", (me, title, s, e, location, _via(), int(state.clock())))
        state.db.executemany("INSERT INTO attendees VALUES(?,?)", [(cur.lastrowid, p) for p in people if p != me])
    row = state.db.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    note = "参加者の予定表にも入りました。" if len(people) > 1 else "自分の予定表に入りました。"
    if clash:
        note += "（重なりを承知で入れました: " + "／".join(clash) + "）"
    return Saved(event=_event(row, me), note=note)


def _my_event(event_id: int, me: str) -> sqlite3.Row:
    r = state.db.execute("SELECT * FROM events WHERE id=? AND cancelled_at IS NULL", (event_id,)).fetchone()
    if r is None or (r["organizer"] != me and not state.db.execute(
            "SELECT 1 FROM attendees WHERE event_id=? AND username=?", (event_id, me)).fetchone()):
        raise ToolError(f"予定 {event_id} は見つかりません。")
    if r["organizer"] != me:
        raise ToolError(f"予定 {event_id}（{r['title']}）の主催者は {_name(r['organizer'])} さんです。"
                        "動かしたり取り消したりできるのは主催者だけです。主催者に頼んでください。")
    return r


@tool("予定を動かす", WRITE)
def move_event(
    event_id: Annotated[int, Field(description="list_events で得た番号")],
    start: Annotated[str, Field(description="新しい開始「2026-10-13 15:00」")],
    duration_minutes: Annotated[int | None, Field(ge=5, le=720, description="新しい長さ。省略すると今と同じ")] = None,
    weekday: Annotated[str | None, Field(description="利用者が曜日で言ったときの曜日")] = None,
    allow_conflict: bool = False,
) -> Saved:
    """自分が主催する予定の日時を変える。参加者の予定表も一緒に変わる。重なりがあれば動かさずに返す。"""
    me = _me()
    r = _my_event(event_id, me)
    s = _parse(start, weekday)
    e = s + (duration_minutes * 60 if duration_minutes else r["end"] - r["start"])
    if s < int(state.clock()):
        raise ToolError(f"{_fmt(s)} は過ぎた時刻です。今は {_fmt(int(state.clock()))} です。")
    people = [r["organizer"]] + [a[0] for a in state.db.execute("SELECT username FROM attendees WHERE event_id=?",
                                                                 (event_id,))]
    clash = _conflicts(people, s, e, me, skip_event=event_id)
    if clash and not allow_conflict:
        raise ToolError("予定が重なるので、動かしませんでした。" + "／".join(clash) + "。")
    with state.lock, state.db:
        state.db.execute("UPDATE events SET start=?, end=? WHERE id=?", (s, e, event_id))
    row = state.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return Saved(event=_event(row, me), note=f"{_fmt(r['start'])} から動かしました。参加者の予定表も変わりました。")


@tool("予定を取り消す", CANCEL)
def cancel_event(
    event_id: Annotated[int, Field(description="取り消す予定の番号")],
) -> str:
    """自分が主催する予定を取り消す。参加者の予定表からも消える（記録は残る）。"""
    me = _me()
    r = _my_event(event_id, me)
    with state.lock, state.db:
        state.db.execute("UPDATE events SET cancelled_at=? WHERE id=?", (int(state.clock()), event_id))
    return f"予定 {event_id}（{r['title']}、{_fmt(r['start'])}）を取り消しました。参加者の予定表からも消えます。"


class Today(BaseModel):
    viewer: str
    now: str
    events: list[Event] = Field(description="今日の予定（終わったものも含む。done=終わった）")
    finished: list[int] = Field(description="もう終わった予定の event_id")
    tasks_overdue: list[Task]
    tasks_due_soon: list[Task] = Field(description="今日から3日以内が期限のタスク")


@tool("今日の予定とタスク", READ_ONLY)
def today_overview() -> Today:
    """「今日やることは？」に答えるための道具。今日の予定と、期限切れ・期限の近いタスクをまとめて返す。"""
    me = _me()
    now = _now()
    lo = int(datetime.combine(now.date(), datetime.min.time(), _tz()).timestamp())
    rows = _busy(me, lo, lo + 86400)
    tasks = [_task(r) for r in state.db.execute(
        "SELECT * FROM tasks WHERE owner=? AND done_at IS NULL AND due IS NOT NULL ORDER BY due", (me,))]
    soon = (now.date() + timedelta(days=3))
    return Today(viewer=_name(me), now=_fmt(int(now.timestamp())), events=[_event(r, me) for r in rows],
                 finished=[r["id"] for r in rows if r["end"] <= now.timestamp()],
                 tasks_overdue=[t for t in tasks if t.overdue],
                 tasks_due_soon=[t for t in tasks if not t.overdue and t.due and date.fromisoformat(t.due[:10]) <= soon])


# ---------------------------------------------------------------- タスクの道具


@tool("タスクを見る", READ_ONLY)
def list_tasks(
    status: Annotated[Literal["open", "done", "all"], Field(description="open=未完了")] = "open",
) -> TaskList:
    """自分のタスクを、期限の近い順に返す。期限を過ぎたものには overdue が付く。"""
    me = _me()
    sql = "SELECT * FROM tasks WHERE owner=?" + {"open": " AND done_at IS NULL", "done": " AND done_at IS NOT NULL",
                                                  "all": ""}[status]
    rows = state.db.execute(sql + " ORDER BY due IS NULL, due, id", (me,)).fetchall()
    return TaskList(viewer=_name(me), today=_day(_now().date()), tasks=[_task(r) for r in rows])


@tool("タスクを足す", WRITE)
def add_task(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    due: Annotated[str | None, Field(description="期限「2026-10-09」")] = None,
    note: Annotated[str | None, Field(max_length=500)] = None,
) -> Task:
    """自分のタスクを一つ足す。"""
    me = _me()
    d = _date(due).isoformat() if due else None
    with state.lock, state.db:
        cur = state.db.execute("INSERT INTO tasks(owner, title, due, note, created_via, created_at) VALUES(?,?,?,?,?,?)",
                               (me, title, d, note, _via(), int(state.clock())))
    return _task(state.db.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())


@tool("タスクを終える", WRITE)
def complete_task(
    task_id: Annotated[int, Field(description="list_tasks で得た番号")],
) -> Task:
    """自分のタスクを終わったことにする。もう終わっていれば、そのまま返す。"""
    me = _me()
    r = state.db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, me)).fetchone()
    if r is None:
        raise ToolError(f"タスク {task_id} は見つかりません。")
    if not r["done_at"]:
        with state.lock, state.db:
            state.db.execute("UPDATE tasks SET done_at=? WHERE id=?", (int(state.clock()), task_id))
    return _task(state.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


# ---------------------------------------------------------------- 試すためのデータ


def _seed() -> None:
    """今週と来週に、サンプル商事の人たちの予定とタスクを入れておく（架空）。"""
    people = [("eto", "江藤"), ("yamada", "山田"), ("sato", "佐藤"), ("suzuki", "鈴木")]
    monday = _now().date() - timedelta(days=_now().weekday())

    def at(day: int, hm: str) -> int:
        h, m = (int(x) for x in hm.split(":"))
        return int(datetime.combine(monday + timedelta(days=day), datetime.min.time(), _tz()).replace(hour=h, minute=m).timestamp())

    events = [  # (主催, 題名, 何日目, 開始, 終了, 参加者, 場所)
        ("yamada", "営業定例", 0, "10:00", "11:00", ["eto", "sato", "suzuki"], "会議室A"),
        ("eto", "アルファ商会 打ち合わせ", 1, "13:00", "14:00", [], "先方"),
        ("suzuki", "1on1", 2, "15:00", "15:30", ["eto"], None),
        ("eto", "見積の社内確認", 3, "09:30", "10:00", ["yamada"], None),
        ("yamada", "外出（ベータ工業）", 1, "14:00", "17:00", [], None),
        ("yamada", "部長面談", 2, "10:00", "12:00", [], None),
        ("sato", "出張（大阪）", 3, "09:00", "18:00", [], None),
        ("sato", "採用面接", 1, "15:00", "16:00", [], None),
        ("yamada", "営業定例", 7, "10:00", "11:00", ["eto", "sato", "suzuki"], "会議室A"),
        ("eto", "研修", 8, "10:00", "12:00", [], "本社"),
        ("yamada", "ガンマ物産 提案", 8, "14:00", "16:00", [], None),
        ("sato", "顧客訪問", 8, "13:00", "15:00", [], None),
        ("suzuki", "全社会議", 9, "16:00", "17:00", ["eto", "yamada", "sato"], "大会議室"),
    ]
    tasks = [("アルファ商会に見積書を送る", 1), ("経費精算", 4), ("研修資料を読んでおく", 7), ("9月の営業報告を出す", -2)]
    now = int(state.clock())
    with state.db:
        state.db.executemany("INSERT INTO people VALUES(?,?)", people)
        for org, title, d, s, e, att, loc in events:
            cur = state.db.execute("INSERT INTO events(organizer, title, start, end, location, created_via, created_at)"
                                   " VALUES(?,?,?,?,?,?,?)", (org, title, at(d, s), at(d, e), loc, "seed", now))
            state.db.executemany("INSERT INTO attendees VALUES(?,?)", [(cur.lastrowid, p) for p in att])
        for title, d in tasks:
            state.db.execute("INSERT INTO tasks(owner, title, due, created_via, created_at) VALUES(?,?,?,?,?)",
                             ("eto", title, (monday + timedelta(days=d)).isoformat(), "seed", now))


# ---------------------------------------------------------------- サーバー


def create_schedule_server(settings: Settings) -> MCPServer:
    kwargs = {}
    if settings.base_url:
        kwargs = dict(
            token_verifier=SharedTokenVerifier(AuthDB(settings.auth_db_path), settings.base_url),
            auth=AuthSettings(issuer_url=settings.base_url, resource_server_url=f"{settings.base_url}/schedule/mcp",
                              validate_token_resource=True),
        )
    mcp = MCPServer(name="browser-ai-schedule", title="サンプル商事 予定とタスク", instructions=INSTRUCTIONS,
                    version=__version__, **kwargs)
    for fn, title, annotations in TOOLS:
        mcp.tool(title=title, annotations=annotations)(fn)
    return mcp


def build_schedule_app(settings: Settings, sc: ScheduleSettings, clock=None):
    configure(settings, sc, clock)
    mcp = create_schedule_server(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.public_hosts,
                       *([urlparse(settings.base_url).netloc] if settings.base_url else [])],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    return RequestLog(mcp.streamable_http_app(transport_security=security, host=settings.host,
                                              streamable_http_path="/schedule/mcp"), path="/schedule/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings, sc = Settings.from_env(), ScheduleSettings.from_env()
    print(f"予定とタスク MCP: http://{settings.host}:{sc.port}/schedule/mcp")
    uvicorn.run(build_schedule_app(settings, sc), host=settings.host, port=sc.port)


if __name__ == "__main__":
    main()
