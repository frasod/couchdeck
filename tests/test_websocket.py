"""The PTY channel: session targeting, resize handling, and cleanup.

A leaked PTY or a stray `tmux attach` process is a resource-exhaustion bug on a
box where every connection costs an fd, a process, and an executor thread.
"""
import json
import os
import sys
import time

import pytest

from conftest import tmux


def open_term(client, name, **extra):
    ws = client.websocket_connect("/ws/term").__enter__()
    ws.send_text(json.dumps({"session": name, **extra}))
    return ws


def test_attaches_to_existing_session(client, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        assert ws.receive_bytes(), "no PTY output — attach did not happen"


def test_nonexistent_session_errors_cleanly(client):
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": "gdtest-nope"}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "error"


def test_session_target_is_exact_not_prefix(client, session_name):
    """`=` prefixing must stop a short name from attaching to a longer session."""
    full = session_name("-extended")
    tmux("new-session", "-d", "-s", full)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": full[:-9]}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "error", "short name attached to a longer session"


@pytest.mark.parametrize("init", [
    "not json",
    "{}",                                       # no session key
    '{}',                                     # no session
    '[]',
    '"string"',
])
def test_malformed_init_closes_without_crashing(client, init):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/term") as ws:
            ws.send_text(init)
            ws.receive_bytes()


@pytest.mark.parametrize("dims", [
    {"cols": 0, "rows": 0},
    {"cols": -1, "rows": -1},
    {"cols": 10**6, "rows": 10**6},
    {"cols": "80", "rows": "24"},
])
def test_hostile_initial_dimensions_do_not_crash(client, session_name, dims):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    try:
        with client.websocket_connect("/ws/term") as ws:
            ws.send_text(json.dumps({"session": name, **dims}))
            ws.receive_bytes()
    except Exception:
        pass  # a clean close is acceptable; a server traceback is not
    # server still serving
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("ctl", [
    '{"type": "resize", "rows": 0, "cols": 0}',
    '{"type": "resize", "rows": -5, "cols": -5}',
    '{"type": "resize", "rows": 99999999, "cols": 99999999}',
    '{"type": "resize"}',                       # missing keys
    '{"type": "resize", "rows": "a", "cols": "b"}',
    '{"type": "unknown"}',
    'garbage',
])
def test_hostile_resize_messages_are_swallowed(client, session_name, ctl):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name}))
        ws.receive_bytes()
        ws.send_text(ctl)
        ws.send_bytes(b"")
        # connection must still be alive
        ws.send_text(json.dumps({"type": "resize", "rows": 24, "cols": 80}))


