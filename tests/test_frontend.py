"""Static analysis of the single-page UI.

Any script execution in this origin can attach a shell. tmux session names are
attacker-influenced (anything on the box can create one) and are rendered into
the tab strip.
"""
import re
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parent.parent / "server" / "static" / "index.html").read_text()


def test_session_names_are_not_injected_as_html():
    """`tab.innerHTML = ... + s.name` is stored XSS: a session named
    `<img src=x onerror=...>` executes on every page load, and the payload can
    attach a shell from this origin.

    Fix: build the dot with createElement and set the label via textContent.
    """
    for line in INDEX.splitlines():
        if "innerHTML" in line and "s.name" in line:
            pytest.fail(f"session name concatenated into innerHTML: {line.strip()}")


def test_no_innerhtml_with_interpolation():
    """Any innerHTML fed a template literal or concatenation is suspect."""
    offenders = [
        l.strip() for l in INDEX.splitlines()
        if re.search(r"innerHTML\s*=.*(\$\{|\+\s*\w)", l)
    ]
    assert not offenders, f"dynamic innerHTML sinks: {offenders}"


def test_no_remote_script_or_style_sources():
    """xterm.js is vendored deliberately — a CDN here would put shell access
    behind someone else's supply chain."""
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', INDEX)
    assert not remote, f"remote asset references: {remote}"


def test_no_eval_or_function_constructor():
    for pattern in (r"\beval\s*\(", r"\bnew\s+Function\s*\("):
        assert not re.search(pattern, INDEX), f"dynamic code execution: {pattern}"


def test_token_is_not_placed_in_a_url():
    """A token in a query string lands in logs, history, and Referer headers."""
    assert not re.search(r"[?&]token=", INDEX)


def test_light_is_the_default_theme():
    """Preference, but also a rendering rule: the default must match the :root
    palette, since the theme attribute is only set once the script at the end
    of <body> runs. A mismatch means a flash of the wrong theme on every load.
    """
    assert re.search(r"getItem\('gd_theme'\)\s*\|\|\s*'light'", INDEX), \
        "default theme is not light"
    root = re.search(r":root\s*\{(.*?)\}", INDEX, re.S).group(1)
    assert "#faf9f5" in root, ":root must carry the light palette, not dark"


def test_cli_guide_separates_claude_project_files_from_auto_memory():
    """The guide is static HTML. It must say Grok loads CLAUDE.md and must
    name the auto-memory folder Grok does not load."""
    assert 'id="help"' in INDEX
    assert "CLAUDE.md" in INDEX
    assert "~/.claude/projects/" in INDEX
    assert "data-help=\"agy\"" in INDEX


def test_pending_command_is_text_not_html_and_lands_before_attach():
    """The command comes from pane text. It must reach the compose box as a
    string assignment, before attach reads drafts, and never as HTML."""
    fn = INDEX[INDEX.index("async function openShell"):]
    fn = fn[:fn.index("document.getElementById('shellBtn')")]
    assert "drafts[data.name] = data.pending" in fn
    assert fn.index("drafts[data.name]") < fn.index("attach(")
    assert "innerHTML" not in fn
    assert "sessDlgCmd.textContent = pending" in INDEX


def test_terminal_fit_uses_content_box_not_fitaddon():
    """Regression: Android Chrome + tmux status bar.

    FitAddon measures getComputedStyle(#term).height, which can be one row
    taller than the visible content box once the soft keyboard is up. That
    extra row is where tmux draws its green status bar. fitTerminal must size
    from clientHeight/clientWidth (and debounce visualViewport).
    """
    assert 'fitTerminal' in INDEX, 'expected a fitTerminal helper'
    assert 'termEl.clientHeight' in INDEX, 'fit must use the content box height'
    assert 'termEl.clientWidth' in INDEX, 'fit must use the content box width'
    assert 'fitTimer' in INDEX, 'keyboard visualViewport events must be debounced'
    assert 'padding: 0 0 12px' in INDEX or re.search(
        r'#term\s*\{[^}]*padding:\s*0\s+0\s+12px', INDEX
    ), '#term should keep a small bottom gap above the key bar'
    assert 'padY' in INDEX, 'fit must subtract #term padding when counting rows'


def test_finger_slide_keeps_viewport_fixes():
    """Sliding on grok painted overlapping garbage. The real causes were the
    stale PTY size (no ctty) and refitting on visualViewport *scroll*; the
    latter must stay fixed. Which panes get wheel events is decided server-side
    from pane state (test_websocket.py), not by a command-name list here.
    """
    assert 'TUI_CMD' not in INDEX
    assert 'pinToVisualViewport' in INDEX
    assert "addEventListener('scroll', pinToVisualViewport)" in INDEX
    assert 'touch-action: none' in INDEX


def test_fit_waits_for_real_term_box():
    """Tablet bug: first fit at 0 height became rows=1 and crushed grok to '>'."""
    assert 'fitRetry' in INDEX
    assert 'availH < 40' in INDEX
    assert 'ResizeObserver' in INDEX
    assert 'Math.max(2, Math.floor(availH' in INDEX


def test_help_does_not_tear_down_a_live_attach():
    """Opening Help/CLIs must not call showDeck() (that closes the WebSocket)."""
    fn = INDEX[INDEX.index("function showHelp"):INDEX.index("function hideHelp")]
    # Ignore the explanatory comment that mentions showDeck().
    code = "\n".join(l for l in fn.splitlines() if not l.strip().startswith("//"))
    assert "showDeck()" not in code
    assert "helpReturn" in INDEX


