"""COUCHDECK_TAILSCALE_USERS: only named Tailscale logins, only via serve.

What `tailscale serve` does was checked against a real tailscaled (1.104)
before this was written: it replaced a forged Tailscale-User-Login with the
real login, dropped a forged Tailscale-Funnel-Request, and connected to a
listener on the Tailscale IP from that same IP. A direct connection kept the
forged header, which is why the header only counts from this machine.
"""
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import live_sessions, load_app, tmux

ME = "me@example.com"
SERVE = ("100.64.0.7", 41000)          # serve connects from the listener's own IP
PHONE = ("100.64.0.9", 41000)          # another tailnet device, direct
BASE = "http://100.64.0.7:3320"
WS = "ws://100.64.0.7:3320/ws/term"


@pytest.fixture
def make(projects_root, monkeypatch):
    app = load_app(projects_root=projects_root)
    monkeypatch.setattr(sys.modules["app"], "TAILSCALE_USERS", {ME})

    def make(client):
        return TestClient(app, base_url=BASE, client=client)
    return make


def test_off_by_default(projects_root):
    load_app(projects_root=projects_root)
    assert sys.modules["app"].TAILSCALE_USERS == set()


def test_allowed_login_through_serve_gets_in(make):
    r = make(SERVE).get("/api/sessions", headers={"Tailscale-User-Login": ME})
    assert r.status_code == 200, r.text


def test_login_is_case_insensitive(make):
    r = make(SERVE).get("/", headers={"Tailscale-User-Login": "Me@Example.COM"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/", "/api/sessions", "/static/xterm.css", "/health"])
def test_other_login_is_refused(make, path):
    r = make(SERVE).get(path, headers={"Tailscale-User-Login": "guest@example.com"})
    assert r.status_code == 403
    assert "not allowed" in r.text


def test_no_login_is_refused(make):
    """Funnel and tagged nodes: serve sends no login at all."""
    r = make(SERVE).get("/", headers={"Tailscale-Funnel-Request": "?1"})
    assert r.status_code == 403


def test_forged_header_on_a_direct_connection_is_refused(make):
    r = make(PHONE).get("/", headers={"Tailscale-User-Login": ME})
    assert r.status_code == 403
    assert "direct connections" in r.text


def test_loopback_proxy_is_trusted(projects_root, monkeypatch):
    app = load_app(projects_root=projects_root)
    monkeypatch.setattr(sys.modules["app"], "TAILSCALE_USERS", {ME})
    c = TestClient(app, base_url="http://127.0.0.1:3320", client=("127.0.0.1", 41000))
    assert c.get("/", headers={"Tailscale-User-Login": ME}).status_code == 200
    assert c.get("/").status_code == 403


def test_forged_header_cannot_open_the_terminal(make, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with pytest.raises(Exception):
        with make(PHONE).websocket_connect(
            WS, headers={"Origin": BASE, "Tailscale-User-Login": ME}
        ) as ws:
            ws.send_text(f'{{"session": "{name}"}}')
            ws.receive_bytes()


def test_forged_header_cannot_create(make, session_name):
    name = session_name()
    r = make(PHONE).post("/api/sessions", json={"name": name, "launcher": "shell"},
                         headers={"Origin": BASE, "Tailscale-User-Login": ME})
    assert r.status_code == 403
    assert name not in live_sessions()


def test_allowed_login_can_open_the_terminal(make, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with make(SERVE).websocket_connect(
        WS, headers={"Origin": BASE, "Tailscale-User-Login": ME}
    ) as ws:
        ws.send_text(f'{{"session": "{name}", "cols": 80, "rows": 24}}')
        assert ws.receive_bytes()


@pytest.mark.parametrize("client,server,ok", [
    (("127.0.0.1", 5), ("100.64.0.7", 3320), True),
    (("::1", 5), ("::1", 3320), True),
    (("100.64.0.7", 5), ("100.64.0.7", 3320), True),
    (("100.64.0.9", 5), ("100.64.0.7", 3320), False),
    (("192.168.1.5", 5), ("100.64.0.7", 3320), False),
    (None, None, True),                       # Unix socket
    (("testclient", 5), ("100.64.0.7", 3320), False),
])
def test_from_this_machine(client, server, ok):
    import app
    assert app.from_this_machine(client, server) is ok
