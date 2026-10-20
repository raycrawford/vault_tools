#!/usr/bin/env python3
"""
Obsidian Vault MCP Server
=========================
Access to this vault for Claude Desktop and Claude Code. Read-only unless the
server is started with --writable (or VAULT_WRITABLE=1).

Tools:
  search_vault  ranked full-text search (SQLite FTS5, stemmed)
  read_note     full Markdown of a note, by vault-relative path or note name
  list_notes    browse notes by folder, name, tag and date, newest first
  vault_stats   note counts per top-level folder and index status

Only with --writable:
  create_note      new note; refuses to overwrite
  append_to_note   add text to the end of a note
  edit_note        replace one exact passage in a note
  move_note        move or rename a note; refuses to overwrite

One server instance serves one vault, given by --vault PATH (or VAULT_ROOT).
--name NAME (or VAULT_NAME) labels it, e.g. "work", so Claude can tell the
servers apart when several are enabled. Writes are off unless --writable is
given, in which case the write tools are not even registered. The search index (see vault_index.py)
is refreshed automatically, so there is nothing to run by hand.

Setup (once per vault per machine):  bash setup.sh <name> <vault path>
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_index as vi  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

VAULT_ROOT      = vi.VAULT_ROOT
REFRESH_SECONDS = 60          # re-scan the vault at most once a minute
MAX_NOTE_CHARS  = 100_000


def resolve_name() -> str:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--name")
    args, _ = pre.parse_known_args(sys.argv[1:])
    raw = args.name or os.environ.get("VAULT_NAME") or VAULT_ROOT.name
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-") or "vault"


def resolve_writable() -> bool:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--writable", action="store_true")
    args, _ = pre.parse_known_args(sys.argv[1:])
    return args.writable or os.environ.get("VAULT_WRITABLE", "").lower() in ("1", "true", "yes")


NAME     = resolve_name()
WRITABLE = resolve_writable()
LABEL = f"the {NAME.upper()} vault ({VAULT_ROOT.name})"

mcp = FastMCP(
    f"vault-{NAME}",
    instructions=f"{'Read-write' if WRITABLE else 'Read-only'} access to {LABEL}. Use these tools "
                 f"only for that vault's notes; other vault-* servers cover other vaults.",
)


def vault_tool(summary: str):
    """Register a tool, prefixing its description with which vault it serves."""
    def register(fn):
        fn.__doc__ = f"{summary.format(label=LABEL)}\n{fn.__doc__}"
        return mcp.tool()(fn)
    return register


# ── Helpers ───────────────────────────────────────────────────────────────────

_last_refresh = 0.0
_last_result: dict = {}


def ensure_index() -> bool:
    """Refresh the index if it's stale. Returns False if the index is unusable."""
    global _last_refresh, _last_result
    if time.monotonic() - _last_refresh < REFRESH_SECONDS:
        return True
    try:
        _last_result = vi.refresh()
        _last_refresh = time.monotonic()
        return True
    except Exception as e:
        print(f"vault: index refresh failed: {e}", file=sys.stderr)
        _last_result = {"errors": [f"index refresh failed: {e}"]}
        return False


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(vi.INDEX_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: words are ANDed, "quoted phrases"
    stay phrases, OR/NOT pass through, and a trailing * means prefix match."""
    terms = []
    for token in re.findall(r'"[^"]+"|\S+', query):
        if token in ("OR", "NOT", "AND"):
            terms.append(token)
        elif token.startswith('"'):
            terms.append('"' + token.strip('"').replace('"', "") + '"')
        else:
            for word in re.findall(r"\w+", token):
                terms.append(f'"{word}"' + ("*" if token.endswith("*") else ""))
    while terms and terms[0] in ("OR", "NOT", "AND"):
        terms.pop(0)
    while terms and terms[-1] in ("OR", "NOT", "AND"):
        terms.pop()
    return " ".join(terms)


def folder_clause(folder: str) -> tuple[str, list]:
    folder = folder.strip().strip("/")
    if not folder:
        return "", []
    return " AND (n.path LIKE ? ESCAPE '\\')", [_like_escape(folder) + "/%"]


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_note(path: str) -> Path | str:
    """Find a note from a vault-relative path, absolute path, or note name.
    Returns the Path, or an error/ambiguity message."""
    raw = path.strip()
    candidates = [Path(raw)] if Path(raw).is_absolute() else [VAULT_ROOT / raw]
    candidates += [c.with_name(c.name + ".md") for c in candidates if c.suffix != ".md"]
    for c in candidates:
        if c.is_file():
            return c

    # Fall back to matching on note name.
    name = Path(raw).name.removesuffix(".md").lower()
    matches = [f for f in vi.iter_notes([]) if name in f.stem.lower()]
    exact = [f for f in matches if f.stem.lower() == name]
    matches = exact or matches
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return f"Note not found: {path}"
    listing = "\n".join(f"  {m.relative_to(VAULT_ROOT).as_posix()}" for m in matches[:15])
    return f"Several notes match '{path}'; pass one of these paths:\n{listing}"


def scan_search(query: str, folder: str, limit: int) -> list[dict]:
    """Plain substring search, used only when the index can't be built."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    prefix = folder.strip().strip("/").lower()
    results = []
    for f in vi.iter_notes([]):
        rel = f.relative_to(VAULT_ROOT).as_posix()
        if prefix and not rel.lower().startswith(prefix + "/"):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = list(pattern.finditer(content))
        if hits:
            m = hits[0]
            snippet = content[max(0, m.start() - 80): m.end() + 80].replace("\n", " ")
            results.append({"path": rel, "hits": len(hits), "snippet": snippet})
    results.sort(key=lambda r: r["hits"], reverse=True)
    return results[:limit]

# ── MCP tools ─────────────────────────────────────────────────────────────────

@vault_tool("Search {label}.")
def search_vault(query: str, folder: str = "", since: str = "", limit: int = 20) -> str:
    """
    Full-text search of the Obsidian vault (meeting notes, journal, pursuits,
    people, clients, solutions...). Results are ranked by relevance, with title
    and tag matches weighted above body matches; words are stemmed.

    Args:
        query:  Words to find (all must appear). Use "quoted phrases" for exact
                phrases, OR between alternatives, and word* for prefixes.
        folder: Optional folder to search within, e.g. 'Pursuits',
                'Pursuits/Active', 'People', 'Journal'.
        since:  Optional YYYY-MM-DD; only notes dated on or after it.
        limit:  Maximum results (default 20).

    Returns matching notes with vault-relative path, date, tags and an excerpt.
    Pass a path to read_note for the full text.
    """
    limit = max(1, min(limit, 100))
    match = fts_query(query)
    if not match:
        return "Empty query."

    if ensure_index():
        where, params = folder_clause(folder)
        if since:
            where += " AND n.date >= ?"
            params.append(since)
        try:
            with connect() as conn:
                rows = conn.execute(f"""
                    SELECT n.path, n.date, n.tags,
                           snippet(notes_fts, 2, '**', '**', ' … ', 32) AS excerpt
                    FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
                    WHERE notes_fts MATCH ? {where}
                    ORDER BY bm25(notes_fts, 10.0, 5.0, 1.0)
                    LIMIT ?""", [match, *params, limit]).fetchall()
        except sqlite3.OperationalError as e:
            return f"Search error for {match!r}: {e}"

        if not rows:
            return f"No results for {query!r}" + (f" in {folder}" if folder else "") + "."
        out = [f"{len(rows)} result(s) for {query!r}" + (f" in {folder}" if folder else "") + ":\n"]
        for r in rows:
            tags = f"  {r['tags'].strip()}" if r["tags"] else ""
            out.append(f"- {r['path']}  ({r['date']}){tags}\n  {r['excerpt'].strip()}")
        return "\n".join(out)

    results = scan_search(query, folder, limit)
    if not results:
        return f"No results for {query!r} (index unavailable; used plain text scan)."
    out = [f"{len(results)} file(s) contain {query!r} (index unavailable; plain text scan):\n"]
    for r in results:
        out.append(f"- {r['path']}  ({r['hits']} hit(s))\n  …{r['snippet']}…")
    return "\n".join(out)


@vault_tool("Read a note from {label}.")
def read_note(path: str) -> str:
    """
    Read the full Markdown of a vault note, including frontmatter.

    Args:
        path: Vault-relative path as returned by search_vault or list_notes
              (e.g. 'People/Kayleigh Gillis.md'), or just a note name
              (e.g. 'Kayleigh Gillis'), which is looked up across the vault.
    """
    found = resolve_note(path)
    if isinstance(found, str):
        return found
    try:
        found.resolve().relative_to(VAULT_ROOT)
    except ValueError:
        return f"Refusing to read outside the vault: {path}"
    if found.suffix != ".md":
        return f"Not a Markdown note: {path}"

    try:
        text = found.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading {path}: {e}"
    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS] + f"\n\n[… truncated at {MAX_NOTE_CHARS:,} chars]"
    return f"# {found.relative_to(VAULT_ROOT).as_posix()}\n\n{text}"


