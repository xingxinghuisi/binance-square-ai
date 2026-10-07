from __future__ import annotations

import calendar
import hashlib
import logging
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from src.http import HTTPClient

logger = logging.getLogger(__name__)
RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
SYMBOL_ALIASES = {
    "BTC": ("bitcoin", "比特币"), "ETH": ("ethereum", "ether", "以太坊"),
    "BNB": ("binance coin",), "SOL": ("solana",), "XRP": ("ripple",),
    "DOGE": ("dogecoin",), "ADA": ("cardano",), "AVAX": ("avalanche",),
    "LINK": ("chainlink",), "DOT": ("polkadot",), "SUI": (), "TON": ("toncoin",),
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("invalid news URL")
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
             and k.lower() not in {"ref", "fbclid", "gclid"}]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def extract_symbols(text: str) -> list[str]:
    found = []
    for symbol, aliases in SYMBOL_ALIASES.items():
        ticker = re.search(r"(?<![\w])\$?" + re.escape(symbol) + r"(?![\w])", text)
        if ticker or any(re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text, re.I)
                         if term.isascii() else term in text for term in aliases):
            found.append(symbol)
    return found


class CryptoNewsCollector:
    def __init__(self, http: HTTPClient, feeds: dict[str, str] | None = None, *, max_items: int = 30):
        self.http, self.feeds, self.max_items = http, RSS_FEEDS if feeds is None else feeds, max_items
        self.last_errors: list[dict] = []

    @staticmethod
    def parse(source: str, body: bytes, *, max_items: int = 30) -> list[dict]:
        parsed = feedparser.parse(body)
        if parsed.bozo and not parsed.entries:
            raise ValueError("invalid RSS/Atom feed")
        result = []
        seen = set()
        for entry in parsed.entries[:max_items]:
            try:
                title = clean_text(entry.get("title", ""))[:500]
                if not title:
                    continue
                url = canonical_url(entry.get("link", ""))
                # Never replace an unknown publication date with 'now'.
                date = entry.get("published_parsed") or entry.get("updated_parsed")
                if not date:
                    continue
                published_at = datetime.fromtimestamp(calendar.timegm(date), timezone.utc).isoformat()
                summary = clean_text(entry.get("summary", ""))[:1200]
                item_id = hashlib.sha256(f"{source}:{url}".encode()).hexdigest()
                if item_id in seen:
                    continue
                seen.add(item_id)
                result.append({"id": item_id, "source": source, "title": title, "summary": summary,
                               "url": url, "published_at": published_at,
                               "symbols": extract_symbols(title + " " + summary)})
            except (ValueError, TypeError, OverflowError):
                logger.warning("Skipping malformed RSS item from %s", source)
        return result

    async def collect(self) -> list[dict]:
        result = []
        self.last_errors = []
        for source, url in self.feeds.items():
            try:
                response = await self.http.request("GET", url, headers={"User-Agent": "BinanceSquareAI/Phase1 RSS"})
                result.extend(self.parse(source, response.body, max_items=self.max_items))
            except Exception as exc:
                # One blocked/unavailable feed must not stop the remaining collectors.
                logger.warning("RSS source %s unavailable (%s)", source, type(exc).__name__)
                self.last_errors.append({"source": source, "error": type(exc).__name__})
        return result
