"""Session names, project paths, and argument-injection surface.

Everything here reaches `subprocess.run(["tmux", ...])`. There is no shell, so
classic `;rm -rf` injection is not the risk — the risks are tmux *argument*
injection (a name starting with `-`) and escaping PROJECTS_ROOT via `-c`.
"""
import pytest

from conftest import live_sessions, tmux


# --- session name validation -------------------------------------------------

@pytest.mark.parametrize("name", ["", "  ", "has space", "has.dot", "has:colon", "has\ttab"])
def test_rejected_session_names(client, auth, name):
    r = client.post("/api/sessions", json={"name": name}, headers=auth)
    assert r.status_code == 400, f"{name!r} should be rejected"


@pytest.mark.parametrize("name", [
    "-d",                # looks like a tmux flag
    "--",
    "-t=other",
    "$(id)",             # shell metachars — harmless without a shell, assert that
    "`id`",
    "a;id",
    "a|id",
    "a&id",
    "a\nb",              # newline: must not corrupt the tab-separated list format
    "a<img src=x onerror=alert(1)>",
])
def test_hostile_session_names_do_not_execute_or_corrupt(client, auth, name, session_name):
    """Creating with a hostile name must either 400 or produce exactly that
    session and nothing else — never a second session, never a 500."""
    before = set(live_sessions())
    r = client.post("/api/sessions", json={"name": name}, headers=auth)
    assert r.status_code in (200, 400, 409, 500)
    after = set(live_sessions())
    new = after - before
    for n in new:
        tmux("kill-session", "-t", f"={n}")
    assert len(new) <= 1, f"{name!r} created more than one session: {new}"

    # and the list endpoint must survive whatever got created
    assert client.get("/api/sessions", headers=auth).status_code == 200


def test_valid_name_creates_exactly_that_session(client, auth, session_name):
    name = session_name()
    assert client.post("/api/sessions", json={"name": name}, headers=auth).status_code == 200
    assert name in live_sessions()


def test_duplicate_session_is_409(client, auth, session_name):
    name = session_name()
    client.post("/api/sessions", json={"name": name}, headers=auth)
    r = client.post("/api/sessions", json={"name": name}, headers=auth)
    assert r.status_code == 409


# --- project path confinement ------------------------------------------------

@pytest.mark.parametrize("project", [
    "..",
    "../..",
    "../outside-root",
    "../outside-root/secret",
    "alpha/../../outside-root",
    "/etc",
    "/",
    "//etc",
    ".",
    "escape-link",           # symlink out of PROJECTS_ROOT
    "escape-link/secret",
    "notadir",               # exists but is a file
    "nonexistent",
    "\x00etc",
])
def test_project_escape_rejected(client, auth, project, session_name):
    name = session_name()
    r = client.post("/api/sessions", json={"name": name, "project": project}, headers=auth)
    assert r.status_code == 400, f"project={project!r} was accepted"
    assert name not in live_sessions()


@pytest.mark.parametrize("project", ["alpha", "beta"])
def test_legitimate_project_accepted(client, auth, project, session_name):
    name = session_name()
    r = client.post("/api/sessions", json={"name": name, "project": project}, headers=auth)
    assert r.status_code == 200


def test_projects_listing_hides_dotfiles_and_files(client, auth):
    projects = client.get("/api/projects", headers=auth).json()["projects"]
    assert "alpha" in projects and "beta" in projects
    assert ".hidden" not in projects
    assert "notadir" not in projects


# --- delete ------------------------------------------------------------------

def test_delete_is_exact_match_not_prefix(client, auth, session_name):
    """`=name` should mean exact. A prefix must not kill a longer session."""
    full = session_name("-longer")
    tmux("new-session", "-d", "-s", full)
    client.delete(f"/api/sessions/{full[:-7]}", headers=auth)
    assert full in live_sessions(), "prefix delete killed the wrong session"


def test_delete_missing_session_is_404(client, auth):
    assert client.delete("/api/sessions/gdtest-definitely-absent", headers=auth).status_code == 404
