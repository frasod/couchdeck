"""couchdeck: mobile web terminal for tmux sessions over Tailscale.

Each WebSocket connection attaches a real PTY running `tmux attach -d -t <name>`,
so the phone sees the same live sessions as the desktop (grok TUIs included).
The -d matters: a tmux window has one size, so another attached client would
otherwise keep the window at its width and the phone would render a clipped
view. See the README's Terminal sizing section.
Creating a session can optionally cd into a ~/projects dir and launch a command
(e.g. grok) in it.
"""
import asyncio
import atexit
import fcntl
import ipaddress
import json
import logging
import os
import pty
import re
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import termios
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("couchdeck")

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", Path.home() / "projects")).resolve()
# Free-text CreateSessionRequest.command is opt-in. Launchers are the default.
ALLOW_CUSTOM_CMD = os.environ.get("COUCHDECK_ALLOW_CUSTOM_CMD", "").strip() in ("1", "true", "yes")

# Extra origins allowed to open /ws/term, comma-separated. Same-origin (Origin
# host == Host) is always allowed; this is for reverse proxies or a custom name.
ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get("COUCHDECK_ORIGINS", "").split(",") if o.strip()
}


# Host names the app answers to, comma-separated, without ports. IP literals,
# localhost, this machine's hostname and its Tailscale MagicDNS name are always
# allowed; this is for anything else (a reverse proxy's name, a CNAME).
EXTRA_HOSTS = {
    h.strip().lower().rstrip(".")
    for h in os.environ.get("COUCHDECK_HOSTS", "").split(",") if h.strip()
}
# The app hands out a shell and has no login. It refuses requests that arrive
# on a publicly routable local address unless this is set.
ALLOW_PUBLIC = os.environ.get("COUCHDECK_ALLOW_PUBLIC", "").strip() in ("1", "true", "yes")


def _split_host(host: str) -> tuple[str, str]:
    """`name:port` / `[v6]:port` -> (name, port). Port may be empty."""
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        return name, rest[1:] if rest.startswith(":") else rest
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name, port
    return host, ""


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


_ts_name: str | None = None
_ts_checked = 0.0


def _tailscale_name() -> str | None:
    """This machine's MagicDNS name, which `tailscale serve` forwards as Host.
    Re-asked at most once a minute, so a tailscaled that came up after
    couchdeck is picked up without a restart."""
    global _ts_name, _ts_checked
    if _ts_name is None and time.monotonic() - _ts_checked > 60:
        _ts_checked = time.monotonic()
        try:
            r = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=3,
            )
            name = json.loads(r.stdout)["Self"]["DNSName"]
            _ts_name = name.lower().rstrip(".") or None
        except Exception:
            pass
    return _ts_name


def host_ok(host: str | None) -> bool:
    """DNS rebinding defence. An attacker's page on evil.example can make
    evil.example resolve to this box, after which the browser treats the app as
    same-origin with the attacker: it can read /api/sessions and open
    /ws/term. The only thing that still says "evil.example" is the Host header,
    so only names this machine actually goes by are accepted. An IP literal
    can't be rebound, so any is fine."""
    if not host:
        return False
    name, port = _split_host(host)
    if port and not port.isdigit():
        return False
    name = name.lower().rstrip(".")
    if _is_ip(name):
        return True
    if name in EXTRA_HOSTS or name == "localhost":
        return True
    if name == socket.gethostname().lower():
        return True
    return name == _tailscale_name()


def listener_ok(server: tuple | None) -> bool:
    """False when the request reached us on a publicly routable address.
    Loopback, RFC 1918, Tailscale's 100.64/10 and IPv6 ULA all pass; so does a
    Unix socket (no address). A reverse proxy on localhost passes too, which is
    the documented way to put a login in front of this."""
    if ALLOW_PUBLIC or not server:
        return True
    try:
        return not ipaddress.ip_address(server[0]).is_global
    except ValueError:
        return True


def origin_ok(headers) -> bool:
    """WebSockets are exempt from same-origin policy, and a cross-site form
    POST needs no preflight, so without this check any page the browser visits
    can dial the terminal or create a session. Only meaningful after host_ok:
    comparing Origin to Host is what a rebinding attacker controls both of.
    A missing Origin means a non-browser client and is allowed through."""
    origin = headers.get("origin")
    if origin is None:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    return urlparse(origin).netloc == headers.get("host", "")


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
]


