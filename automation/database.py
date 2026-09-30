from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


TERMINAL_RECIPIENT_STATUSES = ("files_received", "declined", "closed_no_response")
WAITING_RECIPIENT_STATUSES = ("pending", "sent", "reply_without_files", "send_failed", "send_unknown")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    excluded INTEGER NOT NULL DEFAULT 0,
    excluded_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_categories (
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    category TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (supplier_id, category)
);

CREATE TABLE IF NOT EXISTS supplier_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER REFERENCES supplier_searches(id) ON DELETE SET NULL,
    identity_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    website TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    categories_json TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    contact_source_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_checked_at TEXT,
    request_path TEXT
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    email TEXT NOT NULL COLLATE NOCASE,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    last_response_at TEXT,
    completed_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(campaign_id, email)
);

CREATE TABLE IF NOT EXISTS campaign_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, sha256)
);

CREATE TABLE IF NOT EXISTS outgoing_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES campaign_recipients(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
    subject TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    raw_eml_path TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'sent'
);

CREATE TABLE IF NOT EXISTS incoming_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox TEXT NOT NULL COLLATE NOCASE,
    folder TEXT NOT NULL,
    uid_validity TEXT NOT NULL,
    imap_uid INTEGER NOT NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    recipient_id INTEGER REFERENCES campaign_recipients(id) ON DELETE SET NULL,
    message_id TEXT COLLATE NOCASE,
    in_reply_to TEXT,
    sender_email TEXT NOT NULL COLLATE NOCASE,
    subject TEXT NOT NULL,
    received_at TEXT,
    body_text TEXT NOT NULL DEFAULT '',
    raw_eml_path TEXT NOT NULL,
    match_method TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(mailbox, folder, uid_validity, imap_uid)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incoming_message_id INTEGER NOT NULL REFERENCES incoming_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    is_allowed INTEGER NOT NULL DEFAULT 0,
    is_selected INTEGER NOT NULL DEFAULT 1,
    duplicate_of_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(incoming_message_id, sha256, filename)
);

