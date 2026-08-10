# Codex Task: Phase 2.2.1 SmartAPI Sequence Health Correction

Read `AGENTS.md`, `README.md`, all Phase 2/2.1/2.2 task files, and the current implementation before editing.

Work from current head:

`0d656b89e85e2f72005975b0bb8eeef10db37684`

Do not implement Phase 3, paper execution, position sizing, or any broker order path.

## Real live-feed evidence

A real six-symbol SmartAPI WebSocket run successfully subscribed to:

- HDFCBANK-EQ
- INFY-EQ
- NIFTY 50
- RELIANCE-EQ
- SBIN-EQ
- TCS-EQ

Thousands of fresh ticks were persisted for all six symbols, but `scan` marked every instrument `market_data_untrusted`.

The cause is the current `MarketDataHealthMonitor` assumption that `sequence_number` must advance contiguously by +1 per `(exchange, symbol_token)`.

That assumption is invalid for SmartAPI. Positive sequence-number jumps for successive received ticks are normal and must not be interpreted as missing packets for an individual token.

## 1. Remove sequence-contiguity as a trust condition

Inspect `src/trading_bot/marketdata/health.py`.

Current behavior:

```python
gap = tick.sequence_number - state.sequence - 1
if gap > allowed_sequence_gap:
    DEGRADED
```

Remove this trust rule.

Requirements:
- positive non-contiguous sequence jumps do not degrade or untrust data;
- do not infer lost packets from per-instrument SmartAPI sequence-number gaps;
- examples such as `100, 150, 892, 1200` for the same token remain trusted if timestamps/freshness are valid;
- interleaved multi-token feeds remain trusted;
- sequence number may remain available as telemetry;
- reconnect epochs still reset any sequence telemetry.

## 2. Do not use backward sequence alone as out-of-order proof

The current code also rejects a tick when:

`tick.sequence_number < state.sequence`

Do not use sequence-number monotonicity by itself as a market-data ordering rule.

Out-of-order trust decisions should use broker-neutral evidence such as:
- exchange timestamp moving backward;
- a clearly duplicated tick identity;
- stale exchange data;
- reconnect recovery state;
- malformed payload;
- ingestion/writer failure.

A lower sequence number with a non-decreasing valid exchange timestamp must not automatically be rejected.

## 3. Make duplicate detection conservative

Do not treat `same sequence_number` alone as sufficient proof of a duplicate.

Prefer a conservative identity such as the same instrument plus:
- same sequence number; and
- same exchange timestamp;

or another deterministic broker-neutral identity already supported by the domain model.

Two updates sharing a sequence value but carrying a later exchange timestamp must be accepted.

Do not invent packet IDs.

## 4. Preserve fail-closed timestamp/freshness behavior

Keep:
- exchange timestamp regression => out-of-order/untrusted;
- stale exchange timestamp => STALE;
- reconnect => new feed epoch and recovery period;
- recovery requires configured consecutive valid fresh ticks;
- queue/writer failures remain fatal to recording;
- malformed ticks remain rejected;
- current candle trust propagation remains intact.

## 5. Clean up obsolete sequence-gap configuration

`MARKET_DATA_ALLOWED_SEQUENCE_GAP` / `market_data_allowed_sequence_gap` currently encodes the invalid +1 assumption.

Remove it if no longer meaningful.

If backward compatibility requires temporarily retaining it:
- it must not affect trust;
- mark/document it as deprecated;
- do not expose misleading "gap" diagnostics.

Likewise review `sequence_gap_count` in `InstrumentHealth`.

Preferred result:
- remove/rename it if safe before Phase 3; or
- retain only as non-actionable telemetry with clear naming/documentation.

Do not leave a field that claims packet loss from positive SmartAPI sequence jumps.

Update config, README, exports, fixtures and tests consistently.

## 6. Replay existing recorded data correctly

The fix must work on already-recorded SQLite ticks from the live six-symbol session.

No database migration should be required merely to reinterpret sequence health.

After the fix, replaying those ticks should not mark all instruments untrusted solely because of positive sequence-number jumps.

## 7. Regression tests

Add or update tests for all of the following:

1. Same token sequence values `100, 150, 892, 1200` with increasing timestamps:
   - accepted;
   - trusted;
   - no degraded transition from sequence jumps.

2. Interleaved tokens, for example:
   - A seq 100
   - B seq 105
   - A seq 150
   - B seq 900
   - A seq 1200
   All remain trusted with valid timestamps.

3. Lower sequence number but later exchange timestamp:
   - accepted;
   - not degraded merely because sequence decreased.

4. Exact duplicate identity:
   - rejected as duplicate;
   - duplicate count increments.

5. Same sequence number but later exchange timestamp:
   - accepted.

6. Exchange timestamp regression:
   - rejected/untrusted as out-of-order.

7. Stale tick:
   - accepted only according to existing semantics but never trusted;
   - status STALE.

8. Reconnect:
   - begins new epoch;
   - requires configured recovery ticks;
   - sequence values after reconnect may restart/reset freely.

9. Existing deterministic candle/replay tests still pass.

10. Existing TLS, reconnect, queue, historical-throttle, bootstrap, scanner and reference-data tests all remain passing.

## 8. Scanner regression

Add a scanner/replay regression representing the real six-symbol condition:

- multiple instruments;
- large positive sequence jumps;
- fresh timestamps;
- no historical/opening data assumption necessary for this specific health assertion.

Expected:
- instruments are not classified `market_data_untrusted` because of sequence jumps;
- scanner may still be `NOT_READY` for legitimate reasons such as missing 09:15 opening data or missing historical bootstrap.

This distinction is important.

## 9. Documentation

README must explicitly state:
- SmartAPI `sequence_number` is telemetry, not a per-token contiguous packet counter;
- positive jumps do not imply packet loss;
- trust is based on timestamp freshness/order, reconnect epochs, ingestion integrity, and malformed-data handling.

## Acceptance gate

Before finishing:

- `python -m ruff check .` passes.
- complete pytest suite passes.
- no order code is added.
- `TRADING_MODE=PAPER`.
- `LIVE_TRADING_ENABLED=false`.
- valid large positive sequence jumps stay trusted.
- lower sequence alone does not make data untrusted.
- exact duplicates remain detectable.
- exchange timestamp regression remains fail-closed.
- reconnect recovery remains fail-closed.
- existing six-symbol recorded data can be replayed without false `market_data_untrusted` caused by sequence jumps.

Commit and push with a clear message.

At the end report:
1. commit SHA;
2. files changed;
3. test count/result;
4. Ruff result;
5. exact new sequence semantics;
6. duplicate semantics;
7. reconnect semantics;
8. whether obsolete config/fields were removed or deprecated;
9. confirmation no order execution was added.
