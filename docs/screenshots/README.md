# Screenshots

The main README shows three images from this folder. Take them on a phone
(portrait, about 390x844 CSS px) against a throwaway tmux server, so no real
session names, paths, host names or pane output appear:

- `deck.png`: the deck with three or four demo sessions, one amber.
- `attached.png`: attached to a session, with the key bar visible.
- `new-session.png`: the ＋ dialog with a demo project picked.

A throwaway tmux server: export the same `TMUX_TMPDIR` (for example
`/tmp/couchdeck-demo`) in the shell that runs `scripts/run.sh` and in the shell
where you run `tmux new-session -d -s demo-claude` and so on. Point
`PROJECTS_ROOT` at a folder of empty demo projects. Strip image metadata
(EXIF) before committing.
