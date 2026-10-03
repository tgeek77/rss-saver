"""Textual TUI for RSS Saver — wraps the same Store operations as the CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def run_tui(store, add_source=None, update_all=None):
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical
        from textual.widgets import (
            Button,
            DataTable,
            Footer,
            Header,
            Input,
            Label,
            Static,
        )
    except ImportError as exc:
        raise ImportError("textual is required for the TUI") from exc

    def since_8h():
        return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    class RssSaverApp(App):
        CSS = """
        Screen { layout: vertical; }
        #sidebar { width: 36; border: solid $primary; }
        #main { width: 1fr; border: solid $accent; }
        #status { height: 3; dock: bottom; }
        DataTable { height: 1fr; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("n", "show_new", "New"),
            Binding("f", "show_feeds", "Feeds"),
            Binding("u", "pull_update", "Update"),
            Binding("r", "refresh", "Refresh"),
            Binding("z", "freeze", "Freeze"),
            Binding("o", "open_item", "Open"),
            Binding("d", "delete_feed", "Del feed"),
            Binding("/", "focus_search", "Search"),
        ]

        def __init__(self, store, add_source=None, update_all=None):
            super().__init__()
            self.store = store
            self.add_source = add_source
            self.update_all = update_all
            self.mode = "new"
            self._rows = []

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal():
                with Vertical(id="sidebar"):
                    yield Label("rss-saver TUI")
                    yield Button("New (8h)", id="btn_new")
                    yield Button("Feeds", id="btn_feeds")
                    yield Button("Pull update", id="btn_update")
                    yield Button("Search", id="btn_search")
                    yield Input(placeholder="FTS query…", id="search")
                    yield Label("Add feed / OPML")
                    yield Input(
                        placeholder="RSS URL or OPML path/https…",
                        id="add_source",
                    )
                    yield Button("Add RSS", id="btn_add_rss")
                    yield Button("Add OPML", id="btn_add_opml")
                    yield Input(
                        placeholder="Retention days for selected feed",
                        id="retention",
                    )
                    yield Button("Set retention", id="btn_retention")
                    yield Button("Delete feed", id="btn_delete_feed")
                with Vertical(id="main"):
                    yield Static("What's new (8h)", id="title")
                    yield DataTable(id="table")
            yield Static(f"DB: {self.store.path}", id="status")
            yield Footer()

        def on_mount(self):
            table = self.query_one("#table", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True
            self.action_show_new()

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id == "btn_new":
                self.action_show_new()
            elif event.button.id == "btn_feeds":
                self.action_show_feeds()
            elif event.button.id == "btn_update":
                self.action_pull_update()
            elif event.button.id == "btn_search":
                self.action_focus_search()
                q = self.query_one("#search", Input).value.strip()
                if q:
                    self._load_search(q)
            elif event.button.id == "btn_retention":
                self._set_retention()
            elif event.button.id == "btn_add_rss":
                self._add_source(kind="rss")
            elif event.button.id == "btn_add_opml":
                self._add_source(kind="opml")
            elif event.button.id == "btn_delete_feed":
                self.action_delete_feed()

        def on_input_submitted(self, event: Input.Submitted):
            if event.input.id == "search":
                q = event.value.strip()
                if q:
                    self._load_search(q)
            elif event.input.id == "add_source":
                self._add_source(kind="auto")

        def _clear_table(self, columns):
            table = self.query_one("#table", DataTable)
            table.clear(columns=True)
            for col in columns:
                table.add_column(col, key=col)
            return table

        def action_show_new(self):
            self.mode = "new"
            self.query_one("#title", Static).update("What's new (last 8 hours)")
            since = since_8h()
            rows = self.store.list_new(since)
            self._rows = rows
            table = self._clear_table(
                ["published", "feed", "title", "rev", "item", "frozen"]
            )
            for r in rows:
                table.add_row(
                    (r.get("published_at") or "")[:19],
                    (r.get("feed_title") or "")[:24],
                    (r.get("title") or "")[:48],
                    str(r.get("rev")),
                    (r.get("item_uuid") or "")[:8],
                    "Y" if r.get("frozen") else "",
                    key=r.get("revision_uuid"),
                )
            self.query_one("#status", Static).update(
                f"{len(rows)} revision(s) since {since} | DB: {self.store.path}"
            )

        def action_show_feeds(self):
            self.mode = "feeds"
            self.query_one("#title", Static).update("Feeds")
            feeds = [dict(f) for f in self.store.list_feeds()]
            self._rows = feeds
            table = self._clear_table(["title", "items", "retention", "uuid"])
            for f in feeds:
                ret = f.get("retention_days")
                table.add_row(
                    (f.get("title") or f.get("url") or "")[:40],
                    str(f.get("item_count") or 0),
                    "∞" if ret is None else str(ret),
                    (f.get("uuid") or "")[:8],
                    key=f.get("uuid"),
                )
            self.query_one("#status", Static).update(
                f"{len(feeds)} feed(s) | DB: {self.store.path}"
            )

        def action_pull_update(self):
            if self.update_all is None:
                self.query_one("#status", Static).update(
                    "Update not available in this TUI session"
                )
                return
            n = len(self.store.list_feeds())
            self.query_one("#status", Static).update(
                f"Delta-updating {n} stored feed(s)…"
            )
            try:
                msg = self.update_all(mode="full", jobs=8)
                self.query_one("#status", Static).update(msg)
                self.action_show_new()
            except Exception as exc:
                self.query_one("#status", Static).update(f"Update failed: {exc}")

        def _load_search(self, query):
            self.mode = "search"
            self.query_one("#title", Static).update(f"Search: {query}")
            rows = self.store.search(query, limit=100)
            self._rows = rows
            table = self._clear_table(["title", "feed", "snippet", "item"])
            for r in rows:
                table.add_row(
                    (r.get("title") or "")[:40],
                    (r.get("feed_title") or "")[:20],
                    (r.get("snippet") or "")[:40],
                    (r.get("item_uuid") or "")[:8],
                    key=r.get("revision_uuid"),
                )
            self.query_one("#status", Static).update(
                f"{len(rows)} hit(s) | DB: {self.store.path}"
            )

        def action_refresh(self):
            if self.mode == "feeds":
                self.action_show_feeds()
            elif self.mode == "search":
                q = self.query_one("#search", Input).value.strip()
                if q:
                    self._load_search(q)
            else:
                self.action_show_new()

        def action_focus_search(self):
            self.query_one("#search", Input).focus()

        def _selected_key(self):
            table = self.query_one("#table", DataTable)
            if table.row_count == 0:
                return None
            try:
                return table.coordinate_to_cell_key(
                    table.cursor_coordinate
                ).row_key.value
            except Exception:
                return None

        def _add_source(self, kind="auto"):
            if self.add_source is None:
                self.query_one("#status", Static).update(
                    "Add not available in this TUI session"
                )
                return
            source = self.query_one("#add_source", Input).value.strip()
            if not source:
                self.query_one("#status", Static).update(
                    "Enter an RSS URL or OPML path/https URL"
                )
                return
            self.query_one("#status", Static).update(f"Adding {source}…")
            try:
                msg = self.add_source(source, kind=kind)
                self.query_one("#add_source", Input).value = ""
                self.query_one("#status", Static).update(msg)
                self.action_show_feeds()
            except Exception as exc:
                self.query_one("#status", Static).update(f"Add failed: {exc}")

        def action_delete_feed(self):
            if self.mode != "feeds":
                self.query_one("#status", Static).update(
                    "Switch to Feeds (f), select a feed, then Delete"
                )
                return
            key = self._selected_key()
            if not key:
                self.query_one("#status", Static).update("Select a feed to delete")
                return
            try:
                result = self.store.delete_feed(key, force=True)
                self.query_one("#status", Static).update(
                    f"Deleted feed {result['title'] or result['url']} "
                    f"({result['items_deleted']} items)"
                )
                self.action_show_feeds()
            except Exception as exc:
                self.query_one("#status", Static).update(str(exc))

        def action_freeze(self):
            key = self._selected_key()
            if not key:
                self.query_one("#status", Static).update("Nothing selected")
                return
            if self.mode == "feeds":
                self.query_one("#status", Static).update(
                    "Select an item row to freeze"
                )
                return
            rev = self.store.get_revision(key)
            if not rev:
                self.query_one("#status", Static).update(
                    "Could not resolve selection"
                )
                return
            item = self.store.conn.execute(
                "SELECT uuid FROM items WHERE id = ?", (rev["item_id"],)
            ).fetchone()
            self.store.set_frozen(item["uuid"], True)
            self.query_one("#status", Static).update(f"Frozen {item['uuid']}")
            self.action_refresh()

        def action_open_item(self):
            import subprocess
            import sys
            import tempfile

            key = self._selected_key()
            if not key or self.mode == "feeds":
                self.query_one("#status", Static).update("Select a revision row")
                return
            try:
                dest = tempfile.mkdtemp(prefix="rss-saver-tui-")
                paths = self.store.export_revision(key, dest)
                target = paths.get("html") or paths.get("png")
                if not target:
                    self.query_one("#status", Static).update("No HTML/PNG")
                    return
                if sys.platform == "darwin":
                    subprocess.Popen(["open", target])
                else:
                    subprocess.Popen(["xdg-open", target])
                self.query_one("#status", Static).update(f"Opened {target}")
            except Exception as exc:
                self.query_one("#status", Static).update(str(exc))

        def _set_retention(self):
            key = self._selected_key()
            if self.mode != "feeds" or not key:
                self.query_one("#status", Static).update("Select a feed first")
                return
            raw = self.query_one("#retention", Input).value.strip()
            if raw in ("", "unlimited", "0"):
                days = None
            else:
                days = int(raw)
            feed = self.store.set_retention(key, days)
            self.query_one("#status", Static).update(
                f"Retention for {feed['title'] or feed['url']}: "
                f"{feed['retention_days']}"
            )
            self.action_show_feeds()

    RssSaverApp(store, add_source=add_source, update_all=update_all).run()
