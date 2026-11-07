"""Access is the tailnet. There is no token.

The WebSocket still checks Origin. A page the browser visits can open a socket
to a host the phone can reach, and Tailscale does not stop that.
"""
import re

import pytest

from conftest import tmux


@pytest.mark.parametrize("path", ["/api/sessions", "/api/projects", "/api/launchers"])
def test_api_needs_no_token(client, path):
    assert client.get(path).status_code == 200


def test_writes_need_no_token(client):
    """Create and delete answer with validation errors, not 401."""
    assert client.post("/api/sessions", json={"name": "-bad"}).status_code == 400
    assert client.delete("/api/sessions/gdtest-no-such").status_code == 404


def test_ws_needs_no_token(client, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(f'{{"session": "{name}"}}')
        assert ws.receive_bytes()


def test_ws_rejects_cross_origin(client, session_name):
    """A page on evil.example must not be able to dial the terminal."""
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/term", headers={"Origin": "https://evil.example"}
        ) as ws:
            ws.send_text(f'{{"session": "{name}"}}')
            ws.receive_bytes()


def test_ws_allows_same_origin(client, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect(
        "/ws/term", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_text(f'{{"session": "{name}"}}')
        assert ws.receive_bytes()


def test_ws_allows_missing_origin(client, session_name):
    """Non-browser clients (curl, scripts) send no Origin and stay supported."""
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(f'{{"session": "{name}"}}')
        assert ws.receive_bytes()


def test_static_and_index_are_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/xterm.css").status_code == 200


def test_index_does_not_ask_for_a_token(client):
    page = client.get("/").text
    assert "Access token" not in page
    assert not re.search(r"\w+_token\b", page)  # no token kept in storage
    assert "Forget token" not in page
