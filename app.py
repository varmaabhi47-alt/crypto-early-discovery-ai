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
GECKO = "https://api.geckoterminal.com/api/v2"
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


async def get_json_with_retry(client: httpx.AsyncClient, url: str, attempts: int = 2):
    """Retry a public feed briefly; never substitute fabricated or stale values."""
    last_error = None
    for attempt in range(attempts):
        try:
            return await get_json(client, url)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last_error


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

    liquidity_usd = float(liquidity.get("usd") or 0)
    volume_24h = float(volume.get("h24") or 0)
    buys = int(h24.get("buys") or 0)
    sells = int(h24.get("sells") or 0)
    change_24h = price_change.get("h24")
    try:
        change_24h = float(change_24h) if change_24h is not None else 0.0
    except (TypeError, ValueError):
        change_24h = 0.0
    # Transparent, experimental screening heuristic; not a probability or buy signal.
    import math
    liquidity_score = min(100.0, max(0.0, (math.log10(max(liquidity_usd, 1) / 100) / 4) * 100))
    turnover_score = min(100.0, max(0.0, (volume_24h / max(liquidity_usd, 1)) * 50))
    trade_total = buys + sells
    flow_score = (buys / trade_total * 100) if trade_total else 50.0
    momentum_score = min(100.0, max(0.0, 50.0 + change_24h * 2.0))
    opportunity_score = round(
        liquidity_score * 0.30 + turnover_score * 0.25 +
        flow_score * 0.25 + momentum_score * 0.20, 1
    )

    return {
        "name": (pair.get("baseToken") or {}).get("name") or token.get("description") or "Unknown",
        "symbol": (pair.get("baseToken") or {}).get("symbol") or "—",
        "address": address,
        "pair_address": pair.get("pairAddress"),
        "url": pair.get("url") or token.get("url"),
        "dex": pair.get("dexId") or "Unknown",
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": liquidity_usd,
        "volume_24h_usd": volume_24h,
        "opportunity_score": opportunity_score,
        "score_components": {"liquidity": round(liquidity_score, 1), "turnover": round(turnover_score, 1), "buy_flow": round(flow_score, 1), "momentum": round(momentum_score, 1)},
        "price_change_24h_pct": price_change.get("h24"),
        "buys_24h": buys,
        "sells_24h": sells,
        "boost_amount": token.get("amount") or 0,
        "boost_total": token.get("totalAmount") or 0,
        "image": token.get("icon") or token.get("header"),
        "pair_created_at": pair.get("pairCreatedAt"),
        "security_status": "Not assessed",
        "source": "DexScreener"
    }


