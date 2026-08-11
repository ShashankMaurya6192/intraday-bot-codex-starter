# Codex Task: Phase 2.4 Signal Qualification Gate

Work from commit `16054f4a538cc7a4f6e109b5aa8f19b14446f95e`.

Read AGENTS.md, README.md, and all prior Phase 2 task files before editing. Do not implement Phase 3, paper fills, positions, risk execution, or any Angel order path. Keep TRADING_MODE=PAPER and LIVE_TRADING_ENABLED=false.

## Goal

Phase 2.3 is operational: the scanner is READY on live Angel data with empty diagnostics, but ranking candidates are not the same as executable signals. Keep ranking, add a strict fail-closed qualification layer.

## Mandatory LONG gates

A LONG is qualified only if all are true:
- market data trusted/READY
- market regime == TREND_UP
- LTP > VWAP
- EMA9 > EMA20 on completed 5m data
- RVOL >= configured threshold, default 1.5
- relative strength vs NIFTY > 0
- a completed trusted 5m candle after 09:30 closes strictly above opening-range high
- abs(VWAP distance in ATR units) <= configured maximum
- no required indicator/reference is unavailable

## Mandatory SHORT gates

Exact inverse:
- market data trusted/READY
- market regime == TREND_DOWN
- LTP < VWAP
- EMA9 < EMA20
- RVOL >= threshold
- relative strength < 0
- a completed trusted 5m candle after 09:30 closes strictly below opening-range low
- abs(VWAP distance in ATR units) <= configured maximum
- no required dependency unavailable

## Ranking vs qualification

Do not delete current component scoring. A candidate may rank highly and still be unqualified.

Add explicit machine-readable qualification output, for example:
- qualified
- qualification_reasons

Stable reason identifiers should include:
- market_regime_not_trend_up
- market_regime_not_trend_down
- price_not_above_vwap
- price_not_below_vwap
- ema9_not_above_ema20
- ema9_not_below_ema20
- rvol_below_threshold
- relative_strength_not_positive
- relative_strength_not_negative
- opening_breakout_not_confirmed
- opening_breakdown_not_confirmed
- vwap_atr_extension_exceeded

Preserve ranked long_candidates and short_candidates. Optionally add qualified_long_signals / qualified_short_signals plus counts.

## Breakout semantics

Opening-range proximity is not confirmation.

LONG confirmation:
- current-session only
- completed and trusted 5m candle
- candle must finish after opening range
- close strictly > opening-range high

SHORT:
- same, but close strictly < opening-range low

No partial 5m candle, tick-only touch, or intrabar spike qualifies. Replay/as-of scans must not see a confirmation candle before end_at.

## VWAP/ATR extension

Add config such as MAX_VWAP_DISTANCE_ATR=1.5, validated > 0.

For both sides require abs(vwap_distance_atr) <= max. Equality passes. Missing ATR/distance fails closed.

## Sector confirmation

Do not fabricate sector mapping. Keep sector=None for now and document sector confirmation as a future enhancement until authoritative sector/index data exists.

## Real-live regression

Use the first READY Phase 2.3 live scan as a regression:

SBIN-EQ approximately:
- regime TREND_DOWN
- LTP 1069.6
- VWAP 1065.55
- EMA9 1066.6497
- EMA20 1062.1592
- RVOL 1.0905
- RS +0.3875
- opening high 1071
- opening low 1060.4

It may rank highly LONG, but MUST NOT be LONG-qualified.

HDFCBANK-EQ approximately:
- regime TREND_DOWN
- LTP 727.85
- VWAP 727.97
- EMA9 727.9111
- EMA20 729.8224
- RVOL 0.8991
- RS +0.1554
- opening low 727.3

It may rank SHORT, but MUST NOT be SHORT-qualified because RVOL is below threshold and RS is not negative.

## Required tests

Cover at least:
1. high LONG score + TREND_DOWN -> not qualified
2. RVOL 1.49 fails
3. RVOL exactly 1.5 passes RVOL gate
4. price above opening high without completed 5m close -> fails
5. completed trusted 5m close above opening high -> passes breakout gate
6. partial 5m candle cannot qualify
7. future confirmation hidden by as-of cutoff
8. LONG RS <= 0 fails
9. SHORT RS >= 0 fails
10. EMA equality fails
11. VWAP equality fails
12. extension above max fails
13. extension exactly at max passes
14. RANGE qualifies neither side
15. UNKNOWN qualifies neither side
16. all-gates LONG qualifies
17. all-gates SHORT qualifies
18. ranking remains available when unqualified
19. qualification reason order deterministic
20. all existing Phase 2.3 tests remain passing
21. no order code added

## Acceptance

Run:
- python -m pytest -q
- python -m ruff check .

No paper/live execution code. PAPER only. Only fully qualified signals may be eligible for a future Phase 3 engine.

Commit and push. Report SHA, files changed, tests, Ruff, exact LONG/SHORT gates, breakout semantics, VWAP/ATR limit, regression result, and confirmation no execution code was added.
