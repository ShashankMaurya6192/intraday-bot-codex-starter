# Intraday Bot

Local-first, paper-first Python foundation for recording Angel One SmartAPI
market data for Indian-equity intraday research.

Phase 1 includes configuration, broker-neutral domain models, a SQLite market
recorder, broker interfaces, and an Angel One WebSocket V2 adapter. It does
**not** include an order interface or any live order-placement code.

## Safety status

- `TRADING_MODE=PAPER` is the default.
- Phase 1 has no CLI option that can confirm or enable LIVE mode.
- `.env` is gitignored and secrets are represented as redacted settings.
- The Angel adapter can authenticate and receive market data, but it cannot
  place, modify, cancel, or query orders.
- SQLite records normalized ticks and market-feed lifecycle/error events.
- WebSocket reconnects use bounded exponential backoff.

## Windows 10/11 setup (PowerShell)

Install 64-bit Python 3.11 first. Then open PowerShell and run these
commands exactly from the repository directory:

```powershell
cd C:\Users\prash\Downloads\intraday-bot-codex-starter
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

In `.env`, keep these safety values unchanged:

```dotenv
TRADING_MODE=PAPER
LIVE_TRADING_ENABLED=false
```

Populate the four Angel One values only in your local `.env`:

```dotenv
ANGEL_API_KEY=your_smartapi_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_pin_or_password
ANGEL_TOTP_SECRET=your_totp_seed
```

`ANGEL_TOTP_SECRET` is the TOTP seed/QR value, not a current six-digit TOTP.
The application generates the current code at login. Never commit `.env` or
paste its contents into logs or issues.

Configure subscriptions using comma-separated
`EXCHANGE:TOKEN[:TRADING_SYMBOL]` entries. For example:

```dotenv
ANGEL_MARKET_SUBSCRIPTIONS=NSE_CM:3045:SBIN-EQ
ANGEL_SUBSCRIPTION_MODE=QUOTE
```

Symbol tokens are broker identifiers, not ticker names. Verify tokens against
Angel One's current instrument data before recording. Supported Phase 1
exchange names are `NSE_CM`, `NSE_FO`, `BSE_CM`, `BSE_FO`, `MCX_FO`, `NCX_FO`,
and `CDE_FO`; the initial intended universe is liquid `NSE_CM` equities.

Run the checks and initialize the database:

```powershell
python -m pytest
python -m ruff check .
python -m trading_bot status
python -m trading_bot init-db
```

Start the market recorder during an appropriate market session:

```powershell
python -m trading_bot record-market
```

Press `Ctrl+C` to stop. The default database is `trading_bot.db`. It is
gitignored and contains `market_ticks` plus `market_feed_events` tables.

If the PowerShell launcher `py` is unavailable but `python --version` reports
Python 3.11 or newer, create the environment with:

```powershell
python -m venv .venv
```

## Phase 1 architecture

```text
Angel One SmartWebSocketV2
  -> AngelOneMarketDataAdapter
  -> validated MarketTick (IST timestamps, Decimal rupee prices)
  -> SQLiteMarketRecorder
```

- `trading_bot.config`: environment parsing, risk defaults, and live safety gates
- `trading_bot.domain`: broker-neutral subscriptions, ticks, and feed events
- `trading_bot.broker.interfaces`: authentication and market-data contracts only
- `trading_bot.broker.angel`: official SDK authentication and WebSocket V2 adapter
- `trading_bot.storage.sqlite`: idempotent tick and feed-event persistence

The adapter follows the official
[Angel One SmartAPI Python SDK](https://github.com/angel-one/smartapi-python)
and [SmartAPI WebSocket documentation](https://smartapi.angelone.in/docs/Instruments).
The SDK is pinned to `smartapi-python==1.5.5`; review official release notes and
retest before upgrading it.

## Phase 2 scope (not implemented)

Phase 2 should add tick freshness/sequence monitoring, one-minute and
five-minute candle aggregation, persisted candle replay, indicators (VWAP,
EMA, ATR, relative volume, and relative strength), market-regime inputs, and a
configurable scanner/ranker. It must continue to suppress signals when market
data is stale, incomplete, or unreliable. Paper execution and all order logic
remain later phases.

See `AGENTS.md` for the full engineering and real-money safety contract.
