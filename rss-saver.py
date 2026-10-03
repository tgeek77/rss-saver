#!/usr/bin/python3
"""RSS Saver — download RSS/Atom/OPML feeds into SQLite (+ optional screenshots/files). Version 2.1."""

import argparse
import base64
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import feedparser
import requests
from bs4 import BeautifulSoup

from rss_saver_db import (
    Store,
    content_hash_bytes,
    content_hash_text,
    default_db_path,
    parse_since,
)
# Installed Chrome-based browsers (preferred over chrome-headless-shell).
SYSTEM_BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "brave-browser",
    "brave",
    "chromium",
    "chromium-browser",
)

MAC_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

CFT_DOWNLOADS_JSON = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)

# Desktop faux display for screenshots (match a real browser window; full-page height scrolls).
# ~1024 collapses many sites' sidebars; 1920x1080 matches typical FireShot desktop captures.
SCREENSHOT_WIDTH = 1920
SCREENSHOT_HEIGHT = 1080

# Light-touch offline CSS: do not override site layout/theme, only tame runaway media.
OFFLINE_HTML_CSS = """
/* rss-saver offline media safeguards (keep site CSS otherwise intact) */
img, picture img, video, svg, canvas, iframe, object, embed {
  max-width: 100% !important;
  height: auto !important;
}
img[src^="data:"] {
  max-width: 100% !important;
  max-height: 90vh !important;
  width: auto !important;
  object-fit: contain !important;
}
pre, table {
  max-width: 100% !important;
  overflow-x: auto !important;
}
"""

URL_ATTRS = (
    "href",
    "src",
    "action",
    "poster",
    "data",
    "cite",
    "formaction",
    "icon",
    "manifest",
    "background",
)


def absolutize_url(base_url, value):
    """Turn a URL-like attribute value into an absolute URL when possible."""
    if not value or not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return value
    lower = value.lower()
    if lower.startswith(
        ("data:", "javascript:", "mailto:", "tel:", "blob:", "#", "about:")
    ):
        return value
    return urljoin(base_url, value)


def absolutize_srcset(base_url, value):
    """Rewrite each candidate URL in a srcset attribute."""
    if not value or not isinstance(value, str):
        return value
    parts = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        bits = candidate.split()
        bits[0] = absolutize_url(base_url, bits[0])
        parts.append(" ".join(bits))
    return ", ".join(parts)


def absolutize_css_urls(base_url, css_text):
    """Rewrite url(...) references inside CSS text."""
    if not css_text:
        return css_text

    def repl(match):
        quote = match.group(1) or ""
        raw = match.group(2).strip().strip("'\"")
        abs_url = absolutize_url(base_url, raw)
        return f"url({quote}{abs_url}{quote})"

    return re.sub(
        r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)",
        repl,
        css_text,
        flags=re.IGNORECASE,
    )


def make_urls_absolute(soup, page_url):
    """Rewrite root-relative and relative URLs so file:// viewing still loads assets."""
    base_url = page_url
    base_tag = soup.find("base", href=True)
    if base_tag and base_tag.get("href"):
        base_url = urljoin(page_url, base_tag["href"])

    for tag in soup.find_all(True):
        for attr in URL_ATTRS:
            if tag.has_attr(attr):
                tag[attr] = absolutize_url(base_url, tag.get(attr))
        if tag.has_attr("srcset"):
            tag["srcset"] = absolutize_srcset(base_url, tag.get("srcset"))
        if tag.has_attr("style"):
            tag["style"] = absolutize_css_urls(base_url, tag.get("style"))

    for style in soup.find_all("style"):
        if style.string:
            style.string = absolutize_css_urls(base_url, str(style.string))

    # Prefer a single absolute <base> so any remaining relatives resolve correctly.
    for old_base in soup.find_all("base"):
        old_base.decompose()
    if soup.head:
        base = soup.new_tag("base", href=page_url)
        soup.head.insert(0, base)

    return soup


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
    """Return an installed Chrome/Brave/Chromium binary, or None."""
    for env_name in ("RSS_SAVER_BROWSER", "CHROME_BIN"):
        override = os.environ.get(env_name)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override

    for name in SYSTEM_BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path

    for path in MAC_BROWSER_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None


def cft_platform():
    """
    Map this machine to a Chrome for Testing platform id, or None if unsupported.
    Supported: linux64, linux-arm64, mac-x64, mac-arm64, win32, win64.
    """
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Linux":
        if machine in ("aarch64", "arm64"):
            return "linux-arm64"
        if machine in ("x86_64", "amd64"):
            return "linux64"
        return None
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "mac-arm64"
        return "mac-x64"
    if system == "Windows":
        if machine in ("amd64", "x86_64"):
            return "win64"
        return "win32"
    return None


def headless_shell_supported():
    return cft_platform() is not None


def rss_saver_cache_dir():
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(xdg, "rss-saver")
    return os.path.join(os.path.expanduser("~"), ".cache", "rss-saver")


def find_headless_shell():
    """Find chrome-headless-shell on PATH or in the rss-saver cache."""
    path = shutil.which("chrome-headless-shell")
    if path:
        return path

    cache_root = os.path.join(rss_saver_cache_dir(), "chrome-headless-shell")
    if not os.path.isdir(cache_root):
        return None
    for root, _dirs, files in os.walk(cache_root):
        for name in files:
            if name in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                candidate = os.path.join(root, name)
                if os.access(candidate, os.X_OK) or name.endswith(".exe"):
                    return candidate
    return None


