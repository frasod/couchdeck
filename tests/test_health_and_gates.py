"""Health endpoint, custom-command gate, and send-keys failure cleanup."""
from conftest import live_sessions, load_app, tmux


def test_health_reports_ok_and_tmux(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("tmux") is True


def test_free_text_command_rejected_by_default(client, auth, session_name):
    name = session_name()
    r = client.post(
        "/api/sessions",
        json={"name": name, "command": "echo SHOULD-NOT-RUN"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "COUCHDECK_ALLOW_CUSTOM_CMD" in r.json()["detail"]
    assert name not in live_sessions()


def test_free_text_command_allowed_when_flag_on(projects_root, session_name):
    from fastapi.testclient import TestClient
    c = TestClient(load_app(projects_root=projects_root, allow_custom_cmd=True))
    name = session_name()
    r = c.post("/api/sessions", json={"name": name, "command": "true"})
    assert r.status_code == 200, r.text
    assert name in live_sessions()


def test_send_keys_failure_kills_the_new_session(client, auth, session_name, monkeypatch):
    import sys
    name = session_name()
    mod = sys.modules["app"]
    real_tmux = mod.tmux

    def boom(*args):
        if args and args[0] == "send-keys":
            class R:
                returncode = 1
                stderr = "send-keys exploded\n"
                stdout = ""
            return R()
        return real_tmux(*args)

    monkeypatch.setattr(mod, "tmux", boom)
    monkeypatch.setattr(mod, "ALLOW_CUSTOM_CMD", True)
    r = client.post(
        "/api/sessions",
        json={"name": name, "command": "echo hi"},
        headers=auth,
    )
    assert r.status_code == 500
    assert "exploded" in r.json()["detail"]
    assert name not in live_sessions()
