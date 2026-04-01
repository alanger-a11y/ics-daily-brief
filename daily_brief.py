"""
ChainifyIT Daily Intelligence Brief
Runs on a cron schedule via Render. Pipeline:
  1. Fetch last-24h articles from RSS feeds (feedparser, no auth)
  2. Keyword-rank articles by title relevance to ChainifyIT's business
  3. Get top 3 X-trending stories via Grok Responses API + x_search
  4. Synthesize 3-story brief via Claude; append 5 notable links in Python
  5. Send formatted HTML email via Gmail SMTP

Patches applied:
  - Issue 1: env var errors caught at startup with clear message
  - Issue 4: word-boundary regex for short ambiguous keywords (SEC, NFT, EVM, etc.)
  - Issue 6: pipe-safe article block format with labeled fields
  - Issue 7: Grok polling handles both 'queued' and 'in_progress' statuses
"""

import os
import re
import time
import html as html_lib
import smtplib
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Environment variables ─────────────────────────────────────────────────────
# Validated at startup with clear error messages rather than cryptic KeyError.
def _require_env(key):
    val = os.environ.get(key)
    if not val:
        raise SystemExit(f"ERROR: Required environment variable '{key}' is not set. "
                         f"Add it in Render → Environment before deploying.")
    return val

XAI_API_KEY        = _require_env("XAI_API_KEY")
ANTHROPIC_API_KEY  = _require_env("ANTHROPIC_API_KEY")
GMAIL_ADDRESS      = _require_env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _require_env("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL    = _require_env("RECIPIENT_EMAIL")

# ── RSS feed list ─────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.theblock.co/rss.xml",
    "https://blockworks.co/feed",
    "https://www.dlnews.com/rss/",
    "https://www.sec.gov/rss/news/press.xml",
    "https://www.cftc.gov/rss/pressreleases.xml",
    "https://a16zcrypto.com/feed/",
    "https://www.bis.org/rss/bis_research.rss",
]

STRIP_HTML = re.compile(r"<[^>]+>")

# ── Keyword relevance scoring ─────────────────────────────────────────────────
# Derived from ChainifyIT litepaper. Scored on title only — fast, free, no API.
# Score 3 = core business, Score 2 = regulatory/institutional, Score 1 = general.
# Zero-score articles are dropped before Claude is called.
#
# PATCH (Issue 4): short uppercase/proper-noun keywords use word-boundary regex
# to prevent false matches (SEC in "second", Ondo in "London", NFT in nothing
# but guarded anyway, Maple in "maple syrup", EVM in "government").
# Multi-word and stem keywords use plain substring match — safe by construction.

# Keywords matched with word-boundary regex (avoids substring false positives)
BOUNDARY_KEYWORDS = {
    "SEC":      2,
    "CFTC":     2,
    "MiCA":     2,
    "RWA":      3,
    "NFT":      3,
    "EVM":      2,
    "Ondo":     2,
    "Maple":    2,
}

# Keywords matched with plain substring (stems, phrases, long proper nouns)
SUBSTRING_KEYWORDS = {
    "tokeniz":            3,   # tokenization, tokenized, tokenizing
    "tokenis":            3,   # British spelling
    "real-world asset":   3,
    "real world asset":   3,
    "smart contract":     3,
    "GENIUS Act":         3,
    "stablecoin":         2,
    "digital asset":      2,
    "licensing":          2,
    "regulation":         2,
    "compliance":         2,
    "BlackRock":          2,
    "Franklin Templeton": 2,
    "JPMorgan":           2,
    "Fidelity":           2,
    "asset manager":      2,
    "institutional":      2,
    "neobank":            2,
    "Securitize":         2,
    "OpenEden":           2,
    "Goldfinch":          2,
    "Polygon":            2,
    "blockchain":         1,
    "on-chain":           1,
    "onchain":            1,
    "custody":            1,
    "settlement":         1,
    "fintech":            1,
}

# Pre-compile boundary patterns once at module load
_BOUNDARY_PATTERNS = {
    kw: re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
    for kw in BOUNDARY_KEYWORDS
}


