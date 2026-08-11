# Codex Task: Phase 2.5.1 Projection Concurrency Hardening

Work from commit `f3b9bd809553a4904a66b122d6968332f78320a1`.

Read AGENTS.md, README.md, all Phase 2 task files, and the Phase 2.5 projection implementation before editing.

Do not implement Phase 3, paper fills, positions, risk sizing, or any broker order path. Keep PAPER mode only.

## Bug found in review

Phase 2.5 currently allows both `record-scanner-market` and `scan` to materialize/write the same `market_projection_checkpoint`.

The live recorder advances the projection through `ProjectionMarketDataWriter`.

Normal `scan` creates another `MarketIntelligenceProjection`, restores the checkpoint, runs initialize/catch_up/view, and `_materialize()` writes projection state again.

SQLite correctly rejects a backwards checkpoint with:

`ValueError("projection checkpoint cursor cannot move backward")`

A realistic race is:

1. scan loads checkpoint tick_id=100
2. live recorder processes tick_id=101 and persists checkpoint=101
3. scan materializes older engine state based on tick_id=100
4. SQLite rejects the backwards cursor, or stale derived state competes with the live writer

Fix this before real Phase 2.5 validation.

## Goal

Make projection state single-writer / concurrency-safe.

The live `record-scanner-market` process should normally own projection mutation. `scan` should consume a consistent read-only materialized projection snapshot and must never rewind or overwrite newer live state.

## Requirements

1. Add a read-only projection load path for scan. Load checkpoint, current-session health snapshots, latest references, opening-range references, and completed projected candles from one consistent SQLite read snapshot/transaction where practical.

2. Normal `scan` must not call `_materialize()` or save projection state merely to read it.

3. If raw ticks are ahead of the projection during scan, do not silently run an independent mutable catch-up that races the recorder. Prefer fail-closed `projection_catch_up_pending` / recorder catch-up required. A CAS-safe serialized alternative is acceptable.

4. Any non-recorder projection write path that remains must use optimistic compare-and-swap. It must only update if the persisted predecessor checkpoint/cursors still match what it restored. If another writer advanced first, raise a dedicated `ProjectionConcurrentAdvanceError` subclassing `ProjectionError`; never overwrite newer state.

5. `_scan` must fail closed on projection concurrency conflicts and never leak a raw ValueError traceback.

6. Do not mix checkpoint/version N with health/reference state from another commit. Projection snapshot reads must be transactionally consistent.

7. Preserve `SCANNER_PROJECTION_STALE_AFTER_SECONDS`. A scan must not change checkpoint `updated_at`, `last_tick_id`, or engine `state_json`.

8. Preserve recorder restart behavior: first Phase 2.5 startup may rebuild once from retained current-session raw ticks; later restarts catch up only suffix; raw rows remain authoritative; feed epochs stay causal.

9. `compare-projection-replay` must not mutate the live projection just to compare it. Prefer read-only persisted projection at a deterministic cutoff plus raw replay to that same cutoff, or an isolated in-memory projection.

10. Preserve all Phase 2.3 late-start recovery and Phase 2.4 qualification semantics exactly.

## Tests

Add at minimum:
- normal scan projection load performs no projection writes
- recorder can advance while scan reads without scan overwriting it
- simulated checkpoint 100 -> recorder 101 race cannot move backwards
- concurrency conflict becomes ProjectionConcurrentAdvanceError if a CAS write path remains
- `_scan` fails closed instead of traceback on conflict
- read-only projection snapshot is transactionally consistent
- scan does not change checkpoint updated_at
- scan does not change checkpoint last_tick_id
- scan does not change engine state_json
- stale projection still fails closed
- recorder suffix catch-up still works
- reconnect/feed epoch equivalence remains
- late-start recovery equivalence remains
- qualification output remains equivalent
- compare command does not mutate live checkpoint
- all existing tests pass
- no order/paper execution code added

## Real validation after patch

After pulling this patch:
1. stop the pre-2.5 recorder cleanly
2. start new `record-scanner-market`
3. allow first projection rebuild/catch-up
4. leave recorder running
5. run `scan --json` repeatedly from another PowerShell
6. verify no checkpoint race/traceback
7. verify `raw_ticks_replayed=0`
8. verify `full_replay_used=false`
9. verify projection age remains fresh
10. verify runtime is materially below the old ~12s
11. run projection/replay equivalence without mutating the live checkpoint

## Acceptance

- full pytest passes
- Ruff passes
- projection has one normal writer
- scan is read-only against projection state
- no checkpoint rewind is possible
- no raw concurrency ValueError
- full replay remains audit/debug only
- Phase 2.3 and 2.4 semantics unchanged
- no order code
- PAPER only

Commit and push. Report SHA, files changed, tests/Ruff, writer ownership model, read consistency model, CAS/conflict behavior if retained, repeated-live-scan race test result, and confirmation no execution/order code was added.
