"""Persistent observations; GET status reads never initiate external requests."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from src.http import BudgetExceeded, HTTPClient, HTTPFailure


def safe_error(exc: Exception) -> str:
    # Deliberately do not echo arbitrary exception text, response bodies or URLs.
    if isinstance(exc, HTTPFailure):
        if exc.status:
            return f"HTTP {exc.status}" + ("; invalid/oversized response" if exc.status < 400 else "")
        if exc.category:
            return {"dns": "DNS lookup failed", "connect_timeout": "connect timeout",
                    "read_timeout": "read timeout", "tls": "TLS handshake/certificate failed",
                    "timeout": "request timeout (total deadline)", "connection": "connection failed"}[exc.category]
        return "request failed: timeout, connection or TLS error"
    if isinstance(exc, BudgetExceeded):
        return "daily AI request budget exhausted"
    return f"request/response validation failed ({type(exc).__name__})"


def provider_identity(name: str, model: str, key: str) -> str:
    # Observations of an old credential/model must not imply a new configuration works.
    digest = hashlib.sha256((model + "\0" + key).encode()).hexdigest()
    return name + ":" + digest


class ObservedProvider:
    def __init__(self, provider, store):
        self.provider, self.store = provider, store
        self.name, self.model = provider.name, provider.model
        self.identity = provider_identity(self.name, self.model, provider.api_key)

    @property
    def request_count(self):
        return self.provider.request_count

    async def generate(self, system: str, prompt: str, *, max_tokens: int = 900) -> str:
        try:
            text = await self.provider.generate(system, prompt, max_tokens=max_tokens)
            if not text.strip():
                raise ValueError("empty model response")
        except Exception as exc:
            self.store.record_status("provider", self.identity, success=False, error=safe_error(exc))
            raise
        self.store.record_status("provider", self.identity, success=True)
        return text


def providers_status(settings, store) -> dict:
    result = {}
    for name in ("gemini", "groq"):
        model, key = getattr(settings, name + "_model"), getattr(settings, name + "_api_key")
        state = store.diagnostic("provider", provider_identity(name, model, key)) if key else {}
        result[name] = {"configured": bool(key), "model": model, "reachable": state.get("reachable"),
                        "last_success": state.get("last_success"), "last_error": state.get("last_error")}
    return result


def sources_status(store) -> list[dict]:
    from src.collectors.crypto_news import RSS_FEEDS
    result = []
    for source in ("Binance Spot", *RSS_FEEDS):
        state = store.diagnostic("source", source)
        reachable = state.get("reachable")
        status = "unknown" if reachable is None else "error" if not reachable else "ok" if state["items"] else "empty"
        result.append({"source": source, "critical": source == "Binance Spot", "status": status, "latency_ms": state.get("latency_ms"),
                       "last_success": state.get("last_success"), "last_error": state.get("last_error"),
                       "items": state.get("items", 0)})
    return result


def staging_readiness(settings, store, *, database_initialized: bool = True) -> dict:
    """Local preflight, not a live probe. RSS failures do not block market-only drafting."""
    import db
    from src.collectors.crypto_news import RSS_FEEDS
    from src.settings import enforce_dry_run, publishing_enabled
    enforce_dry_run()
    database_ok = False
    if database_initialized:
        try:
            with db.get_conn() as conn:
                database_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                conn.execute("SELECT 1 FROM binance_queue LIMIT 1").fetchone()
        except Exception:
            database_ok = False
    try:
        states = sources_status(store) if database_ok else []
        spot_state = store.diagnostic("source", "Binance Spot") if database_ok else {}
    except Exception:
        states, spot_state = [], {}
        database_ok = False
    spot = next((s for s in states if s["critical"]), {"source": "Binance Spot", "critical": True,
                                                    "status": "unknown", "last_error": None})
    try:
        checked = datetime.fromisoformat(spot_state["checked_at"])
        age = datetime.now(timezone.utc).timestamp() - checked.timestamp() if checked.tzinfo else -1
        fresh = 0 <= age <= settings.readiness_max_age
    except (KeyError, TypeError, ValueError, OverflowError):
        fresh = False
    spot = {**spot, "checked_at": spot_state.get("checked_at"), "fresh": fresh}
    if spot["status"] in {"ok", "empty"} and not fresh:
        spot["status"] = "stale"
    rss = [s for s in states if not s["critical"]] or [{"source": source, "status": "unknown"} for source in RSS_FEEDS]
    configured = {name: bool(getattr(settings, name + "_api_key").strip()) for name in ("gemini", "groq")}
    dry_run_lock = not publishing_enabled()
    checks = {"database": database_ok, "dry_run_lock": dry_run_lock, "ai_configured": any(configured.values()),
              "admin_token": bool(settings.admin_token.strip()), "binance_spot": spot["status"] == "ok" and fresh}
    return {"ready": all(checks.values()), "database": database_ok, "dry_run_lock": dry_run_lock,
            "providers": configured, "admin_token_configured": checks["admin_token"], "binance_spot": spot,
            "rss": rss, "square_api_key": "ignored/not required", "auto_publish": False,
            "blocking": [name for name, ok in checks.items() if not ok],
            "warnings": [f"{source['source']}: {source['status']} (noncritical)" for source in rss if source["status"] != "ok"]}


class ReadOnlyHTTPClient(HTTPClient):
    """Allow only data/AI endpoints in staging; no Square or media routes."""
    async def request(self, method: str, url: str, **kwargs):
        from src.collectors.crypto_news import RSS_FEEDS
        parts = urlsplit(url)
        allowed = parts.scheme == "https" and not parts.username and not parts.password and (
            (method == "GET" and url in RSS_FEEDS.values())
            or (method == "GET" and parts.netloc == "api.binance.com"
                and parts.path in {"/api/v3/exchangeInfo", "/api/v3/ticker/24hr"})
            or (method == "POST" and parts.netloc == "generativelanguage.googleapis.com"
                and parts.path.startswith("/v1beta/models/") and parts.path.endswith(":generateContent"))
            or (method == "POST" and url == "https://api.groq.com/openai/v1/chat/completions")
        )
        if not allowed:
            raise ValueError("Dry-run endpoint denied")
        kwargs["allow_redirects"] = False
        rss_host = parts.netloc if url in RSS_FEEDS.values() else None
        for _ in range(5):
            response = await super().request(method, url, **kwargs)
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            target = urljoin(url, response.headers.get("Location", ""))
            dest = urlsplit(target)
            # Never forward model credentials or follow a feed to Square/media endpoints.
            if method != "GET" or not rss_host or dest.scheme != "https" or dest.netloc != rss_host or target == url:
                raise HTTPFailure(response.status)
            url = target
        raise HTTPFailure(310)


async def test_ai(settings, store, http) -> dict:
    from src.ai.provider import build_provider
    # Probe each configured provider independently; no fallback, writer or queue.
    provider = build_provider(settings, http, store=store)
    for candidate in provider.providers:
        try:
            raw = await candidate.provider.generate('Return JSON only: {"ok":true}.', 'Connectivity check. JSON only.', max_tokens=256)
            if json.loads(raw) != {"ok": True}:
                raise ValueError("invalid smoke response")
            store.record_status("provider", candidate.identity, success=True)
        except Exception as exc:
            store.record_status("provider", candidate.identity, success=False, error=safe_error(exc))
    return providers_status(settings, store)


async def test_sources(settings, store, http) -> list[dict]:
    from src.collectors.binance_market import BinanceMarketCollector
    from src.collectors.crypto_news import CryptoNewsCollector
    news = CryptoNewsCollector(http, observer=source_observer(store))
    market = BinanceMarketCollector(http, settings.market_symbols, observer=source_observer(store))
    await news.collect()
    try:
        await market.collect()
    except Exception:
        pass  # collector already persisted a source-specific failure
    return sources_status(store)


def source_observer(store):
    def observe(source, *, success, started, items=0, error=None):
        store.record_status("source", source, success=success, error=error, items=items,
                            latency_ms=max(0, round((time.monotonic() - started) * 1000)))
    return observe
