# What is RSS Saver?

RSS Saver saves an rss feed from an blog, podcast, a youtube feed, newspaper, etc. as a local html file. This html can be the full html of the page where of the article or it can be stripped down "simple" version of the article. You can then use Linux/Unix tools like `grep` to search those files quickly and easily.

# How do I use it?

## Prerequisites and Installation

RSS Saver is a python script that requires the addition of a few dependencies: BeautifulSoup4, feedparser, requests.

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

The command line options for this script are below. The **url**, **output**, and the **type** are manditory.

```
usage: rss-saver.py [-h] [--url URL] [--output OUTPUT] [--type TYPE]

An RSS feed article downloader.

options:
  -h, --help                    show this help message and exit
  --url URL, -u URL             What is the URL of the RSS Feed?
  --output OUTPUT, -o OUTPUT    Which directory should the articles be saved into?
  --type TYPE, -t TYPE          Do you want "full" or "simple" articles?
```

### About Simple Output

The "simple" output only works with RSS feeds that are properly formatted with "content" tags. It looks like around 50% of the time, feeds do not have those tags and therefore that option is not available. If you're trying to download articles with "simple" and they aren't working, change to the "full" option instead.

# Advanced Usage

In the [resources](resources/) directory, you will find rss_list. This is a list of a 2875 rss feeds. 

You can extract the feeds you want in my_rss_list and download them daily like this:

```
for feed in `cat my_rss_list`;
    do ./rss-saver.py --url $feed -o ~/feeds/ -t simple;
done
```

You can do this with either the full or the simple versions.

I would **not** suggest downloading all 2877 feeds. Choose the ones that you want to monitor and add them your own list. While large, this list is also not exhaustive. Add your own!

All credits to [Kovid Goyal](https://github.com/kovidgoyal/calibre) for rss_list which is used in [Calibre](https://calibre-ebook.com/).

# Why?

Why not just use an RSS reader?

RSS Readers are for reading news feeds only by humans. They are not meant for long-term storage or for quick searching multiple news articles at once.

If you need to keep an eye on any mention of "IBM" in the news, make a list of rss feeds for all of the news sources that you want to monitor, download the articles from those feeds, and search them quickly from your local filesystem.

# Future

I am a novice programmer. This code should be cleaned up to avoid a lot of repitition but for now it works.

I would eventually like to see it have a database backend with very good search functionality.
