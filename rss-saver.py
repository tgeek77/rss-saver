#!/usr/bin/python3
"""RSS Saver — download RSS/Atom/OPML feeds as local HTML (+ optional screenshots). Version 2.0."""

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
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import feedparser
import requests
from bs4 import BeautifulSoup

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

    def capture_screenshot(self, url, dest_png):
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
            # Brief settle for late layout / lazy content / responsive CSS
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

    browser_path = resolve_browser_for_screenshots()
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
                "simple mode requires a Chrome-based browser for screenshots; nothing to do.",
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
