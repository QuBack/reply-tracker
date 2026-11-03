import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from automation.summary import _tesseract_ocr


class TesseractTests(unittest.TestCase):
    def test_tsv_words_are_grouped_into_positioned_lines(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        tsv = header + "5\t1\t1\t1\t1\t1\t10\t20\t80\t10\t90\tМеталлоторг\n" \
            + "5\t1\t1\t1\t1\t2\t100\t20\t50\t10\t90\tСчёт\n" \
            + "5\t1\t2\t1\t1\t1\t300\t250\t40\t10\t90\t1,250\n"
        with patch("automation.summary.shutil.which", return_value="tesseract"), \
             patch("automation.summary.subprocess.run", return_value=CompletedProcess([], 0, tsv, "")):
            result = _tesseract_ocr(Path("image.png"))
        self.assertEqual(result, [{"text": "Металлоторг Счёт", "x": 10.0, "y": 20.0},
                                  {"text": "1,250", "x": 300.0, "y": 250.0}])

    def test_missing_ocr_reports_installation_requirement(self):
        with patch("automation.summary.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Tesseract"):
                _tesseract_ocr(Path("image.png"))