def install_chrome_headless_shell():
    """
    Download Stable chrome-headless-shell for this platform into the cache.
    Returns the binary path, or raises on failure.
    """
    plat = cft_platform()
    if not plat:
        raise RuntimeError("chrome-headless-shell is not available for this OS/arch")

    print(
        f"No Chrome/Brave/Chromium found; downloading chrome-headless-shell ({plat})...",
        file=sys.stderr,
    )
    response = requests.get(CFT_DOWNLOADS_JSON, timeout=60)
    response.raise_for_status()
    data = response.json()
    downloads = (
        data.get("channels", {})
        .get("Stable", {})
        .get("downloads", {})
        .get("chrome-headless-shell", [])
    )
    url = None
    version = data.get("channels", {}).get("Stable", {}).get("version", "unknown")
    for item in downloads:
        if item.get("platform") == plat:
            url = item.get("url")
            break
    if not url:
        raise RuntimeError(
            f"No chrome-headless-shell download listed for platform {plat}"
        )

    dest_dir = os.path.join(rss_saver_cache_dir(), "chrome-headless-shell", version, plat)
    os.makedirs(dest_dir, exist_ok=True)
    binary_name = (
        "chrome-headless-shell.exe" if plat.startswith("win") else "chrome-headless-shell"
    )
    existing = None
    for root, _dirs, files in os.walk(dest_dir):
        if binary_name in files:
            existing = os.path.join(root, binary_name)
            break
    if existing and (os.access(existing, os.X_OK) or existing.endswith(".exe")):
        print(f"Using cached chrome-headless-shell: {existing}")
        return existing

    print(f"Fetching {url}", file=sys.stderr)
    zip_resp = requests.get(url, timeout=300)
    zip_resp.raise_for_status()
    with zipfile.ZipFile(BytesIO(zip_resp.content)) as zf:
        zf.extractall(dest_dir)

    binary_path = None
    for root, _dirs, files in os.walk(dest_dir):
        if binary_name in files:
            binary_path = os.path.join(root, binary_name)
            break
    if not binary_path:
        raise RuntimeError(f"chrome-headless-shell binary missing after extract in {dest_dir}")

    if not binary_path.endswith(".exe"):
        os.chmod(
            binary_path,
            os.stat(binary_path).st_mode | 0o111,
        )
    print(f"Installed chrome-headless-shell to {binary_path}")
    return binary_path


def resolve_browser_for_screenshots():
    """
    1) Use installed Chrome / Brave / Chromium if present.
    2) Else use chrome-headless-shell (PATH/cache), installing it when supported.
    3) Else return None (screenshots unavailable).
    """
    browser = find_browser()
    if browser:
        return browser

    shell = find_headless_shell()
    if shell:
        return shell

    if headless_shell_supported():
        try:
            return install_chrome_headless_shell()
        except Exception as exc:
            print(
                f"Failed to install chrome-headless-shell: {exc}",
                file=sys.stderr,
            )
            return None

    return None


