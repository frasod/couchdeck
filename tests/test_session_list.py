"""The deck view needs more than a name per session, and must not 500 on a
session name that breaks the tab-separated format.
"""
from conftest import live_sessions, tmux


def test_session_fields(client, auth, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    sessions = client.get("/api/sessions", headers=auth).json()["sessions"]
    s = next(x for x in sessions if x["name"] == name)
    for field in ("name", "created", "attached", "activity", "command", "path"):
        assert field in s, f"missing {field}"
    assert isinstance(s["activity"], int)
    assert s["attached"] is False
    assert s["path"].startswith("/"), "active pane path should be an absolute dir"


def test_pane_command_reported(client, auth, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    sessions = client.get("/api/sessions", headers=auth).json()["sessions"]
    s = next(x for x in sessions if x["name"] == name)
    assert s["command"], "a fresh session runs a shell; command should not be blank"


def test_list_survives_a_name_with_a_tab(client, auth):
    """A tab in a session name splits the -F output into the wrong number of
    fields. The old code unpacked into exactly three names and raised, turning
    any session on the box into a 500 for the whole list."""
    name = "gdtest-tab\there"
    tmux("new-session", "-d", "-s", name)
    try:
        r = client.get("/api/sessions", headers=auth)
        assert r.status_code == 200
    finally:
        for n in live_sessions():
            if n.startswith("gdtest-tab"):
                tmux("kill-session", "-t", f"={n}")
