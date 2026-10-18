"""The test harness keeps every test on temporary data and off the network."""
from __future__ import annotations

import socket

import httpx
import pytest


@pytest.mark.req("N7.1")
def test_jobs_come_from_the_temporary_database(client, _isolated_data):
    import paths

    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert response.json() == {"jobs": []}
    assert paths.db_path().exists()
    assert _isolated_data.resolve() in paths.db_path().parents


@pytest.mark.req("N7.1")
def test_unregistered_http_request_fails_the_test():
    with pytest.raises(AssertionError, match="Unregistered outbound request"):
        httpx.get("https://example.com/")


@pytest.mark.req("N7.1")
def test_registered_fake_answers(net):
    net.register("api.example.test",
                 lambda request: httpx.Response(200, json={"ok": True}))
    assert httpx.get("https://api.example.test/ping").json() == {"ok": True}
    assert net.requests[-1].url.path == "/ping"


@pytest.mark.req("N7.1")
def test_ollama_looks_absent_by_default():
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://localhost:11434/api/tags")


@pytest.mark.req("N7.1")
def test_socket_to_public_address_is_refused():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="disabled in tests"):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


@pytest.mark.req("N7.1")
def test_dns_lookup_of_real_host_is_refused():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("www.youtube.com", 443)


@pytest.mark.req("N7.1")
def test_fake_llm_replaces_the_ai_engine(fake_llm):
    import llm

    fake_llm.queue('{"ok": true}')
    assert llm.is_configured()
    assert llm.chat("hello", system="sys") == '{"ok": true}'
    assert fake_llm.calls[0]["prompt"] == "hello"
