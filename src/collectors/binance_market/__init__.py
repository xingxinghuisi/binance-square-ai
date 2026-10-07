from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from src.http import HTTPClient
from src.diagnostics import safe_error


def decimal_string(value, *, signed: bool = False) -> str:
    if len(str(value)) > 80:
        raise ValueError("invalid market number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid market number") from exc
    if (not number.is_finite() or (not signed and number < 0) or abs(number) > Decimal("1e30")
            or not -30 <= number.as_tuple().exponent <= 30):
        raise ValueError("invalid market number")
    return format(number, "f")


class BinanceMarketCollector:
    BASE = "https://api.binance.com/api/v3"

    def __init__(self, http: HTTPClient, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), *, observer=None):
        if not symbols or len(symbols) > 20 or any(not re.fullmatch(r"[A-Z0-9]{3,24}", s) for s in symbols):
            raise ValueError("MARKET_SYMBOLS must contain 1–20 Binance trading pairs")
        self.http, self.symbols = http, symbols
        self.observer = observer
        self.assets: dict[str, tuple[str, str]] = {}

    @staticmethod
    def normalize(ticker: dict, base_asset: str, quote_asset: str) -> dict:
        result = {"symbol": ticker["symbol"], "base_asset": base_asset, "quote_asset": quote_asset,
                  "last_price": decimal_string(ticker["lastPrice"]),
                  "change_24h": decimal_string(ticker["priceChangePercent"], signed=True),
                  "volume_24h": decimal_string(ticker["volume"]),
                  "quote_volume_24h": decimal_string(ticker["quoteVolume"]),
                  "high_24h": decimal_string(ticker["highPrice"]),
                  "low_24h": decimal_string(ticker["lowPrice"]),
                  "timestamp": datetime.fromtimestamp(int(ticker["closeTime"]) / 1000, timezone.utc).isoformat(),
                  "source": "Binance Spot"}
        if Decimal(result["high_24h"]) < Decimal(result["low_24h"]):
            raise ValueError("market high lower than low")
        return result

    async def collect(self) -> list[dict]:
        started = time.monotonic()
        try:
            rows = await self._collect()
        except Exception as exc:
            if self.observer:
                self.observer("Binance Spot", success=False, started=started, error=safe_error(exc))
            raise
        if self.observer:
            self.observer("Binance Spot", success=True, started=started, items=len(rows))
        return rows

    async def _collect(self) -> list[dict]:
        params = {"symbols": json.dumps(self.symbols, separators=(",", ":"))}
        if not self.assets:
            info = await self.http.json("GET", f"{self.BASE}/exchangeInfo", params=params)
            self.assets = {row["symbol"]: (row["baseAsset"], row["quoteAsset"])
                           for row in info["symbols"] if row["symbol"] in self.symbols}
            if set(self.assets) != set(self.symbols):
                self.assets = {}
                raise ValueError("Binance did not return all requested symbols")
        tickers = await self.http.json("GET", f"{self.BASE}/ticker/24hr", params=params)
        if not isinstance(tickers, list) or {r["symbol"] for r in tickers} != set(self.symbols):
            raise ValueError("Binance ticker response incomplete")
        return [self.normalize(row, *self.assets[row["symbol"]]) for row in tickers]
