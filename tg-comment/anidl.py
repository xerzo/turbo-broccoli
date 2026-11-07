import json
import logging
import os
import requests
import feedparser
import dbm
import re
from dataclasses import dataclass, field
from time import sleep
from html import escape, unescape
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
from typing import List, Optional
from dateutil import parser as date_parser

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("tg-comment")
logger.setLevel(logging.INFO)

# File handler — rotates at 5 MB, keeps 3 backups
fh = RotatingFileHandler(
    os.path.join(LOG_DIR, "tg-comment.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
# Console handler — so docker logs still works
ch = logging.StreamHandler()

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
fh.setFormatter(fmt)
ch.setFormatter(fmt)
logger.addHandler(fh)
logger.addHandler(ch)

DEFAULT_MESSAGE_PREFIX = "New post:"
# Delay between each Telegram message to avoid rate limits
MESSAGE_DELAY = 3
# Max retries on rate limit before giving up for this cycle
MAX_RETRIES = 3


@dataclass
class FeedConfig:
    """Configuration for a single feed."""
    feed_url: str
    chat_id: int
    feed_type: str = "rss"              # "rss" or "api"
    base_url: Optional[str] = None      # e.g. "https://anidl.org" — prepended to relative hrefs
    message_prefix: str = DEFAULT_MESSAGE_PREFIX


@dataclass
class Config:
    """Configuration for tg-feed-bot.
    Supports single feed (legacy) or multiple feeds.
    """
    db_path: str
    telegram_token: str
    feeds: List[FeedConfig] = field(default_factory=list)

    @staticmethod
    def from_file(filename):
        with open(filename) as f:
            d = json.load(f)

        # Legacy single-feed format (always RSS)
        if 'feed_url' in d:
            feeds = [FeedConfig(
                feed_url=d['feed_url'],
                chat_id=d['chat_id'],
                feed_type="rss",
                base_url=d.get('base_url'),
                message_prefix=d.get('message_prefix') or DEFAULT_MESSAGE_PREFIX,
            )]
        # Multi-feed format
        else:
            feeds = [
                FeedConfig(
                    feed_url=fd['feed_url'],
                    chat_id=fd.get('chat_id', d.get('chat_id')),
                    feed_type=fd.get('feed_type', 'rss'),
                    base_url=fd.get('base_url'),
                    message_prefix=fd.get('message_prefix') or DEFAULT_MESSAGE_PREFIX,
                )
                for fd in d['feeds']
            ]

        return Config(
            db_path=d.get('db_path', 'tg-feed-bot.db'),
            telegram_token=d['telegram_token'],
            feeds=feeds,
        )


# ── Unified entry format ────────────────────────────────────────

def format_datetime(date_str: Optional[str]) -> Optional[str]:
    """Parse and format ISO or RFC date strings to a readable format."""
    if not date_str:
        return None
    try:
        dt = date_parser.parse(date_str)
        return dt.strftime("%b %d, %Y • %H:%M UTC")
    except Exception:
        return str(date_str)


@dataclass
class Entry:
    """Normalized entry — works for both RSS and API items."""
    id: str           # unique key for dedup (url or item id)
    title: str        # page/post title
    author: str       # who wrote the comment
    body: str         # comment text (plain text or HTML)
    link: str         # full URL to the comment/page
    is_reply: bool    # whether it's a reply to someone
    reply_to: Optional[str] = None  # who they replied to
    created_at: Optional[str] = None  # formatted creation date/time


def parse_rss_entries(feed_url, base_url=None):
    """Parse RSS/Atom feed into normalized entries."""
    d = feedparser.parse(feed_url)
    entries = []
    for e in d.entries[::-1]:
        link_href = _first(lnk['href'] for lnk in e.links if lnk.type == "text/html")
        if not link_href:
            continue

        # Resolve relative link
        if base_url and link_href.startswith("/"):
            link_href = base_url.rstrip("/") + link_href

        # Clean HTML from description
        description = unescape(e.description)
        soup = BeautifulSoup(description, 'html.parser')
        body = soup.get_text(separator='\n', strip=True)
        body = re.sub(r'\n+', '\n', body)

        author = re.sub(r"@\w+", "", e.author).strip() if hasattr(e, 'author') else "Unknown"
        title = e.title
        # Strip "Re: " prefix if present
        if title.startswith("Re: "):
            title = title[4:]

        published = e.get("published") or e.get("updated") or e.get("created")
        created_at = format_datetime(published) if published else None

        entries.append(Entry(
            id=link_href,
            title=title,
            author=author,
            body=body,
            link=link_href,
            is_reply=e.title.startswith("Re: "),
            created_at=created_at,
        ))
    return entries


def parse_api_entries(feed_url, base_url=None):
    """Parse JSON API feed into normalized entries.
    Expected format: { "data": { "items": [ ... ] } }
    """
    res = requests.get(feed_url, timeout=30)
    res.raise_for_status()
    data = res.json()

    items = data.get("data", {}).get("items", [])
    entries = []
    for item in reversed(items):
        item_id = item.get("id", "")
        author_info = item.get("author", {})
        author = author_info.get("name", "Unknown")
        target = item.get("target", {})
        href = target.get("href", "")
        title = target.get("label", "")
        body = item.get("body", "")
        parent = item.get("parent")

        # Build full link from relative href
        if base_url and href.startswith("/"):
            link = base_url.rstrip("/") + href
        else:
            link = href

        # Use item id + href as dedup key (href alone would dedup all comments on same page)
        dedup_key = f"{link}#{item_id}"

        raw_date = item.get("createdAt") or item.get("created_at") or item.get("date")
        created_at = format_datetime(raw_date) if raw_date else None

        entries.append(Entry(
            id=dedup_key,
            title=title,
            author=author,
            body=body,
            link=link,
            is_reply=parent is not None,
            reply_to=parent.get("authorName") if parent else None,
            created_at=created_at,
        ))
    return entries


# ── Bot ──────────────────────────────────────────────────────────

class FeedBot:
    """Telegram Bot to notify a telegram channel for every new entry in RSS/Atom or API feeds."""

    def __init__(self, config: Config):
        self.config = config
        self.db = dbm.open(config.db_path, 'c')

    def run(self):
        for feed in self.config.feeds:
            logger.info("Checking feed [%s]: %s", feed.feed_type, feed.feed_url)
            try:
                if feed.feed_type == "api":
                    entries = parse_api_entries(feed.feed_url, feed.base_url)
                else:
                    entries = parse_rss_entries(feed.feed_url, feed.base_url)

                for entry in entries:
                    self.notify(feed, entry)
            except Exception:
                logger.exception("Error processing feed: %s", feed.feed_url)

    def notify(self, feed: FeedConfig, entry: Entry):
        if entry.id in self.db:
            logger.info("Entry %s already posted. ignoring...", entry.id)
            return

        logger.info("Posting notification for %s...", entry.id)
        message = self.format_message(feed.message_prefix, entry)

        self.send_telegram_message(
            token=self.config.telegram_token,
            chat_id=feed.chat_id,
            message=message)
        self.db[entry.id] = "true"

        # Delay between messages to avoid hitting Telegram rate limits
        sleep(MESSAGE_DELAY)

    def format_message(self, prefix, entry: Entry):
        """Format a normalized entry into a Telegram HTML message."""
        title = escape(entry.title)
        author = escape(entry.author)
        body = escape(entry.body, quote=False)
        link = entry.link

        parts = [f"<i>{prefix}</i> <a href=\"{link}\">{title}</a>"]

        if entry.is_reply and entry.reply_to:
            parts.append(f"<b>{author}</b> replied to <b>{escape(entry.reply_to)}</b>")
        else:
            parts.append(f"<b>{author}</b>")

        parts.append(body)

        if entry.created_at:
            parts.append(f"🕒 <i>{escape(entry.created_at)}</i>")

        return "\n".join(parts)

    def send_telegram_message(self, token, chat_id, message):
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = {'chat_id': chat_id, 'text': message, 'parse_mode': 'html'}

        for attempt in range(1, MAX_RETRIES + 1):
            res = requests.post(url, data).json()

            if res.get("ok"):
                return

            # Rate limited — respect retry_after
            retry_after = res.get("parameters", {}).get("retry_after")
            if res.get("error_code") == 429 and retry_after and attempt < MAX_RETRIES:
                logger.warning("Rate limited. Waiting %s seconds (attempt %d/%d)...",
                               retry_after, attempt, MAX_RETRIES)
                sleep(retry_after + 1)
                continue

            # Other error or final attempt — log and raise
            logger.error("Failed to send message to telegram: %s", res)
            raise Exception(f"Failed to send message to telegram: {res}")


def _first(seq):
    try:
        return next(iter(seq))
    except StopIteration:
        return None


def main():
    config = Config.from_file("config.json")
    bot = FeedBot(config)
    bot.run()


if __name__ == "__main__":
    logger.info("tg-comment bot started")
    while True:
        try:
            main()
        except Exception:
            logger.exception("Error in main loop")
        sleep(5 * 60)