async def discover_gecko(client: httpx.AsyncClient):
    """Fallback discovery from GeckoTerminal's public Solana new-pools feed."""
    raw = await get_json_with_retry(
        client,
        f"{GECKO}/networks/solana/new_pools?include=base_token,dex",
        attempts=2,
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        raise ValueError("GeckoTerminal returned an unexpected response format.")

    included = {
        item.get("id"): item.get("attributes", {})
        for item in raw.get("included", [])
        if isinstance(item, dict) and item.get("type") == "token"
    }
    tokens = []
    for pool in raw["data"]:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        base_rel = (rel.get("base_token") or {}).get("data") or {}
        mint = str(base_rel.get("id") or "")
        if mint.startswith("solana_"):
            mint = mint[len("solana_"):]
        if not mint:
            continue
        base = included.get(base_rel.get("id"), {})
        txns = attrs.get("transactions") or {}
        h24 = txns.get("h24") or {}
        volume = attrs.get("volume_usd") or {}
        changes = attrs.get("price_change_percentage") or {}
        try:
            liquidity = float(attrs.get("reserve_in_usd") or 0)
            volume_24h = float(volume.get("h24") or 0)
            buys = int(h24.get("buys") or 0)
            sells = int(h24.get("sells") or 0)
            change = float(changes.get("h24") or 0)
        except (TypeError, ValueError):
            continue
        import math
        liq_score = min(100.0, max(0.0, (math.log10(max(liquidity, 1) / 100) / 4) * 100))
        turnover_score = min(100.0, max(0.0, (volume_24h / max(liquidity, 1)) * 50))
        total = buys + sells
        flow_score = buys / total * 100 if total else 50.0
        momentum_score = min(100.0, max(0.0, 50.0 + change * 2.0))
        score = round(liq_score * .30 + turnover_score * .25 + flow_score * .25 + momentum_score * .20, 1)
        pool_address = attrs.get("address")
        tokens.append({
            "name": base.get("name") or attrs.get("name") or "Unknown",
            "symbol": base.get("symbol") or "—",
            "address": mint,
            "pair_address": pool_address,
            "url": f"https://www.geckoterminal.com/solana/pools/{pool_address}" if pool_address else None,
            "dex": "GeckoTerminal",
            "price_usd": attrs.get("base_token_price_usd"),
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume_24h,
            "opportunity_score": score,
            "score_components": {"liquidity": round(liq_score, 1), "turnover": round(turnover_score, 1), "buy_flow": round(flow_score, 1), "momentum": round(momentum_score, 1)},
            "price_change_24h_pct": changes.get("h24"),
            "buys_24h": buys,
            "sells_24h": sells,
            "boost_amount": 0,
            "boost_total": 0,
            "image": base.get("image_url"),
            "pair_created_at": attrs.get("pool_created_at"),
            "security_status": "Not assessed",
            "source": "GeckoTerminal"
        })
    tokens.sort(key=lambda item: item["opportunity_score"], reverse=True)
    return tokens


async def discover_jupiter(client: httpx.AsyncClient):
    """Fallback discovery using Jupiter's public recently-created Solana tokens."""
    raw = await get_json_with_retry(
        client,
        "https://api.jup.ag/tokens/v2/recent",
        attempts=1,
    )
    if not isinstance(raw, list):
        raise ValueError("Jupiter returned an unexpected response format.")

    tokens = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mint = item.get("id") or item.get("address")
        if not mint:
            continue
        stats = item.get("stats24h") or item.get("stats") or {}
        liquidity = item.get("liquidity") or item.get("liquidityUsd") or 0
        volume = stats.get("volume") or stats.get("volumeUsd") or item.get("volume24h") or 0
        buys = stats.get("numBuys") or stats.get("buys") or 0
        sells = stats.get("numSells") or stats.get("sells") or 0
        change = stats.get("priceChange") or stats.get("priceChangePct") or 0
        try:
            liquidity = float(liquidity or 0)
            volume = float(volume or 0)
            buys = int(buys or 0)
            sells = int(sells or 0)
            change = float(change or 0)
        except (TypeError, ValueError):
            continue
        import math
        liq_score = min(100.0, max(0.0, (math.log10(max(liquidity, 1) / 100) / 4) * 100))
        turnover_score = min(100.0, max(0.0, (volume / max(liquidity, 1)) * 50))
        total = buys + sells
        flow_score = buys / total * 100 if total else 50.0
        momentum_score = min(100.0, max(0.0, 50.0 + change * 2.0))
        organic = item.get("organicScore")
        try:
            organic_score = float(organic) if organic is not None else 50.0
        except (TypeError, ValueError):
            organic_score = 50.0
        # Jupiter organic score contributes a modest quality signal, not a safety verdict.
        score = round(
            liq_score * .25 + turnover_score * .20 +
            flow_score * .20 + momentum_score * .15 + organic_score * .20, 1
        )
        tokens.append({
            "name": item.get("name") or "Unknown",
            "symbol": item.get("symbol") or "—",
            "address": mint,
            "pair_address": None,
            "url": f"https://jup.ag/tokens/{mint}",
            "dex": "Jupiter recent tokens",
            "price_usd": item.get("usdPrice") or item.get("price"),
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "opportunity_score": score,
            "score_components": {
                "liquidity": round(liq_score, 1),
                "turnover": round(turnover_score, 1),
                "buy_flow": round(flow_score, 1),
                "momentum": round(momentum_score, 1),
                "organic": round(organic_score, 1),
            },
            "price_change_24h_pct": change,
            "buys_24h": buys,
            "sells_24h": sells,
            "boost_amount": 0,
            "boost_total": 0,
            "image": item.get("icon"),
            "pair_created_at": None,
            "security_status": "Not assessed",
            "source": "Jupiter"
        })
    tokens.sort(key=lambda item: item["opportunity_score"], reverse=True)
    return tokens


async def discover():
    now = time.time()
    if now - _cache["at"] < CACHE_TTL and _cache["data"]:
        return _cache["data"], True, _cache.get("sources", {})

    timeout = httpx.Timeout(12.0, connect=6.0)
    headers = {"User-Agent": "CryptoEarlyDiscoveryAI/0.2"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        try:
            profile_result, boost_result = await asyncio.gather(
                get_json_with_retry(client, f"{DEX}/token-profiles/latest/v1"),
                get_json_with_retry(client, f"{DEX}/token-boosts/latest/v1"),
                return_exceptions=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Unable to contact the public discovery feeds.") from exc

        profiles_ok = not isinstance(profile_result, BaseException)
        boosts_ok = not isinstance(boost_result, BaseException)
        if not profiles_ok and not boosts_ok:
            try:
                gecko_tokens = await discover_gecko(client)
                _cache["at"] = now
                _cache["data"] = gecko_tokens
                _cache["sources"] = {"geckoterminal_new_pools": "available", "dexscreener": "unavailable"}
                return gecko_tokens, False, _cache["sources"]
            except Exception as gecko_exc:
                try:
                    jupiter_tokens = await discover_jupiter(client)
                    if not jupiter_tokens:
                        raise ValueError("Jupiter returned no usable recent tokens.")
                    _cache["at"] = now
                    _cache["data"] = jupiter_tokens
                    _cache["sources"] = {
                        "jupiter_recent_tokens": "available",
                        "dexscreener": "rate limited",
                        "geckoterminal": "rate limited"
                    }
                    return jupiter_tokens, False, _cache["sources"]
                except Exception as jupiter_exc:
                    def reason(exc):
                        if isinstance(exc, httpx.HTTPStatusError):
                            return f"HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.TimeoutException):
                            return "timeout"
                        if isinstance(exc, httpx.RequestError):
                            return "network error"
                        return type(exc).__name__
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "All configured public discovery feeds are unavailable.",
                            "dexscreener": {
                                "profiles": reason(profile_result),
                                "boosts": reason(boost_result)
                            },
                            "geckoterminal": reason(gecko_exc),
                            "jupiter": reason(jupiter_exc)
                        }
                    ) from jupiter_exc

        profiles = profile_result if profiles_ok and isinstance(profile_result, list) else []
        boosts = boost_result if boosts_ok and isinstance(boost_result, list) else []
        sources = {
            "token_profiles": "available" if profiles_ok and isinstance(profile_result, list) else "unavailable",
            "token_boosts": "available" if boosts_ok and isinstance(boost_result, list) else "unavailable",
        }

        combined = {}
        for item in profiles + boosts:
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
    tokens.sort(key=lambda item: item["opportunity_score"], reverse=True)
    _cache["at"] = now
    _cache["data"] = tokens
    _cache["sources"] = sources
    return tokens, False, sources


@app.get("/api/health")
async def health():
    return {"status": "ok", "app_version": app.version}


@app.get("/api/discover")
async def api_discover(
    min_liquidity: float = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=35)
):
    tokens, cached, data_sources = await discover()
    filtered = [t for t in tokens if t["liquidity_usd"] >= min_liquidity]
    return {
        "chain": "Solana",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cached": cached,
        "data_sources": data_sources,
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


@app.get("/api/holders/{mint}")
async def api_holders(mint: str):
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        raise HTTPException(status_code=400, detail="Invalid Solana mint address format.")

    now = time.time()
    key = "holders:" + mint
    cached = _security_cache.get(key)
    if cached and now - cached["at"] < SECURITY_CACHE_TTL:
        return {**cached["data"], "cached": True}

    url = f"{RUGCHECK}/v1/tokens/{mint}/report"
    timeout = httpx.Timeout(18.0, connect=6.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"}) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="No full cached RugCheck report is available for this token.")
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="RugCheck rate limit reached. Please wait before retrying.")
            response.raise_for_status()
            raw = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="RugCheck full report is unavailable or unreadable.") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="Unexpected full report format.")

    holders_raw = raw.get("topHolders")
    holders = []
    if isinstance(holders_raw, list):
        for item in holders_raw:
            if not isinstance(item, dict):
                continue
            pct = item.get("pct")
            try:
                pct = float(pct) if pct is not None else None
            except (TypeError, ValueError):
                pct = None
            holders.append({
                "address": item.get("address"),
                "owner": item.get("owner"),
                "pct": pct,
                "insider": item.get("insider") if isinstance(item.get("insider"), bool) else None
            })
    known_pcts = [h["pct"] for h in holders if h["pct"] is not None and h["pct"] >= 0]
    known_pcts.sort(reverse=True)
    report = {
        "mint": mint,
        "source": "RugCheck full report",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "holder_count_returned": len(holders),
        "top_1_pct": round(sum(known_pcts[:1]), 2) if known_pcts else None,
        "top_5_pct": round(sum(known_pcts[:5]), 2) if known_pcts else None,
        "top_10_pct": round(sum(known_pcts[:10]), 2) if known_pcts else None,
        "known_insider_pct": round(sum(h["pct"] or 0 for h in holders if h["insider"] is True), 2),
        "creator": raw.get("creator"),
        "creator_balance": raw.get("creatorBalance"),
        "mint_authority": (raw.get("token") or {}).get("mintAuthority") if isinstance(raw.get("token"), dict) else raw.get("mintAuthority"),
        "freeze_authority": (raw.get("token") or {}).get("freezeAuthority") if isinstance(raw.get("token"), dict) else raw.get("freezeAuthority"),
        "metadata_mutable": (raw.get("tokenMeta") or {}).get("mutable") if isinstance(raw.get("tokenMeta"), dict) else None,
        "top_holders": holders[:10],
        "limitations": "Only the top-holder entries returned by RugCheck are summarized. Percentages may include pool, burn, exchange or program accounts and are not necessarily unique beneficial owners. Missing data is unknown, not zero or safe."
    }
    _security_cache[key] = {"at": now, "data": report}
    return {**report, "cached": False}
