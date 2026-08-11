# Codex Task: Phase 2.5 Incremental Scanner Projection

Work from commit:

`1899a3e73b1caf10527b7e83eca0c2841a72c2bd`

Read `AGENTS.md`, `README.md`, and all prior Phase 2 task files before editing.

Do not implement Phase 3, paper fills, positions, capital sizing, risk execution, or any Angel order path. Keep `TRADING_MODE=PAPER` and `LIVE_TRADING_ENABLED=false`.

## Why this patch exists

Phase 2.4 is now fully validated locally:

- 134 tests passed
- Ruff passed
- live scanner reached `readiness=READY`
- qualification gates correctly produced zero qualified signals in a RANGE market
- current scan replays the full current-day raw tick history every invocation

A real live scan replayed ~70,466 ticks and took ~12.1 seconds, crossing the configured 10-second replay warning threshold. This cost will continue growing intraday.

The scanner must not replay the whole trading day for every invocation before Phase 3 paper execution is built.

## Goal

Materialize the current-session market-intelligence state incrementally while `record-scanner-market` is running, persist that state in SQLite, and let `scan` read the materialized state instead of replaying all raw ticks.

Preserve raw ticks for audit/replay. Full deterministic replay remains available as a verification/debug path, but it is no longer the normal scanner hot path.

Target normal `scan` latency for the five-stock + NIFTY smoke universe: comfortably below the existing 10-second warning and preferably sub-second to low-single-digit seconds on the user's current laptop.

## 1. Add an incremental market-intelligence projection

Introduce a component such as `MarketIntelligenceProjection` that consumes accepted live ticks and feed-epoch events in received order.

It should reuse the existing authoritative semantics rather than reimplement them differently:

- `MarketDataHealthMonitor`
- `SessionCandleAggregator`
- `SessionReferenceAccumulator`
- Phase 2.2.1 feed-epoch/reconnect semantics
- Phase 2.3 session/opening recovery semantics

The projection must produce/persist:

- latest `InstrumentHealth` snapshot for each instrument
- trusted/latest `SessionReference`
- pre-09:20 recovery reference when available
- pre-09:30 opening-range reference when available
- completed trusted/untrusted 1m and 5m candles
- feed epoch / projection checkpoint metadata
- last processed raw tick identity/cursor

Do not fabricate missing ticks or candles.

## 2. Integrate projection into `record-scanner-market`

The recorder already owns the live SmartAPI stream and normalized tick ingestion.

Extend the scanner recorder path so every accepted persisted live tick is also processed by the incremental projection in the same received order.

Requirements:

- raw tick persistence remains authoritative audit storage
- projection failure must fail closed and surface clearly
- no silent loss between tick persistence and projection
- preserve bounded queues/backpressure semantics
- do not add any order-related callback
- reconnect/feed events must be delivered to the projection so health epoch semantics remain correct

If exact atomicity between raw tick persistence and projection tables is practical, use it. Otherwise persist a replayable checkpoint/cursor so projection can safely catch up from raw ticks after restart.

## 3. Persist projection state safely

Add SQLite tables/migration for current-session materialized state.

Suggested logical tables:

- `market_health_snapshot`
- `market_session_reference`
- `market_projection_checkpoint`

Existing `market_candles` may continue to store completed projected candles using existing provenance.

Persist enough information to recover safely after process restart without replaying the entire session.

At minimum checkpoint:

- session date
- last processed raw tick id
- last processed received_at
- feed epoch / last processed feed-event cursor as needed
- projection updated_at
- schema/version identifier if useful

Do not persist secrets or broker auth material.

## 4. Incremental catch-up after recorder restart

On `record-scanner-market` startup:

1. load projection checkpoint for the current IST session if valid;
2. load any raw ticks/feed events after the checkpoint;
3. deterministically catch up only the missing suffix;
4. resume live projection;
5. if checkpoint is missing/corrupt/inconsistent, rebuild safely from current-session raw ticks once, persist a new checkpoint, and continue.

Never trust a checkpoint that claims to be ahead of SQLite raw data.

Never skip feed epoch events that affect market-health semantics.

## 5. Scanner hot path

Change normal `python -m trading_bot scan` so it reads:

- persisted prior-session historical candles/RVOL baselines
- current-session projected completed candles
- current health snapshots
- current session references/opening-range references

It should not replay all raw ticks in the normal healthy path.

Add explicit diagnostics to make the path visible, e.g.:

- `market_state_source=INCREMENTAL_PROJECTION`
- `projection_age_seconds`
- `projection_last_tick_id`
- `incremental_ticks_caught_up`

If the projection is stale, missing, corrupt, behind, or inconsistent, fail closed or use a bounded catch-up path. Do not silently fall back to an expensive full-day replay without an explicit diagnostic.

Full replay remains available through the existing `replay` command and can be used for equivalence testing.

## 6. Projection freshness gate

