"""RequestGate: Host allowlist (DNS rebinding), the public-listener guard,
cross-origin writes, and the page's CSP.

The rebinding case was live before this existed: `Host: evil.example:3320`
with a matching Origin got `101 Switching Protocols` from the real listener,
because the Origin check compared two headers the attacker sets.
"""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import live_sessions, load_app, tmux


def _app_module():
    import app
    return app


@pytest.fixture
def evil(projects_root):
    """A browser that resolved evil.example to this box."""
    return TestClient(load_app(projects_root=projects_root), base_url="http://evil.example:3320")


@pytest.mark.parametrize("path", ["/", "/api/sessions", "/health", "/static/xterm.css"])
def test_rebound_host_is_refused(evil, path):
    assert evil.get(path).status_code == 403


def test_rebound_host_cannot_open_the_terminal(evil, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with pytest.raises(Exception):
        with evil.websocket_connect(
            "/ws/term", headers={"Origin": "http://evil.example:3320"}
        ) as ws:
            ws.send_text(f'{{"session": "{name}"}}')
            ws.receive_bytes()


def test_rebound_host_cannot_create(evil, session_name):
    name = session_name()
    r = evil.post("/api/sessions", json={"name": name, "launcher": "shell"},
                  headers={"Origin": "http://evil.example:3320"})
    assert r.status_code == 403
    assert name not in live_sessions()


@pytest.mark.parametrize("host,ok", [
    ("100.64.0.7:3320", True),
    ("127.0.0.1", True),
    ("[fd7a:115c:a1e0::1]:3320", True),
    ("localhost:3320", True),
    ("testserver", True),           # COUCHDECK_HOSTS in conftest
    ("TESTSERVER.", True),          # case and trailing dot
    ("evil.example", False),
    ("evil.example:3320", False),
    ("100.64.0.7:80 x", False),   # junk port
    ("", False),
    (None, False),
])
def test_host_ok(host, ok):
    assert _app_module().host_ok(host) is ok


def test_tailscale_name_is_allowed(monkeypatch):
    app = _app_module()
    monkeypatch.setattr(app, "_tailscale_name", lambda: "box.tail123.ts.net")
    assert app.host_ok("box.tail123.ts.net:3443")
    assert not app.host_ok("other.tail123.ts.net:3443")


@pytest.mark.parametrize("server,ok", [
    (("100.64.0.7", 3320), True),   # Tailscale CGNAT
    (("127.0.0.1", 3320), True),
    (("192.168.1.5", 3320), True),
    (("fd7a:115c:a1e0::1", 3320), True),
    (("8.8.8.8", 3320), False),
    (("2001:4860:4860::8888", 3320), False),
    (None, True),                     # Unix socket
])
def test_listener_ok(server, ok):
    assert _app_module().listener_ok(server) is ok


def test_public_listener_can_be_allowed(monkeypatch):
    app = _app_module()
    monkeypatch.setattr(app, "ALLOW_PUBLIC", True)
    assert app.listener_ok(("8.8.8.8", 3320))


def test_cross_origin_write_is_refused(client, session_name):
    """A form on another site can POST without a preflight."""
    name = session_name()
    r = client.post("/api/sessions", json={"name": name, "launcher": "shell"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post(f"/api/sessions/{name}/shell", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.delete("/api/sessions/x", headers={"Origin": "https://evil.example"}).status_code == 403
    assert name not in live_sessions()


def test_same_origin_write_passes(client, session_name):
    name = session_name()
    r = client.post("/api/sessions", json={"name": name, "launcher": "shell"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text


def test_page_has_csp_with_a_fresh_nonce(client):
    a, b = client.get("/"), client.get("/")
    csp = a.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    scripts = re.findall(r"<script\b[^>]*>", a.text)
    assert scripts and all(f'nonce="{nonce}"' in s for s in scripts)
    assert nonce not in b.headers["content-security-policy"], "nonce must not repeat"


def test_security_headers_everywhere(client):
    for path in ("/", "/api/sessions", "/static/xterm.css"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"


def test_index_is_not_served_from_static(client):
    """/static/index.html would skip the CSP and the no-store."""
    assert client.get("/static/index.html").status_code == 404
