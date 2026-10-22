#!/bin/sh
set -e

INSTALL_URL="${PENSERO_LITELLM_INSTALL_URL:-https://raw.githubusercontent.com/Pensero/pensero-litellm/main/install.py}"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH"
  else
    echo "uv is not installed. Installing it from $UV_INSTALLER_URL into ~/.local/bin ..."
    curl -fsSL "$UV_INSTALLER_URL" | env INSTALLER_NO_MODIFY_PATH=1 UV_NO_MODIFY_PATH=1 sh
    PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
      echo "uv is not on PATH after the install. Install it (https://docs.astral.sh/uv/) and run this again." >&2
      exit 1
    fi
  fi
fi
export PATH
[ -n "${UV_NO_MANAGED_PYTHON:-}" ] || export UV_MANAGED_PYTHON=1

# Piped into sh, stdin is this script, so the prompts read from the terminal when there is one.
if [ ! -t 0 ] && ( : < /dev/tty ) 2>/dev/null; then
  exec uv run -q "$INSTALL_URL" "$@" < /dev/tty
fi
exec uv run -q "$INSTALL_URL" "$@"
