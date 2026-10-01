#!/usr/bin/python3

import argparse
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import feedparser
import requests
from bs4 import BeautifulSoup

BROWSER_CANDIDATES = (
    "chrome-headless-shell",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)

MAC_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)


def slugify(text, fallback="untitled"):
    """Keep letters/digits; collapse everything else to underscores."""
    if not text:
        text = fallback
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")
    return cleaned or fallback


def feed_dir_name(title, url):
    if title:
        return slugify(title)
    parsed = urlparse(url)
    host = parsed.netloc or "feed"
    path = parsed.path.strip("/").replace("/", "_")
    return slugify(f"{host}_{path}" if path else host, fallback="feed")


def article_basename(title):
    return slugify(title)


def find_browser():
    """Return a Chromium-family binary path, or None if not found."""
    for env_name in ("RSS_SAVER_BROWSER", "CHROME_BIN"):
        override = os.environ.get(env_name)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override

    for name in BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path

    for path in MAC_BROWSER_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None


def screenshot_unavailable_hint():
    return (
        "Screenshots unavailable: no chrome-headless-shell or Chrome/Chromium found.\n"
        "Install Chromium to enable screenshots, e.g.:\n"
        "  Arch:     sudo pacman -S chromium\n"
        "  Debian:   sudo apt install chromium\n"
        "  macOS:    brew install --cask chromium\n"
        "  OpenBSD:  doas pkg_add chromium\n"
        "Or set RSS_SAVER_BROWSER=/path/to/chromium\n"
        "Or put chrome-headless-shell on your PATH."
    )


def parse_opml(source):
    """
    Parse an OPML document from a local path or HTTP(S) URL.
    Returns a list of dicts: [{"title": ..., "url": ...}, ...]
    """
    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=60)
        response.raise_for_status()
        raw = response.content
    else:
        with open(source, "rb") as f:
            raw = f.read()

    root = ElementTree.fromstring(raw)
    feeds = []
    seen = set()

    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl") or outline.get("xmlurl")
        if not xml_url:
            continue
        if xml_url in seen:
            continue
        seen.add(xml_url)
        title = outline.get("title") or outline.get("text") or ""
        feeds.append({"title": title.strip(), "url": xml_url.strip()})

    return feeds