@vault_tool("List notes in {label}.")
def list_notes(folder: str = "", name: str = "", tag: str = "", since: str = "",
               limit: int = 50) -> str:
    """
    Browse vault notes, newest first. All filters are optional and combine.

    Args:
        folder: Folder to list within, e.g. 'Journal', 'Pursuits/Active', 'People'.
        name:   Text that must appear in the note's path or title (case-insensitive).
        tag:    Tag without '#', e.g. 'Freshworks' or 'clients/Omnicom'.
        since:  YYYY-MM-DD; only notes dated on or after it (e.g. this week's notes).
        limit:  Maximum notes to list (default 50).
    """
    limit = max(1, min(limit, 500))
    if not ensure_index():
        return "Index unavailable: " + "; ".join(_last_result.get("errors", []))

    where, params = folder_clause(folder)
    if name:
        where += " AND (n.path LIKE ? ESCAPE '\\' OR n.title LIKE ? ESCAPE '\\')"
        params += [f"%{_like_escape(name)}%"] * 2
    if tag:
        where += " AND n.tags LIKE ? ESCAPE '\\'"
        params.append(f"% #{_like_escape(tag.lstrip('#'))} %")
    if since:
        where += " AND n.date >= ?"
        params.append(since)

    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM notes n WHERE 1=1 {where}", params).fetchone()[0]
        rows = conn.execute(f"""
            SELECT n.path, n.date, n.tags, n.status, n.word_count FROM notes n
            WHERE 1=1 {where} ORDER BY n.date DESC, n.path LIMIT ?""",
            [*params, limit]).fetchall()

    if not rows:
        return "No notes match those filters."
    out = [f"{total} note(s) match" + (f"; showing newest {len(rows)}" if total > len(rows) else "") + ":\n"]
    for r in rows:
        extra = "".join([f"  [{r['status']}]" if r["status"] else "",
                         f"  {r['tags'].strip()}" if r["tags"] else ""])
        out.append(f"- {r['path']}  ({r['date']}, {r['word_count']} words){extra}")
    return "\n".join(out)


