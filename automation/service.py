from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import smtplib
import threading
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Iterable

from .credentials import WindowsCredentialStore
from .database import Database, utc_now
from .mail_gateway import (
    DEFAULT_MAIL_PROVIDER,
    MAIL_PROVIDERS,
    MailGateway,
    MailSettings,
    decode_header_value,
    extract_message_text,
    iter_named_attachments,
    message_received_at,
    message_reference_ids,
    normalize_message_id,
    sender_email,
    subject_campaign_code,
)
from .paths import AppPaths


ALLOWED_RESPONSE_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
INVALID_WINDOWS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class MatchResult:
    relevant: bool
    campaign_id: int | None = None
    recipient_id: int | None = None
    method: str = "unmatched"
    needs_review: bool = False


@dataclass(frozen=True)
class OperationResult:
    message: str
    details: dict[str, int | str]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(value: str, fallback: str = "file") -> str:
    cleaned = INVALID_WINDOWS_CHARS_RE.sub("_", value).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:180]


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / safe_name(filename)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        alternative = directory / f"{stem} ({counter}){suffix}"
        if not alternative.exists():
            return alternative
        counter += 1


class AppService:
    def __init__(
        self,
        paths: AppPaths,
        database: Database,
        credentials: WindowsCredentialStore,
    ) -> None:
        self.paths = paths
        self.db = database
        self.credentials = credentials
        self.paths.ensure()
        self._mail_lock = threading.Lock()

    def get_mail_preferences(self) -> dict[str, str]:
        return {
            "email_address": self.db.get_setting("email_address"),
            "mail_provider": self._mail_provider_key(),
            "check_interval_minutes": self.db.get_setting("check_interval_minutes", "10"),
            "has_password": "1" if self.credentials.has_secret() else "0",
        }

    def _mail_provider_key(self) -> str:
        key = self.db.get_setting("mail_provider", DEFAULT_MAIL_PROVIDER)
        # Базы, созданные до выбора почты, работали только с Mail.ru.
        return key if key in MAIL_PROVIDERS else DEFAULT_MAIL_PROVIDER

    def save_mail_preferences(
        self,
        email_address: str,
        check_interval_minutes: int,
        password: str = "",
        mail_provider: str | None = None,
    ) -> None:
        email_address = email_address.strip().lower()
        if "@" not in email_address or email_address.startswith("@"):
            raise ValueError("Введите корректный адрес почты")
        if check_interval_minutes not in (5, 10, 15, 30, 60):
            raise ValueError("Недопустимый интервал проверки")
        current_provider = self._mail_provider_key()
        mail_provider = mail_provider or current_provider
        if mail_provider not in MAIL_PROVIDERS:
            raise ValueError("Неизвестный почтовый сервис")
        current = self.credentials.read()
        if not password and current and current[0].lower() != email_address:
            raise ValueError("При смене почтового адреса нужно заново указать пароль")
        if not password and current and mail_provider != current_provider:
            raise ValueError("При смене почтового сервиса нужно заново указать пароль")
        self.db.set_settings(
            {
                "email_address": email_address,
                "mail_provider": mail_provider,
                "check_interval_minutes": str(check_interval_minutes),
            }
        )
        if password:
            self.credentials.write(email_address, password)
        self.db.log(
            "info", "mail_settings_saved", None, {"email": email_address, "provider": mail_provider}
        )

    def delete_saved_password(self) -> None:
        self.credentials.delete()
        self.db.log("info", "mail_password_deleted", None, {})

    def mail_settings(self) -> MailSettings:
        email_address = self.db.get_setting("email_address").strip().lower()
        credential = self.credentials.read()
        if not email_address:
            raise ValueError("Сначала укажите адрес почты в Настройках")
        if not credential:
            raise ValueError("Сначала сохраните пароль почты в Настройках")
        username, password = credential
        if username.lower() != email_address:
            raise ValueError("Сохранённый пароль относится к другому почтовому адресу")
        return MailSettings.for_provider(self._mail_provider_key(), email_address, password)

    def test_mail_connection(self) -> OperationResult:
        with self._mail_lock:
            MailGateway(self.mail_settings()).test_connection()
        self.db.log("info", "mail_connection_tested", None, {"status": "ok"})
        provider = MAIL_PROVIDERS[self._mail_provider_key()]
        return OperationResult(f"Подключение к {provider.title} успешно", {})

    def create_and_send_campaign(
        self,
        *,
        name: str,
        subject: str,
        body: str,
        deadline: str | None,
        supplier_ids: Iterable[int],
        attachment_paths: Iterable[str],
        request_path: str | None = None,
    ) -> OperationResult:
        if not name.strip() or not subject.strip() or not body.strip():
            raise ValueError("Заполните название, тему и текст письма")
        # Проверяем настройки до создания записи, чтобы ошибка конфигурации не
        # оставляла лишнюю рассылку-черновик.
        self.mail_settings()
        normalized_deadline = self._validate_deadline(deadline)
        request_source = Path(request_path) if request_path else None
        if request_source and (not request_source.is_file() or request_source.suffix.lower() != ".xlsx"):
            raise ValueError("Выберите исходную заявку XLSX")
        source_files = [Path(value) for value in attachment_paths]
        if request_source and all(path.resolve() != request_source.resolve() for path in source_files):
            source_files.insert(0, request_source)
        for path in source_files:
            if not path.is_file():
                raise ValueError(f"Файл не найден: {path}")

        campaign_id = self.db.create_campaign(
            name=name,
            subject=subject,
            body=body,
            deadline=normalized_deadline,
            supplier_ids=supplier_ids,
        )
        campaign = self.db.get_campaign(campaign_id)
        assert campaign is not None
        campaign_root = self.paths.campaigns / campaign["code"]
        outbound_dir = campaign_root / "outgoing" / "attachments"
        for source in source_files:
            destination = unique_path(outbound_dir, source.name)
            shutil.copy2(source, destination)
            self.db.add_campaign_attachment(
                campaign_id,
                destination.name,
                str(destination),
                destination.stat().st_size,
                sha256_file(destination),
            )
            if request_source and source.resolve() == request_source.resolve():
                self.db.set_campaign_request_path(campaign_id, str(destination))
        return self.send_campaign(campaign_id)

    def send_campaign(self, campaign_id: int) -> OperationResult:
        campaign = self.db.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Рассылка не найдена")
        recipients = self.db.list_recipients(campaign_id)
        unsent = [row for row in recipients if row["status"] in ("pending", "send_failed")]
        # Поставщика могли исключить уже после создания рассылки.
        pending = [row for row in unsent if not row["supplier_excluded"]]
        skipped = len(unsent) - len(pending)
        if not pending:
            if skipped:
                raise ValueError("Все неотправленные адресаты исключены из рассылок")
            raise ValueError("В этой рассылке нет неотправленных адресатов")
        attachment_paths = [
            Path(row["path"]) for row in self.db.list_outgoing_attachments(campaign_id)
        ]
        subject = campaign["subject"]
        if campaign["code"].lower() not in subject.lower():
            subject = f"{subject} [{campaign['code']}]"

        sent = 0
        failed = 0
        unknown = 0
        with self._mail_lock:
            gateway = MailGateway(self.mail_settings())
            with gateway.smtp_session() as smtp:
                for recipient in pending:
                    attempt_recorded = False
                    try:
                        message = gateway.build_message(
                            recipient=recipient["email"],
                            subject=subject,
                            body=campaign["body"],
                            campaign_code=campaign["code"],
                            attachment_paths=attachment_paths,
                        )
                        raw_dir = self.paths.campaigns / campaign["code"] / "outgoing" / "messages"
                        raw_path = unique_path(
                            raw_dir,
                            f"{recipient['id']}_{safe_name(recipient['email'], 'recipient')}.eml",
                        )
                        raw_path.write_bytes(message.as_bytes())
                        message_id = normalize_message_id(message["Message-ID"])
                        self.db.record_outgoing_attempt(
                            campaign_id, int(recipient["id"]), message_id, subject, str(raw_path)
                        )
                        attempt_recorded = True
                        smtp.send_message(message)
                        self.db.confirm_outgoing(message_id)
                        sent += 1
                    except Exception as exc:
                        if attempt_recorded:
                            if isinstance(
                                exc,
                                (
                                    smtplib.SMTPRecipientsRefused,
                                    smtplib.SMTPSenderRefused,
                                    smtplib.SMTPDataError,
                                ),
                            ):
                                failed += 1
                                self.db.mark_outgoing_rejected(
                                    message_id, int(recipient["id"]), str(exc)
                                )
                            else:
                                unknown += 1
                                self.db.mark_send_unknown(int(recipient["id"]), str(exc))
                                break
                        else:
                            failed += 1
                            self.db.mark_send_failed(int(recipient["id"]), str(exc))
        self.db.log(
            "info" if not failed and not unknown else "warning",
            "campaign_send_finished",
            campaign_id,
            {"sent": sent, "failed": failed, "unknown": unknown, "skipped_excluded": skipped},
        )
        message = f"Отправлено: {sent}. Ошибок до отправки: {failed}. Исход неизвестен: {unknown}."
        if skipped:
            message += f" Пропущено исключённых: {skipped}."
        return OperationResult(
            message,
            {"campaign_id": campaign_id, "sent": sent, "failed": failed, "unknown": unknown,
             "skipped_excluded": skipped},
        )

    def check_mail(self, trigger_name: str = "manual") -> OperationResult:
        if not self._mail_lock.acquire(blocking=False):
            return OperationResult("Проверка почты уже выполняется", {"busy": 1})
        check_id = self.db.start_mail_check(trigger_name)
        new_messages = 0
        new_attachments = 0
        try:
            if self.db.active_campaign_count() == 0:
                self.db.finish_mail_check(check_id, "no_active_campaigns")
                return OperationResult("Нет активных рассылок для проверки", {})
            settings = self.mail_settings()
            state = self.db.get_sync_state(settings.email_address)
            last_uid = int(state["last_uid"]) if state else 0
            uid_validity = state["uid_validity"] if state else None
            since = self._mail_scan_start_date()
            fetched = MailGateway(settings).fetch_new_messages(
                last_uid=last_uid,
                previous_uid_validity=uid_validity,
                since=since,
            )
            if not fetched.uid_validity:
                raise RuntimeError("IMAP не сообщил UIDVALIDITY; проверка не может безопасно продолжаться")
            for envelope in fetched.messages:
                result = self._process_incoming(
                    mailbox=settings.email_address,
                    folder="INBOX",
                    uid_validity=fetched.uid_validity,
                    uid=envelope.uid,
                    message=envelope.message,
                    raw=envelope.raw,
                )
                if result is None:
                    continue
                created, attachment_count = result
                new_messages += int(created)
                new_attachments += attachment_count
            self.db.set_sync_state(
                settings.email_address,
                "INBOX",
                fetched.uid_validity,
                fetched.last_uid,
            )
            self.db.finish_mail_check(
                check_id,
                "ok",
                new_messages=new_messages,
                new_attachments=new_attachments,
            )
            self.db.log(
                "info",
                "mail_check_finished",
                None,
                {
                    "trigger": trigger_name,
                    "new_messages": new_messages,
                    "new_attachments": new_attachments,
                },
            )
            return OperationResult(
                f"Проверка завершена. Новых ответов: {new_messages}, файлов: {new_attachments}.",
                {"new_messages": new_messages, "new_attachments": new_attachments},
            )
        except Exception as exc:
            self.db.finish_mail_check(check_id, "error", new_messages, new_attachments, str(exc))
            self.db.log("error", "mail_check_failed", None, {"error": str(exc)})
            raise
        finally:
            self._mail_lock.release()

    def _process_incoming(
        self, *, mailbox: str, folder: str, uid_validity: str, uid: int,
        message: Message, raw: bytes
    ) -> tuple[bool, int] | None:
        existing = self.db.get_incoming_by_uid(mailbox, folder, uid_validity, uid)
        if existing and existing["processed_at"]:
            return False, 0
        subject = decode_header_value(message.get("Subject"))
        sender = sender_email(message)
        received_at = message_received_at(message)
        if existing:
            match = MatchResult(
                relevant=True,
                campaign_id=existing["campaign_id"],
                recipient_id=existing["recipient_id"],
                method=existing["match_method"],
                needs_review=bool(existing["needs_review"]),
            )
        else:
            match = self._match_message(message, subject, sender, received_at)
        if not match.relevant:
            return None

        campaign = self.db.get_campaign(match.campaign_id) if match.campaign_id else None
        code = campaign["code"] if campaign else "unmatched"
        if existing:
            incoming_id = int(existing["id"])
        else:
            raw_dir = (
                self.paths.campaigns / code / "incoming" / "raw"
                if campaign
                else self.paths.unmatched / "raw"
            )
            raw_path = unique_path(raw_dir, f"uid-{uid}.eml")
            raw_path.write_bytes(raw)
            incoming_id, created = self.db.insert_incoming(
                mailbox=mailbox,
                folder=folder,
                uid_validity=uid_validity,
                imap_uid=uid,
                campaign_id=match.campaign_id,
                recipient_id=match.recipient_id,
                message_id=normalize_message_id(message.get("Message-ID")),
                in_reply_to=message.get("In-Reply-To", ""),
                sender_email=sender,
                subject=subject,
                received_at=received_at,
                body_text=extract_message_text(message),
                raw_eml_path=str(raw_path),
                match_method=match.method,
                needs_review=match.needs_review,
            )
            if not created:
                return False, 0

        attachment_count = 0
        attachment_dir = (
            self.paths.campaigns / code / "incoming" / f"recipient-{match.recipient_id or 'review'}"
            if campaign
            else self.paths.unmatched / "attachments"
        )
        for filename, content_type, payload in iter_named_attachments(message):
            digest = sha256_bytes(payload)
            clean_filename = safe_name(filename)
            already_saved = self.db.find_incoming_attachment(incoming_id, digest, clean_filename)
            if already_saved:
                if not Path(already_saved["path"]).is_file():
                    raise OSError(f"Ранее сохранённое вложение недоступно: {clean_filename}")
                continue
            suffix = Path(filename).suffix.lower()
            allowed = suffix in ALLOWED_RESPONSE_EXTENSIONS
            duplicate = self.db.find_attachment_duplicate(match.recipient_id, digest)
            if duplicate:
                stored_path = Path(duplicate["path"])
                duplicate_id = int(duplicate["id"])
            else:
                stored_path = unique_path(attachment_dir, filename)
                stored_path.write_bytes(payload)
                duplicate_id = None
            attachment_id = self.db.add_incoming_attachment(
                incoming_message_id=incoming_id,
                filename=clean_filename,
                content_type=content_type,
                size=len(payload),
                sha256=digest,
                path=str(stored_path),
                is_allowed=allowed,
                duplicate_of_id=duplicate_id,
            )
            if attachment_id:
                attachment_count += 1
        self.db.finalize_incoming(incoming_id)
        self.db.mark_incoming_complete(incoming_id)
        self.db.log(
            "info" if not match.needs_review else "warning",
            "incoming_saved",
            match.campaign_id,
            {
                "incoming_id": incoming_id,
                "sender": sender,
                "attachments": attachment_count,
                "match_method": match.method,
                "needs_review": match.needs_review,
            },
        )
        return True, attachment_count

    def _match_message(
        self, message: Message, subject: str, sender: str, received_at: str | None
    ) -> MatchResult:
        outgoing = self.db.find_outgoing_by_references(message_reference_ids(message))
        if outgoing:
            return MatchResult(
                relevant=True,
                campaign_id=int(outgoing["campaign_id"]),
                recipient_id=int(outgoing["recipient_id"]),
                method="reply_headers",
            )

        code = subject_campaign_code(subject)
        if code:
            campaign = self.db.get_campaign_by_code(code)
            if campaign and campaign["status"] in ("active", "ready"):
                recipient = self.db.find_recipient_in_campaign(int(campaign["id"]), sender)
                return MatchResult(
                    relevant=True,
                    campaign_id=int(campaign["id"]),
                    recipient_id=int(recipient["id"]) if recipient else None,
                    method="subject_code",
                    needs_review=recipient is None,
                )

        candidates = self.db.find_active_recipients_by_sender(sender)
        candidates = [row for row in candidates if self._message_is_after_campaign(row, received_at)]
        if len(candidates) == 1:
            row = candidates[0]
            return MatchResult(
                relevant=True,
                campaign_id=int(row["campaign_id"]),
                recipient_id=int(row["id"]),
                method="sender_single_active",
            )
        if len(candidates) > 1:
            return MatchResult(relevant=True, method="sender_ambiguous", needs_review=True)
        return MatchResult(relevant=False)

    @staticmethod
    def _message_is_after_campaign(recipient_row: sqlite3.Row, received_at: str | None) -> bool:
        sent_at = recipient_row["sent_at"] or recipient_row["campaign_sent_at"]
        if not sent_at or not received_at:
            return True
        try:
            sent = datetime.fromisoformat(sent_at)
            received = datetime.fromisoformat(received_at)
            if sent.tzinfo and not received.tzinfo:
                received = received.replace(tzinfo=sent.tzinfo)
            if received.tzinfo and not sent.tzinfo:
                sent = sent.replace(tzinfo=received.tzinfo)
            return received >= sent - timedelta(minutes=5)
        except ValueError:
            return True

    def _mail_scan_start_date(self) -> date:
        earliest = self.db.earliest_active_sent_at()
        if earliest:
            try:
                return datetime.fromisoformat(earliest).date() - timedelta(days=2)
            except ValueError:
                pass
        return date.today() - timedelta(days=30)

    @staticmethod
    def _validate_deadline(value: str | None) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("Срок ответа должен быть в формате ГГГГ-ММ-ДД") from exc

    def assign_incoming(self, incoming_id: int, recipient_id: int) -> None:
        self.db.assign_incoming(incoming_id, recipient_id)

    def create_summary(self, campaign_id: int, request_path: Path | None = None) -> OperationResult:
        from .summary import OfferSource, build_summary

        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            raise ValueError("Рассылка не найдена")
        if campaign["status"] not in ("ready", "closed"):
            raise ValueError("Дождитесь ответов или завершите ожидание адресатов")
        if request_path is None:
            stored = campaign["request_path"]
            if stored and Path(stored).is_file():
                request_path = Path(stored)
            else:
                candidates = [Path(row["path"]) for row in self.db.list_outgoing_attachments(campaign_id)
                              if Path(row["filename"]).suffix.lower() == ".xlsx"]
                if len(candidates) == 1:
                    request_path = candidates[0]
                else:
                    raise ValueError("Для сводной выберите исходную заявку XLSX")
        request_path = Path(request_path)
        if not request_path.is_file() or request_path.suffix.lower() != ".xlsx":
            raise ValueError("Выберите исходную заявку в формате XLSX")
        if not campaign["request_path"] or not Path(campaign["request_path"]).is_file():
            source_dir = self.paths.campaigns / campaign["code"] / "summary" / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            destination = unique_path(source_dir, request_path.name)
            shutil.copy2(request_path, destination)
            request_path = destination
            self.db.set_campaign_request_path(campaign_id, str(destination))
        sources = [
            OfferSource(Path(row["path"]), str(row["supplier_name"]))
            for row in self.db.list_selected_offer_attachments(campaign_id)
        ]
        if not sources:
            raise ValueError("У рассылки нет привязанных счетов PDF/XLSX")
        output_dir = self.paths.campaigns / campaign["code"] / "summary"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output = output_dir / f"{campaign['code']}-summary-{timestamp}.xlsx"
        result = build_summary(request_path, sources, output)
        self.db.log("info", "summary_created", campaign_id, {
            "path": str(output), "request_rows": len(result.request_lines),
            "offer_lines": len(result.allocations), "review_items": len(result.issues),
        })
        return OperationResult(
            f"Сводная создана: {output.name}. Строк на проверке: {len(result.issues)}.",
            {"path": str(output), "review_items": len(result.issues)},
        )

    def create_backup(self) -> OperationResult:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = self.paths.backups / f"backup-{timestamp}.zip"
        snapshot = self.paths.backups / f"app-{timestamp}.db"
        self.db.backup_to(snapshot)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                package.write(snapshot, "app.db")
                for base in (self.paths.campaigns, self.paths.unmatched):
                    if not base.exists():
                        continue
                    for path in base.rglob("*"):
                        if path.is_file():
                            package.write(path, path.relative_to(self.paths.root))
        finally:
            snapshot.unlink(missing_ok=True)
        self.db.log("info", "backup_created", None, {"path": str(archive)})
        return OperationResult(f"Резервная копия создана: {archive.name}", {"path": str(archive)})
