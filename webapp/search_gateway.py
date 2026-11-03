"""Internal Codex service. Deploy without access to the web application's data volume."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request
from automation.supplier_search import clean_categories, run_codex_search


def create_gateway():
    token = os.environ.get("CODEX_GATEWAY_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("CODEX_GATEWAY_TOKEN must contain at least 32 characters")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

    @app.post("/search")
    def search():
        if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            return {"error": "unauthorized"}, 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return {"error": "invalid payload"}, 400
        try:
            query, region = payload["query"], payload.get("region", "")
            count, categories = payload.get("count", 10), payload.get("categories", [])
            if (not isinstance(query, str) or not 5 <= len(query.strip()) <= 600
                or not isinstance(region, str) or len(region) > 150
                or type(count) is not int or not 1 <= count <= 50
                or not isinstance(categories, list) or len(categories) > 100
                or any(not isinstance(value, str) for value in categories)):
                return {"error": "invalid fields"}, 400
            with tempfile.TemporaryDirectory(prefix="codex-search-") as folder:
                result = run_codex_search(query, region, clean_categories(categories),
                    project_root=Path(__file__).resolve().parents[1], data_root=Path(folder), maximum_companies=count)
            return jsonify({"candidates": [asdict(candidate) for candidate in result.candidates]})
        except (KeyError, TypeError, ValueError):
            return {"error": "invalid fields"}, 400
        except Exception:
            app.logger.exception("Codex search failed")
            return {"error": "search failed"}, 502

    return app


if __name__ == "__main__":
    from waitress import serve
    # One Codex invocation at a time; the web server holds the durable queue.
    serve(create_gateway(), host="0.0.0.0", port=8090, threads=1, channel_timeout=1300)
