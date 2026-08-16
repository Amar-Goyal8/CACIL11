"""Fact-check an article: pull out its checkable claims and verify each against outside sources.

Usage:
    python factcheck.py <article_url>
    python factcheck.py <article_url> --max-claims 8 --sources-per-claim 5
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from google.genai import types

from find_counterparts import MODEL, client, domain, parse_json, resolve, try_fetch
from scraper import fetch_article

CLAIMS_PROMPT = """You are given the full text of a news article. Extract its CHECKABLE FACTUAL CLAIMS —
statements that outside evidence could confirm or refute.

Good claims: specific numbers and statistics, dates, named events that did or did not happen, attributed
quotes, who did what, official actions, causal assertions stated as fact.
Skip: opinions, predictions about the future, vague framing, the writer's analysis, anything unfalsifiable.

Order them by how much the article's credibility rests on them (most load-bearing first).

Respond with JSON only, no markdown fences:
{{
  "topic": "5-10 word neutral description of the story",
  "claims": [
    {{
      "claim": "one self-contained sentence stating the claim, with enough context to check it standalone",
      "quote": "the exact sentence from the article making this claim",
      "type": "statistic" | "event" | "quote" | "attribution" | "causal",
      "search_queries": ["2-3 web searches that would surface evidence for or against this claim"]
    }}
  ]
}}

Return up to {max_claims} claims.

Article text:
{text}
"""

EVIDENCE_PROMPT = """Use web search to find evidence that CONFIRMS or REFUTES this specific factual claim.

Claim: {claim}
Story context: {topic}
Do not return this URL (it is the article being checked): {url}

Suggested searches: {queries}

Prioritise, in order:
1. Primary sources (government agencies, official statistics, court filings, company statements, the study itself)
2. Established news organisations with their own reporting
3. Fact-checking organisations

Actively look for evidence the claim is WRONG: corrections, retractions, fact-checks, updated figures,
other outlets reporting different numbers.

Respond with JSON only, no markdown fences:
{{
  "candidates": [
    {{"url": "full https URL", "title": "...", "publisher": "...", "kind": "primary" | "news" | "factcheck"}}
  ]
}}

Return 4-8 candidates. URLs must be real ones you saw in search results, not guesses.
"""

VERDICT_PROMPT = """Judge whether the source document confirms or refutes a specific claim.

Claim being checked: {claim}

Respond with JSON only, no markdown fences:
{{
  "verdict": "confirms" | "refutes" | "partly" | "silent",
  "confidence": 0.0-1.0,
  "evidence_quote": "the exact sentence(s) from the source document that decide this, or empty if silent",
  "note": "one sentence — for 'refutes' or 'partly', state precisely what differs (the source's number vs the claim's number, etc.)"
}}

"confirms" = the document independently states the same fact.
"refutes" = the document states a fact that CANNOT both be true alongside the claim — a different number for
the same measure, an explicit denial, the event happening differently or not at all, a different person or date
for the same specific occurrence.
"partly" = the core is right but a detail is off, overstated, or missing context.
"silent" = the document does not address the claim.

Bar for "refutes" is high. These are NOT refutations — use "confirms", "partly", or "silent" instead:
- the document simply does not mention part of the claim
- the document covers a different date, period, or scope than the claim
- the document adds context, caveats, or extra detail the claim omits
- the numbers differ but measure different things (annual vs monthly, core vs headline, revised vs initial)
- you are inferring a contradiction rather than reading one stated in the text

Never guess from the topic alone. If the document does not actually contain the fact, answer "silent".

Source document ({publisher}):
{text}
"""

SUMMARY_PROMPT = """You checked the factual claims in an article against outside sources. Write the verdict.

Article: {headline}

Per-claim results:
{results}

Respond in this format:

**Overall:** (2-3 sentences — is this article factually reliable? Are the errors minor, or do they undermine it?)
**False or disputed claims:** (bullet each claim rated FALSE or DISPUTED, with what the evidence actually says; write "none" if there are none)
**Unverified claims:** (bullet each claim no source could confirm — note that unverified means unchecked, not false; write "none" if there are none)
**Confidence in this check:** (1-2 sentences on coverage: how many claims got real evidence, what the weak spots are)
"""

ADJUDICATE_PROMPT = """Sources disagree about one factual claim. Decide which reading is right.

Claim: {claim}

Evidence gathered:
{evidence}

A source only refutes the claim if its evidence quote states something that cannot be true at the same time
as the claim. A source that covers a different period, omits a detail, or adds caveats does NOT refute it.

