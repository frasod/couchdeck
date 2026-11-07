"""Nothing personal in the files a public release would ship.

"Shipped" means every tracked file that `git archive` would export, i.e. not
marked `export-ignore` in .gitattributes. Outside a git checkout (an exported
tree) every file under the repo is checked.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDORED = {"server/static/xterm.js", "server/static/xterm.css",
            "server/static/xterm-addon-fit.js", "server/static/xterm-addon-unicode11.js"}
# Addresses that are fine in public files: test fixtures and documentation ranges.
ALLOWED_IPS = {"100.64.0.7", "127.0.0.1", "0.0.0.0", "192.168.1.5"}
ALLOWED_TS_NAMES = {"box.tail123.ts.net", "other.tail123.ts.net"}


def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def shipped_files() -> list[str]:
    ls = _git("ls-files", "-z")
    if ls.returncode != 0:
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts and "venv" not in p.parts
                and "__pycache__" not in p.parts]
    files = [f for f in ls.stdout.split("\0") if f]
    attrs = _git("check-attr", "export-ignore", "--", *files).stdout.splitlines()
    ignored = {line.split(": ")[0] for line in attrs if line.endswith(": set")}
    return [f for f in files if f not in ignored]


def _texts():
    for rel in shipped_files():
        if rel in VENDORED:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            yield rel, path.read_text()
        except UnicodeDecodeError:
            continue


def test_private_files_are_not_exported():
    shipped = set(shipped_files())
    for name in ("CLAUDE.md", "ROADMAP.md", "PUBLISH-AUDIT.md", "OPERATOR.md", ".env"):
        assert name not in shipped, f"{name} would be published"


def test_no_tailnet_names_or_private_addresses():
    bad = []
    for rel, text in _texts():
        for name in re.findall(r"[\w-]+\.[\w-]+\.ts\.net", text):
            if name not in ALLOWED_TS_NAMES:
                bad.append(f"{rel}: {name}")
        for ip in re.findall(r"\b(?:10|100|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text):
            if ip not in ALLOWED_IPS:
                bad.append(f"{rel}: {ip}")
    assert not bad, bad


def test_no_home_paths_or_email_addresses():
    bad = []
    for rel, text in _texts():
        for m in re.findall(r"/home/[a-z_][\w-]*", text):
            if m not in ("/home/you",):
                bad.append(f"{rel}: {m}")
        for m in re.findall(r"[\w.+-]+@[\w-]+\.(?:com|net|org|io)\b", text):
            bad.append(f"{rel}: {m}")
    assert not bad, bad


def test_no_private_words():
    """Names that must never ship live outside the repo, one per line."""
    path = Path(os.environ.get("COUCHDECK_PRIVATE_WORDS",
                               Path.home() / ".config" / "couchdeck" / "private-words.txt"))
    if not path.is_file():
        pytest.skip(f"no {path}")
    words = [w.strip().lower() for w in path.read_text().splitlines()
             if w.strip() and not w.startswith("#")]
    bad = [f"{rel}: {w}" for rel, text in _texts() for w in words if w in text.lower()]
    assert not bad, bad
