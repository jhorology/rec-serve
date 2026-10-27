"""Reverse proxy in front of `rec serve` with macOS sleep-prevention keepalive.

`rec serve` は 127.0.0.1 にしかバインドできないため、このプロキシが任意の
インターフェース (Tailscale IP など) で待ち受けて転送する。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from rich.console import Console
from rich.text import Text

from rec_serve import __version__
from rec_serve.keepalive import KeepAliveManager

console = Console(soft_wrap=True)

DEFAULT_PUBLIC_PORT = 11501
DEFAULT_BACKEND_PORT = 11500
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def read_api_key(rec_bin: str) -> str:
    """rec のローカル API キーを取得 (`rec auth token`)。"""
    res = subprocess.run(
        [rec_bin, "auth", "token"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "NO_COLOR": "1"},
    )
    key = res.stdout.strip()
    if res.returncode != 0 or not key:
        raise RuntimeError(f"rec auth token failed: {res.stderr.strip()}")
    return key


async def wait_for_url(url: str, headers: dict[str, str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"backend not ready: {url}")
            await asyncio.sleep(0.5)


def _stop_rec_process(proc: subprocess.Popen[bytes]) -> None:
    """rec serve 子プロセスを終了 (terminate → wait → kill)。

    rec serve は SIGTERM で配下の _supervise / rec-mlx も終了させる。
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def create_app(
    *,
    backend_base: str,
    api_key: str,
    keepalive_timeout: float = 3600.0,
    enable_keepalive: bool = True,
    rec_process: subprocess.Popen[bytes] | None = None,
) -> FastAPI:
    keepalive = KeepAliveManager(
        timeout_sec=keepalive_timeout, enabled=enable_keepalive
    )
    auth_header = {"authorization": f"Bearer {api_key}"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(
            base_url=backend_base.rstrip("/"),
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        )
        app.state.http = client
        keepalive.acquire()
        monitor_task = asyncio.create_task(keepalive.monitor_loop(check_interval=30.0))
        try:
            yield
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            keepalive.release()
            await client.aclose()
            if rec_process is not None:
                _stop_rec_process(rec_process)

    app = FastAPI(title="rec-serve", version=__version__, lifespan=lifespan)
    app.state.keepalive = keepalive
    app.state.backend_base = backend_base
    app.state.rec_process = rec_process

    @app.middleware("http")
    async def keepalive_middleware(request: Request, call_next):
        # ヘルスチェック以外のリクエストで keepalive (スリープ抑制) を更新
        if request.url.path not in ("/", "/health"):
            keepalive.touch()
        return await call_next(request)

    @app.get("/")
    @app.get("/health")
    async def health():
        upstream: dict[str, Any] = {}
        upstream_ok = False
        try:
            client: httpx.AsyncClient = app.state.http
            resp = await client.get("/health", headers=auth_header)
            upstream_ok = resp.status_code == 200
            try:
                upstream = resp.json()
            except Exception:
                upstream = {"raw": resp.text}
        except Exception as e:
            upstream = {"error": str(e)}

        rec_running = rec_process is None or rec_process.poll() is None
        status = "healthy" if upstream_ok and rec_running else "degraded"
        return {
            "status": status,
            "service": "rec-serve",
            "backend": backend_base,
            "upstream": upstream,
            "rec_running": rec_running,
            "keepalive": keepalive.status(),
        }

    async def _proxy(request: Request, path: str) -> Response:
        client: httpx.AsyncClient = app.state.http
        target = "/" + path.lstrip("/")
        if request.url.query:
            target = f"{target}?{request.url.query}"

        # Clients may send any (or no) API key; the proxy injects rec's local key.
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "authorization"
        }
        headers.update(auth_header)
        body = await request.body()

        req = client.build_request(
            request.method,
            target,
            headers=headers,
            content=body if body else None,
        )
        upstream = await client.send(req, stream=True)

        resp_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
        }

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_all(path: str, request: Request):
        # / and /health are handled above; this catches everything else including
        # /v1/models and /v1/chat/completions (streaming).
        try:
            return await _proxy(request, path)
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=502,
                content={"error": "upstream_unavailable", "detail": str(e)},
            )

    return app


