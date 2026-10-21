# Intraday Bot

Local-first, paper-first Angel One SmartAPI market-data recorder and deterministic
market-intelligence scanner for Indian equities.

Phase 2.5.4 materializes current-session health, candles, and recovery
references incrementally while `record-scanner-market` runs. That recorder is
the single normal projection writer. Normal scans read one transactionally
consistent SQLite snapshot and never advance or refresh the checkpoint.
Durable candle completion follows same-instrument exchange-time progression;
wall-clock-complete working buckets are read-only ephemeral views until that
progression makes them stable.
Phase 2.4 ranking and qualification semantics remain unchanged, and scanner
results and qualified signals remain analysis only. Phase 3's safe foundation
adds a broker-free risk engine, persisted paper orders/positions, deterministic
simulated fills, protective exits, restart recovery, and a paper kill switch.
A separate policy can convert a qualified candidate into an explicit local
paper intent only after rechecking current breakout state, freshness,
authoritative identity, same-day intraday eligibility, liquidity, and the
initial stop. An explicit replay-only harness can exercise that policy and the
paper simulator against recorded ticks using immutable same-session reference
data and isolated in-memory paper accounts. There is deliberately no automatic
scanner-to-trade runner and no live order-placement code.

No trading system can promise 100% accuracy. This project optimizes for
repeatable decisions, bounded loss, conservative simulation, and refusing to
trade when state is uncertain—not a guaranteed win rate.

## Safety status

- `TRADING_MODE=PAPER` remains the default and only practical mode.
- No command confirms or enables LIVE mode.
- `.env`, SQLite databases, and `data/cache/` are gitignored.
- Credentials use secret settings and are never written to reference caches.
- The Angel adapter can authenticate, read live/historical market data, and read
  the official intraday-allowed list. It cannot place, modify, cancel, or query
  orders.
- Paper orders are local SQLite records only. A paper fill is never represented
  as an Angel/broker fill.
- New paper entries require a qualified signal flag, trusted data, a valid hard
  stop, verified simulated funds/liquidity, and every configured risk limit.
- A stale/untrusted tick affecting exposure, a queue/writer failure reported to
  the paper engine, or uncertain state cancels pending entries and blocks new
  ones. Exits are never fabricated without a trusted tick.
- The production WebSocket verifies certificates and hostnames.
- Reconnects, downloads, queues, and shutdown drains are bounded.
- Missing, stale, corrupt, ambiguous, timestamp-disordered, or incomplete data
  suppresses scanner readiness.
- With the default `HISTORICAL_PROVIDER=angel`, a Historical API rate-limit
  response aborts the whole bootstrap immediately; it is never retried as a
  transient error and never advances to another symbol.
- A yfinance continuation is possible only in PAPER mode when the operator sets
  both `HISTORICAL_PROVIDER=auto` and `HISTORICAL_FALLBACK_ENABLED=true`.
- Angel may throttle the Historical API independently while normal login and
  live WebSocket market data remain healthy.

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
HISTORICAL_PROVIDER=angel
HISTORICAL_FALLBACK_ENABLED=false
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
python -m trading_bot paper-init
python -m trading_bot paper-status
python -m trading_bot paper-report --from-date 2026-10-01 --to-date 2026-10-31
python -m trading_bot paper-replay --date 2026-10-20 --json
```

If `py` is unavailable but `python --version` is 3.11 or newer, use:

```powershell
python -m venv .venv
```

The default paper account and simulation controls are:

```dotenv
PAPER_STARTING_EQUITY=1000000
PAPER_ENTRY_START_TIME=09:30:00
PAPER_LAST_ENTRY_TIME=14:45:00
PAPER_SQUARE_OFF_TIME=15:15:00
PAPER_SLIPPAGE_BPS=5
PAPER_TICK_PARTICIPATION_PCT=0.10
PAPER_FIRST_PARTIAL_PCT=0.50
PAPER_SECOND_PARTIAL_PCT=0.25
PAPER_STOP_BUFFER_ATR=0.10
PAPER_MINIMUM_STOP_TICKS=2
PAPER_MAXIMUM_STOP_ATR=1.50
```

These settings affect only local simulation. They do not create an Angel order
path or relax the PAPER/LIVE gate.

The checked-in estimated-cost defaults are decimal fractions, where `0.001`
means 0.1%:

```dotenv
PAPER_COST_SCHEDULE_AS_OF=2026-10-21
PAPER_BROKERAGE_RATE=0.001
PAPER_BROKERAGE_MINIMUM_PER_ORDER=5
PAPER_BROKERAGE_MAXIMUM_PER_ORDER=20
PAPER_EXCHANGE_TRANSACTION_RATE=0.000030699
PAPER_STT_SELL_RATE=0.00025
PAPER_IPFT_RATE=0.000000001
PAPER_SEBI_TURNOVER_RATE=0.000001
PAPER_STAMP_DUTY_BUY_RATE=0.00003
PAPER_GST_RATE=0.18
PAPER_REPLAY_MAX_TICKS=250000
PAPER_REPLAY_MAX_INSTRUMENTS=20
PAPER_REPLAY_LIQUIDITY_PARTICIPATION_PCT=0.10
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
- `refresh-nifty500` downloads the CSV linked by the current NSE NIFTY 500
  product page from `nsearchives.nseindia.com`, with
  `archives.nseindia.com` as an official HTTPS fallback.

