from __future__ import annotations

import json
import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from automation.database import Database
from automation.supplier_search import (MAX_COMPANIES, Candidate, candidate_identity,
                                       export_candidates_xlsx, normalize_phone,
                                       parse_codex_result, run_codex_search)


def sample_candidate(**overrides: object) -> Candidate:
    fields = dict(
        name="Завод Гидропривод",
        website="https://www.gidro.example.ru",
        email="sales@gidro.example.ru",
        region="Россия",
        categories=["Гидроцилиндры"],
        evidence="На сайте есть раздел гидроцилиндров.",
        source_urls=["https://www.gidro.example.ru/catalog"],
        contact_source_url="https://www.gidro.example.ru/contacts",
    )
    fields.update(overrides)
    return Candidate(**fields)


class SupplierSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "app.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_without_email_stays_available_for_review(self) -> None:
        candidate = sample_candidate(email="", contact_source_url="")
        added, existing = self.db.import_candidates("гидроцилиндры", "Россия", [candidate])
        self.assertEqual((added, existing), (1, 0))
        row = self.db.list_candidates()[0]
        self.assertEqual(row["status"], "new")
        with self.assertRaisesRegex(ValueError, "email"):
            self.db.approve_candidate(int(row["id"]))
        self.assertEqual(self.db.list_suppliers(), [])

    def test_repeated_site_merges_categories_and_approval_is_idempotent(self) -> None:
        first = sample_candidate()
        second = sample_candidate(
            name="Гидропривод", website="https://gidro.example.ru/",
            categories=["Гидростанции"],
            source_urls=["https://gidro.example.ru/hydraulic-stations"],
        )
        self.assertEqual(self.db.import_candidates("цилиндры", "Россия", [first]), (1, 0))
        self.assertEqual(self.db.import_candidates("станции", "Россия", [second]), (0, 1))
        row = self.db.list_candidates()[0]
        self.assertEqual(len(self.db.list_candidates()), 1)
        self.assertEqual(set(json.loads(row["categories_json"])),
                         {"Гидроцилиндры", "Гидростанции"})
        supplier_id = self.db.approve_candidate(int(row["id"]))
        self.assertEqual(self.db.approve_candidate(int(row["id"])), supplier_id)
        self.assertEqual(len(self.db.list_suppliers()), 1)
        self.assertEqual(set(self.db.list_categories()), {"Гидроцилиндры", "Гидростанции"})
        with self.assertRaisesRegex(ValueError, "справочнике"):
            self.db.update_candidate(
                int(row["id"]), name="Изменено", website=first.website, email=first.email,
                region=first.region, categories=[], evidence="", source_urls=first.source_urls,
                contact_source_url=first.contact_source_url,
            )

    def test_bulk_category_and_delete_skip_approved_candidates(self) -> None:
        self.db.import_candidates("гидроцилиндры", "Россия", [
            sample_candidate(),
            sample_candidate(name="Второй", website="https://second.example.ru",
                             email="info@second.example.ru",
                             contact_source_url="https://second.example.ru/contacts"),
        ])
        rows = {row["name"]: int(row["id"]) for row in self.db.list_candidates()}
        self.db.approve_candidate(rows["Завод Гидропривод"])
        changed = self.db.add_candidates_category(rows.values(), " гидроцилиндры ")
        self.assertEqual(changed, 0)  # у второго такая категория уже есть (без учёта регистра)
        self.assertEqual(self.db.add_candidates_category(rows.values(), "Насосы"), 1)
        second = self.db.get_candidate(rows["Второй"])
        self.assertEqual(json.loads(second["categories_json"]), ["Гидроцилиндры", "Насосы"])
        self.assertEqual(self.db.delete_candidates(rows.values()), 2)
        self.assertEqual(self.db.list_candidates(), [])
        self.assertEqual(len(self.db.list_suppliers()), 1)

    def test_bulk_supplier_exclusion_and_categories(self) -> None:
        first = self.db.save_supplier("Первый", "one@example.ru", categories=["Насосы"])
        second = self.db.save_supplier("Второй", "two@example.ru")
        self.assertEqual(self.db.set_suppliers_excluded([first, second], True, "дорого"), 2)
        self.assertTrue(all(row["excluded"] and row["excluded_reason"] == "дорого"
                            for row in self.db.list_suppliers()))
        self.db.set_suppliers_excluded([first], False, "игнорируется")
        by_id = {int(row["id"]): row for row in self.db.list_suppliers()}
        self.assertEqual((by_id[first]["excluded"], by_id[first]["excluded_reason"]), (0, ""))
        self.assertEqual(self.db.change_suppliers_category([first, second], "насосы"), 1)
        self.assertEqual(self.db.list_categories(), ["Насосы"])
        self.assertEqual(
            self.db.change_suppliers_category([first, second], "НАСОСЫ", remove=True), 2
        )
        self.assertEqual(self.db.list_categories(), [])

    def test_existing_supplier_is_linked_without_overwriting_its_name(self) -> None:
        existing_id = self.db.save_supplier(
            "Проверенное название", "sales@gidro.example.ru", categories=["Металлоконструкции"]
        )
        self.db.import_candidates("гидроцилиндры", "Россия", [sample_candidate()])
        candidate_id = int(self.db.list_candidates()[0]["id"])
        self.assertEqual(self.db.approve_candidate(candidate_id), existing_id)
        supplier = self.db.list_suppliers()[0]
        self.assertEqual(supplier["name"], "Проверенное название")
        self.assertEqual(set(self.db.list_categories()),
                         {"Металлоконструкции", "Гидроцилиндры"})

    def test_parser_discards_bad_links_and_requires_email_provenance(self) -> None:
        valid = sample_candidate().__dict__
        invalid = {**valid, "name": "Непроверенная компания", "website": "javascript:alert(1)"}
        no_email_source = {**valid, "name": "Без источника email", "website": "https://other.example.ru",
                           "contact_source_url": ""}
        result = parse_codex_result(json.dumps({"candidates": [valid, invalid, no_email_source]}))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.rejected_count, 2)
        self.assertIn(valid["contact_source_url"], result.candidates[0].source_urls)

    def test_parser_reads_contacts_and_drops_bad_phones(self) -> None:
        valid = {**sample_candidate().__dict__,
                 "phones": ["+7 (495) 123-45-67 доб. 12", "8 495 123 45 67 доб 12",
                            "звоните нам", "+7 800 555-35-35"],
                 "contact_person": "Иванов Иван, менеджер отдела продаж",
                 "address": "г. Москва, ул. Заводская, 1"}
        phone_only = {**valid, "name": "Только телефон", "website": "https://phone.example.ru",
                      "email": "", "contact_person": "", "contact_source_url": ""}
        result = parse_codex_result(json.dumps({"candidates": [valid, phone_only]}))
        self.assertEqual(result.rejected_count, 1)  # телефон без страницы-источника
        candidate = result.candidates[0]
        self.assertEqual(candidate.phones, ["+7 (495) 123-45-67 доб. 12", "+7 800 555-35-35"])
        self.assertEqual(candidate.contact_person, "Иванов Иван, менеджер отдела продаж")
        self.assertEqual(candidate.address, "г. Москва, ул. Заводская, 1")

    def test_phone_validation(self) -> None:
        self.assertEqual(normalize_phone("  +7  (495)–123-45-67 "), "+7 (495)-123-45-67")
        self.assertEqual(normalize_phone(""), "")
        for bad in ("12-34", "+7 495 abc", "1" * 16):
            with self.assertRaises(ValueError):
                normalize_phone(bad)

    def test_contacts_are_merged_and_copied_to_supplier_notes(self) -> None:
        self.db.import_candidates("цилиндры", "Россия", [sample_candidate(
            email="", phones=["+7 495 123-45-67"], address="Москва",
        )])
        self.db.import_candidates("станции", "Россия", [sample_candidate(
            phones=["8 (495) 123-45-67", "+7 495 765-43-21"], contact_person="Петров П.",
            address="Тула",
        )])
        row = self.db.list_candidates()[0]
        self.assertEqual(json.loads(row["phones_json"]),
                         ["+7 495 123-45-67", "+7 495 765-43-21"])
        self.assertEqual((row["contact_person"], row["address"]), ("Петров П.", "Москва"))
        self.db.approve_candidate(int(row["id"]))
        notes = self.db.list_suppliers()[0]["notes"]
        self.assertIn("Телефоны: +7 495 123-45-67, +7 495 765-43-21", notes)
        self.assertIn("Контактное лицо: Петров П.", notes)

    def test_existing_candidates_table_gets_contact_columns(self) -> None:
        legacy = Path(self.temp.name) / "legacy-candidates.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute(
                "CREATE TABLE supplier_candidates (id INTEGER PRIMARY KEY, search_id INTEGER, "
                "identity_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
                "website TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', "
                "region TEXT NOT NULL DEFAULT '', categories_json TEXT NOT NULL DEFAULT '[]', "
                "evidence TEXT NOT NULL DEFAULT '', source_urls_json TEXT NOT NULL DEFAULT '[]', "
                "contact_source_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new', "
                "supplier_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO supplier_candidates(identity_key, name, created_at, updated_at) "
                "VALUES('name:старый', 'Старый', 'x', 'x')"
            )
            connection.commit()
        row = Database(legacy).list_candidates()[0]
        self.assertEqual((row["phones_json"], row["contact_person"], row["address"]),
                         ("[]", "", ""))

    def test_parser_keeps_up_to_fifty_companies(self) -> None:
        valid = sample_candidate().__dict__
        candidates = [
            {**valid, "name": f"Компания {i}", "website": f"https://c{i}.example.ru",
             "email": f"sales@c{i}.example.ru", "contact_source_url": f"https://c{i}.example.ru/contacts",
             "source_urls": [f"https://c{i}.example.ru/catalog"]}
            for i in range(MAX_COMPANIES + 5)
        ]
        result = parse_codex_result(json.dumps({"candidates": candidates}))
        self.assertEqual(MAX_COMPANIES, 50)
        self.assertEqual(len(result.candidates), 50)

    def test_search_rejects_more_than_fifty_companies(self) -> None:
        with self.assertRaisesRegex(ValueError, "от 1 до 50"):
            run_codex_search("гидроцилиндры", "Россия", [], project_root=Path(self.temp.name),
                             data_root=Path(self.temp.name), maximum_companies=51)

    def test_identity_uses_site_before_email(self) -> None:
        first = candidate_identity("Завод", "https://www.gidro.example.ru/catalog", "", "Россия")
        second = candidate_identity("Другое написание", "https://gidro.example.ru", "x@y.ru", "")
        self.assertEqual(first, second)

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl не установлен")
    def test_export_contains_sources_and_does_not_create_excel_formula(self) -> None:
        from openpyxl import load_workbook

        self.db.import_candidates("=опасный запрос", "Россия", [sample_candidate()])
        output = Path(self.temp.name) / "suppliers.xlsx"
        export_candidates_xlsx(self.db.list_candidates(), output)
        sheet = load_workbook(output).active
        self.assertEqual(sheet["B2"].value, "Завод Гидропривод")
        self.assertIn("/catalog", sheet["G2"].value)
        self.assertEqual(sheet["J2"].data_type, "s")


if __name__ == "__main__":
    unittest.main()
