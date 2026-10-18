"""Shared test fixtures.

Every test runs against a temporary data folder (CLIPMAKER_DATA_DIR) with the
background threads off, and with outbound network access blocked:

* httpx requests go to fakes registered on the `net` fixture by host; an
  unregistered host fails the test, except the local Ollama port, which looks
  like Ollama is not running;
* raw socket connections to anything but loopback are refused;
* DNS lookups of real host names fail.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Callable

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Set before any app module is imported, so nothing can resolve the real data
# folder even at import time. Each test then gets its own folder (see below).
os.environ["CLIPMAKER_DATA_DIR"] = tempfile.mkdtemp(prefix="clipmaker-tests-")
os.environ["CLIPMAKER_NO_BACKGROUND"] = "1"

OLLAMA_PORT = 11434
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    return host in _LOOPBACK_NAMES or host.startswith("127.")


class FakeNet:
    """Routes httpx requests to fake handlers registered by host (or host:port)."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def register(self, host: str,
                 handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handlers[host.lower()] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = (request.url.host or "").lower()
        port = request.url.port
        handler = self.handlers.get(f"{host}:{port}") if port else None
        handler = handler or self.handlers.get(host)
        if handler is not None:
            return handler(request)
        if _is_loopback(host) and port == OLLAMA_PORT:
            raise httpx.ConnectError("Ollama is not running (test default)",
                                     request=request)
        raise AssertionError(
            f"Unregistered outbound request in a test: {request.method} {request.url}")


class FakeLLM:
    """Stands in for llm.chat. Queue replies (strings, or exceptions to raise),
    or set `responder(prompt, system)`. Like the real chat(), each call counts
    toward llm.count_calls() and a successful reply records `model` as the
    model that answered."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.replies: list = []
        self.responder: Callable[[str, str], str] | None = None
        self.model = "fake-model"

    def queue(self, *replies) -> None:
        self.replies.extend(replies)

    def __call__(self, prompt: str, system: str = "", **kwargs) -> str:
        import llm

        llm.count_call()
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        if self.replies:
            reply = self.replies.pop(0)
        elif self.responder is not None:
            reply = self.responder(prompt, system)
        else:
            raise AssertionError("fake_llm has no reply queued for this call")
        if isinstance(reply, BaseException):
            raise reply
        llm._req.last_model = self.model
        return reply


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CLIPMAKER_DATA_DIR", str(data))
    monkeypatch.setenv("CLIPMAKER_NO_BACKGROUND", "1")
    for var in ("CLIPMAKER_AUTH", "CLIPMAKER_SIGNUP", "KIMCHI_API_KEY",
                "CLIPMAKER_ALLOW_ANY_SITE"):
        monkeypatch.delenv(var, raising=False)
    import paths
    assert data.resolve() in paths.db_path().parents, "database must be in the test folder"
    saved_umask = os.umask(0o022)        # bootstrap() tightens it; restore afterwards
    os.umask(saved_umask)
    try:
        yield data
    finally:
        os.umask(saved_umask)


@pytest.fixture(autouse=True)
def net(monkeypatch):
    fake = FakeNet()

    def handle_request(self, request):
        return fake.handle(request)

    async def handle_async_request(self, request):
        return fake.handle(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        handle_async_request)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _allowed(sock, address) -> bool:
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return True
        host = address[0] if isinstance(address, tuple) else address
        return _is_loopback(host)

    def connect(sock, address):
        if not _allowed(sock, address):
            raise OSError(f"Network access is disabled in tests (tried {address!r})")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if not _allowed(sock, address):
            raise OSError(f"Network access is disabled in tests (tried {address!r})")
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is None or _is_loopback(host):
            return real_getaddrinfo(host, *args, **kwargs)
        try:                                   # IP literals resolve locally
            socket.inet_pton(socket.AF_INET6 if ":" in str(host) else socket.AF_INET,
                             str(host))
            return real_getaddrinfo(host, *args, **kwargs)
        except OSError:
            raise socket.gaierror(f"DNS is disabled in tests (looked up {host!r})")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return fake


@pytest.fixture(autouse=True)
def _reset_llm_state():
    """Clear llm's remembered model and per-thread state, so one test's AI
    calls never change what the next test sees."""
    import llm

    llm._last_ok_model = None
    for attr in ("last_model", "cfg", "counters"):
        if hasattr(llm._req, attr):
            delattr(llm._req, attr)
    yield


@pytest.fixture
def fake_llm(monkeypatch):
    import llm

    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    return fake


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def two_users(monkeypatch):
    """Multi-user mode with two signed-in users; yields (alice, bob) clients.

    Each client carries its own session cookie, so isolation tests can check
    both directions.
    """
    from fastapi.testclient import TestClient

    import app as app_module

    monkeypatch.setenv("CLIPMAKER_AUTH", "1")
    monkeypatch.setenv("CLIPMAKER_SIGNUP", "open")
    with TestClient(app_module.app) as alice, TestClient(app_module.app) as bob:
        for c, email in ((alice, "alice@example.com"), (bob, "bob@example.com")):
            r = c.post("/api/auth/signup", json={"email": email, "password": "password123"})
            assert r.status_code == 200, r.text
        yield alice, bob
