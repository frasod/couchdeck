# Contributing to couchdeck

Thanks for helping. Bug reports, phone/browser compatibility reports and small
focused pull requests are all welcome.

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on
  the shape before you spend time on it.
- couchdeck deliberately stays small: one FastAPI file, one HTML page, vendored
  xterm.js, no build step, no database. Changes that add a frontend build,
  a login system or a container are worth discussing first.
- **Security issues go through [SECURITY.md](SECURITY.md), not public issues.**

## Development setup

```bash
git clone https://github.com/OWNER/couchdeck.git
cd couchdeck
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m pytest tests/ -q
```

The suite needs `tmux` on `PATH`. It talks to **your default tmux server**:
it creates and kills sessions whose names start with `gdtest`, and leaves every
other session alone. If you would rather keep it fully apart, run it with a
private socket directory:

```bash
TMUX_TMPDIR="$(mktemp -d)" ./venv/bin/python -m pytest tests/ -q
```

To try the UI locally, `cp .env.example .env` and run `./scripts/run.sh`, then
open `http://127.0.0.1:3320`. Browser dev tools' device mode is fine for layout;
keyboard and IME behaviour only show up on a real phone.

## What a good change looks like

- **Tests assert on tmux, not just HTTP.** Several real bugs returned `200`
  while nothing reached the pane. If you change how commands, keys or sizes
  reach tmux, assert on `tmux capture-pane` / `display-message` output.
- **Untrusted text stays text.** Session names, pane contents and launcher
  labels are rendered with `textContent` / `createTextNode`, never
  `innerHTML`. `tests/test_frontend.py` checks this for the known spots.
- **tmux targets use exact match**: `=name` for sessions, `=name:` for panes
  (`send-keys`, `capture-pane`).
- **Anything typed into a shell is quoted** with `shlex.quote`.
- **No personal data in the page or tests**: no real host names, tailnet
  names, IPs, account names or paths. Use `example`, `100.64.0.7` and
  `/home/<user>` style placeholders. The test suite checks `index.html` for
  `*.ts.net` names and Tailscale addresses, and for any words you list in
  `~/.config/couchdeck/private-words.txt` (one per line, kept outside the repo).
- Keep the vendored xterm.js files unmodified; update them as a whole and note
  the version in `THIRD_PARTY_NOTICES.md`.

## Pull requests

- One topic per PR, with a short description of the problem and how you
  verified the fix (which phone/browser, if it is a UI change).
- `pytest` must pass.
- Commit messages: an imperative summary line, then a body saying *why*.
- By contributing you agree your work is released under the
  [MIT License](LICENSE).
