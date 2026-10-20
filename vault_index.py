#!/usr/bin/env python3
"""
Vault indexer
=============
Builds and refreshes a SQLite FTS5 index of the vault's Markdown notes.

Refreshes are incremental: only notes whose modification time or size changed
are re-read, so a refresh of the whole vault takes well under a second. The MCP
server refreshes the index itself before answering, so running this by hand is
only needed to force a full rebuild or to check what gets indexed:

    python3 vault_index.py --vault PATH            # incremental refresh
    python3 vault_index.py --vault PATH --rebuild  # drop and rebuild from scratch

The vault root comes from --vault PATH or the VAULT_ROOT environment variable.
The index lives OUTSIDE the vault, in ~/.cache/vault-mcp/, so it never syncs
through git or OneDrive; each machine keeps its own (override with VAULT_INDEX).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

def resolve_vault_root(argv: list[str] | None = None) -> Path:
    """Vault root from --vault, else $VAULT_ROOT. Exits with a clear message if it
    is missing or isn't a directory."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--vault")
    args, _ = pre.parse_known_args(sys.argv[1:] if argv is None else argv)
    raw = args.vault or os.environ.get("VAULT_ROOT")
    if not raw:
        sys.exit("vault: no vault given. Pass --vault PATH or set VAULT_ROOT.")
    root = Path(raw).expanduser()
    if not root.is_dir():
        sys.exit(f"vault: not a directory: {root}")
    return root.resolve()


VAULT_ROOT = resolve_vault_root()


def default_index_path() -> Path:
    digest = hashlib.sha1(str(VAULT_ROOT).encode()).hexdigest()[:8]
    return Path.home() / ".cache" / "vault-mcp" / f"{VAULT_ROOT.name}-{digest}.db"


INDEX_PATH = Path(os.environ.get("VAULT_INDEX") or default_index_path())

# Top-level folders that hold no searchable notes.
SKIP_TOP_LEVEL = {"00 - Templates", "Attachments"}

SCHEMA_VERSION = "2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,   -- vault-relative, forward slashes
    section    TEXT,                   -- top-level folder, or '(root)'
    title      TEXT,
    date       TEXT,                   -- YYYY-MM-DD
    tags       TEXT,                   -- ' #a #b ' (padded for LIKE matching)
    status     TEXT,
    word_count INTEGER,
    mtime      REAL,
    size       INTEGER,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS notes_date ON notes(date);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
    USING fts5(title, tags, body, tokenize = 'porter unicode61');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# ── Parsing ───────────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
DATE_RE        = re.compile(r"(\d{4}-\d{2}-\d{2})")
TAG_RE         = re.compile(r"(?:^|(?<=\s))#([A-Za-z][\w/-]*)", re.M)


