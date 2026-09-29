from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator


CAMPAIGN_CODE_RE = re.compile(r"\bRFQ-\d{4}-\d{4,}\b", re.IGNORECASE)
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class MailSettings:
    email_address: str
    password: str
    imap_host: str = "imap.mail.ru"
    imap_port: int = 993
    smtp_host: str = "smtp.mail.ru"
    smtp_port: int = 465


@dataclass(frozen=True)
class IncomingEnvelope:
    uid: int
    raw: bytes
    message: Message


@dataclass(frozen=True)
class FetchResult:
    uid_validity: str | None
    last_uid: int
    messages: list[IncomingEnvelope]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)

    def text(self) -> str:
        return "\n".join(self.parts)


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        return value


def normalize_message_id(value: str | None) -> str:
    if not value:
        return ""
    match = MESSAGE_ID_RE.search(value)
    return (match.group(0) if match else value.strip()).lower()


def message_reference_ids(message: Message) -> list[str]:
    values = [message.get("In-Reply-To", ""), message.get("References", "")]
    result: list[str] = []
    for value in values:
        for message_id in MESSAGE_ID_RE.findall(value):
            normalized = message_id.lower()
            if normalized not in result:
                result.append(normalized)
    return result


def subject_campaign_code(subject: str) -> str | None:
    match = CAMPAIGN_CODE_RE.search(subject)
    return match.group(0).upper() if match else None


def sender_email(message: Message) -> str:
    return parseaddr(decode_header_value(message.get("From")))[1].strip().lower()


def message_received_at(message: Message) -> str | None:
    value = message.get("Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.isoformat()
        return parsed.astimezone().replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def extract_message_text(message: Message, limit: int = 100_000) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts: Iterable[Message] = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeDecodeError, ValueError):
            payload = part.get_payload(decode=True) or b""
            value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(value, str):
            continue
        if content_type == "text/plain":
            plain.append(value)
        else:
            parser = _HTMLTextExtractor()
            parser.feed(value)
            html.append(parser.text())
    result = "\n\n".join(plain or html).strip()
    return result[:limit]


def attachment_filename(part: Message) -> str:
    return decode_header_value(part.get_filename())


def iter_named_attachments(message: Message) -> Iterable[tuple[str, str, bytes]]:
    for part in message.walk():
        filename = attachment_filename(part)
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        yield filename, part.get_content_type() or "application/octet-stream", payload


class MailGateway:
    def __init__(self, settings: MailSettings, timeout: int = 30) -> None:
        self.settings = settings
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()

    def test_connection(self) -> None:
        smtp = smtplib.SMTP_SSL(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.timeout,
            context=self.ssl_context,
        )
        try:
            smtp.login(self.settings.email_address, self.settings.password)
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                smtp.close()

        imap = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            ssl_context=self.ssl_context,
            timeout=self.timeout,
        )
        try:
            imap.login(self.settings.email_address, self.settings.password)
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Mail.ru не разрешил открыть папку Входящие")
        finally:
            try:
                imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    def build_message(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        campaign_code: str,
        attachment_paths: Iterable[Path],
    ) -> EmailMessage:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.settings.email_address
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        domain = self.settings.email_address.rsplit("@", 1)[-1]
        message["Message-ID"] = make_msgid(idstring=campaign_code, domain=domain)
        message["X-Campaign-ID"] = campaign_code
        message.set_content(body)
        for path in attachment_paths:
            payload = path.read_bytes()
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                maintype, subtype = "application", "pdf"
            elif suffix == ".xlsx":
                maintype, subtype = (
                    "application",
                    "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            elif suffix == ".xls":
                maintype, subtype = "application", "vnd.ms-excel"
            else:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=path.name)
        return message

    def send_message(self, message: EmailMessage) -> None:
        with self.smtp_session() as smtp:
            smtp.send_message(message)

    @contextmanager
    def smtp_session(self) -> Iterator[smtplib.SMTP_SSL]:
        smtp = smtplib.SMTP_SSL(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.timeout,
            context=self.ssl_context,
        )
        try:
            smtp.login(self.settings.email_address, self.settings.password)
            yield smtp
        finally:
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                smtp.close()

    def fetch_new_messages(
        self,
        *,
        last_uid: int,
        previous_uid_validity: str | None,
        since: date,
    ) -> FetchResult:
        imap = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            ssl_context=self.ssl_context,
            timeout=self.timeout,
        )
        try:
            imap.login(self.settings.email_address, self.settings.password)
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Не удалось открыть папку Входящие")
            uid_validity, uid_next = self._uid_state(imap)
            reset = bool(previous_uid_validity and uid_validity != previous_uid_validity)
            if last_uid > 0 and not reset:
                status, data = imap.uid("search", None, "UID", f"{last_uid + 1}:*")
            else:
                status, data = imap.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
            if status != "OK":
                raise RuntimeError("Mail.ru не выполнил поиск новых писем")
            uids = sorted(int(value) for value in (data[0] or b"").split() if value.isdigit())
            envelopes: list[IncomingEnvelope] = []
            first_failed_uid: int | None = None
            for uid in uids:
                status, fetched = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    first_failed_uid = min(first_failed_uid or uid, uid)
                    continue
                raw = self._raw_from_fetch(fetched)
                if not raw:
                    first_failed_uid = min(first_failed_uid or uid, uid)
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                envelopes.append(IncomingEnvelope(uid=uid, raw=raw, message=parsed))
            baseline = 0 if reset else last_uid
            if uids:
                next_cursor = max(baseline, uids[-1])
                if first_failed_uid is not None:
                    next_cursor = min(next_cursor, first_failed_uid - 1)
            else:
                next_cursor = max(baseline, (uid_next or 1) - 1)
            return FetchResult(
                uid_validity=uid_validity,
                last_uid=next_cursor,
                messages=envelopes,
            )
        finally:
            try:
                imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    @staticmethod
    def _uid_state(imap: imaplib.IMAP4_SSL) -> tuple[str | None, int | None]:
        # Читать UIDNEXT до SEARCH: новое письмо, поступившее между командами,
        # не должно оказаться по ту сторону сохранённого курсора.
        status, data = imap.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        if status != "OK" or not data or not data[0]:
            return None, None
        validity = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
        next_uid = re.search(rb"UIDNEXT\s+(\d+)", data[0])
        return (
            validity.group(1).decode("ascii") if validity else None,
            int(next_uid.group(1)) if next_uid else None,
        )

    @staticmethod
    def _raw_from_fetch(data: list[bytes | tuple[bytes, bytes] | None]) -> bytes:
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        return b""
