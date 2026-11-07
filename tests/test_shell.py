"""An empty shell next to an agent session, for commands the agent cannot run.

The command is identified from a permission question and handed back for the
compose box. Opening the shell must not type it, and must not run it.
"""
import time
from pathlib import Path

from app import blocked_command
from conftest import live_sessions, tmux


def pane_text(name):
    return tmux("capture-pane", "-p", "-t", f"={name}:").stdout


def _permission(body: str) -> str:
    return (
        "Ready.\n"
        "╭──────────────────────────────────────╮\n"
        f"│ {body:<38}│\n"
        "│ ❯ 1. Yes                             │\n"
        "│   2. No                              │\n"
        "╰──────────────────────────────────────╯\n"
        "  Esc to cancel\n"
    )


def test_backtick_command_inside_a_permission_question():
    screen = _permission("Run `git push --force`?")
    assert blocked_command(screen) == "git push --force"


def test_bare_command_line_inside_the_question():
    screen = _permission("git push --force")
    assert blocked_command(screen) == "git push --force"


def test_command_just_above_the_choices():
    screen = (
        "Bash command\n"
        "\n"
        "git push\n"
        "╭────────────────────╮\n"
        "│ Do you want to proceed? │\n"
        "│ ❯ 1. Yes           │\n"
        "│   2. No            │\n"
        "╰────────────────────╯\n"
    )
    assert blocked_command(screen) == "git push"


def test_ordinary_agent_text_is_not_a_command():
    screen = (
        "I rewrote the parser and the suite is green.\n"
        "╭──────────────────────────────────────╮\n"
        "│ ❯                                    │\n"
        "╰──────────────────────────────────────╯\n"
    )
    assert blocked_command(screen) is None


def test_a_shell_prompt_is_not_a_permission_question():
    assert blocked_command("$ ls\nREADME.md\n$ \n") is None


def test_backticks_outside_a_question_are_left_alone():
    screen = "Use `git push --force` only if you mean it.\n"
    assert blocked_command(screen) is None


def test_a_question_with_no_command_copies_nothing():
    screen = _permission("Run the migration?")
    assert blocked_command(screen) is None


def test_prose_above_the_choices_is_not_the_command():
    screen = (
        "I think we should ship it today.\n"
        "╭──────────────────────────────────────╮\n"
        "│ ❯ 1. Yes                             │\n"
        "│   2. No                              │\n"
        "╰──────────────────────────────────────╯\n"
    )
    assert blocked_command(screen) is None


def test_shell_opens_in_the_session_directory(client, auth, session_name, projects_root):
    name = session_name()
    cwd = projects_root / "alpha"
    assert tmux("new-session", "-d", "-s", name, "-c", str(cwd)).returncode == 0
    r = client.post(f"/api/sessions/{name}/shell", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"].startswith(name)
    assert body["name"].endswith("-sh")
    assert body["pending"] is None
    path = tmux("list-panes", "-t", f"={body['name']}", "-F", "#{pane_current_path}").stdout.strip()
    assert Path(path).resolve() == cwd.resolve()


def test_opening_a_shell_does_not_run_the_pending_command(client, auth, session_name, projects_root, monkeypatch):
    import app as app_module
    name = session_name()
    cwd = projects_root / "alpha"
    assert tmux("new-session", "-d", "-s", name, "-c", str(cwd)).returncode == 0
    monkeypatch.setattr(app_module, "blocked_command", lambda screen: "echo GDUNIQUE-RAN")
    r = client.post(f"/api/sessions/{name}/shell", headers=auth)
    assert r.status_code == 200
    shell = r.json()["name"]
    assert r.json()["pending"] == "echo GDUNIQUE-RAN"
    time.sleep(0.4)
    text = pane_text(shell)
    assert "GDUNIQUE-RAN" not in text, f"the command was typed or run:\n{text}"


def test_second_shell_gets_a_distinct_name(client, auth, session_name, projects_root):
    name = session_name()
    assert tmux("new-session", "-d", "-s", name, "-c", str(projects_root / "alpha")).returncode == 0
    first = client.post(f"/api/sessions/{name}/shell", headers=auth).json()["name"]
    second = client.post(f"/api/sessions/{name}/shell", headers=auth).json()["name"]
    assert first != second
    assert second.endswith("-sh2")
    assert {first, second} <= set(live_sessions())


def test_missing_session_opens_nothing(client, auth):
    before = set(live_sessions())
    r = client.post("/api/sessions/gdtest-missing/shell", headers=auth)
    assert r.status_code == 404
    assert set(live_sessions()) == before


def test_shell_needs_no_token(client, session_name, projects_root):
    name = session_name()
    tmux("new-session", "-d", "-s", name, "-c", str(projects_root / "alpha"))
    r = client.post(f"/api/sessions/{name}/shell")
    assert r.status_code == 200
    assert r.json()["name"] in live_sessions()


def test_symlink_outside_the_projects_root_is_refused(client, auth, session_name, projects_root):
    name = session_name()
    link = projects_root / "escape-link"
    assert tmux("new-session", "-d", "-s", name, "-c", str(link)).returncode == 0
    before = set(live_sessions())
    r = client.post(f"/api/sessions/{name}/shell", headers=auth)
    assert r.status_code == 400
    assert set(live_sessions()) == before


def test_session_list_reports_pending(client, auth, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    sessions = client.get("/api/sessions", headers=auth).json()["sessions"]
    s = next(x for x in sessions if x["name"] == name)
    assert "pending" in s
    assert s["pending"] is None
