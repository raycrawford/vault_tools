#!/usr/bin/env bash
# Vault MCP server setup: run once per machine (macOS or Linux), and again any
# time the server stops connecting. Safe to re-run.
#
#   bash .tools/setup.sh
#
# 1. Creates a Python virtualenv OUTSIDE the vault (~/.local/share/vault-mcp)
#    and installs the `mcp` package into it.
# 2. Builds this machine's search index (~/.cache/vault-mcp/).
# 3. Starts the server once and checks it answers an MCP handshake.
# 4. Registers it as "vault" with Claude Code (user scope) and Claude Desktop.
#    Claude Desktop must be fully quit (Cmd-Q) and reopened afterwards.

set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$TOOLS_DIR/vault-mcp-server.py"
VENV="${VAULT_MCP_VENV:-$HOME/.local/share/vault-mcp/venv}"
PY="$VENV/bin/python"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Python environment ─────────────────────────────────────────────────────

find_python() {
  # mcp needs Python 3.10+. macOS's /usr/bin/python3 is 3.9, so look for others.
  for c in python3.13 python3.12 python3.11 python3.10 \
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
elif BASE_PY="$(find_python)"; then
  echo "Using $BASE_PY ($("$BASE_PY" --version))"
  mkdir -p "$(dirname "$VENV")"
  "$BASE_PY" -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet 'mcp<2'
elif command -v uv >/dev/null 2>&1; then
  echo "No Python 3.10+ found; using uv to provide one."
  uv venv --quiet --python 3.12 "$VENV"
  uv pip install --quiet --python "$PY" 'mcp<2'
else
  fail "Need Python 3.10 or newer. Install one with 'brew install python@3.12'
       (or install uv: https://docs.astral.sh/uv/), then re-run this script."
fi
"$PY" -c 'from mcp.server.fastmcp import FastMCP' || fail "the mcp package did not install (proxy/VPN blocking pip?)"

# ── 2. Index ──────────────────────────────────────────────────────────────────

step "Building search index"
"$PY" "$TOOLS_DIR/vault_index.py"

# ── 3. Handshake test ─────────────────────────────────────────────────────────

step "Testing the server"
"$PY" - "$PY" "$SERVER" <<'EOF'
import json, subprocess, sys
py, server = sys.argv[1], sys.argv[2]
proc = subprocess.Popen([py, server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
if 2 not in replies:
    print("Server did not answer. Its output was:\n" + (err or out or "(nothing)"))
    sys.exit(1)
print(replies[2]["result"]["content"][0]["text"])
EOF

# ── 4. Register with Claude ───────────────────────────────────────────────────

step "Registering with Claude Code"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove vault --scope user >/dev/null 2>&1 || true
  claude mcp add vault --scope user -- "$PY" "$SERVER"
else
  echo "Claude Code not found on PATH; skipped."
fi

step "Registering with Claude Desktop"
case "$(uname)" in
  Darwin) DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
  *)      DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json" ;;
esac
if [ -d "$(dirname "$DESKTOP_CONFIG")" ]; then
  "$PY" - "$DESKTOP_CONFIG" "$PY" "$SERVER" <<'EOF'
import json, shutil, sys
from pathlib import Path
config_path, py, server = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
config = {}
if config_path.exists():
    shutil.copy(config_path, config_path.with_suffix(".json.bak"))
    config = json.loads(config_path.read_text() or "{}")
servers = config.setdefault("mcpServers", {})
servers["vault"] = {"command": py, "args": [server]}
config_path.write_text(json.dumps(config, indent=2) + "\n")
print(f"Wrote 'vault' to {config_path} (backup: .json.bak)")
others = [n for n in servers if n != "vault"]
if others:
    print("Other MCP servers in that file: " + ", ".join(others))
    print("  If any of these are old copies of this server (e.g. 'vault-search'),")
    print("  delete them from the file: they will keep failing to connect.")
EOF
  echo "Now fully quit Claude Desktop (Cmd-Q) and reopen it."
else
  echo "Claude Desktop config folder not found; skipped."
fi

step "Done"
echo "Try asking Claude: \"vault stats\" or \"search my vault for <client name>\""
