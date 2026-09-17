from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import Settings


class ReadOnlyKalshi:
    """Deliberately exposes no generic request method and no write endpoints."""
    def __init__(self, settings: Settings):
        self.s = settings
        self._key = None
        if settings.credentials_ready:
            self._key = serialization.load_pem_private_key(
                Path(settings.private_key_path).read_bytes(), password=None)

    def _headers(self, path: str) -> dict[str, str]:
        if not self._key:
            return {}
        timestamp = str(int(time.time() * 1000))
        text = timestamp + "GET" + path.split("?")[0]
        signature = self._key.sign(
            text.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                       salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.s.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def _get(self, route: str, params: dict | None = None) -> dict:
        path = "/trade-api/v2" + route
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(self.s.rest_url + route, params=params, headers=self._headers(path))
            response.raise_for_status()
            return response.json()

    async def markets(self, status: str = "open") -> list[dict]:
        data = await self._get("/markets", {"series_ticker": self.s.series, "status": status, "limit": 100})
        return data.get("markets", [])

    async def market(self, ticker: str) -> dict:
        return (await self._get(f"/markets/{ticker}"))["market"]

    async def series(self) -> dict:
        return (await self._get(f"/series/{self.s.series}"))["series"]

    async def stream(self, tickers: Callable[[], list[str]],
                     on_event: Callable[[dict, float], Awaitable[None]],
                     on_health: Callable[[str, str], Awaitable[None]]) -> None:
        if not self._key:
            await on_health("error", "Missing production API credentials")
            return
        reconnect = 0
        while True:
            try:
                headers = self._headers("/trade-api/ws/v2")
                async with websockets.connect(self.s.ws_url, additional_headers=headers,
                                              ping_interval=20, ping_timeout=20, max_queue=20_000) as ws:
                    await on_health("connected", "")
                    active = tickers()
                    if not active:
                        raise RuntimeError("No valid active BTC15 ticker available for subscription")
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"], "market_tickers": active,
                        "use_yes_price": True}}))
                    await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                        "channels": ["ticker", "trade"], "market_tickers": active}}))
                    await ws.send(json.dumps({"id": 3, "cmd": "subscribe", "params": {
                        "channels": ["market_lifecycle_v2"]}}))
                    await ws.send(json.dumps({"id": 4, "cmd": "subscribe", "params": {
                        "channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]}}))
                    await ws.send(json.dumps({"id": 5, "cmd": "subscribe", "params": {
                        "channels": ["cfbenchmarks_value_5hz"], "index_ids": ["BRTI"]}}))
                    async for text in ws:
                        received = time.time()
                        msg = json.loads(text)
                        await on_event(msg, received)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reconnect += 1
                await on_health("reconnecting", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(min(30, 2 ** min(reconnect, 5)) + random.random())
