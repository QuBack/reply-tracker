from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from automation.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_campaign_status_becomes_ready_when_all_recipients_terminal(self) -> None:
        first = self.db.save_supplier("Первый", "one@example.ru")
        second = self.db.save_supplier("Второй", "two@example.ru")
        campaign_id = self.db.create_campaign(
            "Тест", "Запрос", "Текст", None, [first, second]
        )
        recipients = self.db.list_recipients(campaign_id)
        self.db.record_outgoing(
            campaign_id,
            int(recipients[0]["id"]),
            "<one@test>",
            "Запрос",
            "one.eml",
        )
        self.db.record_outgoing(
            campaign_id,
            int(recipients[1]["id"]),
            "<two@test>",
            "Запрос",
            "two.eml",
        )
        self.db.set_recipient_status(int(recipients[0]["id"]), "files_received")
        self.assertEqual(self.db.get_campaign(campaign_id)["status"], "active")
        self.db.set_recipient_status(int(recipients[1]["id"]), "closed_no_response")
        self.assertEqual(self.db.get_campaign(campaign_id)["status"], "ready")

    def test_supplier_email_is_unique_case_insensitively(self) -> None:
        self.db.save_supplier("Первый", "Sales@Example.ru")
        with self.assertRaises(Exception):
            self.db.save_supplier("Дубликат", "sales@example.ru")

    def test_excluded_supplier_cannot_be_added_to_campaign(self) -> None:
        allowed = self.db.save_supplier("Обычный", "ok@example.ru")
        blocked = self.db.save_supplier(
            "Исключённый", "no@example.ru", excluded=True, excluded_reason="Конкурент"
        )
        row = next(r for r in self.db.list_suppliers() if r["id"] == blocked)
        self.assertEqual((row["excluded"], row["excluded_reason"]), (1, "Конкурент"))
        with self.assertRaisesRegex(ValueError, "Исключённый"):
            self.db.create_campaign("Тест", "Запрос", "Текст", None, [allowed, blocked])
        self.assertEqual(self.db.list_campaigns(), [])

    def test_supplier_exclusion_can_be_lifted(self) -> None:
        supplier = self.db.save_supplier("Поставщик", "s@example.ru", excluded=True,
                                         excluded_reason="Проверка")
        self.db.save_supplier("Поставщик", "s@example.ru", supplier_id=supplier, excluded=False,
                              excluded_reason="Проверка")
        row = self.db.list_suppliers()[0]
        self.assertEqual((row["excluded"], row["excluded_reason"]), (0, ""))
        self.db.create_campaign("Тест", "Запрос", "Текст", None, [supplier])

    def test_existing_suppliers_get_exclusion_columns(self) -> None:
        legacy = Path(self.temp.name) / "legacy-suppliers.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute(
                "CREATE TABLE suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                "email TEXT NOT NULL COLLATE NOCASE UNIQUE, notes TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO suppliers VALUES (1, 'Старый', 'old@example.ru', '', '2026-09-01', "
                "'2026-09-01')"
            )
            connection.commit()
        row = Database(legacy).list_suppliers()[0]
        self.assertEqual((row["name"], row["excluded"], row["excluded_reason"]), ("Старый", 0, ""))

    def test_existing_suppliers_table_allows_supplier_without_email(self) -> None:
        legacy = Path(self.temp.name) / "legacy-v3.db"
        Database(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                "PRAGMA foreign_keys = OFF;"
                "DROP TABLE suppliers;"
                "CREATE TABLE suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                "email TEXT NOT NULL COLLATE NOCASE UNIQUE, notes TEXT NOT NULL DEFAULT '', "
                "excluded INTEGER NOT NULL DEFAULT 0, excluded_reason TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                "INSERT INTO suppliers(id, name, email, created_at, updated_at) "
                "VALUES (1, 'Старый', 'old@example.ru', 'x', 'x');"
                "INSERT INTO supplier_categories VALUES (1, 'Насосы');"
            )
        upgraded = Database(legacy)
        self.assertEqual(upgraded.list_categories(), ["Насосы"])
        upgraded.save_supplier("Без почты 1", "", phones=["+7 495 000-00-01"])
        upgraded.save_supplier("Без почты 2", "", phones=["+7 495 000-00-02"])
        rows = {row["name"]: row for row in upgraded.list_suppliers()}
        self.assertEqual(rows["Старый"]["email"], "old@example.ru")
        self.assertIsNone(rows["Без почты 1"]["email"])
        upgraded.delete_supplier(1)  # каскадное удаление категорий по-прежнему работает
        self.assertEqual(upgraded.list_categories(), [])
        self.assertTrue(list(Path(self.temp.name).glob("legacy-v3.pre-v4-*.db")))

    def test_settings_round_trip(self) -> None:
        self.db.set_settings({"email_address": "box@mail.ru", "check_interval_minutes": "15"})
        self.assertEqual(self.db.get_setting("email_address"), "box@mail.ru")
        self.assertEqual(self.db.get_setting("check_interval_minutes"), "15")

    def test_existing_campaign_keeps_data_when_request_column_is_added(self) -> None:
        legacy = Path(self.temp.name) / "legacy-campaign.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute(
                "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, code TEXT, name TEXT, "
                "subject TEXT, body TEXT, deadline TEXT, status TEXT, created_at TEXT, "
                "sent_at TEXT, last_checked_at TEXT)"
            )
            connection.execute(
                "INSERT INTO campaigns VALUES (1, 'RFQ-2026-0001', 'Старая рассылка', "
                "'Тема', 'Текст', NULL, 'ready', '2026-09-24', NULL, NULL)"
            )
            connection.commit()
        upgraded = Database(legacy)
        self.assertEqual(upgraded.get_campaign(1)["name"], "Старая рассылка")
        self.assertIsNone(upgraded.get_campaign(1)["request_path"])

    def test_same_uid_with_new_uidvalidity_is_a_new_message(self) -> None:
        fields = dict(
            mailbox="buyer@mail.ru", folder="INBOX", imap_uid=101,
            campaign_id=None, recipient_id=None, message_id="<first@example.ru>",
            in_reply_to="", sender_email="supplier@example.ru", subject="Ответ",
            received_at=None, body_text="", raw_eml_path="first.eml",
            match_method="manual", needs_review=True,
        )
        first_id, first_created = self.db.insert_incoming(uid_validity="42", **fields)
        second_id, second_created = self.db.insert_incoming(
            uid_validity="43", **{**fields, "message_id": "<second@example.ru>",
                                   "raw_eml_path": "second.eml"}
        )
        self.assertTrue(first_created and second_created)
        self.assertNotEqual(first_id, second_id)

    def test_existing_database_is_migrated_without_losing_incoming(self) -> None:
        old_path = Path(self.temp.name) / "old.db"
        with closing(sqlite3.connect(old_path)) as connection:
            connection.execute(
                "CREATE TABLE incoming_messages ("
                "id INTEGER PRIMARY KEY, mailbox TEXT NOT NULL, folder TEXT NOT NULL, "
                "imap_uid INTEGER NOT NULL, campaign_id INTEGER, recipient_id INTEGER, "
                "message_id TEXT, in_reply_to TEXT, sender_email TEXT NOT NULL, "
                "subject TEXT NOT NULL, received_at TEXT, body_text TEXT NOT NULL, "
                "raw_eml_path TEXT NOT NULL, match_method TEXT NOT NULL, "
                "needs_review INTEGER NOT NULL, created_at TEXT NOT NULL, "
                "UNIQUE(mailbox, folder, imap_uid))"
            )
            connection.execute(
                "INSERT INTO incoming_messages VALUES "
                "(1, 'buyer@mail.ru', 'INBOX', 101, NULL, NULL, '<old@example.ru>', "
                "'', 'supplier@example.ru', 'Ответ', NULL, '', 'old.eml', 'manual', 1, "
                "'2026-09-15T00:00:00+00:00')"
            )
            connection.execute(
                "CREATE TABLE attachments (id INTEGER PRIMARY KEY, "
                "incoming_message_id INTEGER NOT NULL REFERENCES incoming_messages(id), "
                "filename TEXT NOT NULL, content_type TEXT NOT NULL, "
                "size INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, "
                "is_allowed INTEGER NOT NULL, is_selected INTEGER NOT NULL DEFAULT 1, "
                "duplicate_of_id INTEGER, created_at TEXT NOT NULL, "
                "UNIQUE(incoming_message_id, sha256, filename))"
            )
            connection.execute(
                "INSERT INTO attachments VALUES "
                "(1, 1, 'offer.pdf', 'application/pdf', 5, 'abc', 'offer.pdf', 1, 1, NULL, "
                "'2026-09-15T00:00:00+00:00')"
            )
            connection.commit()
        migrated = Database(old_path)
        with migrated.connect() as connection:
            row = connection.execute("SELECT * FROM incoming_messages WHERE id = 1").fetchone()
            self.assertEqual(row["message_id"], "<old@example.ru>")
            self.assertEqual(row["uid_validity"], "legacy")
            attachment = connection.execute("SELECT * FROM attachments WHERE id = 1").fetchone()
            self.assertEqual(attachment["incoming_message_id"], 1)
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
