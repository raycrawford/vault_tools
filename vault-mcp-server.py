#!/usr/bin/env python3
"""
Obsidian Vault MCP Server
=========================
Read-only access to this vault for Claude Desktop and Claude Code.

Tools:
  search_vault  ranked full-text search (SQLite FTS5, stemmed)
  read_note     full Markdown of a note, by vault-relative path or note name
  list_notes    browse notes by folder, name, tag and date, newest first
  vault_stats   note counts per top-level folder and index status

The vault root is the parent of this .tools folder, so the same file works on
every machine. The search index (see vault_index.py) is refreshed
automatically, so there is nothing to run by hand.

Setup (once per machine):  bash .tools/setup.sh
"""

from __future__ import annotations

import re
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

mcp = FastMCP("vault")

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

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def vault_stats() -> str:
    """
    Summary of the vault: notes, words and date range per top-level folder,
    plus where the index lives and when it was last refreshed.
    """
    ok = ensure_index()
    lines = [f"Vault: {VAULT_ROOT}", f"Index: {vi.INDEX_PATH}"]
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

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
