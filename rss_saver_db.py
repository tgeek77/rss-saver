"""SQLite storage for RSS Saver: feeds, items, revisions, tags, FTS5."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    last_fetched_at TEXT,
    retention_days INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_revision_id INTEGER,
    frozen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    downloaded_at TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'full',
    html TEXT,
    screenshot BLOB,
    html_path TEXT,
    screenshot_path TEXT,
    UNIQUE(item_id, content_hash),
    UNIQUE(item_id, rev)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS revisions_fts USING fts5(
    title,
    body,
    content='',
    tokenize='porter unicode61'
);

CREATE INDEX IF NOT EXISTS idx_revisions_item ON revisions(item_id);
CREATE INDEX IF NOT EXISTS idx_revisions_downloaded ON revisions(downloaded_at);
CREATE INDEX IF NOT EXISTS idx_items_feed ON items(feed_id);
CREATE INDEX IF NOT EXISTS idx_feeds_fetched ON feeds(last_fetched_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    return str(uuid.uuid4())


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash_text(text: str) -> str:
    return content_hash_bytes(text.encode("utf-8"))


def default_db_path() -> str:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        base = os.path.join(xdg, "rss-saver")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share", "rss-saver")
    return os.path.join(base, "rss-saver.db")


def parse_since(value: str) -> str:
    """
    Parse --since into a UTC ISO timestamp string.
    Accepts absolute ISO timestamps or relative like 8h, 7d, 30m, 2w.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("empty --since")
    if value.endswith(("h", "d", "m", "w")) and value[:-1].replace(".", "", 1).isdigit():
        amount = float(value[:-1])
        unit = value[-1]
        seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        dt = datetime.now(timezone.utc) - timedelta(seconds=amount * seconds)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Normalize Z
    if value.endswith("Z"):
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"invalid --since value: {value}") from exc


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = __import__("threading").RLock()
        self._migrate()

    def close(self):
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _migrate(self):
        row = self.conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
        if version < 1:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()

    # --- feeds ---

    def upsert_feed(self, url: str, title: str = "") -> sqlite3.Row:
        with self._lock:
            row = self.conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
            if row:
                if title and title != row["title"]:
                    self.conn.execute(
                        "UPDATE feeds SET title = ? WHERE id = ?", (title, row["id"])
                    )
                    self.conn.commit()
                    row = self.conn.execute(
                        "SELECT * FROM feeds WHERE id = ?", (row["id"],)
                    ).fetchone()
                return row
            feed_uuid = new_uuid()
            cur = self.conn.execute(
                "INSERT INTO feeds (uuid, url, title) VALUES (?, ?, ?)",
                (feed_uuid, url, title or ""),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM feeds WHERE id = ?", (cur.lastrowid,)
            ).fetchone()

    def touch_feed(self, feed_id: int, error: Optional[str] = None):
        with self._lock:
            self.conn.execute(
                "UPDATE feeds SET last_fetched_at = ?, last_error = ? WHERE id = ?",
                (utc_now(), error, feed_id),
            )
            self.conn.commit()

    def get_feed(self, key: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM feeds WHERE uuid = ? OR url = ?", (key, key)
        ).fetchone()
        if row:
            return row
        if key.isdigit():
            return self.conn.execute(
                "SELECT * FROM feeds WHERE id = ?", (int(key),)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM feeds WHERE title = ? COLLATE NOCASE", (key,)
        ).fetchone()

    def list_feeds(self, updated_since: Optional[str] = None) -> list[sqlite3.Row]:
        if updated_since:
            return self.conn.execute(
                """
                SELECT f.*,
                       (SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id) AS item_count,
                       (SELECT COUNT(*) FROM revisions r
                        JOIN items i ON i.id = r.item_id
                        WHERE i.feed_id = f.id AND r.downloaded_at >= ?) AS new_revs
                FROM feeds f
                WHERE f.last_fetched_at >= ?
                   OR EXISTS (
                       SELECT 1 FROM revisions r
                       JOIN items i ON i.id = r.item_id
                       WHERE i.feed_id = f.id AND r.downloaded_at >= ?
                   )
                ORDER BY f.last_fetched_at DESC, f.title
                """,
                (updated_since, updated_since, updated_since),
            ).fetchall()
        return self.conn.execute(
            """
            SELECT f.*,
                   (SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id) AS item_count,
                   0 AS new_revs
            FROM feeds f
            ORDER BY f.title COLLATE NOCASE
            """
        ).fetchall()

    def set_frozen(self, item_key: str, frozen: bool) -> sqlite3.Row:
        with self._lock:
            item = self.get_item(item_key)
            if not item:
                raise KeyError(f"item not found: {item_key}")
            self.conn.execute(
                "UPDATE items SET frozen = ? WHERE id = ?",
                (1 if frozen else 0, item["id"]),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM items WHERE id = ?", (item["id"],)
            ).fetchone()

    def set_retention(self, feed_key: str, days: Optional[int]) -> sqlite3.Row:
        with self._lock:
            feed = self.get_feed(feed_key)
            if not feed:
                raise KeyError(f"feed not found: {feed_key}")
            self.conn.execute(
                "UPDATE feeds SET retention_days = ? WHERE id = ?", (days, feed["id"])
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM feeds WHERE id = ?", (feed["id"],)
            ).fetchone()

    # --- items / revisions ---

    def get_item_by_url(self, url: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE url = ?", (url,)
        ).fetchone()

    def get_item(self, key: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM items WHERE uuid = ?", (key,)
        ).fetchone()
        if row:
            return row
        if key.isdigit():
            return self.conn.execute(
                "SELECT * FROM items WHERE id = ?", (int(key),)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM items WHERE url = ?", (key,)
        ).fetchone()

    def get_revision(self, key: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM revisions WHERE uuid = ?", (key,)
        ).fetchone()
        if row:
            return row
        if key.isdigit():
            return self.conn.execute(
                "SELECT * FROM revisions WHERE id = ?", (int(key),)
            ).fetchone()
        return None

    def ensure_item(self, feed_id: int, url: str) -> sqlite3.Row:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM items WHERE url = ?", (url,)
            ).fetchone()
            now = utc_now()
            if row:
                self.conn.execute(
                    "UPDATE items SET last_seen_at = ? WHERE id = ?", (now, row["id"])
                )
                self.conn.commit()
                return self.conn.execute(
                    "SELECT * FROM items WHERE id = ?", (row["id"],)
                ).fetchone()
            item_uuid = new_uuid()
            cur = self.conn.execute(
                """
                INSERT INTO items (uuid, feed_id, url, first_seen_at, last_seen_at, frozen)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (item_uuid, feed_id, url, now, now),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM items WHERE id = ?", (cur.lastrowid,)
            ).fetchone()

    def has_content_hash(self, item_id: int, digest: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM revisions WHERE item_id = ? AND content_hash = ?",
                (item_id, digest),
            ).fetchone()
            return row is not None

    def insert_revision(
        self,
        item_id: int,
        *,
        content_hash: str,
        title: str,
        published_at: str,
        mode: str,
        html: Optional[str],
        screenshot: Optional[bytes],
        html_path: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        body_text: str = "",
    ) -> Optional[sqlite3.Row]:
        """Insert a revision if hash is new. Returns row or None if duplicate hash."""
        with self._lock:
            if self.conn.execute(
                "SELECT 1 FROM revisions WHERE item_id = ? AND content_hash = ?",
                (item_id, content_hash),
            ).fetchone():
                return None
            row = self.conn.execute(
                "SELECT COALESCE(MAX(rev), 0) AS m FROM revisions WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            next_rev = int(row["m"]) + 1
            rev_uuid = new_uuid()
            downloaded = utc_now()
            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO revisions (
                        uuid, item_id, rev, content_hash, title, published_at,
                        downloaded_at, mode, html, screenshot, html_path, screenshot_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev_uuid,
                        item_id,
                        next_rev,
                        content_hash,
                        title,
                        published_at or None,
                        downloaded,
                        mode,
                        html,
                        screenshot,
                        html_path,
                        screenshot_path,
                    ),
                )
            except sqlite3.IntegrityError:
                self.conn.rollback()
                return None
            rev_id = cur.lastrowid
            self.conn.execute(
                "UPDATE items SET latest_revision_id = ?, last_seen_at = ? WHERE id = ?",
                (rev_id, downloaded, item_id),
            )
            self.conn.execute(
                "INSERT INTO revisions_fts(rowid, title, body) VALUES (?, ?, ?)",
                (rev_id, title or "", body_text or ""),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM revisions WHERE id = ?", (rev_id,)
            ).fetchone()

    def delete_item(self, item_key: str, force: bool = False) -> None:
        with self._lock:
            item = self.get_item(item_key)
            if not item:
                raise KeyError(f"item not found: {item_key}")
            if item["frozen"] and not force:
                raise PermissionError(
                    f"item {item['uuid']} is frozen; pass --force to delete"
                )
            rev_ids = [
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM revisions WHERE item_id = ?", (item["id"],)
                )
            ]
            for rid in rev_ids:
                self.conn.execute(
                    "INSERT INTO revisions_fts(revisions_fts, rowid) VALUES('delete', ?)",
                    (rid,),
                )
            self.conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
            self.conn.commit()

    def gc(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Delete unfrozen items whose newest revision is older than feed retention."""
        feeds = self.conn.execute(
            "SELECT * FROM feeds WHERE retention_days IS NOT NULL AND retention_days > 0"
        ).fetchall()
        removed = []
        now = datetime.now(timezone.utc)
        for feed in feeds:
            cutoff = (now - timedelta(days=int(feed["retention_days"]))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            items = self.conn.execute(
                """
                SELECT i.*,
                       (SELECT MAX(r.downloaded_at) FROM revisions r WHERE r.item_id = i.id)
                         AS newest
                FROM items i
                WHERE i.feed_id = ? AND i.frozen = 0
                """,
                (feed["id"],),
            ).fetchall()
            for item in items:
                newest = item["newest"]
                if not newest or newest >= cutoff:
                    continue
                removed.append(
                    {
                        "item_uuid": item["uuid"],
                        "url": item["url"],
                        "newest": newest,
                        "feed": feed["title"] or feed["url"],
                        "retention_days": feed["retention_days"],
                    }
                )
                if not dry_run:
                    self.delete_item(item["uuid"], force=True)
        return removed

    # --- tags ---

    def ensure_tag(self, name: str) -> sqlite3.Row:
        name = name.strip()
        if not name:
            raise ValueError("empty tag")
        row = self.conn.execute(
            "SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row:
            return row
        cur = self.conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM tags WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    def tag_item(self, item_key: str, names: Iterable[str]) -> list[str]:
        item = self.get_item(item_key)
        if not item:
            raise KeyError(f"item not found: {item_key}")
        applied = []
        for name in names:
            tag = self.ensure_tag(name)
            self.conn.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                (item["id"], tag["id"]),
            )
            applied.append(tag["name"])
        self.conn.commit()
        return applied

    def untag_item(self, item_key: str, names: Iterable[str]) -> None:
        item = self.get_item(item_key)
        if not item:
            raise KeyError(f"item not found: {item_key}")
        for name in names:
            tag = self.conn.execute(
                "SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            if not tag:
                continue
            self.conn.execute(
                "DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?",
                (item["id"], tag["id"]),
            )
        self.conn.commit()

    def list_tags(self, item_key: Optional[str] = None) -> list[sqlite3.Row]:
        if item_key:
            item = self.get_item(item_key)
            if not item:
                raise KeyError(f"item not found: {item_key}")
            return self.conn.execute(
                """
                SELECT t.* FROM tags t
                JOIN item_tags it ON it.tag_id = t.id
                WHERE it.item_id = ?
                ORDER BY t.name COLLATE NOCASE
                """,
                (item["id"],),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM tags ORDER BY name COLLATE NOCASE"
        ).fetchall()

    # --- queries ---

    def list_new(self, since: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT r.uuid AS revision_uuid, r.rev, r.title, r.downloaded_at,
                   r.published_at, r.content_hash, r.mode,
                   i.uuid AS item_uuid, i.url, i.frozen,
                   f.uuid AS feed_uuid, f.title AS feed_title, f.url AS feed_url,
                   (SELECT COUNT(*) FROM revisions r2 WHERE r2.item_id = i.id) AS rev_count
            FROM revisions r
            JOIN items i ON i.id = r.item_id
            JOIN feeds f ON f.id = i.feed_id
            WHERE r.downloaded_at >= ?
            ORDER BY COALESCE(r.published_at, r.downloaded_at) DESC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_items(
        self,
        *,
        feed: Optional[str] = None,
        tag: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        url: Optional[str] = None,
        limit: int = 50,
        all_revisions: bool = False,
        sort: str = "downloaded",
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if feed:
            frow = self.get_feed(feed)
            if not frow:
                return []
            clauses.append("f.id = ?")
            params.append(frow["id"])
        if tag:
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM item_tags it
                    JOIN tags t ON t.id = it.tag_id
                    WHERE it.item_id = i.id AND t.name = ? COLLATE NOCASE
                )"""
            )
            params.append(tag)
        if url:
            clauses.append("i.url = ?")
            params.append(url)
        if since:
            clauses.append("r.downloaded_at >= ?")
            params.append(since)
        if until:
            clauses.append("r.downloaded_at <= ?")
            params.append(until)
        if not all_revisions:
            clauses.append("r.id = i.latest_revision_id")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = (
            "r.published_at DESC"
            if sort == "published"
            else "r.downloaded_at DESC"
        )
        sql = f"""
            SELECT r.uuid AS revision_uuid, r.rev, r.title, r.downloaded_at,
                   r.published_at, r.content_hash, r.mode,
                   i.uuid AS item_uuid, i.url, i.frozen,
                   f.uuid AS feed_uuid, f.title AS feed_title, f.url AS feed_url
            FROM revisions r
            JOIN items i ON i.id = r.item_id
            JOIN feeds f ON f.id = i.feed_id
            {where}
            ORDER BY {order}
            LIMIT ?
        """
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_revisions(self, item_key: str) -> list[dict[str, Any]]:
        item = self.get_item(item_key)
        if not item:
            raise KeyError(f"item not found: {item_key}")
        rows = self.conn.execute(
            """
            SELECT r.uuid, r.rev, r.title, r.downloaded_at, r.published_at,
                   r.content_hash, r.mode,
                   length(r.html) AS html_bytes,
                   length(r.screenshot) AS png_bytes
            FROM revisions r
            WHERE r.item_id = ?
            ORDER BY r.rev DESC
            """,
            (item["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def search(
        self,
        query: str,
        *,
        feed: Optional[str] = None,
        tag: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["revisions_fts MATCH ?"]
        params: list[Any] = [query]
        if feed:
            frow = self.get_feed(feed)
            if not frow:
                return []
            clauses.append("f.id = ?")
            params.append(frow["id"])
        if tag:
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM item_tags it
                    JOIN tags t ON t.id = it.tag_id
                    WHERE it.item_id = i.id AND t.name = ? COLLATE NOCASE
                )"""
            )
            params.append(tag)
        if since:
            clauses.append("r.downloaded_at >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT r.uuid AS revision_uuid, r.rev, r.title, r.downloaded_at,
                   i.uuid AS item_uuid, i.url, i.frozen,
                   f.title AS feed_title,
                   snippet(revisions_fts, 1, '[', ']', '…', 20) AS snippet
            FROM revisions_fts
            JOIN revisions r ON r.id = revisions_fts.rowid
            JOIN items i ON i.id = r.item_id
            JOIN feeds f ON f.id = i.feed_id
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """
        params.append(limit)
        try:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            # Fallback: LIKE search if FTS query syntax fails
            like = f"%{query}%"
            return [
                dict(r)
                for r in self.conn.execute(
                    """
                    SELECT r.uuid AS revision_uuid, r.rev, r.title, r.downloaded_at,
                           i.uuid AS item_uuid, i.url, i.frozen,
                           f.title AS feed_title,
                           '' AS snippet
                    FROM revisions r
                    JOIN items i ON i.id = r.item_id
                    JOIN feeds f ON f.id = i.feed_id
                    WHERE r.title LIKE ? OR r.html LIKE ?
                    ORDER BY r.downloaded_at DESC
                    LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()
            ]

    def show_revision(self, key: str) -> Optional[dict[str, Any]]:
        rev = self.get_revision(key)
        if not rev:
            return None
        item = self.conn.execute(
            "SELECT * FROM items WHERE id = ?", (rev["item_id"],)
        ).fetchone()
        feed = self.conn.execute(
            "SELECT * FROM feeds WHERE id = ?", (item["feed_id"],)
        ).fetchone()
        tags = [
            t["name"]
            for t in self.conn.execute(
                """
                SELECT t.name FROM tags t
                JOIN item_tags it ON it.tag_id = t.id
                WHERE it.item_id = ?
                """,
                (item["id"],),
            )
        ]
        return {
            "revision_uuid": rev["uuid"],
            "rev": rev["rev"],
            "title": rev["title"],
            "published_at": rev["published_at"],
            "downloaded_at": rev["downloaded_at"],
            "content_hash": rev["content_hash"],
            "mode": rev["mode"],
            "has_html": bool(rev["html"]),
            "has_screenshot": bool(rev["screenshot"]),
            "html_bytes": len(rev["html"] or ""),
            "png_bytes": len(rev["screenshot"] or b""),
            "html_path": rev["html_path"],
            "screenshot_path": rev["screenshot_path"],
            "item_uuid": item["uuid"],
            "url": item["url"],
            "frozen": bool(item["frozen"]),
            "feed_uuid": feed["uuid"],
            "feed_title": feed["title"],
            "feed_url": feed["url"],
            "tags": tags,
        }

    def export_revision(self, key: str, dest_dir: str) -> dict[str, str]:
        rev = self.get_revision(key)
        if not rev:
            raise KeyError(f"revision not found: {key}")
        os.makedirs(dest_dir, exist_ok=True)
        out = {}
        base = rev["uuid"]
        if rev["html"]:
            path = os.path.join(dest_dir, f"{base}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(rev["html"])
            out["html"] = path
        if rev["screenshot"]:
            path = os.path.join(dest_dir, f"{base}.png")
            with open(path, "wb") as f:
                f.write(rev["screenshot"])
            out["png"] = path
        meta = os.path.join(dest_dir, f"{base}.json")
        import json

        with open(meta, "w", encoding="utf-8") as f:
            json.dump(self.show_revision(key), f, indent=2)
        out["meta"] = meta
        return out
