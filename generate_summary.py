"""Создать сводную из заявки XLSX и счетов PDF.

Пример: python generate_summary.py --request request.xlsx \
    --offer "Поставщик=invoice.pdf" --output summary.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rosa_mail.summary import OfferSource, build_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Сводная по заявке и счетам поставщиков")
    parser.add_argument("--request", required=True, type=Path, help="Исходная заявка XLSX")
    parser.add_argument("--offer", action="append", required=True, metavar="ПОСТАВЩИК=ФАЙЛ",
                        help="Счёт PDF; параметр можно повторить")
    parser.add_argument("--output", required=True, type=Path, help="Выходной файл XLSX")
    args = parser.parse_args()
    sources: list[OfferSource] = []
    for item in args.offer:
        if "=" not in item:
            parser.error("--offer укажите как ПОСТАВЩИК=ФАЙЛ")
        supplier, filename = item.split("=", 1)
        if not supplier.strip() or not filename.strip():
            parser.error("В --offer нужны название поставщика и путь к PDF")
        sources.append(OfferSource(Path(filename), supplier.strip()))
    result = build_summary(args.request, sources, args.output)
    print(f"Создано: {result.output_path}")
    print(f"Строк заявки: {len(result.request_lines)}; строк счетов: {len(result.allocations)}; "
          f"замечаний: {len(result.issues)}")
    if result.issues:
        print("Проверьте лист «Проверка» перед использованием сводной.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
