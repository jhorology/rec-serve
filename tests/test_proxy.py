import time

import httpx
from fastapi.testclient import TestClient

from rec_serve.keepalive import KeepAliveManager
from rec_serve.proxy import create_app


def test_keepalive_initial_state():
    km = KeepAliveManager(timeout_sec=10.0, enabled=False)
    assert km.is_active is False
    assert km.status()["timeout_sec"] == 10


def test_health_includes_keepalive():
    # create_app without real backend: health will be degraded but still return keepalive
    app = create_app(
        backend_base="http://127.0.0.1:9",
        api_key="secret",
        keepalive_timeout=1800.0,
        enable_keepalive=False,
    )
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "rec-serve"
        assert data["status"] == "degraded"
        assert data["keepalive"]["timeout_sec"] == 1800


def test_proxy_replaces_client_api_key():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(b'{"object": "list", "data": [{"id": "flint"}]}'),
        )

    app = create_app(
        backend_base="http://backend", api_key="secret", enable_keepalive=False
    )
    with TestClient(app) as client:
        app.state.http = httpx.AsyncClient(
            base_url="http://backend", transport=httpx.MockTransport(handler)
        )
        km: KeepAliveManager = app.state.keepalive
        km.last_activity = time.monotonic() - 100.0
        resp = client.get("/v1/models", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        assert resp.json()["data"][0]["id"] == "flint"
        client.get("/v1/models")
        assert km.remaining_sec > 3500

    assert seen == ["Bearer secret", "Bearer secret"]


def test_proxy_reports_unavailable_backend():
    app = create_app(
        backend_base="http://127.0.0.1:9", api_key="secret", enable_keepalive=False
    )
    with TestClient(app) as client:
        resp = client.get("/v1/models")
        assert resp.status_code == 502
        assert resp.json()["error"] == "upstream_unavailable"
