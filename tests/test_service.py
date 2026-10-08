from __future__ import annotations

import json
import tempfile
import unittest
import smtplib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from automation.database import Database
from automation.mail_gateway import MailGateway
from automation.paths import AppPaths
from automation.service import AppService
from automation.ui import incoming_result_label


class DummyCredentialStore:
    def read(self):
        return None

    def has_secret(self):
        return False


class TestCredentialStore:
    def read(self):
        return "buyer@mail.ru", "test-secret"

    def has_secret(self):
        return True


class ServiceIncomingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.from_root(Path(self.temp.name))
        self.db = Database(self.paths.database)
        self.service = AppService(self.paths, self.db, DummyCredentialStore())
        supplier_id = self.db.save_supplier("Поставщик", "supplier@example.ru")
        self.campaign_id = self.db.create_campaign(
            "Закупка", "Предложение", "Пришлите предложение", None, [supplier_id]
        )
        self.recipient = self.db.list_recipients(self.campaign_id)[0]
        self.db.record_outgoing(
            self.campaign_id,
            int(self.recipient["id"]),
            "<outgoing@example.ru>",
            "Предложение [RFQ-2026-0001]",
            "outgoing.eml",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _reply() -> EmailMessage:
        message = EmailMessage(policy=policy.default)
        message["From"] = "supplier@example.ru"
        message["To"] = "buyer@mail.ru"
        message["Subject"] = "Re: Предложение [RFQ-2026-0001]"
        message["Message-ID"] = "<incoming@example.ru>"
        message["In-Reply-To"] = "<outgoing@example.ru>"
        message.set_content("Добрый день, предложение во вложении.")
        message.add_attachment(
            b"%PDF-test",
            maintype="application",
            subtype="pdf",
            filename="offer.pdf",
        )
        return message

    def test_reply_with_pdf_completes_recipient_and_saves_files(self) -> None:
        message = self._reply()
        raw = message.as_bytes()
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        result = self.service._process_incoming(
            mailbox="buyer@mail.ru",
            folder="INBOX",
            uid_validity="42",
            uid=101,
            message=parsed,
            raw=raw,
        )
        self.assertEqual(result, (True, 1))
        recipient = self.db.get_recipient(int(self.recipient["id"]))
        self.assertEqual(recipient["status"], "files_received")
        incoming = self.db.list_incoming()[0]
        self.assertEqual(incoming["match_method"], "reply_headers")
        attachment = self.db.list_incoming_attachments(int(incoming["id"]))[0]
        self.assertTrue(Path(attachment["path"]).is_file())
        self.assertEqual(attachment["is_allowed"], 1)

    def test_same_uid_is_idempotent(self) -> None:
        message = self._reply()
        raw = message.as_bytes()
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        first = self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42",
            uid=102, message=parsed, raw=raw
        )
        second = self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42",
            uid=102, message=parsed, raw=raw
        )
        self.assertEqual(first, (True, 1))
        self.assertEqual(second, (False, 0))
        self.assertEqual(len(self.db.list_incoming()), 1)

    def test_text_only_reply_does_not_erase_earlier_file_status(self) -> None:
        first = self._reply()
        first_raw = first.as_bytes()
        self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42", uid=103,
            message=BytesParser(policy=policy.default).parsebytes(first_raw), raw=first_raw,
        )

        second = EmailMessage(policy=policy.default)
        second["From"] = "supplier@example.ru"
        second["To"] = "buyer@mail.ru"
        second["Subject"] = "Re: Предложение [RFQ-2026-0001]"
        second["Message-ID"] = "<text-only@example.ru>"
        second["In-Reply-To"] = "<outgoing@example.ru>"
        second.set_content("Спасибо, получили.")
        second_raw = second.as_bytes()
        self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42", uid=104,
            message=BytesParser(policy=policy.default).parsebytes(second_raw), raw=second_raw,
        )

        self.assertEqual(self.db.get_recipient(int(self.recipient["id"]))["status"],
                         "files_received")
        text_only = self.db.get_incoming_by_uid("buyer@mail.ru", "INBOX", "42", 104)
        rows = {row["id"]: row for row in self.db.list_incoming()}
        self.assertEqual(rows[text_only["id"]]["attachment_count"], 0)
        self.assertEqual(rows[text_only["id"]]["allowed_count"], 0)
        self.assertEqual(incoming_result_label(rows[text_only["id"]]), "Ответ без файлов")
        with_file = self.db.get_incoming_by_uid("buyer@mail.ru", "INBOX", "42", 103)
        self.assertEqual(incoming_result_label(rows[with_file["id"]]), "Файлы получены")

    def test_first_text_only_reply_marks_recipient_waiting_for_files(self) -> None:
        message = EmailMessage(policy=policy.default)
        message["From"] = "supplier@example.ru"
        message["To"] = "buyer@mail.ru"
        message["Subject"] = "Re: Предложение [RFQ-2026-0001]"
        message["Message-ID"] = "<plain-only@example.ru>"
        message["In-Reply-To"] = "<outgoing@example.ru>"
        message.set_content("Ответим позже с предложением.")
        raw = message.as_bytes()
        self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42", uid=106,
            message=BytesParser(policy=policy.default).parsebytes(raw), raw=raw,
        )
        self.assertEqual(
            self.db.get_recipient(int(self.recipient["id"]))["status"],
            "reply_without_files",
        )
        self.assertEqual(incoming_result_label(self.db.list_incoming()[0]), "Ответ без файлов")

    def test_interrupted_attachment_save_is_resumed(self) -> None:
        message = self._reply()
        message.add_attachment(
            b"second-file", maintype="application", subtype="pdf", filename="second.pdf"
        )
        raw = message.as_bytes()
        parsed = BytesParser(policy=policy.default).parsebytes(raw)

        def interrupted_parts(_message):
            yield "offer.pdf", "application/pdf", b"%PDF-test"
            raise OSError("Диск временно недоступен")

        with patch("automation.service.iter_named_attachments", interrupted_parts):
            with self.assertRaises(OSError):
                self.service._process_incoming(
                    mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42",
                    uid=105, message=parsed, raw=raw
                )

        self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42",
            uid=105, message=parsed, raw=raw
        )
        incoming = self.db.list_incoming()
        self.assertEqual(len(incoming), 1)
        attachments = self.db.list_incoming_attachments(int(incoming[0]["id"]))
        self.assertEqual({item["filename"] for item in attachments}, {"offer.pdf", "second.pdf"})
        self.assertTrue(all(Path(item["path"]).is_file() for item in attachments))
        self.assertEqual(self.db.get_recipient(int(self.recipient["id"]))["status"], "files_received")

    def test_uncertain_smtp_result_is_not_retried(self) -> None:
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})
        supplier_id = self.db.save_supplier("Другой", "other@example.ru")
        campaign_id = self.db.create_campaign("Новая", "Запрос", "Текст", None, [supplier_id])
        recipient_id = int(self.db.list_recipients(campaign_id)[0]["id"])
        actual_build = MailGateway(self.service.mail_settings()).build_message

        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            gateway.smtp_session.return_value.__enter__.return_value.send_message.side_effect = (
                ConnectionResetError("Соединение потеряно после передачи письма")
            )
            first = self.service.send_campaign(campaign_id)
            self.assertEqual(first.details["sent"], 0)
            self.assertEqual(self.db.get_recipient(recipient_id)["status"], "send_unknown")
            self.assertEqual(
                Database(self.paths.database).get_recipient(recipient_id)["status"],
                "send_unknown",
            )
            with self.assertRaises(ValueError):
                self.service.send_campaign(campaign_id)
            self.assertEqual(
                gateway.smtp_session.return_value.__enter__.return_value.send_message.call_count,
                1,
            )

    def test_confirmed_smtp_send_is_recorded_and_not_retried(self) -> None:
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})
        supplier_id = self.db.save_supplier("Другой", "other@example.ru")
        campaign_id = self.db.create_campaign("Новая", "Запрос", "Текст", None, [supplier_id])
        recipient_id = int(self.db.list_recipients(campaign_id)[0]["id"])
        actual_build = MailGateway(self.service.mail_settings()).build_message

        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            first = self.service.send_campaign(campaign_id)
            self.assertEqual(first.details, {
                "campaign_id": campaign_id, "sent": 1, "failed": 0, "unknown": 0,
                "skipped_excluded": 0,
            })
            self.assertEqual(self.db.get_recipient(recipient_id)["status"], "sent")
            with self.db.connect() as connection:
                outgoing = connection.execute(
                    "SELECT * FROM outgoing_messages WHERE recipient_id = ?", (recipient_id,)
                ).fetchone()
            self.assertEqual(outgoing["delivery_status"], "sent")
            self.assertTrue(Path(outgoing["raw_eml_path"]).is_file())
            with self.assertRaises(ValueError):
                self.service.send_campaign(campaign_id)
            self.assertEqual(
                gateway.smtp_session.return_value.__enter__.return_value.send_message.call_count,
                1,
            )

    def test_confirmed_smtp_rejection_can_be_retried(self) -> None:
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})
        supplier_id = self.db.save_supplier("Другой", "other@example.ru")
        campaign_id = self.db.create_campaign("Новая", "Запрос", "Текст", None, [supplier_id])
        recipient_id = int(self.db.list_recipients(campaign_id)[0]["id"])
        actual_build = MailGateway(self.service.mail_settings()).build_message

        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            send = gateway.smtp_session.return_value.__enter__.return_value.send_message
            send.side_effect = [
                smtplib.SMTPRecipientsRefused({"other@example.ru": (550, b"rejected")}),
                None,
            ]
            self.service.send_campaign(campaign_id)
            self.assertEqual(self.db.get_recipient(recipient_id)["status"], "send_failed")
            self.service.send_campaign(campaign_id)
            self.assertEqual(self.db.get_recipient(recipient_id)["status"], "sent")
            self.assertEqual(send.call_count, 2)

    def test_supplier_excluded_after_campaign_creation_is_not_sent(self) -> None:
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})
        kept_id = self.db.save_supplier("Оставить", "keep@example.ru")
        blocked_id = self.db.save_supplier("Исключить", "block@example.ru")
        campaign_id = self.db.create_campaign(
            "Новая", "Запрос", "Текст", None, [kept_id, blocked_id]
        )
        self.db.save_supplier("Исключить", "block@example.ru", supplier_id=blocked_id,
                              excluded=True, excluded_reason="Попросили не писать")
        actual_build = MailGateway(self.service.mail_settings()).build_message

        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            send = gateway.smtp_session.return_value.__enter__.return_value.send_message
            result = self.service.send_campaign(campaign_id)

        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0]["To"], "keep@example.ru")
        self.assertEqual(result.details["skipped_excluded"], 1)
        statuses = {row["email"]: row["status"] for row in self.db.list_recipients(campaign_id)}
        self.assertEqual(statuses, {"keep@example.ru": "sent", "block@example.ru": "pending"})

    def test_connection_loss_stops_batch_before_next_recipient(self) -> None:
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})
        first_id = self.db.save_supplier("А", "a@example.ru")
        second_id = self.db.save_supplier("Б", "b@example.ru")
        campaign_id = self.db.create_campaign(
            "Новая", "Запрос", "Текст", None, [first_id, second_id]
        )
        actual_build = MailGateway(self.service.mail_settings()).build_message

        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            send = gateway.smtp_session.return_value.__enter__.return_value.send_message
            send.side_effect = ConnectionResetError("Соединение потеряно")
            self.service.send_campaign(campaign_id)

        statuses = [row["status"] for row in self.db.list_recipients(campaign_id)]
        self.assertEqual(statuses.count("send_unknown"), 1)
        self.assertEqual(statuses.count("pending"), 1)
        self.assertEqual(send.call_count, 1)

    def test_request_is_saved_once_and_added_to_outgoing_files(self) -> None:
        request = Path(self.temp.name) / "request.xlsx"
        request.write_bytes(b"test request")
        with patch.object(self.service, "mail_settings", return_value=object()), \
             patch.object(self.service, "send_campaign"):
            self.service.create_and_send_campaign(
                name="Новая", subject="Запрос", body="Пришлите предложение",
                deadline=None, supplier_ids=[int(self.recipient["supplier_id"])],
                attachment_paths=[], request_path=str(request),
            )
        campaign = self.db.list_campaigns()[0]
        saved_request = Path(campaign["request_path"])
        self.assertTrue(saved_request.is_file())
        self.assertEqual(saved_request.read_bytes(), request.read_bytes())
        outgoing = self.db.list_outgoing_attachments(int(campaign["id"]))
        self.assertEqual([Path(row["path"]) for row in outgoing], [saved_request])


