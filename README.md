# Crypto Early Discovery AI

A read-only crypto intelligence terminal, starting with Solana.

## Current milestone: Solana discovery MVP

- Fetches the latest public token profiles and boosts from DexScreener.
- Enriches Solana token addresses with available pair data.
- Displays price, liquidity, 24-hour volume, price movement, and buy/sell counts.
- Uses a short in-memory cache to reduce repeated public API requests.
- Clearly labels security status as **Not assessed**.

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

## Data and cost

- Uses public DexScreener API endpoints; provider limits and availability can change.
- No paid API, hosting, subscription, or database is required for this local prototype.
- No private wallet keys, wallet connection, or real-money execution.

## Important limitations

This is an early discovery dashboard, not a trading signal or investment recommendation. Token promotions can be misleading. Security checks, holder concentration, whale-wallet intelligence, social intelligence, explainable scoring, persistent storage, and paper-trading evaluation are not implemented yet. Do not interpret missing security data as a safe token.
