import unittest
from unittest.mock import patch
from automation.supplier_search import SearchResult
from webapp.search_gateway import create_gateway


class GatewayTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {"CODEX_GATEWAY_TOKEN": "a" * 48}):
            self.app = create_gateway()
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + "a" * 48}

    def test_gateway_requires_internal_token_and_validates_payload(self):
        self.assertEqual(self.client.post("/search", json={"query": "steel"}).status_code, 401)
        for payload in [[], {}, {"query": "steel", "count": "50"}, {"query": "steel", "categories": [3]}]:
            self.assertEqual(self.client.post("/search", json=payload, headers=self.headers).status_code, 400)

    def test_valid_request_returns_schema_compatible_results(self):
        with patch("webapp.search_gateway.run_codex_search", return_value=SearchResult([], 0)) as search:
            response = self.client.post("/search", json={"query": "steel suppliers", "count": 5}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"candidates": []})
        self.assertEqual(search.call_args.kwargs["maximum_companies"], 5)