class MemoryCredentialStore:
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self.value = (username, password) if username else None

    def read(self):
        return self.value

    def write(self, username, password):
        self.value = (username, password)

    def has_secret(self):
        return self.value is not None


class MailProviderSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.from_root(Path(self.temp.name))
        self.db = Database(self.paths.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_existing_setup_defaults_to_mailru(self) -> None:
        service = AppService(self.paths, self.db, MemoryCredentialStore("buyer@mail.ru", "secret"))
        self.db.set_settings({"email_address": "buyer@mail.ru"})

        settings = service.mail_settings()

        self.assertEqual(service.get_mail_preferences()["mail_provider"], "mailru")
        self.assertEqual((settings.imap_host, settings.smtp_host), ("imap.mail.ru", "smtp.mail.ru"))

    def test_timeweb_uses_timeweb_servers(self) -> None:
        service = AppService(self.paths, self.db, MemoryCredentialStore())
        service.save_mail_preferences("Buyer@Company.ru", 10, "secret", "timeweb")

        settings = service.mail_settings()

        self.assertEqual(settings.email_address, "buyer@company.ru")
        self.assertEqual((settings.imap_host, settings.imap_port), ("imap.timeweb.ru", 993))
        self.assertEqual((settings.smtp_host, settings.smtp_port), ("smtp.timeweb.ru", 465))

    def test_changing_provider_requires_password(self) -> None:
        service = AppService(self.paths, self.db, MemoryCredentialStore())
        service.save_mail_preferences("buyer@company.ru", 10, "secret", "mailru")

        with self.assertRaises(ValueError):
            service.save_mail_preferences("buyer@company.ru", 10, "", "timeweb")
        service.save_mail_preferences("buyer@company.ru", 15, "", "mailru")

        self.assertEqual(service.get_mail_preferences()["check_interval_minutes"], "15")

    def test_unknown_provider_is_rejected(self) -> None:
        service = AppService(self.paths, self.db, MemoryCredentialStore())

        with self.assertRaises(ValueError):
            service.save_mail_preferences("buyer@company.ru", 10, "secret", "gmail")


class ReplyToSupplierTests(unittest.TestCase):
    """Ответ поставщику продолжает цепочку письма и не меняет состояние адресата."""

    tearDown = ServiceIncomingTests.tearDown
    _reply = staticmethod(ServiceIncomingTests._reply)

    def setUp(self) -> None:
        ServiceIncomingTests.setUp(self)
        self.service.credentials = TestCredentialStore()
        self.db.set_settings({"email_address": "buyer@mail.ru"})

    def _receive(self, message: EmailMessage, uid: int) -> None:
        raw = message.as_bytes()
        self.service._process_incoming(
            mailbox="buyer@mail.ru", folder="INBOX", uid_validity="42", uid=uid,
            message=BytesParser(policy=policy.default).parsebytes(raw), raw=raw,
        )

    def _send(self, draft, **kwargs):
        actual_build = MailGateway(self.service.mail_settings()).build_message
        with patch("automation.service.MailGateway") as gateway_class:
            gateway = gateway_class.return_value
            gateway.build_message.side_effect = actual_build
            send = gateway.smtp_session.return_value.__enter__.return_value.send_message
            if "error" in kwargs:
                send.side_effect = kwargs.pop("error")
            try:
                result = self.service.send_reply(draft, **kwargs)
            finally:
                self.sent = [call.args[0] for call in send.call_args_list]
        return result

    def test_reply_threads_quotes_and_attaches(self) -> None:
        incoming = self._reply()
        incoming["Reply-To"] = "Менеджер <manager@example.ru>"
        self._receive(incoming, 201)
        incoming_id = int(self.db.list_incoming()[0]["id"])
        draft = self.service.prepare_reply(incoming_id=incoming_id)
        self.assertEqual(draft.to_email, "manager@example.ru")
        self.assertEqual(draft.subject, "Re: Предложение [RFQ-2026-0001]")
        spec = Path(self.temp.name) / "Спецификация.pdf"
        spec.write_bytes(b"%PDF-spec")

        self._send(draft, body="Уточните срок поставки.", attachment_paths=[str(spec)])

        message = self.sent[0]
        self.assertEqual(message["To"], "manager@example.ru")
        self.assertEqual(message["In-Reply-To"], "<incoming@example.ru>")
        self.assertEqual(message["References"], "<outgoing@example.ru> <incoming@example.ru>")
        text = message.get_body(("plain",)).get_content()
        self.assertTrue(text.startswith("Уточните срок поставки."))
        self.assertIn("> Добрый день, предложение во вложении.", text)
        self.assertEqual([part.get_filename() for part in message.iter_attachments()],
                         ["Спецификация.pdf"])
        reply = self.db.list_sent_replies(self.campaign_id)[0]
        self.assertEqual((reply["delivery_status"], reply["to_email"]), ("sent", "manager@example.ru"))
        self.assertTrue(Path(json.loads(reply["attachments_json"])[0]).is_file())
        # Состояние адресата остаётся «Файлы получены».
        recipient_id = int(self.recipient["id"])
        self.assertEqual(self.db.get_recipient(recipient_id)["status"], "files_received")

        # Следующий ответ поставщика ссылается только на наш ответ и всё равно привязывается.
        answer = EmailMessage(policy=policy.default)
        answer["From"] = "manager@example.ru"
        answer["Subject"] = "Re: Re: Предложение"
        answer["Message-ID"] = "<answer@example.ru>"
        answer["In-Reply-To"] = message["Message-ID"]
        answer.set_content("Срок поставки — 2 недели.")
        self._receive(answer, 202)
        latest = self.db.latest_incoming_for_recipient(recipient_id)
        self.assertEqual((latest["message_id"], latest["match_method"]),
                         ("<answer@example.ru>", "reply_headers"))

    def test_follow_up_without_answer_replies_to_our_letter(self) -> None:
        draft = self.service.prepare_reply(recipient_id=int(self.recipient["id"]))
        self.assertIsNone(draft.incoming_id)
        self.assertEqual(draft.to_email, "supplier@example.ru")
        self.assertEqual(draft.in_reply_to, "<outgoing@example.ru>")
        self.assertEqual(draft.subject, "Re: Предложение [RFQ-2026-0001]")
        self._send(draft, body="Напоминаем о запросе.", quote=False)
        text = self.sent[0].get_body(("plain",)).get_content()
        self.assertEqual(text.strip(), "Напоминаем о запросе.")

    def test_unbound_message_cannot_be_answered(self) -> None:
        stranger = self._reply()
        del stranger["In-Reply-To"]
        stranger.replace_header("Subject", "Вопрос [RFQ-2026-0001]")
        stranger.replace_header("From", "unknown@example.ru")
        self._receive(stranger, 203)
        incoming = self.db.list_incoming()[0]
        self.assertIsNone(incoming["recipient_id"])
        with self.assertRaisesRegex(ValueError, "не привязано"):
            self.service.prepare_reply(incoming_id=int(incoming["id"]))

    def test_rejected_reply_is_marked_failed(self) -> None:
        draft = self.service.prepare_reply(recipient_id=int(self.recipient["id"]))
        with self.assertRaisesRegex(RuntimeError, "отклонил"):
            self._send(draft, body="Текст", error=smtplib.SMTPRecipientsRefused({}))
        self.assertEqual(self.db.list_sent_replies(self.campaign_id)[0]["delivery_status"],
                         "failed")
        with self.assertRaisesRegex(ValueError, "текст"):
            self.service.send_reply(draft, body="   ")


if __name__ == "__main__":
    unittest.main()
