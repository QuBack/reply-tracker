import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from automation.openai_search import (APIUsage, OpenAISearchError, parse_search_response,
                                      request_search, response_usage)
from webapp import create_app
from webapp.config import load_environment


def api_response():
    url = "https://supplier.example/contact"
    candidate = dict(name="Verified company", website="https://supplier.example", email="sales@supplier.example",
        region="Moscow", categories=["Steel"], evidence="Supplies steel", source_urls=[url], contact_source_url=url,
        phones=[], contact_person="", address="")
    return {"status": "completed", "usage": {"input_tokens": 8000, "output_tokens": 2000}, "output": [
        {"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": url}]}},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"candidates": [candidate]})}]}]}


class OpenAISearchTests(unittest.TestCase):
    def test_request_has_fixed_model_limits_schema_and_no_retries(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(api_response()).encode()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-secret-000000000000", "OPENAI_SEARCH_MODEL": "gpt-6-luna"}), \
             patch("automation.openai_search.urlopen", return_value=response) as call:
            result = request_search("steel suppliers", "Moscow", [], 3)
        request = call.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["max_tool_calls"], 2)
        self.assertEqual(payload["max_output_tokens"], 4000)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertNotIn("sk-test", request.data.decode())
        self.assertEqual(len(parse_search_response(result, 3).candidates), 1)
        call.assert_called_once()

    def test_invented_source_or_missing_web_search_is_not_accepted(self):
        response = api_response()
        response["output"][0]["action"]["sources"] = [{"url": "https://unrelated.example"}]
        self.assertEqual(parse_search_response(response, 3).candidates, [])
        response["output"] = response["output"][1:]
        with self.assertRaises(OpenAISearchError):
            parse_search_response(response, 3)

    def test_incomplete_output_is_not_imported(self):
        response = api_response()
        response["status"] = "incomplete"
        with self.assertRaises(OpenAISearchError):
            parse_search_response(response, 3)

    def test_usage_rounds_up_and_counts_search_calls(self):
        usage = response_usage(api_response())
        self.assertEqual(usage.searches, 1)
        self.assertEqual(usage.estimated_cents, 2)
        self.assertIsNone(response_usage({"output": []}))
        self.assertEqual(APIUsage(8000, 2000, 2).estimated_cents, 3)

    def test_network_error_never_retries_or_reveals_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-private-secret-000000000"}), \
             patch("automation.openai_search.urlopen", side_effect=URLError("failure")) as call:
            with self.assertRaises(OpenAISearchError) as error:
                request_search("steel suppliers", "", [], 1)
        call.assert_called_once()
        self.assertNotIn("sk-private", str(error.exception))

    def test_quota_error_is_actionable_without_upstream_body(self):
        raw = json.dumps({"error": {"code": "insufficient_quota", "message": "sk-private"}}).encode()
        http_error = HTTPError("https://api.openai.com", 429, "error", {}, io.BytesIO(raw))
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-private-secret-000000000"}), \
             patch("automation.openai_search.urlopen", side_effect=http_error):
            with self.assertRaises(OpenAISearchError) as error:
                request_search("steel suppliers", "", [], 1)
        self.assertIn("Billing", str(error.exception))
        self.assertNotIn("sk-private", str(error.exception))

    def test_legacy_env_key_loads_without_overriding_server_environment(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "standard-value", "chatgpt_api": "legacy-value"}), \
             patch("webapp.config.load_dotenv"):
            load_environment()
            import os
            self.assertEqual(os.environ["OPENAI_API_KEY"], "standard-value")


class SearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.temp.name})
        self.runtime = self.app.extensions["runtime"]
        self.store = self.runtime.store
        self.alice = self.store.create_user("alice@example.test", "strong-password-123")
        self.bob = self.store.create_user("bob@example.test", "strong-password-456")

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_budget_blocks_another_account_before_api_call(self):
        self.store.enqueue(self.alice, "search", {"query": "steel suppliers", "region": "", "count": 1})
        self.store.enqueue(self.bob, "search", {"query": "steel suppliers", "region": "", "count": 1})
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "openai", "OPENAI_SEARCH_BUDGET_USD": "0.25"}), \
             patch("webapp.runtime.request_search", side_effect=OpenAISearchError("Timeout")) as call:
            self.runtime.run_one(True)
            self.runtime.run_one(True)
        call.assert_called_once()
        self.assertIn("лимит расходов", self.store.jobs(self.bob)[0]["message"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT SUM(cents) FROM search_spend").fetchone()[0], 25)

    def test_success_settles_reservation_and_preserves_account_isolation(self):
        self.store.enqueue(self.alice, "search", {"query": "steel suppliers", "region": "", "count": 1})
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "openai", "OPENAI_SEARCH_BUDGET_USD": "5"}), \
             patch("webapp.runtime.request_search", return_value=api_response()):
            self.runtime.run_one(True)
        job = self.store.jobs(self.alice)[0]
        self.assertEqual(job["state"], "done")
        self.assertEqual(json.loads(job["result"])["estimated_cost_usd"], "0.02")
        self.assertEqual(len(self.runtime.service(self.alice).db.list_candidates()), 1)
        self.assertEqual(len(self.runtime.service(self.bob).db.list_candidates()), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT cents FROM search_spend").fetchone()[0], 2)

    def test_budget_persists_across_restart(self):
        from webapp.store import Store
        self.assertTrue(self.store.reserve_search("job1", self.alice, 25, 25))
        reopened = Store(Path(self.temp.name))
        self.assertFalse(reopened.reserve_search("job2", self.bob, 25, 25))
