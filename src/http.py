from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

import aiohttp


class HTTPFailure(RuntimeError):
    def __init__(self, status: int | None = None, *, transport: bool = False):
        # Never include URL, response body, request headers, or raw exceptions.
        super().__init__(f"HTTP status {status}" if status else "external request failed")
        self.status = status
        self.transport = transport


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError) as exc:
            raise HTTPFailure(self.status) from exc


class HTTPClient:
    def __init__(self, *, timeout: float = 30, attempts: int = 3, backoff: float = 1,
                 session: aiohttp.ClientSession | None = None):
        if timeout <= 0 or attempts < 1 or backoff < 0:
            raise ValueError("invalid HTTP retry configuration")
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.session = session

    async def request(self, method: str, url: str, *, retry: bool = True, return_errors: bool = False,
                      max_bytes: int = 5_000_000, before_attempt=None, **kwargs) -> Response:
        attempts = self.attempts if retry else 1
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=self.timeout))
        for attempt in range(attempts):
            if before_attempt is not None:
                before_attempt()
            owned = self.session is None
            session = self.session or aiohttp.ClientSession()
            retry_after = 0
            try:
                async with session.request(method, url, timeout=timeout, **kwargs) as resp:
                    try:
                        retry_after = min(60, max(0, float(resp.headers.get("Retry-After", "0"))))
                    except ValueError:
                        pass
                    parts = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        parts.extend(chunk)
                        if len(parts) > max_bytes:
                            raise HTTPFailure(resp.status)
                    response = Response(resp.status, bytes(parts), dict(resp.headers))
                    transient = resp.status == 429 or resp.status >= 500
                    if transient and retry and attempt + 1 < attempts:
                        pass
                    elif resp.status >= 400 and not return_errors:
                        raise HTTPFailure(resp.status)
                    else:
                        return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt + 1 >= attempts:
                    raise HTTPFailure(transport=True) from exc
            finally:
                if owned:
                    await session.close()
            delay = max(retry_after, min(60, self.backoff * 2 ** attempt))
            await asyncio.sleep(delay + (random.uniform(0, self.backoff / 4) if self.backoff else 0))
        raise HTTPFailure()

    async def json(self, method: str, url: str, **kwargs):
        return (await self.request(method, url, **kwargs)).json()
