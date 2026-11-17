import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Crypto Early Discovery AI", version="0.2.0")
DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz"
CACHE_TTL = 60
SECURITY_CACHE_TTL = 3600
_cache: dict[str, Any] = {"at": 0.0, "data": []}
_security_cache: dict[str, Any] = {}
_semaphore = asyncio.Semaphore(5)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def home():
    return (Path("static") / "index.html").read_text(encoding="utf-8")


async def get_json(client: httpx.AsyncClient, url: str):
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def enrich_token(client: httpx.AsyncClient, token: dict):
    address = token.get("tokenAddress")
    if not address:
        return None
    async with _semaphore:
        try:
            pairs = await get_json(client, f"{DEX}/token-pairs/v1/solana/{address}")
        except (httpx.HTTPError, ValueError):
            return None

    if not isinstance(pairs, list) or not pairs:
        return None
    pairs = [p for p in pairs if isinstance(p, dict)]
    if not pairs:
        return None
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    price_change = pair.get("priceChange") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}

    return {
        "name": (pair.get("baseToken") or {}).get("name") or token.get("description") or "Unknown",
        "symbol": (pair.get("baseToken") or {}).get("symbol") or "—",
        "address": address,
        "pair_address": pair.get("pairAddress"),
        "url": pair.get("url") or token.get("url"),
        "dex": pair.get("dexId") or "Unknown",
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": liquidity.get("usd") or 0,
        "volume_24h_usd": volume.get("h24") or 0,
        "price_change_24h_pct": price_change.get("h24"),
        "buys_24h": h24.get("buys") or 0,
        "sells_24h": h24.get("sells") or 0,
        "boost_amount": token.get("amount") or 0,
        "boost_total": token.get("totalAmount") or 0,
        "image": token.get("icon") or token.get("header"),
        "pair_created_at": pair.get("pairCreatedAt"),
        "security_status": "Not assessed",
        "source": "DexScreener"
    }


async def discover():
    now = time.time()
    if now - _cache["at"] < CACHE_TTL and _cache["data"]:
        return _cache["data"], True

    timeout = httpx.Timeout(12.0, connect=6.0)
    headers = {"User-Agent": "CryptoEarlyDiscoveryAI/0.2"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        try:
            profiles, boosts = await asyncio.gather(
                get_json(client, f"{DEX}/token-profiles/latest/v1"),
                get_json(client, f"{DEX}/token-boosts/latest/v1"),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="The public market-data provider is temporarily unavailable.") from exc

        combined = {}
        for item in (profiles or []) + (boosts or []):
            if isinstance(item, dict) and item.get("chainId") == "solana":
                address = item.get("tokenAddress")
                if address:
                    combined.setdefault(address, {}).update(item)

        candidates = list(combined.values())[:35]
        results = await asyncio.gather(
            *(enrich_token(client, item) for item in candidates),
            return_exceptions=True
        )

    tokens = [item for item in results if isinstance(item, dict)]
    tokens.sort(key=lambda item: item["liquidity_usd"], reverse=True)
    _cache["at"] = now
    _cache["data"] = tokens
    return tokens, False


@app.get("/api/health")
async def health():
    return {"status": "ok", "app_version": app.version}


@app.get("/api/discover")
async def api_discover(
    min_liquidity: float = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=35)
):
    tokens, cached = await discover()
    filtered = [t for t in tokens if t["liquidity_usd"] >= min_liquidity]
    return {
        "chain": "Solana",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cached": cached,
        "count": len(filtered[:limit]),
        "tokens": filtered[:limit],
        "disclaimer": "Discovery data is informational, can be delayed or incomplete, and is not a buy recommendation."
    }


@app.get("/api/security/{mint}")
async def api_security(mint: str):
    # Solana public keys are base58 strings; reject malformed input before network access.
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        raise HTTPException(status_code=400, detail="Invalid Solana mint address format.")

    now = time.time()
    cached = _security_cache.get(mint)
    if cached and now - cached["at"] < SECURITY_CACHE_TTL:
        return {**cached["data"], "cached": True}

    url = f"{RUGCHECK}/v1/tokens/{mint}/report/summary"
    timeout = httpx.Timeout(15.0, connect=6.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"}) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="No cached RugCheck report is available for this token.")
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="RugCheck rate limit reached. Please wait before retrying.")
            response.raise_for_status()
            raw = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="RugCheck is temporarily unavailable or returned an unreadable response.") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="Unexpected security report format.")

    risks = raw.get("risks") if isinstance(raw.get("risks"), list) else []
    report = {
        "mint": mint,
        "source": "RugCheck",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "score_normalised": raw.get("score_normalised"),
        "score_raw": raw.get("score"),
        "lp_locked_pct": raw.get("lpLockedPct"),
        "risks": [
            {
                "name": item.get("name", "Unnamed risk"),
                "level": item.get("level", "unknown"),
                "value": item.get("value", ""),
                "description": item.get("description", "")
            }
            for item in risks if isinstance(item, dict)
        ],
        "note": "A cached third-party heuristic report, not a guarantee of safety. Missing fields mean unknown, not safe."
    }
    _security_cache[mint] = {"at": now, "data": report}
    return {**report, "cached": False}
