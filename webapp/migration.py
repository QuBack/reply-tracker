"""Offline, non-destructive import of a desktop data directory into a new account."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path, PurePosixPath

from automation.database import Database
from .store import Store


def import_desktop(store: Store, email: str, source: Path) -> Path:
    source = source.resolve()
    if not (source / "app.db").is_file():
        raise ValueError("В исходной папке нет app.db")
    with store.connect() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if user is None:
            raise ValueError("Сначала создайте аккаунт")
    accounts = store.root / "accounts"
    accounts.mkdir(exist_ok=True)
    target = accounts / user["id"]
    if target.exists():
        raise ValueError("Импорт разрешён только в новый аккаунт до первого входа. Данные не перезаписаны.")
    if source.is_relative_to(store.root) or store.root.is_relative_to(source):
        raise ValueError("Исходная папка и папка веб-данных должны быть раздельными")

    def remap(value):
        if not value:
            return value
        normalized = str(value).replace("\\", "/")
        for folder in ("campaigns", "unmatched", "backups"):
            marker = "/" + folder + "/"
            index = normalized.casefold().find(marker)
            if index >= 0:
                relative = PurePosixPath(normalized[index + 1:])
                if ".." in relative.parts:
                    raise ValueError("В базе есть небезопасный путь")
                return str(target.joinpath(*relative.parts))
        raise ValueError("В базе есть внешние пути к файлам. Сначала соберите файлы внутри папки данных.")

    # Stage the entire import next to the final directory, then atomically publish it.
    with tempfile.TemporaryDirectory(prefix="desktop-import-", dir=store.root) as temporary:
        staging = Path(temporary).resolve()
        if staging.parent != store.root:
            raise ValueError("Некорректная папка импорта")
        for folder in ("campaigns", "unmatched", "backups"):
            origin = source / folder
            if origin.is_dir():
                for entry in origin.rglob("*"):
                    if entry.is_symlink() or not entry.resolve().is_relative_to(source):
                        raise ValueError("Исходная папка содержит ссылки за её пределы")
                shutil.copytree(origin, staging / folder)
        original = sqlite3.connect((source / "app.db").as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(staging / "app.db")
        try:
            original.backup(snapshot)
        finally:
            snapshot.close()
            original.close()
        database = Database(staging / "app.db")
        with database.connect() as db:
            for table, column in (("campaigns", "request_path"), ("campaign_attachments", "path"),
                ("outgoing_messages", "raw_eml_path"), ("incoming_messages", "raw_eml_path"),
                ("attachments", "path"), ("ai_runs", "output_path")):
                for row in db.execute(f"SELECT id,{column} FROM {table}").fetchall():
                    db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (remap(row[column]), row["id"]))
            for row in db.execute("SELECT id,attachments_json FROM outgoing_messages").fetchall():
                attachments = [remap(value) for value in json.loads(row["attachments_json"] or "[]")]
                db.execute("UPDATE outgoing_messages SET attachments_json=? WHERE id=?",
                           (json.dumps(attachments, ensure_ascii=False), row["id"]))
            # Audit details may contain obsolete absolute paths, not used for file access.
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('email_address','')")
        if not target.resolve().is_relative_to(accounts.resolve()) or target.exists():
            raise ValueError("Целевая папка занята")
        # TemporaryDirectory owns the staging parent; moving only its child avoids
        # its cleanup attempting to remove a destination that now belongs to a user.
        final_stage = staging / "account"
        final_stage.mkdir()
        for item in list(staging.iterdir()):
            if item != final_stage:
                item.rename(final_stage / item.name)
        final_stage.rename(target)
    return target