def start_rec_process(
    *,
    port: int,
    rec_bin: str,
    context: int | None = None,
) -> subprocess.Popen[bytes]:
    cmd = [rec_bin, "serve", "--port", str(port)]
    if context:
        cmd += ["--context", str(context)]
    console.print(f"[cyan]>> rec serve 起動: {' '.join(cmd)}[/cyan]")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "NO_COLOR": "1"},
    )


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PUBLIC_PORT,
    backend_port: int = DEFAULT_BACKEND_PORT,
    rec_bin: str = "rec",
    context: int | None = None,
    load_timeout: float = 300.0,
    keepalive_timeout: float = 3600.0,
    enable_keepalive: bool = True,
) -> None:
    backend_host = "127.0.0.1"
    if backend_port == port and host in (backend_host, "0.0.0.0"):
        raise ValueError("backend_port must differ from public port")
    if port_in_use(backend_port):
        # 手動の `rec serve` / `rec launch` と競合している。
        raise RuntimeError(
            f"127.0.0.1:{backend_port} は既に使用中です。`rec stop` で停止してください"
        )

    api_key = read_api_key(rec_bin)
    rec_proc = start_rec_process(port=backend_port, rec_bin=rec_bin, context=context)
    backend_base = f"http://{backend_host}:{backend_port}"

    # Drain rec stdout in the background so the pipe does not fill up.
    # Must start BEFORE the readiness wait to avoid deadlocking rec.
    # rec の /health は読み込み途中 (生成チェック中) から 200 を返すため、
    # 「<model> is ready.」の出力も待つ。
    loaded = threading.Event()

    def _drain() -> None:
        if not rec_proc.stdout:
            return
        for line in iter(rec_proc.stdout.readline, b""):
            try:
                text = _ANSI_RE.sub("", line.decode(errors="replace")).rstrip()
            except Exception:
                continue
            if text:
                console.print(Text.assemble(("rec ", "dim"), text), soft_wrap=True)
            if "is ready." in text:
                loaded.set()

    threading.Thread(target=_drain, name="rec-serve-drain", daemon=True).start()

    async def _ready() -> None:
        deadline = time.monotonic() + load_timeout
        await wait_for_url(
            f"{backend_base}/health",
            {"authorization": f"Bearer {api_key}"},
            timeout=load_timeout,
        )
        while not loaded.is_set() and time.monotonic() < deadline:
            if rec_proc.poll() is not None:
                raise RuntimeError("rec serve exited")
            await asyncio.sleep(0.2)
        if not loaded.is_set():
            # /health は応答しているので出力形式の変化とみなして続行
            console.print("[yellow]⚠ rec の ready 表示を確認できませんでした (続行)[/yellow]")

    def _on_sigterm(signum, frame):
        raise SystemExit(128 + signum)

    # 読み込み中に launchd から SIGTERM を受けても rec を残さない
    # (以降は uvicorn がシグナルを処理する)。
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        asyncio.run(_ready())
    except BaseException:
        _stop_rec_process(rec_proc)
        raise RuntimeError(f"rec serve failed to become healthy on {backend_base}") from None

    console.print(
        f"[green]✓ rec serve ready[/green]  backend={backend_base}  "
        f"public=http://{host}:{port}"
    )

    app = create_app(
        backend_base=backend_base,
        api_key=api_key,
        keepalive_timeout=keepalive_timeout,
        enable_keepalive=enable_keepalive,
        rec_process=rec_proc,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    monitor_stop = threading.Event()

    def _monitor_backend() -> None:
        while not monitor_stop.wait(1):
            if rec_proc.poll() is not None:
                console.print("[red]rec serve が終了しました。プロキシを停止します。[/red]")
                server.should_exit = True  # launchd KeepAlive restarts both processes.
                return

    monitor = threading.Thread(target=_monitor_backend, daemon=True)
    monitor.start()
    try:
        server.run()
    finally:
        monitor_stop.set()
        monitor.join(timeout=2)
        # lifespan が実行されない場合 (例: ポートバインド失敗) の子プロセス掃除
        _stop_rec_process(rec_proc)
