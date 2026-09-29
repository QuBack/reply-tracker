from __future__ import annotations

import unittest
from email import policy
from email.parser import BytesParser

from rosa_mail.mail_gateway import (
    extract_message_text,
    message_reference_ids,
    subject_campaign_code,
)


class MailHelperTests(unittest.TestCase):
    def test_extracts_campaign_code_case_insensitively(self) -> None:
        self.assertEqual(
            subject_campaign_code("Re: предложение [rfq-2026-0042]"),
            "RFQ-2026-0042",
        )

    def test_extracts_reference_headers(self) -> None:
        message = BytesParser(policy=policy.default).parsebytes(
            b"In-Reply-To: <first@example>\r\n"
            b"References: <older@example> <first@example>\r\n\r\n"
        )
        self.assertEqual(
            message_reference_ids(message),
            ["<first@example>", "<older@example>"],
        )

    def test_prefers_plain_text_body(self) -> None:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: multipart/alternative; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain answer\r\n"
            b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<b>HTML answer</b>\r\n"
            b"--x--\r\n"
        )
        self.assertEqual(extract_message_text(message), "Plain answer")


if __name__ == "__main__":
    unittest.main()