class RequestGate:
    """Host allowlist, public-listener guard, cross-origin writes, and security
    headers, for HTTP and WebSocket alike. ASGI middleware rather than a
    FastAPI dependency so nothing (static files, a future route) can skip it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        headers = Headers(scope=scope)
        if not listener_ok(scope.get("server")):
            log.warning("refused request on public address %s; see COUCHDECK_ALLOW_PUBLIC",
                        scope.get("server"))
            reason = "refusing a public listener: couchdeck has no login (COUCHDECK_ALLOW_PUBLIC)"
            return await self._deny(scope, receive, send, reason)
        if not host_ok(headers.get("host")):
            return await self._deny(scope, receive, send, "unknown host (COUCHDECK_HOSTS)")
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            if not origin_ok(headers):
                return await self._deny(scope, receive, send, "cross-origin request")
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _deny(self, scope, receive, send, reason):
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4403})
            return
        await PlainTextResponse(reason, status_code=403)(scope, receive, send)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _restore_orphaned_status()
    yield
    _shutdown_restore_status()


class _Static(StaticFiles):
    """index.html is served by `/` with no-store and a CSP; the copy under
    /static would bypass both."""

    async def get_response(self, path, scope):
        if path == "index.html":
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestGate)
app.mount("/static", _Static(directory=STATIC_DIR), name="static")


@app.get("/")
async def index(request: Request):
    # Any script in this origin can attach a shell, so only the page's own
    # scripts run: each <script> tag gets a per-response nonce. Styles stay
    # 'unsafe-inline' — xterm.js and the page set them inline everywhere, and
    # CSS cannot reach the PTY. connect-src names the WebSocket explicitly
    # because older Safari does not count ws: as 'self'.
    nonce = secrets.token_urlsafe(16)
    html = (STATIC_DIR / "index.html").read_text().replace("<script", f'<script nonce="{nonce}"')
    host = request.headers.get("host", "")  # already checked by RequestGate
    csp = "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        f"connect-src 'self' ws://{host} wss://{host}",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
    # Phones otherwise keep a stale shell and miss fit/auth fixes after a deploy.
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Content-Security-Policy": csp,
        },
    )


@app.get("/health")
async def health():
    """Liveness for the service and a cheap tmux reachability check."""
    try:
        r = tmux("-V")
        ok_tmux = r.returncode == 0
    except Exception:
        ok_tmux = False
    return {"ok": True, "tmux": ok_tmux}


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)


def _active_panes() -> dict[str, dict]:
    """session name -> {command, path} for the active pane of the active window.

    A separate call from list-sessions because session formats can't reach the
    pane. Best-effort: a failure here just means the deck shows no subtitle.
    """
    r = tmux(
        "list-panes", "-a",
        "-f", "#{&&:#{window_active},#{pane_active}}",
        "-F", "#{session_name}\t#{pane_current_command}\t#{pane_current_path}",
    )
    out: dict[str, dict] = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = {"command": parts[1], "path": parts[2]}
    return out


# Box-drawing and block characters: TUI chrome (input boxes, rules, scrollbars).
_CHROME = set("─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║╔╗╚╝▏▕█▌▐░▒▓ ")


def _is_rule(line: str) -> bool:
    s = line.strip()
    return len(s) >= 8 and sum(c in "─━═╭╮╰╯┌┐└┘" for c in s) / len(s) > 0.6


def _box_span(lines: list[str]) -> tuple[int, int] | None:
    """Top and bottom rule indexes of the lowest input/question box, if any."""
    rules = [i for i, l in enumerate(lines) if _is_rule(l)]
    if not rules:
        return None
    bottom = rules[-1]
    top = next((i for i in reversed(rules[:-1]) if bottom - i <= 15), None)
    if top is None:
        return None
    return top, bottom


def _clean_pane_line(line: str) -> str:
    return line.strip().strip("│┃ ").rstrip("█▕▏ ").strip()


def preview_lines(screen: str, n: int = 3) -> list[str]:
    """The last few meaningful lines of a pane, for the deck card.

    Agent TUIs end with an input box and a key-hint line (grok: `╭─╮ │ ❯ ╰─╯`,
    Claude: `─── ❯ ───`), so the literal last lines are chrome. Take the text
    above the bottom box, plus whatever the box holds if it is more than an
    empty prompt — that is where a permission question shows up. A plain
    shell has no box and just gets its last lines.
    """
    lines = [l.rstrip() for l in screen.splitlines()]
    span = _box_span(lines)
    above, inside = lines, []
    if span is not None:
        top, bottom = span
        above, inside = lines[:top], lines[top + 1:bottom]
    out = []
    for l in above + inside:
        s = _clean_pane_line(l)
        if not s or all(c in _CHROME for c in s):
            continue
        if s in ("❯", ">", "›"):  # an empty prompt
            continue
        out.append(re.sub(r"\s{3,}", "  ", s)[:160])  # right-aligned columns
    return out[-n:]


# A permission question offers numbered Yes/No (or Allow/Deny) choices. The
# shell command the agent is not allowed to run sits in that question, usually
# in backticks or on its own line just above the choices.
_OPTION = re.compile(r"^[❯>›]?\s*\d+\.\s+\S")
_BACKTICK = re.compile(r"`([^`\n]{1,500})`")
_DOLLAR = re.compile(r"^\$\s+(\S.*?)\s*$")
_YESNO = re.compile(r"\b(?:yes|no|allow|deny)\b", re.I)
_SHELL_VERBS = frozenset("""
git sudo npm npx pnpm yarn cargo make python python3 pip pip3 pytest docker
podman systemctl ssh scp rsync curl wget apt apt-get dnf pacman brew tmux
just go rustup kubectl helm terraform ansible bash sh zsh fish node deno bun
uv poetry pdm grok claude codex opencode echo printf cat rm cp mv mkdir ln
chmod chown tar gzip gunzip unzip find grep sed awk head tail less more
kill pkill ps env source cd ls pwd touch install
""".split())


def _is_permission(lines: list[str]) -> bool:
    opts = [l for l in lines if _OPTION.match(l)]
    if len(opts) >= 2 and any(_YESNO.search(l) for l in opts):
        return True
    blob = " ".join(lines).lower()
    return "do you want to proceed" in blob or "allow this" in blob


def _commandish(text: str) -> bool:
    """A single shell line, not a sentence and not a Yes/No choice."""
    s = text.strip()
    if not s or len(s) > 500 or any(c in s for c in "\n\r"):
        return False
    if _OPTION.match(s) or s.endswith("?"):
        return False
    first = s.split()[0]
    if first == "sudo" and len(s.split()) > 1:
        first = s.split()[1]
    if first.startswith(("./", "/", "~/")):
        return True
    base = first.rsplit("/", 1)[-1]
    if base in _SHELL_VERBS:
        return True
    return any(op in s for op in ("|", "&&", "||", ";", ">", "<", "`", "$(", " -", " --"))


def blocked_command(screen: str) -> str | None:
    """The shell command a permission question is asking about, if there is one.

    Returned only when the pane is actually waiting on a Yes/No choice, so a
    normal transcript or a shell prompt is left alone. The caller copies it
    into a new shell for the user to run; nothing here executes it.
    """
    lines = [l.rstrip() for l in screen.splitlines()]
    span = _box_span(lines)
    if span is None:
        region = lines[-12:]
    else:
        top, bottom = span
        region = lines[max(0, top - 4):bottom]
    cleaned = []
    for line in region:
        s = _clean_pane_line(line)
        if s and not all(c in _CHROME for c in s):
            cleaned.append(s)
    if not _is_permission(cleaned):
        return None
    found: list[str] = []
    for s in cleaned:
        found.extend(m.strip() for m in _BACKTICK.findall(s))
        dollar = _DOLLAR.match(s)
        if dollar:
            found.append(dollar.group(1).strip())
        elif not s.endswith("?") and not _OPTION.match(s):
            found.append(s)
    for cand in reversed(found):
        if _commandish(cand):
            return cand
    return None


def _screen(session: str) -> str:
    r = tmux("capture-pane", "-p", "-t", f"={session}:")
    return r.stdout if r.returncode == 0 else ""


def _preview(session: str) -> list[str]:
    return preview_lines(_screen(session))


@app.get("/api/sessions")
async def list_sessions():
    r = tmux(
        "list-sessions", "-F",
        "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_activity}",
    )
    panes = _active_panes()
    sessions = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue  # a name containing a tab or newline; skip rather than crash
            name, created, attached, activity = parts
            pane = panes.get(name, {})
            screen = _screen(name)
            sessions.append({
                "name": name,
                "created": int(created),
                "attached": int(attached) > 0,
                "activity": int(activity),
                "command": pane.get("command", ""),
                "path": pane.get("path", ""),
                "preview": preview_lines(screen),
                "pending": blocked_command(screen),
            })
    return {"sessions": sessions}


@app.get("/api/projects")
async def list_projects():
    dirs = sorted(
        p.name for p in PROJECTS_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ) if PROJECTS_ROOT.is_dir() else []
    return {"projects": dirs}


# --- launchers ---------------------------------------------------------------
# The point of the app: start a coding agent in a project from the phone without
# typing a command line. Each launcher is a command template; when it takes a
# model, ` --model <shell-quoted id>` is appended (see build_command).

# Defaults; ~/.config/couchdeck/launchers.json (or COUCHDECK_LAUNCHERS) replaces
# the whole list. An entry may carry a "note", shown under "Your launchers" in
# the CLI guide (e.g. which account a wrapper script logs in as). That file is trusted like a shell rc file: its templates are
# typed into a shell. The first entry is preselected in the create dialog.
LAUNCHERS = [
    # Remote Control so the phone-started TUI also shows up on claude.ai / the Claude app.
    {"id": "claude", "label": "Claude Code", "template": "claude --remote-control {name}"},
    {"id": "grok", "label": "Grok", "template": "grok"},
    {"id": "opencode", "label": "opencode", "template": "opencode", "models": "opencode"},
    {"id": "codex", "label": "Codex", "template": "codex"},
    {"id": "agy", "label": "Antigravity", "template": "agy"},
    {"id": "shell", "label": "Shell only", "template": ""},
]


def _launchers_path() -> Path:
    raw = os.environ.get("COUCHDECK_LAUNCHERS", "")
    return Path(raw) if raw else Path.home() / ".config" / "couchdeck" / "launchers.json"


def launchers() -> list[dict]:
    """The configured launchers, or the defaults. Read per request so editing
    the file needs no restart. A broken file falls back to the defaults with a
    log line rather than leaving the phone with nothing to start."""
    path = _launchers_path()
    if not path.is_file():
        return LAUNCHERS
    try:
        data = json.loads(path.read_text())
        out = []
        for l in data:
            entry = {"id": l["id"], "label": l["label"], "template": l["template"]}
            if not all(isinstance(v, str) for v in entry.values()):
                raise TypeError("id, label and template must be strings")
            if not VALID_NAME.match(entry["id"]):
                raise ValueError(f"bad launcher id {entry['id']!r}")
            if l.get("models") == "opencode":
                entry["models"] = "opencode"
            if "note" in l:
                if not isinstance(l["note"], str):
                    raise TypeError("note must be a string")
                entry["note"] = l["note"]
            out.append(entry)
        if not out:
            raise ValueError("empty launcher list")
        return out
    except (OSError, ValueError, TypeError, KeyError) as e:
        log.warning("ignoring %s (%s); using default launchers", path, e)
        return LAUNCHERS

OPENCODE_CONFIGS = [
    Path.home() / ".config/opencode/opencode.jsonc",
    Path.home() / ".config/opencode/opencode.json",
]


def _strip_jsonc(src: str) -> str:
    """Drop // and /* */ comments without touching them inside string literals —
    a naive regex eats the `//` in every https:// URL in the config."""
    out, i, n, in_str = [], 0, len(src), False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = j + 2 if j >= 0 else n
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def opencode_models() -> dict:
    """Models declared in the user's opencode config, as provider/model ids.

    Read at request time, not import time, so editing the config doesn't need a
    couchdeck restart. Any parse failure degrades to an empty list — the launcher
    still works, it just starts opencode with its own default model.
    """
    for path in OPENCODE_CONFIGS:
        if not path.is_file():
            continue
        try:
            cfg = json.loads(_strip_jsonc(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        models = []
        for provider_id, provider in (cfg.get("provider") or {}).items():
            for model_id, model in (provider.get("models") or {}).items():
                full = f"{provider_id}/{model_id}"
                models.append({"id": full, "label": (model or {}).get("name") or full})
        return {"models": models, "default": cfg.get("model")}
    return {"models": [], "default": None}


@app.get("/api/launchers")
async def list_launchers():
    oc = opencode_models()
    out = []
    for l in launchers():
        entry = {
            "id": l["id"], "label": l["label"], "models": [], "default_model": None,
            # Free text from the user's own launchers.json, shown in the CLI guide.
            "note": l.get("note") or None,
        }
        if l.get("models") == "opencode":
            entry["models"] = oc["models"]
            entry["default_model"] = oc["default"]
        out.append(entry)
    return {"launchers": out}


def build_command(
    launcher_id: str, model: str | None, session_name: str | None = None
) -> str:
    """Turn a launcher id + model into the command line sent via send-keys.

    send-keys types into a shell, so the model id is shell-quoted rather than
    trusted — it arrives from the client and could otherwise carry `; rm -rf`.
    `{name}` in a template is replaced with the (already allowlisted) session
    name, used e.g. so Claude Remote Control shows the same name as tmux.
    """
    launcher = next((l for l in launchers() if l["id"] == launcher_id), None)
    if launcher is None:
        raise HTTPException(status_code=400, detail="unknown launcher")
    cmd = launcher["template"]
    if "{name}" in cmd:
        # Session names are allowlisted before we get here; still quote for the shell.
        cmd = cmd.replace("{name}", shlex.quote(session_name or "session"))
    if cmd and model and launcher.get("models"):
        cmd = f"{cmd} --model {shlex.quote(model)}"
    return cmd


class CreateSessionRequest(BaseModel):
    name: str
    project: str | None = None
    command: str | None = None  # e.g. "grok" — runs inside the new session
    launcher: str | None = None  # preferred over `command`; see LAUNCHERS
    model: str | None = None     # only meaningful for launchers that take one


# An allowlist, not a denylist. The old check ("no dots/colons/spaces") let
# through names DELETE could never kill: tmux reads a leading `$` as a
# session-ID prefix, and a leading `-` as a flag. See the README's Known gaps.
VALID_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    name = req.name.strip()
    if not VALID_NAME.match(name):
        raise HTTPException(
            status_code=400,
            detail="invalid session name (letters, digits, _ and -; must not start with -)",
        )
    if tmux("has-session", "-t", f"={name}").returncode == 0:
        raise HTTPException(status_code=409, detail="session already exists")

    cwd = PROJECTS_ROOT
    if req.project:
        try:
            cwd = (PROJECTS_ROOT / req.project).resolve()
        except ValueError:  # e.g. embedded null byte
            raise HTTPException(status_code=400, detail="bad project")
        if PROJECTS_ROOT not in cwd.parents or not cwd.is_dir():
            raise HTTPException(status_code=400, detail="bad project")

    # launcher wins over free-text. Free-text is gated: a phone UI that only
    # offers launchers must not be able to POST an arbitrary shell line unless
    # COUCHDECK_ALLOW_CUSTOM_CMD is explicitly enabled.
    if req.launcher:
        command = build_command(req.launcher, req.model, name)
    elif req.command:
        if not ALLOW_CUSTOM_CMD:
            raise HTTPException(
                status_code=400,
                detail="custom command disabled; set COUCHDECK_ALLOW_CUSTOM_CMD=1",
            )
        command = req.command
    else:
        command = None

    # COUCHDECK_SESSION tells ~/.bashrc's rename-to-folder prompt hook to leave
    # this session alone. Without it the first prompt renames `proj-claude` to
    # `proj`, and the phone attaches to a name that no longer exists. -e sets
    # it before the shell starts, so there is no window for the rename to win.
    args = ["new-session", "-d", "-e", "COUCHDECK_SESSION=1", "-s", name, "-c", str(cwd)]
    r = tmux(*args)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip())
    if command:
        # send-keys instead of passing the command to new-session, so the
        # session drops back to a shell when the command exits instead of dying.
        #
        # The trailing colon is load-bearing. send-keys takes a *pane* target,
        # and `=name` alone is not a valid one — tmux answers "can't find pane"
        # and the command silently never runs (this shipped broken: every
        # session opened at a bare shell instead of the agent). `=name:` means
        # "the active pane of the active window of exactly this session", and
        # keeps the exact-match guarantee: it refuses to fall through to a
        # longer session name.
        sk = tmux("send-keys", "-t", f"={name}:", command, "Enter")
        if sk.returncode != 0:
            tmux("kill-session", "-t", f"={name}")
            raise HTTPException(
                status_code=500,
                detail=sk.stderr.strip() or "send-keys failed",
            )
    return {"ok": True, "name": name}


def _session_cwd(name: str) -> Path | None:
    r = tmux(
        "list-panes", "-t", f"={name}",
        "-f", "#{pane_active}",
        "-F", "#{pane_current_path}",
    )
    if r.returncode != 0:
        return None
    raw = r.stdout.strip().splitlines()
    if not raw or not raw[0]:
        return None
    try:
        return Path(raw[0])
    except ValueError:
        return None


def _cwd_allowed(path: Path) -> bool:
    """The shell opens where the agent already is. A symlink that resolves
    outside the home directory or the projects root is not a place to start one."""
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    if not resolved.is_dir():
        return False
    roots = [PROJECTS_ROOT, Path.home().resolve()]
    return any(resolved == root or root in resolved.parents for root in roots)


def _fresh_shell_name(source: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", source).strip("-")
    if not stem or not stem[0].isalnum() and stem[0] != "_":
        stem = "sh" + stem
    for i in range(1, 30):
        suffix = "-sh" if i == 1 else f"-sh{i}"
        name = stem[:64 - len(suffix)] + suffix
        if not VALID_NAME.match(name):
            continue
        if tmux("has-session", "-t", f"={name}").returncode != 0:
            return name
    raise HTTPException(status_code=409, detail="too many shells for this session")


@app.post("/api/sessions/{name}/shell")
async def open_shell(name: str):
    """A new shell in the same directory as `name`.

    When that pane is sitting on a permission question, the command is returned
    as `pending` so the phone can drop it into the compose box. It is not typed
    into the shell and not run — the user sends it after looking at it.
    """
    if not VALID_NAME.match(name):
        raise HTTPException(status_code=400, detail="invalid session name")
    if tmux("has-session", "-t", f"={name}").returncode != 0:
        raise HTTPException(status_code=404, detail="no such session")
    cwd = _session_cwd(name)
    if cwd is None or not _cwd_allowed(cwd):
        raise HTTPException(status_code=400, detail="session directory is not available")
    shell_name = _fresh_shell_name(name)
    r = tmux(
        "new-session", "-d", "-e", "COUCHDECK_SESSION=1",
        "-s", shell_name, "-c", str(cwd.resolve()),
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip() or "could not open a shell")
    return {"ok": True, "name": shell_name, "pending": blocked_command(_screen(name))}


@app.delete("/api/sessions/{name}")
async def kill_session(name: str):
    r = tmux("kill-session", "-t", f"={name}")
    if r.returncode != 0:
        raise HTTPException(status_code=404, detail=r.stderr.strip())
    return {"ok": True}


# Generous for any real terminal (a 4K display at a tiny font is ~500 cols) but
# far below the unsigned-short ceiling. Sizes near 65535 are accepted by
# TIOCSWINSZ and then wedge the whole tmux server — every session, not just
# this one — while it tries to allocate the grid.
MAX_COLS, MAX_ROWS = 1000, 1000


def set_winsize(fd, rows, cols):
    """Clamp to a sane terminal size. A hostile client must not be able to turn
    a resize message into either a struct.error or a tmux-wide stall."""
    rows = max(1, min(int(rows), MAX_ROWS))
    cols = max(1, min(int(cols), MAX_COLS))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _own_session_with_ctty(slave_fd: int):
    """preexec_fn for the tmux client: new session, and make the PTY slave its
    controlling terminal.

    setsid() alone leaves the process with *no* controlling terminal, because
    the slave was opened before the new session existed. TIOCSWINSZ on the
    master then has no foreground process group to SIGWINCH, so tmux only ever
    saw the size from the first message: every later resize (soft keyboard,
    rotation, font change) was silently dropped and the TUI kept drawing at its
    old size into a terminal that had changed under it.
    """
    os.setsid()
    # slave_fd, not fd 0: uvloop runs preexec_fn straight after fork, before
    # libuv dup2s the stdio fds, so fd 0 is still the server's stdin here.
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


# The status bar is a session option shared by every client, and a reconnect
# overlaps: the new socket attaches (and -d kicks the old tmux client) before
# the old socket's cleanup runs. Count couchdeck connections per session so only
# the first hides the bar and only the last puts it back.
_status_holders: dict[str, dict] = {}


def _status_state_path() -> Path:
    raw = os.environ.get("COUCHDECK_STATUS_STATE", "")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "state" / "couchdeck-status.json"


def _read_status_state() -> dict[str, str]:
    try:
        data = json.loads(_status_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in data.items()}


def _write_status_state(data: dict[str, str]) -> None:
    path = _status_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _persist_hidden() -> None:
    """Record sessions whose bar we turned off, and the value to put back.

    Holders live in memory and vanish on crash. The file is what startup uses,
    so a session the user set to off is not confused with one we hid.
    """
    _write_status_state({
        name: held.get("prev", "")
        for name, held in _status_holders.items()
        if held.get("count", 0) > 0
    })


def _apply_status_prev(session: str, prev: str):
    """Put status back. Empty means the option was unset (inherit the global).
    A stored "off" is the user's setting and must be written back as off."""
    if prev:
        tmux("set-option", "-t", f"={session}:", "status", prev)
    else:
        tmux("set-option", "-u", "-t", f"={session}:", "status")


def _hide_status(session: str):
    held = _status_holders.get(session)
    if held:
        held["count"] += 1
        return
    # Pane target with the trailing colon: set-option/show-options reject a bare
    # `=name` ("no such session"), and `=name:` keeps the exact-match guarantee.
    prev = tmux("show-options", "-v", "-t", f"={session}:", "status")
    _status_holders[session] = {
        "count": 1,
        "prev": prev.stdout.strip() if prev.returncode == 0 else "",
    }
    _persist_hidden()
    tmux("set-option", "-t", f"={session}:", "status", "off")


def _restore_status(session: str):
    held = _status_holders.get(session)
    if not held:
        return
    held["count"] -= 1
    if held["count"] > 0:
        return
    del _status_holders[session]
    _persist_hidden()
    _apply_status_prev(session, held["prev"])


def _restore_orphaned_status():
    """After a crash, put back only the sessions we recorded as hidden.

    Any other session with status off was set that way on purpose. Unsetting
    those turns the bar back on.
    """
    state = _read_status_state()
    if not state:
        return
    for name, prev in list(state.items()):
        if tmux("has-session", "-t", f"={name}").returncode != 0:
            state.pop(name, None)
            continue
        _apply_status_prev(name, prev)
        state.pop(name, None)
        log.info("restored status on session %s after restart", name)
    _write_status_state(state)


def _shutdown_restore_status():
    """Best-effort: put every held session's status bar back on stop."""
    for name, held in list(_status_holders.items()):
        try:
            _apply_status_prev(name, held.get("prev", ""))
        except Exception:
            log.info("failed to restore status on %s during shutdown", name)
    _status_holders.clear()
    _persist_hidden()


atexit.register(_shutdown_restore_status)


async def _spawn_attach(session: str, slave_fd: int):
    return await asyncio.create_subprocess_exec(
        # -d detaches any other client. A tmux window has ONE size, so a desktop
        # client attached at 146 cols would keep the window at 146 while the
        # phone shows 47 — everything past column 47 is silently cut off, which
        # looks like garbled text and a missing input box. Grouped sessions do
        # not help: they share windows, so they share the size too.
        "tmux", "attach-session", "-d", "-t", f"={session}",
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"},
        preexec_fn=lambda: _own_session_with_ctty(slave_fd),
    )


# Close codes for a terminal whose tmux client exited by itself, i.e. not a
# dropped connection. The page reattaches after a drop, but must not after
# these: reattaching is `attach -d`, which would kick the device that just took
# the session, and that device's page would kick back. Two open pages on one
# session then trade it every second, forever.
# Anything else (a couchdeck restart ends clients with "[terminated]") keeps the
# plain close, and the page reattaches as it should.
CLOSE_TAKEN_OVER = 4409   # tmux printed "[detached ...]": another attach -d, or prefix-d
CLOSE_SESSION_GONE = 4410  # the session was killed or its last pane exited


# SGR wheel events, as the frontend's drag-to-scroll sends them.
WHEEL_RE = re.compile(rb"\x1b\[<6[4-7];\d+;\d+[Mm]")


def _wheel_route(session: str) -> str:
    """Where a wheel event on this session's active pane ends up, going by the
    WheelUpPane binding in ~/.tmux.conf (the tmux default does the same):

    - "app":  the app tracks the mouse (grok, claude: mouse_any_flag=1), or the
              pane is already in copy-mode. tmux forwards it; the app scrolls.
    - "drop": a full-screen app that does NOT track the mouse (less, htop).
              tmux would open copy-mode over it — the shell's history painted
              on top of the TUI — and eat the next keys. Nothing useful to do.
    - "copy": a plain shell. copy-mode scrollback, as on the desktop.
    """
    r = tmux("display", "-p", "-t", f"={session}:",
             "#{mouse_any_flag} #{alternate_on} #{pane_in_mode}")
    parts = r.stdout.split() if r.returncode == 0 else []
    if len(parts) != 3:
        return "copy"  # unknown: the pre-existing behaviour
    mouse, alt, in_mode = (p == "1" for p in parts)
    if mouse or in_mode:
        return "app"
    return "drop" if alt else "copy"


@app.websocket("/ws/term")
async def term_ws(ws: WebSocket):
    if not origin_ok(ws.headers):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        init = json.loads(await ws.receive_text())
        session = init["session"]
        cols = int(init.get("cols", 80))
        rows = int(init.get("rows", 24))
    except Exception:
        await ws.close(code=4400)
        return

    if tmux("has-session", "-t", f"={session}").returncode != 0:
        await ws.send_text(json.dumps({"type": "error", "message": "no such tmux session"}))
        await ws.close()
        return

    # Phone view: hide the green status bar for this session while attached.
    # Status is a session option (not per-client), so we restore whatever it was
    # on disconnect — desktop keeps its usual bar after the phone leaves.
    _hide_status(session)

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, rows, cols)
    try:
        proc = await _spawn_attach(session, slave_fd)
    except Exception:
        log.info("attach failed for session %s", session, exc_info=True)
        os.close(master_fd)
        _restore_status(session)
        raise
    finally:
        os.close(slave_fd)
    loop = asyncio.get_running_loop()

    # Read the PTY via the event loop rather than a worker thread. A thread
    # blocked in os.read() cannot be cancelled, so the old version closed
    # master_fd out from under a live reader — and that fd number can be handed
    # straight to the next connection. add_reader detaches synchronously.
    os.set_blocking(master_fd, False)
    outbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable():
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # PTY hung up: tmux detached or died
            loop.remove_reader(master_fd)
            outbox.put_nowait(None)
            return
        outbox.put_nowait(data)

    loop.add_reader(master_fd, on_readable)

    async def pty_to_ws():
        tail = b""  # tmux's last words name why it ended the client
        while True:
            data = await outbox.get()
            if data is None:
                break
            tail = (tail + data)[-256:]
            try:
                await ws.send_bytes(data)
            except Exception:
                return
        # The PTY hung up while the socket was still open: tmux ended this
        # client. Say why, so the page knows whether to reattach.
        gone = (await asyncio.to_thread(
            tmux, "has-session", "-t", f"={session}")).returncode != 0
        if gone:
            code = CLOSE_SESSION_GONE
        elif b"[detached" in tail:
            code = CLOSE_TAKEN_OVER
        else:
            code = 1000
        try:
            await ws.close(code=code)
        except Exception:
            pass

    reader = asyncio.create_task(pty_to_ws())
    scrolled = False
    # A swipe is dozens of wheel events; one tmux query per ~gesture is plenty.
    route, route_at = "copy", 0.0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                # A drag scrolls via tmux's WheelUpPane binding, which drops any
                # app without mouse tracking (agy, a shell) into copy-mode, and
                # copy-mode swallows whatever is typed next. Claude tracks the
                # mouse itself, so it never hit this. Typing after a scroll
                # means "back to the prompt": leave copy-mode before the keys.
                typed = WHEEL_RE.sub(b"", data)
                if typed != data:
                    if loop.time() - route_at > 0.5:
                        route = await asyncio.to_thread(_wheel_route, session)
                        route_at = loop.time()
                    if route == "drop":
                        data = typed
                    elif route == "copy":
                        scrolled = True
                if typed and scrolled:
                    scrolled = False
                    await asyncio.to_thread(tmux, "send-keys", "-X", "-t", f"={session}:", "cancel")
                if data:
                    os.write(master_fd, data)
            elif (text := msg.get("text")) is not None:
                # control channel: resize messages come as JSON text
                try:
                    ctl = json.loads(text)
                    if ctl.get("type") == "resize":
                        try:
                            set_winsize(master_fd, ctl["rows"], ctl["cols"])
                        except Exception:
                            log.info(
                                "resize failed for session %s to %sx%s",
                                session, ctl.get("cols"), ctl.get("rows"),
                                exc_info=True,
                            )
                except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        # remove_reader before close: no callback can be mid-read on this fd
        loop.remove_reader(master_fd)
        reader.cancel()
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            except ProcessLookupError:
                pass
        os.close(master_fd)
        # Put the status bar back the way we found it (usually "on").
        try:
            _restore_status(session)
        except Exception:
            log.info("detach/status restore failed for session %s", session, exc_info=True)
