"""Given one article URL, find another article that corroborates it and one that contradicts it.

Usage:
    python find_counterparts.py <article_url>
    python find_counterparts.py <article_url> --compare
"""

import argparse
import json
import re
import sys
import urllib.request
from urllib.parse import urlparse

import trafilatura
from dotenv import load_dotenv
from google import genai
from google.genai import types

from compare import compare
from scraper import fetch_article

MODEL = "gemini-2.5-flash"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",  # urllib will not gunzip for us
}

CLAIM_PROMPT = """You are given the full text of a news article. Summarize it for a search engine.

Respond with JSON only, no markdown fences:
{{
  "headline": "the article's title or a one-line summary",
  "central_claim": "one sentence stating the article's main factual claim or conclusion",
  "topic": "5-10 word neutral description of the event/subject",
  "search_queries": ["3-5 short web search queries that would surface other coverage of this same story, including coverage that disputes it"]
}}

Article text:
{text}
"""

SEARCH_PROMPT = """Use web search to find other news articles about this story.

Topic: {topic}
Central claim of the source article: {claim}
Source article URL (do NOT return this one): {url}

Try these searches and any others you need: {queries}

Find articles from DIFFERENT publications than the source. Return a mix:
- some that corroborate/agree with the central claim
- some that contradict, dispute, or argue against it (look for rebuttals, corrections, opposing outlets, fact-checks, critics)

Respond with JSON only, no markdown fences:
{{
  "candidates": [
    {{"url": "full https URL of the article", "title": "...", "publisher": "...", "expected_stance": "supports" or "contradicts", "why": "one line"}}
  ]
}}

Return 8-12 candidates. URLs must be real article URLs you actually saw in search results, not guesses.
{extra}
"""

CONTRA_HINT = """
IMPORTANT: this round, look ONLY for coverage that pushes back on the claim. Search for rebuttals,
fact-checks, corrections, skeptics, critics, opposing-leaning outlets, and reporting with conflicting
numbers or timelines. Every candidate must have expected_stance "contradicts".
"""

SUPPORT_HINT = """
IMPORTANT: this round, look ONLY for coverage that independently confirms the claim, ideally from
outlets with their own reporting or primary sources. Every candidate must have expected_stance "supports".
"""

STANCE_PROMPT = """You are judging whether a candidate article supports or contradicts a source article's central claim.

Source central claim: {claim}
Source headline: {headline}

Respond with JSON only, no markdown fences:
{{
  "stance": "supports" | "contradicts" | "unrelated",
  "confidence": 0.0-1.0,
  "reason": "one sentence citing specific overlapping or conflicting facts",
  "headline": "candidate article's headline"
}}

"supports" = covers the same story and its facts/conclusions back up the central claim.
"contradicts" = covers the same story but disputes the claim, reports conflicting facts/numbers, or argues the opposite conclusion.
"unrelated" = different story, or same story with no bearing on the claim.

Candidate article text:
{text}
"""


_client = None


def client():
    # cached: a client that goes out of scope closes its connection pool mid-request
    global _client
    if _client is None:
        _client = genai.Client()  # reads GEMINI_API_KEY from env
    return _client


def parse_json(raw):
    """Pull a JSON object out of a model response that may be fenced."""
    if raw is None:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def extract_claim(text):
    response = client().models.generate_content(
        model=MODEL,
        contents=CLAIM_PROMPT.format(text=text),
    )
    data = parse_json(response.text)
    if not data:
        sys.exit("Error: could not parse the source article's claim")
    return data


