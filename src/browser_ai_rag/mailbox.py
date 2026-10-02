"""受信箱（第20〜21章）。本物の IMAP/SMTP と、フォルダを受信箱に見立てた偽物を、同じ形で扱う。

偽物（FolderMailbox）は、メールサーバーがなくても本の手順を試せるようにするためのもの。
data/mailbox/INBOX/ に .eml ファイルを置けば受信、送ったメールは data/mailbox/Sent/ に書き出される。
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path


@dataclass
class Message:
    id: str               # 受信箱の中での番号（IMAP の UID、偽物ならファイル名）
    sender: str
    to: str
    subject: str
    date: str
    body: str
    message_id: str       # メールそのものに付いている Message-ID（返信のときに使う）
    unread: bool = False


def _text_of(msg: email.message.EmailMessage) -> str:
    """本文を文字にする。プレーンテキストがあればそれを、なければ HTML からタグを取ったものを使う。"""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _parse(raw: bytes, id_: str, unread: bool = False) -> Message:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        date = parsedate_to_datetime(msg["Date"]).astimezone().strftime("%Y-%m-%d %H:%M") if msg["Date"] else ""
    except (TypeError, ValueError):
        date = ""
    return Message(id=id_, sender=str(msg["From"] or ""), to=str(msg["To"] or ""), subject=str(msg["Subject"] or ""),
                   date=date, body=_text_of(msg), message_id=str(msg["Message-ID"] or ""), unread=unread)


def build_message(sender: str, to: list[str], subject: str, body: str, in_reply_to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, ", ".join(to), subject
    m["Date"] = format_datetime(datetime.now(timezone.utc))
    m["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    return m


class FolderMailbox:
    """フォルダを受信箱に見立てた偽物。"""

    def __init__(self, root: Path, address: str = "me@example.com") -> None:
        self.root, self.address = root, address
        (root / "INBOX").mkdir(parents=True, exist_ok=True)
        (root / "Sent").mkdir(parents=True, exist_ok=True)

    def list(self, limit: int = 20) -> list[Message]:
        files = sorted((self.root / "INBOX").glob("*.eml"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [_parse(p.read_bytes(), p.stem) for p in files[:limit]]

    def get(self, id_: str) -> Message | None:
        p = self.root / "INBOX" / f"{Path(id_).name}.eml"
        return _parse(p.read_bytes(), p.stem) if p.exists() else None

    def search(self, text: str, limit: int = 20) -> list[Message]:
        return [m for m in self.list(limit=500) if text in m.subject or text in m.body or text in m.sender][:limit]

    def send(self, msg: EmailMessage) -> None:
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (self.root / "Sent" / f"{name}.eml").write_bytes(msg.as_bytes())


class ImapMailbox:
    """本物の受信箱。IMAP で読み、SMTP（587番・STARTTLS）で送る。

    tls_name は、証明書を確かめるときの名前。接続先の名前と証明書の名前が違うメールサーバー
    （例：mail.example.com に接続するが、証明書は example.com 用）のときだけ設定する。
    """

    def __init__(self, host: str, user: str, password: str, smtp_host: str | None = None,
                 tls_name: str | None = None) -> None:
        self.host, self.user, self.password = host, user, password
        self.smtp_host, self.tls_name = smtp_host or host, tls_name or host
        self.address = user
        self.ctx = ssl.create_default_context()

    def _imap(self) -> imaplib.IMAP4_SSL:
        ctx, name = self.ctx, self.tls_name

        class _IMAP(imaplib.IMAP4_SSL):
            def _create_socket(self, timeout):
                return ctx.wrap_socket(socket.create_connection((self.host, self.port), timeout), server_hostname=name)

        im = _IMAP(self.host, 993, ssl_context=ctx, timeout=20)
        im.login(self.user, self.password)
        im.select("INBOX")
        return im

    def _fetch(self, im, uids: list[bytes]) -> list[Message]:
        out = []
        for uid in uids:
            typ, data = im.uid("fetch", uid, "(FLAGS BODY.PEEK[])")  # PEEK: 読んでも既読にしない
            if typ != "OK" or not data or data[0] is None:
                continue
            flags = data[0][0].decode(errors="replace")
            out.append(_parse(data[0][1], uid.decode(), unread="\\Seen" not in flags))
        return out

    def list(self, limit: int = 20) -> list[Message]:
        im = self._imap()
        try:
            typ, data = im.uid("search", None, "ALL")
            uids = data[0].split()[-limit:][::-1]
            return self._fetch(im, uids)
        finally:
            im.logout()

    def get(self, id_: str) -> Message | None:
        if not id_.isdigit():
            return None
        im = self._imap()
        try:
            msgs = self._fetch(im, [id_.encode()])
            return msgs[0] if msgs else None
        finally:
            im.logout()

    def search(self, text: str, limit: int = 20) -> list[Message]:
        im = self._imap()
        try:
            im._encoding = "utf-8"
            typ, data = im.uid("search", "CHARSET", "UTF-8", "TEXT", text.encode("utf-8"))
            uids = data[0].split()[-limit:][::-1] if typ == "OK" else []
            return self._fetch(im, uids)
        finally:
            im.logout()

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.smtp_host, 587, timeout=30) as s:
            s._host = self.tls_name  # STARTTLS で確かめる証明書の名前
            s.starttls(context=self.ctx)
            s.login(self.user, self.password)
            s.send_message(msg)


def recipients_allowed(addresses: list[str], allowed: list[str]) -> list[str]:
    """許可されていない宛先を返す。allowed には完全なアドレスか「@ドメイン」を書く。"""
    bad = []
    for a in addresses:
        addr = parseaddr(a)[1].lower()
        if not addr or not any(addr == x.lower() or (x.startswith("@") and addr.endswith(x.lower())) for x in allowed):
            bad.append(a)
    return bad
