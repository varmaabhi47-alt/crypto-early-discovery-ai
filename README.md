# Crypto Early Discovery AI

A read-only crypto intelligence terminal, starting with Solana.

## Current milestone: discovery + on-demand security summaries

- Fetches latest public Solana token profiles and boosts from DexScreener.
- Enriches token addresses with pair price, liquidity, 24-hour volume, price movement, and buy/sell counts.
- Adds an on-demand RugCheck summary lookup for an individual token mint.
- Displays available normalized score, LP locked percentage, and reported risk items.
- Caches market discovery briefly and security summaries locally for one hour to reduce repeated calls.

## Run locally

Requires Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 in a browser.

On Windows, activate the environment with `.venv\\Scripts\\activate`.

## API

- `GET /api/health`
- `GET /api/discover?min_liquidity=0&limit=30`
- `GET /api/security/{solana_mint_address}`

## Data and cost

- Market discovery uses public DexScreener API endpoints.
- Security summaries use RugCheck's public cached summary endpoint. Free access, caching behavior, rate limits, and provider availability can change. The application only requests a report when the user presses **Check security**.
- No paid API, hosting, subscription, or database is required by this local prototype.
- No private wallet keys, wallet connection, or real-money execution.

## Important limitations

This is an early discovery and risk-screening tool, not a trading signal or investment recommendation. RugCheck data is a third-party heuristic summary, may be cached or incomplete, and cannot guarantee safety. A missing report or missing field means unknown, not safe.

The app does not yet independently verify holder concentration, whale-wallet activity, token creator history, social momentum, or liquidity-lock claims. These need separate evidence and validation. Paper-trading evaluation and persistent storage are not implemented yet.


## Holder and authority analysis

The dashboard also provides an on-demand **Holder analysis** action for a discovered Solana mint. It requests RugCheck's full token report and summarizes the returned top-holder entries (top 1/5/10 concentration, insider-tagged percentages), creator information, mint/freeze authority fields, and metadata mutability.

API: `GET /api/holders/{mint}`

The holder figures describe only the top-holder entries returned by the provider. Token accounts are not always unique beneficial owners; pool, burn, exchange, and program accounts can affect concentration. Missing values are unknown, not zero or safe. Reports are cached locally for one hour. No bulk scanning is performed.
