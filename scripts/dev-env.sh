#!/usr/bin/env bash
# Create a local dev venv (.venv) on top of the Hermes interpreter so tests can
# import gateway.*, aiohttp, etc. Usage: scripts/dev-env.sh
#   HERMES_PYTHON     interpreter to base the venv on (default: newest Hermes
#                     install venv python that can import aiohttp)
#   HERMES_AGENT_DIR  Hermes source checkout added to sys.path via a .pth file
#                     (default: ~/.hermes/hermes-agent)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

resolve_python() {
  if [[ -n "${HERMES_PYTHON:-}" ]]; then
    if ! "$HERMES_PYTHON" -c 'import aiohttp' >/dev/null 2>&1; then
      echo "HERMES_PYTHON=$HERMES_PYTHON cannot import aiohttp" >&2
      return 1
    fi
    echo "$HERMES_PYTHON"
    return 0
  fi
  local candidate
  # newest first by mtime
  while IFS= read -r candidate; do
    [[ -x "$candidate" ]] || continue
    if "$candidate" -c 'import aiohttp' >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done < <(ls -1t "$HOME"/.hermes/installs/*/environments/*/venv/bin/python3 2>/dev/null || true)
  echo "No Hermes interpreter with aiohttp found; set HERMES_PYTHON" >&2
  return 1
}

HERMES_PYTHON="$(resolve_python)"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
if [[ ! -d "$HERMES_AGENT_DIR/gateway" ]]; then
  echo "HERMES_AGENT_DIR=$HERMES_AGENT_DIR does not look like a Hermes checkout" >&2
  exit 1
fi

echo "Using HERMES_PYTHON=$HERMES_PYTHON"
echo "Using HERMES_AGENT_DIR=$HERMES_AGENT_DIR"

"$HERMES_PYTHON" -m venv --system-site-packages .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet pytest pytest-asyncio

site_packages="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# HERMES_PYTHON is usually itself a venv, so --system-site-packages only
# exposes the *base* interpreter's packages. Add the Hermes venv's own
# site-packages (ruamel.yaml, aiohttp, ...) after the agent checkout.
hermes_site="$("$HERMES_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n%s\n' "$HERMES_AGENT_DIR" "$hermes_site" > "$site_packages/hermes.pth"

.venv/bin/python -c 'import gateway.platforms.base, aiohttp, pytest' \
  && echo "dev env ready: .venv"
