# What is RSS Saver?

**Version 2.0**

RSS Saver saves an RSS or Atom feed from a blog, podcast, YouTube feed, newspaper, etc. as local HTML files and optional full-page screenshots. You can then use Linux/Unix tools like `grep` to search those files quickly and easily.

It also accepts **OPML** subscription lists, downloading every listed feed into its own directory with a Markdown index of what was saved.

## What's new in 2.0

- **OPML** input (`--opml` / `-p`) for batch-saving many feeds
- Per-feed output directories with an **`INDEX.md`** (published/downloaded dates, screenshot names, article URLs)
- **`--checkup`** to skip articles already listed in `INDEX.md`
- Optional **full-page screenshots** via Chrome, Brave, Chromium, or auto-installed `chrome-headless-shell`
- Offline-friendly HTML: absolute URLs, inlined stylesheets, source URL at the bottom of each page

# How do I use it?

## Prerequisites and Installation

RSS Saver is a Python script that requires a few dependencies: BeautifulSoup4, feedparser, requests.

You can install these dependencies with the following commands. If you are using Mac or Windows, you'll probably want to use WSL or research these on your own:

* Debian, Ubuntu
`apt install python3-bs4 python3-feedparser python3-requests`

* Arch
`pacman -S python-beautifulsoup4 python-feedparser python-requests`

* OpenSUSE
`zypper in python3-feedparser python3-requests python3-beautifulsoup4`

### Screenshots (optional)

HTML saving works without a browser. Screenshots use this both/and policy:

1. **Prefer an installed Chrome-based browser** if found: Google Chrome, Brave, or Chromium  
   (`RSS_SAVER_BROWSER` / `CHROME_BIN` override, then `PATH`, then macOS `/Applications` paths).
2. **Otherwise**, if this OS/arch supports Google’s `chrome-headless-shell`  
   (Linux x86_64/ARM64, macOS Intel/Apple Silicon, Windows), **download and cache** it under  
   `~/.cache/rss-saver/chrome-headless-shell/` and use that.
3. **Otherwise** (e.g. OpenBSD with no Chromium installed): print a warning that screenshots  
   are unavailable until a Chrome-based browser is installed. HTML still saves.

Install examples when you want a system browser (or when headless-shell isn’t available):

* Arch: `sudo pacman -S chromium` (or install Brave / Google Chrome)
* Debian/Ubuntu: `sudo apt install chromium`
* macOS: `brew install --cask chromium` (or Chrome / Brave)
* OpenBSD: `doas pkg_add chromium`

Or set `RSS_SAVER_BROWSER=/path/to/chrome-or-chromium`.

Once the Python prerequisites are installed, you can either save the script [directly](https://raw.githubusercontent.com/tgeek77/rss-saver/main/rss-saver.py) or download/clone this repository.

Run `python3 rss-saver.py` to run the script with the options below.

## Run RSS Saver

`--output` and `--type` are required. Provide exactly one of `--url` or `--opml`.

```
usage: rss-saver.py [-h] [--url URL] [--opml OPML] [--output OUTPUT]
                    [--type {full,simple}] [--checkup]

An RSS/Atom/OPML feed article downloader.

options:
  -h, --help                    show this help message and exit
  --url URL, -u URL             URL of a single RSS/Atom feed
  --opml OPML, -p OPML          Path or URL of an OPML file listing feeds
  --output OUTPUT, -o OUTPUT    Directory to save articles into
  --type {full,simple}, -t      full: HTML + screenshot; simple: screenshot only
  --checkup, -c                 Only download articles not already in INDEX.md
```

### Modes

| Type | HTML | Screenshot |
|------|------|------------|
| `full` | Always | When a Chrome-based browser (or headless-shell) is available |
| `simple` | No | When a Chrome-based browser (or headless-shell) is available (required) |

If no browser is available: `full` still saves HTML and prints an install hint; `simple` prints the hint and does nothing (does not crash).

### Single feed

```
./rss-saver.py -u https://example.com/feed.xml -o ~/feeds -t full
```

### OPML (many feeds)

```
./rss-saver.py --opml ~/subscriptions.opml -o ~/feeds -t full
./rss-saver.py -p https://example.com/feeds.opml -o ~/feeds -t simple
```

### Checkup (only new items)

Re-run the same feed or OPML later without re-downloading articles already listed in that feed’s `INDEX.md` (matched by article URL):

```
./rss-saver.py -u https://example.com/feed.xml -o ~/feeds -t full --checkup
./rss-saver.py -p ~/subscriptions.opml -o ~/feeds -t full -c
```

Without `--checkup`, the index is rewritten for this run and matching files may be overwritten.

OPML outlines that have an `xmlUrl` attribute are treated as feeds. Category folders without `xmlUrl` are skipped for nesting (feeds are saved flat under `--output`).

### Output layout

Each feed gets its own subdirectory under `--output`, named from the feed title (or OPML title). Inside that directory:

```
~/feeds/
  ExampleBlog/
    INDEX.md
    SomeArticleTitle.html
    SomeArticleTitle.png
  AnotherFeed/
    INDEX.md
    ...
```

`INDEX.md` is a human-readable index of everything currently in that feed directory:

- Feed URL
- Last updated timestamp (UTC)
- Type (`full` or `simple`)
- Article count
- Table of file name, title, published date (from the feed), downloaded date, screenshot filename, and article URL

Each `.html` file ends with a visible `URL: ...` line at the bottom of the page (valid HTML, so formatting stays intact). Linked site stylesheets are inlined so `file://` viewing keeps the theme.

# Advanced Usage

In the [resources](resources/) directory, you will find `rss_list.txt`. This is a list of 2875 RSS feeds.

You can extract the feeds you want into `my_rss_list` and download them daily like this:

```
for feed in `cat my_rss_list`;
    do ./rss-saver.py --url $feed -o ~/feeds/ -t full --checkup;
done
```

Or put those URLs into an OPML file and run once with `--opml`.

I would **not** suggest downloading all 2877 feeds. Choose the ones that you want to monitor and add them your own list. While large, this list is also not exhaustive. Add your own!

All credits to [Kovid Goyal](https://github.com/kovidgoyal/calibre) for rss_list which is used in [Calibre](https://calibre-ebook.com/).

# Why?

Why not just use an RSS reader?

RSS Readers are for reading news feeds only by humans. They are not meant for long-term storage or for quick searching multiple news articles at once.

If you need to keep an eye on any mention of "IBM" in the news, make a list of rss feeds for all of the news sources that you want to monitor, download the articles from those feeds, and search them quickly from your local filesystem.

# Future

I am a novice programmer. This code should be cleaned up to avoid a lot of repitition but for now it works.

I would eventually like to see it have a database backend with very good search functionality.
