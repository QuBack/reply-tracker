from __future__ import annotations

import os
import json
import queue
import sqlite3
import threading
import time
import tkinter as tk
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

from .database import Database
from .mail_gateway import MAIL_PROVIDERS
from .service import AppService, OperationResult
from .supplier_search import (SearchCancelled, clean_categories, clean_phones,
                              export_candidates_xlsx, normalize_email, normalize_web_url,
                              run_codex_search)


CAMPAIGN_STATUS_LABELS = {
    "draft": "Черновик",
    "active": "Ожидаем ответы",
    "ready": "Готово к сводной",
    "closed": "Завершена",
}

RECIPIENT_STATUS_LABELS = {
    "pending": "Не отправлено",
    "sent": "Ожидаем ответ",
    "reply_without_files": "Ответ без файлов",
    "files_received": "Файлы получены",
    "declined": "Отказ",
    "closed_no_response": "Закрыт без ответа",
    "send_failed": "Ошибка отправки",
    "send_unknown": "Исход отправки неизвестен",
}

CANDIDATE_STATUS_LABELS = {
    "new": "На проверке",
    "approved": "Добавлен",
    "rejected": "Отклонён",
}

ALL_CATEGORIES = "Все категории"
ALL_STATUSES = "Все статусы"
ANY_CONTACTS = "Любые контакты"

# Фильтр «Контакты» на вкладке поиска: (есть email, есть телефон) -> показывать ли строку.
CONTACT_FILTERS = {
    ANY_CONTACTS: lambda has_email, has_phone: True,
    "С email": lambda has_email, has_phone: has_email,
    "С телефоном": lambda has_email, has_phone: has_phone,
    "С email и телефоном": lambda has_email, has_phone: has_email and has_phone,
    "Только телефон": lambda has_email, has_phone: has_phone and not has_email,
    "Без email": lambda has_email, has_phone: not has_email,
    "Без контактов": lambda has_email, has_phone: not has_email and not has_phone,
}

MATCH_METHOD_LABELS = {
    "reply_headers": "По цепочке письма",
    "subject_code": "По коду в теме",
    "sender_single_active": "По отправителю",
    "sender_ambiguous": "Неоднозначно",
    "manual": "Вручную",
    "unmatched": "Не привязано",
}


def incoming_result_label(row: sqlite3.Row) -> str:
    if "processed_at" in row.keys() and row["processed_at"] is None and row["uid_validity"] != "legacy":
        return "Обработка не завершена"
    if row["needs_review"] or row["recipient_id"] is None:
        return "Нужна привязка"
    if row["allowed_count"]:
        return "Файлы получены"
    if row["attachment_count"]:
        return "Нет PDF/Excel"
    return "Ответ без файлов"


def text_matches(query: str, *fields: str) -> bool:
    words = query.casefold().split()
    haystack = " ".join(str(field or "") for field in fields).casefold()
    return all(word in haystack for word in words)


def display_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            parsed = parsed.astimezone()
        return parsed.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


