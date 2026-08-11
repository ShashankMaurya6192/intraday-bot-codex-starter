# Codex Task: Phase 2.5.3 Stable Candle Projection / Late-Arrival Equivalence

Work from commit `242a8c78bcc21d84008891fc6f71533f42525b38`.

Read AGENTS.md, README.md, all Phase 2.5 task files, and current candle/projection/replay/storage code. Do not implement Phase 3 or any execution/order code. PAPER only.

## Real failure

The 2026-08-11 comparison showed:
- projection candles: 2688
- replay candles: 2688
- only projection: 0
- only replay: 0
- changed values: 149

Examples:
- TCS 14:50 1m projection = 1 tick, OHLC all 2439.4, volume 0; replay = 77 ticks, O 2439.7 H 2439.9 L 2438.7 C 2439.4, volume 2132.
- TCS 15:00 5m projection = 2 ticks, volume 218; replay = 227 ticks, volume 40404.
- HDFCBANK 14:47 1m projection trusted=True; replay trusted=False.

The mismatch begins around recorder restart/catch-up and continues through live incremental projection.

## Root cause

`MarketIntelligenceProjection._materialize()` currently calls `SessionCandleAggregator.flush(until=evaluation)` after projection batches. `evaluation` follows received/event wall-clock time, while candle buckets use `exchange_timestamp`.

A live batch can cross a wall-clock minute boundary while valid ticks for an earlier exchange-time bucket are still arriving. The projection prematurely persists that bucket. Later ticks for the same exchange bucket create a new working candle and SQLite upsert replaces the earlier candle with only the tail fragment.

Replay avoids this because it consumes all visible raw rows before final as-of flush.

## Required design

1. Normal durable completion must be driven by same-instrument exchange-time progression through `SessionCandleAggregator.process()`. Do not globally flush all working candles on each live batch based on received wall clock.

2. Add a non-mutating as-of snapshot for working candles, e.g. `snapshot_completed(until=...)`. It may expose working buckets with `end_at <= cutoff` as ephemeral completed candles, but must not delete working state or persist these provisional candles.

3. `load_projection_view()` should combine durable stable candles with ephemeral completed working candles reconstructed from checkpoint `state_json`, deduplicated deterministically. Scan remains read-only.

4. Later valid ticks must be able to revise a candle that appeared ephemerally in an earlier view.

5. Restart must restore working candle state and process only the raw suffix. Do not flush restored working candles merely because restart wall clock is later.

6. Existing buggy Phase 2.5 checkpoints cannot be trusted. Bump `PROJECTION_STATE_VERSION` or otherwise force one safe rebuild from authoritative current-session raw ticks. Rebuild should clear only derived current-session projection/LOCAL_TICKS/ANGEL_SESSION_FIELDS state, preserve raw rows and prior historical sessions, then reconstruct with fixed semantics.

7. Reconnect/feed-epoch trust must match replay exactly. Any candle containing recovery/untrusted ticks remains trusted=False. A candle completed before reconnect must not be retroactively changed.

8. Preserve cumulative-volume-delta candle volume semantics.

9. At session close, either keep final working buckets in checkpoint and expose them ephemerally, or finalize only through an explicit recorder-owned safe session-finalization step after queue drain. Never globally flush active buckets after each live batch.

10. Improve `compare-projection-replay` candle mismatch output with bounded counts and first few differing candle identities/fields.

## Tests

Cover at minimum:
- delayed/interleaved received ticks across 1m boundary
- same for 5m
- multi-symbol batch-boundary interleaving
- no durable wall-clock flush per batch
- ephemeral read-view completion without mutation
- later tick revises prior ephemeral view
- restart restores working candles and suffix catch-up equals replay
- old projection state version triggers deterministic rebuild
- rebuild preserves raw/feed events and prior historical data
- reconnect trust matches replay
- session-close final bucket matches replay
- Phase 2.3 recovery unchanged
- Phase 2.4 qualification unchanged
- scan remains read-only
- CAS/single-writer tests remain
- comparison returns equivalent=true on delayed-tick fixture
- bounded mismatch diagnostics
- all tests and Ruff pass
- no order code

## Real acceptance

Using the existing 2026-08-11 raw database:
1. pull/install/tests/Ruff
2. start `record-scanner-market`
3. migration/rebuild should reconstruct today's projection automatically
4. stop cleanly if market is closed
5. run `scan --json`
6. require raw_ticks_replayed=0 and full_replay_used=false
7. run `compare-projection-replay --date 2026-08-11 --json`
8. require equivalent=true and mismatches=[]
9. rerun detailed candle diff and require ONLY PROJECTION=0, ONLY REPLAY=0, CHANGED=0

Commit and push. Report SHA, files changed, test count/Ruff, stable completion rule, ephemeral read semantics, rebuild behavior, reconnect trust behavior, real comparison result, detailed diff counts, and confirmation no execution/order code was added.
