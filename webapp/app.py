from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.exceptions import HTTPException

from automation.mail_gateway import MAIL_PROVIDERS
from automation.service import safe_name, sha256_file
from automation.supplier_search import clean_phones, export_candidates_xlsx, normalize_email, normalize_web_url
from .runtime import Runtime
from .store import Store
from .config import search_limit
from .purchases import FILTERS, filtered_purchases, purchase_rows

CAMPAIGN_LABELS = {"draft": "Черновик", "active": "Ждём ответы", "ready": "Можно сравнить", "closed": "Завершена"}
RECIPIENT_LABELS = {"pending": "Не отправлено", "sent": "Ожидаем ответ", "reply_without_files": "Ответ без файлов",
    "files_received": "Файлы получены", "declined": "Отказ", "closed_no_response": "Закрыт без ответа",
    "send_failed": "Ошибка отправки", "send_unknown": "Исход неизвестен"}
JOB_LABELS = {"search": "Поиск поставщиков", "send": "Отправка запроса", "mail": "Проверка почты",
    "test_mail": "Проверка подключения", "reply": "Ответ поставщику", "summary": "Сравнение предложений", "backup": "Резервная копия"}
STATE_LABELS = {"queued": "В очереди", "running": "Выполняется", "done": "Готово", "failed": "Требует внимания"}


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATA_ROOT=os.environ.get("WEB_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".dev-data" / "web")),
        ALLOW_REGISTRATION=os.environ.get("WEB_ALLOW_REGISTRATION") == "1",
        SESSION_COOKIE_SECURE=os.environ.get("WEB_SECURE_COOKIES") == "1",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="reply_session", PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=256 * 1024, MAX_FORM_PARTS=150,
        TRUSTED_HOSTS=os.environ.get("WEB_TRUSTED_HOSTS", "localhost,127.0.0.1,[::1]").split(","),
    )
    if config:
        app.config.update(config)
    store = Store(Path(app.config["DATA_ROOT"]))
    app.secret_key = store.signing_key
    runtime = Runtime(store)
    app.extensions["runtime"] = runtime

    @app.before_request
    def authenticate():
        g.user = store.user_for_session(session.get("token", ""))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                abort(400, description="Форма устарела. Обновите страницу и повторите действие.")
        if request.endpoint not in {"login", "register", "static", "health"} and g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login"))
        if g.user:
            g.service = runtime.service(g.user["id"])
            g.db = g.service.db

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.context_processor
    def template_context():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        unread_count, current_operation = 0, None
        if g.get("user") and g.get("db"):
            with g.db.connect() as connection:
                unread_count = connection.execute("SELECT COUNT(*) FROM incoming_messages WHERE opened_at IS NULL").fetchone()[0]
            operation_id = request.args.get("operation")
            if operation_id:
                row = next((row for row in store.jobs(g.user["id"]) if row["id"] == operation_id), None)
                if row:
                    current_operation = dict(row)
                    current_operation["result"] = json.loads(row["result"])
        return dict(csrf=session["csrf"], current_user=g.get("user"),
                    unread_count=unread_count, current_operation=current_operation,
                    campaign_labels=CAMPAIGN_LABELS, recipient_labels=RECIPIENT_LABELS,
                    job_labels=JOB_LABELS, state_labels=STATE_LABELS,
                    allow_registration=app.config["ALLOW_REGISTRATION"])

    @app.template_filter("stamp")
    def stamp(value):
        if not value:
            return "—"
        try:
            parsed = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            return "—"

    @app.template_filter("json_list")
    def json_list(value):
        try:
            return json.loads(value or "[]")
        except (TypeError, ValueError):
            return []

    def page(template, title, section, **values):
        return render_template(template, title=title, section=section, **values)

    def required(row):
        if row is None:
            abort(404)
        return row

    def enqueue(kind, payload):
        operation_id = store.enqueue(g.user["id"], kind, payload)
        if kind in {"send", "summary"}:
            target = url_for("campaign", campaign_id=payload["campaign_id"], operation=operation_id)
        elif kind == "reply":
            target = url_for("thread", recipient_id=payload["recipient_id"], operation=operation_id)
        else:
            target = url_for({"search": "search", "mail": "inbox", "test_mail": "settings", "backup": "settings"}[kind], operation=operation_id)
        return redirect(target, code=303)

    def uploaded(field="attachments"):
        files = []
        for upload in request.files.getlist(field):
            if not upload.filename:
                continue
            filename = safe_name(upload.filename.replace("\\", "/").rsplit("/", 1)[-1])
            if Path(filename).suffix.lower() not in {".pdf", ".xlsx", ".xls", ".doc", ".docx", ".txt", ".csv", ".png", ".jpg", ".jpeg"}:
                raise ValueError("Допустимы PDF, Excel, Word, текстовые файлы и изображения")
            folder = g.service.paths.root / "uploads" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            path = folder / filename
            upload.save(path)
            files.append((runtime.register_file(g.user["id"], path), path))
        return files

    def file_link(path, name=None):
        try:
            file_id = runtime.register_file(g.user["id"], Path(path), name)
            return {"name": name or Path(path).name, "url": url_for("download", file_id=file_id)}
        except ValueError:
            return {"name": name or Path(path).name, "url": None}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        error = None
        if request.method == "POST":
            email, password = request.form.get("email", "").strip().lower(), request.form.get("password", "")
            if len(email) > 254 or len(password) > 256:
                raise ValueError("Email или пароль слишком длинный")
            if store.limited("ip:" + (request.remote_addr or "unknown"), 40) or store.limited("email:" + email):
                return page("auth.html", "Вход", "auth", error="Слишком много попыток. Повторите через 15 минут."), 429
            token = store.login(email, password[:257])
            if token:
                session.clear()
                session.update(token=token, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect(url_for("dashboard"), code=303)
            error = "Неверный email или пароль"
        return page("auth.html", "Вход", "auth", error=error)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not app.config["ALLOW_REGISTRATION"]:
            abort(404)
        if request.method == "POST":
            if store.limited("register:" + (request.remote_addr or "unknown"), 5):
                abort(429)
            store.create_user(request.form.get("email", ""), request.form.get("password", ""))
            return redirect(url_for("login"), code=303)
        return page("auth.html", "Создать аккаунт", "auth", registering=True)

    @app.post("/logout")
    def logout():
        store.logout(session.get("token", ""))
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.get("/")
    def dashboard():
        return purchases_page()

    def purchases_page():
        status = request.args.get("status", "all")
        if status not in dict(FILTERS):
            status = "all"
        query = request.args.get("q", "").strip()[:200]
        rows, counts = filtered_purchases(g.db, query, status)
        return page("campaigns.html", "Закупки", "campaigns", campaigns=rows,
                    purchase_filters=FILTERS, filter_counts=counts, selected_filter=status, query=query)

    @app.get("/suppliers")
    def suppliers():
        query = request.args.get("q", "").strip().casefold()
        rows = g.db.list_suppliers()
        if query:
            rows = [row for row in rows if query in " ".join(str(row[key] or "") for key in
                    ("name", "email", "categories", "phones_json")).casefold()]
        edit = next((row for row in rows if str(row["id"]) == request.args.get("edit")), None)
        return page("suppliers.html", "Поставщики", "suppliers", suppliers=rows, edit=edit)

    @app.post("/suppliers/save")
    def save_supplier():
        form = request.form
        name = form.get("name", "").strip()
        if not name or len(name) > 200:
            raise ValueError("Укажите название компании до 200 символов")
        supplier_id = int(form["id"]) if form.get("id") else None
        if supplier_id:
            required(next((r for r in g.db.list_suppliers() if r["id"] == supplier_id), None))
        g.db.save_supplier(name, normalize_email(form.get("email", "")), form.get("notes", "")[:4000],
            supplier_id=supplier_id, categories=form.get("categories", "").split(","),
            phones=clean_phones(form.get("phones", "").splitlines()),
            website=normalize_web_url(form.get("website", "")), contact_person=form.get("contact_person", "")[:200],
            excluded=form.get("excluded") == "1", excluded_reason=form.get("excluded_reason", "")[:1000])
        return redirect(url_for("suppliers"), code=303)

    @app.post("/suppliers/<int:supplier_id>/delete")
    def delete_supplier(supplier_id):
        g.db.delete_supplier(supplier_id)
        return redirect(url_for("suppliers"), code=303)

    @app.get("/search")
    def search():
        return page("search.html", "Найти компании", "suppliers", candidates=g.db.list_candidates(),
                    search_limit=search_limit(),
                    searches=[row for row in store.jobs(g.user["id"]) if row["kind"] == "search"][:3])

    @app.post("/search")
    def start_search():
        query, region = request.form.get("query", "").strip(), request.form.get("region", "").strip()
        count = int(request.form.get("count", "10"))
        limit = search_limit()
        if not 5 <= len(query) <= 600 or len(region) > 150 or not 1 <= count <= limit:
            raise ValueError(f"Запрос: 5–600 символов, регион: до 150, количество: 1–{limit} компаний")
        return enqueue("search", dict(query=query, region=region, count=count))

    @app.post("/candidates/<int:candidate_id>/<action>")
    def candidate_action(candidate_id, action):
        required(g.db.get_candidate(candidate_id))
        if action == "approve":
            g.db.approve_candidate(candidate_id)
        elif action == "reject":
            g.db.set_candidate_status(candidate_id, "rejected")
        else:
            abort(404)
        return redirect(url_for("search"), code=303)

    @app.get("/search/export")
    def export_search():
        target = g.service.paths.root / "exports" / f"suppliers-{uuid.uuid4().hex}.xlsx"
        target.parent.mkdir(exist_ok=True)
        export_candidates_xlsx(g.db.list_candidates(), target)
        return send_file(target, as_attachment=True, download_name="suppliers.xlsx")

    @app.get("/campaigns")
    def campaigns():
        return purchases_page()

    @app.route("/campaigns/new", methods=["GET", "POST"])
    def new_campaign():
        return purchase_form()

    @app.route("/campaigns/<int:campaign_id>/edit", methods=["GET", "POST"])
    def edit_campaign(campaign_id):
        return purchase_form(campaign_id)

    def purchase_form(campaign_id=None):
        draft = required(g.db.get_campaign(campaign_id)) if campaign_id else None
        if draft:
            with store.connect() as connection:
                sending = connection.execute("SELECT 1 FROM jobs WHERE user_id=? AND kind='send' "
                    "AND state IN ('queued','running') AND json_extract(payload,'$.campaign_id')=?",
                    (g.user["id"], campaign_id)).fetchone()
            if draft["status"] != "draft" or sending:
                raise ValueError("Изменить можно только черновик до отправки запроса")
        if request.method == "POST":
            name, subject, body = (request.form.get(k, "").strip() for k in ("name", "subject", "body"))
            if not name or not subject or not body:
                raise ValueError("Заполните название, тему и текст письма")
            if len(name) > 200 or len(subject) > 500 or len(body) > 50000:
                raise ValueError("Слишком длинное название, тема или текст")
            deadline = g.service._validate_deadline(request.form.get("deadline") or None)
            attachments = uploaded()
            requests = uploaded("request")
            if requests and requests[0][1].suffix.lower() != ".xlsx":
                raise ValueError("Исходная заявка должна быть в формате XLSX")
            supplier_ids = list(dict.fromkeys(int(value) for value in request.form.getlist("suppliers")))
            if not draft:
                campaign_id = g.db.create_campaign(name, subject, body, deadline, supplier_ids)
            else:
                allowed = {row["id"]: row for row in g.db.list_suppliers() if row["email"] and not row["excluded"]}
                if not supplier_ids or any(value not in allowed for value in supplier_ids):
                    raise ValueError("Выберите поставщиков с email из своей базы")
                with g.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
                    history = connection.execute("SELECT 1 FROM outgoing_messages WHERE campaign_id=? "
                        "UNION ALL SELECT 1 FROM incoming_messages WHERE campaign_id=? LIMIT 1", (campaign_id, campaign_id)).fetchone()
                    if current["status"] != "draft" or history:
                        raise ValueError("В закупке уже есть переписка. Изменение черновика недоступно.")
                    connection.execute("UPDATE campaigns SET name=?,subject=?,body=?,deadline=? WHERE id=?",
                                       (name, subject, body, deadline, campaign_id))
                    connection.execute("DELETE FROM campaign_recipients WHERE campaign_id=?", (campaign_id,))
                    connection.executemany("INSERT INTO campaign_recipients(campaign_id,supplier_id,email) VALUES(?,?,?)",
                                           [(campaign_id, value, allowed[value]["email"]) for value in supplier_ids])
            campaign = g.db.get_campaign(campaign_id)
            destination = g.service.paths.campaigns / campaign["code"] / "outgoing" / "attachments"
            destination.mkdir(parents=True, exist_ok=True)
            for _, path in attachments + requests:
                target = destination / (uuid.uuid4().hex[:8] + "-" + path.name)
                shutil.copy2(path, target)
                g.db.add_campaign_attachment(campaign_id, path.name, str(target), target.stat().st_size, sha256_file(target))
                if requests and path == requests[0][1]:
                    g.db.set_campaign_request_path(campaign_id, str(target))
            return redirect(url_for("campaign", campaign_id=campaign_id), code=303)
        selected_suppliers = {row["supplier_id"] for row in g.db.list_recipients(campaign_id)} if draft else set()
        return page("compose.html", "Черновик закупки" if draft else "Новая закупка", "campaigns",
                    suppliers=g.db.list_suppliers(), draft=draft, selected_suppliers=selected_suppliers,
                    existing_files=g.db.list_outgoing_attachments(campaign_id) if draft else [])

    @app.get("/campaigns/<int:campaign_id>")
    def campaign(campaign_id):
        row = required(g.db.get_campaign(campaign_id))
        summary = next(item for item in purchase_rows(g.db) if item["id"] == campaign_id)
        completed_files = []
        for job in store.jobs(g.user["id"]):
            if job["kind"] == "summary" and job["state"] == "done":
                result = json.loads(job["result"])
                if result.get("campaign_id") == campaign_id and result.get("file_id"):
                    completed_files.append({"name": "Скачать сравнение в Excel", "url": url_for("download", file_id=result["file_id"])})
        return page("campaign.html", row["name"], "campaigns", campaign=row,
            purchase_summary=summary, completed_files=completed_files,
            recipients=g.db.list_recipients(campaign_id), incoming=g.db.list_incoming(campaign_id=campaign_id),
            attachments=[file_link(r["path"], r["filename"]) for r in g.db.list_outgoing_attachments(campaign_id)],
            suppliers=g.db.list_suppliers())

    @app.post("/campaigns/<int:campaign_id>/<action>")
    def campaign_action(campaign_id, action):
        row = required(g.db.get_campaign(campaign_id))
        if action in {"send", "summary"}:
            if action == "send":
                if row["status"] == "closed":
                    raise ValueError("Рассылка завершена")
                g.service.mail_settings()
            else:
                if not g.db.list_selected_offer_attachments(campaign_id):
                    raise ValueError("Пока нет предложений с файлами для сравнения")
            return enqueue(action, {"campaign_id": campaign_id})
        if action == "close":
            g.db.set_campaign_closed(campaign_id)
        elif action == "add-recipients":
            g.db.add_campaign_recipients(campaign_id, request.form.getlist("suppliers"))
        else:
            abort(404)
        return redirect(url_for("campaign", campaign_id=campaign_id), code=303)

    @app.post("/recipients/<int:recipient_id>/status")
    def recipient_status(recipient_id):
        row = required(g.db.get_recipient(recipient_id))
        status = request.form.get("status")
        if status not in {"declined", "closed_no_response"}:
            raise ValueError("Недопустимый статус")
        g.db.set_recipient_status(recipient_id, status)
        return redirect(url_for("campaign", campaign_id=row["campaign_id"]), code=303)

    @app.get("/recipients/<int:recipient_id>")
    def thread(recipient_id):
        recipient = required(g.db.get_recipient(recipient_id))
        letters = []
        for row in g.db.list_recipient_correspondence(recipient_id):
            item = dict(row)
            if row["direction"] == "in":
                attachments = g.db.list_incoming_attachments(row["id"])
                item["files"] = [file_link(r["path"], r["filename"]) for r in attachments]
            else:
                outgoing = g.db.get_outgoing(row["id"])
                if outgoing["kind"] == "campaign":
                    item["files"] = [file_link(r["path"], r["filename"]) for r in g.db.list_outgoing_attachments(recipient["campaign_id"])]
                else:
                    item["files"] = [file_link(p) for p in json.loads(outgoing["attachments_json"])]
            letters.append(item)
        can_reply = any(letter["direction"] == "in" or letter["delivery_status"] != "failed" for letter in letters)
        draft = g.service.prepare_reply(recipient_id=recipient_id) if can_reply else None
        return page("thread.html", recipient["supplier_name"], "campaigns", recipient=recipient,
                    letters=letters, draft=draft)

    @app.post("/recipients/<int:recipient_id>/reply")
    def reply(recipient_id):
        required(g.db.get_recipient(recipient_id))
        g.service.mail_settings()
        body, subject = request.form.get("body", "").strip(), request.form.get("subject", "").strip()
        if not body or len(body) > 50000 or len(subject) > 500:
            raise ValueError("Напишите ответ до 50 000 символов; тема — до 500")
        incoming_id = int(request.form["incoming_id"]) if request.form.get("incoming_id") else None
        if incoming_id:
            incoming = required(g.db.get_incoming(incoming_id))
            if incoming["recipient_id"] != recipient_id:
                abort(404)
        files = uploaded()
        return enqueue("reply", dict(recipient_id=recipient_id, incoming_id=incoming_id,
                                     body=body, subject=subject, files=[f[0] for f in files]))

    @app.get("/inbox")
    def inbox():
        rows = g.db.list_incoming()
        selected = request.args.get("filter", "all")
        if selected == "unread":
            rows = [row for row in rows if not row["opened_at"]]
        elif selected == "review":
            rows = [row for row in rows if row["needs_review"] or not row["recipient_id"]]
        else:
            selected = "all"
        return page("inbox.html", "Письма", "inbox", incoming=rows, mail_filter=selected)

    @app.get("/inbox/<int:incoming_id>")
    def incoming_message(incoming_id):
        row = required(g.db.get_incoming(incoming_id))
        recipients = [(campaign, g.db.list_recipients(campaign["id"])) for campaign in g.db.list_campaigns()]
        return page("message.html", row["subject"] or "Без темы", "inbox", message=row, recipients=recipients,
                    attachments=[file_link(r["path"], r["filename"]) for r in g.db.list_incoming_attachments(incoming_id)])

    @app.post("/inbox/<int:incoming_id>/<action>")
    def incoming_action(incoming_id, action):
        required(g.db.get_incoming(incoming_id))
        if action == "assign":
            recipient_id = int(request.form.get("recipient_id", "0"))
            required(g.db.get_recipient(recipient_id))
            g.service.assign_incoming(incoming_id, recipient_id)
        elif action == "read":
            g.db.mark_incoming_opened(incoming_id)
        elif action == "unread":
            g.db.mark_incoming_unread([incoming_id])
        else:
            abort(404)
        return redirect(url_for("incoming_message", incoming_id=incoming_id), code=303)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            g.service.save_mail_preferences(request.form.get("email", ""), int(request.form.get("interval", "10")),
                request.form.get("password", ""), request.form.get("provider", "mailru"))
            return redirect(url_for("settings", saved="1"), code=303)
        return page("settings.html", "Настройки почты", "settings", preferences=g.service.get_mail_preferences(),
                    providers=MAIL_PROVIDERS, saved=request.args.get("saved") == "1")

    @app.post("/settings/password/delete")
    def delete_mail_password():
        g.service.delete_saved_password()
        return redirect(url_for("settings"), code=303)

    @app.post("/operations/<kind>")
    def operation(kind):
        if kind not in {"mail", "test_mail", "backup"}:
            abort(404)
        if kind != "backup":
            g.service.mail_settings()
        return enqueue(kind, {})

    @app.get("/jobs")
    def jobs():
        rows = []
        for row in store.jobs(g.user["id"]):
            item = dict(row)
            item["result"] = json.loads(item["result"])
            rows.append(item)
        return page("jobs.html", "Задачи", "jobs", jobs=rows)

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify([dict(r) for r in store.jobs(g.user["id"])])

    @app.get("/files/<file_id>")
    def download(file_id):
        try:
            path, name = runtime.file(g.user["id"], file_id)
        except ValueError:
            abort(404)
        return send_file(path, as_attachment=True, download_name=name)

    @app.get("/logs")
    def logs():
        return page("logs.html", "Журнал действий", "settings", logs=g.db.list_logs(200))

    @app.errorhandler(ValueError)
    def validation_error(exc):
        return page("error.html", "Проверьте данные", "error", message=str(exc)), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict_error(exc):
        return page("error.html", "Действие недоступно", "error",
                    message="Такая запись уже существует или связана с другими данными."), 409

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {400: "Некорректный запрос или устаревшая форма. Обновите страницу.", 401: "Войдите в аккаунт.",
                    404: "Страница или запись не найдена.", 413: "Общий размер загрузки превышает 25 МБ.",
                    429: "Слишком много запросов. Повторите позже."}
        return page("error.html", f"Ошибка {exc.code}", "error", message=messages.get(exc.code, "Действие недоступно.")), exc.code

    @app.errorhandler(500)
    def server_error(exc):
        logging.getLogger(__name__).error("Web request failed: %s", request.path)
        return page("error.html", "Ошибка сервера", "error", message="Не удалось выполнить действие. Обратитесь к администратору."), 500

    return app
