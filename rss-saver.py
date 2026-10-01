#!/usr/bin/python3

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree

import feedparser
import requests
from bs4 import BeautifulSoup


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


def article_filename(title):
    return f"{slugify(title)}.html"


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


def write_index(feed_dir, feed_title, feed_url, mode, articles):
    """Write INDEX.md describing articles saved in this run."""
    saved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# {feed_title}",
        "",
        f"- Feed URL: {feed_url}",
        f"- Saved: {saved_at}",
        f"- Type: {mode}",
        "",
        "| File | Title | Article URL |",
        "|------|-------|-------------|",
    ]
    for article in articles:
        file_cell = article["file"].replace("|", "\\|")
        title_cell = article["title"].replace("|", "\\|")
        url_cell = article["url"].replace("|", "\\|")
        lines.append(f"| {file_cell} | {title_cell} | {url_cell} |")
    lines.append("")

    index_path = os.path.join(feed_dir, "INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def fetch_full_html(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    return str(soup)


def fetch_simple_html(entry):
    content = getattr(entry, "content", None)
    if not content:
        return None
    # feedparser content is a list of dicts with a 'value' key
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and "value" in first:
            return first["value"]
        return str(first)
    return str(content)


def save_feed(feed_url, output_root, mode, preferred_title=None):
    feed = feedparser.parse(feed_url)
    feed_title = preferred_title or feed.feed.get("title") or feed_url
    feed_dir = os.path.join(output_root, feed_dir_name(feed_title, feed_url))
    os.makedirs(feed_dir, exist_ok=True)

    saved = []
    for entry in feed.entries:
        title = getattr(entry, "title", None) or "untitled"
        url = getattr(entry, "link", None)
        if not url:
            print(f"Skipping entry without link in '{feed_title}'", file=sys.stderr)
            continue

        if mode == "full":
            try:
                article_html = fetch_full_html(url)
            except requests.RequestException as exc:
                print(f"Failed to fetch '{title}' ({url}): {exc}", file=sys.stderr)
                continue
        else:
            article_html = fetch_simple_html(entry)
            if article_html is None:
                print(
                    f"Skipping '{title}': no content tag for simple mode",
                    file=sys.stderr,
                )
                continue

        filename = article_filename(title)
        filepath = os.path.join(feed_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n")
            f.write(article_html)

        print(f"Article '{title}' saved to '{filepath}'")
        saved.append({"file": filename, "title": title, "url": url})

    write_index(feed_dir, feed_title, feed_url, mode, saved)
    print(f"Wrote index for '{feed_title}' -> {os.path.join(feed_dir, 'INDEX.md')}")
    return saved


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
        help='Article type: "full" (fetch page HTML) or "simple" (feed content tag)',
    )

    args = parser.parse_args()

    if not args.output or not args.type:
        parser.error("Please specify --output/-o and --type/-t (full or simple)")
    if bool(args.url) == bool(args.opml):
        parser.error("Specify exactly one of --url/-u or --opml/-p")

    if args.opml:
        try:
            feeds = parse_opml(args.opml)
        except (OSError, ElementTree.ParseError, requests.RequestException) as exc:
            parser.error(f"Failed to read OPML: {exc}")
        if not feeds:
            parser.error("No feeds with xmlUrl found in OPML")
        print(f"Found {len(feeds)} feed(s) in OPML")
        for item in feeds:
            save_feed(item["url"], args.output, args.type, preferred_title=item["title"] or None)
    else:
        save_feed(args.url, args.output, args.type)


if __name__ == "__main__":
    main()