@vault_tool("Statistics for {label}.")
def vault_stats() -> str:
    """
    Summary of the vault: notes, words and date range per top-level folder,
    plus where the index lives and when it was last refreshed.
    """
    ok = ensure_index()
    lines = [f"Vault: {NAME.upper()} ({VAULT_ROOT})", f"Index: {vi.INDEX_PATH}",
             f"Mode: {'read-write' if WRITABLE else 'read-only'}"]
    if not ok:
        return "\n".join(lines + ["Index unavailable: " + "; ".join(_last_result.get("errors", []))])

    with connect() as conn:
        rows = conn.execute("""
            SELECT section, COUNT(*) AS notes, SUM(word_count) AS words,
                   MIN(date) AS earliest, MAX(date) AS latest
            FROM notes GROUP BY section ORDER BY section""").fetchall()
        last = conn.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()

    lines += [f"Last refreshed: {last[0] if last else 'never'} (UTC)", "",
              "| Folder | Notes | Words | Dates |", "|---|---:|---:|---|"]
    for r in rows:
        lines.append(f"| {r['section']} | {r['notes']} | {(r['words'] or 0):,} "
                     f"| {r['earliest']} → {r['latest']} |")
    lines.append(f"| **Total** | {sum(r['notes'] for r in rows)} | "
                 f"{sum(r['words'] or 0 for r in rows):,} | |")

    errors = _last_result.get("errors") or []
    if errors:
        lines += ["", f"{len(errors)} file(s) could not be read, e.g.:"] + [f"  {e}" for e in errors[:5]]
        if any("not permitted" in e.lower() for e in errors):
            lines.append("  (macOS privacy: allow Claude under System Settings → "
                         "Privacy & Security → Files and Folders / Full Disk Access.)")
    return "\n".join(lines)

# ── Write tools (only registered with --writable) ─────────────────────────────

