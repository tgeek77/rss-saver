#!/usr/bin/python3

import os
import re
import sys
import feedparser
import requests
import argparse
from bs4 import BeautifulSoup

def full_content(link, output_dir):
    # Parse the RSS feed
    feed = feedparser.parse(link)

    # Create the output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Iterate through each entry in the RSS feed
    for entry in feed.entries:
        title = entry.title
        url = entry.link

        # Fetch the article content
        response = requests.get(url)
        # Parse the HTML content of the article
        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract the article HTML
        article_html = str(soup)

        # Save the article to a file
        title_processed = re.sub(r'[^a-zA-Z]', '', title)
        filename = os.path.join(output_dir, f"{title_processed}.html")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n")
            f.write(article_html)

            print(f"Article '{title}' saved to '{filename}'")

def minimum_content(link, output_dir):
    # Parse the RSS feed
    feed = feedparser.parse(link)

    # Create the output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Iterate through each entry in the RSS feed
    for entry in feed.entries:
        title = entry.title
        url = entry.link

        # Fetch the article content
        response = requests.get(url)

        # Parse the HTML content of the article
        feedContent = entry.content

        # Extract the article HTML
        article_html = str(feedContent)

        # Save the article to a file
        title_processed = re.sub(r'[^a-zA-Z]', '', title)
        filename = os.path.join(output_dir, f"{title_processed}.html")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n")
            f.write(article_html)

            print(f"Article '{title}' saved to '{filename}'")

def main():
    parser = argparse.ArgumentParser(description='An RSS feed article downloader.')

    # Add the feed, output, and type arguments
    parser.add_argument('--url', '-u', help='What is the URL of the RSS Feed?')
    parser.add_argument('--output', '-o', help='Which directory should the articles be saved into?')
    parser.add_argument('--type', '-t', help='Do you want "full" or "simple" articles?')

    args = parser.parse_args()
    link = args.url
    output_dir = args.output

    if args.type == "full":
        full_content(link, output_dir)
    elif args.type == "simple":
        minimum_content(link, output_dir)
    else:
        parser.error("Please specify a URL for the feed, a path to save the files, and a valid type -t full or -t simple")

if __name__ == "__main__":
    main()