def _clean(value: str) -> str:
    return value.strip().strip("'\"")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter reader: scalars, [inline, lists] and '- item' lists."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    meta: dict = {}
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_clean(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z0-9_\- ]+):\s*(.*)$", line)
        if kv:
            key = kv.group(1).strip().lower()
            value = kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [_clean(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                meta[key] = _clean(value)
    return meta, text[m.end():]


def parse_note(rel: str, text: str, mtime: float) -> dict:
    meta, body = parse_frontmatter(text)
    parts = rel.split("/")

    title = meta.get("title")
    if not isinstance(title, str) or not title:
        title = Path(rel).stem

    # Date: frontmatter first, then a date in the filename, then file mtime.
    date = None
    for key in ("date", "date_created", "created"):
        value = meta.get(key)
        if isinstance(value, str) and DATE_RE.match(value):
            date = value[:10]
            break
    if not date:
        m = DATE_RE.search(Path(rel).stem)
        date = m.group(1) if m else datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")

    tags: list[str] = []
    fm_tags = meta.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = re.split(r"[,\s]+", fm_tags)
    for tag in list(fm_tags) + TAG_RE.findall(body):
        tag = tag.lstrip("#")
        if tag and tag not in tags:
            tags.append(tag)

    status = meta.get("status")
    return {
        "section":    parts[0] if len(parts) > 1 else "(root)",
        "title":      title,
        "date":       date,
        "tags":       (" " + " ".join("#" + t for t in tags) + " ") if tags else "",
        "status":     status if isinstance(status, str) else "",
        "word_count": len(body.split()),
        "body":       body,
    }


def iter_notes(errors: list[str]):
    """Yield every .md note, skipping hidden folders and SKIP_TOP_LEVEL."""
    def onerror(e: OSError):
        errors.append(f"{e.filename}: {e.strerror}")

    for dirpath, dirnames, filenames in os.walk(VAULT_ROOT, onerror=onerror):
        at_root = Path(dirpath) == VAULT_ROOT
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and not (at_root and d in SKIP_TOP_LEVEL)]
        for name in filenames:
            if name.endswith(".md") and not name.startswith("."):
                yield Path(dirpath) / name

# ── Index maintenance ─────────────────────────────────────────────────────────

def connect(rebuild: bool = False) -> sqlite3.Connection:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = version[0] if version else None
    except sqlite3.OperationalError:
        version = None

    if rebuild or version != SCHEMA_VERSION:
        conn.executescript("""
            DROP TABLE IF EXISTS notes;
            DROP TABLE IF EXISTS notes_fts;
            DROP TABLE IF EXISTS meta;
        """)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('vault_root', ?)", (str(VAULT_ROOT),))
    conn.commit()
    return conn


def refresh(rebuild: bool = False) -> dict:
    """Bring the index in line with the vault. Returns counts and any read errors."""
    started = time.monotonic()
    conn = connect(rebuild)
    known = {r["path"]: (r["id"], r["mtime"], r["size"])
             for r in conn.execute("SELECT id, path, mtime, size FROM notes")}

    errors: list[str] = []
    seen: set[str] = set()
    added = updated = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with conn:
        for f in iter_notes(errors):
            rel = f.relative_to(VAULT_ROOT).as_posix()
            try:
                st = f.stat()
            except OSError as e:
                errors.append(f"{rel}: {e.strerror}")
                continue
            seen.add(rel)

            old = known.get(rel)
            if old and old[1] == st.st_mtime and old[2] == st.st_size:
                continue

            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                errors.append(f"{rel}: {e.strerror}")
                continue
            n = parse_note(rel, text, st.st_mtime)
            row = (n["section"], n["title"], n["date"], n["tags"], n["status"],
                   n["word_count"], st.st_mtime, st.st_size, now)

            if old:
                note_id = old[0]
                conn.execute("""
                    UPDATE notes SET section=?, title=?, date=?, tags=?, status=?,
                                     word_count=?, mtime=?, size=?, indexed_at=?
                    WHERE id = ?""", row + (note_id,))
                conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
                updated += 1
            else:
                note_id = conn.execute("""
                    INSERT INTO notes (section, title, date, tags, status, word_count,
                                       mtime, size, indexed_at, path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row + (rel,)).lastrowid
                added += 1
            conn.execute("INSERT INTO notes_fts (rowid, title, tags, body) VALUES (?, ?, ?, ?)",
                         (note_id, n["title"], n["tags"], n["body"]))

        # Don't wipe the index if the vault couldn't be read at all.
        removed = 0
        if seen or not errors:
            for rel in known.keys() - seen:
                note_id = known[rel][0]
                conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
                removed += 1

        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_refresh', ?)", (now,))

    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    return {
        "added": added, "updated": updated, "removed": removed, "total": total,
        "errors": errors, "seconds": round(time.monotonic() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vault", help="vault root (or set VAULT_ROOT)")
    parser.add_argument("--rebuild", action="store_true", help="drop and rebuild the index")
    args = parser.parse_args()

    result = refresh(rebuild=args.rebuild)
    print(f"Vault:  {VAULT_ROOT}")
    print(f"Index:  {INDEX_PATH}")
    print(f"Notes:  {result['total']}  (+{result['added']} new, {result['updated']} updated, "
          f"-{result['removed']} removed) in {result['seconds']}s")
    for err in result["errors"][:20]:
        print(f"  ! {err}")
    if len(result["errors"]) > 20:
        print(f"  ! … {len(result['errors']) - 20} more errors")


if __name__ == "__main__":
    main()
