# What is RSS Saver?

**Version 2.1**

RSS Saver archives RSS/Atom feeds (and OPML lists) into a local **SQLite** database for fast, delta-only collection, revision history, search, tagging, and review. Optional full-page screenshots use Chrome, Brave, Chromium, or auto-installed `chrome-headless-shell`.

It is built for OSINT-style feed monitoring: hourly cron pulls only what changed, morning review shows what’s new, and frozen articles survive per-feed retention limits.

## What's new in 2.1

- **SQLite default store** (HTML + PNG BLOBs, metadata, FTS5 search, tags)
- **Delta-only pulls** — skip unchanged content by hash; new hash → new **revision**
- **Parallel feed fetch** (`--jobs`, default 8)
- **UUIDs**, **freeze**, and per-feed **retention** (default unlimited)
- Review CLI: `new`, `feeds`, `list`, `search`, `serve`, `export`, …
- Optional **Textual TUI** (`rss-saver.py tui`)
- **`--dl`** — also write classic per-feed folders + `INDEX.md` on disk

## What's new in 2.0

- OPML input, per-feed dirs / `INDEX.md`, screenshots, offline-friendly HTML

# How do I use it?

## Prerequisites

* BeautifulSoup4, feedparser, requests
* Optional TUI: `pip install textual`
* Optional screenshots: Chrome / Brave / Chromium (or auto `chrome-headless-shell`)

```
# Debian/Ubuntu
apt install python3-bs4 python3-feedparser python3-requests

# Arch
pacman -S python-beautifulsoup4 python-feedparser python-requests
```

### Screenshots (optional)

HTML saving works without a browser. Screenshots:

1. Prefer installed Chrome / Brave / Chromium (`RSS_SAVER_BROWSER` / `CHROME_BIN`, then `PATH`, then macOS apps).
2. Else auto-download `chrome-headless-shell` when the OS/arch is supported (Linux/macOS/Windows CfT platforms).
3. Else warn that screenshots are unavailable; HTML still saves.

## Database location

Default DB: `$XDG_DATA_HOME/rss-saver/rss-saver.db` or `~/.local/share/rss-saver/rss-saver.db`.

Override with `--db /path/to/file.db` on any command.

## Pull (delta by default)

```bash
# Single feed → SQLite only
./rss-saver.py pull -u https://example.com/feed.xml -t full

# Large OPML, parallel workers (cron-friendly)
./rss-saver.py pull -p ~/intel.opml -t full --jobs 8

# Update every feed already in the database (delta only)
./rss-saver.py update -t full --jobs 8
./rss-saver.py pull --all -t full --jobs 8

# Also write HTML/PNG/INDEX.md under ~/feeds
./rss-saver.py pull -u https://example.com/feed.xml -t full --dl -o ~/feeds

# Legacy flag style still works (implies pull)
./rss-saver.py -u https://example.com/feed.xml -t full
```

| Situation | Behavior |
|-----------|----------|
| New article URL | Fetch and store revision 1 |
| Same URL, same content hash | **Skip** |
| Same URL, different content | Store new revision (history kept) |
| `--force` | Always re-fetch; still skip insert if hash unchanged |

`--checkup` is no longer required — delta is always on.

### Modes

| Type | HTML | Screenshot |
|------|------|------------|
| `full` | Yes | When a Chrome-based browser is available |
| `simple` | No | Required (browser) |

## Review and query

All time windows (`--since`, `--until`, `new`, `feeds --updated-since`, retention GC) use **article publish time**. Pull/download time is stored only as metadata.

If a feed entry has no publish date, `published_at` stays empty. Those items are **excluded** from `new --since` / TUI New (pull time is metadata only and is never shown as publish time).

```bash
./rss-saver.py new --since 8h
./rss-saver.py new --since 8h --json          # agent-friendly
./rss-saver.py feeds --updated-since 8h
./rss-saver.py list --feed FEED_UUID --limit 20
./rss-saver.py search "ransomware" --since 7d
./rss-saver.py revisions ITEM_UUID
./rss-saver.py show REVISION_UUID
./rss-saver.py serve --since 8h --port 8765 --open-browser
./rss-saver.py open REVISION_UUID
./rss-saver.py export REVISION_UUID -o /tmp/out
```

## Add and remove feeds

```bash
# Single RSS/Atom feed (also pulls)
./rss-saver.py add -u https://example.com/feed.xml -t full

# OPML from a local file or https URL
./rss-saver.py add -p ~/intel.opml -t full
./rss-saver.py add -p https://example.com/feeds.opml -t full

# Delete a feed and all of its articles (use --force if any are frozen)
./rss-saver.py delete-feed FEED_UUID_OR_URL
./rss-saver.py delete-feed FEED_UUID_OR_URL --force
```

In the TUI (`./rss-saver.py tui`): **Pull update** (or `u`) delta-refreshes all stored feeds. Paste an RSS URL or OPML path/https URL, then **Add RSS** / **Add OPML**. On the Feeds screen, select a feed and **Delete feed** (or press `d`).

## Freeze, retention, GC

Default retention is **unlimited**. Set a per-feed age limit (days) measured from **publish time**; frozen items are never deleted by GC.

```bash
./rss-saver.py retention set --feed FEED_UUID_OR_URL 30   # 1 month
./rss-saver.py retention set --feed FEED_UUID_OR_URL 0    # unlimited again
./rss-saver.py freeze ITEM_UUID     # keep forever despite feed limit
./rss-saver.py unfreeze ITEM_UUID
./rss-saver.py gc --dry-run
./rss-saver.py gc
./rss-saver.py delete ITEM_UUID --force   # even if frozen
```

Example: a politics feed keeps articles 30 days, but a frozen “Trump visit to London…” item remains until you unfreeze + `gc` or `delete --force`.

## Tags

```bash
./rss-saver.py tag add ITEM_UUID ibm apt
./rss-saver.py tag rm ITEM_UUID ibm
./rss-saver.py tag list ITEM_UUID
./rss-saver.py list --tag apt
```

## TUI

```bash
pip install textual
./rss-saver.py tui
```

Browse what’s new, feeds, search, freeze, set retention, open HTML/PNG.

## Cron example

```bash
0 * * * * /path/to/rss-saver.py update -t full --jobs 8 --db /home/you/.local/share/rss-saver/intel.db
0 8 * * * /path/to/rss-saver.py gc --db /home/you/.local/share/rss-saver/intel.db
```

Morning:

```bash
./rss-saver.py new --since 8h --db ~/.local/share/rss-saver/intel.db
./rss-saver.py serve --since 8h --open-browser
```

## Disk layout with `--dl`

Same as 2.0: per-feed directories under `--output` with `INDEX.md`, `.html`, `.png`. The database remains the source of truth for delta/revisions.

# Advanced Usage

In [resources/](resources/) see `rss_list.txt` (~2875 feeds). Prefer a curated OPML over downloading everything.

```bash
./rss-saver.py pull -p ~/my_feeds.opml -t full --jobs 8
```

Credits to [Kovid Goyal](https://github.com/kovidgoyal/calibre) / Calibre for `rss_list`.

# Why?

RSS readers are for reading. RSS Saver is for **keeping**, **diffing revisions**, and **searching** a local archive quickly — without ArchiveBox-scale multi-extractor cost per URL.

# Future

Possible 2.2: watchlist alerts and light IOC extraction. Stay feed-native and fast.
