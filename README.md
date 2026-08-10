# Intraday Bot

Local-first, paper-first Angel One SmartAPI market-data recorder and deterministic
market-intelligence scanner for Indian equities.

Phase 2 adds validated reference-data caches, tick health and sequence epochs,
IST-aligned one/five-minute candles, deterministic replay, indicators, NIFTY
regime classification, and explainable LONG/SHORT attention rankings. Scanner
results are analysis only. There is no paper execution, order interface, or live
order-placement code.

## Safety status

- `TRADING_MODE=PAPER` remains the default and only practical mode.
- No command confirms or enables LIVE mode.
- `.env`, SQLite databases, and `data/cache/` are gitignored.
- Credentials use secret settings and are never written to reference caches.
- The Angel adapter can authenticate, read market data, and read the official
  intraday-allowed list. It cannot place, modify, cancel, or query orders.
- The production WebSocket verifies certificates and hostnames.
- Reconnects, downloads, queues, and shutdown drains are bounded.
- Missing, stale, corrupt, ambiguous, sequence-broken, or incomplete data
  suppresses scanner readiness.

## Windows 10/11 setup (PowerShell)

Install 64-bit Python 3.11 first. Open PowerShell and run these commands exactly:

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

Keep these values unchanged:

```dotenv
TRADING_MODE=PAPER
LIVE_TRADING_ENABLED=false
```

Populate credentials only in the local `.env` file:

```dotenv
ANGEL_API_KEY=your_smartapi_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_pin_or_password
ANGEL_TOTP_SECRET=your_totp_seed
```

`ANGEL_TOTP_SECRET` is the seed/QR value, not a current six-digit code. Never
commit `.env` or paste it into logs or issues.

Run the checks and initialize SQLite:

```powershell
python -m pytest -q
python -m ruff check .
python -m trading_bot status
python -m trading_bot init-db
```

If `py` is unavailable but `python --version` is 3.11 or newer, use:

```powershell
python -m venv .venv
```

## Reference-data refresh workflow

Refreshes are explicit maintenance operations. `record-market`, `replay`, and
`scan` never download reference data.

```powershell
python -m trading_bot refresh-instruments
python -m trading_bot refresh-intraday-universe
python -m trading_bot refresh-nifty500
python -m trading_bot data-status
python -m trading_bot data-status --json
```

- `refresh-instruments` downloads and validates Angel One's complete official
  scrip master. No credentials are required.
- `refresh-intraday-universe` authenticates and stores the current Angel NSE
  intraday-allowed symbol/multiplier snapshot. Run it before or near each market
  session. The multiplier is retained as broker metadata and never used to
  increase strategy risk.
- `refresh-nifty500` downloads the official NSE Indices constituent CSV.

If the NSE Indices host is unavailable, download the authoritative CSV and use
the explicit local import path:

```powershell
Invoke-WebRequest -Uri "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv" -OutFile ".\ind_nifty500list.csv"
python -m trading_bot refresh-nifty500 --source .\ind_nifty500list.csv
```

Every refresh validates the full response before writing a temporary file and
atomically replacing the cache. A failed refresh leaves the previous valid file
unchanged. Metadata includes fetch time, source record count, source SHA-256,
normalized-record SHA-256, source URL, and schema version.

Default freshness policy:

- Angel instrument master: at most 7 days old.
- NIFTY 500 constituents: at most 7 days old.
- Angel NSE intraday snapshot: at most 28,800 seconds old **and from the current
  IST date**. Yesterday's list is never accepted.

`data-status` shows age/count/hash without credentials. It exits with status 2
when a required cache is unavailable or stale. It never refreshes or rewrites a
cache.

## Recording, replay, and scanning

Configure one WebSocket with at most 1,000 token-mode subscriptions:

```dotenv
ANGEL_MARKET_SUBSCRIPTIONS=NSE_CM:3045:SBIN-EQ,NSE_CM:99926000:Nifty 50
ANGEL_SUBSCRIPTION_MODE=QUOTE
```

Tokens and trading symbols must be verified from the local Angel cache. Missing
tokens are never guessed.

Record during the market session:

```powershell
python -m trading_bot record-market
```

Replay a recorded session and idempotently persist completed candles:

```powershell
python -m trading_bot replay --date 2026-08-10
python -m trading_bot replay --date 2026-08-10 --json
```

Scan current locally recorded data:

```powershell
python -m trading_bot scan
python -m trading_bot scan --limit 20
python -m trading_bot scan --json --limit 20
```

The default universe is the intersection of:

1. the cached NIFTY 500 EQ constituent list;
2. unambiguous normal NSE cash-equity identities from Angel's cached master; and
3. the current Angel intraday-allowed snapshot.

Use a small deterministic smoke-test universe without changing the caches:

```dotenv
SCANNER_SYMBOLS=SBIN,RELIANCE,INFY,TCS,HDFCBANK
```

An unmapped, ambiguous, non-NIFTY-500, or non-intraday-allowed symbol is excluded
with a diagnostic rather than assigned a guessed token.

## Phase 2 architecture