CREATE TABLE IF NOT EXISTS mail_sync_state (
    mailbox TEXT PRIMARY KEY COLLATE NOCASE,
    folder TEXT NOT NULL,
    uid_validity TEXT,
    last_uid INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS mail_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    new_messages INTEGER NOT NULL DEFAULT 0,
    new_attachments INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    model TEXT,
    prompt TEXT,
    status TEXT NOT NULL,
    result_json TEXT,
    output_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    details TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON campaign_recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_recipients_email ON campaign_recipients(email);
CREATE INDEX IF NOT EXISTS idx_incoming_campaign ON incoming_messages(campaign_id);
CREATE INDEX IF NOT EXISTS idx_incoming_message_id ON incoming_messages(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(incoming_message_id);
CREATE INDEX IF NOT EXISTS idx_outgoing_message_id ON outgoing_messages(message_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_supplier_candidates_status ON supplier_candidates(status);
CREATE INDEX IF NOT EXISTS idx_supplier_candidates_search ON supplier_candidates(search_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            campaign_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(campaigns)")
            }
            if "request_path" not in campaign_columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN request_path TEXT")
            supplier_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(suppliers)")
            }
            if "excluded" not in supplier_columns:
                connection.execute(
                    "ALTER TABLE suppliers ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"
                )
            if "excluded_reason" not in supplier_columns:
                connection.execute(
                    "ALTER TABLE suppliers ADD COLUMN excluded_reason TEXT NOT NULL DEFAULT ''"
                )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(incoming_messages)")
            }
            if "uid_validity" not in columns:
                self._migrate_incoming_messages(connection)
            outgoing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(outgoing_messages)")
            }
            if "delivery_status" not in outgoing_columns:
                connection.execute(
                    "ALTER TABLE outgoing_messages "
                    "ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'sent'"
                )
            connection.execute("PRAGMA user_version = 3")

    def _migrate_incoming_messages(self, connection: sqlite3.Connection) -> None:
        snapshot = self.path.with_name(
            f"{self.path.stem}.pre-v2-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
        )
        self.backup_to(snapshot)
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE incoming_messages_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mailbox TEXT NOT NULL COLLATE NOCASE,
                    folder TEXT NOT NULL,
                    uid_validity TEXT NOT NULL,
                    imap_uid INTEGER NOT NULL,
                    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
                    recipient_id INTEGER REFERENCES campaign_recipients(id) ON DELETE SET NULL,
                    message_id TEXT COLLATE NOCASE,
                    in_reply_to TEXT,
                    sender_email TEXT NOT NULL COLLATE NOCASE,
                    subject TEXT NOT NULL,
                    received_at TEXT,
                    body_text TEXT NOT NULL DEFAULT '',
                    raw_eml_path TEXT NOT NULL,
                    match_method TEXT NOT NULL,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    UNIQUE(mailbox, folder, uid_validity, imap_uid)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO incoming_messages_v2 (
                    id, mailbox, folder, uid_validity, imap_uid, campaign_id,
                    recipient_id, message_id, in_reply_to, sender_email, subject,
                    received_at, body_text, raw_eml_path, match_method,
                    needs_review, created_at, processed_at
                )
                SELECT id, mailbox, folder, 'legacy', imap_uid, campaign_id,
                       recipient_id, message_id, in_reply_to, sender_email, subject,
                       received_at, body_text, raw_eml_path, match_method,
                       needs_review, created_at, NULL
                FROM incoming_messages
                """
            )
            connection.execute("DROP TABLE incoming_messages")
            connection.execute("ALTER TABLE incoming_messages_v2 RENAME TO incoming_messages")
            connection.execute(
                "CREATE INDEX idx_incoming_campaign ON incoming_messages(campaign_id)"
            )
            connection.execute(
                "CREATE INDEX idx_incoming_message_id ON incoming_messages(message_id)"
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.DatabaseError("После миграции нарушены связи в базе данных")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_settings(self, values: dict[str, str]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                values.items(),
            )

    def list_suppliers(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(
                "SELECT s.*, COALESCE((SELECT GROUP_CONCAT(category, ', ') "
                "FROM supplier_categories WHERE supplier_id = s.id), '') AS categories "
                "FROM suppliers s ORDER BY s.name COLLATE NOCASE"
            ))

    def list_categories(self) -> list[str]:
        with self.connect() as connection:
            return [row["category"] for row in connection.execute(
                "SELECT DISTINCT category FROM supplier_categories ORDER BY category COLLATE NOCASE"
            )]

    def save_supplier(self, name: str, email: str, notes: str = "",
                      supplier_id: int | None = None,
                      categories: Iterable[str] | None = None,
                      excluded: bool | None = None, excluded_reason: str = "") -> int:
        now = utc_now()
        email = email.strip().lower()
        reason = excluded_reason.strip() if excluded else ""
        with self.connect() as connection:
            if supplier_id is None:
                cursor = connection.execute(
                    "INSERT INTO suppliers(name, email, notes, excluded, excluded_reason, "
                    "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (name.strip(), email, notes.strip(), int(bool(excluded)), reason, now, now),
                )
                result = int(cursor.lastrowid)
            else:
                connection.execute(
                    "UPDATE suppliers SET name = ?, email = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (name.strip(), email, notes.strip(), now, supplier_id),
                )
                if excluded is not None:
                    connection.execute(
                        "UPDATE suppliers SET excluded = ?, excluded_reason = ? WHERE id = ?",
                        (int(excluded), reason, supplier_id),
                    )
                result = supplier_id
            if categories is not None:
                self._replace_supplier_categories(connection, result, categories)
        self.log("info", "supplier_saved", None, {
            "supplier_id": result, "email": email, "excluded": excluded,
        })
        return result

    @staticmethod
    def _replace_supplier_categories(connection: sqlite3.Connection, supplier_id: int,
                                     categories: Iterable[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in categories:
            category = value.strip()
            if category and category.casefold() not in seen:
                cleaned.append(category)
                seen.add(category.casefold())
        connection.execute("DELETE FROM supplier_categories WHERE supplier_id = ?", (supplier_id,))
        connection.executemany(
            "INSERT INTO supplier_categories(supplier_id, category) VALUES(?, ?)",
            ((supplier_id, value) for value in cleaned),
        )

    def list_candidates(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(
                "SELECT c.*, s.query AS search_query FROM supplier_candidates c "
                "LEFT JOIN supplier_searches s ON s.id = c.search_id "
                "ORDER BY c.updated_at DESC, c.id DESC"
            ))

    def get_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM supplier_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()

    def import_candidates(self, query: str, region: str, candidates: Iterable[Any]) -> tuple[int, int]:
        from .supplier_search import candidate_identity

        items = list(candidates)
        now = utc_now()
        added = 0
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO supplier_searches(query, region, created_at, candidate_count) "
                "VALUES(?, ?, ?, ?)", (query, region, now, len(items)),
            )
            search_id = int(cursor.lastrowid)
            for item in items:
                identity = candidate_identity(item.name, item.website, item.email, item.region)
                existing = connection.execute(
                    "SELECT * FROM supplier_candidates WHERE identity_key = ?", (identity,)
                ).fetchone()
                if existing:
                    old_categories = json.loads(existing["categories_json"])
                    old_sources = json.loads(existing["source_urls_json"])
                    categories = (old_categories if existing["status"] == "approved" else
                                  list(dict.fromkeys(old_categories + item.categories)))
                    sources = list(dict.fromkeys(old_sources + item.source_urls))
                    connection.execute(
                        "UPDATE supplier_candidates SET search_id = ?, website = ?, email = ?, "
                        "region = ?, categories_json = ?, evidence = ?, source_urls_json = ?, "
                        "contact_source_url = ?, updated_at = ? WHERE id = ?",
                        (search_id, existing["website"] or item.website,
                         existing["email"] or item.email, existing["region"] or item.region,
                         json.dumps(categories, ensure_ascii=False),
                         existing["evidence"] or item.evidence,
                         json.dumps(sources, ensure_ascii=False),
                         existing["contact_source_url"] or item.contact_source_url,
                         now, existing["id"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO supplier_candidates "
                        "(search_id, identity_key, name, website, email, region, "
                        "categories_json, evidence, source_urls_json, contact_source_url, "
                        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (search_id, identity, item.name, item.website, item.email, item.region,
                         json.dumps(item.categories, ensure_ascii=False), item.evidence,
                         json.dumps(item.source_urls, ensure_ascii=False), item.contact_source_url,
                         now, now),
                    )
                    added += 1
        self.log("info", "supplier_search", None, {
            "query": query, "found": len(items), "new": added,
        })
        return added, len(items) - added

    def update_candidate(self, candidate_id: int, *, name: str, website: str, email: str,
                         region: str, categories: Iterable[str], evidence: str,
                         source_urls: Iterable[str], contact_source_url: str) -> None:
        from .supplier_search import candidate_identity

        identity = candidate_identity(name, website, email, region)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM supplier_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if existing is None:
                raise ValueError("Кандидат не найден")
            if existing["status"] == "approved":
                raise ValueError("Поставщик уже добавлен. Измените его в справочнике.")
            connection.execute(
                "UPDATE supplier_candidates SET identity_key = ?, name = ?, website = ?, "
                "email = ?, region = ?, categories_json = ?, evidence = ?, "
                "source_urls_json = ?, contact_source_url = ?, updated_at = ? WHERE id = ?",
                (identity, name.strip(), website.strip(), email.strip().lower(), region.strip(),
                 json.dumps(list(categories), ensure_ascii=False), evidence.strip(),
                 json.dumps(list(source_urls), ensure_ascii=False), contact_source_url.strip(),
                 utc_now(), candidate_id),
            )

    def set_candidate_status(self, candidate_id: int, status: str) -> None:
        if status not in ("new", "rejected"):
            raise ValueError("Недопустимый статус кандидата")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM supplier_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if existing is None:
                raise ValueError("Кандидат не найден")
            if existing["status"] == "approved":
                raise ValueError("Поставщик уже добавлен в справочник")
            connection.execute(
                "UPDATE supplier_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), candidate_id),
            )

    def delete_candidates(self, candidate_ids: Iterable[int]) -> int:
        ids = [int(value) for value in candidate_ids]
        with self.connect() as connection:
            deleted = connection.executemany(
                "DELETE FROM supplier_candidates WHERE id = ?", ((value,) for value in ids)
            ).rowcount
        self.log("info", "supplier_candidates_deleted", None, {"ids": ids})
        return max(deleted, 0)

    def add_candidates_category(self, candidate_ids: Iterable[int], category: str) -> int:
        category = category.strip()[:100]
        if not category:
            raise ValueError("Введите категорию")
        changed = 0
        with self.connect() as connection:
            for candidate_id in candidate_ids:
                row = connection.execute(
                    "SELECT status, categories_json FROM supplier_candidates WHERE id = ?",
                    (int(candidate_id),),
                ).fetchone()
                if row is None or row["status"] == "approved":
                    continue
                categories = json.loads(row["categories_json"])
                if any(value.casefold() == category.casefold() for value in categories):
                    continue
                connection.execute(
                    "UPDATE supplier_candidates SET categories_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(categories + [category], ensure_ascii=False), utc_now(),
                     int(candidate_id)),
                )
                changed += 1
        return changed

    def set_suppliers_excluded(self, supplier_ids: Iterable[int], excluded: bool,
                               reason: str = "") -> int:
        ids = [int(value) for value in supplier_ids]
        reason = reason.strip() if excluded else ""
        now = utc_now()
        with self.connect() as connection:
            changed = connection.executemany(
                "UPDATE suppliers SET excluded = ?, excluded_reason = ?, updated_at = ? WHERE id = ?",
                ((int(excluded), reason, now, value) for value in ids),
            ).rowcount
        self.log("info", "suppliers_exclusion_changed", None, {
            "ids": ids, "excluded": excluded,
        })
        return max(changed, 0)

    def change_suppliers_category(self, supplier_ids: Iterable[int], category: str, *,
                                  remove: bool = False) -> int:
        category = category.strip()[:100]
        if not category:
            raise ValueError("Введите категорию")
        # COLLATE NOCASE в SQLite не сворачивает кириллицу, поэтому сравниваем через casefold.
        key = category.casefold()
        changed = 0
        with self.connect() as connection:
            # Используем уже принятое в справочнике написание, чтобы не плодить дубли в фильтре.
            category = next((row["category"] for row in connection.execute(
                "SELECT DISTINCT category FROM supplier_categories"
            ) if row["category"].casefold() == key), category)
            for supplier_id in supplier_ids:
                existing = [row["category"] for row in connection.execute(
                    "SELECT category FROM supplier_categories WHERE supplier_id = ?",
                    (int(supplier_id),),
                )]
                matches = [value for value in existing if value.casefold() == key]
                if remove and matches:
                    connection.executemany(
                        "DELETE FROM supplier_categories WHERE supplier_id = ? AND category = ?",
                        ((int(supplier_id), value) for value in matches),
                    )
                    changed += 1
                elif not remove and not matches:
                    connection.execute(
                        "INSERT INTO supplier_categories(supplier_id, category) VALUES(?, ?)",
                        (int(supplier_id), category),
                    )
                    changed += 1
        return changed

    def approve_candidate(self, candidate_id: int) -> int:
        with self.connect() as connection:
            candidate = connection.execute(
                "SELECT * FROM supplier_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise ValueError("Кандидат не найден")
            if not candidate["email"]:
                raise ValueError("Перед добавлением поставщика нужен подтверждённый email")
            if candidate["status"] == "approved" and candidate["supplier_id"]:
                return int(candidate["supplier_id"])
            supplier = connection.execute(
                "SELECT id FROM suppliers WHERE email = ? COLLATE NOCASE",
                (candidate["email"],),
            ).fetchone()
            now = utc_now()
            if supplier:
                supplier_id = int(supplier["id"])
                existing_categories = [row["category"] for row in connection.execute(
                    "SELECT category FROM supplier_categories WHERE supplier_id = ?", (supplier_id,)
                )]
            else:
                cursor = connection.execute(
                    "INSERT INTO suppliers(name, email, notes, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (candidate["name"], candidate["email"],
                     f"Найден через поиск. Источники: {', '.join(json.loads(candidate['source_urls_json']))}",
                     now, now),
                )
                supplier_id = int(cursor.lastrowid)
                existing_categories = []
            categories = json.loads(candidate["categories_json"])
            self._replace_supplier_categories(
                connection, supplier_id, existing_categories + categories
            )
            connection.execute(
                "UPDATE supplier_candidates SET status = 'approved', supplier_id = ?, "
                "updated_at = ? WHERE id = ?", (supplier_id, now, candidate_id),
            )
        self.log("info", "supplier_candidate_approved", None, {
            "candidate_id": candidate_id, "supplier_id": supplier_id,
        })
        return supplier_id

    def delete_supplier(self, supplier_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
        self.log("info", "supplier_deleted", None, {"supplier_id": supplier_id})

    def _next_campaign_code(self, connection: sqlite3.Connection) -> str:
        year = datetime.now().year
        prefix = f"RFQ-{year}-"
        rows = connection.execute(
            "SELECT code FROM campaigns WHERE code LIKE ? ORDER BY id DESC LIMIT 50",
            (prefix + "%",),
        ).fetchall()
        numbers = []
        for row in rows:
            match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", row["code"])
            if match:
                numbers.append(int(match.group(1)))
        return f"{prefix}{(max(numbers, default=0) + 1):04d}"

    def create_campaign(
        self,
        name: str,
        subject: str,
        body: str,
        deadline: str | None,
        supplier_ids: Iterable[int],
    ) -> int:
        ids = list(dict.fromkeys(int(value) for value in supplier_ids))
        if not ids:
            raise ValueError("Не выбран ни один поставщик")
        now = utc_now()
        with self.connect() as connection:
            code = self._next_campaign_code(connection)
            cursor = connection.execute(
                "INSERT INTO campaigns(code, name, subject, body, deadline, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (code, name.strip(), subject.strip(), body.strip(), deadline or None, now),
            )
            campaign_id = int(cursor.lastrowid)
            placeholders = ",".join("?" for _ in ids)
            suppliers = connection.execute(
                f"SELECT id, name, email, excluded FROM suppliers WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            if len(suppliers) != len(ids):
                raise ValueError("Часть выбранных поставщиков не найдена")
            excluded = [row["name"] for row in suppliers if row["excluded"]]
            if excluded:
                raise ValueError(
                    "Эти поставщики исключены из рассылок: " + ", ".join(excluded)
                )
            connection.executemany(
                "INSERT INTO campaign_recipients(campaign_id, supplier_id, email) VALUES(?, ?, ?)",
                ((campaign_id, row["id"], row["email"]) for row in suppliers),
            )
        self.log("info", "campaign_created", campaign_id, {"code": code, "recipients": len(ids)})
        return campaign_id

    def add_campaign_attachment(
        self, campaign_id: int, filename: str, path: str, size: int, sha256: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO campaign_attachments"
                "(campaign_id, filename, path, size, sha256, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (campaign_id, filename, path, size, sha256, utc_now()),
            )

    def set_campaign_request_path(self, campaign_id: int, path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE campaigns SET request_path = ? WHERE id = ?", (path, campaign_id)
            )

    def get_campaign(self, campaign_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()

    def get_campaign_by_code(self, code: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM campaigns WHERE code = ?", (code.upper(),)).fetchone()

    def list_campaigns(self) -> list[sqlite3.Row]:
        sql = f"""
            SELECT c.*,
                   COUNT(r.id) AS recipient_count,
                   SUM(CASE WHEN r.status IN ({','.join('?' for _ in TERMINAL_RECIPIENT_STATUSES)}) THEN 1 ELSE 0 END) AS completed_count,
                   SUM(CASE WHEN r.status IN ({','.join('?' for _ in WAITING_RECIPIENT_STATUSES)}) THEN 1 ELSE 0 END) AS waiting_count,
                   COALESCE((SELECT COUNT(*) FROM attachments a
                       JOIN incoming_messages m ON m.id = a.incoming_message_id
                       WHERE m.campaign_id = c.id AND a.is_allowed = 1 AND a.duplicate_of_id IS NULL), 0) AS file_count
            FROM campaigns c
            LEFT JOIN campaign_recipients r ON r.campaign_id = c.id
            GROUP BY c.id
            ORDER BY c.id DESC
        """
        params = TERMINAL_RECIPIENT_STATUSES + WAITING_RECIPIENT_STATUSES
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_recipients(self, campaign_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT r.*, s.name AS supplier_name, s.excluded AS supplier_excluded,
                           COALESCE((SELECT COUNT(*) FROM attachments a
                               JOIN incoming_messages m ON m.id = a.incoming_message_id
                               WHERE m.recipient_id = r.id AND a.is_allowed = 1), 0) AS file_count
                    FROM campaign_recipients r
                    JOIN suppliers s ON s.id = r.supplier_id
                    WHERE r.campaign_id = ?
                    ORDER BY s.name COLLATE NOCASE
                    """,
                    (campaign_id,),
                )
            )

    def get_recipient(self, recipient_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT r.*, s.name AS supplier_name, c.code AS campaign_code
                FROM campaign_recipients r
                JOIN suppliers s ON s.id = r.supplier_id
                JOIN campaigns c ON c.id = r.campaign_id
                WHERE r.id = ?
                """,
                (recipient_id,),
            ).fetchone()

    def list_outgoing_attachments(self, campaign_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM campaign_attachments WHERE campaign_id = ? ORDER BY id",
                    (campaign_id,),
                )
            )

    def list_selected_offer_attachments(self, campaign_id: int) -> list[sqlite3.Row]:
        """Только подтверждённо привязанные файлы ответов без точных дублей."""
        with self.connect() as connection:
            return list(connection.execute(
                """
                SELECT a.path, a.filename, a.sha256, s.name AS supplier_name
                FROM attachments a
                JOIN incoming_messages m ON m.id = a.incoming_message_id
                JOIN campaign_recipients r ON r.id = m.recipient_id
                JOIN suppliers s ON s.id = r.supplier_id
                WHERE m.campaign_id = ? AND m.needs_review = 0
                  AND a.is_allowed = 1 AND a.is_selected = 1
                  AND a.duplicate_of_id IS NULL
                ORDER BY s.name COLLATE NOCASE, m.id, a.id
                """, (campaign_id,),
            ))

    def record_outgoing(
        self, campaign_id: int, recipient_id: int, message_id: str, subject: str, raw_eml_path: str
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO outgoing_messages"
                "(campaign_id, recipient_id, message_id, subject, sent_at, raw_eml_path) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (campaign_id, recipient_id, message_id.lower(), subject, now, raw_eml_path),
            )
            connection.execute(
                "UPDATE campaign_recipients SET status = 'sent', sent_at = ? WHERE id = ?",
                (now, recipient_id),
            )
            connection.execute(
                "UPDATE campaigns SET status = 'active', sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (now, campaign_id),
            )

    def record_outgoing_attempt(
        self, campaign_id: int, recipient_id: int, message_id: str, subject: str, raw_eml_path: str
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO outgoing_messages"
                "(campaign_id, recipient_id, message_id, subject, sent_at, raw_eml_path, delivery_status) "
                "VALUES(?, ?, ?, ?, ?, ?, 'unknown')",
                (campaign_id, recipient_id, message_id.lower(), subject, now, raw_eml_path),
            )
            connection.execute(
                "UPDATE campaign_recipients SET status = 'send_unknown', notes = '' WHERE id = ?",
                (recipient_id,),
            )
            connection.execute(
                "UPDATE campaigns SET status = 'active' WHERE id = ?",
                (campaign_id,),
            )

    def confirm_outgoing(self, message_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT campaign_id, recipient_id FROM outgoing_messages WHERE message_id = ? COLLATE NOCASE",
                (message_id,),
            ).fetchone()
            if not row:
                raise ValueError("Попытка отправки не найдена")
            connection.execute(
                "UPDATE outgoing_messages SET delivery_status = 'sent', sent_at = ? "
                "WHERE message_id = ? COLLATE NOCASE",
                (now, message_id),
            )
            connection.execute(
                "UPDATE campaign_recipients SET status = 'sent', sent_at = ?, notes = '' WHERE id = ?",
                (now, row["recipient_id"]),
            )
            connection.execute(
                "UPDATE campaigns SET sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (now, row["campaign_id"]),
            )

    def mark_send_unknown(self, recipient_id: int, error: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM campaign_recipients WHERE id = ?", (recipient_id,)
            ).fetchone()
            connection.execute(
                "UPDATE campaign_recipients SET status = 'send_unknown', notes = ? WHERE id = ?",
                (error[:1000], recipient_id),
            )
        self.log(
            "warning", "send_unknown", int(row["campaign_id"]) if row else None,
            {"error": error, "recipient_id": recipient_id},
        )

    def mark_outgoing_rejected(self, message_id: str, recipient_id: int, error: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM campaign_recipients WHERE id = ?", (recipient_id,)
            ).fetchone()
            connection.execute(
                "UPDATE outgoing_messages SET delivery_status = 'failed' "
                "WHERE message_id = ? COLLATE NOCASE",
                (message_id,),
            )
            connection.execute(
                "UPDATE campaign_recipients SET status = 'send_failed', notes = ? WHERE id = ?",
                (error[:1000], recipient_id),
            )
        self.log("error", "send_failed", int(row["campaign_id"]) if row else None,
                 {"error": error, "recipient_id": recipient_id})

    def mark_send_failed(self, recipient_id: int, error: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM campaign_recipients WHERE id = ?", (recipient_id,)
            ).fetchone()
            connection.execute(
                "UPDATE campaign_recipients SET status = 'send_failed', notes = ? WHERE id = ?",
                (error[:1000], recipient_id),
            )
        self.log("error", "send_failed", int(row["campaign_id"]) if row else None, {"error": error})

    def find_outgoing_by_references(self, message_ids: Iterable[str]) -> sqlite3.Row | None:
        normalized = [value.strip().lower() for value in message_ids if value.strip()]
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            return connection.execute(
                f"SELECT * FROM outgoing_messages WHERE message_id IN ({placeholders}) "
                "AND delivery_status != 'failed' ORDER BY id DESC LIMIT 1",
                normalized,
            ).fetchone()

    def find_recipient_in_campaign(self, campaign_id: int, sender_email: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND email = ? COLLATE NOCASE",
                (campaign_id, sender_email),
            ).fetchone()

    def find_active_recipients_by_sender(self, sender_email: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT r.*, c.code, COALESCE(c.sent_at, c.created_at) AS campaign_sent_at
                    FROM campaign_recipients r
                    JOIN campaigns c ON c.id = r.campaign_id
                    WHERE r.email = ? COLLATE NOCASE AND c.status IN ('active', 'ready')
                    ORDER BY c.id DESC
                    """,
                    (sender_email,),
                )
            )

    def active_campaign_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS value FROM campaigns WHERE status IN ('active', 'ready')"
            ).fetchone()
        return int(row["value"])

    def earliest_active_sent_at(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MIN(COALESCE(sent_at, created_at)) AS value "
                "FROM campaigns WHERE status IN ('active', 'ready')"
            ).fetchone()
        return row["value"] if row else None

    def insert_incoming(
        self,
        *,
        mailbox: str,
        folder: str,
        uid_validity: str,
        imap_uid: int,
        campaign_id: int | None,
        recipient_id: int | None,
        message_id: str,
        in_reply_to: str,
        sender_email: str,
        subject: str,
        received_at: str | None,
        body_text: str,
        raw_eml_path: str,
        match_method: str,
        needs_review: bool,
    ) -> tuple[int, bool]:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM incoming_messages WHERE mailbox = ? COLLATE NOCASE "
                "AND folder = ? AND uid_validity = ? AND imap_uid = ?",
                (mailbox, folder, uid_validity, imap_uid),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO incoming_messages(
                    mailbox, folder, uid_validity, imap_uid, campaign_id, recipient_id, message_id,
                    in_reply_to, sender_email, subject, received_at, body_text,
                    raw_eml_path, match_method, needs_review, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mailbox,
                    folder,
                    uid_validity,
                    imap_uid,
                    campaign_id,
                    recipient_id,
                    message_id.lower(),
                    in_reply_to,
                    sender_email.lower(),
                    subject,
                    received_at,
                    body_text,
                    raw_eml_path,
                    match_method,
                    int(needs_review),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid), True

    def get_incoming_by_uid(
        self, mailbox: str, folder: str, uid_validity: str, imap_uid: int
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM incoming_messages "
                "WHERE mailbox = ? COLLATE NOCASE AND folder = ? "
                "AND uid_validity = ? AND imap_uid = ?",
                (mailbox, folder, uid_validity, imap_uid),
            ).fetchone()

    def find_incoming_attachment(
        self, incoming_message_id: int, sha256: str, filename: str
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM attachments WHERE incoming_message_id = ? "
                "AND sha256 = ? AND filename = ?",
                (incoming_message_id, sha256, filename),
            ).fetchone()

    def mark_incoming_complete(self, incoming_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE incoming_messages SET processed_at = ? WHERE id = ?",
                (utc_now(), incoming_id),
            )

    def update_incoming_raw_path(self, incoming_id: int, raw_path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE incoming_messages SET raw_eml_path = ? WHERE id = ?", (raw_path, incoming_id)
            )

    def find_attachment_duplicate(self, recipient_id: int | None, sha256: str) -> sqlite3.Row | None:
        if recipient_id is None:
            return None
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT a.* FROM attachments a
                JOIN incoming_messages m ON m.id = a.incoming_message_id
                WHERE m.recipient_id = ? AND a.sha256 = ? AND a.duplicate_of_id IS NULL
                ORDER BY a.id LIMIT 1
                """,
                (recipient_id, sha256),
            ).fetchone()

    def add_incoming_attachment(
        self,
        *,
        incoming_message_id: int,
        filename: str,
        content_type: str,
        size: int,
        sha256: str,
        path: str,
        is_allowed: bool,
        duplicate_of_id: int | None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO attachments(
                    incoming_message_id, filename, content_type, size, sha256, path,
                    is_allowed, duplicate_of_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incoming_message_id,
                    filename,
                    content_type,
                    size,
                    sha256,
                    path,
                    int(is_allowed),
                    duplicate_of_id,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def finalize_incoming(self, incoming_id: int) -> None:
        now = utc_now()
        with self.connect() as connection:
            message = connection.execute(
                "SELECT campaign_id, recipient_id FROM incoming_messages WHERE id = ?", (incoming_id,)
            ).fetchone()
            if not message or message["recipient_id"] is None:
                return
            allowed = connection.execute(
                "SELECT COUNT(*) AS value FROM attachments WHERE incoming_message_id = ? AND is_allowed = 1",
                (incoming_id,),
            ).fetchone()["value"]
            recipient = connection.execute(
                "SELECT status FROM campaign_recipients WHERE id = ?", (message["recipient_id"],)
            ).fetchone()
            if allowed:
                status = "files_received"
                completed_at = now
            elif recipient and recipient["status"] == "files_received":
                status = "files_received"
                completed_at = None
            else:
                status = "reply_without_files"
                completed_at = None
            connection.execute(
                """
                UPDATE campaign_recipients
                SET status = ?, last_response_at = ?, completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (status, now, completed_at, message["recipient_id"]),
            )
        if message["campaign_id"] is not None:
            self.recompute_campaign(int(message["campaign_id"]))

    def assign_incoming(self, incoming_id: int, recipient_id: int) -> None:
        recipient = self.get_recipient(recipient_id)
        if not recipient:
            raise ValueError("Получатель не найден")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE incoming_messages
                SET campaign_id = ?, recipient_id = ?, match_method = 'manual', needs_review = 0
                WHERE id = ?
                """,
                (recipient["campaign_id"], recipient_id, incoming_id),
            )
        self.finalize_incoming(incoming_id)
        self.log(
            "info",
            "incoming_assigned",
            int(recipient["campaign_id"]),
            {"incoming_id": incoming_id, "recipient_id": recipient_id},
        )

    def list_incoming(
        self, limit: int = 300, *, campaign_id: int | None = None,
        needs_review: bool | None = None,
    ) -> list[sqlite3.Row]:
        conditions: list[str] = []
        parameters: list[object] = []
        if campaign_id is not None:
            conditions.append("m.campaign_id = ?")
            parameters.append(campaign_id)
        if needs_review is not None:
            conditions.append("m.needs_review = ?")
            parameters.append(int(needs_review))
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT m.*, c.code AS campaign_code, s.name AS supplier_name,
                           COUNT(a.id) AS attachment_count,
                           SUM(CASE WHEN a.is_allowed = 1 THEN 1 ELSE 0 END) AS allowed_count
                    FROM incoming_messages m
                    LEFT JOIN campaigns c ON c.id = m.campaign_id
                    LEFT JOIN campaign_recipients r ON r.id = m.recipient_id
                    LEFT JOIN suppliers s ON s.id = r.supplier_id
                    LEFT JOIN attachments a ON a.incoming_message_id = m.id
                    {where_clause}
                    GROUP BY m.id
                    ORDER BY COALESCE(m.received_at, m.created_at) DESC
                    LIMIT ?
                    """,
                    (*parameters, limit),
                )
            )

    def get_incoming(self, incoming_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM incoming_messages WHERE id = ?", (incoming_id,)
            ).fetchone()

    def list_incoming_attachments(self, incoming_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM attachments WHERE incoming_message_id = ? ORDER BY id",
                    (incoming_id,),
                )
            )

    def set_recipient_status(self, recipient_id: int, status: str) -> None:
        completed = utc_now() if status in TERMINAL_RECIPIENT_STATUSES else None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM campaign_recipients WHERE id = ?", (recipient_id,)
            ).fetchone()
            if not row:
                raise ValueError("Получатель не найден")
            connection.execute(
                "UPDATE campaign_recipients SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed, recipient_id),
            )
        self.recompute_campaign(int(row["campaign_id"]))
        self.log("info", "recipient_status_changed", int(row["campaign_id"]), {"status": status})

    def recompute_campaign(self, campaign_id: int) -> None:
        placeholders = ",".join("?" for _ in TERMINAL_RECIPIENT_STATUSES)
        with self.connect() as connection:
            campaign = connection.execute(
                "SELECT status FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if not campaign or campaign["status"] in ("draft", "closed"):
                return
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END) AS terminal
                FROM campaign_recipients WHERE campaign_id = ?
                """,
                (*TERMINAL_RECIPIENT_STATUSES, campaign_id),
            ).fetchone()
            status = "ready" if row["total"] and row["total"] == row["terminal"] else "active"
            connection.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))

    def set_campaign_closed(self, campaign_id: int) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE campaigns SET status = 'closed' WHERE id = ?", (campaign_id,))
        self.log("info", "campaign_closed", campaign_id, {})

    def get_sync_state(self, mailbox: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM mail_sync_state WHERE mailbox = ? COLLATE NOCASE", (mailbox,)
            ).fetchone()

    def set_sync_state(
        self, mailbox: str, folder: str, uid_validity: str | None, last_uid: int
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mail_sync_state(mailbox, folder, uid_validity, last_uid, last_checked_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(mailbox) DO UPDATE SET
                    folder = excluded.folder,
                    uid_validity = excluded.uid_validity,
                    last_uid = excluded.last_uid,
                    last_checked_at = excluded.last_checked_at
                """,
                (mailbox.lower(), folder, uid_validity, last_uid, now),
            )
            connection.execute(
                "UPDATE campaigns SET last_checked_at = ? WHERE status IN ('active', 'ready')", (now,)
            )

    def start_mail_check(self, trigger_name: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO mail_checks(trigger_name, started_at, status) VALUES(?, ?, 'running')",
                (trigger_name, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_mail_check(
        self,
        check_id: int,
        status: str,
        new_messages: int = 0,
        new_attachments: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE mail_checks
                SET finished_at = ?, status = ?, new_messages = ?, new_attachments = ?, error = ?
                WHERE id = ?
                """,
                (utc_now(), status, new_messages, new_attachments, error, check_id),
            )

    def dashboard_stats(self) -> dict[str, int]:
        with self.connect() as connection:
            suppliers = connection.execute("SELECT COUNT(*) AS v FROM suppliers").fetchone()["v"]
            active = connection.execute(
                "SELECT COUNT(*) AS v FROM campaigns WHERE status = 'active'"
            ).fetchone()["v"]
            ready = connection.execute(
                "SELECT COUNT(*) AS v FROM campaigns WHERE status = 'ready'"
            ).fetchone()["v"]
            files = connection.execute(
                "SELECT COUNT(*) AS v FROM attachments WHERE is_allowed = 1 AND duplicate_of_id IS NULL"
            ).fetchone()["v"]
        return {"suppliers": suppliers, "active": active, "ready": ready, "files": files}

    def log(
        self,
        level: str,
        event_type: str,
        campaign_id: int | None,
        details: dict[str, Any] | str,
    ) -> None:
        payload = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(created_at, level, event_type, campaign_id, details) VALUES(?, ?, ?, ?, ?)",
                (utc_now(), level, event_type, campaign_id, payload),
            )

    def list_logs(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                )
            )

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(self.path)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
