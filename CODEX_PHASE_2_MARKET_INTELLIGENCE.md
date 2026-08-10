# Codex Task: Phase 2 Market Intelligence and Scanner

Read `AGENTS.md`, `README.md`, and `CODEX_PHASE_1_5_HARDENING.md` completely. Inspect the current implementation and commit history before changing code.

Phase 1.5 established the paper-first broker feed, verified TLS, bounded reconnects, bounded asynchronous tick ingestion, SQLite recording, and fail-closed instrument boundaries. Phase 2 now builds the market-intelligence layer only.

Do not implement order placement, order modification/cancellation, live execution, position management, or paper fills in this phase.

## 1. Preserve Phase 1.5 safety

Keep all existing hardening intact:
- `TRADING_MODE=PAPER` remains the default and only practical mode.
- No order interface or broker order methods.
- No credentials in source, tests, logs, fixtures, or commits.
- No `ssl.CERT_NONE` and no weakening of TLS.
- No silent tick drops.
- Stale, incomplete, sequence-broken, or otherwise unreliable market data must suppress scanner eligibility.
- Do not bypass the 1000-token one-WebSocket limit.

## 2. Implement the concrete Angel instrument-master cache

Implement a concrete cache behind the existing `CachedInstrumentMaster` boundary.

Official source:
`https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json`

Requirements:
- Download only through an explicit refresh command, never as part of the live trading/recording startup path.
- Use bounded connect/read timeouts and bounded retries.
- Parse and validate the entire response before replacing the last-known-good cache.
- Use temp-file plus atomic replace semantics so a failed refresh never destroys the previous valid cache.
- Persist metadata including fetch timestamp, record count, SHA-256, source URL, and schema version.
- Validate at least token, exchange, trading symbol, tick size, lot size, and instrument type.
- Normalize Angel exchange names into existing domain `Exchange` values.
- For the equity scanner, accept only normal NSE cash equity records.
- Duplicate authoritative identities must fail validation rather than choosing one arbitrarily.
- Loading a corrupt cache must raise `InstrumentMasterError`.
- Missing tokens must never be guessed.
- Add `python -m trading_bot refresh-instruments` and a read-only status command showing cache age/count/hash without exposing credentials.

## 3. Implement current NSE intraday-allowed cache

Use Angel One's official authenticated endpoint:
`GET https://apiconnect.angelone.in/rest/secure/angelbroking/marketData/v1/nseIntraday`

Requirements:
- Refresh explicitly before/near the market session and persist a local snapshot with fetched timestamp.
- Store symbol plus multiplier as broker data, but never use multiplier to increase strategy risk.
- Fail closed if the snapshot is missing, malformed, or too stale for configured policy.
- Implement the existing `IntradayAllowedUniverse` interface.
- Scanner eligibility must be the intersection of the configured NSE equity universe, validated instrument master, and current Angel intraday-allowed snapshot.
- Do not infer eligibility from yesterday's list.

## 4. Add a configurable NIFTY 500 universe boundary

The intended initial scan universe is NIFTY 500.

Requirements:
- Add an explicit refresh/import command for a NIFTY 500 constituent CSV from an authoritative NSE/NSE Indices source.
- Cache it locally with timestamp/hash and last-known-good semantics, similar to instrument metadata.
- Do not fetch it on the hot/live scanner path.
- Map constituent symbols to Angel instruments through authoritative trading symbol/token identity.
- If mapping is ambiguous or missing, exclude the item and surface the reason in diagnostics rather than guessing.
- Make the scanner universe configurable so tests and early live smoke tests can use 5-20 symbols.

## 5. Tick health and sequence telemetry

Build a broker-neutral market-data health layer before candle generation.

For each instrument track:
- latest exchange timestamp;
- latest received timestamp;
- sequence number;
- data age;
- duplicate count;
- out-of-order count;
- positive sequence-jump count (telemetry only);
- last health transition.

Requirements:
- Use `MARKET_DATA_STALE_AFTER_SECONDS`.
- A stale instrument is ineligible for signals.
- Out-of-order ticks must not mutate completed candle state incorrectly.
- Do not infer packet loss from per-token sequence contiguity. Angel multiplexes
  instruments on one stream, so positive per-token jumps are expected and must
  not degrade health.
- Exact duplicates and older exchange timestamps remain fail-closed and must not
  mutate candle state. Sequence direction alone is not an ordering signal.
- Do not assume sequence numbers survive broker reconnects unchanged. Model reconnect/feed epochs explicitly if necessary.
- Expose health diagnostics for the CLI/scanner.
- Tests must cover stale ticks, exact duplicates, out-of-order ticks, sparse
  increasing sequences, interleaved tokens, reconnect epochs, and recovery.

## 6. Candle aggregation

Implement deterministic IST session candle aggregation for:
- 1-minute candles;
- 5-minute candles.

Candle model should include:
- exchange/token/symbol;
- interval;
- start/end timestamps;
- open/high/low/close;
- volume;
- tick count;
- completed flag;
- health/trust status or a clear linkage to market-data health.

