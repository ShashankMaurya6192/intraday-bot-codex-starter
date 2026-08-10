# Intraday Bot

Local-first, paper-first Python foundation for recording Angel One SmartAPI
market data for Indian-equity intraday research.

Phase 1 plus the Phase 1.5 hardening gate includes configuration,
broker-neutral domain models, a SQLite market recorder, broker interfaces, a
verified-TLS Angel One WebSocket V2 adapter, deterministic reconnect limits,
and bounded asynchronous tick ingestion. It does **not** include a scanner,
strategy, order interface, or any live order-placement code.

## Safety status

- `TRADING_MODE=PAPER` is the default.
- Phase 1 has no CLI option that can confirm or enable LIVE mode.
- `.env` is gitignored and secrets are represented as redacted settings.
- The Angel adapter can authenticate and receive market data, but it cannot
  place, modify, cancel, or query orders.
- SQLite records normalized ticks and market-feed lifecycle/error events.
- The production WebSocket requires certificate and hostname verification.
- WebSocket reconnects use bounded exponential backoff and a stability window.
- A bounded queue keeps SQLite transactions off the broker callback thread.
- Queue overflow, writer failure, and shutdown drain timeout stop the feed.

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
Angel One's current instrument data before recording. Supported Phase 1.5
exchange names are `NSE_CM`, `NSE_FO`, `BSE_CM`, `BSE_FO`, `MCX_FO`, `NCX_FO`,
and `CDE_FO`; the initial intended universe is liquid `NSE_CM` equities.
One Phase 1.5 process uses one WebSocket and accepts at most 1000 token-mode
subscriptions. A request for 1001 or more fails before authentication or
connection.

The following hardening defaults are configurable in `.env`:

```dotenv
WEBSOCKET_STABLE_CONNECTION_SECONDS=30
TICK_INGESTION_QUEUE_CAPACITY=10000
TICK_INGESTION_BATCH_SIZE=250
TICK_INGESTION_FLUSH_INTERVAL_SECONDS=0.05
TICK_INGESTION_SHUTDOWN_TIMEOUT_SECONDS=10
```

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

## Phase 1.5 architecture

```text
Angel One SmartWebSocketV2 parser/callbacks
  -> verified TLS transport (CERT_REQUIRED + hostname check)
  -> AngelOneMarketDataAdapter
  -> validated MarketTick (IST timestamps, Decimal rupee prices)
  -> bounded TickIngestionService queue
  -> dedicated batch-writer thread
  -> SQLiteMarketRecorder
```

- `trading_bot.config`: environment parsing, risk defaults, and live safety gates
- `trading_bot.domain`: broker-neutral subscriptions, ticks, and feed events
- `trading_bot.broker.interfaces`: authentication and market-data contracts only
- `trading_bot.broker.angel`: official SDK parser with a verified-TLS transport
- `trading_bot.broker.instruments`: fail-closed future instrument-cache boundary
- `trading_bot.storage.ingestion`: bounded queue and dedicated batch writer
- `trading_bot.storage.sqlite`: idempotent tick and feed-event persistence

### TLS guarantee

The adapter follows the official
[Angel One SmartAPI Python SDK](https://github.com/angel-one/smartapi-python)
and [SmartAPI WebSocket documentation](https://smartapi.angelone.in/docs/Instruments).
The SDK is pinned to `smartapi-python==1.5.5`. That SDK's `connect()` passes
`ssl.CERT_NONE`, so this project subclasses only its transport connection and
uses `ssl.CERT_REQUIRED` plus `check_hostname=True`. The official binary parser,
callbacks, subscription format, and heartbeat behavior remain inherited. The
production factory rejects other SDK versions until the wrapper is reviewed and
retested. No global SSL state is changed.

### Reconnect stability rule

Reaching `on_open` is not success for retry accounting. A connection must stay
open for at least `WEBSOCKET_STABLE_CONNECTION_SECONDS` before the consecutive
failure streak resets. Repeated open-and-immediate-close flapping therefore
exhausts the same bounded retry budget as handshake failures. A user-requested
disconnect remains clean and does not reconnect.

### Tick ingestion failure behavior

The WebSocket callback validates a tick and performs a non-blocking enqueue.
The dedicated writer drains batches in SQLite transactions. The queue is
bounded: it never evicts or silently discards ticks. A full queue, SQLite writer
exception, or bounded shutdown-drain timeout marks ingestion `UNHEALTHY`, stops
the feed, rejects further ticks, and prevents downstream use until the process
is repaired/restarted.

### Future instrument-master boundary

Phase 1.5 defines the `CachedInstrumentMaster`, `BrokerInstrument`, and
`IntradayAllowedUniverse` contracts but does not build the scanner. Phase 2 must
load a validated local last-known-good Angel instrument snapshot before market
hours, validate exchange/token/trading-symbol/tick-size identity from it, and
intersect candidates with Angel One's current NSE intraday-allowed list. Missing
or corrupt cache data must raise an error; guessed tokens are forbidden.

## Automated CI

`.github/workflows/ci.yml` runs on every push and pull request on Python 3.11
for both `ubuntu-latest` and `windows-latest`. It installs `.[dev]`, runs Ruff,
and runs the complete pytest suite without broker credentials.

## Phase 2 scope (not implemented)

Phase 2 should add tick freshness/sequence monitoring, one-minute and
five-minute candle aggregation, persisted candle replay, indicators (VWAP,
EMA, ATR, relative volume, and relative strength), market-regime inputs, and a
configurable scanner/ranker. It must continue to suppress signals when market
data is stale, incomplete, or unreliable. Paper execution and all order logic
remain later phases.

The cached instrument-master loader and the broker intraday-allowed-list data
source are deliberately not implemented here; they are the first fail-closed
data boundaries to complete when Phase 2 begins. No credentialed live-market
smoke test is performed by CI.

See `AGENTS.md` for the full engineering and real-money safety contract.
