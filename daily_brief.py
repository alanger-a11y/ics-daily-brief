"""
ChainifyIT Daily Intelligence Brief
Runs on a cron schedule via Render. Pipeline:
  1. Fetch last-24h articles from RSS feeds (feedparser, no auth)
  2. Get X sentiment via Grok Responses API + x_search tool
  3. Synthesize top 3 stories via Claude API
  4. Send formatted email via SendGrid
"""

import os
import re
import time
import html as html_lib
import feedparser
import requests
from datetime import datetime, timezone, timedelta

# ── Environment variables (set in Render dashboard) ──────────────────────────
XAI_API_KEY       = os.environ["XAI_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY  = os.environ["SENDGRID_API_KEY"]
RECIPIENT_EMAIL   = os.environ["RECIPIENT_EMAIL"]
SENDER_EMAIL      = os.environ["SENDER_EMAIL"]

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
                summary = STRIP_HTML.sub("", entry.get("summary", entry.get("description", "")))[:300]
                articles.append({
                    "source":    source,
                    "title":     entry.get("title", ""),
                    "summary":   summary,
                    "link":      entry.get("link", ""),
                    "published": pub.strftime("%Y-%m-%d %H:%M UTC") if pub else "Unknown",
                })
        except Exception as e:
            print(f"Feed error ({url}): {e}")

    print(f"Fetched {len(articles)} articles.")
    return articles


# ── Step 2: Grok x_search (Responses API) ────────────────────────────────────
def fetch_x_sentiment():
    """
    x_search is only available on the Responses API (/v1/responses).
    The chat completions live-search endpoint is deprecated.
    The Responses API with server-side tools can return async (status: in_progress)
    and requires polling until status is 'completed'.
    """
    payload = {
        "model": "grok-4-1-fast",          # correct alias; non-reasoning via no reasoning_effort
        "max_output_tokens": 600,
        "input": [{
            "role": "user",
            "content": (
                "Search X posts from the last 24 hours. "
                "Return a concise briefing on sentiment and notable discussions about: "
                "RWA tokenization, tokenized securities, digital asset regulation, "
                "stablecoin legislation, institutional blockchain adoption. "
                "For each topic: sentiment (bullish/bearish/neutral/mixed), "
                "key themes, any breaking developments. Signal over noise."
            ),
        }],
        "tools": [{"type": "x_search"}],
    }

    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}

    def extract_text(output):
        """Pull output_text from a completed Responses API output block."""
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

        # Poll if the response is still in progress (async tool execution)
        poll_attempts = 0
        while data.get("status") == "in_progress" and poll_attempts < 10:
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
            print("Grok sentiment fetched.")
            return text

        print("Grok: no output_text found in response.")
        return "X sentiment unavailable."

    except Exception as e:
        print(f"Grok error: {e}")
        return "X sentiment unavailable."


# ── Step 3: Claude synthesis ──────────────────────────────────────────────────
def generate_brief(articles, sentiment):
    today = datetime.now().strftime("%A, %B %d, %Y")

    # Cap at 40 articles to keep input tokens manageable (~8k tokens max)
    article_block = "\n".join(
        f"{i}. [{a['source']}] {a['title']} ({a['published']})\n   {a['summary']}\n   {a['link']}"
        for i, a in enumerate(articles[:40], 1)
    )

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1800,
        "system": (
            "You are a senior research analyst at Interlink Capital Strategies. "
            "You write daily intelligence briefs for ChainifyIT, an RWA tokenization "
            "and digital asset infrastructure client. "
            "Style: graduate-level, precise, direct. No filler, no hedging, no AI patterns."
        ),
        "messages": [{
            "role": "user",
            "content": f"""Date: {today}

X SENTIMENT:
{sentiment}

RSS ARTICLES:
{article_block}

---
Identify the THREE articles most relevant to ChainifyIT (RWA tokenization infrastructure, digital asset services).
Prioritise: regulatory developments, institutional adoption, competing platforms, tokenization infrastructure, stablecoin legislation, market structure.

For each story write:
- Headline
- Source and date
- Summary paragraph (what happened)
- Analysis paragraph (implications for ChainifyIT and the RWA space)
- Link

Close with 2–3 sentences on overall X sentiment tone.

Prose with section headers. No bullets. Professional memo style."""
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
    return brief


# ── Step 4: SendGrid email ────────────────────────────────────────────────────
def send_email(brief):
    today = datetime.now().strftime("%B %d, %Y")
    # Escape before injecting into HTML — brief may contain &, <, > in URLs or analysis
    brief_escaped = html_lib.escape(brief)
    html = f"""<html><body style="font-family:Georgia,serif;max-width:680px;margin:40px auto;
color:#1a1a1a;line-height:1.7;font-size:15px">
<div style="border-bottom:2px solid #1A2B4A;padding-bottom:10px;margin-bottom:24px">
<h2 style="color:#1A2B4A;margin:0;font-size:17px">INTERLINK CAPITAL STRATEGIES</h2>
<p style="margin:4px 0 0;color:#888;font-size:12px;font-style:italic">
ChainifyIT Daily Intelligence Brief &mdash; {today}</p></div>
<div style="white-space:pre-wrap">{brief_escaped}</div>
<div style="border-top:1px solid #ddd;margin-top:36px;padding-top:10px;
font-size:11px;color:#999;font-style:italic">
AI-drafted. Requires analyst review before distribution.</div>
</body></html>"""

    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        json={
            "personalizations": [{"to": [{"email": RECIPIENT_EMAIL}]}],
            "from": {"email": SENDER_EMAIL, "name": "Interlink Intelligence"},
            "subject": f"ChainifyIT Daily Brief — {today}",
            "content": [
                {"type": "text/plain", "value": brief},
                {"type": "text/html",  "value": html},
            ],
        },
        timeout=30,
    )
    r.raise_for_status()
    print(f"Email sent to {RECIPIENT_EMAIL}.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n=== ChainifyIT Brief — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")
    articles = fetch_articles()
    if not articles:
        print("No articles. Exiting.")
        return
    sentiment = fetch_x_sentiment()
    brief = generate_brief(articles, sentiment)
    send_email(brief)
    print("\nDone.")

if __name__ == "__main__":
    main()