class AutomationApp(tk.Tk):
    def __init__(self, service: AppService, database: Database) -> None:
        super().__init__()
        self.service = service
        self.db = database
        self.title("Система автоматизации")
        self.geometry("1220x790")
        self.minsize(980, 660)
        self.protocol("WM_DELETE_WINDOW", self._minimize_window)

        self._task_queue: queue.Queue[tuple] = queue.Queue()
        self._background_busy = False
        self._next_auto_check = time.monotonic() + self._interval_seconds()
        self._selected_supplier_id: int | None = None
        self._selected_candidate_id: int | None = None
        self._campaign_supplier_ids: list[int] = []
        self._supplier_rows: dict[int, sqlite3.Row] = {}
        self._candidate_rows: dict[int, sqlite3.Row] = {}
        self._campaign_attachment_paths: list[str] = []
        self._supplier_search_cancel = threading.Event()
        self._search_active = False

        self._configure_style()
        self._build_layout()
        self.refresh_all()
        self.after(200, self._poll_tasks)
        self.after(2500, self._startup_check)
        self.after(30_000, self._timer_tick)

    def _configure_style(self) -> None:
        self.configure(bg="#f3f5f8")
        style = ttk.Style(self)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        style.configure("TNotebook", background="#f3f5f8", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 16))
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", foreground="#5f6b7a")
        style.configure("Status.TLabel", padding=(10, 7), background="#e8edf3")

    def _build_layout(self) -> None:
        header = tk.Frame(self, bg="#17324d", height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="Система автоматизации",
            bg="#17324d",
            fg="white",
            font=("Segoe UI Semibold", 16),
        ).pack(side="left", padx=20)
        self.overview_var = tk.StringVar()
        tk.Label(header, textvariable=self.overview_var, bg="#17324d", fg="#dceaf7",
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(header, text="Проверить почту", command=self._manual_check).pack(
            side="right", padx=(6, 16), pady=15
        )
        ttk.Button(header, text="Выйти", command=self._exit_app).pack(side="right", pady=15)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(12, 4))

        self.campaigns_tab = ttk.Frame(self.notebook, padding=14)
        self.inbox_tab = ttk.Frame(self.notebook, padding=14)
        self.suppliers_tab = ttk.Frame(self.notebook, padding=14)
        self.search_tab = ttk.Frame(self.notebook, padding=14)
        self.settings_tab = ttk.Frame(self.notebook, padding=14)

        self.notebook.add(self.campaigns_tab, text="Рассылки")
        self.notebook.add(self.inbox_tab, text="Требуют внимания")
        self.notebook.add(self.suppliers_tab, text="Поставщики")
        self.notebook.add(self.search_tab, text="Поиск поставщиков")
        self.notebook.add(self.settings_tab, text="Настройки")

        self._build_campaigns_tab()
        self._build_inbox_tab()
        self._build_suppliers_tab()
        self._build_search_tab()
        self._build_settings_tab()
        self._compose_dialog: tk.Toplevel | None = None
        self._logs_dialog: tk.Toplevel | None = None

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(
            fill="x", padx=12, pady=(0, 10)
        )

    def _build_suppliers_tab(self) -> None:
        self.suppliers_tab.columnconfigure(0, weight=3)
        self.suppliers_tab.columnconfigure(1, weight=2)
        self.suppliers_tab.rowconfigure(1, weight=1)
        ttk.Label(self.suppliers_tab, text="Справочник поставщиков", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        left = ttk.Frame(self.suppliers_tab)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        filters = ttk.Frame(left)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Поиск").grid(row=0, column=0, padx=(0, 6))
        self.supplier_search_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.supplier_search_var).grid(row=0, column=1, sticky="ew")
        self.supplier_search_var.trace_add("write", lambda *_args: self.refresh_suppliers())
        self.supplier_filter_var = tk.StringVar(value=ALL_CATEGORIES)
        self.supplier_filter_box = ttk.Combobox(
            filters, textvariable=self.supplier_filter_var, state="readonly", width=24
        )
        self.supplier_filter_box.grid(row=0, column=2, padx=(8, 0))
        self.supplier_filter_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_suppliers())
        self.suppliers_tree = self._make_tree(
            left,
            [
                ("name", "Поставщик", 260),
                ("email", "Email", 260),
                ("categories", "Категории", 230),
                ("excluded", "Рассылки", 110),
                ("notes", "Заметки", 260),
            ],
            selectmode="extended",
        )
        self.suppliers_tree.tag_configure("excluded", foreground="#9A3A3A")
        self.suppliers_tree.grid(row=1, column=0, sticky="nsew")
        self.suppliers_tree.bind("<<TreeviewSelect>>", self._on_supplier_selected)
        self._bind_select_all(self.suppliers_tree)
        bulk = ttk.Frame(left)
        bulk.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bulk, text="Выделить все",
                   command=lambda: self._select_all(self.suppliers_tree)).pack(side="left")
        supplier_actions = ttk.Menubutton(bulk, text="Действия с выбранными")
        supplier_actions.pack(side="left", padx=6)
        supplier_menu = tk.Menu(supplier_actions, tearoff=False)
        supplier_menu.add_command(label="Исключить из рассылок…",
                                  command=lambda: self._set_selected_suppliers_excluded(True))
        supplier_menu.add_command(label="Вернуть в рассылки",
                                  command=lambda: self._set_selected_suppliers_excluded(False))
        supplier_menu.add_separator()
        supplier_menu.add_command(label="Добавить категорию…",
                                  command=lambda: self._change_selected_suppliers_category(False))
        supplier_menu.add_command(label="Убрать категорию…",
                                  command=lambda: self._change_selected_suppliers_category(True))
        supplier_menu.add_separator()
        supplier_menu.add_command(label="Удалить", command=self._delete_supplier)
        supplier_actions["menu"] = supplier_menu
        self.suppliers_count_var = tk.StringVar()
        ttk.Label(bulk, textvariable=self.suppliers_count_var, style="Muted.TLabel").pack(side="right")

        form = ttk.LabelFrame(self.suppliers_tab, text="Карточка поставщика", padding=14)
        form.grid(row=1, column=1, sticky="nsew")
        form.columnconfigure(0, weight=1)
        self.supplier_name_var = tk.StringVar()
        self.supplier_email_var = tk.StringVar()
        self.supplier_categories_var = tk.StringVar()
        ttk.Label(form, text="Название").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.supplier_name_var).grid(row=1, column=0, sticky="ew", pady=(3, 12))
        ttk.Label(form, text="Email").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.supplier_email_var).grid(row=3, column=0, sticky="ew", pady=(3, 12))
        ttk.Label(form, text="Категории (через запятую)").grid(row=4, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.supplier_categories_var).grid(
            row=5, column=0, sticky="ew", pady=(3, 12)
        )
        ttk.Label(form, text="Заметки").grid(row=6, column=0, sticky="w")
        self.supplier_notes = scrolledtext.ScrolledText(form, height=8, wrap="word", font=("Segoe UI", 9))
        self.supplier_notes.grid(row=7, column=0, sticky="nsew", pady=(3, 12))
        form.rowconfigure(7, weight=1)
        self.supplier_excluded_var = tk.BooleanVar(value=False)
        self.supplier_excluded_reason_var = tk.StringVar()
        ttk.Checkbutton(
            form, text="Исключён — не отправлять запросы",
            variable=self.supplier_excluded_var, command=self._update_exclusion_reason_state,
        ).grid(row=8, column=0, sticky="w")
        ttk.Label(form, text="Причина исключения").grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.supplier_excluded_reason_entry = ttk.Entry(
            form, textvariable=self.supplier_excluded_reason_var, state="disabled"
        )
        self.supplier_excluded_reason_entry.grid(row=10, column=0, sticky="ew", pady=(3, 12))
        actions = ttk.Frame(form)
        actions.grid(row=11, column=0, sticky="ew")
        ttk.Button(actions, text="Новая", command=self._clear_supplier_form).pack(side="left")
        ttk.Button(actions, text="Сохранить", command=self._save_supplier).pack(side="left", padx=6)
        ttk.Button(actions, text="Удалить", command=self._delete_supplier).pack(side="left")

    def _build_search_tab(self) -> None:
        self.search_tab.columnconfigure(0, weight=1)
        self.search_tab.rowconfigure(2, weight=1)
        ttk.Label(self.search_tab, text="Поиск новых поставщиков", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        request = ttk.LabelFrame(self.search_tab, text="Что нужно найти", padding=10)
        request.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        request.columnconfigure(0, weight=1)
        self.search_query_text = tk.Text(request, height=3, wrap="word", font=("Segoe UI", 10))
        self.search_query_text.grid(row=0, column=0, columnspan=5, sticky="ew")
        ttk.Label(request, text="Регион").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Label(request, text="Компаний").grid(row=1, column=1, sticky="w", pady=(7, 0))
        self.search_region_var = tk.StringVar(value="Россия")
        ttk.Entry(request, textvariable=self.search_region_var, width=30).grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )
        self.search_limit_var = tk.StringVar(value="10")
        ttk.Combobox(
            request, textvariable=self.search_limit_var, state="readonly", width=5,
            values=("5", "10", "15", "20", "25", "30", "40", "50"),
        ).grid(row=2, column=1, padx=(8, 4))
        self.search_start_button = ttk.Button(
            request, text="Найти", command=self._start_supplier_search
        )
        self.search_start_button.grid(row=2, column=2, padx=4)
        self.search_cancel_button = ttk.Button(
            request, text="Остановить", command=self._cancel_supplier_search, state="disabled"
        )
        self.search_cancel_button.grid(row=2, column=3, padx=4)

        body = ttk.PanedWindow(self.search_tab, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(body)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        filters = ttk.Frame(left)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Фильтр").grid(row=0, column=0, padx=(0, 6))
        self.candidate_filter_text_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.candidate_filter_text_var).grid(
            row=0, column=1, sticky="ew"
        )
        self.candidate_filter_status_var = tk.StringVar(value=ALL_STATUSES)
        ttk.Combobox(
            filters, textvariable=self.candidate_filter_status_var, state="readonly", width=14,
            values=[ALL_STATUSES, *CANDIDATE_STATUS_LABELS.values()],
        ).grid(row=0, column=2, padx=(6, 0))
        self.candidate_filter_category_var = tk.StringVar(value=ALL_CATEGORIES)
        self.candidate_filter_category_box = ttk.Combobox(
            filters, textvariable=self.candidate_filter_category_var, state="readonly", width=20
        )
        self.candidate_filter_category_box.grid(row=0, column=3, padx=(6, 0))
        self.candidate_filter_contacts_var = tk.StringVar(value=ANY_CONTACTS)
        ttk.Combobox(
            filters, textvariable=self.candidate_filter_contacts_var, state="readonly", width=19,
            values=list(CONTACT_FILTERS),
        ).grid(row=0, column=4, padx=(6, 0))
        ttk.Button(filters, text="Сбросить", command=self._reset_candidate_filters).grid(
            row=0, column=5, padx=(6, 0)
        )
        for variable in (self.candidate_filter_text_var, self.candidate_filter_status_var,
                         self.candidate_filter_category_var, self.candidate_filter_contacts_var):
            variable.trace_add("write", lambda *_args: self._fill_candidates_tree())

        self.candidates_tree = self._make_tree(left, [
            ("name", "Компания", 230),
            ("categories", "Категории", 200),
            ("region", "Регион", 120),
            ("email", "Email", 180),
            ("phone", "Телефон", 150),
            ("status", "Статус", 110),
        ], selectmode="extended")
        self.candidates_tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.candidates_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.candidates_tree.configure(yscrollcommand=scrollbar.set)
        self.candidates_tree.bind("<<TreeviewSelect>>", self._on_candidate_selected)
        self._bind_select_all(self.candidates_tree)

        bulk = ttk.Frame(left)
        bulk.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bulk, text="Выделить все",
                   command=lambda: self._select_all(self.candidates_tree)).pack(side="left")
        candidate_actions = ttk.Menubutton(bulk, text="Действия с выбранными")
        candidate_actions.pack(side="left", padx=6)
        candidate_menu = tk.Menu(candidate_actions, tearoff=False)
        candidate_menu.add_command(label="Добавить в справочник",
                                   command=self._approve_selected_candidates)
        candidate_menu.add_command(label="Отклонить",
                                   command=lambda: self._set_selected_candidates_status("rejected"))
        candidate_menu.add_command(label="Вернуть на проверку",
                                   command=lambda: self._set_selected_candidates_status("new"))
        candidate_menu.add_separator()
        candidate_menu.add_command(label="Добавить категорию…",
                                   command=self._add_category_to_selected_candidates)
        candidate_menu.add_separator()
        candidate_menu.add_command(label="Удалить из списка",
                                   command=self._delete_selected_candidates)
        candidate_actions["menu"] = candidate_menu
        ttk.Button(bulk, text="Выгрузить все",
                   command=lambda: self._export_search_candidates(only_selected=False)).pack(
            side="right"
        )
        ttk.Button(bulk, text="Выгрузить выбранные",
                   command=lambda: self._export_search_candidates(only_selected=True)).pack(
            side="right", padx=6
        )
        self.candidates_count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.candidates_count_var, style="Muted.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        body.add(left, weight=3)

        details = ttk.LabelFrame(body, text="Проверка кандидата", padding=9)
        details.columnconfigure(0, weight=1)
        self.candidate_name_var = tk.StringVar()
        self.candidate_site_var = tk.StringVar()
        self.candidate_email_var = tk.StringVar()
        self.candidate_region_var = tk.StringVar()
        self.candidate_categories_var = tk.StringVar()
        self.candidate_contact_url_var = tk.StringVar()
        self.candidate_phones_var = tk.StringVar()
        self.candidate_person_var = tk.StringVar()
        self.candidate_address_var = tk.StringVar()
        fields = [
            ("Компания", self.candidate_name_var),
            ("Сайт", self.candidate_site_var),
            ("Email", self.candidate_email_var),
            ("Телефоны через запятую", self.candidate_phones_var),
            ("Контактное лицо", self.candidate_person_var),
            ("Адрес", self.candidate_address_var),
            ("Регион", self.candidate_region_var),
            ("Категории через запятую", self.candidate_categories_var),
            ("Страница с контактами", self.candidate_contact_url_var),
        ]
        for row_index, (label, variable) in enumerate(fields):
            ttk.Label(details, text=label).grid(row=2 * row_index, column=0, sticky="w")
            ttk.Entry(details, textvariable=variable).grid(
                row=2 * row_index + 1, column=0, sticky="ew", pady=(0, 3)
            )
        row_index = len(fields) * 2
        ttk.Label(details, text="Источники (по одной ссылке в строке)").grid(
            row=row_index, column=0, sticky="w"
        )
        self.candidate_sources_text = tk.Text(details, height=3, wrap="word")
        self.candidate_sources_text.grid(row=row_index + 1, column=0, sticky="ew", pady=(0, 3))
        ttk.Label(details, text="Что подтверждено").grid(row=row_index + 2, column=0, sticky="w")
        self.candidate_evidence_text = tk.Text(details, height=3, wrap="word")
        self.candidate_evidence_text.grid(row=row_index + 3, column=0, sticky="ew", pady=(0, 5))
        actions = ttk.Frame(details)
        actions.grid(row=row_index + 4, column=0, sticky="ew")
        self.candidate_save_button = ttk.Button(
            actions, text="Сохранить", command=self._save_candidate
        )
        self.candidate_save_button.pack(side="left")
        self.candidate_approve_button = ttk.Button(
            actions, text="В справочник", command=self._approve_candidate
        )
        self.candidate_approve_button.pack(
            side="left", padx=4
        )
        self.candidate_reject_button = ttk.Button(
            actions, text="Отклонить", command=self._reject_candidate
        )
        self.candidate_reject_button.pack(side="left")
        ttk.Button(actions, text="Открыть источник", command=self._open_candidate_source).pack(
            side="left", padx=4
        )
        body.add(details, weight=2)
        self.search_status_var = tk.StringVar(
            value="Найденные компании сначала попадают сюда на проверку"
        )
        ttk.Label(self.search_tab, textvariable=self.search_status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )

    def _build_compose_tab(self) -> None:
        self.compose_tab.columnconfigure(0, weight=3)
        self.compose_tab.columnconfigure(1, weight=2)
        self.compose_tab.rowconfigure(1, weight=1)
        ttk.Label(self.compose_tab, text="Новая рассылка", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        form = ttk.Frame(self.compose_tab)
        form.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        form.columnconfigure(0, weight=1)
        form.rowconfigure(9, weight=1)
        self.campaign_name_var = tk.StringVar()
        self.campaign_subject_var = tk.StringVar()
        self.campaign_deadline_var = tk.StringVar(
            value=(date.today() + timedelta(days=7)).isoformat()
        )
        self.campaign_request_var = tk.StringVar()
        ttk.Label(form, text="Название рассылки").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.campaign_name_var).grid(row=1, column=0, sticky="ew", pady=(3, 10))
        ttk.Label(form, text="Тема письма").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.campaign_subject_var).grid(row=3, column=0, sticky="ew", pady=(3, 10))
        deadline_row = ttk.Frame(form)
        deadline_row.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(deadline_row, text="Срок ответа (ГГГГ-ММ-ДД)").pack(side="left")
        ttk.Entry(deadline_row, textvariable=self.campaign_deadline_var, width=14).pack(side="left", padx=10)
        ttk.Label(form, text="Заявка XLSX для сводной и отправки поставщикам").grid(
            row=5, column=0, sticky="w"
        )
        request_row = ttk.Frame(form)
        request_row.grid(row=6, column=0, sticky="ew", pady=(3, 10))
        request_row.columnconfigure(0, weight=1)
        ttk.Entry(request_row, textvariable=self.campaign_request_var, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(request_row, text="Выбрать", command=self._choose_request_file).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Label(form, text="Текст письма").grid(row=8, column=0, sticky="w")
        self.campaign_body = scrolledtext.ScrolledText(form, wrap="word", font=("Segoe UI", 10), height=14)
        self.campaign_body.grid(row=9, column=0, sticky="nsew", pady=(3, 10))

        side = ttk.Frame(self.compose_tab)
        side.grid(row=1, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=2)
        side.rowconfigure(4, weight=1)
        ttk.Label(side, text="Получатели", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.compose_suppliers_list = tk.Listbox(
            side,
            selectmode="extended",
            exportselection=False,
            font=("Segoe UI", 9),
            borderwidth=1,
            relief="solid",
        )
        self.compose_suppliers_list.grid(row=1, column=0, sticky="nsew", pady=(5, 12))
        ttk.Label(side, text="Дополнительные вложения", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        attach_actions = ttk.Frame(side)
        attach_actions.grid(row=3, column=0, sticky="ew", pady=4)
        ttk.Button(attach_actions, text="Добавить файлы", command=self._add_campaign_files).pack(side="left")
        ttk.Button(attach_actions, text="Убрать", command=self._remove_campaign_file).pack(side="left", padx=6)
        self.compose_attachments_list = tk.Listbox(side, font=("Segoe UI", 9), borderwidth=1, relief="solid")
        self.compose_attachments_list.grid(row=4, column=0, sticky="nsew", pady=(0, 12))
        ttk.Button(side, text="Создать и отправить", command=self._create_campaign).grid(
            row=5, column=0, sticky="ew"
        )
        ttk.Button(side, text="Отмена", command=self._close_compose_dialog).grid(
            row=6, column=0, sticky="ew", pady=(8, 0)
        )

    def _open_compose_dialog(self) -> None:
        if self._compose_dialog is not None and self._compose_dialog.winfo_exists():
            self._compose_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        dialog.title("Новая рассылка")
        dialog.geometry("1000x700")
        dialog.minsize(850, 600)
        dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW", self._close_compose_dialog)
        self._compose_dialog = dialog
        self.compose_tab = ttk.Frame(dialog, padding=16)
        self.compose_tab.pack(fill="both", expand=True)
        self._campaign_attachment_paths.clear()
        self._build_compose_tab()
        self.refresh_suppliers()
        dialog.focus_set()

    def _close_compose_dialog(self) -> None:
        if self._background_busy:
            self.status_var.set("Дождитесь завершения отправки")
            return
        if self._compose_dialog is not None and self._compose_dialog.winfo_exists():
            self._compose_dialog.destroy()
        self._compose_dialog = None
        self._campaign_attachment_paths.clear()

    def _choose_request_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Выберите исходную заявку XLSX",
            initialdir=str(Path.home() / "Downloads"),
            filetypes=[("Excel XLSX", "*.xlsx")],
            parent=self._compose_dialog or self,
        )
        if selected:
            self.campaign_request_var.set(selected)

    def _open_logs_dialog(self) -> None:
        if self._logs_dialog is not None and self._logs_dialog.winfo_exists():
            self._logs_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        dialog.title("Журнал операций")
        dialog.geometry("950x540")
        dialog.transient(self)
        self._logs_dialog = dialog
        self.logs_tab = ttk.Frame(dialog, padding=14)
        self.logs_tab.pack(fill="both", expand=True)
        self._build_logs_tab()
        self.refresh_logs()
        dialog.protocol("WM_DELETE_WINDOW", lambda: (dialog.destroy(), setattr(self, "_logs_dialog", None)))

    def _build_campaigns_tab(self) -> None:
        self.campaigns_tab.columnconfigure(0, weight=1, minsize=280)
        self.campaigns_tab.columnconfigure(1, weight=3)
        self.campaigns_tab.rowconfigure(1, weight=1)
        top = ttk.Frame(self.campaigns_tab)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(top, text="Рассылки", style="Header.TLabel").pack(side="left")
        ttk.Button(top, text="Новая рассылка", command=self._open_compose_dialog).pack(side="right")

        left = ttk.Frame(self.campaigns_tab)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        ttk.Label(left, text="Все запросы", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.campaigns_tree = self._make_tree(
            left,
            [
                ("name", "Название", 175),
                ("status", "Состояние", 125),
            ],
        )
        self.campaigns_tree.pack(fill="both", expand=True)
        self.campaigns_tree.bind("<<TreeviewSelect>>", self._on_campaign_selected)

        right = ttk.Frame(self.campaigns_tab)
        right.grid(row=1, column=1, sticky="nsew")
        self.campaign_title_var = tk.StringVar(value="Выберите рассылку")
        self.campaign_info_var = tk.StringVar(value="Здесь появятся поставщики и ответы")
        ttk.Label(right, textvariable=self.campaign_title_var, style="Header.TLabel").pack(anchor="w")
        ttk.Label(right, textvariable=self.campaign_info_var, style="Muted.TLabel").pack(
            anchor="w", pady=(2, 8)
        )
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(0, 10))
        self.summary_button = ttk.Button(
            actions, text="Создать сводную", command=self._create_summary, state="disabled"
        )
        self.summary_button.pack(side="left")
        more = ttk.Menubutton(actions, text="Ещё")
        more.pack(side="left", padx=(8, 0))
        more_menu = tk.Menu(more, tearoff=False)
        more_menu.add_command(label="Проверить почту", command=self._manual_check)
        more_menu.add_command(label="Открыть папку рассылки", command=self._open_campaign_folder)
        more_menu.add_command(label="Повторить ошибки отправки", command=self._retry_campaign)
        more_menu.add_command(label="Завершить рассылку", command=self._close_campaign)
        more["menu"] = more_menu

        recipients = ttk.LabelFrame(right, text="Поставщики", padding=8)
        recipients.pack(fill="both", expand=True, pady=(0, 10))
        self.recipients_tree = self._make_tree(
            recipients,
            [
                ("supplier", "Поставщик", 205),
                ("status", "Состояние", 185),
                ("files", "Файлов", 70),
                ("response", "Последний ответ", 145),
            ],
        )
        self.recipients_tree.configure(height=6)
        self.recipients_tree.pack(fill="both", expand=True)
        recipient_actions = ttk.Menubutton(recipients, text="Изменить состояние адресата")
        recipient_actions.pack(anchor="w", pady=(8, 0))
        recipient_menu = tk.Menu(recipient_actions, tearoff=False)
        for label, status in (("Закрыть без ответа", "closed_no_response"),
                              ("Отказ поставщика", "declined"),
                              ("Вернуть в ожидание", "sent")):
            recipient_menu.add_command(
                label=label, command=lambda value=status: self._set_selected_recipient_status(value)
            )
        recipient_actions["menu"] = recipient_menu

        responses = ttk.LabelFrame(right, text="Ответы по этой рассылке", padding=8)
        responses.pack(fill="both", expand=True)
        self.campaign_inbox_tree = self._make_tree(
            responses,
            [("received", "Получено", 135), ("sender", "Отправитель", 185),
             ("subject", "Тема", 230), ("files", "Файлов", 65)],
        )
        self.campaign_inbox_tree.configure(height=7)
        self.campaign_inbox_tree.pack(fill="both", expand=True)
        self.campaign_inbox_tree.bind("<Double-1>", self._open_campaign_response)
        ttk.Button(responses, text="Открыть ответ", command=self._open_campaign_response).pack(
            anchor="w", pady=(8, 0)
        )

    def _build_inbox_tab(self) -> None:
        self.inbox_tab.columnconfigure(0, weight=1)
        self.inbox_tab.rowconfigure(1, weight=2)
        self.inbox_tab.rowconfigure(4, weight=1)
        top = ttk.Frame(self.inbox_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(top, text="Требуют внимания", style="Header.TLabel").pack(side="left")
        ttk.Button(top, text="Проверить сейчас", command=self._manual_check).pack(side="right")
        self.show_all_incoming_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Все ответы", variable=self.show_all_incoming_var,
                        command=self.refresh_inbox).pack(side="right", padx=12)
        table = ttk.Frame(self.inbox_tab)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.inbox_tree = self._make_tree(
            table,
            [
                ("received", "Получено", 140),
                ("sender", "Отправитель", 190),
                ("subject", "Тема", 260),
                ("campaign", "Рассылка", 120),
                ("supplier", "Поставщик", 140),
                ("result", "Результат письма", 155),
                ("files", "Файлов", 65),
                ("match", "Привязка", 135),
            ],
        )
        self.inbox_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.inbox_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.inbox_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.inbox_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.inbox_tree.bind("<<TreeviewSelect>>", self._on_incoming_selected)
        actions = ttk.Frame(self.inbox_tab)
        actions.grid(row=2, column=0, sticky="ew", pady=8)
        ttk.Button(actions, text="Привязать вручную", command=self._open_assign_dialog).pack(side="left")
        ttk.Button(actions, text="Открыть папку с файлами", command=self._open_incoming_folder).pack(
            side="left", padx=6
        )
        ttk.Label(self.inbox_tab,
                  text="Здесь показаны письма, которым нужна привязка. Остальные ответы — в выбранной рассылке.",
                  style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 6))

        details = ttk.Panedwindow(self.inbox_tab, orient="horizontal")
        details.grid(row=4, column=0, sticky="nsew")
        body_frame = ttk.LabelFrame(details, text="Текст ответа", padding=5)
        files_frame = ttk.LabelFrame(details, text="Вложения", padding=5)
        details.add(body_frame, weight=3)
        details.add(files_frame, weight=2)
        body_frame.rowconfigure(0, weight=1)
        body_frame.columnconfigure(0, weight=1)
        self.incoming_body = scrolledtext.ScrolledText(
            body_frame, wrap="word", height=8, font=("Segoe UI", 9), state="disabled"
        )
        self.incoming_body.grid(row=0, column=0, sticky="nsew")
        files_frame.rowconfigure(0, weight=1)
        files_frame.columnconfigure(0, weight=1)
        self.incoming_files_tree = self._make_tree(
            files_frame,
            [
                ("filename", "Файл", 260),
                ("type", "Тип", 80),
                ("duplicate", "Дубль", 65),
            ],
        )
        self.incoming_files_tree.grid(row=0, column=0, sticky="nsew")
        self.incoming_files_tree.bind("<Double-1>", self._open_selected_attachment)

    def _build_logs_tab(self) -> None:
        self.logs_tab.columnconfigure(0, weight=1)
        self.logs_tab.rowconfigure(1, weight=1)
        top = ttk.Frame(self.logs_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(top, text="Журнал операций", style="Header.TLabel").pack(side="left")
        ttk.Button(top, text="Обновить", command=self.refresh_logs).pack(side="right")
        self.logs_tree = self._make_tree(
            self.logs_tab,
            [
                ("time", "Время", 150),
                ("level", "Уровень", 90),
                ("event", "Событие", 210),
                ("campaign", "Рассылка", 90),
                ("details", "Подробности", 600),
            ],
        )
        self.logs_tree.grid(row=1, column=0, sticky="nsew")

    def _build_settings_tab(self) -> None:
        self.settings_tab.columnconfigure(0, weight=1)
        ttk.Label(self.settings_tab, text="Настройки почты", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )
        form = ttk.LabelFrame(self.settings_tab, text="Почтовый ящик", padding=16)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.settings_provider_var = tk.StringVar(value=MAIL_PROVIDERS["mailru"].title)
        self.settings_email_var = tk.StringVar()
        self.settings_password_var = tk.StringVar()
        self.settings_interval_var = tk.StringVar(value="10")
        self.password_state_var = tk.StringVar(value="Пароль не сохранён")
        self.settings_password_hint_var = tk.StringVar()
        ttk.Label(form, text="Почтовый сервис").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        provider_box = ttk.Combobox(
            form,
            textvariable=self.settings_provider_var,
            values=[provider.title for provider in MAIL_PROVIDERS.values()],
            state="readonly",
            width=14,
        )
        provider_box.grid(row=0, column=1, sticky="w", pady=5)
        provider_box.bind("<<ComboboxSelected>>", lambda _event: self._update_password_hint())
        ttk.Label(form, text="Адрес почты").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Entry(form, textvariable=self.settings_email_var).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(form, text="Пароль").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Entry(form, textvariable=self.settings_password_var, show="●").grid(
            row=2, column=1, sticky="ew", pady=5
        )
        ttk.Label(
            form,
            textvariable=self.settings_password_hint_var,
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=3, column=1, sticky="w")
        ttk.Label(form, text="Интервал проверки").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=10)
        ttk.Combobox(
            form,
            textvariable=self.settings_interval_var,
            values=("5", "10", "15", "30", "60"),
            state="readonly",
            width=8,
        ).grid(row=4, column=1, sticky="w", pady=10)
        ttk.Label(form, textvariable=self.password_state_var, style="Muted.TLabel").grid(
            row=5, column=1, sticky="w"
        )
        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Button(actions, text="Сохранить", command=self._save_settings).pack(side="left")
        ttk.Button(actions, text="Сохранить и проверить", command=self._save_and_test_settings).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="Удалить сохранённый пароль", command=self._delete_password).pack(side="left")

        storage = ttk.LabelFrame(self.settings_tab, text="Данные и резервные копии", padding=16)
        storage.grid(row=2, column=0, sticky="ew", pady=16)
        storage.columnconfigure(0, weight=1)
        ttk.Label(storage, text=str(self.service.paths.root), wraplength=900).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        ttk.Button(storage, text="Открыть папку данных", command=self._open_data_folder).grid(row=1, column=0, sticky="w")
        ttk.Button(storage, text="Создать резервную копию", command=self._create_backup).grid(
            row=1, column=1, sticky="w", padx=8
        )
        ttk.Button(storage, text="Журнал операций", command=self._open_logs_dialog).grid(
            row=1, column=2, sticky="w", padx=8
        )

        note = ttk.LabelFrame(self.settings_tab, text="Важно", padding=14)
        note.grid(row=3, column=0, sticky="ew")
        ttk.Label(
            note,
            text=(
                "Mail.ru: нужен отдельный пароль для внешнего приложения с полным доступом к Почте, "
                "основной пароль аккаунта не подойдёт. Timeweb: логин — полный адрес ящика, пароль — "
                "обычный пароль от ящика. Пароль хранится в Windows Credential Manager, а не в SQLite."
            ),
            wraplength=950,
            justify="left",
        ).pack(anchor="w")

    @staticmethod
    def _make_tree(parent: tk.Misc, columns: list[tuple[str, str, int]],
                   selectmode: str = "browse") -> ttk.Treeview:
        keys = [item[0] for item in columns]
        tree = ttk.Treeview(parent, columns=keys, show="headings", selectmode=selectmode)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=55, stretch=True)
        return tree

    def refresh_all(self) -> None:
        self.refresh_dashboard()
        self.refresh_suppliers()
        self.refresh_search_candidates()
        self.refresh_campaigns()
        self.refresh_inbox()
        if getattr(self, "_logs_dialog", None) is not None and self._logs_dialog.winfo_exists():
            self.refresh_logs()
        self.refresh_settings()

    def refresh_dashboard(self) -> None:
        stats = self.db.dashboard_stats()
        self.overview_var.set(
            f"Ожидаем ответы: {stats.get('active', 0)}   •   "
            f"К сводной: {stats.get('ready', 0)}"
        )

    def refresh_suppliers(self) -> None:
        selected = self.suppliers_tree.selection()
        self.suppliers_tree.delete(*self.suppliers_tree.get_children())
        suppliers = self.db.list_suppliers()
        categories = [ALL_CATEGORIES] + self.db.list_categories()
        self.supplier_filter_box.configure(values=categories)
        if self.supplier_filter_var.get() not in categories:
            self.supplier_filter_var.set(ALL_CATEGORIES)
        selected_category = self.supplier_filter_var.get()
        search = self.supplier_search_var.get()
        self._campaign_supplier_ids = []
        compose_list = getattr(self, "compose_suppliers_list", None)
        if compose_list is not None and compose_list.winfo_exists():
            compose_list.delete(0, "end")
        else:
            compose_list = None
        self._supplier_rows = {int(row["id"]): row for row in suppliers}
        for row in suppliers:
            iid = str(row["id"])
            row_categories = [value.strip() for value in row["categories"].split(",") if value.strip()]
            category_ok = (selected_category == ALL_CATEGORIES or
                           selected_category.casefold() in (value.casefold() for value in row_categories))
            if category_ok and text_matches(search, row["name"], row["email"],
                                            row["categories"], row["notes"]):
                self.suppliers_tree.insert(
                    "", "end", iid=iid,
                    values=(row["name"], row["email"], row["categories"],
                            "⛔ Исключён" if row["excluded"] else "Да", row["notes"]),
                    tags=("excluded",) if row["excluded"] else (),
                )
            if row["excluded"]:
                continue
            self._campaign_supplier_ids.append(int(row["id"]))
            if compose_list is not None:
                compose_list.insert("end", f"{row['name']}  <{row['email']}>")
        visible = [iid for iid in selected if self.suppliers_tree.exists(iid)]
        if visible:
            self.suppliers_tree.selection_set(visible)
        self._update_suppliers_count()

    def _update_suppliers_count(self) -> None:
        shown = len(self.suppliers_tree.get_children())
        selected = len(self.suppliers_tree.selection())
        self.suppliers_count_var.set(
            f"Показано {shown} из {len(self._supplier_rows)}, выбрано {selected}"
        )

    def refresh_campaigns(self) -> None:
        selection = self.campaigns_tree.selection()
        selected_id = selection[0] if selection else None
        self.campaigns_tree.delete(*self.campaigns_tree.get_children())
        for row in self.db.list_campaigns():
            iid = str(row["id"])
            self.campaigns_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row["name"],
                    CAMPAIGN_STATUS_LABELS.get(row["status"], row["status"]),
                ),
            )
        if selected_id and self.campaigns_tree.exists(selected_id):
            self.campaigns_tree.selection_set(selected_id)
        elif self.campaigns_tree.get_children():
            self.campaigns_tree.selection_set(self.campaigns_tree.get_children()[0])
        self._on_campaign_selected()

    def _clear_campaign_details(self) -> None:
        self.campaign_title_var.set("Выберите рассылку")
        self.campaign_info_var.set("Здесь появятся поставщики и ответы")
        self.summary_button.configure(state="disabled")
        self.recipients_tree.delete(*self.recipients_tree.get_children())
        self.campaign_inbox_tree.delete(*self.campaign_inbox_tree.get_children())

    def refresh_inbox(self) -> None:
        selection = self.inbox_tree.selection()
        selected_id = selection[0] if selection else None
        self.inbox_tree.delete(*self.inbox_tree.get_children())
        show_all = self.show_all_incoming_var.get()
        for row in self.db.list_incoming(needs_review=None if show_all else True):
            campaign = row["campaign_code"] or "—"
            if row["needs_review"]:
                campaign = f"⚠ {campaign}"
            self.inbox_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    display_datetime(row["received_at"] or row["created_at"]),
                    row["sender_email"],
                    row["subject"],
                    campaign,
                    row["supplier_name"] or "—",
                    incoming_result_label(row),
                    row["attachment_count"] or 0,
                    MATCH_METHOD_LABELS.get(row["match_method"], row["match_method"]),
                ),
            )
        if selected_id and self.inbox_tree.exists(selected_id):
            self.inbox_tree.selection_set(selected_id)
            self._load_incoming_details(int(selected_id))
        else:
            self._set_incoming_body("")
            self.incoming_files_tree.delete(*self.incoming_files_tree.get_children())

    def refresh_logs(self) -> None:
        self.logs_tree.delete(*self.logs_tree.get_children())
        for row in self.db.list_logs():
            self.logs_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    display_datetime(row["created_at"]),
                    row["level"].upper(),
                    row["event_type"],
                    row["campaign_id"] or "—",
                    row["details"],
                ),
            )

    def refresh_settings(self) -> None:
        preferences = self.service.get_mail_preferences()
        self.settings_provider_var.set(MAIL_PROVIDERS[preferences["mail_provider"]].title)
        self._update_password_hint()
        self.settings_email_var.set(preferences["email_address"])
        self.settings_interval_var.set(preferences["check_interval_minutes"] or "10")
        self.password_state_var.set(
            "Пароль сохранён в Windows" if preferences["has_password"] == "1" else "Пароль не сохранён"
        )

    def _on_supplier_selected(self, _event: tk.Event | None = None) -> None:
        self._update_suppliers_count()
        selection = self.suppliers_tree.selection()
        if not selection:
            return
        if len(selection) > 1:
            # В карточке редактируется только один поставщик; для группы — меню действий.
            self._clear_supplier_fields()
            return
        self._selected_supplier_id = int(selection[0])
        row = self._supplier_rows.get(self._selected_supplier_id)
        if row is None:
            return
        self.supplier_name_var.set(row["name"])
        self.supplier_email_var.set(row["email"])
        self.supplier_categories_var.set(row["categories"])
        self.supplier_notes.delete("1.0", "end")
        self.supplier_notes.insert("1.0", row["notes"])
        self.supplier_excluded_var.set(bool(row["excluded"]))
        self.supplier_excluded_reason_var.set(row["excluded_reason"])
        self._update_exclusion_reason_state()

    def _clear_supplier_form(self) -> None:
        self.suppliers_tree.selection_remove(*self.suppliers_tree.selection())
        self._clear_supplier_fields()

    def _clear_supplier_fields(self) -> None:
        self._selected_supplier_id = None
        self.supplier_name_var.set("")
        self.supplier_email_var.set("")
        self.supplier_categories_var.set("")
        self.supplier_notes.delete("1.0", "end")
        self.supplier_excluded_var.set(False)
        self.supplier_excluded_reason_var.set("")
        self._update_exclusion_reason_state()

    def _update_exclusion_reason_state(self) -> None:
        self.supplier_excluded_reason_entry.configure(
            state="normal" if self.supplier_excluded_var.get() else "disabled"
        )

    def _save_supplier(self) -> None:
        name = self.supplier_name_var.get().strip()
        email = self.supplier_email_var.get().strip()
        if not name or "@" not in email:
            messagebox.showwarning("Поставщик", "Введите название и корректный email.", parent=self)
            return
        try:
            supplier_id = self.db.save_supplier(
                name,
                email,
                self.supplier_notes.get("1.0", "end").strip(),
                self._selected_supplier_id,
                categories=clean_categories(self.supplier_categories_var.get().split(",")),
                excluded=self.supplier_excluded_var.get(),
                excluded_reason=self.supplier_excluded_reason_var.get(),
            )
        except sqlite3.IntegrityError:
            messagebox.showerror("Поставщик", "Поставщик с таким email уже существует.", parent=self)
            return
        self._selected_supplier_id = supplier_id
        self.refresh_suppliers()
        self.refresh_dashboard()
        self.status_var.set("Поставщик сохранён")

    def _delete_supplier(self) -> None:
        supplier_ids = self._selected_ids(self.suppliers_tree)
        if not supplier_ids:
            return
        question = ("Удалить выбранного поставщика?" if len(supplier_ids) == 1 else
                    f"Удалить выбранных поставщиков: {len(supplier_ids)}?")
        if not messagebox.askyesno("Удаление", question, parent=self):
            return
        kept = 0
        for supplier_id in supplier_ids:
            try:
                self.db.delete_supplier(supplier_id)
            except sqlite3.IntegrityError:
                kept += 1
        if kept:
            messagebox.showwarning(
                "Удаление",
                f"Не удалено: {kept}. Эти поставщики уже участвуют в рассылках "
                "и должны остаться в истории — их можно исключить из рассылок.",
                parent=self,
            )
        self._clear_supplier_form()
        self.refresh_suppliers()
        self.refresh_dashboard()
        self.status_var.set(f"Удалено поставщиков: {len(supplier_ids) - kept}")

    def _set_selected_suppliers_excluded(self, excluded: bool) -> None:
        supplier_ids = self._selected_ids(self.suppliers_tree)
        if not supplier_ids:
            self.status_var.set("Выберите поставщиков в таблице")
            return
        reason = ""
        if excluded:
            answer = simpledialog.askstring(
                "Исключение", f"Причина исключения ({len(supplier_ids)} пост.):", parent=self
            )
            if answer is None:
                return
            reason = answer
        changed = self.db.set_suppliers_excluded(supplier_ids, excluded, reason)
        self.refresh_suppliers()
        self._on_supplier_selected()
        self.status_var.set(
            f"{'Исключено из рассылок' if excluded else 'Возвращено в рассылки'}: {changed}"
        )

    def _change_selected_suppliers_category(self, remove: bool) -> None:
        supplier_ids = self._selected_ids(self.suppliers_tree)
        if not supplier_ids:
            self.status_var.set("Выберите поставщиков в таблице")
            return
        category = simpledialog.askstring(
            "Категория",
            f"{'Убрать' if remove else 'Добавить'} категорию ({len(supplier_ids)} пост.):",
            parent=self,
        )
        if not category or not category.strip():
            return
        changed = self.db.change_suppliers_category(supplier_ids, category, remove=remove)
        self.refresh_suppliers()
        self._on_supplier_selected()
        self.status_var.set(
            f"Категория «{category.strip()}» {'убрана' if remove else 'добавлена'}: {changed} пост."
        )

    def refresh_search_candidates(self) -> None:
        rows = self.db.list_candidates()
        self._candidate_rows = {int(row["id"]): row for row in rows}
        categories = sorted(
            {value for row in rows for value in json.loads(row["categories_json"])},
            key=str.casefold,
        )
        values = [ALL_CATEGORIES, *categories]
        self.candidate_filter_category_box.configure(values=values)
        if self.candidate_filter_category_var.get() not in values:
            self.candidate_filter_category_var.set(ALL_CATEGORIES)
        self._fill_candidates_tree()

    def _fill_candidates_tree(self) -> None:
        selected = self.candidates_tree.selection()
        self.candidates_tree.delete(*self.candidates_tree.get_children())
        search = self.candidate_filter_text_var.get()
        status_label = self.candidate_filter_status_var.get()
        category = self.candidate_filter_category_var.get().casefold()
        contacts_match = CONTACT_FILTERS.get(
            self.candidate_filter_contacts_var.get(), CONTACT_FILTERS[ANY_CONTACTS]
        )
        for candidate_id, row in self._candidate_rows.items():
            categories = json.loads(row["categories_json"])
            phones = json.loads(row["phones_json"])
            status = CANDIDATE_STATUS_LABELS.get(row["status"], row["status"])
            if status_label != ALL_STATUSES and status != status_label:
                continue
            if (category != ALL_CATEGORIES.casefold() and
                    category not in (value.casefold() for value in categories)):
                continue
            if not contacts_match(bool(row["email"]), bool(phones)):
                continue
            if not text_matches(search, row["name"], row["email"], row["website"],
                                row["region"], row["evidence"], row["search_query"],
                                " ".join(categories), " ".join(phones),
                                row["contact_person"], row["address"]):
                continue
            phone = phones[0] if phones else ""
            if len(phones) > 1:
                phone += f" (+{len(phones) - 1})"
            self.candidates_tree.insert(
                "", "end", iid=str(candidate_id), values=(
                    row["name"], ", ".join(categories), row["region"], row["email"], phone,
                    status,
                )
            )
        visible = [iid for iid in selected if self.candidates_tree.exists(iid)]
        if visible:
            self.candidates_tree.selection_set(visible)
        self._on_candidate_selected()

    def _reset_candidate_filters(self) -> None:
        self.candidate_filter_text_var.set("")
        self.candidate_filter_status_var.set(ALL_STATUSES)
        self.candidate_filter_category_var.set(ALL_CATEGORIES)
        self.candidate_filter_contacts_var.set(ANY_CONTACTS)
        self._fill_candidates_tree()

    def _update_candidates_count(self) -> None:
        shown = len(self.candidates_tree.get_children())
        selected = len(self.candidates_tree.selection())
        self.candidates_count_var.set(
            f"Показано {shown} из {len(self._candidate_rows)}, выбрано {selected}"
        )

    def _on_candidate_selected(self, _event: tk.Event | None = None) -> None:
        self._update_candidates_count()
        selection = self.candidates_tree.selection()
        if len(selection) != 1:
            # Карточка редактирует одну компанию; для группы — меню «Действия с выбранными».
            self._selected_candidate_id = None
            self._clear_candidate_form()
            return
        candidate_id = int(selection[0])
        row = self.db.get_candidate(candidate_id)
        if row is None:
            return
        self._selected_candidate_id = candidate_id
        self.candidate_name_var.set(row["name"])
        self.candidate_site_var.set(row["website"])
        self.candidate_email_var.set(row["email"])
        self.candidate_region_var.set(row["region"])
        self.candidate_categories_var.set(", ".join(json.loads(row["categories_json"])))
        self.candidate_contact_url_var.set(row["contact_source_url"])
        self.candidate_phones_var.set(", ".join(json.loads(row["phones_json"])))
        self.candidate_person_var.set(row["contact_person"])
        self.candidate_address_var.set(row["address"])
        self.candidate_sources_text.delete("1.0", "end")
        self.candidate_sources_text.insert(
            "1.0", "\n".join(json.loads(row["source_urls_json"]))
        )
        self.candidate_evidence_text.delete("1.0", "end")
        self.candidate_evidence_text.insert("1.0", row["evidence"])
        state = "disabled" if row["status"] == "approved" else "normal"
        for button in (self.candidate_save_button, self.candidate_approve_button,
                       self.candidate_reject_button):
            button.configure(state=state)

    def _clear_candidate_form(self) -> None:
        for variable in (self.candidate_name_var, self.candidate_site_var,
                         self.candidate_email_var, self.candidate_region_var,
                         self.candidate_categories_var, self.candidate_contact_url_var,
                         self.candidate_phones_var, self.candidate_person_var,
                         self.candidate_address_var):
            variable.set("")
        self.candidate_sources_text.delete("1.0", "end")
        self.candidate_evidence_text.delete("1.0", "end")
        for button in (self.candidate_save_button, self.candidate_approve_button,
                       self.candidate_reject_button):
            button.configure(state="disabled")

    def _approve_selected_candidates(self) -> None:
        candidate_ids = self._selected_ids(self.candidates_tree)
        if not candidate_ids:
            self.search_status_var.set("Выберите компании в таблице")
            return
        if len(candidate_ids) > 1 and not messagebox.askyesno(
            "Справочник", f"Добавить в справочник выбранные компании: {len(candidate_ids)}?",
            parent=self,
        ):
            return
        added = already = 0
        problems: list[str] = []
        for candidate_id in candidate_ids:
            row = self._candidate_rows.get(candidate_id)
            if row is not None and row["status"] == "approved":
                already += 1
                continue
            try:
                self.db.approve_candidate(candidate_id)
                added += 1
            except ValueError as exc:
                problems.append(f"{row['name'] if row else candidate_id}: {exc}")
        self.refresh_search_candidates()
        self.refresh_suppliers()
        message = f"Добавлено в справочник: {added}"
        if already:
            message += f", уже были в справочнике: {already}"
        if problems:
            message += f", пропущено: {len(problems)}"
            messagebox.showwarning("Справочник", "\n".join(problems[:15]), parent=self)
        self.search_status_var.set(message)

    def _set_selected_candidates_status(self, status: str) -> None:
        candidate_ids = self._selected_ids(self.candidates_tree)
        if not candidate_ids:
            self.search_status_var.set("Выберите компании в таблице")
            return
        changed = skipped = 0
        for candidate_id in candidate_ids:
            try:
                self.db.set_candidate_status(candidate_id, status)
                changed += 1
            except ValueError:
                skipped += 1
        self.refresh_search_candidates()
        label = "Отклонено" if status == "rejected" else "Возвращено на проверку"
        message = f"{label}: {changed}"
        if skipped:
            message += f", пропущено (уже в справочнике): {skipped}"
        self.search_status_var.set(message)

    def _add_category_to_selected_candidates(self) -> None:
        candidate_ids = self._selected_ids(self.candidates_tree)
        if not candidate_ids:
            self.search_status_var.set("Выберите компании в таблице")
            return
        category = simpledialog.askstring(
            "Категория", f"Добавить категорию ({len(candidate_ids)} комп.):", parent=self
        )
        if not category or not category.strip():
            return
        changed = self.db.add_candidates_category(candidate_ids, category)
        self.refresh_search_candidates()
        self.search_status_var.set(
            f"Категория «{category.strip()}» добавлена: {changed} комп. "
            "(у добавленных в справочник категории меняются в справочнике)"
        )

    def _delete_selected_candidates(self) -> None:
        candidate_ids = self._selected_ids(self.candidates_tree)
        if not candidate_ids:
            self.search_status_var.set("Выберите компании в таблице")
            return
        if not messagebox.askyesno(
            "Удаление",
            f"Удалить из списка найденных: {len(candidate_ids)}?\n"
            "Поставщики в справочнике не затрагиваются.",
            parent=self,
        ):
            return
        deleted = self.db.delete_candidates(candidate_ids)
        self.refresh_search_candidates()
        self.search_status_var.set(f"Удалено из списка найденных: {deleted}")

    def _save_candidate(self) -> bool:
        if self._selected_candidate_id is None:
            self.search_status_var.set("Выберите компанию в таблице")
            return False
        try:
            name = self.candidate_name_var.get().strip()
            if not name:
                raise ValueError("Введите название компании")
            website = normalize_web_url(self.candidate_site_var.get())
            email = normalize_email(self.candidate_email_var.get())
            contact_url = normalize_web_url(self.candidate_contact_url_var.get())
            phones = clean_phones(self.candidate_phones_var.get().split(","))
            person = self.candidate_person_var.get().strip()
            if (email or phones or person) and not contact_url:
                raise ValueError("Для контактов укажите страницу, где они опубликованы")
            sources = list(dict.fromkeys(
                normalize_web_url(value) for value in
                self.candidate_sources_text.get("1.0", "end").splitlines() if value.strip()
            ))
            if not sources:
                raise ValueError("Укажите хотя бы один источник")
            self.db.update_candidate(
                self._selected_candidate_id, name=name, website=website, email=email,
                region=self.candidate_region_var.get().strip(),
                categories=clean_categories(self.candidate_categories_var.get().split(",")),
                evidence=self.candidate_evidence_text.get("1.0", "end").strip(),
                source_urls=sources, contact_source_url=contact_url,
                phones=phones, contact_person=person,
                address=self.candidate_address_var.get().strip(),
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            messagebox.showerror("Кандидат", str(exc), parent=self)
            return False
        self.refresh_search_candidates()
        self.search_status_var.set("Изменения сохранены")
        return True

    def _approve_candidate(self) -> None:
        if self._selected_candidate_id is not None:
            candidate = self.db.get_candidate(self._selected_candidate_id)
            if candidate is not None and candidate["status"] == "approved":
                self.search_status_var.set("Поставщик уже находится в справочнике")
                return
        if not self._save_candidate():
            return
        assert self._selected_candidate_id is not None
        try:
            self.db.approve_candidate(self._selected_candidate_id)
        except ValueError as exc:
            messagebox.showwarning("Поставщик", str(exc), parent=self)
            return
        self.refresh_search_candidates()
        self.refresh_suppliers()
        self.search_status_var.set("Поставщик добавлен в справочник")

    def _reject_candidate(self) -> None:
        if self._selected_candidate_id is None:
            return
        try:
            self.db.set_candidate_status(self._selected_candidate_id, "rejected")
        except ValueError as exc:
            messagebox.showwarning("Кандидат", str(exc), parent=self)
            return
        self.refresh_search_candidates()
        self.search_status_var.set("Кандидат отклонён")

    def _open_candidate_source(self) -> None:
        values = self.candidate_sources_text.get("1.0", "end").splitlines()
        if not values:
            return
        try:
            url = normalize_web_url(values[0])
        except ValueError as exc:
            messagebox.showerror("Источник", str(exc), parent=self)
            return
        if url:
            webbrowser.open(url)

    def _start_supplier_search(self) -> None:
        if self._background_busy:
            self.search_status_var.set("Дождитесь завершения текущей операции")
            return
        query = self.search_query_text.get("1.0", "end").strip()
        region = self.search_region_var.get().strip()
        maximum_companies = int(self.search_limit_var.get())
        if len(query) < 5:
            messagebox.showwarning("Поиск", "Опишите, каких поставщиков найти.", parent=self)
            return
        self._supplier_search_cancel.clear()
        self._search_active = True
        self.search_start_button.configure(state="disabled")
        self.search_cancel_button.configure(state="normal")
        self.search_status_var.set("Codex ищет поставщиков. Это может занять несколько минут...")

        def operation() -> OperationResult:
            result = run_codex_search(
                query, region, self.db.list_categories(),
                project_root=Path(__file__).resolve().parent.parent,
                data_root=self.service.paths.root,
                maximum_companies=maximum_companies,
                cancel_event=self._supplier_search_cancel,
            )
            if self._supplier_search_cancel.is_set():
                raise SearchCancelled("Поиск остановлен")
            added, existing = self.db.import_candidates(query, region, result.candidates)
            message = (f"Поиск завершён: новых {added}, уже найденных {existing}, "
                       f"отброшено неполных записей {result.rejected_count}.")
            return OperationResult(message, {"new": added, "existing": existing})

        self._run_background("Идёт поиск поставщиков", operation, self._on_supplier_search_done)

    def _on_supplier_search_done(self, result: OperationResult) -> None:
        self.refresh_search_candidates()
        self.search_status_var.set(result.message)

    def _cancel_supplier_search(self) -> None:
        self._supplier_search_cancel.set()
        self.search_cancel_button.configure(state="disabled")
        self.search_status_var.set("Останавливаем поиск...")

    def _export_search_candidates(self, only_selected: bool) -> None:
        if only_selected:
            ids = self._selected_ids(self.candidates_tree)
            if not ids:
                messagebox.showinfo("Excel", "Выберите компании в таблице.", parent=self)
                return
        else:
            # «Все» — всё, что показано в таблице с учётом фильтра.
            ids = [int(iid) for iid in self.candidates_tree.get_children()]
            if not ids:
                messagebox.showinfo("Excel", "В таблице нет компаний для выгрузки.", parent=self)
                return
        rows = [self._candidate_rows[value] for value in ids if value in self._candidate_rows]
        filename = filedialog.asksaveasfilename(
            title="Сохранить найденных поставщиков", defaultextension=".xlsx",
            filetypes=[("Excel XLSX", "*.xlsx")], parent=self,
        )
        if not filename:
            return
        try:
            export_candidates_xlsx(rows, Path(filename))
        except Exception as exc:
            messagebox.showerror("Excel", str(exc), parent=self)
            return
        self.search_status_var.set(f"Выгружено компаний: {len(rows)} — {filename}")

    def _add_campaign_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Выберите дополнительные файлы", parent=self._compose_dialog or self
        )
        for path in selected:
            if path not in self._campaign_attachment_paths:
                self._campaign_attachment_paths.append(path)
                self.compose_attachments_list.insert("end", Path(path).name)

    def _remove_campaign_file(self) -> None:
        selection = list(self.compose_attachments_list.curselection())
        for index in reversed(selection):
            del self._campaign_attachment_paths[index]
            self.compose_attachments_list.delete(index)

    def _create_campaign(self) -> None:
        indexes = self.compose_suppliers_list.curselection()
        supplier_ids = [self._campaign_supplier_ids[index] for index in indexes]
        if not supplier_ids:
            messagebox.showwarning("Рассылка", "Выберите хотя бы одного поставщика.",
                                   parent=self._compose_dialog or self)
            return
        if not messagebox.askyesno(
            "Подтверждение отправки",
            f"Будет отправлено отдельных писем: {len(supplier_ids)}. Продолжить?",
            parent=self._compose_dialog or self,
        ):
            return
        parameters = {
            "name": self.campaign_name_var.get(),
            "subject": self.campaign_subject_var.get(),
            "body": self.campaign_body.get("1.0", "end").strip(),
            "deadline": self.campaign_deadline_var.get(),
            "supplier_ids": supplier_ids,
            "attachment_paths": list(self._campaign_attachment_paths),
            "request_path": self.campaign_request_var.get().strip() or None,
        }
        self._run_background(
            "Отправка рассылки…",
            lambda: self.service.create_and_send_campaign(**parameters),
            on_success=self._campaign_created,
            notify=True,
        )

    def _campaign_created(self, _result: OperationResult) -> None:
        if self._compose_dialog is not None and self._compose_dialog.winfo_exists():
            self._compose_dialog.destroy()
        self._compose_dialog = None
        self._campaign_attachment_paths.clear()
        self.refresh_all()
        self.notebook.select(self.campaigns_tab)

    def _on_campaign_selected(self, _event: tk.Event | None = None) -> None:
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        if campaign_id is None:
            self._clear_campaign_details()
            return
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            self._clear_campaign_details()
            return
        recipients = self.db.list_recipients(campaign_id)
        replies = self.db.list_incoming(campaign_id=campaign_id)
        self.campaign_title_var.set(campaign["name"])
        state = CAMPAIGN_STATUS_LABELS.get(campaign["status"], campaign["status"])
        self.campaign_info_var.set(
            f"{state}  •  {len(recipients)} поставщиков  •  {len(replies)} ответов  •  "
            f"служебный номер {campaign['code']}"
        )
        self.summary_button.configure(
            state="normal" if campaign["status"] in ("ready", "closed") else "disabled"
        )
        self._load_recipients(campaign_id, recipients)
        self._load_campaign_responses(replies)

    def _load_recipients(self, campaign_id: int, recipients: list[sqlite3.Row] | None = None) -> None:
        self.recipients_tree.delete(*self.recipients_tree.get_children())
        for row in recipients if recipients is not None else self.db.list_recipients(campaign_id):
            self.recipients_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    ("⛔ " if row["supplier_excluded"] else "") + row["supplier_name"],
                    RECIPIENT_STATUS_LABELS.get(row["status"], row["status"]),
                    row["file_count"],
                    display_datetime(row["last_response_at"]),
                ),
            )

    def _load_campaign_responses(self, replies: list[sqlite3.Row]) -> None:
        self.campaign_inbox_tree.delete(*self.campaign_inbox_tree.get_children())
        for row in replies:
            self.campaign_inbox_tree.insert(
                "", "end", iid=str(row["id"]),
                values=(display_datetime(row["received_at"] or row["created_at"]),
                        row["sender_email"], row["subject"], row["attachment_count"] or 0),
            )

    def _open_campaign_response(self, _event: tk.Event | None = None) -> None:
        incoming_id = self._selected_tree_id(self.campaign_inbox_tree)
        if incoming_id is None:
            return
        message = self.db.get_incoming(incoming_id)
        if message is None:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Ответ поставщика")
        dialog.geometry("860x610")
        dialog.transient(self)
        ttk.Label(dialog, text=message["subject"] or "Без темы", style="Header.TLabel").pack(
            anchor="w", padx=16, pady=(14, 3)
        )
        ttk.Label(dialog, text=f"От: {message['sender_email']}  •  "
                  f"{display_datetime(message['received_at'] or message['created_at'])}").pack(
            anchor="w", padx=16, pady=(0, 12)
        )
        ttk.Label(dialog, text="Текст письма").pack(anchor="w", padx=16)
        body = scrolledtext.ScrolledText(dialog, wrap="word", height=13, font=("Segoe UI", 10))
        body.pack(fill="both", expand=True, padx=16, pady=(4, 12))
        body.insert("1.0", message["body_text"] or "")
        body.configure(state="disabled")
        ttk.Label(dialog, text="Вложения — двойной щелчок открывает файл").pack(anchor="w", padx=16)
        files = self._make_tree(dialog, [("name", "Файл", 500), ("type", "Тип", 120)])
        files.pack(fill="both", expand=True, padx=16, pady=(4, 10))
        paths: dict[str, Path] = {}
        for attachment in self.db.list_incoming_attachments(incoming_id):
            key = str(attachment["id"])
            paths[key] = Path(attachment["path"])
            files.insert("", "end", iid=key,
                         values=(attachment["filename"],
                                 "PDF/Excel" if attachment["is_allowed"] else "Другой"))
        files.bind("<Double-1>", lambda _evt: self._open_path(paths[files.selection()[0]])
                   if files.selection() else None)
        ttk.Button(dialog, text="Закрыть", command=dialog.destroy).pack(
            anchor="e", padx=16, pady=(0, 14)
        )

    def _retry_campaign(self) -> None:
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        if campaign_id is None:
            return
        self._run_background(
            "Повторная отправка…",
            lambda: self.service.send_campaign(campaign_id),
            on_success=lambda _result: self.refresh_all(),
            notify=True,
        )

    def _open_campaign_folder(self) -> None:
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        if campaign_id is None:
            return
        campaign = self.db.get_campaign(campaign_id)
        if campaign:
            self._open_path(self.service.paths.campaigns / campaign["code"])

    def _close_campaign(self) -> None:
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        if campaign_id is None:
            return
        if messagebox.askyesno(
            "Завершение",
            "Завершить выбранную рассылку? Автоматическое ожидание ответов прекратится.",
            parent=self,
        ):
            self.db.set_campaign_closed(campaign_id)
            self.refresh_all()

    def _create_summary(self) -> None:
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        if campaign_id is None:
            messagebox.showinfo("Сводная", "Сначала выберите рассылку.", parent=self)
            return
        campaign = self.db.get_campaign(campaign_id)
        if campaign and campaign["status"] not in ("ready", "closed"):
            messagebox.showwarning(
                "Сводная",
                "Не все адресаты получили конечный статус. Проверьте ответы или закройте отсутствующие вручную.",
                parent=self,
            )
            return
        stored = Path(campaign["request_path"]) if campaign["request_path"] else None
        candidates = [row for row in self.db.list_outgoing_attachments(campaign_id)
                      if Path(row["filename"]).suffix.lower() == ".xlsx"]
        request_path: Path | None = None
        if not (stored and stored.is_file()) and len(candidates) != 1:
            outgoing = self.service.paths.campaigns / campaign["code"] / "outgoing" / "attachments"
            selected = filedialog.askopenfilename(
                title="Выберите исходную заявку XLSX",
                initialdir=str(outgoing if outgoing.is_dir() else self.service.paths.root),
                filetypes=[("Excel XLSX", "*.xlsx")],
                parent=self,
            )
            if not selected:
                return
            request_path = Path(selected)
        self._run_background(
            "Создание сводной…",
            lambda: self.service.create_summary(campaign_id, request_path),
            on_success=lambda result: self._open_path(Path(str(result.details["path"]))),
            notify=True,
        )

    def _set_selected_recipient_status(self, status: str) -> None:
        recipient_id = self._selected_tree_id(self.recipients_tree)
        if recipient_id is None:
            return
        self.db.set_recipient_status(recipient_id, status)
        campaign_id = self._selected_tree_id(self.campaigns_tree)
        self.refresh_campaigns()
        if campaign_id is not None:
            self._load_recipients(campaign_id)
        self.refresh_dashboard()

    def _on_incoming_selected(self, _event: tk.Event | None = None) -> None:
        incoming_id = self._selected_tree_id(self.inbox_tree)
        if incoming_id is not None:
            self._load_incoming_details(incoming_id)

    def _load_incoming_details(self, incoming_id: int) -> None:
        message = self.db.get_incoming(incoming_id)
        self._set_incoming_body(message["body_text"] if message else "")
        self.incoming_files_tree.delete(*self.incoming_files_tree.get_children())
        for row in self.db.list_incoming_attachments(incoming_id):
            self.incoming_files_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row["filename"],
                    "PDF/Excel" if row["is_allowed"] else "Другой",
                    "Да" if row["duplicate_of_id"] else "Нет",
                ),
                tags=(row["path"],),
            )

    def _set_incoming_body(self, value: str) -> None:
        self.incoming_body.configure(state="normal")
        self.incoming_body.delete("1.0", "end")
        self.incoming_body.insert("1.0", value)
        self.incoming_body.configure(state="disabled")

    def _open_assign_dialog(self) -> None:
        incoming_id = self._selected_tree_id(self.inbox_tree)
        if incoming_id is None:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Привязать ответ")
        dialog.geometry("760x430")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text="Выберите адресата и рассылку, к которым относится письмо",
            style="Section.TLabel",
        ).pack(anchor="w", padx=14, pady=12)
        tree = self._make_tree(
            dialog,
            [
                ("campaign", "Рассылка", 150),
                ("supplier", "Поставщик", 240),
                ("email", "Email", 230),
                ("status", "Статус", 150),
            ],
        )
        tree.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        for campaign in self.db.list_campaigns():
            if campaign["status"] not in ("active", "ready"):
                continue
            for recipient in self.db.list_recipients(int(campaign["id"])):
                tree.insert(
                    "",
                    "end",
                    iid=str(recipient["id"]),
                    values=(
                        campaign["code"],
                        recipient["supplier_name"],
                        recipient["email"],
                        RECIPIENT_STATUS_LABELS.get(recipient["status"], recipient["status"]),
                    ),
                )

        def assign() -> None:
            recipient_id = self._selected_tree_id(tree)
            if recipient_id is None:
                return
            try:
                self.service.assign_incoming(incoming_id, recipient_id)
            except Exception as exc:
                messagebox.showerror("Привязка", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.refresh_all()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="Привязать", command=assign).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=6)
        tree.bind("<Double-1>", lambda _event: assign())

    def _open_incoming_folder(self) -> None:
        incoming_id = self._selected_tree_id(self.inbox_tree)
        if incoming_id is None:
            return
        attachments = self.db.list_incoming_attachments(incoming_id)
        if attachments:
            self._open_path(Path(attachments[0]["path"]).parent)
            return
        message = self.db.get_incoming(incoming_id)
        if message:
            self._open_path(Path(message["raw_eml_path"]).parent)

    def _open_selected_attachment(self, _event: tk.Event | None = None) -> None:
        selection = self.incoming_files_tree.selection()
        if not selection:
            return
        tags = self.incoming_files_tree.item(selection[0], "tags")
        if tags:
            self._open_path(Path(tags[0]))

    def _selected_provider_key(self) -> str:
        title = self.settings_provider_var.get()
        return next((p.key for p in MAIL_PROVIDERS.values() if p.title == title), "mailru")

    def _update_password_hint(self) -> None:
        if self._selected_provider_key() == "mailru":
            hint = "Пароль для внешних приложений Mail.ru (Настройки → Безопасность). "
        else:
            hint = "Обычный пароль от почтового ящика. "
        self.settings_password_hint_var.set(
            hint + "Оставьте поле пустым, чтобы не менять уже сохранённый пароль."
        )

    def _save_settings(self, silent: bool = False) -> bool:
        try:
            interval = int(self.settings_interval_var.get())
            self.service.save_mail_preferences(
                self.settings_email_var.get(),
                interval,
                self.settings_password_var.get(),
                self._selected_provider_key(),
            )
        except Exception as exc:
            messagebox.showerror("Настройки", str(exc), parent=self)
            return False
        self.settings_password_var.set("")
        self._next_auto_check = time.monotonic() + self._interval_seconds()
        self.refresh_dashboard()
        self.refresh_settings()
        if not silent:
            messagebox.showinfo("Настройки", "Настройки сохранены.", parent=self)
        return True

    def _save_and_test_settings(self) -> None:
        if not self._save_settings(silent=True):
            return
        self._run_background(
            "Проверка подключения…",
            self.service.test_mail_connection,
            notify=True,
        )

    def _delete_password(self) -> None:
        if not messagebox.askyesno(
            "Удаление пароля", "Удалить сохранённый пароль почты?", parent=self
        ):
            return
        try:
            self.service.delete_saved_password()
        except Exception as exc:
            messagebox.showerror("Удаление пароля", str(exc), parent=self)
            return
        self.refresh_all()

    def _manual_check(self) -> None:
        self._check_mail("manual", notify=True)

    def _check_mail(self, trigger: str, notify: bool) -> None:
        self._run_background(
            "Проверка входящей почты…",
            lambda: self.service.check_mail(trigger),
            on_success=lambda _result: self.refresh_all(),
            notify=notify,
        )

    def _startup_check(self) -> None:
        try:
            preferences = self.service.get_mail_preferences()
            ready = preferences["email_address"] and preferences["has_password"] == "1"
            if ready and self.db.active_campaign_count() > 0:
                self._check_mail("startup", notify=False)
        except Exception as exc:
            self.status_var.set(f"Стартовая проверка пропущена: {exc}")

    def _timer_tick(self) -> None:
        try:
            if time.monotonic() >= self._next_auto_check and not self._background_busy:
                self._next_auto_check = time.monotonic() + self._interval_seconds()
                preferences = self.service.get_mail_preferences()
                ready = preferences["email_address"] and preferences["has_password"] == "1"
                if ready and self.db.active_campaign_count() > 0:
                    self._check_mail("timer", notify=False)
        finally:
            self.after(30_000, self._timer_tick)

    def _interval_seconds(self) -> int:
        try:
            minutes = int(self.db.get_setting("check_interval_minutes", "10"))
        except ValueError:
            minutes = 10
        return max(5, minutes) * 60

    def _create_backup(self) -> None:
        self._run_background(
            "Создание резервной копии…",
            self.service.create_backup,
            on_success=lambda _result: self.refresh_logs(),
            notify=True,
        )

    def _open_data_folder(self) -> None:
        self._open_path(self.service.paths.root)

    @staticmethod
    def _open_path(path: Path) -> None:
        path = path.resolve()
        if path.exists():
            os.startfile(str(path))

    @staticmethod
    def _selected_ids(tree: ttk.Treeview) -> list[int]:
        # В порядке строк таблицы, а не в порядке щелчков.
        selected = set(tree.selection())
        return [int(iid) for iid in tree.get_children() if iid in selected]

    @staticmethod
    def _select_all(tree: ttk.Treeview) -> None:
        if tree.get_children():
            tree.selection_set(tree.get_children())

    def _bind_select_all(self, tree: ttk.Treeview) -> None:
        for sequence in ("<Control-a>", "<Control-A>", "<Control-Cyrillic_ef>",
                         "<Control-Cyrillic_EF>"):
            tree.bind(sequence, lambda _event: (self._select_all(tree), "break")[1])

    @staticmethod
    def _selected_tree_id(tree: ttk.Treeview) -> int | None:
        selection = tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def _run_background(
        self,
        label: str,
        operation: Callable[[], OperationResult],
        on_success: Callable[[OperationResult], None] | None = None,
        notify: bool = False,
    ) -> None:
        if self._background_busy:
            self.status_var.set("Дождитесь завершения текущей операции")
            return
        self._background_busy = True
        self.status_var.set(label)

        def worker() -> None:
            try:
                result = operation()
                self._task_queue.put(("success", result, on_success, notify))
            except Exception as exc:
                self._task_queue.put(("error", exc, None, notify))

        import threading

        threading.Thread(target=worker, daemon=True).start()

    def _poll_tasks(self) -> None:
        try:
            while True:
                kind, payload, callback, notify = self._task_queue.get_nowait()
                was_search = self._search_active
                if was_search:
                    self._search_active = False
                    self.search_start_button.configure(state="normal")
                    self.search_cancel_button.configure(state="disabled")
                if kind == "success":
                    result: OperationResult = payload
                    self.status_var.set(result.message)
                    if callback:
                        callback(result)
                    if notify:
                        messagebox.showinfo("Готово", result.message, parent=self)
                else:
                    if isinstance(payload, SearchCancelled):
                        self.status_var.set("Поиск остановлен")
                        self.search_status_var.set("Поиск остановлен")
                    else:
                        self.status_var.set(f"Ошибка: {payload}")
                        if was_search:
                            self.search_status_var.set(str(payload))
                    if notify and not isinstance(payload, SearchCancelled):
                        messagebox.showerror("Ошибка", str(payload), parent=self)
                self._background_busy = False
                self._next_auto_check = time.monotonic() + self._interval_seconds()
        except queue.Empty:
            pass
        finally:
            self.after(200, self._poll_tasks)

    def _minimize_window(self) -> None:
        self.iconify()
        self.status_var.set("Программа свёрнута и продолжает проверять почту")

    def _exit_app(self) -> None:
        if messagebox.askyesno(
            "Выход",
            "Полностью закрыть программу? Проверка продолжится при следующем запуске.",
            parent=self,
        ):
            if self._search_active:
                self._supplier_search_cancel.set()
                self.after(1500, self.destroy)
            else:
                self.destroy()