def search_candidates(claim, url, want=None):
    extra = {"contradicts": CONTRA_HINT, "supports": SUPPORT_HINT}.get(want, "")
    response = client().models.generate_content(
        model=MODEL,
        contents=SEARCH_PROMPT.format(
            topic=claim.get("topic", ""),
            claim=claim.get("central_claim", ""),
            url=url,
            queries=", ".join(claim.get("search_queries", []) or []),
            extra=extra,
        ),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    data = parse_json(response.text) or {}
    return data.get("candidates", [])


def resolve(url):
    """Grounded search hands back vertexaisearch redirect links — follow them to the real article."""
    if "vertexaisearch" not in url and "grounding-api-redirect" not in url:
        return url
    # plain UA only: the redirect service rejects some browser header sets
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.geturl()
    except Exception:
        return None


def try_fetch(url):
    """Fetch and extract article text, or None if the page can't be read."""
    try:
        downloaded = trafilatura.fetch_url(url)
    except Exception:
        downloaded = None
    if downloaded is None:
        # some publishers block trafilatura's default agent; retry as a browser
        request = urllib.request.Request(url, headers=BROWSER_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                downloaded = response.read().decode("utf-8", "replace")
        except Exception:
            return None
    return trafilatura.extract(downloaded)


def classify(claim, text):
    response = client().models.generate_content(
        model=MODEL,
        contents=STANCE_PROMPT.format(
            claim=claim.get("central_claim", ""),
            headline=claim.get("headline", ""),
            text=text[:40000],
        ),
    )
    return parse_json(response.text)


def clean_headline(headline):
    if not headline or headline.strip().lower() in {"n/a", "none", "unknown", ""}:
        return None
    return headline.strip()


def domain(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def dedupe(candidates, source_url, seen_urls):
    """Drop duplicates, the source itself, and anything from the source's domain.

    seen_urls is carried across search rounds so a second pass never re-checks a page.
    """
    source_domain = domain(source_url)
    seen_domains, out = set(), []
    for c in candidates:
        url = (c.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        url = resolve(url)
        if not url or url in seen_urls:
            continue
        d = domain(url)
        if d == source_domain or d in seen_domains:
            continue
        seen_urls.add(url)
        seen_domains.add(d)
        out.append(dict(c, url=url))
    return out


def evaluate(claim, candidates, max_checks, need=("supports", "contradicts")):
    """Fetch and stance-classify candidates until every needed stance is found or the budget runs out."""
    supporting, opposing = [], []
    found = set()
    checked = 0
    for c in candidates:
        if checked >= max_checks or found >= set(need):
            break
        url = c["url"]
        print(f"  checking {url} ...", file=sys.stderr)
        text = try_fetch(url)
        if not text or len(text) < 400:
            print("    skipped (could not read page)", file=sys.stderr)
            continue
        checked += 1
        verdict = classify(claim, text)
        if not verdict:
            continue
        stance = verdict.get("stance")
        print(f"    {stance} ({verdict.get('confidence')})", file=sys.stderr)
        record = {
            "url": url,
            "publisher": c.get("publisher") or domain(url),
            "headline": clean_headline(verdict.get("headline")) or c.get("title") or url,
            "confidence": verdict.get("confidence") or 0,
            "reason": verdict.get("reason", ""),
            "text": text,
        }
        if stance == "supports":
            supporting.append(record)
            found.add("supports")
        elif stance == "contradicts":
            opposing.append(record)
            found.add("contradicts")

    best = lambda group: max(group, key=lambda r: r["confidence"]) if group else None
    return best(supporting), best(opposing)


def show(label, record):
    print(f"\n**{label}**")
    if not record:
        print("  none found")
        return
    print(f"  {record['headline']}")
    print(f"  {record['publisher']} — {record['url']}")
    print(f"  Why: {record['reason']} (confidence {record['confidence']})")


def main():
    parser = argparse.ArgumentParser(
        description="Find one corroborating and one contradicting article for a given article"
    )
    parser.add_argument("url", help="URL of the source article")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="also run the full comparison against each article found",
    )
    parser.add_argument(
        "--max-checks",
        type=int,
        default=8,
        help="maximum candidate articles to fetch and classify (default: 8)",
    )
    args = parser.parse_args()

    load_dotenv()

    print(f"Fetching {args.url} ...", file=sys.stderr)
    text = fetch_article(args.url)

    print("Extracting central claim ...", file=sys.stderr)
    claim = extract_claim(text)
    print(f"  Claim: {claim.get('central_claim')}", file=sys.stderr)

    print("Searching for related coverage ...", file=sys.stderr)
    seen = {args.url}
    raw = search_candidates(claim, args.url)
    print(f"  {len(raw)} raw search results", file=sys.stderr)
    candidates = dedupe(raw, args.url, seen)
    if not candidates:
        sys.exit("Error: search returned no usable candidate articles")
    print(f"  {len(candidates)} candidates found", file=sys.stderr)

    supporting, opposing = evaluate(claim, candidates, args.max_checks)

    # a side is missing: search again, this time hunting only for that side
    missing = "contradicts" if not opposing else ("supports" if not supporting else None)
    if missing:
        print(f"Second pass, searching specifically for {missing} ...", file=sys.stderr)
        more = dedupe(search_candidates(claim, args.url, want=missing), args.url, seen)
        extra_support, extra_oppose = evaluate(claim, more, args.max_checks, need=(missing,))
        supporting = supporting or extra_support
        opposing = opposing or extra_oppose

    print(f"\n**Source:** {claim.get('headline')}")
    print(f"**Central claim:** {claim.get('central_claim')}")
    show("Corroborates", supporting)
    show("Contradicts", opposing)

    if args.compare:
        for label, record in (("CORROBORATING", supporting), ("CONTRADICTING", opposing)):
            if not record:
                continue
            print(f"\n\n=== SOURCE vs {label} ARTICLE ===\n")
            print(compare(text, record["text"]))


if __name__ == "__main__":
    main()