def format_entry_date(entry):
    """Best-effort article date from feedparser entry fields (UTC ISO)."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key in ("published", "updated", "created"):
        value = getattr(entry, key, None)
        if value:
            return str(value)
    return ""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_index(feed_dir, feed_title, feed_url, mode, articles):
    """Write INDEX.md for the given article list."""
    lines = [
        f"# {feed_title}",
        "",
        f"- Feed URL: {feed_url}",
        f"- Updated: {utc_now()}",
        f"- Type: {mode}",
        f"- Articles: {len(articles)}",
        "",
        "| File | Title | Published | Downloaded | Screenshot | Article URL |",
        "|------|-------|-----------|------------|------------|-------------|",
    ]
    for article in articles:
        file_cell = article.get("file", "").replace("|", "\\|")
        title_cell = article["title"].replace("|", "\\|")
        published = article.get("published", "").replace("|", "\\|")
        downloaded = article.get("downloaded", "").replace("|", "\\|")
        screenshot = article.get("screenshot", "").replace("|", "\\|")
        url_cell = article["url"].replace("|", "\\|")
        lines.append(
            f"| {file_cell} | {title_cell} | {published} | {downloaded} | "
            f"{screenshot} | {url_cell} |"
        )
    lines.append("")

    index_path = os.path.join(feed_dir, "INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def load_index(feed_dir):
    """Parse INDEX.md table rows into article dicts (URL is the checkup key)."""
    index_path = os.path.join(feed_dir, "INDEX.md")
    articles = []
    if not os.path.isfile(index_path):
        return articles

    with open(index_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("| ") or line.startswith("| File") or line.startswith("|------"):
                continue
            parts = [p.strip().replace("\\|", "|") for p in line.strip().strip("|").split("|")]
            if len(parts) >= 6:
                # File | Title | Published | Downloaded | Screenshot | Article URL
                articles.append(
                    {
                        "file": parts[0],
                        "title": parts[1],
                        "published": parts[2],
                        "downloaded": parts[3],
                        "screenshot": parts[4],
                        "url": parts[5],
                    }
                )
            elif len(parts) >= 5:
                # File | Title | Published | Downloaded | Article URL
                articles.append(
                    {
                        "file": parts[0],
                        "title": parts[1],
                        "published": parts[2],
                        "downloaded": parts[3],
                        "screenshot": "",
                        "url": parts[4],
                    }
                )
            elif len(parts) >= 3:
                # Legacy: File | Title | Article URL
                articles.append(
                    {
                        "file": parts[0],
                        "title": parts[1],
                        "published": "",
                        "downloaded": "",
                        "screenshot": "",
                        "url": parts[2],
                    }
                )
    return articles


def fetch_full_html(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    return str(soup)


class _WebSocketClient:
    """Minimal RFC6455 client for Chromium CDP (text JSON frames)."""

    def __init__(self, ws_url, timeout=60):
        parsed = urlparse(ws_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket upgrade rejected: {response[:200]!r}")

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def recv_text(self):
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:  # text
                return payload.decode("utf-8")
            if opcode == 0x8:  # close
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:  # ping -> pong
                self._send_raw(0xA, payload)
                continue
            # ignore binary / continuation

    def _send_raw(self, opcode, payload):
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            data += chunk
        return data

    def close(self):
        try:
            self._send_raw(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ChromiumSession:
    """Reusable headless Chromium session for full-page screenshots via CDP."""

    def __init__(self, browser_path, timeout=60):
        self.browser_path = browser_path
        self.timeout = timeout
        self.proc = None
        self.profile_dir = None
        self.port = None
        self._msg_id = 0

    def start(self):
        self.profile_dir = tempfile.mkdtemp(prefix="rss-saver-chrome-")
        cmd = [
            self.browser_path,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            f"--user-data-dir={self.profile_dir}",
            f"--crash-dumps-dir={os.path.join(self.profile_dir, 'crashes')}",
            "--remote-debugging-port=0",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.port = self._wait_for_devtools_port()
        return self

    def _wait_for_devtools_port(self):
        deadline = time.time() + self.timeout
        buf = b""
        assert self.proc is not None and self.proc.stderr is not None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                rest = self.proc.stderr.read() or b""
                raise RuntimeError(
                    f"Browser exited early (code {self.proc.returncode}): "
                    f"{(buf + rest).decode('utf-8', errors='replace')[:500]}"
                )
            line = self.proc.stderr.readline()
            if not line:
                time.sleep(0.05)
                continue
            buf += line
            match = re.search(rb"DevTools listening on ws://[^:]+:(\d+)/", line)
            if match:
                return int(match.group(1))
        raise TimeoutError("Timed out waiting for Chromium DevTools port")

    def _ws_url_for_page(self):
        # Prefer an existing target (about:blank from launch), else create one.
        with urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=self.timeout) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        for target in targets:
            ws_url = target.get("webSocketDebuggerUrl")
            if ws_url and target.get("type") == "page":
                return ws_url

        req = Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
        with urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ws_url = data.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(f"No webSocketDebuggerUrl from /json/new: {data}")
        return ws_url

    def _cdp(self, ws, method, params=None, wait_event=None, event_timeout=None):
        self._msg_id += 1
        msg_id = self._msg_id
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        ws.send_text(json.dumps(payload))
        deadline = time.time() + (event_timeout or self.timeout)
        result = None
        event_seen = wait_event is None
        while time.time() < deadline:
            raw = ws.recv_text()
            message = json.loads(raw)
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} error: {message['error']}")
                result = message.get("result", {})
                if event_seen:
                    return result
            if wait_event and message.get("method") == wait_event:
                event_seen = True
                if result is not None:
                    return result
        raise TimeoutError(f"CDP timeout on {method}" + (f" / {wait_event}" if wait_event else ""))

    def capture_screenshot(self, url, dest_png):
        ws = _WebSocketClient(self._ws_url_for_page(), timeout=self.timeout)
        try:
            self._cdp(ws, "Page.enable")
            self._cdp(
                ws,
                "Page.navigate",
                {"url": url},
                wait_event="Page.loadEventFired",
                event_timeout=self.timeout,
            )
            # Brief settle for late layout / lazy content
            time.sleep(0.5)
            result = self._cdp(
                ws,
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                },
            )
            data = result.get("data")
            if not data:
                raise RuntimeError("Page.captureScreenshot returned no data")
            with open(dest_png, "wb") as f:
                f.write(base64.b64decode(data))
        finally:
            ws.close()

    def close(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
        if self.profile_dir and os.path.isdir(self.profile_dir):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def save_feed(feed_url, output_root, mode, preferred_title=None, checkup=False, browser_session=None):
    feed = feedparser.parse(feed_url)
    feed_title = preferred_title or feed.feed.get("title") or feed_url
    feed_dir = os.path.join(output_root, feed_dir_name(feed_title, feed_url))
    os.makedirs(feed_dir, exist_ok=True)

    if mode == "simple" and browser_session is None:
        print(
            f"Skipping feed '{feed_title}': simple mode needs Chromium for screenshots",
            file=sys.stderr,
        )
        return []

    articles = load_index(feed_dir) if checkup else []
    known_urls = {a["url"] for a in articles}
    new_count = 0
    skipped = 0

    for entry in feed.entries:
        title = getattr(entry, "title", None) or "untitled"
        url = getattr(entry, "link", None)
        if not url:
            print(f"Skipping entry without link in '{feed_title}'", file=sys.stderr)
            continue

        if checkup and url in known_urls:
            skipped += 1
            continue

        base = article_basename(title)
        html_name = f"{base}.html"
        png_name = f"{base}.png"
        html_path = os.path.join(feed_dir, html_name)
        png_path = os.path.join(feed_dir, png_name)
        screenshot_saved = ""
        html_saved = ""

        if mode == "full":
            try:
                article_html = fetch_full_html(url)
            except requests.RequestException as exc:
                print(f"Failed to fetch '{title}' ({url}): {exc}", file=sys.stderr)
                continue
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(f"URL: {url}\n\n")
                f.write(article_html)
            html_saved = html_name
            print(f"Article '{title}' saved to '{html_path}'")

        if browser_session is not None:
            try:
                browser_session.capture_screenshot(url, png_path)
                screenshot_saved = png_name
                print(f"Screenshot '{title}' saved to '{png_path}'")
            except Exception as exc:
                print(f"Screenshot failed for '{title}' ({url}): {exc}", file=sys.stderr)
                if mode == "simple":
                    continue
        elif mode == "simple":
            continue

        articles.append(
            {
                "file": html_saved,
                "title": title,
                "published": format_entry_date(entry),
                "downloaded": utc_now(),
                "screenshot": screenshot_saved,
                "url": url,
            }
        )
        known_urls.add(url)
        new_count += 1

    write_index(feed_dir, feed_title, feed_url, mode, articles)
    if checkup:
        print(
            f"Checkup '{feed_title}': {new_count} new, {skipped} already in index "
            f"({len(articles)} total) -> {os.path.join(feed_dir, 'INDEX.md')}"
        )
    else:
        print(
            f"Wrote index for '{feed_title}': {new_count} saved "
            f"({len(articles)} total) -> {os.path.join(feed_dir, 'INDEX.md')}"
        )
    return articles


def main():
    parser = argparse.ArgumentParser(description="An RSS/Atom/OPML feed article downloader.")
    parser.add_argument("--url", "-u", help="URL of a single RSS/Atom feed")
    parser.add_argument(
        "--opml",
        "-p",
        help="Path or URL of an OPML file listing feeds to download",
    )
    parser.add_argument("--output", "-o", help="Directory to save articles into")
    parser.add_argument(
        "--type",
        "-t",
        choices=["full", "simple"],
        help='full: HTML + screenshot when Chromium is available; '
        "simple: screenshot only (requires Chromium)",
    )
    parser.add_argument(
        "--checkup",
        "-c",
        action="store_true",
        help="Only download articles whose URLs are not already listed in INDEX.md",
    )

    args = parser.parse_args()

    if not args.output or not args.type:
        parser.error("Please specify --output/-o and --type/-t (full or simple)")
    if bool(args.url) == bool(args.opml):
        parser.error("Specify exactly one of --url/-u or --opml/-p")

    browser_path = find_browser()
    browser_session = None
    try:
        if browser_path:
            try:
                browser_session = ChromiumSession(browser_path).start()
                print(f"Using browser for screenshots: {browser_path}")
            except Exception as exc:
                print(
                    f"Failed to start browser for screenshots ({exc}). "
                    "Continuing without screenshots.",
                    file=sys.stderr,
                )
                print(screenshot_unavailable_hint(), file=sys.stderr)
                browser_session = None
        else:
            print(screenshot_unavailable_hint(), file=sys.stderr)

        if args.type == "simple" and browser_session is None:
            print(
                "simple mode requires Chromium for screenshots; nothing to do.",
                file=sys.stderr,
            )
            return

        def run_feed(feed_url, preferred_title=None):
            save_feed(
                feed_url,
                args.output,
                args.type,
                preferred_title=preferred_title,
                checkup=args.checkup,
                browser_session=browser_session,
            )

        if args.opml:
            try:
                feeds = parse_opml(args.opml)
            except (OSError, ElementTree.ParseError, requests.RequestException) as exc:
                parser.error(f"Failed to read OPML: {exc}")
            if not feeds:
                parser.error("No feeds with xmlUrl found in OPML")
            print(f"Found {len(feeds)} feed(s) in OPML")
            for item in feeds:
                run_feed(item["url"], preferred_title=item["title"] or None)
        else:
            run_feed(args.url)
    finally:
        if browser_session is not None:
            browser_session.close()

if __name__ == "__main__":
    main()