def write_target(path: str, must_exist: bool) -> Path | str:
    """Validate a path for writing. Returns the absolute Path or an error message.
    Writes are limited to .md files inside the vault, outside hidden folders and
    the folders the indexer skips (templates, attachments)."""
    raw = path.strip()
    if not raw:
        return "Empty path."
    target = Path(raw) if Path(raw).is_absolute() else VAULT_ROOT / raw
    if target.suffix != ".md":
        target = target.with_name(target.name + ".md")
    try:
        parts = target.resolve().relative_to(VAULT_ROOT).parts
    except ValueError:
        return f"Refusing to write outside the vault: {path}"
    if any(part.startswith(".") for part in parts):
        return f"Refusing to write to hidden files or folders: {path}"
    if len(parts) > 1 and parts[0] in vi.SKIP_TOP_LEVEL:
        return f"Refusing to write in '{parts[0]}': {path}"
    if must_exist and not target.is_file():
        return f"Note not found: {path} (write tools need an exact path)"
    return target


def after_write() -> None:
    """Make the change searchable immediately rather than within a minute."""
    global _last_refresh
    _last_refresh = 0.0
    ensure_index()


def rel(path: Path) -> str:
    return path.resolve().relative_to(VAULT_ROOT).as_posix()


if WRITABLE:
    @vault_tool("Create a new note in {label}.")
    def create_note(path: str, content: str) -> str:
        """
        Create a new Markdown note. Fails if the note already exists; it never
        overwrites. Missing folders are created.

        Args:
            path:    Vault-relative path, e.g. 'Journal/2026-10-20.md' ('.md' is
                     added if left off).
            content: Full text of the note, including any frontmatter.
        """
        target = write_target(path, must_exist=False)
        if isinstance(target, str):
            return target
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "x", encoding="utf-8", newline="") as f:
                f.write(content)
        except FileExistsError:
            return f"Note already exists: {rel(target)}. Use append_to_note or edit_note."
        except OSError as e:
            return f"Error creating {path}: {e}"
        after_write()
        return f"Created {rel(target)} ({len(content.encode('utf-8')):,} bytes)."

    @vault_tool("Append text to an existing note in {label}.")
    def append_to_note(path: str, content: str) -> str:
        """
        Add text to the end of an existing note. A newline is inserted first if
        the note doesn't end with one. Nothing already in the note changes.

        Args:
            path:    Exact vault-relative path of an existing note.
            content: Text to add.
        """
        target = write_target(path, must_exist=True)
        if isinstance(target, str):
            return target
        try:
            existing = target.read_bytes()
            lead = "" if not existing or existing.endswith(b"\n") else "\n"
            with open(target, "a", encoding="utf-8", newline="") as f:
                f.write(lead + content)
        except OSError as e:
            return f"Error appending to {path}: {e}"
        after_write()
        return f"Appended {len(content.encode('utf-8')):,} bytes to {rel(target)}."

    @vault_tool("Replace one passage of a note in {label}.")
    def edit_note(path: str, old_text: str, new_text: str) -> str:
        """
        Replace an exact passage in an existing note. old_text must appear
        exactly once, so the edit fails rather than guess if the note has
        changed since you read it. Include enough surrounding text to be unique.

        Args:
            path:     Exact vault-relative path of an existing note.
            old_text: The exact text to replace (whitespace matters).
            new_text: Its replacement (empty string deletes the passage).
        """
        target = write_target(path, must_exist=True)
        if isinstance(target, str):
            return target
        if not old_text:
            return "old_text is empty; use append_to_note to add text."
        try:
            text = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"Error reading {path}: {e}"
        count = text.count(old_text)
        if count != 1:
            return (f"old_text was not found in {rel(target)}; read the note again." if count == 0
                    else f"old_text appears {count} times in {rel(target)}; include more context.")
        tmp = target.with_name(f".vault-mcp-{os.getpid()}.tmp")
        try:
            tmp.write_bytes(text.replace(old_text, new_text, 1).encode("utf-8"))
            shutil.copymode(target, tmp)
            os.replace(tmp, target)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return f"Error editing {path}: {e}"
        after_write()
        return f"Edited {rel(target)}."

    @vault_tool("Move or rename a note in {label}.")
    def move_note(path: str, new_path: str) -> str:
        """
        Move or rename a note. Fails if the destination already exists. Links
        in other notes are NOT updated.

        Args:
            path:     Exact vault-relative path of an existing note.
            new_path: Vault-relative destination path.
        """
        src = write_target(path, must_exist=True)
        if isinstance(src, str):
            return src
        dest = write_target(new_path, must_exist=False)
        if isinstance(dest, str):
            return dest
        if dest.exists():
            return f"Destination already exists: {rel(dest)}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
        except OSError as e:
            return f"Error moving {path}: {e}"
        after_write()
        return f"Moved {rel(src)} to {rel(dest)}."

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