def test_deck_patches_cards_in_place():
    """Polling must update existing cards by name, not wipe and rebuild."""
    fn = INDEX[INDEX.index("async function loadSessions"):]
    fn = fn[:fn.index("\nfunction ")]
    assert "querySelectorAll('.card').forEach" not in fn
    assert "CSS.escape(s.name)" in fn or "CSS.escape(s.name)" in INDEX
    assert "card.remove()" in fn  # only for sessions that disappeared


def test_pending_badge_is_visible_on_cards():
    assert "badge.hidden = !s.pending" in INDEX or "badge.hidden = !s.pending" in INDEX
    assert ".badge" in INDEX


def test_dual_attach_confirm_before_detach():
    assert "Detach other viewer?" in INDEX
    fn = INDEX[INDEX.index("async function requestAttach"):INDEX.index("function attach")]
    assert "detachDlg.showModal()" in fn
    assert "/api/sessions" in fn, "attached flag must be re-read before attach -d"


def test_long_press_does_not_also_attach():
    fn = INDEX[INDEX.index("async function loadSessions"):]
    fn = fn[:fn.index("\nfunction ")]
    assert "dataset.long = '1'" in fn
    assert "dataset.long === '1'" in fn


def test_escape_closes_help_and_status_stays_hidden():
    assert "e.key !== 'Escape'" in INDEX
    assert "hideHelp()" in INDEX
    set_status = INDEX[INDEX.index("function setStatus"):INDEX.index("let detachTarget")]
    assert "helpEl.hidden" in set_status


def test_reconnect_pauses_while_tab_hidden():
    assert "visibilityState === 'hidden'" in INDEX
    assert "visibilitychange" in INDEX


def test_create_dialog_has_project_search():
    """Long project lists need a search box; filter must not use innerHTML."""
    assert 'id="cProjectFilter"' in INDEX
    assert 'function filterProjects()' in INDEX
    assert 'type="search"' in INDEX
    # Filter toggles option.hidden; never rebuilds with innerHTML.
    fn = INDEX[INDEX.index("function filterProjects()"):]
    fn = fn[:fn.index("\n}")]
    assert "innerHTML" not in fn


def test_deck_has_session_search():
    """Search hides cards in place and survives the 5s poll."""
    assert 'id="deckFilter"' in INDEX
    fn = INDEX[INDEX.index("function filterDeck()"):]
    fn = fn[:fn.index("\n}")]
    assert "innerHTML" not in fn
    assert "card.hidden" in fn
    poll = INDEX[INDEX.index("async function loadSessions"):]
    poll = poll[:poll.index("\nfunction ")]
    assert "filterDeck()" in poll, "a poll must re-apply the filter to new cards"
    assert "card.dataset.search" in poll


def test_page_carries_no_tailnet_names_or_addresses():
    """The page ships publicly. Per-person notes belong in launchers.json."""
    assert not re.search(r"[\w-]+\.ts\.net", INDEX), "a MagicDNS name in the page"
    for ip in re.findall(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", INDEX):
        assert ip == "100.64.0.7", f"a real-looking Tailscale address in the page: {ip}"


def test_page_carries_none_of_your_private_words():
    """Names that must never ship (accounts, hosts) live outside the repo,
    one per line, so this test can name them without publishing them."""
    import os
    path = Path(os.environ.get("COUCHDECK_PRIVATE_WORDS",
                               Path.home() / ".config" / "couchdeck" / "private-words.txt"))
    if not path.is_file():
        pytest.skip(f"no {path}")
    words = [w.strip() for w in path.read_text().splitlines() if w.strip() and not w.startswith("#")]
    found = [w for w in words if w.lower() in INDEX.lower()]
    assert not found, f"private words in index.html: {found}"


def test_launcher_notes_are_text_not_html():
    fn = INDEX[INDEX.index("async function renderLauncherNotes"):]
    fn = fn[:fn.index("\n}\n")]
    assert "textContent = l.note" in fn and "textContent = l.label" in fn
    assert "innerHTML" not in fn


def test_key_bar_can_hide_the_phone_keyboard():
    # Phones have no "close keyboard" API: blurring the focused field is the
    # only way. The button sits in the key bar, whose mousedown is cancelled so
    # the tap doesn't steal focus before the handler reads it.
    bar = INDEX[INDEX.index('<div id="ctrlbar"'):INDEX.index("</div>", INDEX.index('<div id="ctrlbar"'))]
    assert 'id="hideKbBtn"' in bar
    handler = INDEX[INDEX.index("hideKbBtn.onclick"):]
    handler = handler[:handler.index("};")]
    assert "document.activeElement.blur()" in handler
    assert "focusInput()" in handler  # second tap brings it back
    assert "addEventListener('focusin', syncKbBtn)" in INDEX


def test_taken_over_page_does_not_reattach():
    onclose = INDEX[INDEX.index("ws.onclose = (ev) =>"):]
    onclose = onclose[:onclose.index("reconnectTimer = setTimeout")]
    # Both codes return before the reconnect timer is armed.
    for code in ("4409", "4410"):
        branch = onclose[onclose.index(f"ev.code === {code}"):]
        assert "closing = true" in branch[:branch.index("return;")]