Add a configurable maximum projection age, for example:

`SCANNER_PROJECTION_STALE_AFTER_SECONDS=5`

Normal scan must fail closed if the materialized state is older than the configured threshold while the market is expected to be live.

Use exchange/received timestamps and current IST time consistently.

Outside active market hours, freshness semantics may be relaxed only if clearly deterministic/documented.

## 7. Preserve Phase 2.3 late-start recovery

Today's recorder started at 09:17:36.

Incremental projection must still preserve the validated recovery semantics:

- no fake 09:15-09:17 one-minute bars
- pre-09:20 trusted broker session snapshot can recover the first 5m aggregate
- pre-09:30 trusted broker session snapshot can provide opening-range open/high/low after 09:30
- ATP can anchor late-start equity VWAP
- cumulative broker volume can anchor current RVOL numerator
- NIFTY zero ATP/volume behavior remains unchanged

Persist the recovered references/aggregate so `scan` does not need a full raw replay to rediscover them each invocation.

## 8. Preserve Phase 2.4 qualification exactly

No strategy changes in this phase.

The following must remain exactly enforced:

LONG:
- TREND_UP
- LTP > VWAP
- EMA9 > EMA20
- RVOL >= threshold
- relative strength > 0
- completed trusted post-opening-range 5m close above opening high
- abs VWAP distance <= configured ATR maximum

SHORT exact inverse.

Do not change scoring, thresholds, qualification reason strings, or signal semantics merely for performance.

## 9. Deterministic equivalence check

Add a safe command, for example:

`python -m trading_bot compare-projection-replay --date YYYY-MM-DD --json`

For a completed/current recorded session, compare materialized projection state against deterministic raw replay at the same as-of timestamp.

Compare at minimum:

- health status/trust
- latest exchange timestamp
- sequence telemetry relevant to trust
- completed candle identities/OHLCV/trust/provenance
- session open/reference
- opening range reference
- scanner readiness/regime
- candidate indicator values
- scores
- qualification booleans/reasons

Tiny timing-only fields such as wall-clock replay duration may differ.

Any semantic mismatch must return non-zero and print a bounded diagnostic.

No order capability.

## 10. Performance telemetry

Add scanner output fields such as:

- `state_source`
- `projection_age_seconds`
- `projection_load_seconds`
- `incremental_ticks_processed`
- `full_replay_used` (normally false)

Keep the existing `raw_ticks_replayed` field for backward compatibility if practical, but in the normal projection path it should be 0 or clearly indicate no full replay occurred.

Do not "fix" performance by simply increasing `scan_replay_warning_seconds`.

## 11. Tests

Add regression tests at minimum for:

1. initial projection from fresh live ticks
2. health snapshots persist and reload
3. completed 1m/5m candles persist incrementally
4. session references persist
5. pre-09:20 recovery persists without fake 1m bars
6. pre-09:30 opening-range recovery persists
7. reconnect feed epoch degrades/re-recovers health exactly as replay
8. projection checkpoint advances monotonically
9. restart catches up only suffix ticks
10. checkpoint ahead of raw DB fails closed
11. corrupt checkpoint rebuild/fail-closed behavior is deterministic
12. stale projection causes scan NOT_READY/DEGRADED as designed
13. normal scan does not load/replay all current-day raw ticks
14. projection scanner result equals deterministic replay result for same fixture/as-of
15. Phase 2.4 qualification booleans/reasons match between both paths
16. late-start VWAP/RVOL/reference semantics match
17. NIFTY zero-volume behavior matches
18. no full replay fallback occurs silently
19. performance telemetry fields present
20. all existing 134 tests remain passing
21. no order/paper execution code added

## 12. Real acceptance test

After implementation on the current SQLite database:

1. keep/restart `record-scanner-market` as required by the new projection code;
2. allow projection catch-up;
3. run `scan --json`;
4. verify scanner remains READY if data is fresh;
5. verify the same qualification semantics as Phase 2.4;
6. verify normal scan no longer replays ~70k+ raw ticks;
7. verify scan runtime is materially below the previous ~12.1 seconds;
8. run projection-vs-replay comparison and require semantic match.

## Acceptance gate

Before finishing:

- `python -m pytest -q` passes
- `python -m ruff check .` passes
- raw ticks remain stored for audit/replay
- normal scanner hot path uses incremental projection
- projection restart/catch-up is deterministic
- no look-ahead
- no fake candles
- Phase 2.3 recovery preserved
- Phase 2.4 qualification preserved
- no order code
- PAPER mode remains mandatory

Commit and push with a clear message.

At the end report:

1. commit SHA
2. files changed
3. test count/result
4. Ruff result
5. new projection tables/state
6. restart/catch-up semantics
7. scan hot-path behavior
8. projection-vs-replay equivalence result
9. before/after scanner runtime on the smoke universe
10. confirmation no paper/live execution code was added
