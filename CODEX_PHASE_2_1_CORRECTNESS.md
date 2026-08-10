# Codex Task: Phase 2.1 Correctness and Usability Gate

Read `AGENTS.md`, `README.md`, `CODEX_PHASE_2_MARKET_INTELLIGENCE.md`, and the current Phase 2 implementation completely before making changes.

Work from commit `c5f15b87bef756c03dff359de4d4e06f739fd3d4`.

This is a correctness and operability gate before paper execution. Do not add broker order placement, modify/cancel orders, live execution, paper fills, or position sizing in this task.

## Why this gate exists

The Phase 2 architecture is useful, but several issues prevent trustworthy real-session scanning:

1. Replay currently flushes today's working candles to 15:30 even when `evaluation_at` is earlier. That can mark a partial current candle as completed and lets the scanner consume it.
2. `evaluation_at` is not a true as-of boundary. Future ticks can still be processed if they are present in the replay input.
3. NIFTY 50 is an index and Angel provides no meaningful traded volume for the index. The current regime classifier calls volume-weighted VWAP for NIFTY, so real NIFTY data can remain `UNKNOWN`.
4. `record-market` still requires manually listing every scanner token. A NIFTY 500 scanner must derive validated subscriptions from the cached universe.
5. The scanner requires prior-session RVOL baselines and rolling EMA/ATR warm-up, but there is no practical historical bootstrap. With session-only five-minute history, EMA20 is not ready until roughly 100 minutes after market open.
6. Expected broker eligibility exclusions should not automatically mean the whole scanner is degraded.

Fix these before Phase 3.

## 1. Make replay strictly causal

Change `MarketReplayEngine` so `evaluation_at` is a real as-of boundary.

Requirements:
- When `evaluation_at` is supplied, discard/ignore every tick whose `received_at` is after `evaluation_at`.
- Also ignore feed-epoch events after `evaluation_at`.
- Never use a future tick to close or mutate a candle visible at the evaluation time.
- Flush working candles only up to `min(evaluation_at, session_close)` when `evaluation_at` is supplied.
- A candle whose `end_at` is after `evaluation_at` must remain incomplete and must not be emitted as a completed candle.
- A full historical replay with no `evaluation_at` may flush through the normal 15:30 session close.
- Preserve deterministic ordering and idempotent persistence.

Add tests including:
- evaluation at 09:17 leaves the 09:15-09:20 five-minute candle incomplete;
- evaluation at 09:19:59 still does not emit that five-minute candle;
- evaluation at 09:20:00 may emit the completed 09:15-09:20 candle;
- a tick received at 09:21 is invisible to a 09:20 evaluation;
- a historical full-day replay still completes all eligible session candles;
- repeated as-of replay is deterministic.

## 2. Remove volume-weighted VWAP from NIFTY regime logic

Do not fabricate index volume and do not give NIFTY a fake volume.

Keep stock `session_vwap()` exactly as a stock indicator.

Add a separate non-volume benchmark anchor for indices, for example:
- `session_twap()` using the average typical price of completed trusted benchmark candles, or
- another clearly documented deterministic non-volume session price anchor.

Use that benchmark anchor for NIFTY regime classification.

Requirements:
- NIFTY 50 volume may be zero for every candle and regime classification must still work.
- Regime may use NIFTY price vs benchmark session TWAP, EMA9/EMA20 and short-horizon slope.
- Keep `UNKNOWN` when benchmark data is stale/untrusted or required history is absent.
- Do not call the new index anchor "VWAP".
- Update README and all field/diagnostic names accordingly.
- Add tests with NIFTY token `99926000`, `instrument_type=AMXIDX`, and zero volume.
- Tests must show deterministic TREND_UP, TREND_DOWN, RANGE and UNKNOWN results with zero-volume index candles.

## 3. Separate session indicators from rolling indicators

The scanner should be useful soon after the opening range instead of waiting until today's session alone has 20 five-minute candles.

Session-only indicators:
- stock session VWAP;
- opening range;
- session relative return/relative strength;
- current-session RVOL.

Rolling indicators:
- EMA9;
- EMA20;
- ATR14;
- any benchmark EMA warm-up used by regime classification.

Requirements:
- Rolling EMA/ATR may use completed trusted five-minute candles from prior trading sessions plus today's completed candles.
- Never use future candles.
- Preserve overnight gaps correctly for ATR by allowing previous-session close as the previous close.
- Current-session VWAP/opening range/RVOL must never use prior-session values.
- Scanner must explicitly verify that today's session data begins at 09:15 before using "session open" for relative strength. If the first trusted session candle is missing, return NOT_READY for that component rather than treating a midday first observation as the open.
- Add tests proving the scanner can have EMA20 and ATR14 ready shortly after 09:30 when valid prior-session warm-up exists.

## 4. Add explicit historical bootstrap for research baselines

Implement an explicit maintenance command such as:

`python -m trading_bot bootstrap-history`

This command may use Angel One's Historical API. It must never run automatically on `record-market` or `scan`.

The historical API endpoint is:
`POST https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData`

Use one-minute data as the canonical bootstrap input where practical, then deterministically derive five-minute candles locally.

