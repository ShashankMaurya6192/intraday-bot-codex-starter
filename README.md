# Intraday Bot

Local-first, paper-first Angel One SmartAPI market-data recorder and deterministic
market-intelligence scanner for Indian equities.

Phase 2.5.3 materializes current-session health, candles, and recovery
references incrementally while `record-scanner-market` runs. That recorder is
the single normal projection writer. Normal scans read one transactionally
consistent SQLite snapshot and never advance or refresh the checkpoint.
Durable candle completion follows same-instrument exchange-time progression;
wall-clock-complete working buckets are read-only ephemeral views until that
progression makes them stable.
Phase 2.4 ranking and qualification semantics remain unchanged, and scanner
results and qualified signals remain analysis only.
There is no paper execution, order interface, or live order-placement code.

## Safety status

- `TRADING_MODE=PAPER` remains the default and only practical mode.
- No command confirms or enables LIVE mode.
- `.env`, SQLite databases, and `data/cache/` are gitignored.
- Credentials use secret settings and are never written to reference caches.
- The Angel adapter can authenticate, read live/historical market data, and read
  the official intraday-allowed list. It cannot place, modify, cancel, or query
  orders.
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

## Phase 2.5.3 architecture

```text
explicit refresh commands
  -> validated atomic Angel/NIFTY caches

explicit SmartAPI historical diagnostic/bootstrap
  -> distinct throttle/rejection/transport failure categories
  -> default global abort, or explicit PAPER-only yfinance continuation
  -> validated complete 1m sessions
  -> provider/provenance status + local 5m/RVOL derivation

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
- `trading_bot.storage`: raw ticks/feed events, projected health/references,
  projection checkpoints, candles, and RVOL baselines in SQLite.

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

Normal as-of scans load prior historical candles plus one read-only current
projection snapshot; they do not count or replay the full raw-tick day. The raw
replay hard limit and timing fields remain for explicit
`replay`/projection-comparison diagnostics and backward-compatible output.
Phase 2.5.3 is still local SQLite architecture: large
universes need measured paper-session validation, projection-lag monitoring,
and restart/reconnect testing before any future automation.

Phase 3 may add deterministic paper execution, fill simulation assumptions,
position/risk state, protective exits, restart recovery, and kill-switch tests.
It must not enable live orders. Phase 2 scanner output first needs validation
across multiple recorded/replayed sessions, including reconnects, timestamp
regressions, and cache refresh operations.

See `AGENTS.md` for the full engineering and real-money safety contract.
