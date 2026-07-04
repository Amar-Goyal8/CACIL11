"""Scrape an article URL and extract key details using Gemini.

Usage:
    python scraper.py <article_url>
"""

import argparse
import sys

import trafilatura
from dotenv import load_dotenv
from google import genai

PROMPT = """You are given the full text of a news article. Extract the key details and respond in this format:

**Headline:** (the article's title or a one-line summary)
**Summary:** (2-3 sentence summary)
**Key Facts:**
- (bullet points of the most important facts)
**People/Organizations:** (who is involved)
**Date/Location:** (when and where, if mentioned)

Article text:
{text}
"""


def fetch_article(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        sys.exit(f"Error: could not fetch {url}")
    text = trafilatura.extract(downloaded)
    if not text:
        sys.exit("Error: could not extract article text from page")
    return text


def analyze(text: str) -> str:
    client = genai.Client()  # reads GEMINI_API_KEY from env
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=PROMPT.format(text=text),
    )
    return response.text


def main():
    parser = argparse.ArgumentParser(description="Extract key details from a news article")
    parser.add_argument("url", help="URL of the article to analyze")
    args = parser.parse_args()

    load_dotenv()
    print(f"Fetching {args.url} ...", file=sys.stderr)
    text = fetch_article(args.url)
    print(f"Extracted {len(text)} chars, sending to Gemini ...", file=sys.stderr)
    print(analyze(text))


if __name__ == "__main__":
    main()
