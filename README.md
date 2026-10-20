# vault-tools

A small, read-only [MCP](https://modelcontextprotocol.io) server that lets Claude Desktop and Claude Code search and read Obsidian vaults. One copy of the code serves any number of vaults. Each vault gets its own registration (`vault-work`, `vault-personal`, ...) and its own search index.

The notes can be confidential, so this is a local stdio server. Nothing leaves the machine, and by default the server never writes to a vault. Writing is an opt-in, per-vault setting (see [Enabling writes](#enabling-writes)).

## Prerequisites

- **macOS** (Linux works too).
- **Python 3.10 or newer, or [`uv`](https://docs.astral.sh/uv/).** The `mcp` package needs 3.10+, and macOS's built-in `/usr/bin/python3` is 3.9. `setup.sh` looks for `python3.14` … `python3.10`, then Homebrew's `python3`, then plain `python3`, and falls back to `uv` if none is 3.10+. Easiest fix if you have neither: `brew install python@3.12`.
- **The `claude` CLI (Claude Code)** on your PATH. If it's missing, `setup.sh` skips Claude Code registration, prints the exact `claude mcp add` command to run later, and still registers with Claude Desktop.
- **Claude Desktop**, if you want it there. Setup skips it if its config folder doesn't exist.

## Setup

Clone this repo anywhere, then run one command per vault. The vault can be at any path, which can differ between your Macs:

```bash
bash setup.sh work     ~/repos/notes-unisys
bash setup.sh personal ~/repos/notes-personal
# e.g. a vault in OneDrive:
bash setup.sh work ~/Library/CloudStorage/OneDrive-Unisys/repos/notes-unisys
```

`setup.sh <name> <vault path>` registers a server called `vault-<name>`. It:

1. Creates one shared virtualenv at `~/.local/share/vault-mcp/venv` (`VAULT_MCP_VENV` overrides it) and installs `'mcp<2'`. A venv that can't `from mcp.server.fastmcp import FastMCP` is treated as broken and rebuilt.
2. Builds the vault's search index.
3. Starts the server and runs a handshake (`initialize`, `notifications/initialized`, `tools/call vault_stats`), then prints the result. If it fails, nothing is registered.
4. Runs `claude mcp add vault-<name> --scope user`, replacing that entry only.
5. Merges `vault-<name>` into Claude Desktop's config, after backing it up to `claude_desktop_config.json.bak`. Other entries (for example `onedrive`) are kept. It warns about stale `vault` or `vault-search` entries from earlier attempts but doesn't delete them.

Then **quit Claude Desktop completely with Cmd-Q and reopen it.** It only reads its config at startup.

Re-running the same command is safe. It replaces only that vault's entry. Run it again after moving a vault, or whenever a server stops connecting.

Check that it worked:

```bash
claude mcp list        # vault-work and vault-personal should show ✔ Connected
```

### Enabling writes

Vaults are read-only unless you ask otherwise. Add `--writable` to the setup command for each vault that should accept writes:

```bash
bash setup.sh personal ~/repos/notes-personal --writable   # read-write
bash setup.sh work     ~/repos/notes-unisys                # stays read-only
```

Then Cmd-Q and reopen Claude Desktop. Re-run the command without `--writable` to turn writing off again. `vault_stats` shows `Mode: read-only` or `Mode: read-write`. Without the flag, the write tools aren't registered at all, so a read-only vault can't be changed by this server.

### Adding a third vault

```bash
bash setup.sh research ~/repos/notes-research
```

That creates `vault-research` with its own index. Nothing else needs to change.

### Running by hand

```bash
VENV=~/.local/share/vault-mcp/venv/bin/python
$VENV vault_index.py --vault ~/repos/notes-unisys [--rebuild]
$VENV vault-mcp-server.py --vault ~/repos/notes-unisys --name work
```

The vault comes from `--vault PATH` or the `VAULT_ROOT` environment variable, and the label from `--name` or `VAULT_NAME`. The server exits with a clear message if the vault is missing or isn't a directory.

## How it works

- **One server per vault.** The server name is `vault-<name>`, and every tool description says which vault it covers, for example "Search the WORK vault (notes-unisys)". That lets Claude choose the right server when both are enabled.
- **Indexes live outside the repos**, at `~/.cache/vault-mcp/<vault-folder>-<sha1(path)[:8]>.db` (`VAULT_INDEX` overrides it). Each vault and each machine has its own. A SQLite database inside a git or OneDrive folder can get corrupted, so no index ever goes in a vault or in this repo, and `.gitignore` excludes `*.db`, `*.db-wal`, `*.db-shm` and `.venv/`.
- **No manual indexing.** Before answering, the server runs an incremental refresh, at most once every 60 seconds. It only re-reads notes whose modified time or size changed. A full build of about 2,500 notes takes about 2 seconds.
- **Standard library only, apart from `mcp`.** The frontmatter parser is built in. `mcp` is pinned to `<2` because mcp 2.x renamed `FastMCP` to `MCPServer`.
- **If the index can't be built**, for example because SQLite lacks FTS5, `search_vault` falls back to a plain case-insensitive substring scan.

### What gets indexed

- **Files:** every `*.md` under the vault root, skipping dot-folders (`.git`, `.obsidian`, `.tools`, `.trash`) and the top-level folders `00 - Templates` and `Attachments`.
- **Section:** the note's top-level folder, or `(root)` for files at the vault root.
- **Title:** frontmatter `title`, otherwise the filename without its extension.
- **Date**, the first of: frontmatter `date`, `date_created` or `created` (first 10 characters); a `YYYY-MM-DD` in the filename; the file's modified time.
- **Tags:** frontmatter `tags` plus inline `#tags` in the body (nested tags like `#clients/Omnicom` work).
- **Also stored:** `status` from frontmatter, and word count.
- **Search:** FTS5 with `porter unicode61`, so words are stemmed.
- **Safeguard:** if the walk can't read anything, for example because macOS privacy settings deny access, the refresh does not remove notes from the index, so the index isn't wiped.
- Changing `SCHEMA_VERSION` rebuilds the index automatically.

## Tools

All paths in tool output are relative to the vault root.

| Tool | Arguments | Behavior |
|---|---|---|
| `search_vault` | `query`, `folder=""`, `since=""`, `limit=20` | FTS5 search ranked by `bm25` with weights title 10, tags 5, body 1. Words are ANDed, `"phrases"` stay phrases, `OR`/`NOT` pass through, and `word*` matches prefixes. `folder` is a path prefix (`Pursuits/Active`). `since` is `YYYY-MM-DD`. Each result has a snippet. |
| `read_note` | `path` | Takes a vault-relative path, an absolute path, or a bare note name (`Kayleigh Gillis`). A name prefers an exact filename match, and if several notes match it lists the candidates. Reads outside the vault and non-`.md` files are refused. Output is cut off at 100k characters. |
| `list_notes` | `folder`, `name`, `tag`, `since`, `limit=50` | Notes newest first, with date, word count, status and tags. All filters combine. |
| `vault_stats` | none | Notes, words and date range per folder, the index location, the last refresh time, and any read errors (with a hint about macOS privacy settings). |

### Write tools (only with `--writable`)

| Tool | Arguments | Behavior |
|---|---|---|
| `create_note` | `path`, `content` | Creates a new note, adding `.md` and any missing folders. Fails if the note exists, so it never overwrites. |
| `append_to_note` | `path`, `content` | Adds text to the end of an existing note, inserting a newline first if needed. |
| `edit_note` | `path`, `old_text`, `new_text` | Replaces one exact passage. Fails if `old_text` is missing or appears more than once, so a note that changed since Claude read it isn't overwritten blindly. The file is replaced atomically. |
| `move_note` | `path`, `new_path` | Moves or renames a note. Fails if the destination exists. Links in other notes are not updated. |

Guardrails: only `.md` files inside the vault; nothing in hidden folders (`.git`, `.obsidian`, `.trash`) or in `00 - Templates` and `Attachments`; no delete tool. A change is searchable immediately, because the index refreshes right after a write. Claude Desktop and Claude Code still ask for approval on tool calls unless you've auto-approved them. Keeping the vault in git lets you review writes with `git diff` and undo them with `git restore`. Edits made while Obsidian has the same note open, or while OneDrive is syncing it, can conflict.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Desktop shows "failed to connect" | The server crashed at startup. Read `~/Library/Logs/Claude/mcp-server-vault-<name>.log` (for example `mcp-server-vault-work.log`). Then re-run `setup.sh`. |
| The log says `No module named 'mcp'` or a syntax error | The config runs `python3` instead of the venv's Python. Claude Desktop does not use your shell PATH, so it finds macOS's Python 3.9. The config must use the venv's Python by absolute path, which `setup.sh` writes. Re-run it and check `command` in `claude_desktop_config.json`. |
| The log says `No module named 'mcp.server.fastmcp'` | The venv picked up mcp 2.x. Re-run `setup.sh`: it detects the broken venv and reinstalls `'mcp<2'`. |
| `vault_stats` reports "Operation not permitted" | macOS privacy is blocking access, which is likely for vaults under `~/Library/CloudStorage` (OneDrive). Allow Claude, and the terminal you run setup from, under System Settings → Privacy & Security → Files and Folders or Full Disk Access. A managed work Mac may not allow this. The server won't wipe an index it couldn't refresh. |
| A chat says no vault tools are available, or offers "Add folder" or "clone the repo" | That chat didn't load the local server. Cloud and workspace chat modes may not load servers from `claude_desktop_config.json`. Check Settings → Developer: `vault-<name>` should be **running**. Use a regular Desktop chat with the server turned on in the tools menu. claude.ai in a browser can't use local servers at all. |
| Not sure whether the server or Desktop is at fault | Run `claude mcp list`. If it shows `✔ Connected`, the server is fine and the problem is in Desktop. |
| Stale `vault` or `vault-search` entries are failing | They are left over from earlier setups. Delete them from `claude_desktop_config.json` and restart Desktop. |
| `pip install` fails on a work Mac | A corporate proxy or VPN is probably blocking downloads. Try off VPN, or set `HTTPS_PROXY`. |
| Search results seem out of date | Changes appear within 60 seconds. To rebuild everything, run `vault_index.py --vault PATH --rebuild`. |

## Ideas not yet built

- Backlinks and outgoing links (`[[wikilinks]]`) as tools, and updating links on `move_note`.
- A delete tool that moves notes to `.trash/`.
- Filtering on frontmatter fields beyond `status`.
