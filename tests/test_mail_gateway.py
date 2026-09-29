from __future__ import annotations

import unittest
from datetime import date
from email.message import EmailMessage
from unittest.mock import patch

from rosa_mail.mail_gateway import MailGateway, MailSettings


class FakeImap:
    def __init__(self, *, uids: bytes, failed_uid: int | None = None, uid_next: int = 104):
        self.uids = uids
        self.failed_uid = failed_uid
        self.uid_next = uid_next
        self.searches: list[tuple] = []

    def login(self, *_args):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "OK", [b"3"]

    def status(self, *_args):
        return "OK", [f'INBOX (UIDVALIDITY 42 UIDNEXT {self.uid_next})'.encode()]

    def uid(self, command, *args):
        if command == "search":
            self.searches.append(args)
            return "OK", [self.uids]
        uid = int(args[0])
        if uid == self.failed_uid:
            return "NO", []
        message = EmailMessage()
        message["From"] = "supplier@example.ru"
        message["To"] = "buyer@mail.ru"
        message["Subject"] = f"Ответ {uid}"
        message.set_content("Предложение")
        return "OK", [(b"BODY[]", message.as_bytes())]

    def logout(self):
        return "BYE", []


class MailGatewayFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = MailGateway(MailSettings("buyer@mail.ru", "test-secret"))

    def test_failed_fetch_does_not_advance_cursor_past_missing_message(self) -> None:
        fake = FakeImap(uids=b"101 102 103", failed_uid=102)
        with patch("rosa_mail.mail_gateway.imaplib.IMAP4_SSL", return_value=fake):
            result = self.gateway.fetch_new_messages(
                last_uid=100, previous_uid_validity="42", since=date(2026, 9, 1)
            )
        self.assertEqual([message.uid for message in result.messages], [101, 103])
        self.assertEqual(result.last_uid, 101)

    def test_empty_initial_search_uses_uidnext_from_before_search(self) -> None:
        fake = FakeImap(uids=b"", uid_next=500)
        with patch("rosa_mail.mail_gateway.imaplib.IMAP4_SSL", return_value=fake):
            result = self.gateway.fetch_new_messages(
                last_uid=0, previous_uid_validity=None, since=date(2026, 9, 1)
            )
        self.assertEqual(result.last_uid, 499)


if __name__ == "__main__":
    unittest.main()