def rank_articles(articles):
    """
    Score each article by keyword hits in title only.
    Boundary keywords use regex word-boundary matching.
    Substring keywords use plain case-insensitive contains.
    Drop zeros. Sort descending. Return all ranked articles.
    """
    scored = []
    for a in articles:
        title = a["title"]
        title_lower = title.lower()

        score = 0
        # Boundary-matched keywords
        for kw, weight in BOUNDARY_KEYWORDS.items():
            if _BOUNDARY_PATTERNS[kw].search(title):
                score += weight
        # Substring-matched keywords
        for kw, weight in SUBSTRING_KEYWORDS.items():
            if kw.lower() in title_lower:
                score += weight

        if score > 0:
            scored.append((score, a))

    scored.sort(key=lambda x: x[0], reverse=True)
    ranked = [a for _, a in scored]
    print(f"Ranked {len(ranked)} relevant articles from {len(articles)} total.")
    return ranked


# ── Step 1: RSS ───────────────────────────────────────────────────────────────
def fetch_articles():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    articles = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries:
                pp = entry.get("published_parsed")
                pub = datetime(*pp[:6], tzinfo=timezone.utc) if pp else None
                if pub and pub < cutoff:
                    continue
                summary = STRIP_HTML.sub(
                    "", entry.get("summary", entry.get("description", ""))
                )[:150]
                articles.append({
                    "source":  source,
                    "title":   entry.get("title", ""),
                    "summary": summary,
                    "link":    entry.get("link", ""),
                })
        except Exception as e:
            print(f"Feed error ({url}): {e}")

    print(f"Fetched {len(articles)} articles.")
    return articles


# ── Step 2: Grok x_search — top 3 trending stories ───────────────────────────
def fetch_x_stories():
    """
    Returns top 3 most-discussed RWA/digital asset stories on X in the last 24h.
    Claude uses this to weight RSS story selection.
    x_search only available on Responses API (/v1/responses).

    PATCH (Issue 7): polling loop now handles both 'queued' and 'in_progress'
    statuses, preventing premature fallback on queued responses.
    """
    payload = {
        "model": "grok-4-1-fast",
        "max_output_tokens": 300,
        "input": [{
            "role": "user",
            "content": (
                "Search X posts from the last 24 hours. "
                "Identify the TOP 3 most-discussed news stories in this niche: "
                "RWA tokenization, tokenized securities, digital asset regulation, "
                "stablecoin legislation, institutional blockchain adoption. "
                "For each: one headline, one sentence on why it is trending. "
                "Numbered list only. No other commentary."
            ),
        }],
        "tools": [{"type": "x_search"}],
    }

    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    def extract_text(output):
        for block in output:
            if block.get("type") == "message":
                for part in block.get("content", []):
                    if part.get("type") == "output_text":
                        return part["text"]
        return None

    try:
        r = requests.post(
            "https://api.x.ai/v1/responses",
            headers=headers,
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        # Poll on both 'queued' and 'in_progress' — the API may queue before processing
        poll_attempts = 0
        while data.get("status") in ("queued", "in_progress") and poll_attempts < 10:
            poll_attempts += 1
            response_id = data.get("id")
            if not response_id:
                break
            time.sleep(3)
            poll = requests.get(
                f"https://api.x.ai/v1/responses/{response_id}",
                headers=headers,
                timeout=30,
            )
            poll.raise_for_status()
            data = poll.json()

        text = extract_text(data.get("output", []))
        if text:
            print("Grok X stories fetched.")
            return text

        print("Grok: no output_text found.")
        return "X trending stories unavailable."

    except Exception as e:
        print(f"Grok error: {e}")
        return "X trending stories unavailable."


# ── Step 3: Claude synthesis ──────────────────────────────────────────────────
def generate_brief(articles, x_stories):
    today = datetime.now().strftime("%A, %B %d, %Y")

    # Top 20 go to Claude. Articles 21-25 become the "Also Notable" section,
    # built in Python — no Claude tokens spent on simple list formatting.
    top_articles = articles[:20]
    also_notable = articles[20:25]

    # PATCH (Issue 6): labeled field format replaces pipe-delimited format.
    # Pipe characters in financial headlines (e.g. "BlackRock | ETF Update")
    # would break the previous format. Labeled fields are unambiguous.
    article_block = "\n\n".join(
        f"SOURCE: {a['source']}\nTITLE: {a['title']}\nSUMMARY: {a['summary']}\nURL: {a['link']}"
        for a in top_articles
    )

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1200,
        "system": (
            "You are a senior research analyst at Interlink Capital Strategies. "
            "Write daily intelligence briefs for ChainifyIT, an RWA tokenization "
            "and digital asset infrastructure client. "
            "Graduate-level, precise, direct. No filler, no hedging, no AI writing patterns. "
            "Never begin a sentence with This, The, or a gerund."
        ),
        "messages": [{
            "role": "user",
            "content": (
                f"Date: {today}\n\n"
                f"TOP STORIES ON X TODAY:\n{x_stories}\n\n"
                f"RSS ARTICLES:\n{article_block}\n\n"
                "---\n"
                "SELECTION RULES — apply in order:\n"
                "1. Stories in BOTH the X list and RSS rank highest.\n"
                "2. RSS-only priority: regulatory > institutional adoption > "
                "competing platforms > market structure.\n"
                "3. If an X story has no RSS match, note it briefly as a gap.\n\n"
                "Select THREE stories. For each use EXACTLY this structure:\n\n"
                "---\n"
                "## [N]. [Headline in title case]\n"
                "**Source:** [Publication] | [Date]\n"
                "**Link:** [URL]\n\n"
                "**What Happened**\n"
                "[One paragraph. Facts only — what occurred, who was involved, "
                "key figures or deadlines. No analysis.]\n\n"
                "**What It Means for ChainifyIT**\n"
                "[One paragraph. Strategic implications only — competitive impact, "
                "client opportunities, risks, required actions. No news summary.]\n\n"
                "---\n\n"
                "After the three stories:\n\n"
                "## Market Pulse\n"
                "[2 sentences. What X is most focused on today and whether it "
                "aligns with or diverges from RSS coverage.]"
            ),
        }],
    }

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    brief = r.json()["content"][0]["text"]
    print("Brief generated.")
    return brief, also_notable