Respond with JSON only, no markdown fences:
{{
  "rating": "SUPPORTED" | "PARTLY SUPPORTED" | "DISPUTED" | "FALSE" | "UNVERIFIED",
  "reason": "one or two sentences explaining the call, citing the decisive evidence"
}}

Weigh the sources rather than counting them:
- a primary source (agency, official statistic, the document itself) outweighs any number of secondary reports
- a source quoting a specific figure outweighs one asserting a fact with no quoted evidence
- if the strongest evidence refutes the claim, say "FALSE" even when weaker sources agree with it

"DISPUTED" = credible sources of comparable weight genuinely conflict, and the evidence does not settle it.
"FALSE" = the strongest evidence shows the claim is wrong.
"UNVERIFIED" = only for when no evidence quote actually addresses the claim. Do not fall back to it merely
because the sources disagree — that is what DISPUTED and FALSE are for.
"""

RATING = {
    "confirms": "SUPPORTED",
    "refutes": "DISPUTED",
    "partly": "PARTLY SUPPORTED",
}


def extract_claims(text, max_claims):
    response = client().models.generate_content(
        model=MODEL,
        contents=CLAIMS_PROMPT.format(text=text, max_claims=max_claims),
    )
    data = parse_json(response.text)
    if not data or not data.get("claims"):
        sys.exit("Error: could not extract checkable claims from this article")
    return data


def find_evidence(claim, topic, url):
    response = client().models.generate_content(
        model=MODEL,
        contents=EVIDENCE_PROMPT.format(
            claim=claim["claim"],
            topic=topic,
            url=url,
            queries=", ".join(claim.get("search_queries", []) or []),
        ),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    data = parse_json(response.text) or {}
    return data.get("candidates", [])


def judge(claim_text, source_text, publisher):
    response = client().models.generate_content(
        model=MODEL,
        contents=VERDICT_PROMPT.format(
            claim=claim_text,
            publisher=publisher,
            text=source_text[:40000],
        ),
    )
    return parse_json(response.text)


def usable_sources(candidates, source_url, limit):
    """Resolve redirect URLs, drop the article under test and repeat publishers."""
    source_domain = domain(source_url)
    seen, out = set(), []
    for c in candidates:
        url = (c.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        url = resolve(url)
        if not url:
            continue
        d = domain(url)
        if d == source_domain or d in seen:
            continue
        seen.add(d)
        out.append(dict(c, url=url))
        if len(out) >= limit:
            break
    return out


def rate(claim_text, verdicts):
    """Turn per-source verdicts into one rating for the claim."""
    refutes = [v for v in verdicts if v["verdict"] == "refutes"]
    confirms = [v for v in verdicts if v["verdict"] == "confirms"]
    partly = [v for v in verdicts if v["verdict"] == "partly"]

    strong = lambda group: [v for v in group if (v.get("confidence") or 0) >= 0.6]
    if strong(refutes):
        # sources conflict: re-read the evidence quotes before calling the article wrong
        return adjudicate(claim_text, verdicts) if strong(confirms) or strong(partly) else "FALSE"
    if strong(confirms):
        return "PARTLY SUPPORTED" if strong(partly) else "SUPPORTED"
    if strong(partly):
        return "PARTLY SUPPORTED"
    return "UNVERIFIED"


def adjudicate(claim_text, verdicts):
    """Second look when sources conflict — one bad 'refutes' should not brand a claim false."""
    evidence = "\n".join(
        "- {publisher} ({kind}) says it {verdict}: \"{quote}\" — {note}".format(
            publisher=v["publisher"],
            kind=v.get("kind", "news"),
            verdict=v["verdict"],
            quote=v.get("evidence_quote", ""),
            note=v.get("note", ""),
        )
        for v in verdicts
    )
    response = client().models.generate_content(
        model=MODEL,
        contents=ADJUDICATE_PROMPT.format(claim=claim_text, evidence=evidence),
    )
    data = parse_json(response.text) or {}
    rating = data.get("rating")
    if data.get("reason"):
        # keep the reasoning visible in the per-claim output
        verdicts.append(
            {
                "verdict": "adjudicated",
                "confidence": 1.0,
                "publisher": "conflict review",
                "kind": "review",
                "url": "",
                "evidence_quote": "",
                "note": data["reason"],
            }
        )
    return rating if rating in {"SUPPORTED", "PARTLY SUPPORTED", "DISPUTED", "FALSE", "UNVERIFIED"} else "DISPUTED"


def check_claim(claim, topic, source_url, sources_per_claim, log):
    """Search for evidence on one claim, judge each source, and rate the claim."""
    candidates = usable_sources(find_evidence(claim, topic, source_url), source_url, sources_per_claim)
    verdicts = []
    for c in candidates:
        text = try_fetch(c["url"])
        if not text or len(text) < 300:
            log.append(f"    unreadable: {c['url']}")
            continue
        verdict = judge(claim["claim"], text, c.get("publisher") or domain(c["url"]))
        if not verdict:
            continue
        verdict.update(
            url=c["url"],
            publisher=c.get("publisher") or domain(c["url"]),
            kind=c.get("kind", "news"),
        )
        log.append(f"    {verdict['verdict']} ({verdict.get('confidence')}) — {verdict['publisher']}")
        if verdict["verdict"] != "silent":
            verdicts.append(verdict)
    return {"claim": claim, "verdicts": verdicts, "rating": rate(claim["claim"], verdicts)}


def check_all(claims, topic, source_url, sources_per_claim, workers):
    """Check claims concurrently; each claim's log is buffered so output stays readable."""
    logs = [[f"  [{i + 1}] {c['claim']}"] for i, c in enumerate(claims)]

    def run(pair):
        i, claim = pair
        try:
            return check_claim(claim, topic, source_url, sources_per_claim, logs[i])
        except Exception as e:
            logs[i].append(f"    error: {e}")
            return {"claim": claim, "verdicts": [], "rating": "UNVERIFIED"}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, enumerate(claims)))

    for lines in logs:
        print("\n".join(lines), file=sys.stderr)
    return results


