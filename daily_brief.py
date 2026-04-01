"""
ChainifyIT Daily Intelligence Brief
Runs on a cron schedule via Render. Pipeline:
  1. Fetch last-24h articles from RSS feeds (feedparser, no auth)
  2. Keyword-rank articles by title relevance to ChainifyIT's business
  3. Get top 3 X-trending stories via Grok Responses API + x_search
  4. Synthesize 3-story brief via Claude; append 5 notable links in Python
  5. Send formatted HTML email via Gmail SMTP
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
def _require_env(key):
    val = os.environ.get(key)
    if not val:
        raise SystemExit(
            f"ERROR: Required environment variable '{key}' is not set. "
            f"Add it in Render → Environment before deploying."
        )
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
BOUNDARY_KEYWORDS = {
    "SEC":   2,
    "CFTC":  2,
    "MiCA":  2,
    "RWA":   3,
    "NFT":   3,
    "EVM":   2,
    "Ondo":  2,
    "Maple": 2,
}

SUBSTRING_KEYWORDS = {
    "tokeniz":            3,
    "tokenis":            3,
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

_BOUNDARY_PATTERNS = {
    kw: re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
    for kw in BOUNDARY_KEYWORDS
}


def rank_articles(articles):
    """
    Score by keyword hits in title only. Returns a tuple:
      (ranked, unranked) — ranked sorted descending, unranked are zero-score
    articles kept as fallback for the Also Notable section on quiet news days.
    """
    scored = []
    unranked = []
    for a in articles:
        title = a["title"]
        title_lower = title.lower()
        score = 0
        for kw, weight in BOUNDARY_KEYWORDS.items():
            if _BOUNDARY_PATTERNS[kw].search(title):
                score += weight
        for kw, weight in SUBSTRING_KEYWORDS.items():
            if kw.lower() in title_lower:
                score += weight
        if score > 0:
            scored.append((score, a))
        else:
            unranked.append(a)

    scored.sort(key=lambda x: x[0], reverse=True)
    ranked = [a for _, a in scored]
    print(f"Ranked {len(ranked)} relevant articles from {len(articles)} total.")
    return ranked, unranked


# ── Markdown to HTML converter ────────────────────────────────────────────────
def markdown_to_html(text):
    """
    Convert Claude's markdown output to HTML for proper email rendering.
    Handles: ## headers, **bold**, and paragraph breaks.
    Keeps it simple — only the patterns Claude actually outputs.
    """
    # Escape HTML special chars first
    text = html_lib.escape(text)

    # Convert ## headers to styled divs
    text = re.sub(
        r'^## (.+)$',
        r'<div style="font-size:16px;font-weight:bold;color:#1A2B4A;'
        r'margin:24px 0 8px;padding-bottom:4px;border-bottom:1px solid #e0e0e0">\1</div>',
        text, flags=re.MULTILINE
    )

    # Convert **bold** to <strong>
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

    # Convert --- dividers to visual separators
    text = re.sub(
        r'^---$',
        r'<hr style="border:none;border-top:1px solid #eee;margin:20px 0">',
        text, flags=re.MULTILINE
    )

    # Convert double newlines to paragraph breaks
    # Single newlines within a paragraph preserved as <br>
    paragraphs = text.split('\n\n')
    html_parts = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Already-converted HTML tags pass through unchanged
        if para.startswith('<div') or para.startswith('<hr'):
            html_parts.append(para)
        else:
            para = para.replace('\n', '<br>')
            html_parts.append(
                f'<p style="margin:0 0 12px;line-height:1.7">{para}</p>'
            )

    return '\n'.join(html_parts)


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
    Returns top 3 most-discussed RWA/digital asset stories on X in last 24h.
    x_search only available on Responses API (/v1/responses).
    Polling handles both 'queued' and 'in_progress' statuses.
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
def generate_brief(ranked, unranked, x_stories):
    today = datetime.now().strftime("%A, %B %d, %Y")

    # Top 20 ranked go to Claude.
    # Also Notable: articles 21-25 from ranked list.
    # If fewer than 21 ranked articles exist, pad with unranked articles
    # so the section never appears empty on quiet news days.
    top_articles = ranked[:20]
    also_notable = ranked[20:25]
    if len(also_notable) < 5:
        shortfall = 5 - len(also_notable)
        also_notable = also_notable + unranked[:shortfall]

    article_block = "\n\n".join(
        f"SOURCE: {a['source']}\nTITLE: {a['title']}\n"
        f"SUMMARY: {a['summary']}\nURL: {a['link']}"
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
                "[One paragraph. Facts only.]\n\n"
                "**What It Means for ChainifyIT**\n"
                "[One paragraph. Strategic implications only.]\n\n"
                "---\n\n"
                "After the three stories:\n\n"
                "## Market Pulse\n"
                "[2 sentences. X focus today and whether it aligns with RSS coverage.]"
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

    # Convert Claude's markdown to HTML for proper rendering
    brief_html = markdown_to_html(brief)

    # Also Notable — clickable linked titles, built in Python
    if also_notable:
        notable_items = "".join(
            f'<li style="margin-bottom:8px">'
            f'<a href="{html_lib.escape(a["link"])}" '
            f'style="color:#1A2B4A;text-decoration:none;font-size:14px">'
            f'{html_lib.escape(a["title"])}</a>'
            f'<span style="color:#888;font-size:12px"> — {html_lib.escape(a["source"])}</span>'
            f'</li>'
            for a in also_notable
        )
        notable_block = (
            '<div style="margin-top:32px;padding-top:20px;border-top:2px solid #eee">'
            '<p style="font-weight:bold;color:#1A2B4A;margin:0 0 12px;'
            'font-size:13px;letter-spacing:1px;text-transform:uppercase">'
            'Also Notable</p>'
            f'<ul style="margin:0;padding-left:16px;list-style:none">'
            f'{notable_items}</ul></div>'
        )
    else:
        notable_block = ""

    html_body = (
        '<!DOCTYPE html><html><body style="font-family:Georgia,serif;'
        'max-width:680px;margin:40px auto;color:#1a1a1a;font-size:15px">'

        # Header
        '<div style="border-bottom:3px solid #1A2B4A;padding-bottom:12px;margin-bottom:28px">'
        '<div style="font-size:11px;color:#888;letter-spacing:2px;'
        'text-transform:uppercase;margin-bottom:4px">Interlink Capital Strategies</div>'
        '<div style="font-size:20px;font-weight:bold;color:#1A2B4A">'
        'ChainifyIT Intelligence Brief</div>'
        f'<div style="font-size:13px;color:#888;margin-top:2px">{today}</div>'
        '</div>'

        # Main brief content
        f'{brief_html}'

        # Also Notable
        f'{notable_block}'

        # Footer
        '<div style="border-top:1px solid #ddd;margin-top:40px;padding-top:12px;'
        'font-size:11px;color:#aaa;font-style:italic">'
        'AI-drafted. Requires analyst review before distribution. '
        'Interlink Capital Strategies.</div>'
        '</body></html>'
    )

    # Plain text fallback
    plain_notable = ""
    if also_notable:
        plain_notable = "\n\nALSO NOTABLE\n" + "\n".join(
            f"- {a['title']} ({a['source']})\n  {a['link']}"
            for a in also_notable
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ChainifyIT Daily Brief — {today}"
    msg["From"]    = f"Interlink Intelligence <{GMAIL_ADDRESS}>"
    msg["To"]      = RECIPIENT_EMAIL
    # Plain text first, HTML last — MIME standard; email clients prefer last part
    msg.attach(MIMEText(brief + plain_notable, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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

    ranked, unranked = rank_articles(articles)
    if not ranked:
        print("No relevant articles after ranking. Exiting.")
        return

    x_stories = fetch_x_stories()
    brief, also_notable = generate_brief(ranked, unranked, x_stories)
    send_email(brief, also_notable)
    print("\nDone.")

if __name__ == "__main__":
    main()
