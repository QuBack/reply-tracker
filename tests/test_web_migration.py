from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from automation.database import Database
from webapp.migration import import_desktop
from webapp.store import Store


class MigrationTests(unittest.TestCase):
    def test_import_remaps_paths_keeps_source_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "desktop"
            db = Database(source / "app.db")
            supplier = db.save_supplier("Old supplier", "old@example.test")
            campaign = db.create_campaign("Old RFQ", "Subject", "Body", None, [supplier])
            folder = source / "campaigns" / "RFQ-2026-0001" / "outgoing"
            folder.mkdir(parents=True)
            request = folder / "request.xlsx"
            request.write_bytes(b"test-document")
            # The source can be imported on Linux even when the database has Windows paths.
            old_path = "C:\\Users\\example\\AutomationSystem\\campaigns\\RFQ-2026-0001\\outgoing\\request.xlsx"
            db.set_campaign_request_path(campaign, old_path)
            db.add_campaign_attachment(campaign, "request.xlsx", old_path, 13, "hash")
            store = Store(base / "web")
            user_id = store.create_user("owner@example.test", "strong-password-123")
            target = import_desktop(store, "owner@example.test", source)
            imported = Database(target / "app.db")
            self.assertEqual(imported.list_suppliers()[0]["name"], "Old supplier")
            self.assertEqual(Path(imported.get_campaign(campaign)["request_path"]).read_bytes(), b"test-document")
            self.assertTrue(target.is_relative_to(store.root / "accounts" / user_id))
            self.assertEqual(db.get_campaign(campaign)["request_path"], old_path)
            with self.assertRaises(ValueError):
                import_desktop(store, "owner@example.test", source)

    def test_external_paths_are_rejected_without_publishing_partial_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source, root = Path(directory) / "old", Path(directory) / "web"
            db = Database(source / "app.db")
            supplier = db.save_supplier("Supplier", "s@example.test")
            campaign = db.create_campaign("RFQ", "Subject", "Body", None, [supplier])
            db.set_campaign_request_path(campaign, "C:\\private\\other-account.xlsx")
            store = Store(root)
            user_id = store.create_user("owner@example.test", "strong-password-123")
            with self.assertRaises(ValueError):
                import_desktop(store, "owner@example.test", source)
            self.assertFalse((root / "accounts" / user_id).exists())
