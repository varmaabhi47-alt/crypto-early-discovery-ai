import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
RUGCHECK = "https://api.rugcheck.xyz"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
REDDIT_SEARCH = "https://www.reddit.com/search.json"
CACHE_TTL = 900
_cache: dict[str, dict[str, Any]] = {}
_rpc_semaphore = asyncio.Semaphore(3)
_wallet_re = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

POSITIVE_WORDS = {
    "bullish", "moon", "mooning", "pump", "pumping", "breakout", "buy", "buying",
    "accumulate", "accumulation", "undervalued", "strong", "growth", "gem",
    "adoption", "partnership", "launch", "win", "winner", "profit", "profits",
    "surge", "rally", "upside", "green", "solid", "great"
}
NEGATIVE_WORDS = {
    "bearish", "dump", "dumping", "rug", "rugpull", "scam", "fraud", "sell",
    "selling", "exit", "exited", "exploit", "hack", "hacked", "drain", "drained",
    "dead", "collapse", "crash", "crashing", "loss", "losses", "warning",
    "suspicious", "fake", "honeypot", "red", "weak", "avoid"
}


def classify_sentiment(text: str) -> str:
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    positive = len(words & POSITIVE_WORDS)
    negative = len(words & NEGATIVE_WORDS)
    if positive > negative:
        return "positive"
    if negative > positive:
        return "negative"
    return "neutral"


def aggregate_wallets(creator: str | None, holders: list[dict]) -> list[dict]:
    wallets: dict[str, dict] = {}
    if creator and _wallet_re.fullmatch(str(creator)):
        wallets[creator] = {"address": creator, "roles": ["creator"], "holder_pct": 0.0, "insider_flagged": False}
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        owner = holder.get("owner")
        if not owner or not _wallet_re.fullmatch(str(owner)):
            # RugCheck may return token-account addresses without a beneficial owner.
            # Do not mislabel those token accounts as wallets.
            continue
        row = wallets.setdefault(owner, {"address": owner, "roles": [], "holder_pct": 0.0, "insider_flagged": False})
        if "top holder" not in row["roles"]:
            row["roles"].append("top holder")
        try:
            row["holder_pct"] += max(0.0, float(holder.get("pct") or 0))
        except (TypeError, ValueError):
            pass
        row["insider_flagged"] = row["insider_flagged"] or holder.get("insider") is True
    result = list(wallets.values())
    result.sort(key=lambda row: (row["address"] not in (creator or ""), row["holder_pct"]), reverse=False)
    return result[:6]


async def solana_rpc(client: httpx.AsyncClient, method: str, params: list):
    async with _rpc_semaphore:
        response = await client.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("error"):
        raise ValueError(str(payload["error"]))
    return payload.get("result")


def token_balance_for_wallet(rows: list, mint: str, owner: str) -> tuple[int, int] | None:
    amount = 0
    decimals = None
    found = False
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("mint") != mint or row.get("owner") != owner:
            continue
        token_amount = row.get("uiTokenAmount") or {}
        try:
            raw_amount = int(token_amount.get("amount") or 0)
            row_decimals = int(token_amount.get("decimals") or 0)
        except (TypeError, ValueError):
            continue
        amount += raw_amount
        decimals = row_decimals
        found = True
    return (amount, decimals or 0) if found else None


async def inspect_wallet(client: httpx.AsyncClient, wallet: dict, mint: str) -> dict:
    address = wallet["address"]
    result = {
        **wallet,
        "activity_status": "unavailable",
        "latest_signature": None,
        "latest_activity_at": None,
        "token_events": [],
        "activity_note": "Recent activity could not be verified from the public Solana RPC."
    }
    try:
        signatures = await solana_rpc(
            client, "getSignaturesForAddress",
            [address, {"limit": 8, "commitment": "confirmed"}]
        )
        if not isinstance(signatures, list):
            return result
        result["latest_signature"] = signatures[0].get("signature") if signatures else None
        result["latest_activity_at"] = (
            datetime.fromtimestamp(signatures[0]["blockTime"], tz=timezone.utc).isoformat()
            if signatures and isinstance(signatures[0].get("blockTime"), (int, float)) else None
        )
        events = []
        for signature_row in signatures[:5]:
            signature = signature_row.get("signature")
            if not signature:
                continue
            transaction = await solana_rpc(
                client, "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
            )
            if not isinstance(transaction, dict):
                continue
            meta = transaction.get("meta") or {}
            pre = token_balance_for_wallet(meta.get("preTokenBalances"), mint, address)
            post = token_balance_for_wallet(meta.get("postTokenBalances"), mint, address)
            if pre is None and post is None:
                continue
            decimals = (post or pre)[1]
            before = pre[0] if pre else 0
            after = post[0] if post else 0
            delta = after - before
            if delta == 0:
                continue
            amount = delta / (10 ** decimals) if decimals >= 0 else float(delta)
            events.append({
                "direction": "accumulation" if delta > 0 else "distribution",
                "token_amount_change": round(amount, min(decimals, 8)),
                "block_time": transaction.get("blockTime"),
                "signature": signature,
                "explorer_url": "https://solscan.io/tx/" + signature
            })
        result["token_events"] = events
        result["activity_status"] = "available"
        result["activity_note"] = (
            "Recent transaction sample only. Balance changes are inferred from parsed transaction token balances; "
            "transfers, LP operations, account changes and incomplete RPC metadata can affect interpretation."
        )
        if not signatures:
            result["activity_note"] = "No recent signatures were returned by the public RPC."
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
        return result


