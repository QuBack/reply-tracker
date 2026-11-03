from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.supplier_search import Candidate, SearchResult
from webapp import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.temp.name, "ALLOW_REGISTRATION": True})
        self.runtime = self.app.extensions["runtime"]
        self.store = self.runtime.store
        self.alice_id = self.store.create_user("alice@example.com", "correct-password-123")
        self.bob_id = self.store.create_user("bob@example.com", "another-password-123")
        self.alice, self.bob = self.app.test_client(), self.app.test_client()
        self.login(self.alice, "alice@example.com", "correct-password-123")
        self.login(self.bob, "bob@example.com", "another-password-123")

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def csrf(self, client):
        with client.session_transaction() as session:
            return session["csrf"]

    def post(self, client, path, data=None, **kwargs):
        return client.post(path, data={"csrf": self.csrf(client), **(data or {})}, **kwargs)

    def login(self, client, email, password):
        client.get("/login")
        self.assertEqual(self.post(client, "/login", {"email": email, "password": password}).status_code, 303)

    def supplier(self, client, name="Supplier", email="supplier@example.com"):
        response = self.post(client, "/suppliers/save", {"name": name, "email": email, "categories": "Steel"})
        self.assertEqual(response.status_code, 303, response.data.decode())

    def campaign(self, client):
        self.supplier(client)
        response = self.post(client, "/campaigns/new", {"name": "Private RFQ", "subject": "Prices",
            "body": "Please quote", "suppliers": "1", "request": (io.BytesIO(b"xlsx"), "request.xlsx")})
        self.assertEqual(response.status_code, 303, response.data.decode())
        return response.headers["Location"]

    def test_business_data_files_jobs_and_credentials_are_isolated(self):
        location = self.campaign(self.alice)
        service = self.runtime.service(self.alice_id)
        service.credentials.write("alice-mail@example.com", "mail-secret-password")
        path = service.paths.root / "private.txt"
        path.write_text("private attachment")
        file_id = self.runtime.register_file(self.alice_id, path)
        self.store.enqueue(self.alice_id, "mail", {})
        self.assertNotIn(b"Private RFQ", self.bob.get("/campaigns").data)
        self.assertNotIn(b"supplier@example.com", self.bob.get("/suppliers").data)
        self.assertEqual(self.bob.get(location).status_code, 404)
        self.assertEqual(self.bob.get("/files/" + file_id).status_code, 404)
        response = self.alice.get("/files/" + file_id)
        self.assertEqual(response.data, b"private attachment")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        response.close()
        self.assertEqual(self.bob.get("/api/jobs").json, [])
        self.assertFalse(self.runtime.service(self.bob_id).credentials.has_secret())
        with self.store.connect() as db:
            encrypted = db.execute("SELECT encrypted FROM credentials").fetchone()[0]
        self.assertNotIn(b"mail-secret-password", encrypted)
        self.assertEqual(service.credentials.read(), ("alice-mail@example.com", "mail-secret-password"))
        self.assertNotIn(b"mail-secret-password", self.alice.get("/settings").data)

    def test_anonymous_requests_and_csrf(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/suppliers").status_code, 302)
        self.assertEqual(anonymous.get("/api/jobs").status_code, 401)
        self.assertEqual(self.alice.post("/suppliers/save", data={"name": "Evil"}).status_code, 400)
        self.assertEqual(self.alice.post("/logout", data={"csrf": "wrong"}).status_code, 400)
        self.assertIsNotNone(self.alice.get("/api/jobs").json)

    def test_overlapping_numeric_ids_still_resolve_only_own_data(self):
        self.campaign(self.alice)
        self.campaign(self.bob)
        bob = self.runtime.service(self.bob_id)
        with bob.db.connect() as db:
            db.execute("UPDATE campaigns SET name='Bob private campaign' WHERE id=1")
        self.assertNotIn(b"Bob private campaign", self.alice.get("/campaigns/1").data)
        self.assertIn(b"Bob private campaign", self.bob.get("/campaigns/1").data)
        self.assertEqual(self.post(self.alice, "/campaigns/1/close").status_code, 303)
        self.assertEqual(bob.db.get_campaign(1)["status"], "draft")

    def test_incoming_html_is_plain_text_and_attachment_is_private(self):
        self.campaign(self.alice)
        service = self.runtime.service(self.alice_id)
        message, _ = service.db.insert_incoming(mailbox="alice@example.com", folder="INBOX", uid_validity="1",
            imap_uid=1, campaign_id=1, recipient_id=1, message_id="<reply@example.com>", in_reply_to="",
            sender_email="supplier@example.com", subject="Our quote", received_at=None,
            body_text='<script>alert("email")</script>', raw_eml_path="", match_method="manual", needs_review=False)
        attachment = service.paths.campaigns / "quote.pdf"
        attachment.write_bytes(b"%PDF-demo")
        service.db.add_incoming_attachment(incoming_message_id=message, filename="quote.pdf", content_type="application/pdf",
            size=9, sha256="hash", path=str(attachment), is_allowed=True, duplicate_of_id=None)
        service.db.finalize_incoming(message)
        self.assertEqual(self.bob.get(f"/inbox/{message}").status_code, 404)
        html = self.alice.get(f"/inbox/{message}").data
        self.assertIn(b"&lt;script&gt;", html)
        self.assertNotIn(b'<script>alert("email")</script>', html)
        self.assertEqual(self.alice.get("/recipients/1").status_code, 200)
        self.assertEqual(self.post(self.alice, f"/inbox/{message}/read").status_code, 303)
        self.assertIsNotNone(service.db.get_incoming(message)["opened_at"])

    def test_single_server_lock_rejects_second_process(self):
        from webapp.__main__ import acquire_server_lock
        lock = acquire_server_lock(Path(self.temp.name))
        try:
            with self.assertRaises(RuntimeError):
                acquire_server_lock(Path(self.temp.name))
        finally:
            lock.close()

    def test_logout_revokes_captured_session(self):
        cookie = self.alice.get_cookie("reply_session").value
        self.post(self.alice, "/logout")
        attacker = self.app.test_client()
        attacker.set_cookie("reply_session", cookie)
        self.assertEqual(attacker.get("/api/jobs").status_code, 401)

    def test_password_reset_revokes_existing_sessions(self):
        self.store.reset_password("alice@example.com", "new-password-1234")
        self.assertEqual(self.alice.get("/api/jobs").status_code, 401)

    def test_drafts_render_and_do_not_send_mail(self):
        with patch("automation.service.MailGateway") as gateway:
            location = self.campaign(self.alice)
            for route in [location, "/", "/campaigns", "/suppliers", "/search", "/campaigns/new",
                          "/inbox", "/settings", "/jobs", "/logs", "/recipients/1"]:
                response = self.alice.get(route)
                self.assertEqual(response.status_code, 200, f"{route}: {response.data.decode()}")
            gateway.assert_not_called()
        self.assertEqual(self.store.jobs(self.alice_id), [])
        service = self.runtime.service(self.alice_id)
        self.assertEqual(service.db.get_campaign(1)["status"], "draft")
        request_path = Path(service.db.get_campaign(1)["request_path"])
        self.assertTrue(request_path.is_relative_to(service.paths.campaigns))

    def test_sending_requires_mail_setup_and_creates_one_job(self):
        self.campaign(self.alice)
        response = self.post(self.alice, "/campaigns/1/send")
        self.assertEqual(response.status_code, 400)
        service = self.runtime.service(self.alice_id)
        service.save_mail_preferences("alice@example.com", 10, "fake-mail-password")
        self.assertEqual(self.post(self.alice, "/campaigns/1/send").status_code, 303)
        self.assertEqual(self.post(self.alice, "/campaigns/1/send").status_code, 400)
        self.assertEqual(len(self.store.jobs(self.alice_id)), 1)
        self.assertEqual(self.bob.get("/api/jobs").json, [])

    def test_search_worker_imports_results_only_for_owner(self):
        response = self.post(self.alice, "/search", {"query": "steel suppliers", "region": "Moscow", "count": "5"})
        self.assertEqual(response.status_code, 303)
        candidate = Candidate("Found company", "https://found.example", "contact@found.example", "Moscow",
            ["Steel"], "Makes steel", ["https://found.example/contact"], "https://found.example/contact")
        with patch.dict("os.environ", {"WEB_LOCAL_CODEX": "1", "CODEX_SEARCH_URL": ""}), \
             patch("webapp.runtime.run_codex_search", return_value=SearchResult([candidate], 0)):
            self.assertTrue(self.runtime.run_one(search=True))
        self.assertEqual(self.store.jobs(self.alice_id)[0]["state"], "done")
        self.assertIn(b"Found company", self.alice.get("/search").data)
        self.assertNotIn(b"Found company", self.bob.get("/search").data)
        self.assertEqual(self.post(self.alice, "/candidates/1/approve").status_code, 303)
        self.assertIn(b"Found company", self.alice.get("/suppliers").data)

    def test_search_gateway_receives_no_tenant_paths_or_credentials(self):
        self.store.enqueue(self.alice_id, "search", {"query": "steel suppliers", "region": "", "count": 5})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"candidates":[]}'
        with patch.dict("os.environ", {"CODEX_SEARCH_URL": "http://search:8090/search", "CODEX_GATEWAY_TOKEN": "token"}), \
             patch("webapp.runtime.urlopen", return_value=response) as call:
            self.runtime.run_one(True)
        payload = json.loads(call.call_args.args[0].data)
        self.assertEqual(set(payload), {"query", "region", "count", "categories"})
        self.assertNotIn(self.alice_id, json.dumps(payload))

    def test_interrupted_jobs_are_not_replayed(self):
        self.store.enqueue(self.alice_id, "send", {"campaign_id": 1})
        job = self.store.claim(False)
        self.assertIsNotNone(job)
        self.runtime.start()
        self.runtime.close()
        self.assertEqual(self.store.jobs(self.alice_id)[0]["state"], "failed")

    def test_scheduler_checks_active_mail_without_browser_and_deduplicates(self):
        self.campaign(self.alice)
        service = self.runtime.service(self.alice_id)
        service.save_mail_preferences("alice@example.com", 10, "fake-mail-password")
        service.db.record_outgoing(1, 1, "<sent@example.test>", "Prices", "unused.eml")
        self.runtime.schedule_mail()
        jobs = self.store.jobs(self.alice_id)
        self.assertEqual([(j["kind"], j["state"]) for j in jobs], [("mail", "queued")])
        with self.store.connect() as db:
            db.execute("UPDATE users SET next_mail_check=0 WHERE id=?", (self.alice_id,))
        self.runtime.schedule_mail()
        self.assertEqual(len(self.store.jobs(self.alice_id)), 1)
        self.assertEqual(self.store.jobs(self.bob_id), [])

    def test_backup_job_download_contains_only_owners_business_data(self):
        self.campaign(self.alice)
        self.supplier(self.bob, "Bob supplier", "bob-supplier@example.test")
        self.store.enqueue(self.alice_id, "backup", {})
        self.assertTrue(self.runtime.run_one())
        job = self.store.jobs(self.alice_id)[0]
        self.assertEqual(job["state"], "done")
        result = json.loads(job["result"])
        self.assertNotIn("path", result)
        file_id = result["file_id"]
        self.assertEqual(self.bob.get("/files/" + file_id).status_code, 404)
        path, _ = self.runtime.file(self.alice_id, file_id)
        import zipfile
        with zipfile.ZipFile(path) as archive:
            self.assertIn("app.db", archive.namelist())
            self.assertTrue(any(name.endswith("request.xlsx") for name in archive.namelist()))

    def test_download_rejects_paths_outside_tenant(self):
        foreign = Path(self.temp.name) / "server.db"
        with self.assertRaises(ValueError):
            self.runtime.register_file(self.alice_id, foreign)
        with self.store.connect() as db:
            db.execute("INSERT INTO files VALUES(?,?,?,?)", ("poison", self.alice_id, str(foreign), "server.db"))
        self.assertEqual(self.alice.get("/files/poison").status_code, 404)

    def test_template_escapes_supplier_and_email_content(self):
        self.supplier(self.alice, '<script>alert("x")</script>')
        html = self.alice.get("/suppliers").data
        self.assertNotIn(b'<script>alert("x")</script>', html)
        self.assertIn(b"&lt;script&gt;", html)
        self.assertIn("script-src 'self'", self.alice.get("/").headers["Content-Security-Policy"])

    def test_login_rate_limit_and_registration_can_be_disabled(self):
        other = self.app.test_client()
        other.get("/login")
        for _ in range(10):
            response = self.post(other, "/login", {"email": "missing@example.com", "password": "bad"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(other, "/login", {"email": "missing@example.com", "password": "bad"}).status_code, 429)
        self.app.config["ALLOW_REGISTRATION"] = False
        self.assertEqual(other.get("/register").status_code, 404)

    def test_upload_rejects_executable_and_large_request(self):
        self.supplier(self.alice)
        response = self.post(self.alice, "/campaigns/new", {"name": "RFQ", "subject": "Prices", "body": "Quote",
            "suppliers": "1", "attachments": (io.BytesIO(b"executable"), "run.exe")})
        self.assertEqual(response.status_code, 400)
        self.app.config["MAX_CONTENT_LENGTH"] = 1024
        self.assertEqual(self.alice.post("/campaigns/new", data=b"x" * 2048).status_code, 413)


if __name__ == "__main__":
    unittest.main()
