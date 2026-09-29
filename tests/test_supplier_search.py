from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from automation.database import Database
from automation.supplier_search import (Candidate, candidate_identity, export_candidates_xlsx,
                                       parse_codex_result)


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
