"""The launch command must actually reach the pane.

Regression: `send-keys -t =name` looks right next to every other tmux call in
this app, but send-keys takes a *pane* target and `=name` is not one. tmux
answered "can't find pane" on stderr, the return code was never checked, and
every session created from the phone quietly opened at a bare shell instead of
the agent. Nothing in the suite noticed, because nothing asserted on the pane.
"""
import time

import pytest

from conftest import live_sessions, tmux

@pytest.fixture
def allow_custom(client, monkeypatch):
    """Patch the *live* app module the TestClient is serving.

    A top-level `import app` would pin the pre-reload module; `client` reloads
    `app` via load_app(), so we resolve it from sys.modules after that.
    """
    import sys
    monkeypatch.setattr(sys.modules["app"], "ALLOW_CUSTOM_CMD", True)




def pane_text(name):
    return tmux("capture-pane", "-p", "-t", f"={name}:").stdout


def wait_for(name, needle, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in pane_text(name):
            return True
        time.sleep(0.2)
    return False


def test_command_actually_runs_in_the_pane(client, auth, session_name, allow_custom):
    name = session_name()
    r = client.post(
        "/api/sessions",
        json={"name": name, "command": "echo GDMARKER-RAN"},
        headers=auth,
    )
    assert r.status_code == 200
    assert wait_for(name, "GDMARKER-RAN"), (
        f"command never reached the pane; pane was:\n{pane_text(name)}"
    )


def test_launcher_command_reaches_the_pane(client, auth, session_name):
    """Same path, but via a launcher preset rather than free text."""
    import app
    app.LAUNCHERS.append({"id": "gdtest-echo", "label": "t", "template": "echo GDMARKER-LAUNCHER"})
    try:
        name = session_name()
        r = client.post("/api/sessions", json={"name": name, "launcher": "gdtest-echo"}, headers=auth)
        assert r.status_code == 200
        assert wait_for(name, "GDMARKER-LAUNCHER"), pane_text(name)
    finally:
        app.LAUNCHERS[:] = [l for l in app.LAUNCHERS if l["id"] != "gdtest-echo"]


def test_send_keys_target_is_exact_match(client, auth, session_name, allow_custom):
    """`=name:` must not fall through to a longer session, the way a bare
    prefix target would."""
    longer = session_name("-longer")
    tmux("new-session", "-d", "-s", longer)
    short = longer[: -len("-longer")]
    # the short name does not exist; creating it must not type into `longer`
    r = client.post(
        "/api/sessions",
        json={"name": short, "command": "echo GDMARKER-WRONG-SESSION"},
        headers=auth,
    )
    assert r.status_code == 200
    # wait for the keys to land somewhere before asserting where; a fixed sleep
    # is not enough once the suite has the box busy
    assert wait_for(short, "GDMARKER-WRONG-SESSION"), pane_text(short)
    assert "GDMARKER-WRONG-SESSION" not in pane_text(longer), "keys landed in the wrong session"


def test_shell_launcher_leaves_the_pane_alone(client, auth, session_name):
    name = session_name()
    assert client.post(
        "/api/sessions", json={"name": name, "launcher": "shell"}, headers=auth
    ).status_code == 200
    assert name in live_sessions()