Requirements:
- Use current authenticated SmartAPI session.
- Resolve all tokens from the validated local instrument cache. Never guess.
- Support `SCANNER_SYMBOLS` for an initial 5-20-symbol smoke test.
- Include the NIFTY 50 benchmark separately even though it is not an equity and not part of the intraday-allowed equity universe.
- Fetch a configurable lookback, default enough to obtain at least 15 complete prior trading sessions.
- Throttle conservatively. Do not run requests at the documented maximum rate.
- Use bounded retries with exponential backoff for transient broker errors.
- Persist the raw or normalized historical candles in a deterministic local historical-candle store.
- Build RVOL baselines only from complete trusted equity sessions.
- Do not build RVOL baselines for NIFTY index because index volume is not meaningful.
- Persist prior five-minute candles needed for EMA/ATR/regime warm-up.
- Do not overwrite a previously valid historical dataset with a malformed partial response.
- Make the operation restartable/idempotent so a failure after 200 symbols does not require downloading the first 200 again unless explicitly requested.
- Add a `--force` option for intentional refresh.
- Add status/summary output with requested symbols, completed symbols, failed symbols, sessions found, and cache age. Never print credentials.

Tests must be synthetic and offline.

## 5. Add automatic scanner-universe market recording

Manual `ANGEL_MARKET_SUBSCRIPTIONS` is fine for diagnostic recording, but not for the intended scanner.

Add a separate command, for example:

`python -m trading_bot record-scanner-market`

Requirements:
- Load fresh instrument master, NIFTY 500 cache and same-day Angel intraday-allowed cache.
- Resolve the configured `SCANNER_SYMBOLS` override if present, otherwise resolve the full cached NIFTY 500 intersection.
- Convert the resolved instruments into validated `MarketSubscription` objects.
- Add the authoritative NIFTY 50 benchmark subscription separately.
- NIFTY benchmark does not need to appear in the intraday-allowed equity list.
- Deduplicate subscriptions.
- Enforce the existing 1000-token limit before authentication/connection.
- Print counts for requested, eligible, expected-ineligible, mapping failures, benchmark and final subscriptions.
- Reuse the existing verified-TLS WebSocket and bounded ingestion service.
- Do not place orders.
- Do not download reference data on this hot path.
- Fail closed when caches are stale/missing or benchmark mapping is unavailable.

Keep existing `record-market` for single-symbol/debug use.

## 6. Fix scanner readiness semantics

Do not mark the whole scanner DEGRADED merely because Angel currently does not allow intraday on some otherwise valid NIFTY 500 members.

Classify diagnostics into at least:
- expected eligibility exclusions, such as `not_currently_intraday_allowed`;
- mapping/reference failures;
- unhealthy/stale market data;
- indicator NOT_READY;
- benchmark/regime failures.

Requirements:
- Expected current intraday ineligibility reduces eligible universe count but does not by itself make the scanner DEGRADED.
- Missing/ambiguous authoritative mappings should be visible as reference-data failures and may degrade readiness.
- Stale/untrusted subscribed instruments should degrade readiness.
- Missing/stale NIFTY benchmark or UNKNOWN regime should make scanner NOT_READY.
- Output explicit counts so a user can distinguish "420 eligible, 80 expected-ineligible" from "80 broken mappings".
- Preserve deterministic ordering and JSON output.

## 7. Harden intraday-list parsing

The current Angel sample uses a lowercase `exchange` key while code currently checks `Exchange` with a default.

Requirements:
- Support the documented response casing deliberately.
- Validate the exchange value when present.
- Do not silently replace an invalid/missing exchange value with NSE.
- Accept only NSE rows for this cache.
- Add tests for lowercase `exchange`, uppercase compatibility if needed, malformed exchange, duplicate symbols, invalid multiplier and missing fields.

## 8. Prefer the current authoritative NIFTY 500 source

The NSE NIFTY 500 product page currently links the constituent CSV from the NSE archives host.

Use an authoritative NSE/NSE Indices source and document the source. It is okay to support a fallback between official hosts, but:
- no unofficial mirrors;
- HTTPS only;
- full schema validation;
- atomic last-known-good replacement remains mandatory.

## 9. Do not solve scaling by replaying unbounded data forever

For this task, keep the implementation simple, but add explicit guardrails:
- `scan` must report the number of raw ticks replayed and elapsed replay time;
- add configurable warning thresholds for replay tick count/runtime;
- if replay size exceeds a hard configured safety bound, return NOT_READY with a diagnostic instead of hanging or consuming unbounded memory;
- document that an incremental live intelligence worker/persisted health snapshot is the next scaling step before live automation.

Do not build live order execution.

## 10. Regression tests

In addition to the tests above, make sure the suite covers:
- zero-volume NIFTY benchmark;
- no partial current five-minute candle used by scanner;
- no look-ahead at `evaluation_at`;
- session-open requirement for relative strength;
- rolling prior-session EMA/ATR warm-up;
- explicit historical bootstrap idempotency;
- automatic scanner subscription generation;
- benchmark included exactly once;
- expected intraday exclusions do not degrade scanner by themselves;
- malformed reference data fails closed;
- all Phase 1.5 TLS/reconnect/queue tests continue passing.

## Acceptance gate

Before finishing:
- `python -m ruff check .` passes.
- Complete pytest suite passes.
- Existing Windows and Ubuntu CI configuration remains intact.
- `TRADING_MODE=PAPER`.
- `LIVE_TRADING_ENABLED=false`.
- No broker order placement method or CLI command exists.
- No secrets are committed.
- NIFTY zero-volume real-world behavior is represented in tests.
- An as-of scan can never consume a partial current candle as completed.
- A 5-20-symbol scanner smoke test can be configured without manually entering token IDs.
- Historical bootstrap can make EMA/ATR and RVOL baselines available from prior sessions.

Commit and push with a clear message.

At the end report:
1. files changed;
2. test count/result;
3. Ruff result;
4. replay-as-of fix;
5. zero-volume NIFTY regime design;
6. historical bootstrap behavior;
7. automatic scanner recording behavior;
8. new readiness/count semantics;
9. known remaining scaling limitations before Phase 3.

Do not proceed to Phase 3 in this task.
