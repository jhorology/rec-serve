import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from rec_serve.keepalive import KeepAliveManager
from rec_serve.proxy import PAGE_ASSIST_ORIGIN, check_bindable, create_app


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


def _mock_backend(app, handler):
    app.state.http = httpx.AsyncClient(
        base_url="http://backend", transport=httpx.MockTransport(handler)
    )


def test_streaming_is_forwarded_and_touches_keepalive():
    chunks = [b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    app = create_app(
        backend_base="http://backend", api_key="secret", enable_keepalive=False
    )
    with TestClient(app) as client:
        _mock_backend(app, handler)
        km: KeepAliveManager = app.state.keepalive
        touches = []
        original = km.touch
        km.touch = lambda: (touches.append(1), original())[1]
        resp = client.post("/v1/chat/completions", json={"stream": True})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.content == b"".join(chunks)
    # middleware で 1 回 + ストリームのチャンクごと
    assert len(touches) >= 2


@pytest.mark.parametrize(
    "origin", ["https://example.com", "null", PAGE_ASSIST_ORIGIN + ".evil"]
)
def test_untrusted_browser_origin_is_rejected(origin):
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    app = create_app(
        backend_base="http://backend", api_key="secret", enable_keepalive=False
    )
    with TestClient(app) as client:
        _mock_backend(app, handler)
        resp = client.post("/v1/chat/completions", headers={"Origin": origin})
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in resp.headers
    assert called == []


def test_page_assist_origin_is_allowed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'))

    app = create_app(
        backend_base="http://backend", api_key="secret", enable_keepalive=False
    )
    with TestClient(app) as client:
        _mock_backend(app, handler)
        preflight = client.options(
            "/v1/chat/completions",
            headers={
                "Origin": PAGE_ASSIST_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == PAGE_ASSIST_ORIGIN
        resp = client.post(
            "/v1/chat/completions",
            headers={"Origin": PAGE_ASSIST_ORIGIN, "Authorization": "Bearer dummy"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == PAGE_ASSIST_ORIGIN


def test_check_bindable_reports_port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        with pytest.raises(RuntimeError, match="待ち受けできません"):
            check_bindable("127.0.0.1", port)
