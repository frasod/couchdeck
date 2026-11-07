"""Launcher presets: the phone picks an agent and a model instead of typing a
command line. The model id arrives from the client and is typed into a shell by
`send-keys`, so quoting is the security boundary here.
"""
import json

import pytest

from conftest import live_sessions, tmux


def _app_module():
    import app
    return app


# --- /api/launchers ----------------------------------------------------------

def test_launchers_listed(client, auth):
    data = client.get("/api/launchers", headers=auth).json()
    ids = {l["id"] for l in data["launchers"]}
    assert {"opencode", "claude", "codex", "agy", "grok", "shell"} <= ids
    assert data["launchers"][0]["id"] == "claude", "first entry is the dialog default"


def test_launchers_need_no_token(client):
    assert client.get("/api/launchers").status_code == 200


def test_only_opencode_offers_models(client, auth):
    for l in client.get("/api/launchers", headers=auth).json()["launchers"]:
        if l["id"] != "opencode":
            assert l["models"] == [], f"{l['id']} should not offer models"


# --- jsonc parsing -----------------------------------------------------------

def test_strip_jsonc_keeps_urls_inside_strings():
    """The obvious `//.*$` regex eats the `//` in every https:// URL, which is
    how the opencode config's $schema line silently broke the parse."""
    strip = _app_module()._strip_jsonc
    src = '{"$schema": "https://opencode.ai/config.json", // a comment\n "a": 1,}'
    assert json.loads(strip(src)) == {"$schema": "https://opencode.ai/config.json", "a": 1}


def test_strip_jsonc_handles_block_comments_and_escapes():
    strip = _app_module()._strip_jsonc
    src = '{/* off */ "a": "say \\"hi\\" // not a comment", "b": 2,}'
    assert json.loads(strip(src)) == {"a": 'say "hi" // not a comment', "b": 2}


def test_opencode_models_survives_a_missing_config(client, auth, monkeypatch):
    app = _app_module()
    monkeypatch.setattr(app, "OPENCODE_CONFIGS", [])
    assert app.opencode_models() == {"models": [], "default": None}


def test_opencode_models_survives_a_broken_config(tmp_path, monkeypatch):
    app = _app_module()
    bad = tmp_path / "opencode.json"
    bad.write_text("{not json at all")
    monkeypatch.setattr(app, "OPENCODE_CONFIGS", [bad])
    assert app.opencode_models()["models"] == []


def test_opencode_models_reads_provider_models(tmp_path, monkeypatch):
    app = _app_module()
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(
        '{ // comment\n "model": "local/big",\n'
        ' "provider": {"local": {"models": {"big": {"name": "Big One"}, "small": {}}}} }'
    )
    monkeypatch.setattr(app, "OPENCODE_CONFIGS", [cfg])
    got = app.opencode_models()
    assert got["default"] == "local/big"
    by_id = {m["id"]: m["label"] for m in got["models"]}
    assert by_id["local/big"] == "Big One"
    assert by_id["local/small"] == "local/small"  # falls back to the id


# --- build_command -----------------------------------------------------------

def test_build_command_appends_model_for_opencode():
    assert _app_module().build_command("opencode", "local/x") == "opencode --model local/x"


def test_build_command_ignores_model_for_launchers_without_one():
    # Claude always starts with Remote Control; model is ignored (no models key).
    assert _app_module().build_command("claude", "local/x") == "claude --remote-control session"
    assert (
        _app_module().build_command("claude", None, "myproj")
        == "claude --remote-control myproj"
    )


def test_agy_launcher_runs_agy():
    assert _app_module().build_command("agy", None) == "agy"
    assert _app_module().build_command("agy", "ignored") == "agy"


def test_shell_launcher_runs_nothing():
    assert _app_module().build_command("shell", None) == ""


def test_unknown_launcher_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _app_module().build_command("rm -rf /", None)


@pytest.mark.parametrize("model", [
    "a; id",
    "a && id",
    "a`id`",
    "a$(id)",
    "a\nid",
    "a | id",
])
def test_model_is_shell_quoted(model):
    """send-keys types into a live shell, so an unquoted model id is command
    injection with no shell metacharacter filtering in between."""
    cmd = _app_module().build_command("opencode", model)
    quoted = cmd[len("opencode --model "):]
    import shlex
    assert shlex.split(cmd)[2] == model, "model must survive quoting intact"
    assert quoted.startswith("'") or all(c not in quoted for c in ";&|`$()\n")


def test_create_with_launcher_and_model(client, auth, session_name):
    name = session_name()
    r = client.post(
        "/api/sessions",
        json={"name": name, "project": "alpha", "launcher": "shell"},
        headers=auth,
    )
    assert r.status_code == 200
    assert name in live_sessions()


def test_create_with_unknown_launcher_creates_nothing(client, auth, session_name):
    name = session_name()
    before = set(live_sessions())
    r = client.post("/api/sessions", json={"name": name, "launcher": "nope"}, headers=auth)
    assert r.status_code == 400
    assert set(live_sessions()) == before, "a rejected launcher must not leave a session behind"


# --- launcher config file ----------------------------------------------------

def test_launcher_config_replaces_defaults(tmp_path, monkeypatch):
    app = _app_module()
    cfg = tmp_path / "launchers.json"
    cfg.write_text(json.dumps([
        {"id": "mine", "label": "Mine", "template": "my-agent {name}"},
        {"id": "oc", "label": "oc", "template": "opencode", "models": "opencode"},
    ]))
    monkeypatch.setenv("COUCHDECK_LAUNCHERS", str(cfg))
    assert [l["id"] for l in app.launchers()] == ["mine", "oc"]
    assert app.build_command("mine", None, "proj") == "my-agent proj"
    assert app.build_command("oc", "a/b") == "opencode --model a/b"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        app.build_command("claude", None)  # defaults are replaced, not merged


@pytest.mark.parametrize("body", [
    "{not json",
    '[{"id": "x", "label": "x", "template": "x", "note": 3}]',
    "[]",
    '{"id": "x"}',
    '[{"id": "x", "label": "x"}]',
    '[{"id": "-flag", "label": "x", "template": "x"}]',
    '[{"id": "x", "label": "x", "template": 3}]',
])
def test_broken_launcher_config_falls_back(tmp_path, monkeypatch, body):
    app = _app_module()
    cfg = tmp_path / "launchers.json"
    cfg.write_text(body)
    monkeypatch.setenv("COUCHDECK_LAUNCHERS", str(cfg))
    assert app.launchers() is app.LAUNCHERS


def test_launcher_note_reaches_the_api(client, auth, tmp_path, monkeypatch):
    cfg = tmp_path / "launchers.json"
    cfg.write_text(json.dumps([
        {"id": "a", "label": "Agent A", "template": "a", "note": "Logs in as the work account."},
        {"id": "b", "label": "Agent B", "template": "b"},
    ]))
    monkeypatch.setenv("COUCHDECK_LAUNCHERS", str(cfg))
    got = {l["id"]: l["note"] for l in client.get("/api/launchers", headers=auth).json()["launchers"]}
    assert got == {"a": "Logs in as the work account.", "b": None}


def test_default_launchers_carry_no_notes(client, auth):
    assert all(l["note"] is None for l in client.get("/api/launchers", headers=auth).json()["launchers"])
