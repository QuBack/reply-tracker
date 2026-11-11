from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash


class Store:
    """Shared authentication and queue; business databases are physically separate."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        key_path = self.root / "secret.key"
        if not key_path.exists():
            try:
                with key_path.open("xb") as stream:
                    stream.write(Fernet.generate_key())
                key_path.chmod(0o600)
            except FileExistsError:
                pass
        self.key = key_path.read_bytes().strip()
        self.cipher = Fernet(self.key)
        self.signing_key = hashlib.sha256(self.key + b":web-sessions").digest()
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(32))
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, created REAL NOT NULL,
                    next_mail_check REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (
                    user_id TEXT PRIMARY KEY REFERENCES users(id), encrypted BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, started REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    path TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(user_id, path));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    message TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
                CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(user_id, created);
                CREATE TABLE IF NOT EXISTS search_spend (
                    job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    cents INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, searches INTEGER);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "server.db", timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, email: str, password: str) -> str:
        email = email.strip().lower()
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Укажите корректный email")
        if not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 12 до 256 символов")
        user_id = uuid.uuid4().hex
        password_hash = generate_password_hash(password)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO users(id,email,password_hash,created) VALUES(?,?,?,?)",
                           (user_id, email, password_hash, time.time()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Не удалось создать аккаунт с этим email") from exc
        return user_id

    def limited(self, key: str, limit: int = 10) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE started < ?", (now - 900,))
            db.execute("INSERT INTO attempts(key,count,started) VALUES(?,1,?) "
                       "ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now))
            return db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()[0] > limit

    def login(self, email: str, password: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else self.dummy_hash, password)
        if not row or not valid:
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (self.digest(token), row["id"], now + 12 * 3600))
        return token

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def user_for_session(self, token: str):
        with self.connect() as db:
            return db.execute("SELECT u.id,u.email FROM users u JOIN sessions s ON u.id=s.user_id "
                              "WHERE s.token_hash=? AND s.expires>?",
                              (self.digest(token), time.time())).fetchone()

    def logout(self, token: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.digest(token),))

    def reset_password(self, email: str, password: str):
        if not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 12 до 256 символов")
        with self.connect() as db:
            user = db.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
            if user is None:
                raise ValueError("Аккаунт не найден")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), user[0]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user[0],))

    def enqueue(self, user_id: str, kind: str, payload: dict) -> str:
        now, job_id = time.time(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT id,kind FROM jobs WHERE user_id=? AND state IN ('queued','running')",
                                (user_id,)).fetchall()
            if any(row["kind"] == kind for row in active):
                raise ValueError("Такая операция уже выполняется. Дождитесь результата в разделе «Задачи».")
            if len(active) >= 5:
                raise ValueError("В очереди уже пять задач. Дождитесь их завершения.")
            db.execute("INSERT INTO jobs(id,user_id,kind,payload,state,created,updated) VALUES(?,?,?,?,'queued',?,?)",
                       (job_id, user_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
        return job_id

    def claim(self, search: bool):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' AND "
                             + ("kind='search'" if search else "kind!='search'")
                             + " ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return row

    def finish(self, job_id: str, state: str, message: str, result: dict | None = None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,message=?,result=?,updated=? WHERE id=?",
                       (state, message, json.dumps(result or {}, ensure_ascii=False), time.time(), job_id))

    def jobs(self, user_id: str):
        with self.connect() as db:
            return db.execute("SELECT id,kind,state,created,updated,message,result FROM jobs "
                              "WHERE user_id=? ORDER BY created DESC LIMIT 100", (user_id,)).fetchall()

    def reserve_search(self, job_id: str, user_id: str, limit_cents: int, amount: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            spent = db.execute("SELECT COALESCE(SUM(cents),0) FROM search_spend").fetchone()[0]
            if limit_cents <= 0 or spent + amount > limit_cents:
                return False
            db.execute("INSERT INTO search_spend(job_id,user_id,cents,state,created) VALUES(?,?,?,'reserved',?)",
                       (job_id, user_id, amount, time.time()))
        return True

    def settle_search(self, job_id: str, usage):
        with self.connect() as db:
            db.execute("UPDATE search_spend SET cents=?,state='estimated',input_tokens=?,output_tokens=?,searches=? "
                       "WHERE job_id=?", (usage.estimated_cents, usage.input_tokens, usage.output_tokens, usage.searches, job_id))


class EncryptedCredentials:
    def __init__(self, store: Store, user_id: str):
        self.store, self.user_id = store, user_id

    def write(self, username: str, secret: str):
        if not secret:
            raise ValueError("Укажите пароль почты")
        encrypted = self.store.cipher.encrypt(json.dumps([username, secret]).encode())
        with self.store.connect() as db:
            db.execute("INSERT INTO credentials VALUES(?,?) ON CONFLICT(user_id) "
                       "DO UPDATE SET encrypted=excluded.encrypted", (self.user_id, encrypted))

    def read(self):
        with self.store.connect() as db:
            row = db.execute("SELECT encrypted FROM credentials WHERE user_id=?", (self.user_id,)).fetchone()
        return tuple(json.loads(self.store.cipher.decrypt(row[0]))) if row else None

    def has_secret(self):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM credentials WHERE user_id=?", (self.user_id,)).fetchone() is not None

    def delete(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM credentials WHERE user_id=?", (self.user_id,))
