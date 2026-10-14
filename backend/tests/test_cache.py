from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
import requests
import app.api as api_module


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = {"result": []} if data is None else data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("synthetic upstream failure")

    def json(self):
        return self.data


def test_503_preserves_valid_stale_cache(app, client, monkeypatch):
    path = Path(app.config["TRADE_CACHE_DIR"]) / "static.json"
    path.parent.mkdir()
    original = '{"result":["good"]}'
    path.write_text(original)
    os.utime(path, (0, 0))
    monkeypatch.setattr(api_module.requests, "get", lambda *a, **k: Response(503))
    response = client.get("/api/trade/static")
    assert response.status_code == 200 and response.json == {"result": ["good"]}
    assert response.headers["X-Cache-Stale"] == "true" and path.read_text() == original


def test_error_or_malformed_response_never_cached(app, client, monkeypatch):
    for response in [
        Response(503),
        Response(200, {"error": "bad"}),
        Response(200, "<html>bad</html>"),
    ]:
        monkeypatch.setattr(
            api_module.requests, "get", lambda *a, _response=response, **k: _response
        )
        assert client.get("/api/trade/static").status_code == 502
        assert not (Path(app.config["TRADE_CACHE_DIR"]) / "static.json").exists()


def test_cache_conditional_and_single_flight(app, monkeypatch):
    calls = []

    def get(*a, **k):
        calls.append(1)
        time.sleep(0.02)
        return Response()

    monkeypatch.setattr(api_module.requests, "get", get)

    def fetch(_):
        with app.test_client() as client:
            return client.get("/api/trade/static")

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fetch, range(5)))
    assert all(r.status_code == 200 for r in results) and len(calls) == 1
    with app.test_client() as client:
        assert (
            client.get(
                "/api/trade/static", headers={"If-None-Match": results[0].headers["ETag"]}
            ).status_code
            == 304
        )
