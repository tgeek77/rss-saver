import os
import sys
import feedparser
import requests
from bs4 import BeautifulSoup

def fetch_articles_from_rss(link, output_dir):
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
        if response.status_code == 200:
            # Parse the HTML content of the article
            soup = BeautifulSoup(response.content, 'html.parser')

            # Extract the article text
            article_text = ""
            for paragraph in soup.find_all('p'):
                article_text += paragraph.get_text() + "\n"

            # Extract the title of the article
            title = soup.title.string.strip()

            # Save the article to a file
            filename = os.path.join(output_dir, f"{title}.txt")
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"URL: {url}\n\n")
                f.write(article_text)

            print(f"Article '{title}' saved to '{filename}'")
        else:
            print(f"Failed to fetch article '{title}' from '{url}'")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python fetch_articles.py rss_link output_dir")
        sys.exit(1)

    rss_link = sys.argv[1]
    output_dir = sys.argv[2]

    fetch_articles_from_rss(rss_link, output_dir)

