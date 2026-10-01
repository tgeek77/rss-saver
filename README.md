# What is RSS Saver?

RSS Saver saves an RSS or Atom feed from a blog, podcast, YouTube feed, newspaper, etc. as local HTML files. HTML can be the full page of the article or a stripped-down "simple" version from the feed's content tags. You can then use Linux/Unix tools like `grep` to search those files quickly and easily.

It also accepts **OPML** subscription lists, downloading every listed feed into its own directory with a Markdown index of what was saved.

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

Once the prerequisites are installed, you can either save the script [directly](https://raw.githubusercontent.com/tgeek77/rss-saver/main/rss-saver.py) or download/clone this repository.

Run `python3 rss-saver.py` to run the script with the options below.

## Run RSS Saver

`--output` and `--type` are required. Provide exactly one of `--url` or `--opml`.

```
usage: rss-saver.py [-h] [--url URL] [--opml OPML] [--output OUTPUT] [--type {full,simple}]

An RSS/Atom/OPML feed article downloader.

options:
  -h, --help                    show this help message and exit
  --url URL, -u URL             URL of a single RSS/Atom feed
  --opml OPML, -p OPML          Path or URL of an OPML file listing feeds
  --output OUTPUT, -o OUTPUT    Directory to save articles into
  --type {full,simple}, -t      "full" (fetch page HTML) or "simple" (feed content)
```

### Single feed

```
./rss-saver.py -u https://example.com/feed.xml -o ~/feeds -t full
```

### OPML (many feeds)

```
./rss-saver.py --opml ~/subscriptions.opml -o ~/feeds -t full
./rss-saver.py -p https://example.com/feeds.opml -o ~/feeds -t simple
```

OPML outlines that have an `xmlUrl` attribute are treated as feeds. Category folders without `xmlUrl` are skipped for nesting (feeds are saved flat under `--output`).

### Output layout

Each feed gets its own subdirectory under `--output`, named from the feed title (or OPML title). Inside that directory:

```
~/feeds/
  ExampleBlog/
    INDEX.md
    SomeArticleTitle.html
  AnotherFeed/
    INDEX.md
    ...
```

`INDEX.md` is a human-readable index for that run:

- Feed URL
- Save timestamp (UTC)
- Type (`full` or `simple`)
- Table of file name, article title, and article URL

Each `.html` file also starts with a `URL: ...` line for easy grepping.

### About Simple Output

The "simple" output only works with feeds that include `content` tags. Roughly half of feeds lack those tags; those entries are skipped in simple mode. If articles are missing, use `-t full` instead.

# Advanced Usage

In the [resources](resources/) directory, you will find `rss_list.txt`. This is a list of 2875 RSS feeds.

You can extract the feeds you want into `my_rss_list` and download them daily like this:

```
for feed in `cat my_rss_list`;
    do ./rss-saver.py --url $feed -o ~/feeds/ -t simple;
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

Optional full-page screenshots (alongside HTML) are under consideration. Faithful captures need a layout engine; the lightest path is likely driving the system Chromium via CDP, or optionally an embedded engine such as servo-fetch / moli if avoiding Chromium entirely.