def format_results(results):
    out = []
    for i, r in enumerate(results, 1):
        out.append(f"[{i}] {r['rating']}: {r['claim']['claim']}")
        out.append(f"    Article said: \"{r['claim'].get('quote', '')}\"")
        if not r["verdicts"]:
            out.append("    No source addressed this claim.")
        for v in r["verdicts"]:
            out.append(
                f"    {v['verdict']} ({v.get('confidence')}, {v['kind']}) {v['publisher']} {v['url']}"
            )
            if v.get("evidence_quote"):
                out.append(f"      evidence: \"{v['evidence_quote']}\"")
            if v.get("note"):
                out.append(f"      note: {v['note']}")
    return "\n".join(out)


def summarize(headline, results):
    response = client().models.generate_content(
        model=MODEL,
        contents=SUMMARY_PROMPT.format(headline=headline, results=format_results(results)),
    )
    return response.text


def main():
    parser = argparse.ArgumentParser(description="Fact-check an article's claims against outside sources")
    parser.add_argument("url", help="URL of the article to check")
    parser.add_argument("--max-claims", type=int, default=6, help="claims to check (default: 6)")
    parser.add_argument(
        "--sources-per-claim", type=int, default=4, help="sources to check per claim (default: 4)"
    )
    parser.add_argument("--workers", type=int, default=4, help="claims to check in parallel (default: 4)")
    parser.add_argument("--detail", action="store_true", help="print every source verdict, not just the summary")
    args = parser.parse_args()

    load_dotenv()

    print(f"Fetching {args.url} ...", file=sys.stderr)
    text = fetch_article(args.url)

    print("Extracting checkable claims ...", file=sys.stderr)
    extracted = extract_claims(text, args.max_claims)
    claims = extracted["claims"][: args.max_claims]
    topic = extracted.get("topic", "")
    print(f"  {len(claims)} claims to check", file=sys.stderr)

    print("Gathering and judging evidence ...", file=sys.stderr)
    results = check_all(claims, topic, args.url, args.sources_per_claim, args.workers)

    print("\n**Article checked:** " + args.url)
    print("**Topic:** " + topic + "\n")

    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['rating']} — {r['claim']['claim']}")
        print(f"    Article: \"{r['claim'].get('quote', '')}\"")
        if not r["verdicts"]:
            print("    No outside source addressed this claim.")
        for v in r["verdicts"]:
            print(f"    - {v['verdict'].upper()} ({v['publisher']}, {v['kind']}): {v.get('note', '')}")
            if args.detail and v.get("evidence_quote"):
                print(f"        \"{v['evidence_quote']}\"")
            if v.get("url"):
                print(f"        {v['url']}")
        print()

    print(summarize(topic, results))


if __name__ == "__main__":
    main()