If the NSE Indices host is unavailable, download the authoritative CSV and use
the explicit local import path:

```powershell
Invoke-WebRequest -Uri "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv" -OutFile ".\ind_nifty500list.csv"
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

For the intended scanner, derive every token from fresh local caches and add the
authoritative NIFTY 50 benchmark automatically:

```powershell
python -m trading_bot refresh-instruments
python -m trading_bot refresh-nifty500
python -m trading_bot refresh-intraday-universe
python -m trading_bot record-scanner-market
```

`record-scanner-market` performs no downloads. Before authentication it prints
requested, eligible, expected-ineligible, mapping-failure, benchmark, and final
subscription counts and enforces the one-socket 1,000-token limit. The benchmark
is added exactly once and does not need equity intraday eligibility. Stale or
missing caches and an invalid benchmark mapping fail closed. Keep
`record-market` for manual diagnostic subscriptions.

Replay a recorded session and idempotently persist completed candles:

```powershell
python -m trading_bot replay --date 2026-08-10
python -m trading_bot replay --date 2026-08-10 --json
```

Manual replay persistence follows a strict IST session-completion rule:

- a date before the current IST date may be replayed and persisted;
- the current IST date is rejected with non-zero status and
  `reason=current_session_not_complete` before 15:30 IST;
- at or after 15:30 IST, a current-date replay may persist only as a full replay
  with no as-of cutoff, using the authoritative raw rows committed when the
  command reads SQLite and the fixed 15:30 NSE cash-session close;
- a future date is rejected with `future_session_not_complete`;
- any replay with an explicit `evaluation_at` is analysis-only and never writes
  candles or RVOL baselines, even if persistence was requested internally.

Incomplete or untrusted one-minute coverage never replaces an RVOL baseline.
Only a complete, trusted set covering all 375 regular-session minute offsets is
eligible for baseline persistence. These replay rules do not modify the
incremental scanner projection or its single-writer ownership.

Scan current locally recorded data:

```powershell
python -m trading_bot scan
python -m trading_bot scan --limit 20
python -m trading_bot scan --json --limit 20
python -m trading_bot compare-projection-replay --date 2026-08-11 --json
```

Start or restart `record-scanner-market` after upgrading so it can build the
current-session projection. Phase 2.5.3 increments the projection state version,
so its first start deterministically rebuilds today's derived projection once
from retained authoritative raw ticks. The rebuild preserves raw ticks, feed
events, and prior historical candles. Later restarts restore the checkpoint and
read only raw ticks/feed events after its cursors.
Normal `scan` never silently falls back to a full-day replay when the projection
is unavailable or inconsistent.

`scan` is a read-only projection consumer. It loads the checkpoint, health,
references, and completed current-session candles in one SQLite read
transaction. If authoritative raw ticks or feed events are ahead of that
checkpoint, it returns `NOT_READY` with `projection_catch_up_pending`; leave or
restart `record-scanner-market` so the recorder-owned projector can catch up.
The scan process never performs an independent mutable suffix catch-up and
never changes checkpoint cursors, `updated_at`, or `state_json`.

Latest-state scan reads intentionally do not use the command's pre-cache start
time as an as-of cutoff. A recorder checkpoint committed while caches are being
loaded is valid current state. The scanner evaluates exactly at that committed
checkpoint's `updated_at`, then samples the wall clock again after the snapshot
load to calculate age from `last_received_at` and enforce the five-second live
freshness gate. Explicit historical/as-of projection reads still reject a
checkpoint newer than their requested cutoff.

The ranked candidate lists are preserved even when a candidate is unqualified.
Human output includes qualification counts and a `QUAL` column. JSON includes
`qualified`, ordered `qualification_reasons`, `qualified_long_signals`,
`qualified_short_signals`, and their counts. These are analytical gates only;
there is still no execution consumer.

Normal JSON output also includes `state_source=INCREMENTAL_PROJECTION`,
`projection_age_seconds`, `projection_load_seconds`,
`projection_last_tick_id`, `incremental_ticks_processed`,
`full_replay_used=false`, and `raw_ticks_replayed=0`. During the live NSE cash
session, a missing projection or age above
`SCANNER_PROJECTION_STALE_AFTER_SECONDS` (default `5`) fails closed. Any raw-row
lead requires the scanner recorder to catch up and does not trigger full replay.

Bootstrap prior research history explicitly before expecting early-session
EMA/ATR/RVOL readiness. This command never runs from `record-market`,
`record-scanner-market`, or `scan`:

```powershell
notepad .env
python -m trading_bot bootstrap-history
python -m trading_bot bootstrap-history --json
python -m trading_bot bootstrap-history --force
```

Before a larger bootstrap, test one authoritative cached NSE equity and one
completed session with exactly one broker request and no retries:

```powershell
python -m trading_bot test-historical --symbol SBIN --date 2026-08-07
python -m trading_bot test-historical --symbol SBIN --date 2026-08-07 --json
```

To diagnose the optional yfinance path without writing SQLite, use the exact
PAPER-only command below. It reports the provider symbol, chunk/row counts,
complete and discarded-partial sessions, first/last timestamp, validation, and
the disabled order capability:

```powershell
python -m trading_bot test-history-provider --provider yfinance --symbol SBIN --lookback-days 30
python -m trading_bot test-history-provider --provider yfinance --symbol SBIN --lookback-days 30 --json
```

The diagnostic authenticates normally, resolves `SBIN-EQ` only from the fresh
local Angel instrument cache, and prints broker status, message, error code, row
count, and first/last timestamp. It never prints the symbol token, credentials,
session tokens, or request headers. Throttled and rejected requests exit with
status code 2.

For the initial smoke test, set 5-20 symbols by name—never token ID—in `.env`:

```dotenv
SCANNER_SYMBOLS=SBIN,RELIANCE,INFY,TCS,HDFCBANK
```

`bootstrap-history` authenticates the official SmartAPI Python SDK, calls its
authenticated `POST /rest/secure/angelbroking/historical/v1/getCandleData`
Historical API with `ONE_MINUTE`, spaces requests conservatively, retries only
transient transport/server failures with bounded backoff, validates every row,
requires complete 09:15-15:30 sessions, derives five-minute candles locally,
and atomically stores normalized history in SQLite. Rejected or malformed
responses are not retried. The default 30-calendar-day range is split at
deterministic session boundaries into requests of at most 22 calendar days, so
the possible one-minute record count remains below the broker limit; all chunks
are combined and validated before a symbol is replaced.

On the first Historical API throttle response, including SmartAPI SDK
non-JSON errors such as `Access denied because of exceeding access rate`, the
batch returns `status=ABORTED_RATE_LIMIT` and stops immediately. It does not
retry that request or call later symbols, even with `--force`. Human and JSON
output report `requested`, `completed`, `failed`, `skipped_after_abort`,
`rate_limited_symbol`, `reason_category=RATE_LIMITED`, and
`order_placement_available=false`, plus a short operator action. Do not loop the
command: wait for Angel's Historical API access window to reset, then rerun it.
Previously completed atomic writes remain valid, the throttled symbol retains
any older valid dataset, and a normal rerun skips completed symbols before
resuming.

Angel remains the primary historical provider. The default configuration never
contacts yfinance. To permit a PAPER-mode bootstrap to continue through
yfinance after the first Angel throttle, set these values explicitly:

```dotenv
TRADING_MODE=PAPER
HISTORICAL_PROVIDER=auto
HISTORICAL_FALLBACK_ENABLED=true
```

After that first throttle, the run makes no further Angel historical requests.
It validates each yfinance row, maps `FOO-EQ` to `FOO.NS` and NIFTY 50 to
`^NSEI`, downloads deterministic chunks of at most seven calendar days,
normalizes timestamps to `Asia/Kolkata`, discards individual partial sessions,
and accepts only complete 375-minute regular NSE sessions. Only then does it use
the same canonical five-minute/RVOL persistence path. A symbol is replaced only
after the configured minimum complete-session count passes. Provider and
provenance are persisted in bootstrap status. Direct
`HISTORICAL_PROVIDER=yfinance` also requires
`HISTORICAL_FALLBACK_ENABLED=true`; all yfinance bootstrap use is rejected in
LIVE mode. This is a research fallback, not an executable market-data source.

`record-scanner-market` remains usable while historical bootstrap is
broker-throttled because its WebSocket path is independent. Until enough prior
history is available, scanner EMA/ATR/RVOL warm-ups may remain incomplete and
scanner readiness will continue to fail safe.

The default range targets at least 15 complete prior sessions. Complete equity
sessions create RVOL baselines; NIFTY never does. A rerun skips current
per-symbol datasets, while `--force` intentionally refreshes them. A malformed,
partial, rejected, or throttled response cannot replace the previous valid
symbol dataset.

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

## Market-intelligence and paper architecture

```text
explicit refresh commands
  -> validated atomic Angel/NIFTY caches

