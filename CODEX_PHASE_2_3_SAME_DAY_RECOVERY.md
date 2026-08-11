# Codex Task: Phase 2.3 Same-Day Scanner Recovery and Historical Fallback

Work from commit `811073aac085eb949e6e9f7159747be5142c99e1`.

Read AGENTS.md, README.md, and all Phase 2 task files before editing. Do not implement Phase 3, fills, positions, risk execution, or any order path. PAPER mode only.

## Goal

Recover today's late-start 09:17:36 scanner session safely from broker-maintained SmartAPI session fields, without fabricating missing 1m bars, and add an explicitly configured PAPER-only historical fallback provider so EMA/ATR/RVOL can warm up even when Angel Historical API is throttled.

## Live evidence to preserve in tests

A trusted tick immediately before 09:20 contained broker session fields for each instrument. Example SBIN-EQ at 09:19:59: open=1071, high=1071, low=1060.4, LTP=1064.3, ATP=1064.71, cumulative_volume=613366. NIFTY 50 at 09:19:59: open=24575.1, high=24576.85, low=24500.85, LTP=24503.95, ATP=0, volume=0.

At 09:29:58/59 the same fields provide the complete session-open through pre-09:30 high/low reference for the opening range.

## 1. Never fake missing 1m candles

Do not synthesize 09:15-09:17 one-minute candles. Unknown intraminute paths remain unknown. Any recovery must use only values actually proven by trusted persisted SmartAPI ticks at deterministic cutoffs.

## 2. Session-reference model

Add a broker-neutral session-reference model derived from persisted/replayed trusted ticks with: exchange, token, symbol, session date, observed timestamp, session open, session high-so-far, session low-so-far, LTP, ATP when meaningful, cumulative volume when meaningful, provenance=ANGEL_SESSION_FIELDS, trusted flag.

Use only ticks with received_at and exchange_timestamp <= evaluation cutoff. Validate positive/finite OHLC and consistency.

## 3. Recover first 5m aggregate when provable

For a late-start process, if there is a trusted tick immediately before 09:20 (define strict tolerance, e.g. <=2 seconds), derive only the 09:15-09:20 FIVE_MINUTES candle:
- open = broker session/day open
- high = broker day high as of that pre-09:20 tick
- low = broker day low as of that pre-09:20 tick
- close = LTP
- volume = cumulative day volume for normal equities, 0 for NIFTY/index
- completed=true
- trusted=true only if all proof checks pass
- explicit provenance showing broker-session-snapshot aggregate

Never use a snapshot observed at/after 09:20 to reconstruct this candle. Never fabricate the missing 1m bars.

## 4. Recover 09:15-09:30 opening range directly

When local 1m data does not start at 09:15, allow a trusted pre-09:30 SmartAPI session snapshot within strict tolerance to provide session_open, opening_range_high and opening_range_low after 09:30. Prefer complete local 1m data when available. Do not create fake 1m bars to satisfy session-open checks.

## 5. Relative-strength session-open anchors

Use the authoritative recovered session open for both stock and NIFTY when local 1m data began late. If neither a complete local session nor a trusted broker session-open reference exists, fail closed.

## 6. Late-start VWAP correctness

For normal equities, when recording began after 09:15, do not pretend post-start local volume forms a full-session VWAP. Allow the latest trusted positive SmartAPI average_traded_price to be the current session VWAP anchor. With a complete local session, retain the deterministic local VWAP path. NIFTY ATP=0 is not meaningful and must never be used as VWAP.

## 7. Late-start RVOL numerator

For normal equities, use the latest trusted SmartAPI volume_trade_for_the_day as the current cumulative-volume numerator. This avoids undercounting because the recorder started late. Never use NIFTY volume for RVOL. Historical baselines must remain like-for-like cumulative volume at the same time offset.

## 8. PAPER-only historical fallback abstraction

Preserve Angel as primary. Add explicit configuration/CLI for a yfinance research fallback, for example HISTORICAL_PROVIDER=angel|yfinance|auto and HISTORICAL_FALLBACK_ENABLED=false by default. In auto PAPER mode, if Angel raises BrokerHistoricalRateLimitError, make no further Angel historical requests for that run. If fallback is explicitly enabled, continue through yfinance. Never silently switch providers in any future live mode.

## 9. yfinance fallback rules

Map normal NSE `FOO-EQ` -> `FOO.NS`; NIFTY 50 -> `^NSEI`. Fetch `interval=1m`, `auto_adjust=False`, `prepost=False`, with deterministic small chunks (max 7 calendar days per request). Normalize to Asia/Kolkata. Validate expected columns, finite positive OHLC, integer nonnegative volume, minute alignment, regular NSE hours, duplicates, OHLC consistency and no current incomplete session.

Discard individual partial sessions. Accept only complete 375-minute sessions and require the configured minimum number before replacing history. Locally derive 5m candles from canonical 1m history exactly as the Angel path does. Build RVOL baselines only for equities. Persist provider/provenance in historical bootstrap status with a safe schema migration if needed.

## 10. Safe fallback diagnostic

Add e.g. `python -m trading_bot test-history-provider --provider yfinance --symbol SBIN --lookback-days 30` plus `--json`. Print provider symbol, chunks, rows, complete sessions, discarded partial sessions, first/last timestamps, validation status and order_placement_available=false.

## 11. Causality

A 09:20 aggregate is usable only after 09:20. A 09:30 opening-range reference is usable only after 09:30. Historical warm-up uses sessions strictly before today. Replays must reproduce the same result from persisted ticks with no look-ahead.

## 12. Tests

Cover at least: late start + 09:19:59 recovery; post-09:20 snapshot cannot recover; stale cutoff snapshot cannot recover; 09:29:59 opening-range recovery; no fake 1m bars; session-open RS anchors; ATP late-start VWAP; cumulative-volume RVOL numerator; NIFTY ignores zero ATP/volume; replay causality; generic ticker mapping; chunked fallback; timezone/session validation; partial-session discard; insufficient fallback history fails closed; malformed fallback never replaces valid history; Angel throttle stops further Angel calls before auto fallback; fallback opt-in; all existing tests remain passing.

## Acceptance

Run full pytest and Ruff. No order code. PAPER only. Scanner may become READY today only if all remaining dependencies are genuinely satisfied. Commit and push, then report SHA, files, tests, Ruff, recovery semantics, fallback semantics, provenance, remaining blockers, and confirmation no order execution was added.