```text
explicit refresh commands
  -> validated atomic Angel/NIFTY caches

recorded SmartAPI ticks
  -> feed-epoch health/sequence monitor
  -> deterministic 09:15 IST candle aggregation
  -> idempotent SQLite candles + complete-session RVOL baselines
  -> replay-safe indicators + NIFTY regime
  -> explainable analysis-only scanner rankings
```

- `trading_bot.reference_data`: atomic cache metadata, bounded verified-HTTPS
  downloads, NIFTY 500 import, and fail-closed universe resolution.
- `trading_bot.broker.instruments`: concrete Angel master and authoritative
  instrument identity validation.
- `trading_bot.broker.intraday`: current-date Angel NSE intraday snapshot.
- `trading_bot.marketdata`: tick health, reconnect epochs, candles, replay, and
  market regime.
- `trading_bot.indicators`: pure indicator functions with explicit warm-ups.
- `trading_bot.scanner`: deterministic candidate scoring, diagnostics, and JSON.
- `trading_bot.storage`: ticks, feed events, candles, and RVOL baselines in SQLite.

## Data-health meanings

- `READY`: all required caches are current; NIFTY and every ranked instrument
  have trusted, fresh data and all required indicators are warmed up.
- `DEGRADED`: reference boundaries are valid but one or more universe members
  are stale, untrusted, unmapped, excluded, or indicator-incomplete. Any shown
  ranking is partial and must not be treated as executable.
- `NOT_READY`: a required cache/universe/benchmark is unavailable or stale, or
  the scanner cannot form a trustworthy market view.
- `UNKNOWN`: the market-regime value when NIFTY is stale/untrusted or VWAP,
  EMA, or slope history is not ready. `UNKNOWN` prevents scanner readiness.

Ticks become stale after `MARKET_DATA_STALE_AFTER_SECONDS` (default 5). Sequence
duplicates are ignored; out-of-order ticks cannot mutate candles. Any sequence
gap is material by default. Reconnects begin a new feed epoch, and degraded data
requires three consecutive in-order ticks by default before returning to
trusted state. A candle touched by untrusted ticks remains untrusted.

## Candle and indicator definitions

- One/five-minute candles align from the NSE cash open at 09:15 IST, not process
  start. Only completed candles are persisted; strategy-style confirmation uses
  completed five-minute candles.
- Candle volume is the non-negative delta of Angel's cumulative session volume.
  The first observation and a cumulative reset contribute zero, preventing
  fabricated volume.
- Session VWAP uses completed/trusted candles and typical price
  `(high + low + close) / 3`, weighted by incremental candle volume.
- EMA 9/20 is seeded with the first period's simple mean.
- ATR 14 is the simple average of 14 true ranges and requires 15 candles so each
  range has a prior close.
- RVOL compares cumulative volume at the same session minute against up to 15
  prior complete/trusted local sessions and requires 10 by default.
- Relative strength is stock session return minus NIFTY session return, in
  percentage points.
- VWAP distance is reported in percentage and ATR units.
- Opening range is the trusted high/low over a complete, contiguous first 15
  session minutes by default.
- Regime is `TREND_UP`, `TREND_DOWN`, `RANGE`, or `UNKNOWN`, using NIFTY relative
  to VWAP, five-minute EMA 9/20, and short-horizon slope. No breadth is invented.

## Scanner scoring

The scanner ranks LONG-bias and SHORT-bias attention lists independently. The
default maximum score is 10:

- price on the bias side of VWAP: 2;
- EMA 9/20 aligned with the bias: 2;
- RVOL at or above 1.5: 2;
- relative strength aligned with the bias: 2;
- within configured opening-range breakout/breakdown proximity: 1;
- NIFTY regime aligned with the bias: 1.

Weights and thresholds are configuration. Ties sort by trading symbol. Candidate
output includes LTP, health/age, VWAP distances, EMA values, ATR, RVOL, relative
strength, opening range/distances, a sector placeholder, regime, component
scores, total score, and diagnostics. It contains no quantity, executable entry,
fill, or order instruction.

## TLS, reconnect, and ingestion guarantees

The project pins `smartapi-python==1.5.5`. That SDK's WebSocket `connect()` uses
`ssl.CERT_NONE`, so this project narrowly subclasses its transport and connects
with `ssl.CERT_REQUIRED` and `check_hostname=True`. The official parser,
callbacks, subscriptions, and heartbeat behavior remain inherited. Other SDK
versions fail closed until reviewed. No global SSL state is modified.

Reaching `on_open` does not reset retry accounting. The socket must remain open
for `WEBSOCKET_STABLE_CONNECTION_SECONDS` before the failure streak resets, so
rapid flapping exhausts the bounded budget. The bounded ingestion queue never
evicts ticks: overflow, SQLite writer failure, or drain timeout marks the feed
unsafe and stops it.

## Automated CI

`.github/workflows/ci.yml` runs Python 3.11 on `ubuntu-latest` and
`windows-latest`, installing `.[dev]`, running Ruff, and running the complete
credential-free pytest suite.

## Phase 3 remains unimplemented

Phase 3 may add deterministic paper execution, fill simulation assumptions,
position/risk state, protective exits, restart recovery, and kill-switch tests.
It must not enable live orders. Phase 2 scanner output first needs validation
across multiple recorded/replayed sessions, including feed gaps and cache refresh
operations.

See `AGENTS.md` for the full engineering and real-money safety contract.