explicit SmartAPI historical diagnostic/bootstrap
  -> distinct throttle/rejection/transport failure categories
  -> default global abort, or explicit PAPER-only yfinance continuation
  -> validated complete 1m sessions
  -> provider/provenance status + local 5m/RVOL derivation

manual raw replay
  -> reject unfinished current/future IST sessions before any persistence
  -> prior session or current date at/after fixed 15:30 close
  -> full replay only for canonical candle persistence
  -> complete/trusted 375-minute sessions only for RVOL baselines

recorded SmartAPI ticks
  -> feed-epoch health monitor + sequence telemetry
  -> causal session references + deterministic 09:15 IST candle aggregation
  -> raw SQLite audit rows
  -> exchange-progressed stable candles + checkpointed working state
  -> read-only ephemeral as-of candles for wall-clock-complete working buckets
  -> idempotent SQLite candles + complete-session RVOL baselines
  -> replay-safe indicators + NIFTY regime
  -> explainable analysis-only scanner rankings
  -> strict analysis-only signal qualification

qualified signal + independent strategy-to-paper policy
  -> recheck current breakout, trusted freshness, identity, eligibility,
     verified liquidity, and deterministic initial stop
  -> explicit paper trade intent (not automatically submitted)
  -> fail-closed risk gates + risk/capital/liquidity sizing
  -> persisted local paper order
  -> later trusted tick + adverse-slippage/participation fill model
  -> persisted paper position + hard stop/partials/time exit
  -> daily limits + kill switch + restart-safe audit state

