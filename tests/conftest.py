"""Test fixtures for couchdeck.

The app reads PROJECTS_ROOT at import time, so it is set here before
`server.app` is ever imported.
"""
import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

PREFIX = "gdtest"
# Startup status restore must not read or write the real couchdeck state file.
os.environ["COUCHDECK_STATUS_STATE"] = f"/tmp/couchdeck-test-status-{os.getpid()}.json"
# Nor the real launcher config, which would replace the defaults under test.
os.environ["COUCHDECK_LAUNCHERS"] = f"/tmp/couchdeck-test-no-launchers-{os.getpid()}.json"
# TestClient sends `Host: testserver`; the Host allowlist must know it.
os.environ["COUCHDECK_HOSTS"] = "testserver"


def load_app(projects_root=None, allow_custom_cmd=False):
    """(Re)import server.app with the given env. Returns the FastAPI app."""
    if allow_custom_cmd:
        os.environ["COUCHDECK_ALLOW_CUSTOM_CMD"] = "1"
    else:
        os.environ.pop("COUCHDECK_ALLOW_CUSTOM_CMD", None)
    if projects_root:
        os.environ["PROJECTS_ROOT"] = str(projects_root)
    sys.modules.pop("app", None)
    return importlib.import_module("app").app


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)


def live_sessions():
    r = tmux("list-sessions", "-F", "#{session_name}")
    return r.stdout.splitlines() if r.returncode == 0 else []




@pytest.fixture(scope="session")
def projects_root(tmp_path_factory):
    """A sandboxed ~/projects stand-in, so tests never touch the real one."""
    root = tmp_path_factory.mktemp("projects")
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    (root / ".hidden").mkdir()
    (root / "notadir").write_text("file")
    outside = root.parent / "outside-root"
    outside.mkdir(exist_ok=True)
    (outside / "secret").mkdir(exist_ok=True)
    (root / "escape-link").symlink_to(outside)
    return root


@pytest.fixture
def client(projects_root):
    from fastapi.testclient import TestClient
    return TestClient(load_app(projects_root=projects_root))


@pytest.fixture
def auth():
    """Kept for call-site compatibility; auth is not required."""
    return {}


@pytest.fixture
def session_name():
    """A unique tmux session name, killed afterwards however the test ends."""
    created = []

    def make(suffix=""):
        name = f"{PREFIX}-{uuid.uuid4().hex[:8]}{suffix}"
        created.append(name)
        return name

    yield make
    for name in created:
        tmux("kill-session", "-t", f"={name}")


@pytest.fixture(autouse=True, scope="session")
def _reap_strays():
    """Backstop: nothing named gdtest-* survives the run."""
    yield
    for name in live_sessions():
        if name.startswith(PREFIX):
            tmux("kill-session", "-t", f"={name}")
