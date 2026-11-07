#!/usr/bin/env bash
# Start couchdeck. Reads ./.env (next to this repo's README) if present.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$here/.env" ]]; then
  set -a; . "$here/.env"; set +a
fi
bind="${COUCHDECK_BIND:-127.0.0.1}"
if [[ "$bind" == "tailscale" ]]; then
  bind="$(tailscale ip -4 | head -n1)"
  [[ -n "$bind" ]] || { echo "couchdeck: no Tailscale IPv4 address" >&2; exit 1; }
fi
py="${COUCHDECK_PYTHON:-$here/venv/bin/python}"
if [[ -x "$py" ]]; then py="$(realpath "$py")"; else py=python3; fi
cd "$here/server"
exec "$py" -m uvicorn app:app --host "$bind" --port "${COUCHDECK_PORT:-3320}"
