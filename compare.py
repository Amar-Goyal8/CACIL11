"""Compare two article URLs and contrast their coverage using Gemini.

Usage:
    python compare.py <article_url_1> <article_url_2>
"""

import argparse
import sys

from dotenv import load_dotenv
from google import genai

from scraper import fetch_article

PROMPT = """You are given the full text of two news articles. Compare and contrast them, and respond in this format:

**Article 1 Headline:** (title or one-line summary of the first article)
**Article 2 Headline:** (title or one-line summary of the second article)

**Shared Topic:** (1-2 sentences on what both articles are covering)

**Where They Agree:**
- (bullet points of facts, framing, or conclusions both articles share)

**Where They Corroborate Each Other:**
- (specific details, statistics, quotes, or sources in one article that back up claims in the other)

**Where They Conflict:**
- (contradictory facts, numbers, timelines, or claims — quote or cite each side)

**Differences in Framing/Perspective:**
- (how the articles differ in tone, emphasis, sources used, or angle, even where facts align)

**Unique to Article 1:**
- (important information only the first article covers)

**Unique to Article 2:**
- (important information only the second article covers)

**Bottom Line:** (2-3 sentences: overall, do these articles corroborate or contradict each other, and what should a reader take away from seeing both?)

Article 1 text:
{text1}

---

Article 2 text:
{text2}
"""


def compare(text1: str, text2: str) -> str:
    client = genai.Client()  # reads GEMINI_API_KEY from env
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=PROMPT.format(text1=text1, text2=text2),
    )
    return response.text


def main():
    parser = argparse.ArgumentParser(description="Compare and contrast two news articles")
    parser.add_argument("url1", help="URL of the first article")
    parser.add_argument("url2", help="URL of the second article")
    args = parser.parse_args()

    load_dotenv()
    print(f"Fetching {args.url1} ...", file=sys.stderr)
    text1 = fetch_article(args.url1)
    print(f"Fetching {args.url2} ...", file=sys.stderr)
    text2 = fetch_article(args.url2)
    print(f"Extracted {len(text1)} + {len(text2)} chars, sending to Gemini ...", file=sys.stderr)
    print(compare(text1, text2))


if __name__ == "__main__":
    main()