def test_huge_resize_does_not_stall_the_tmux_server(client, session_name):
    """Regression: clamping to the unsigned-short ceiling (65535) is accepted by
    TIOCSWINSZ but makes tmux try to allocate a 65535x65535 grid, wedging the
    whole server — every session on the box, not just this one. The clamp must
    be to a realistic terminal size.
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name}))
        ws.receive_bytes()
        ws.send_text(json.dumps({"type": "resize", "rows": 10**6, "cols": 10**6}))
        time.sleep(0.5)

    start = time.monotonic()
    assert tmux("list-sessions").returncode == 0, "tmux server unresponsive"
    assert time.monotonic() - start < 5, "tmux server stalled after a huge resize"


def test_typing_after_a_scroll_reaches_the_app(client, session_name):
    """Regression: a drag-to-scroll drops a non-mouse app (agy, a shell) into
    tmux copy-mode, and copy-mode ate the keys typed next — on the phone it
    looked like the keyboard losing letters. Typing must land in the app.
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        ws.receive_bytes()
        ws.send_bytes(b"\x1b[<64;1;1M")  # the frontend's WHEEL_UP
        # Don't depend on ~/.tmux.conf's wheel binding: make sure we're in copy-mode.
        deadline = time.time() + 5
        while tmux("display", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout.strip() != "1":
            tmux("copy-mode", "-t", f"={name}:")
            assert time.time() < deadline, "pane never entered copy-mode"
            time.sleep(0.1)
        ws.send_bytes(b"echo GDMARKER-TYPED\r")
        deadline = time.time() + 5
        # the output line, not the echoed command line
        while "GDMARKER-TYPED" not in tmux("capture-pane", "-p", "-t", f"={name}:").stdout.splitlines():
            assert time.time() < deadline, (
                "typed keys never reached the shell; in_mode="
                + tmux("display", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout
            )
            time.sleep(0.1)
    assert tmux("display", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout.strip() == "0"


# A stand-in full-screen TUI: alternate screen, optionally mouse tracking (as
# grok and claude do), raw tty, and every byte it receives appended to a log.
FAKE_TUI = r"""
import os, sys, tty
tty.setraw(0)
os.write(1, b"\x1b[?1049h" + (b"\x1b[?1000h\x1b[?1006h" if sys.argv[2] == "mouse" else b""))
with open(sys.argv[1], "ab", buffering=0) as log:
    while True:
        b = os.read(0, 4096)
        if not b:
            break
        log.write(b)
"""


def _start_fake_tui(name, tmp_path, mode):
    script, log = tmp_path / "tui.py", tmp_path / "tui.log"
    script.write_text(FAKE_TUI)
    log.write_bytes(b"")
    tmux("new-session", "-d", "-s", name, f"{sys.executable} {script} {log} {mode}")
    # Don't depend on ~/.tmux.conf for the thing under test.
    tmux("set-option", "-t", f"={name}:", "mouse", "on")
    want = "1 1" if mode == "mouse" else "1 0"
    _wait_for(lambda: tmux("display", "-p", "-t", f"={name}:",
                           "#{alternate_on} #{mouse_any_flag}").stdout.strip(), want, "fake TUI up")
    return log


def _in_mode(name):
    return tmux("display", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout.strip()


def test_swipe_scrolls_a_mouse_tracking_tui(client, session_name, tmp_path):
    """grok/claude track the mouse, so a swipe must reach them as wheel events
    (they scroll their own transcript) — not be blocked, and not open
    copy-mode. The frontend used to skip wheel events for these by command
    name, which is why swiping in grok did nothing.
    """
    name = session_name()
    log = _start_fake_tui(name, tmp_path, "mouse")
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        ws.receive_bytes()
        ws.send_bytes(b"\x1b[<64;10;5M\x1b[<64;10;5M")
        _wait_for(lambda: log.read_bytes().count(b"\x1b[<64;"), 2, "wheel events at the app")
    assert _in_mode(name) == "0"


def test_swipe_over_a_non_mouse_fullscreen_app_is_dropped(client, session_name, tmp_path):
    """A full-screen app that ignores the mouse (less, htop): tmux's wheel
    binding would open copy-mode on top of it — shell history painted over the
    TUI — and eat the next keys. The server drops those wheel events instead.
    """
    name = session_name()
    log = _start_fake_tui(name, tmp_path, "nomouse")
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        ws.receive_bytes()
        ws.send_bytes(b"\x1b[<64;10;5M")
        # Check before typing: typing cancels copy-mode, which would hide it.
        end = time.time() + 1.0
        while time.time() < end:
            assert _in_mode(name) == "0", "wheel opened copy-mode over a full-screen app"
            time.sleep(0.05)
        ws.send_bytes(b"GDKEYS")
        _wait_for(lambda: log.read_bytes(), b"GDKEYS", "keys after the swipe, and nothing else")


def _open_fds():
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def test_disconnect_leaks_no_fds(client, session_name):
    """Attach/detach cycles must not ratchet the fd count.

    Each connection opens a PTY master and a socket, so a genuine leak grows
    ~1-2 fds *per cycle*. The bound is therefore a fraction of the cycle count,
    not a small constant: the process picks up a couple of fds from anyio's
    thread pool regardless of this loop, which made a `<= 2` bound fail once
    enough earlier tests had run first (measured: 40 cycles grows the count by
    2, i.e. flat, not 40).
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)

    cycles = 20

    def run_cycles(n):
        for _ in range(n):
            with client.websocket_connect("/ws/term") as ws:
                ws.send_text(json.dumps({"session": name}))
                ws.receive_bytes()
            time.sleep(0.05)
        time.sleep(0.5)

    run_cycles(2)          # warm up pools so they aren't counted as a leak
    baseline = _open_fds()
    run_cycles(cycles)
    grown = _open_fds() - baseline
    assert grown < cycles / 2, f"fd count grew by {grown} over {cycles} connections"


def test_disconnect_leaves_no_stray_attach_processes(client, session_name):
    import subprocess
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    for _ in range(5):
        with client.websocket_connect("/ws/term") as ws:
            ws.send_text(json.dumps({"session": name}))
            ws.receive_bytes()
    time.sleep(1.0)
    # Must match the real argv (`attach-session -d -t`), or this passes vacuously.
    ps = subprocess.run(["pgrep", "-af", f"attach-session -d -t ={name}"],
                        capture_output=True, text=True)
    assert not ps.stdout.strip(), f"stray attach processes: {ps.stdout}"


def test_session_survives_disconnect(client, session_name):
    """Detaching must never kill the user's work."""
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name}))
        ws.receive_bytes()
    time.sleep(0.3)
    assert tmux("has-session", "-t", f"={name}").returncode == 0


def _wait_for(fn, want, what, timeout=5):
    deadline = time.time() + timeout
    while (got := fn()) != want:
        assert time.time() < deadline, f"{what}: wanted {want!r}, got {got!r}"
        time.sleep(0.1)


def _pane_size(name):
    return tmux("display", "-p", "-t", f"={name}:", "#{pane_width}x#{pane_height}").stdout.strip()


def _session_status(name):
    # show-options needs the pane form `=name:`; a bare `=name` is "no such session".
    return tmux("show-options", "-v", "-t", f"={name}:", "status").stdout.strip()


def test_resize_after_attach_reaches_tmux(client, session_name):
    """Regression: the tmux client had no controlling terminal (setsid without
    TIOCSCTTY), so TIOCSWINSZ on the master signalled nobody and only the
    *initial* size ever landed. Opening the soft keyboard on the tablet shrank
    xterm while grok kept drawing its old height into it: garbled screen,
    prompt under the keyboard. Assert on the pane, not on the socket.
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 100, "rows": 40}))
        ws.receive_bytes()
        _wait_for(lambda: _pane_size(name), "100x40", "initial size")
        ws.send_text(json.dumps({"type": "resize", "cols": 70, "rows": 20}))
        _wait_for(lambda: _pane_size(name), "70x20", "size after resize (keyboard up)")
        ws.send_text(json.dumps({"type": "resize", "cols": 128, "rows": 28}))
        _wait_for(lambda: _pane_size(name), "128x28", "size after resize (rotate)")


def test_status_bar_hidden_while_attached_then_restored(client, session_name):
    """Regression: set-option was targeted at `=name`, which tmux rejects, and
    the return code was discarded — the bar never hid and cost the TUI a row.
    Restore must leave the option unset (inherited), not pinned to "on".
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    assert _session_status(name) == ""
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        ws.receive_bytes()
        _wait_for(lambda: _session_status(name), "off", "status while attached")
        _wait_for(lambda: _pane_size(name), "80x24", "pane uses the full height")
    _wait_for(lambda: _session_status(name), "", "status after disconnect")


def test_intentional_status_off_is_restored(client, session_name):
    """A session whose bar was already off must still be off after detach.

    Restoring by unsetting the option turns the global default back on.
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    assert tmux("set-option", "-t", f"={name}:", "status", "off").returncode == 0
    assert _session_status(name) == "off"
    with client.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"session": name, "cols": 80, "rows": 24}))
        ws.receive_bytes()
        _wait_for(lambda: _session_status(name), "off", "status while attached")
    _wait_for(lambda: _session_status(name), "off", "status after disconnect")


def test_orphan_restore_leaves_unrelated_off_alone(session_name):
    import app as app_module
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    assert tmux("set-option", "-t", f"={name}:", "status", "off").returncode == 0
    app_module._write_status_state({})
    app_module._restore_orphaned_status()
    assert _session_status(name) == "off"


def test_orphan_restore_puts_back_only_recorded_sessions(session_name):
    import app as app_module
    hidden = session_name()
    intentional = session_name()
    tmux("new-session", "-d", "-s", hidden)
    tmux("new-session", "-d", "-s", intentional)
    assert tmux("set-option", "-t", f"={hidden}:", "status", "off").returncode == 0
    assert tmux("set-option", "-t", f"={intentional}:", "status", "off").returncode == 0
    # We had hidden `hidden` from an unset bar. `intentional` was already off.
    app_module._write_status_state({hidden: ""})
    app_module._restore_orphaned_status()
    assert _session_status(hidden) == ""
    assert _session_status(intentional) == "off"


def test_reconnect_overlap_keeps_status_hidden(client, session_name):
    """A reconnect attaches the new socket before the old one's cleanup runs.
    The old cleanup must not put the bar back under the live connection.
    """
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    old = open_term(client, name, cols=80, rows=24)
    old.receive_bytes()
    new = open_term(client, name, cols=80, rows=24)
    new.receive_bytes()
    old.__exit__(None, None, None)
    time.sleep(0.5)
    assert _session_status(name) == "off", "old socket's cleanup re-showed the bar"
    new.__exit__(None, None, None)
    _wait_for(lambda: _session_status(name), "", "status after last disconnect")


def _close_code(ws, timeout=5.0):
    """Drain output until the server closes the socket; return its close code.

    The test client's receive() has no timeout and hands back the close as a
    message rather than raising, so read in a thread and give up after timeout.
    """
    import threading
    result = {}

    def drain():
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                result["code"] = msg.get("code")
                return

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    t.join(timeout)
    assert "code" in result, "server never closed the socket"
    return result["code"]


def test_taken_over_says_so_instead_of_dropping(client, session_name):
    """A second attach -d ends the first client. The page must hear that as
    "taken over" (no reattach), not a dropped line, or two open pages kick
    each other off every second."""
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    first = open_term(client, name, cols=80, rows=24)
    assert first.receive_bytes()
    second = open_term(client, name, cols=80, rows=24)
    assert second.receive_bytes()
    assert _close_code(first) == 4409
    second.close()


def test_killed_session_says_it_is_gone(client, session_name):
    name = session_name()
    tmux("new-session", "-d", "-s", name)
    ws = open_term(client, name, cols=80, rows=24)
    assert ws.receive_bytes()
    tmux("kill-session", "-t", f"={name}")
    assert _close_code(ws) == 4410
