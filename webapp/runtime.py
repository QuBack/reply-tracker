from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from decimal import Decimal
from urllib.request import Request, urlopen
from pathlib import Path

from automation.database import Database
from automation.paths import AppPaths
from automation.service import AppService
from automation.supplier_search import parse_codex_result, run_codex_search
from automation.openai_search import (OpenAISearchError, RESERVATION_CENTS, request_search,
                                      response_usage, parse_search_response)
from .config import search_provider
from .store import EncryptedCredentials, Store

LOG = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Runtime:
    def __init__(self, store: Store):
        self.store = store
        self._services: dict[str, AppService] = {}
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def service(self, user_id: str) -> AppService:
        # The only caller-supplied identity accepted here is a server-side account ID.
        if len(user_id) != 32 or any(c not in "0123456789abcdef" for c in user_id):
            raise ValueError("Некорректный аккаунт")
        with self._lock:
            if user_id not in self._services:
                with self.store.connect() as db:
                    if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                        raise ValueError("Аккаунт не найден")
                paths = AppPaths.from_root(self.store.root / "accounts" / user_id)
                paths.ensure()
                self._services[user_id] = AppService(paths, Database(paths.database),
                                                     EncryptedCredentials(self.store, user_id))
            return self._services[user_id]

    def register_file(self, user_id: str, path: Path, name: str | None = None) -> str:
        root = self.service(user_id).paths.root
        path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Файл недоступен")
        file_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO files(id,user_id,path,name) VALUES(?,?,?,?) "
                       "ON CONFLICT(user_id,path) DO NOTHING", (file_id, user_id, str(path), name or path.name))
            return db.execute("SELECT id FROM files WHERE user_id=? AND path=?", (user_id, str(path))).fetchone()[0]

    def file(self, user_id: str, file_id: str):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM files WHERE user_id=? AND id=?", (user_id, file_id)).fetchone()
        if row is None:
            raise ValueError("Файл не найден")
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(self.service(user_id).paths.root) or not path.is_file():
            raise ValueError("Файл недоступен")
        return path, row["name"]

    def run_one(self, search: bool = False) -> bool:
        job = self.store.claim(search)
        if job is None:
            return False
        try:
            user_id, kind = job["user_id"], job["kind"]
            payload = json.loads(job["payload"])
            service = self.service(user_id)
            if kind == "search":
                gateway = os.environ.get("CODEX_SEARCH_URL", "")
                details = {}
                if search_provider() == "openai":
                    limit = int(Decimal(os.environ.get("OPENAI_SEARCH_BUDGET_USD", "5")) * 100)
                    if not self.store.reserve_search(job["id"], user_id, limit, RESERVATION_CENTS):
                        raise OpenAISearchError("Достигнут общий лимит расходов на поиск. Обратитесь к администратору.")
                    response = request_search(payload["query"], payload["region"], service.db.list_categories(), payload["count"])
                    usage = response_usage(response)
                    if usage:
                        self.store.settle_search(job["id"], usage)
                        details = {"model": "gpt-6-luna", "estimated_cost_usd": f"{usage.estimated_cents / 100:.2f}",
                                   "web_search_calls": usage.searches}
                    result = parse_search_response(response, payload["count"])
                elif gateway:
                    body = json.dumps({**payload, "categories": service.db.list_categories()[:100]}).encode()
                    request = Request(gateway, data=body, headers={"Content-Type": "application/json",
                        "Authorization": "Bearer " + os.environ.get("CODEX_GATEWAY_TOKEN", "")})
                    with urlopen(request, timeout=1250) as response:
                        output = response.read(2 * 1024 * 1024 + 1)
                    if len(output) > 2 * 1024 * 1024:
                        raise ValueError("Search response too large")
                    result = parse_codex_result(output.decode())
                elif os.environ.get("WEB_LOCAL_CODEX") == "1":
                    # Explicit development-only fallback. Production uses the isolated gateway.
                    result = run_codex_search(payload["query"], payload["region"], service.db.list_categories(),
                                              project_root=PROJECT_ROOT,
                                              data_root=service.paths.root / "search-work",
                                              maximum_companies=payload["count"], cancel_event=self.stop)
                else:
                    raise RuntimeError("Configure CODEX_SEARCH_URL and CODEX_GATEWAY_TOKEN")
                added, updated = service.db.import_candidates(payload["query"], payload["region"], result.candidates)
                message = f"Поиск завершён. Добавлено: {added}, обновлено: {updated}."
                if details:
                    message += f" Оценка расходов: ${details['estimated_cost_usd']}."
            else:
                if kind == "send":
                    campaign = service.db.get_campaign(payload["campaign_id"])
                    if campaign is None or campaign["status"] == "closed":
                        raise ValueError("Рассылка завершена или удалена")
                    result = service.send_campaign(payload["campaign_id"])
                elif kind == "mail":
                    result = service.check_mail("web")
                elif kind == "test_mail":
                    result = service.test_mail_connection()
                elif kind == "reply":
                    draft = service.prepare_reply(recipient_id=payload["recipient_id"],
                                                  incoming_id=payload.get("incoming_id"))
                    result = service.send_reply(draft, body=payload["body"], subject=payload["subject"],
                        attachment_paths=[str(self.file(user_id, value)[0]) for value in payload["files"]])
                elif kind == "summary":
                    result = service.create_summary(payload["campaign_id"], allow_partial=True)
                elif kind == "backup":
                    result = service.create_backup()
                else:
                    raise ValueError("Неизвестная операция")
                message, details = result.message, dict(result.details)
                if kind == "summary":
                    details["campaign_id"] = payload["campaign_id"]
                if "path" in details:
                    details["file_id"] = self.register_file(user_id, Path(str(details.pop("path"))))
                    # Never expose internal paths in job messages.
                    message = "Файл готов к скачиванию."
            self.store.finish(job["id"], "done", message, details)
        except OpenAISearchError as exc:
            # These deliberately sanitized messages explain billing/auth failures.
            self.store.finish(job["id"], "failed", str(exc))
        except Exception:
            # Exceptions may include SMTP credentials, subprocess stderr, or file paths.
            # Keep the browser response generic; diagnose using the server-side log.
            LOG.exception("Job %s (%s) failed", job["id"], job["kind"])
            self.store.finish(job["id"], "failed",
                              "Операция не завершена. Проверьте настройки и данные. "
                              "Для диагностики сообщите администратору номер задачи.")
        return True

    def schedule_mail(self):
        now = time.time()
        with self.store.connect() as db:
            users = db.execute("SELECT id FROM users WHERE next_mail_check<=?", (now,)).fetchall()
        for user in users:
            if not (self.store.root / "accounts" / user["id"] / "app.db").is_file():
                continue  # Leave unused accounts unprovisioned, including before offline import.
            service = self.service(user["id"])
            preferences = service.get_mail_preferences()
            try:
                minutes = max(5, int(preferences["check_interval_minutes"]))
            except ValueError:
                minutes = 10
            if preferences["has_password"] == "1" and service.db.active_campaign_count():
                try:
                    self.store.enqueue(user["id"], "mail", {})
                except ValueError:
                    pass  # A pending mail job already covers this interval.
            with self.store.connect() as db:
                db.execute("UPDATE users SET next_mail_check=? WHERE id=?", (now + minutes * 60, user["id"]))

    def start(self):
        if self.threads:
            return
        # Interrupted sends must never be replayed automatically.
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET state='failed',message=?,updated=? WHERE state='running'",
                       ("Сервер перезапущен. Проверьте результат операции перед повтором.", time.time()))
        def work(search):
            while not self.stop.is_set():
                try:
                    if not self.run_one(search):
                        self.stop.wait(1)
                except Exception:
                    LOG.exception("Queue worker failed")
                    self.stop.wait(3)
        def schedule():
            while not self.stop.is_set():
                try:
                    self.schedule_mail()
                except Exception:
                    LOG.exception("Mail scheduler failed")
                self.stop.wait(30)
        for target, args, name in [(work, (True,), "search"), (work, (False,), "mail"),
                                    (schedule, (), "scheduler")]:
            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=6)
