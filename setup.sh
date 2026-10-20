#!/usr/bin/env bash
# Vault MCP server setup: run once per vault per machine (macOS or Linux), and
# again any time a server stops connecting. Safe to re-run.
#
#   bash setup.sh <name> <vault path> [--writable]
#   bash setup.sh work     ~/repos/notes-unisys
#   bash setup.sh personal ~/repos/notes-personal --writable
#
# Vaults are read-only unless --writable is given. Re-run without it to turn
# writing back off.
#
# 1. Creates ONE shared Python virtualenv (~/.local/share/vault-mcp/venv) and
#    installs 'mcp<2' into it.
# 2. Builds this vault's search index (~/.cache/vault-mcp/).
# 3. Starts the server once and checks it answers an MCP handshake.
# 4. Registers it as "vault-<name>" with Claude Code (user scope) and Claude
#    Desktop, replacing only that entry. Claude Desktop must be fully quit
#    (Cmd-Q) and reopened afterwards.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$HERE/vault-mcp-server.py"
VENV="${VAULT_MCP_VENV:-$HOME/.local/share/vault-mcp/venv}"
PY="$VENV/bin/python"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

WRITABLE=""
if [ $# -eq 3 ] && [ "$3" = "--writable" ]; then WRITABLE="--writable"; set -- "$1" "$2"; fi
[ $# -eq 2 ] || fail "usage: bash setup.sh <name> <vault path> [--writable]
       e.g. bash setup.sh work ~/repos/notes-unisys"
NAME="$1"
case "$NAME" in
  ''|*[!A-Za-z0-9_-]*) fail "name must contain only letters, digits, '-' and '_' (got '$NAME')" ;;
esac
[ -d "$2" ] || fail "not a directory: $2"
VAULT="$(cd "$2" && pwd)"
ENTRY="vault-$NAME"
MODE="read-only"; [ -n "$WRITABLE" ] && MODE="read-write"

# ── 1. Python environment ─────────────────────────────────────────────────────

find_python() {
  # mcp needs Python 3.10+. macOS's /usr/bin/python3 is 3.9, so look for others.
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

step "Python environment: $VENV"
if [ -x "$PY" ] && "$PY" -c 'from mcp.server.fastmcp import FastMCP' 2>/dev/null; then
  echo "Already set up ($("$PY" --version))."
else
  [ -e "$VENV" ] && echo "Existing venv is broken or has mcp 2.x; rebuilding it."
  if BASE_PY="$(find_python)"; then
    echo "Using $BASE_PY ($("$BASE_PY" --version))"
    mkdir -p "$(dirname "$VENV")"
    "$BASE_PY" -m venv --clear "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet 'mcp<2'
  elif command -v uv >/dev/null 2>&1; then
    echo "No Python 3.10+ found; using uv to provide one."
    mkdir -p "$(dirname "$VENV")"
    uv venv --quiet --clear --python 3.12 "$VENV"
    uv pip install --quiet --python "$PY" 'mcp<2'
  else
    fail "Need Python 3.10 or newer. Install one with 'brew install python@3.12'
       (or install uv: https://docs.astral.sh/uv/), then re-run this script."
  fi
fi
"$PY" -c 'from mcp.server.fastmcp import FastMCP' || fail "the mcp package did not install (proxy/VPN blocking pip?)"

# ── 2. Index ──────────────────────────────────────────────────────────────────

step "Building search index for $NAME"
"$PY" "$HERE/vault_index.py" --vault "$VAULT"

# ── 3. Handshake test ─────────────────────────────────────────────────────────

step "Testing the server"
"$PY" - "$PY" "$SERVER" "$VAULT" "$NAME" $WRITABLE <<'EOF'
import json, subprocess, sys
py, server, vault, name, *extra = sys.argv[1:]
proc = subprocess.Popen([py, server, "--vault", vault, "--name", name, *extra],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "setup", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "vault_stats", "arguments": {}}},
]
try:
    out, err = proc.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=60)
except subprocess.TimeoutExpired:
    proc.kill(); out, err = proc.communicate()
replies = {}
for line in out.splitlines():
    try:
        msg = json.loads(line)
        replies[msg.get("id")] = msg
    except ValueError:
        pass
if 2 not in replies or "result" not in replies[2]:
    print("Server did not answer. Its output was:\n" + (err or out or "(nothing)"))
    sys.exit(1)
print("Server name: " + replies[1]["result"]["serverInfo"]["name"])
print(replies[2]["result"]["content"][0]["text"])
print("\nHandshake OK.")
EOF

# ── 4. Register with Claude ───────────────────────────────────────────────────

step "Registering $ENTRY with Claude Code ($MODE)"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove "$ENTRY" --scope user >/dev/null 2>&1 || true
  claude mcp add "$ENTRY" --scope user -- "$PY" "$SERVER" --vault "$VAULT" --name "$NAME" $WRITABLE
else
  echo "The 'claude' CLI is not on PATH, so Claude Code was NOT registered."
  echo "Install it, then re-run this script, or register by hand:"
  echo "  claude mcp add $ENTRY --scope user -- \"$PY\" \"$SERVER\" --vault \"$VAULT\" --name \"$NAME\" $WRITABLE"
fi

step "Registering $ENTRY with Claude Desktop"
case "$(uname)" in
  Darwin) DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
  *)      DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json" ;;
esac
if [ -d "$(dirname "$DESKTOP_CONFIG")" ]; then
  "$PY" - "$DESKTOP_CONFIG" "$ENTRY" "$PY" "$SERVER" "$VAULT" "$NAME" $WRITABLE <<'EOF'
import json, shutil, sys
from pathlib import Path
config_path, entry, py, server, vault, name, *extra = Path(sys.argv[1]), *sys.argv[2:]
config = {}
if config_path.exists():
    shutil.copy(config_path, config_path.with_suffix(".json.bak"))
    config = json.loads(config_path.read_text() or "{}")
servers = config.setdefault("mcpServers", {})
servers[entry] = {"command": py, "args": [server, "--vault", vault, "--name", name, *extra]}
config_path.write_text(json.dumps(config, indent=2) + "\n")
print(f"Wrote '{entry}' to {config_path} (backup: .json.bak)")
others = [n for n in servers if n != entry]
if others:
    print("Other MCP servers in that file: " + ", ".join(others))
stale = [n for n in ("vault", "vault-search") if n in servers]
if stale:
    print("WARNING: stale entries from earlier attempts: " + ", ".join(stale))
    print("  They are not touched. If they are old copies of this server they will keep")
    print("  failing or pointing at old code; remove them from the file yourself.")
EOF
  echo "Now fully quit Claude Desktop (Cmd-Q) and reopen it."
else
  echo "Claude Desktop config folder not found; skipped."
fi

step "Done"
echo "Try asking Claude: \"vault stats\" or \"search my $NAME vault for <topic>\""