@router.get("/api/wallets/{mint}")
async def api_wallet_intelligence(mint: str):
    if not _wallet_re.fullmatch(mint):
        raise HTTPException(status_code=400, detail="Invalid Solana mint address format.")
    cache_key = "wallets:" + mint
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["at"] < CACHE_TTL:
        return {**cached["data"], "cached": True}

    timeout = httpx.Timeout(18.0, connect=7.0)
    headers = {"Accept": "application/json", "User-Agent": "CryptoEarlyDiscoveryAI/0.5"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(f"{RUGCHECK}/v1/tokens/{mint}/report")
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="No full RugCheck holder report is available for this token.")
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="RugCheck rate limit reached. Try again later.")
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict):
                raise ValueError("Unexpected RugCheck report format.")
            holder_rows = raw.get("topHolders") if isinstance(raw.get("topHolders"), list) else []
            wallets = aggregate_wallets(raw.get("creator"), holder_rows)
            tracked = await asyncio.gather(*(inspect_wallet(client, row, mint) for row in wallets))
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Wallet intelligence providers are temporarily unavailable.") from exc

    data = {
        "mint": mint,
        "source": ["RugCheck full holder report", "Solana public mainnet RPC"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "wallet_count": len(tracked),
        "wallets": tracked,
        "coverage": "Creator and up to five distinct beneficial-owner addresses among returned top holders. Token-account addresses without an owner are not treated as wallets.",
        "limitations": "This is an on-demand snapshot, not continuous surveillance. Recent signatures are sampled (up to five per wallet). RPC history may be incomplete or rate limited. A token-balance increase/decrease is not definitive proof of a market buy/sell; transfers, liquidity operations and account changes can produce similar changes. Holder percentages are provider-reported and may not represent unique beneficial ownership."
    }
    _cache[cache_key] = {"at": time.time(), "data": data}
    return {**data, "cached": False}


@router.get("/api/social/{mint}")
async def api_social_sentiment(mint: str, name: str = "", symbol: str = ""):
    if not _wallet_re.fullmatch(mint):
        raise HTTPException(status_code=400, detail="Invalid Solana mint address format.")
    safe_name = re.sub(r"[^A-Za-z0-9 ._-]", " ", name).strip()[:60]
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol).strip()[:16]
    terms = []
    if len(safe_name) >= 4:
        terms.append('"' + safe_name + '"')
    if len(safe_symbol) >= 4 and safe_symbol.lower() not in {safe_name.lower()}:
        terms.append(safe_symbol)
    if not terms:
        terms.append(mint)
    cache_key = "social:" + mint
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["at"] < CACHE_TTL:
        return {**cached["data"], "cached": True}

    headers = {
        "Accept": "application/json",
        "User-Agent": "CryptoEarlyDiscoveryAI/0.5 (public token research dashboard)"
    }
    posts_by_id: dict[str, dict] = {}
    source_errors = []
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for term in terms[:2]:
            try:
                response = await client.get(
                    REDDIT_SEARCH,
                    params={"q": term, "sort": "new", "t": "week", "limit": 25, "type": "link"}
                )
                response.raise_for_status()
                payload = response.json()
                children = ((payload.get("data") or {}).get("children") or [])
                for child in children:
                    post = child.get("data") or {}
                    post_id = post.get("id")
                    if not post_id:
                        continue
                    text_value = (str(post.get("title") or "") + " " + str(post.get("selftext") or "")).strip()
                    posts_by_id[post_id] = {
                        "id": post_id,
                        "title": str(post.get("title") or "")[:240],
                        "subreddit": post.get("subreddit"),
                        "score": post.get("score"),
                        "comments": post.get("num_comments"),
                        "created_at": datetime.fromtimestamp(post["created_utc"], tz=timezone.utc).isoformat()
                            if isinstance(post.get("created_utc"), (int, float)) else None,
                        "url": "https://www.reddit.com" + str(post.get("permalink") or ""),
                        "sentiment": classify_sentiment(text_value),
                        "text_sample": str(post.get("selftext") or "")[:280]
                    }
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                source_errors.append(type(exc).__name__)

    posts = list(posts_by_id.values())
    positive = sum(1 for post in posts if post["sentiment"] == "positive")
    negative = sum(1 for post in posts if post["sentiment"] == "negative")
    neutral = len(posts) - positive - negative
    score = round(((positive - negative) / len(posts)) * 100, 1) if posts else None
    data = {
        "mint": mint,
        "query_terms": terms,
        "source": "Reddit public search",
        "window": "past 7 days",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mention_count": len(posts),
        "positive_count": positive,
        "negative_count": negative,
        "neutral_count": neutral,
        "sentiment_score": score,
        "engagement": {
            "post_score_total": sum(int(post.get("score") or 0) for post in posts),
            "comment_total": sum(int(post.get("comments") or 0) for post in posts)
        },
        "posts": sorted(posts, key=lambda post: post.get("created_at") or "", reverse=True)[:12],
        "provider_status": "available" if posts or not source_errors else "unavailable",
        "provider_errors": source_errors,
        "method": "Transparent keyword lexicon over post titles and self-text. It is not an LLM judgement; sarcasm, spam, coordinated promotion and ambiguous tickers can mislead it.",
        "limitations": "Reddit only. This does not cover X, Telegram, Discord or all crypto communities. Search results are incomplete, can be rate limited, and mentions are not proof of genuine interest or token quality."
    }
    _cache[cache_key] = {"at": time.time(), "data": data}
    return {**data, "cached": False}
