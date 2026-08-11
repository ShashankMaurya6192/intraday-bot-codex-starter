# Codex Task: Phase 2.5.2 Live Projection Read-Time Race

Work from commit `24cefb22c270d53a2813663230f72425c4373cf3`.

Do not implement Phase 3 or any execution/order code. PAPER only.

## Live failure reproduced

- 158 tests passed
- Ruff passed
- recorder caught up 64,020 ticks
- scan used INCREMENTAL_PROJECTION
- raw_ticks_replayed=0
- projection_load_seconds≈0.119
- whole CLI scan≈3.59s

But scan and compare both failed with:
`ProjectionCutoffBehindCheckpointError: projection checkpoint is ahead of the requested read time`

## Root cause

`_scan()` captures `now` before cache loading and before the SQLite projection snapshot is read. The live recorder can legitimately commit a newer checkpoint while those steps run. `load_projection_view(..., now=old_now)` then mistakes that valid newer committed checkpoint for future/look-ahead data.

The same stale caller-time race affects `compare-projection-replay`.

## Required fix

1. Normal live scan must read the latest transactionally consistent committed projection snapshot without rejecting it just because it advanced after command start.
2. Do not use a stale pre-cache CLI timestamp as the causal cutoff for a latest-state read.
3. Scanner evaluation time should be the committed projection checkpoint `updated_at`, or an equally causal committed-state timestamp.
4. Freshness must compare checkpoint `last_received_at` against a fresh wall-clock sampled after the snapshot load. Preserve the 5-second live freshness gate.
5. `compare-projection-replay` must load the latest committed projection read-only, then replay raw rows exactly to that checkpoint `updated_at`.
6. Preserve `ProjectionCutoffBehindCheckpointError` for genuine explicit historical/as-of callers. Do not remove causal cutoff protection globally.
7. Scan must remain read-only. Do not reintroduce mutable scan catch-up/writes.
8. Preserve Phase 2.5.1 transactional snapshot reads and CAS writer protection.
9. Preserve fail-closed corrupt/ahead/stale semantics.
10. Preserve Phase 2.3 recovery and Phase 2.4 qualification exactly.

## Tests

Add tests for:
- early CLI start time + later recorder checkpoint still succeeds for latest live read
- latest snapshot updated_at may be later than CLI start without being future data
- freshness wall clock sampled after snapshot load
- scanner evaluation time equals committed checkpoint time
- genuine explicit cutoff behind checkpoint still raises ProjectionCutoffBehindCheckpointError
- compare survives recorder advancement after command start
- compare replay cutoff equals checkpoint updated_at
- scan remains read-only
- CAS/snapshot consistency tests remain
- stale projection still fails closed
- all tests + Ruff pass
- no order code

## Real validation

With recorder running:
1. pull/install/tests/Ruff
2. restart recorder
3. run scan repeatedly
4. no ProjectionCutoffBehindCheckpointError
5. raw_ticks_replayed=0
6. full_replay_used=false
7. projection age fresh
8. measure whole CLI runtime
9. run compare-projection-replay for 2026-08-11
10. require equivalent=true and mismatches=[]

Commit and push. Report SHA, tests/Ruff, read-time semantics, freshness semantics, comparison cutoff semantics, and confirmation no execution/order code was added.