Requirements:
- Session alignment starts from the NSE cash session boundary, not from process start time.
- Do not use future ticks to build past calculations.
- Emit/use only completed 5-minute candles for strategy-style confirmations.
- Handle zero/new-session cumulative volume safely.
- Persist completed candles in SQLite with idempotent keys.
- Add replay support so recorded ticks can deterministically reproduce the same candles.

## 7. Indicators

Implement broker-neutral, independently tested indicator functions/services for:
- session VWAP;
- EMA 9;
- EMA 20;
- ATR 14;
- relative volume;
- stock relative strength versus NIFTY benchmark;
- distance from VWAP in percentage and ATR units;
- opening-range high/low for configurable 15-minute default.

Rules:
- No look-ahead bias.
- Define warm-up requirements explicitly.
- Return `NOT_READY`/equivalent rather than fabricating values when history is insufficient.
- VWAP must use a defensible price-volume definition and document it.
- Relative volume must compare like-for-like time-of-day history, not today's partial volume against whole-day average volume.
- Persist or cache only what is needed for deterministic replay.

## 8. Historical baseline for relative volume

Relative volume requires prior sessions.

Implement a simple baseline store built from locally recorded/replayed daily sessions first.

Requirements:
- Support configurable lookback, initially 10-20 valid sessions.
- Compare cumulative volume at the same session minute.
- Require a minimum number of valid baseline sessions.
- Exclude incomplete/degraded sessions from the baseline.
- Do not call an external historical API on every scanner cycle.
- It is acceptable for RVOL to be `NOT_READY` until enough local/history data exists.

## 9. Market regime inputs

Implement deterministic inputs for:
- `TREND_UP`
- `TREND_DOWN`
- `RANGE`
- `UNKNOWN`

Start conservatively using NIFTY benchmark data:
- position vs session VWAP;
- 5-minute EMA 9/20 relationship;
- short-horizon slope/momentum;
- optional breadth only if trustworthy breadth data has been explicitly implemented.

Requirements:
- `UNKNOWN` when benchmark data is stale or indicators are not ready.
- Do not invent breadth from an incomplete subscription universe.
- Keep thresholds configurable.

## 10. Scanner and ranking, no trading signals yet

Build a scanner that evaluates only healthy, mapped, intraday-eligible universe members and produces ranked LONG-bias and SHORT-bias candidates.

Candidate fields should include:
- symbol/token;
- LTP;
- data age/health;
- VWAP and distance;
- EMA 9/20;
- ATR;
- RVOL;
- relative strength vs NIFTY;
- opening range high/low;
- distance to breakout/breakdown;
- sector placeholder if not yet authoritative;
- market regime;
- component scores;
- total score;
- rejection reason(s).

Ranking must be deterministic and explainable. Do not use an LLM for numeric scoring.

Initial scoring should reward confluence, not one indicator. Keep weights in configuration and test exact score calculations.

Do not generate broker orders, quantities, or executable entry instructions in Phase 2. This phase answers: "Which stocks deserve attention and why?"

## 11. CLI

Add useful commands such as:
- `refresh-instruments`
- `refresh-intraday-universe`
- `refresh-nifty500`
- `data-status`
- `replay --date YYYY-MM-DD`
- `scan`

`scan` should print a compact table of top candidates and rejected/unhealthy counts. Add JSON output mode so a future dashboard can consume it.

The scanner must refuse to claim `READY` if required caches/data are stale or incomplete.

## 12. Testing

Add strong unit/integration tests for:
- instrument cache atomic last-known-good behavior;
- malformed instrument rows;
- duplicate/ambiguous mapping;
- intraday eligibility freshness;
- NIFTY 500 mapping;
- tick health and reconnect epochs;
- 1m and 5m candle boundaries;
- cumulative-volume resets;
- replay determinism;
- VWAP;
- EMA 9/20;
- ATR 14;
- RVOL time-of-day baseline;
- relative strength;
- opening range;
- market regime `UNKNOWN` fail-closed behavior;
- exact deterministic scanner ranking;
- scanner refusal on stale/untrusted data.

Tests must use synthetic fixtures and require no Angel credentials or internet.

## 13. CI and documentation

Keep Windows + Ubuntu CI green.

Update README with:
- Phase 2 architecture;
- cache refresh workflow;
- replay workflow;
- scanner command examples;
- precise meaning of `READY`, `DEGRADED`, `NOT_READY`, and `UNKNOWN`;
- why scanner results are analysis only and not orders;
- what remains for Phase 3 paper execution.

## Acceptance gate

Before finishing:
- `python -m ruff check .` passes;
- complete pytest suite passes on local environment;
- CI remains configured for Windows and Ubuntu;
- no live order APIs exist;
- no live execution code exists;
- no secrets are committed;
- replay of the same tick fixture produces deterministic candle and scanner output;
- stale/untrusted data reliably suppresses readiness.

Commit and push with a clear message.

At the end report:
1. files changed;
2. test count and result;
3. Ruff result;
4. new CLI commands;
5. cache freshness rules;
6. candle/indicator definitions;
7. scanner score components;
8. any known blocker or assumption before Phase 3.
