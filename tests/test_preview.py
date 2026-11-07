"""A deck card shows the last few meaningful lines of its pane, so you can see
what an agent said — or what it is asking — without attaching.

The hard part is that agent TUIs end with chrome: an input box and a key-hint
line. Taking the literal last lines of the pane shows you a box outline and
nothing else, so preview_lines has to skip past the box — while still showing
what is *inside* it when that is a permission question rather than an empty
prompt.
"""
from app import preview_lines


def test_skips_the_bottom_input_box():
    """grok/Claude draw an input box last. The interesting text is above it."""
    screen = (
        "I rewrote the parser and the suite is green.\n"
        "╭──────────────────────────────────────╮\n"
        "│ ❯                                    │\n"
        "╰──────────────────────────────────────╯\n"
        "  ? for shortcuts\n"
    )
    assert preview_lines(screen) == ["I rewrote the parser and the suite is green."]


def test_shows_a_question_asked_inside_the_box():
    """A permission prompt lives inside the box. Skipping the box entirely
    would hide the one thing you most need to see from the deck."""
    screen = (
        "Ready to run the migration.\n"
        "╭──────────────────────────────────────╮\n"
        "│ Run `git push --force`?              │\n"
        "│ ❯ 1. Yes                             │\n"
        "│   2. No                              │\n"
        "╰──────────────────────────────────────╯\n"
        "  Esc to cancel\n"
    )
    assert preview_lines(screen) == ["Run `git push --force`?", "❯ 1. Yes", "2. No"]


def test_a_bare_prompt_is_not_content():
    """An empty `❯` inside the box is chrome, not something the agent said."""
    screen = "the answer is 42\n╭────────────╮\n│ ❯          │\n╰────────────╯\n"
    assert preview_lines(screen) == ["the answer is 42"]


def test_plain_shell_has_no_box():
    """A shell prompt draws no box; fall back to the last real lines."""
    screen = "$ ls\nREADME.md  server  tests\n$ \n"
    assert preview_lines(screen) == ["$ ls", "README.md  server  tests", "$"]


def test_drops_chrome_only_lines():
    """Rules, scrollbars and blank lines carry no information."""
    screen = "real text\n\n────────────────\n   ▐\n█████\n"
    assert preview_lines(screen) == ["real text"]


def test_returns_at_most_n_lines():
    screen = "".join(f"line {i}\n" for i in range(20))
    assert preview_lines(screen, 3) == ["line 17", "line 18", "line 19"]
    assert preview_lines(screen, 1) == ["line 19"]


def test_collapses_right_aligned_columns():
    """TUIs pad with runs of spaces to right-align; on a phone that reads as a
    gap wide enough to push the real text off the card."""
    assert preview_lines("done            12:22 PM\n") == ["done  12:22 PM"]


def test_truncates_a_very_long_line():
    assert len(preview_lines("x" * 500)[0]) == 160


def test_empty_pane_is_empty_not_an_error():
    assert preview_lines("") == []
    assert preview_lines("\n\n\n") == []


def test_api_includes_a_preview(client, auth, session_name):
    name = session_name()
    from conftest import tmux
    tmux("new-session", "-d", "-s", name)
    sessions = client.get("/api/sessions", headers=auth).json()["sessions"]
    s = next(x for x in sessions if x["name"] == name)
    assert isinstance(s["preview"], list)
    assert all(isinstance(l, str) for l in s["preview"])


def test_preview_of_a_dead_session_is_empty(client, auth):
    """capture-pane fails for a name that is not there; that must not 500."""
    from app import _preview
    assert _preview("gdtest-no-such-session") == []