explicit completed-session PAPER replay
  -> immutable same-session cache hashes + eligible instrument identities
  -> causal five-minute scanner evaluations with no future ticks
  -> isolated in-memory paper accounts for each sensitivity scenario
  -> recorded next-tick gaps + zero/base/double slippage + double-cost reports
```

- `trading_bot.reference_data`: atomic cache metadata, bounded verified-HTTPS
  downloads, NIFTY 500 import, and fail-closed universe resolution.
- `trading_bot.broker.instruments`: concrete Angel master and authoritative
  instrument identity validation.
- `trading_bot.broker.intraday`: current-date Angel NSE intraday snapshot.
- `trading_bot.marketdata`: tick health, reconnect epochs, candles, causal
  replay, incremental projection, historical bootstrap, and market regime.
- `trading_bot.indicators`: pure indicator functions with explicit warm-ups.
- `trading_bot.scanner`: deterministic candidate scoring, diagnostics, and JSON.
- `trading_bot.strategy`: broker-free qualified-signal to PAPER-intent policy.
- `trading_bot.backtest`: frozen reference snapshots and causal PAPER replay.
- `trading_bot.risk`: broker-neutral sizing and portfolio/day entry gates.
- `trading_bot.paper`: broker-free paper order/fill/position state machine.
- `trading_bot.storage`: raw ticks/feed events, projected health/references,
  projection checkpoints, candles, RVOL baselines, and paper audit state in
  SQLite.

## Incremental projection and restart safety

Scanner recording sends normalized ticks and feed events through one bounded
FIFO ingestion queue. Each item is committed to authoritative raw audit storage
before its projection is advanced. If derived-state persistence fails after the
raw commit, ingestion becomes unhealthy and disconnects the feed; the durable
cursor remains behind so the next startup can replay that missing suffix. No
tick is silently discarded between storage and projection.

SQLite materializes:

- `market_health_snapshot`: latest epoch-aware health per instrument;
- `market_session_reference`: latest, pre-09:20, and pre-09:30 trusted broker
  session references;
- `market_projection_checkpoint`: current session, raw tick/feed-event cursors,
  timestamps, feed epoch, state version, and validated working-engine state;
- `market_candles`: existing idempotent completed 1m/5m candles, including
  `ANGEL_SESSION_FIELDS` recovery provenance.

The checkpoint includes in-progress candle buckets, cumulative-volume anchors,
health recovery counters, and session-reference accumulators. A valid restart
therefore processes only rows after both cursors and does not flush restored
working candles merely because restart wall time is later. A missing, corrupt,
or older-version checkpoint is rebuilt once by `record-scanner-market`; the
rebuild clears only current-session derived projection rows with
`LOCAL_TICKS`/`ANGEL_SESSION_FIELDS` provenance. A checkpoint claiming a raw
tick/event ID ahead of SQLite fails closed because it could indicate lost
authoritative data. Current-session raw ticks and feed events, plus prior
historical sessions, remain retained for audit and explicit replay.

Normal durable candle completion is caused only when a valid tick for the same
instrument advances into a later exchange-time bucket. A recorder batch crossing
a received-time or wall-clock minute boundary never finalizes working candles.
The checkpoint retains those working OHLCV/trust aggregates. A read-only
projection snapshot may expose a working bucket whose `end_at` is at or before
the committed checkpoint time as an ephemeral completed candle. It does not
delete working state or write that provisional candle to `market_candles`, so a
later valid tick in the same exchange-time bucket can revise the next view.
Cumulative-volume deltas and reconnect/untrusted-tick contamination therefore
remain identical to deterministic raw replay.

Projection commits use optimistic compare-and-swap inside an immediate SQLite
write transaction. The writer may replace the materialized health/reference
rows and checkpoint only when the persisted predecessor still exactly matches
the checkpoint it restored. If another writer advanced first, the stale writer
gets `ProjectionConcurrentAdvanceError`; it cannot rewind or overwrite the
newer state. A scan encountering a projection conflict fails closed with a
bounded diagnostic instead of exposing a raw SQLite/value traceback.

Read-side consistency uses an explicit SQLite snapshot transaction. Checkpoint
version N is returned with health, references, durable current-session candles,
and ephemeral completed working candles reconstructed from version N's
`state_json`, never mixed with version N+1. Ephemeral identities are
deterministically deduplicated with durable rows and first-five-minute recovery.
Projection age is measured from a fresh post-load wall-clock sample without
touching the checkpoint, so repeated scans cannot make stale state appear fresh.
The latest read API has no stale caller-time cutoff; its optional explicit cutoff
retains the causal `ProjectionCutoffBehindCheckpointError` guard for historical
callers.

The read-only equivalence command compares projected and replayed health, sequence
telemetry, completed OHLCV/trust/provenance, references, scanner
readiness/regime, candidate indicators/scores, and qualification output at the
persisted checkpoint `updated_at`. It loads latest committed state first, so a
recorder advance during cache loading is valid rather than mistaken for future
data. It never materializes or catches up the live projection. It exits
non-zero on any mismatch and emits only bounded category diagnostics:

```powershell
python -m trading_bot compare-projection-replay --date 2026-08-11
python -m trading_bot compare-projection-replay --date 2026-08-11 --json
```

This verification path intentionally performs raw replay. Candle mismatches
include bounded projection/replay counts, only-on-one-side counts, changed-value
counts, and at most five example identities with their differing fields. It has
no order capability.

## Data-health meanings

- `READY`: all required caches are current; NIFTY and every ranked instrument
  have trusted, fresh data and all required indicators are warmed up. Expected
  current intraday ineligibility alone does not change this state.
- `DEGRADED`: one or more eligible instruments are stale/untrusted or
  indicator-incomplete, or an authoritative equity mapping is missing or
  ambiguous. Any shown ranking is partial and must not be treated as executable.
- `NOT_READY`: a required cache/universe/benchmark is unavailable or stale, or
  the scanner cannot form a trustworthy market view.
- `UNKNOWN`: the market-regime value when NIFTY is stale/untrusted or its
  non-volume session anchor, EMA, or slope history is not ready. `UNKNOWN`
  prevents scanner readiness.

JSON diagnostics are split deterministically into expected eligibility
exclusions, reference failures, unhealthy market data, indicator NOT_READY,
benchmark failures, and scaling warnings. Counts distinguish requested,
eligible, expected-ineligible, broken mappings, indicator failures, and
benchmark failures.

Ticks become stale after `MARKET_DATA_STALE_AFTER_SECONDS` (default 5). An exact
same-token sequence/timestamp duplicate is ignored, while an older exchange
timestamp cannot mutate candles. SmartAPI `sequence_number` is telemetry, not a
per-token contiguous packet counter: one connection multiplexes instruments, so
positive jumps do not imply packet loss, and a lower sequence alone does not
prove reordering. Positive jumps are counted only as `sequence_jump_count`
telemetry and never trigger recovery. There is therefore no configurable
allowed-gap threshold. Trust instead depends on exchange timestamp freshness
and ordering, reconnect epochs and recovery, ingestion/queue/writer integrity,
and malformed-data rejection. Reconnects begin a new feed epoch, reset sequence
telemetry, and require three consecutive valid fresh ticks by default before
returning to trusted state. A candle touched by untrusted ticks remains
untrusted.

When recording starts after 09:15, replay never invents missing one-minute bars.
A trusted tick timestamped from 09:19:58 through 09:19:59.999 can prove the
completed 09:15-09:20 five-minute aggregate from Angel's maintained session
open/high/low, LTP, and cumulative equity volume. That aggregate becomes visible
only at or after 09:20 and carries `ANGEL_SESSION_FIELDS` provenance. A tick at
or after 09:20, or an older snapshot outside the strict two-second tolerance,
cannot recover it. A trusted 09:29:58-09:29:59.999 snapshot may supply the
session open and opening-range high/low only at or after 09:30. Complete local
one-minute data beginning at 09:15 always takes precedence.

For a late-start equity session, the latest trusted positive Angel ATP is the
VWAP reference and the latest trusted cumulative session volume is the RVOL
numerator. Recovered stock and NIFTY session opens anchor relative strength.
NIFTY ATP/volume remain ignored. Any missing, malformed, stale, out-of-order, or
post-cutoff proof keeps the affected indicator not ready; replay checks both
exchange and local receive timestamps so future knowledge cannot leak into an
as-of scan.

## Candle and indicator definitions

- One/five-minute candles align from the NSE cash open at 09:15 IST, not process
  start. Only completed candles are persisted; strategy-style confirmation uses
  completed five-minute candles.
- Incremental persistence completes a candle only after a later exchange-time
  bucket is observed for that same instrument. Read views may include a
  non-persisted, wall-clock-complete working candle at the committed as-of time;
  later ticks can revise it until exchange-time progression makes it durable.
- Candle volume is the non-negative delta of Angel's cumulative session volume.
  The first observation and a cumulative reset contribute zero, preventing
  fabricated volume.
- With complete local data, session VWAP uses completed/trusted candles and
  typical price `(high + low + close) / 3`, weighted by incremental candle
  volume. A late-start equity may instead use only a latest trusted positive
  Angel ATP session reference.
- NIFTY regime uses a separate session TWAP: the arithmetic mean of completed,
  trusted five-minute typical prices. NIFTY volume may remain zero and is never
  fabricated or passed to stock VWAP/RVOL logic.
- EMA 9/20 is seeded with the first period's simple mean.
- ATR 14 is the simple average of 14 true ranges and requires 15 candles so each
  range has a prior close.
- RVOL compares cumulative volume at the same session minute against up to 15
  prior complete/trusted local sessions and requires 10 by default.
- Relative strength is stock session return minus NIFTY session return, in
  percentage points. Its opens must come from complete local sessions or
  validated Angel session references; a midday LTP is never treated as open.
- VWAP distance is reported in percentage and ATR units.
- Opening range is the trusted high/low over a complete, contiguous first 15
  local session minutes by default, or a causal trusted pre-09:30 Angel session
  snapshot when local recording began late.
- VWAP, opening range, RVOL and relative strength use only today's session.
  EMA9/EMA20, ATR14 and benchmark EMA warm-up may use prior complete/trusted
  five-minute sessions plus today's completed candles. Sorting across sessions
  preserves the previous close and therefore the overnight ATR gap.
- Regime is `TREND_UP`, `TREND_DOWN`, `RANGE`, or `UNKNOWN`, using NIFTY relative
  to its session TWAP, rolling five-minute EMA 9/20, and short-horizon slope. No
  breadth or index volume is invented.

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

## Signal qualification

Qualification is separate from ranking. A high-scoring candidate remains in its
ranked LONG/SHORT list when one or more strict gates fail.

A LONG qualification requires all of:

- instrument market data is `READY`;
- regime is `TREND_UP`;
- LTP is strictly above VWAP;
- EMA9 is strictly above EMA20 on completed five-minute data;
- RVOL is at least `RELATIVE_VOLUME_THRESHOLD` (default `1.5`);
- relative strength versus NIFTY is strictly positive;
- a trusted, completed current-session five-minute candle ending after the
  opening-range boundary closes strictly above opening-range high;
- absolute VWAP distance is at most `MAX_VWAP_DISTANCE_ATR` (default `1.5`);
- every required indicator/reference is available.

A SHORT qualification uses the exact directional inverse: `TREND_DOWN`, LTP
strictly below VWAP, EMA9 strictly below EMA20, negative relative strength, and
a trusted completed five-minute close strictly below opening-range low. The same
RVOL and absolute VWAP/ATR-extension limits apply. Equality passes the RVOL and
extension limits but fails price, EMA, relative-strength, and breakout direction
checks.

Opening-range proximity remains only a ranking component. Tick touches,
intrabar high/low spikes, partial candles, untrusted candles, candles from prior
sessions, and candles whose `end_at` is after the replay cutoff never confirm a
breakout. With the default 15-minute range, the 09:25-09:30 candle does not
qualify because it finishes at—not after—the 09:30 boundary; the first possible
confirmation is the completed 09:30-09:35 candle.

Qualification reasons are emitted in stable gate order, including
`market_regime_not_trend_up`, `market_regime_not_trend_down`,
`price_not_above_vwap`, `price_not_below_vwap`,
`ema9_not_above_ema20`, `ema9_not_below_ema20`, `rvol_below_threshold`,
`relative_strength_not_positive`, `relative_strength_not_negative`,
`opening_breakout_not_confirmed`, `opening_breakdown_not_confirmed`, and
`vwap_atr_extension_exceeded`. Sector remains `None`; sector confirmation is a
future enhancement until authoritative sector/index mapping is implemented.

## Paper risk and execution foundation

Initialize and inspect a paper session with explicit, local-only commands:

```powershell
python -m trading_bot paper-init
python -m trading_bot paper-status
python -m trading_bot paper-status --json
python -m trading_bot paper-report --from-date 2026-10-01 --to-date 2026-10-31
python -m trading_bot paper-report --from-date 2026-10-01 --to-date 2026-10-31 --json
```

To test the operator kill switch, provide an auditable reason:

```powershell
python -m trading_bot paper-kill-switch --reason "operator requested stop"
python -m trading_bot paper-status
```

For a historical test database session, `paper-init`, `paper-status`, and
`paper-kill-switch` accept `--date YYYY-MM-DD`; `paper-report` accepts an
inclusive `--from-date`/`--to-date` range. `paper-kill-switch` cancels pending
paper entries and queues local exit simulations for open paper positions; it
never transmits a broker order.

The risk engine uses the configured starting equity rather than available
leverage:

```text
risk_equity = min(starting_equity, current_equity)
risk_budget = risk_equity * risk_per_trade_pct
risk_per_share = abs(planned_entry - protective_stop)
qty_by_risk = floor(risk_budget / risk_per_share)
final_qty = min(risk_qty, cash/capital_qty, verified_liquidity_qty)
```

Profits never increase the session's sizing base, while realized/unrealized
losses reduce it. The final quantity is rounded down to the instrument lot
size. Full notional is reserved for both long and short paper positions, so
broker leverage can never increase planned rupee risk. New entries fail closed
on an invalid/zero stop,
unqualified signal, untrusted market data, stale marks on existing positions,
unverified funds or liquidity, entry-window violation, daily-loss limit,
position/trade limits, consecutive-loss limit, kill switch, or uncertain state.
Only one trade per instrument per session is allowed, preventing averaging down
and same-symbol revenge re-entry.

`RelativeStrengthOpeningBreakoutV1IntentPolicy` is the only implemented bridge
from a qualified scanner candidate to a `PaperTradeIntent`. It does not submit
the intent. It rechecks `READY`/fresh market data, the 09:30-14:45 window, exact
cached instrument identity, normal NSE EQ status, the current intraday-allowed
snapshot, a positive externally verified liquidity cap, and a completed trusted
five-minute confirmation candle. LONG entry requires price to remain above the
opening-range high; SHORT requires price to remain below the low. The initial
stop uses `OPENING_RANGE_FAILURE_ATR_BUFFER_V1`: the failed-breakout boundary
plus a configurable ATR buffer, rounded away from entry to the broker tick,
with a minimum tick distance and maximum ATR distance. The first confirming
candle anchors an idempotent client tag, preventing repeated scans from creating
new signal identities.

Paper fills use `NEXT_TRUSTED_TICK_ADVERSE_SLIPPAGE_V1`:

- an order cannot fill on the tick/time that created it;
- only a later `READY`, fresh exchange/receive timestamp tick may fill it;
- missing or zero last-traded quantity produces no fill;
- fill size is capped by `last_traded_quantity *
  PAPER_TICK_PARTICIPATION_PCT` and the remaining order quantity;
- buy prices round up and sell prices round down after configured adverse
  slippage;
- if that simulated entry price would exceed the original per-share risk or
  capital plan, the unfilled entry quantity is cancelled instead of increasing
  risk;
- a deterministic tick/order fill identity prevents a replay or restart from
  duplicating a fill.

Every accepted entry has a persisted hard stop. The simulator can take the
configured partial at +1R, the second partial at +1.5R, accept only
risk-tightening stop updates, and queue a time exit at 15:15 IST by default.
Triggers create an exit that can fill only from a later trusted tick, making gap
and latency risk visible instead of granting an optimistic trigger-price fill.
If data becomes unsafe while exposure exists, the day becomes
`STATE_UNCERTAIN`; pending entries are cancelled, new entries stop, and no exit
fill is invented. Uncertainty can be cleared only when the local paper state is
flat.

SQLite persists the paper day, orders, fills, positions, marks, counters, kill
switch, uncertainty flag, and audit events atomically. Restarting reopens the
same pending/partial state rather than resetting limits or fabricating fills.
`paper-status` retains the persisted simulator's gross P&L. `paper-report`
adds a deterministic multi-session research report with closed-trade win rate,
expectancy, net R, profit factor, closed-trade drawdown, rejected entries,
unfinished exposure, and gross-versus-estimated-net P&L. Its configurable cost
schedule estimates Angel One brokerage per executed order and NSE cash-intraday
exchange charges, sell-side STT, IPFT, SEBI turnover fees, buy-side stamp duty,
and GST. Defaults reflect the official [Angel One transaction-charge
table](https://www.angelone.in/exchange-transaction-charges) and [NSE statutory
levy table](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies)
reviewed on 2026-10-21. Output is explicitly
`ESTIMATE_ONLY_NOT_A_CONTRACT_NOTE`: promotions, taxes, and broker/exchange
rounding can change, so compare it with actual contract notes and update the
`PAPER_*_RATE` settings before relying on results. Historical reports use the
currently configured schedule and include its date/rates/source URLs; the rate
snapshot is not yet frozen per session.

The policy remains a library surface and is intentionally not wired to a
continuous scanner loop. This prevents a qualified analytical row from silently
becoming even a simulated order before replay validation and operator review.

## Causal PAPER replay

`record-scanner-market` now freezes the validated instrument-master,
intraday-allowed, and NIFTY 500 source hashes plus the exact eligible instrument
and benchmark identities before recording starts. The first snapshot for an IST
session is immutable. An identical recorder restart reuses it; a changed cache,
configured universe, mapping, or eligibility set fails closed rather than
mixing reference regimes within one session.

Use a small explicit universe before recording and do not change it during the
session:

```dotenv
SCANNER_SYMBOLS=SBIN,RELIANCE,INFY,TCS,HDFCBANK
PAPER_REPLAY_MAX_INSTRUMENTS=20
PAPER_REPLAY_MAX_TICKS=250000
PAPER_REPLAY_LIQUIDITY_PARTICIPATION_PCT=0.10
```

After the recorded cash session has completed, run the read-only harness:

```powershell
python -m trading_bot paper-replay --date 2026-10-20
python -m trading_bot paper-replay --date 2026-10-20 --json
```

The command requires the reference snapshot captured before that session and
fails `NOT_READY` for older databases that do not have one. It never substitutes
today's eligibility for an earlier date. It evaluates only at five-minute
boundaries, rebuilds scanner state using ticks visible at each boundary, and
uses completed trusted candles. Liquidity is bounded by the configured fraction
of the latest completed five-minute recorded volume. Each accepted signal is
submitted to separate in-memory paper engines; the source ticks, candles, and
persisted paper tables are not changed.

The default scenarios are zero slippage, configured base slippage, double
slippage, and double estimated transaction costs. Protective stops still fill
only on a later recorded tick, so actual recorded stop gaps remain visible and
are counted separately. An interrupted feed while exposure exists marks the
isolated scenario uncertain. `INCOMPLETE` results, open positions, active
orders, uncertainty, a missing reference snapshot, excessive universe size, or
the replay tick safety limit all produce a non-zero exit. These scenarios test
robustness; they are not evidence of future returns.

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

## Current boundary and next correctness gate

Normal as-of scans load prior historical candles plus one read-only current
projection snapshot; they do not count or replay the full raw-tick day. The raw
replay hard limit and timing fields remain for explicit
`replay`/projection-comparison diagnostics and backward-compatible output.
Phase 2.5.4 and the paper engine remain local SQLite architecture: large
universes need measured paper-session validation, projection-lag monitoring,
and restart/reconnect testing before any future automation.

The next gate is not live trading. It is to collect several new sessions with
their immutable reference snapshots, run the replay matrix, and review rejected
signals, uncertainty events, recorded gaps, win rate, expectancy, and drawdown.
Only after those results are credible should a separate, reviewed paper-session
orchestrator connect trusted projection updates to the persisted simulator. It
still needs frozen per-session cost snapshots, automatic trailing-stop policy,
sector confirmation, and measured forward paper runs across multiple market
regimes. No backtest or paper result can prove future accuracy.

Live order placement, positions, order reconciliation, and any Angel order
adapter remain absent. They must stay absent until the full acceptance gate in
`AGENTS.md` is met and the user explicitly chooses to begin a separately
reviewed live-order phase.

See `AGENTS.md` for the full engineering and real-money safety contract.