# ── Step 4: Gmail SMTP email ──────────────────────────────────────────────────
def send_email(brief, also_notable):
    today = datetime.now().strftime("%B %d, %Y")

    # "Also Notable" — clickable linked titles, built in Python (no Claude tokens).
    if also_notable:
        notable_items = "".join(
            f'<li style="margin-bottom:6px">'
            f'<a href="{html_lib.escape(a["link"])}" '
            f'style="color:#1A2B4A;text-decoration:none">'
            f'{html_lib.escape(a["title"])}</a>'
            f' <span style="color:#888;font-size:12px">'
            f'— {html_lib.escape(a["source"])}</span>'
            f'</li>'
            for a in also_notable
        )
        notable_block = (
            '<div style="margin-top:32px;padding-top:16px;border-top:1px solid #ddd">'
            '<p style="font-weight:bold;color:#1A2B4A;margin:0 0 10px;font-size:14px">'
            'ALSO NOTABLE</p>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8">'
            f'{notable_items}</ul></div>'
        )
    else:
        notable_block = ""

    brief_escaped = html_lib.escape(brief)

    html_body = (
        '<html><body style="font-family:Georgia,serif;max-width:680px;margin:40px auto;'
        'color:#1a1a1a;line-height:1.7;font-size:15px">'
        '<div style="border-bottom:2px solid #1A2B4A;padding-bottom:10px;margin-bottom:24px">'
        '<h2 style="color:#1A2B4A;margin:0;font-size:17px;letter-spacing:0.5px">'
        'INTERLINK CAPITAL STRATEGIES</h2>'
        '<p style="margin:4px 0 0;color:#888;font-size:12px;font-style:italic">'
        f'ChainifyIT Daily Intelligence Brief &mdash; {today}</p></div>'
        f'<div style="white-space:pre-wrap">{brief_escaped}</div>'
        f'{notable_block}'
        '<div style="border-top:1px solid #ddd;margin-top:36px;padding-top:10px;'
        'font-size:11px;color:#999;font-style:italic">'
        'AI-drafted. Requires analyst review before distribution.</div>'
        '</body></html>'
    )

    plain_notable = ""
    if also_notable:
        plain_notable = "\n\nALSO NOTABLE\n" + "\n".join(
            f"- {a['title']} ({a['source']}) {a['link']}"
            for a in also_notable
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ChainifyIT Daily Brief — {today}"
    msg["From"]    = f"Interlink Intelligence <{GMAIL_ADDRESS}>"
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(brief + plain_notable, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())

    print(f"Email sent to {RECIPIENT_EMAIL}.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n=== ChainifyIT Brief — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    articles = fetch_articles()
    if not articles:
        print("No articles. Exiting.")
        return

    articles = rank_articles(articles)
    if not articles:
        print("No relevant articles after ranking. Exiting.")
        return

    x_stories = fetch_x_stories()
    brief, also_notable = generate_brief(articles, x_stories)
    send_email(brief, also_notable)
    print("\nDone.")

if __name__ == "__main__":
    main()