def screenshot_unavailable_hint():
    if headless_shell_supported():
        return (
            "Screenshots unavailable: no Chrome/Brave/Chromium found, and "
            "chrome-headless-shell could not be installed automatically.\n"
            "Install a Chrome-based browser, e.g.:\n"
            "  Arch:     sudo pacman -S chromium\n"
            "  Debian:   sudo apt install chromium\n"
            "  macOS:    brew install --cask chromium\n"
            "  Or Brave / Google Chrome\n"
            "Or set RSS_SAVER_BROWSER=/path/to/chrome-or-chromium"
        )
    return (
        "Screenshots unavailable: no Chrome/Brave/Chromium found, and "
        "chrome-headless-shell is not available for this OS/architecture.\n"
        "Install a Chrome-based browser to enable screenshots, e.g.:\n"
        "  Arch:     sudo pacman -S chromium\n"
        "  Debian:   sudo apt install chromium\n"
        "  macOS:    brew install --cask chromium\n"
        "  OpenBSD:  doas pkg_add chromium\n"
        "  Or Brave / Google Chrome\n"
        "Or set RSS_SAVER_BROWSER=/path/to/chrome-or-chromium"
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
    """Best-effort article date from feedparser entry fields (UTC ISO), or ''."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated", "created"):
        value = getattr(entry, key, None)
        if not value:
            continue
        # Prefer structured parse of RFC2822 / common feed date strings
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
        try:
            raw = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
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


def inline_remote_stylesheets(soup, timeout=30):
    """
    Fetch linked stylesheets and embed them so file:// viewing keeps the site theme.
    Leaves font provider links (e.g. fonts.googleapis.com) as remote links.
    """
    for link in list(soup.find_all("link", href=True)):
        rel = " ".join(link.get("rel") or []).lower()
        href = link.get("href") or ""
        if "stylesheet" not in rel and ".css" not in href.split("?", 1)[0].lower():
            continue
        if "fonts.googleapis.com" in href or "fonts.gstatic.com" in href:
            continue
        if not href.startswith(("http://", "https://")):
            continue
        try:
            response = requests.get(href, timeout=timeout)
            response.raise_for_status()
            css_text = absolutize_css_urls(href, response.text)
            style = soup.new_tag("style", attrs={"data-rss-saver-href": href})
            style.string = css_text
            link.replace_with(style)
        except requests.RequestException as exc:
            print(
                f"Warning: could not inline stylesheet {href}: {exc}",
                file=sys.stderr,
            )


def fetch_full_html(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    # Prefer the final URL after redirects as the absolutization base.
    page_url = response.url or url
    soup = BeautifulSoup(response.content, "html.parser")
    make_urls_absolute(soup, page_url)
    inline_remote_stylesheets(soup)

    # Ensure a sensible viewport for local viewing.
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if viewport is None:
        viewport = soup.new_tag("meta", attrs={"name": "viewport"})
        if soup.head:
            soup.head.insert(0, viewport)
    viewport["content"] = "width=device-width, initial-scale=1"

    # Light media safeguards; do not override the site's layout/theme.
    style = soup.new_tag("style", attrs={"id": "rss-saver-offline"})
    style.string = OFFLINE_HTML_CSS
    if soup.head:
        soup.head.append(style)
    elif soup.html:
        head = soup.new_tag("head")
        head.append(style)
        soup.html.insert(0, head)
    else:
        soup.insert(0, style)

    # Visible, greppable source URL at end of page (valid HTML, does not break layout).
    url_line = soup.new_tag("p", attrs={"id": "rss-saver-source-url"})
    url_line.string = f"URL: {page_url}"
    if soup.body:
        soup.body.append(url_line)
    elif soup.html:
        soup.html.append(url_line)
    else:
        soup.append(url_line)
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
            f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}",
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

    def capture_screenshot_bytes(self, url):
        """Navigate and return full-page PNG bytes."""
        ws = _WebSocketClient(self._ws_url_for_page(), timeout=self.timeout)
        try:
            self._cdp(ws, "Page.enable")
            self._cdp(
                ws,
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": SCREENSHOT_WIDTH,
                    "height": SCREENSHOT_HEIGHT,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": SCREENSHOT_WIDTH,
                    "screenHeight": SCREENSHOT_HEIGHT,
                },
            )
            try:
                self._cdp(ws, "Emulation.setScrollbarsHidden", {"hidden": True})
            except RuntimeError:
                pass
            self._cdp(
                ws,
                "Page.navigate",
                {"url": url},
                wait_event="Page.loadEventFired",
                event_timeout=self.timeout,
            )
            time.sleep(1.0)
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
            return base64.b64decode(data)
        finally:
            ws.close()

    def capture_screenshot(self, url, dest_png):
        png = self.capture_screenshot_bytes(url)
        with open(dest_png, "wb") as f:
            f.write(png)
        return png
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


def html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)


def print_table(rows, columns):
    if not rows:
        print("(none)")
        return
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(row.get(c, "") if row.get(c) is not None else "")))
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print(
            "  ".join(
                str(row.get(c, "") if row.get(c) is not None else "").ljust(widths[c])
                for c in columns
            )
        )


def emit(data, as_json=False, columns=None):
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if isinstance(data, list):
        if not data:
            print("(none)")
            return
        cols = columns or list(data[0].keys())
        print_table(data, cols)
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {v}")
    else:
        print(data)


def pull_feed(
    store,
    feed_url,
    mode,
    preferred_title=None,
    output_root=None,
    download_files=False,
    force=False,
    browser_session=None,
    browser_lock=None,
):
    """
    Delta pull one feed into SQLite. Optionally dual-write files with --dl.
    Returns dict with counts.
    """
    feed_row = store.upsert_feed(feed_url, preferred_title or "")
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:
        store.touch_feed(feed_row["id"], error=str(exc))
        print(f"Failed to parse feed {feed_url}: {exc}", file=sys.stderr)
        return {"feed": feed_url, "new": 0, "skipped": 0, "errors": 1}

    feed_title = preferred_title or parsed.feed.get("title") or feed_url
    store.upsert_feed(feed_url, feed_title)
    feed_row = store.get_feed(feed_url)

    if mode == "simple" and browser_session is None:
        print(
            f"Skipping feed '{feed_title}': simple mode needs a browser for screenshots",
            file=sys.stderr,
        )
        store.touch_feed(feed_row["id"], error="simple mode requires browser")
        return {"feed": feed_title, "new": 0, "skipped": 0, "errors": 1}

    feed_dir = None
    disk_articles = []
    if download_files:
        if not output_root:
            raise ValueError("--dl requires --output")
        feed_dir = os.path.join(output_root, feed_dir_name(feed_title, feed_url))
        os.makedirs(feed_dir, exist_ok=True)
        disk_articles = load_index(feed_dir)

    new_count = 0
    skipped = 0
    errors = 0

    for entry in parsed.entries:
        title = getattr(entry, "title", None) or "untitled"
        url = getattr(entry, "link", None)
        if not url:
            print(f"Skipping entry without link in '{feed_title}'", file=sys.stderr)
            continue

        item = store.ensure_item(feed_row["id"], url)

        # Fast path: if not forcing and we already have any revision, we still
        # must fetch to detect content changes — unless we only skip when we
        # cannot/won't fetch. Plan: always fetch for delta hash compare unless
        # we could skip without fetch... We need content to hash. So we fetch.
        # Optimization: without --force, still fetch HTML to compare hash.

        article_html = None
        png_bytes = None
        digest = None

        if mode == "full":
            try:
                article_html = fetch_full_html(url)
            except requests.RequestException as exc:
                print(f"Failed to fetch '{title}' ({url}): {exc}", file=sys.stderr)
                errors += 1
                continue
            digest = content_hash_text(article_html)
            if not force and store.has_content_hash(item["id"], digest):
                skipped += 1
                continue

        if browser_session is not None:
            try:
                lock = browser_lock or threading.Lock()
                with lock:
                    png_bytes = browser_session.capture_screenshot_bytes(url)
            except Exception as exc:
                print(f"Screenshot failed for '{title}' ({url}): {exc}", file=sys.stderr)
                if mode == "simple":
                    errors += 1
                    continue
            if mode == "simple":
                if not png_bytes:
                    errors += 1
                    continue
                digest = content_hash_bytes(png_bytes)
                if not force and store.has_content_hash(item["id"], digest):
                    skipped += 1
                    continue

        if not digest:
            errors += 1
            continue

        if not force and store.has_content_hash(item["id"], digest):
            skipped += 1
            continue

        # Determine next rev number for optional disk naming
        existing = store.list_revisions(item["uuid"])
        next_rev = (existing[0]["rev"] if existing else 0) + 1

        html_path = None
        png_path = None
        html_name = ""
        png_name = ""
        if download_files and feed_dir:
            base = article_basename(title)
            if next_rev > 1:
                base = f"{base}_r{next_rev}"
            if article_html is not None:
                html_name = f"{base}.html"
                html_path_full = os.path.join(feed_dir, html_name)
                with open(html_path_full, "w", encoding="utf-8") as f:
                    f.write(article_html)
                html_path = html_name
                print(f"Article '{title}' saved to '{html_path_full}'")
            if png_bytes is not None:
                png_name = f"{base}.png"
                png_path_full = os.path.join(feed_dir, png_name)
                with open(png_path_full, "wb") as f:
                    f.write(png_bytes)
                png_path = png_name
                print(f"Screenshot '{title}' saved to '{png_path_full}'")

        rev = store.insert_revision(
            item["id"],
            content_hash=digest,
            title=title,
            published_at=format_entry_date(entry),
            mode=mode,
            html=article_html,
            screenshot=png_bytes,
            html_path=html_path,
            screenshot_path=png_path,
            body_text=html_to_text(article_html) if article_html else title,
        )
        if rev is None:
            skipped += 1
            continue

        new_count += 1
        print(
            f"Stored '{title}' item={item['uuid']} rev={rev['rev']} "
            f"revision={rev['uuid']}"
        )
        if download_files:
            disk_articles.append(
                {
                    "file": html_name,
                    "title": title,
                    "published": format_entry_date(entry),
                    "downloaded": utc_now(),
                    "screenshot": png_name,
                    "url": url,
                }
            )

    if download_files and feed_dir:
        # Merge unique by URL keeping latest disk_articles entries last
        by_url = {}
        for a in load_index(feed_dir) + disk_articles:
            by_url[a["url"]] = a
        write_index(feed_dir, feed_title, feed_url, mode, list(by_url.values()))

    store.touch_feed(feed_row["id"], error=None)
    print(
        f"Delta '{feed_title}': {new_count} new revision(s), {skipped} unchanged, "
        f"{errors} error(s)"
    )
    return {
        "feed": feed_title,
        "feed_uuid": feed_row["uuid"],
        "new": new_count,
        "skipped": skipped,
        "errors": errors,
    }


def run_pull_feeds(
    store,
    feeds,
    mode,
    *,
    jobs=8,
    output_root=None,
    download_files=False,
    force=False,
    run_gc=False,
    as_json=False,
):
    """
    Delta-pull a list of {"url", "title"} feeds. Returns result dicts.
    Skips unchanged content hashes; only stores new URLs / changed revisions.
    """
    if not feeds:
        return []
    if mode == "simple":
        pass  # validated after browser resolve

    browser_path = resolve_browser_for_screenshots()
    browser_session = None
    browser_lock = threading.Lock()
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

        if mode == "simple" and browser_session is None:
            raise SystemExit(
                "simple mode requires a Chrome-based browser for screenshots"
            )

        results = []

        def work(item):
            return pull_feed(
                store,
                item["url"],
                mode,
                preferred_title=item.get("title") or None,
                output_root=output_root,
                download_files=download_files,
                force=force,
                browser_session=browser_session,
                browser_lock=browser_lock,
            )

        workers = max(1, int(jobs))
        print(f"Updating {len(feeds)} feed(s); jobs={workers}")
        if workers == 1 or len(feeds) == 1:
            for item in feeds:
                results.append(work(item))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(work, item): item for item in feeds}
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        item = futs[fut]
                        print(
                            f"Feed worker failed for {item['url']}: {exc}",
                            file=sys.stderr,
                        )
                        results.append(
                            {
                                "feed": item["url"],
                                "new": 0,
                                "skipped": 0,
                                "errors": 1,
                            }
                        )

        if run_gc:
            removed = store.gc(dry_run=False)
            print(f"GC removed {len(removed)} item(s)")

        emit(results, as_json=as_json, columns=["feed", "new", "skipped", "errors"])
        return results
    finally:
        if browser_session is not None:
            browser_session.close()


def cmd_pull(args, store):
    if not args.type:
        raise SystemExit("pull requires --type/-t (full or simple)")
    sources = sum(bool(x) for x in (args.url, args.opml, getattr(args, "all", False)))
    if sources != 1:
        raise SystemExit("Specify exactly one of --url/-u, --opml/-p, or --all")
    if args.dl and not args.output:
        raise SystemExit("--dl requires --output/-o")

    if getattr(args, "all", False):
        rows = store.list_feeds()
        if not rows:
            raise SystemExit("No feeds in the database yet; add some with add/pull first")
        feeds = [{"url": r["url"], "title": r["title"] or ""} for r in rows]
    elif args.opml:
        try:
            feeds = parse_opml(args.opml)
        except (OSError, ElementTree.ParseError, requests.RequestException) as exc:
            raise SystemExit(f"Failed to read OPML: {exc}") from exc
        if not feeds:
            raise SystemExit("No feeds with xmlUrl found in OPML")
    else:
        feeds = [{"url": args.url, "title": ""}]

    run_pull_feeds(
        store,
        feeds,
        args.type,
        jobs=args.jobs,
        output_root=args.output,
        download_files=args.dl,
        force=args.force,
        run_gc=args.gc,
        as_json=args.json,
    )


def cmd_update(args, store):
    """Delta-pull every feed already stored in the database."""
    mode = args.type or "full"
    rows = store.list_feeds()
    if not rows:
        raise SystemExit("No feeds in the database yet; add some with add/pull first")
    feeds = [{"url": r["url"], "title": r["title"] or ""} for r in rows]
    run_pull_feeds(
        store,
        feeds,
        mode,
        jobs=args.jobs,
        force=args.force,
        run_gc=args.gc,
        as_json=args.json,
    )


def cmd_new(args, store):
    since = parse_since(args.since)
    rows = store.list_new(since)
    if args.json:
        emit(rows, as_json=True)
        return
    # Group summary
    feeds = {}
    for r in rows:
        key = r["feed_title"] or r["feed_url"]
        feeds.setdefault(key, {"new_items": 0, "new_revisions": 0, "rows": []})
        if r["rev_count"] == 1 and r["rev"] == 1:
            feeds[key]["new_items"] += 1
        else:
            feeds[key]["new_revisions"] += 1
        feeds[key]["rows"].append(r)
    print(f"Since {since}: {len(rows)} revision(s) across {len(feeds)} feed(s)\n")
    for feed_name, info in sorted(feeds.items()):
        print(
            f"## {feed_name}  (new items={info['new_items']}, "
            f"new revisions of existing={info['new_revisions']})"
        )
        print_table(
            info["rows"],
            ["published_at", "rev", "title", "item_uuid", "revision_uuid", "frozen"],
        )
        print()


def cmd_feeds(args, store):
    since = parse_since(args.updated_since) if args.updated_since else None
    rows = [dict(r) for r in store.list_feeds(updated_since=since)]
    emit(
        rows,
        as_json=args.json,
        columns=[
            "uuid",
            "title",
            "url",
            "retention_days",
            "item_count",
            "new_revs",
        ],
    )


def cmd_list(args, store):
    since = parse_since(args.since) if args.since else None
    until = parse_since(args.until) if args.until else None
    rows = store.list_items(
        feed=args.feed,
        tag=args.tag,
        since=since,
        until=until,
        url=args.url_filter,
        limit=args.limit,
        all_revisions=args.all_revisions,
        sort=args.sort,
    )
    emit(
        rows,
        as_json=args.json,
        columns=[
            "published_at",
            "feed_title",
            "title",
            "rev",
            "item_uuid",
            "revision_uuid",
            "frozen",
        ],
    )


def cmd_search(args, store):
    since = parse_since(args.since) if args.since else None
    rows = store.search(
        args.query,
        feed=args.feed,
        tag=args.tag,
        since=since,
        limit=args.limit,
    )
    emit(
        rows,
        as_json=args.json,
        columns=["revision_uuid", "title", "feed_title", "snippet", "item_uuid"],
    )


def cmd_revisions(args, store):
    rows = store.list_revisions(args.item)
    emit(
        rows,
        as_json=args.json,
        columns=["uuid", "rev", "published_at", "title", "content_hash", "html_bytes", "png_bytes"],
    )


def cmd_show(args, store):
    data = store.show_revision(args.revision)
    if not data:
        raise SystemExit(f"revision not found: {args.revision}")
    if args.meta_only or not args.json:
        slim = {k: v for k, v in data.items()}
        emit(slim, as_json=args.json)
    else:
        emit(data, as_json=True)


def cmd_export(args, store):
    if args.revision:
        keys = [args.revision]
    elif args.since:
        since = parse_since(args.since)
        keys = [r["revision_uuid"] for r in store.list_new(since)]
    else:
        raise SystemExit("export requires REVISION or --since")
    dest = args.output or tempfile.mkdtemp(prefix="rss-saver-export-")
    written = []
    for key in keys:
        paths = store.export_revision(key, os.path.join(dest, key))
        written.append({"revision": key, **paths})
    emit(written, as_json=args.json)


def cmd_tag(args, store):
    if args.tag_action == "list":
        rows = [dict(r) for r in store.list_tags(args.item)]
        emit(rows, as_json=args.json, columns=["id", "name"])
        return
    if not args.item or not args.names:
        raise SystemExit("tag add/rm requires ITEM and tag names")
    if args.tag_action == "add":
        applied = store.tag_item(args.item, args.names)
        emit({"item": args.item, "tags": applied}, as_json=args.json)
    else:
        store.untag_item(args.item, args.names)
        emit({"item": args.item, "removed": args.names}, as_json=args.json)


def cmd_freeze(args, store):
    item = store.set_frozen(args.item, True)
    emit({"item_uuid": item["uuid"], "frozen": True}, as_json=args.json)


def cmd_unfreeze(args, store):
    item = store.set_frozen(args.item, False)
    emit({"item_uuid": item["uuid"], "frozen": False}, as_json=args.json)


def cmd_retention(args, store):
    if args.retention_action == "set":
        days = None if args.days in (None, 0, "0", "unlimited") else int(args.days)
        feed = store.set_retention(args.feed, days)
        emit(
            {
                "feed_uuid": feed["uuid"],
                "title": feed["title"],
                "retention_days": feed["retention_days"],
            },
            as_json=args.json,
        )
        return
    # get
    feed = store.get_feed(args.feed)
    if not feed:
        raise SystemExit(f"feed not found: {args.feed}")
    emit(dict(feed), as_json=args.json)


def cmd_gc(args, store):
    removed = store.gc(dry_run=args.dry_run)
    emit(removed, as_json=args.json)


def cmd_delete(args, store):
    store.delete_item(args.item, force=args.force)
    emit({"deleted": args.item}, as_json=args.json)


def cmd_delete_feed(args, store):
    try:
        result = store.delete_feed(args.feed, force=args.force)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    except PermissionError as exc:
        raise SystemExit(str(exc)) from exc
    emit(result, as_json=args.json)


def cmd_add(args, store):
    """Register feed(s) from --url or --opml (path or https), then delta-pull them."""
    if bool(args.url) == bool(args.opml):
        raise SystemExit("Specify exactly one of --url/-u or --opml/-p")
    mode = args.type or "full"
    if args.opml:
        try:
            feeds = parse_opml(args.opml)
        except (OSError, ElementTree.ParseError, requests.RequestException) as exc:
            raise SystemExit(f"Failed to read OPML: {exc}") from exc
        if not feeds:
            raise SystemExit("No feeds with xmlUrl found in OPML")
    else:
        feeds = [{"url": args.url, "title": ""}]
    for item in feeds:
        store.upsert_feed(item["url"], item.get("title") or "")
    run_pull_feeds(
        store,
        feeds,
        mode,
        jobs=args.jobs,
        as_json=args.json,
    )


def _open_path(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


def cmd_open(args, store):
    rev = store.get_revision(args.revision)
    if not rev:
        raise SystemExit(f"revision not found: {args.revision}")
    dest = tempfile.mkdtemp(prefix="rss-saver-open-")
    paths = store.export_revision(args.revision, dest)
    target = paths.get("html") or paths.get("png")
    if not target:
        raise SystemExit("revision has no HTML or PNG to open")
    print(target)
    if not args.no_launch:
        _open_path(target)


def cmd_serve(args, store):
    since = parse_since(args.since) if args.since else None
    port = int(args.port)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *fmt_args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                rows = store.list_new(since) if since else store.list_items(limit=200)
                body = [
                    "<!doctype html><meta charset=utf-8><title>rss-saver</title>",
                    "<style>body{font-family:sans-serif;max-width:960px;margin:2rem auto}"
                    "a{color:#06c} .meta{color:#666;font-size:0.9em}</style>",
                    "<h1>rss-saver archive</h1>",
                ]
                if since:
                    body.append(f"<p class=meta>Since {since}</p>")
                body.append("<ul>")
                for r in rows:
                    ru = r.get("revision_uuid")
                    title = (r.get("title") or "(untitled)").replace("<", "&lt;")
                    feed = (r.get("feed_title") or "").replace("<", "&lt;")
                    body.append(
                        f"<li><strong>{feed}</strong>: "
                        f"<a href='/r/{ru}.html'>{title}</a> "
                        f"<a href='/r/{ru}.png'>[png]</a> "
                        f"<span class=meta>rev {r.get('rev')} "
                        f"{r.get('published_at')}</span></li>"
                    )
                body.append("</ul>")
                data = "\n".join(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path.startswith("/r/") and path.endswith(".html"):
                key = path[len("/r/") : -len(".html")]
                rev = store.get_revision(key)
                if not rev or not rev["html"]:
                    self.send_error(404)
                    return
                data = rev["html"].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path.startswith("/r/") and path.endswith(".png"):
                key = path[len("/r/") : -len(".png")]
                rev = store.get_revision(key)
                if not rev or not rev["screenshot"]:
                    self.send_error(404)
                    return
                data = rev["screenshot"]
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Serving {store.path} at {url}  (Ctrl+C to stop)")
    if args.open_browser:
        _open_path(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def cmd_tui(args, store):
    try:
        from rss_saver_tui import run_tui
    except ImportError as exc:
        raise SystemExit(
            "TUI requires the 'textual' package. Install with: pip install textual\n"
            f"({exc})"
        ) from exc

    def add_source(source: str, kind: str = "auto"):
        """
        Add an RSS URL or OPML (local path / https URL) and pull.
        kind: 'rss', 'opml', or 'auto' (detect by .opml extension or content).
        Returns a short status string.
        """
        source = (source or "").strip()
        if not source:
            raise ValueError("empty source")
        lower = source.lower()
        is_opml = kind == "opml" or (
            kind == "auto"
            and (
                lower.endswith(".opml")
                or "/opml" in lower
                or lower.endswith(".xml")
                and "opml" in lower
            )
        )
        # Prefer explicit buttons; for auto, try OPML parse if path/url looks like opml
        if kind == "auto":
            if lower.endswith(".opml") or source.startswith(("http://", "https://")) and ".opml" in lower:
                is_opml = True
            elif not source.startswith(("http://", "https://")) and os.path.isfile(source):
                # peek
                try:
                    with open(source, "rb") as f:
                        head = f.read(200).lower()
                    is_opml = b"<opml" in head
                except OSError:
                    is_opml = False
            else:
                is_opml = False

        if is_opml or kind == "opml":
            feeds = parse_opml(source)
            if not feeds:
                raise ValueError("No feeds with xmlUrl found in OPML")
        else:
            feeds = [{"url": source, "title": ""}]

        # Register immediately so they appear in Feeds even if pull fails
        for item in feeds:
            store.upsert_feed(item["url"], item.get("title") or "")

        results = run_pull_feeds(store, feeds, "full", jobs=4, as_json=False)
        new_total = sum(r.get("new", 0) for r in results)
        return f"Added {len(feeds)} feed(s); {new_total} new revision(s)"

    def update_all(mode="full", jobs=8):
        rows = store.list_feeds()
        if not rows:
            raise ValueError("No feeds stored yet")
        feeds = [{"url": r["url"], "title": r["title"] or ""} for r in rows]
        results = run_pull_feeds(store, feeds, mode, jobs=jobs, as_json=False)
        new_total = sum(r.get("new", 0) for r in results)
        skipped = sum(r.get("skipped", 0) for r in results)
        errors = sum(r.get("errors", 0) for r in results)
        return (
            f"Updated {len(feeds)} feed(s): "
            f"{new_total} new, {skipped} unchanged, {errors} error(s)"
        )

    run_tui(store, add_source=add_source, update_all=update_all)


def build_parser():
    parser = argparse.ArgumentParser(
        description="RSS Saver 2.1 — SQLite feed archive with delta pulls, revisions, and review CLI."
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--db",
        default=None,
        help=f"SQLite database path (default: {default_db_path()})",
    )
    shared.add_argument("--json", action="store_true", help="JSON output for agents")

    sub = parser.add_subparsers(dest="command")

    p_pull = sub.add_parser("pull", parents=[shared], help="Delta-pull feed(s) into the database")
    p_pull.add_argument("--url", "-u")
    p_pull.add_argument("--opml", "-p")
    p_pull.add_argument(
        "--all",
        action="store_true",
        help="Delta-pull every feed already stored in the database",
    )
    p_pull.add_argument("--type", "-t", choices=["full", "simple"])
    p_pull.add_argument("--dl", action="store_true", help="Also write HTML/PNG/INDEX.md to disk")
    p_pull.add_argument("--output", "-o", help="Output directory (required with --dl)")
    p_pull.add_argument("--jobs", type=int, default=8, help="Parallel feed workers (default 8)")
    p_pull.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch pages even when a revision may exist (still skips identical hashes)",
    )
    p_pull.add_argument("--gc", action="store_true", help="Run retention GC after pull")
    p_pull.set_defaults(func=cmd_pull)

    p_update = sub.add_parser(
        "update",
        parents=[shared],
        help="Delta-pull all stored feeds (only new/changed articles)",
    )
    p_update.add_argument(
        "--type",
        "-t",
        choices=["full", "simple"],
        default="full",
        help="Capture mode (default: full)",
    )
    p_update.add_argument("--jobs", type=int, default=8)
    p_update.add_argument("--force", action="store_true")
    p_update.add_argument("--gc", action="store_true")
    p_update.set_defaults(func=cmd_update)

    p_new = sub.add_parser(
        "new",
        parents=[shared],
        help="Articles published since a time window",
    )
    p_new.add_argument("--since", required=True, help="e.g. 8h, 7d, or ISO timestamp (publish time)")
    p_new.set_defaults(func=cmd_new)

    p_feeds = sub.add_parser("feeds", parents=[shared], help="List feeds")
    p_feeds.add_argument(
        "--updated-since",
        dest="updated_since",
        help="Feeds with articles published since this window",
    )
    p_feeds.set_defaults(func=cmd_feeds)

    p_list = sub.add_parser("list", parents=[shared], help="List items/revisions")
    p_list.add_argument("--feed")
    p_list.add_argument("--tag")
    p_list.add_argument("--since")
    p_list.add_argument("--until")
    p_list.add_argument("--url-filter", dest="url_filter")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--all-revisions", action="store_true")
    p_list.add_argument("--sort", choices=["published", "downloaded"], default="published")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", parents=[shared], help="Full-text search")
    p_search.add_argument("query")
    p_search.add_argument("--feed")
    p_search.add_argument("--tag")
    p_search.add_argument("--since")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.set_defaults(func=cmd_search)

    p_revs = sub.add_parser("revisions", parents=[shared], help="Revision history for an item UUID/URL")
    p_revs.add_argument("item")
    p_revs.set_defaults(func=cmd_revisions)

    p_show = sub.add_parser("show", parents=[shared], help="Show revision metadata")
    p_show.add_argument("revision")
    p_show.add_argument("--meta-only", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_export = sub.add_parser("export", parents=[shared], help="Export revision BLOBs to a directory")
    p_export.add_argument("revision", nargs="?")
    p_export.add_argument("--since")
    p_export.add_argument("--output", "-o")
    p_export.set_defaults(func=cmd_export)

    p_tag = sub.add_parser("tag", parents=[shared], help="Tag management")
    p_tag.add_argument("tag_action", choices=["add", "rm", "list"])
    p_tag.add_argument("item", nargs="?")
    p_tag.add_argument("names", nargs="*")
    p_tag.set_defaults(func=cmd_tag)

    p_freeze = sub.add_parser("freeze", parents=[shared], help="Freeze an item (exempt from GC)")
    p_freeze.add_argument("item")
    p_freeze.set_defaults(func=cmd_freeze)

    p_unfreeze = sub.add_parser("unfreeze", parents=[shared], help="Unfreeze an item")
    p_unfreeze.add_argument("item")
    p_unfreeze.set_defaults(func=cmd_unfreeze)

    p_ret = sub.add_parser("retention", parents=[shared], help="Get/set per-feed retention days")
    p_ret.add_argument("retention_action", choices=["get", "set"])
    p_ret.add_argument("--feed", required=True)
    p_ret.add_argument("days", nargs="?", help="Days, or 0/unlimited for no limit")
    p_ret.set_defaults(func=cmd_retention)

    p_gc = sub.add_parser("gc", parents=[shared], help="Apply per-feed retention (skips frozen items)")
    p_gc.add_argument("--dry-run", action="store_true")
    p_gc.set_defaults(func=cmd_gc)

    p_del = sub.add_parser("delete", parents=[shared], help="Delete an item and all revisions")
    p_del.add_argument("item")
    p_del.add_argument("--force", action="store_true", help="Allow deleting frozen items")
    p_del.set_defaults(func=cmd_delete)

    p_delfeed = sub.add_parser(
        "delete-feed",
        parents=[shared],
        help="Delete a feed and all of its items/revisions",
    )
    p_delfeed.add_argument("feed", help="Feed UUID, URL, or title")
    p_delfeed.add_argument(
        "--force",
        action="store_true",
        help="Allow deleting a feed that contains frozen items",
    )
    p_delfeed.set_defaults(func=cmd_delete_feed)

    p_add = sub.add_parser(
        "add",
        parents=[shared],
        help="Add an RSS URL or OPML (path or https URL) and pull it",
    )
    p_add.add_argument("--url", "-u", help="Single RSS/Atom feed URL")
    p_add.add_argument("--opml", "-p", help="OPML file path or https URL")
    p_add.add_argument(
        "--type",
        "-t",
        choices=["full", "simple"],
        default="full",
        help="Capture mode (default: full)",
    )
    p_add.add_argument("--jobs", type=int, default=8)
    p_add.set_defaults(func=cmd_add)

    p_open = sub.add_parser("open", parents=[shared], help="Export a revision and open in the browser")
    p_open.add_argument("revision")
    p_open.add_argument("--no-launch", action="store_true")
    p_open.set_defaults(func=cmd_open)

    p_serve = sub.add_parser("serve", parents=[shared], help="Serve HTML/PNG from the DB on localhost")
    p_serve.add_argument("--port", default=8765)
    p_serve.add_argument("--since")
    p_serve.add_argument("--open-browser", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_tui = sub.add_parser("tui", parents=[shared], help="Interactive Textual TUI")
    p_tui.set_defaults(func=cmd_tui)

    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    known = {
        "pull", "update", "add", "new", "feeds", "list", "search", "revisions", "show",
        "export", "tag", "freeze", "unfreeze", "retention", "gc", "delete", "delete-feed",
        "open", "serve", "tui",
    }

    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return

    # If a known subcommand appears anywhere before the first non-option that
    # isn't a flag value, use argv as-is. Otherwise treat as legacy pull flags.
    has_command = any(a in known for a in argv if not a.startswith("-"))
    if has_command:
        # Move subcommand to front if globals precede it: --db x feeds → feeds --db x
        cmd_idx = next(i for i, a in enumerate(argv) if a in known)
        if cmd_idx > 0:
            argv = [argv[cmd_idx]] + argv[:cmd_idx] + argv[cmd_idx + 1 :]
        args = parser.parse_args(argv)
    else:
        args = parser.parse_args(["pull"] + argv)

    db_path = getattr(args, "db", None) or default_db_path()
    store = Store(db_path)
    try:
        if not hasattr(args, "func"):
            parser.print_help()
            return
        args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
