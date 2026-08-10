# AGENTS.md

## Mission
Build a production-minded, local-first automated intraday trading system for Indian equities using Angel One SmartAPI.

The system must:
1. consume broker WebSocket market data as the source of truth for executable market state;
2. scan a configurable liquid NSE universe;
3. generate deterministic LONG/SHORT signals;
4. size positions from risk, never from maximum available leverage;
5. support paper trading first and live trading only after explicit configuration;
6. place, monitor, modify, and exit INTRADAY orders through a broker adapter;
7. fail safe when market data, broker state, authentication, or internal state is uncertain;
8. maintain a complete audit trail.

## Non-negotiable safety rules
- Default mode is PAPER.
- Never silently switch PAPER to LIVE.
- Never hard-code API keys, passwords, PINs, TOTP secrets, JWTs, feed tokens, or client codes.
- Secrets belong in `.env`, which must remain gitignored.
- LIVE mode requires `LIVE_TRADING_ENABLED=true` plus an explicit startup confirmation.
- No averaging down.
- No martingale.
- No revenge-trading logic.
- Never increase quantity merely because additional margin is available.
- Every live entry must have a defined protective exit plan.
- Reject a new trade if stop distance is zero/invalid, market data is stale, available funds cannot be verified, or risk limits would be exceeded.
- Daily loss limit and kill switch override every strategy signal.
- On uncertain order state, stop creating new positions until reconciliation succeeds.
- Never fabricate fills. Broker-confirmed fills are authoritative.
- Use idempotent client/order tags where supported to reduce duplicate-order risk.
- Do not log secrets.

## Initial strategy
Implement `RelativeStrengthOpeningBreakoutV1`.

Trading window:
- Observe from 09:15 IST.
- No entries before 09:30 IST.
- No new entries after 14:45 IST.
- Square off remaining intraday positions before broker/exchange cutoff, with a configurable default of 15:15 IST.

LONG candidate:
- price above VWAP;
- EMA(9) above EMA(20) on 5-minute candles;
- relative volume >= configurable threshold, initially 1.5;
- stock relative strength vs NIFTY positive;
- sector confirmation when sector data is available;
- opening-range high breakout;
- breakout confirmed by a completed 5-minute candle;
- reject excessively extended entries based on ATR/VWAP distance.

SHORT candidate:
- inverse conditions.

Do not enter merely because an intraday high/low is touched.

## Risk model
Defaults are configuration, not constants:
- risk_per_trade_pct = 0.30%
- max_daily_loss_pct = 0.90%
- max_open_positions = 2
- max_trades_per_day = 4
- max_consecutive_losses = 3
- maximum capital utilization must be configurable
- leverage/margin may reduce cash required but must NOT increase planned rupee risk

Position sizing:
`risk_budget = equity * risk_per_trade_pct`
`risk_per_share = abs(entry - stop)`
`qty_by_risk = floor(risk_budget / risk_per_share)`

Final quantity is the minimum allowed by risk, capital/margin constraints, liquidity constraints, and broker/exchange constraints.

## Exit model
Initial implementation:
- hard stop;
- optional partial at +1R;
- optional second partial at +1.5R;
- trailing remainder using configured swing/EMA logic;
- time-based forced exit.

Protective exit handling must be tested for:
- entry rejected;
- partial fill;
- exit rejected;
- network timeout;
- WebSocket disconnect;
- process restart;
- stale position cache.

## Market regime
Classify market as:
- TREND_UP
- TREND_DOWN
- RANGE
- UNKNOWN

Use NIFTY price/VWAP, short EMA structure, breadth if available, and sector participation. UNKNOWN and unreliable data should suppress new entries.

## Architecture
Keep broker-specific code behind interfaces.

Suggested modules:
- `broker/angel.py`: authentication, market feed, funds, orders, positions, order reconciliation
- `marketdata/`: ticks, candle aggregation, freshness checks
- `indicators/`: VWAP, EMA, ATR, relative volume, relative strength
- `strategy/`: signal generation only
- `risk/`: sizing and portfolio/day limits
- `execution/`: order state machine and protective exits
- `storage/`: SQLite persistence and audit events
- `app/`: CLI/service orchestration
- `backtest/`: replay engine
- `paper/`: simulated execution
- `monitoring/`: health and structured logs

## Engineering requirements
- Python 3.11+.
- Type hints for public functions.
- `pytest` tests for strategy, sizing, order state transitions, stale data, and kill switch.
- Use timezone-aware datetimes and `Asia/Kolkata`.
- Decimal or carefully controlled tick-size rounding for prices.
- Structured logs.
- SQLite is sufficient initially.
- Reconnect WebSocket with bounded exponential backoff.
- Persist enough state to reconcile safely after restart.
- Separate signal generation from execution.
- Strategy code must never call broker order methods directly.
- Add dry-run CLI commands before live commands.
- Prefer simple, inspectable code over clever abstractions.

## Development sequence
1. Project skeleton and config.
2. Angel authentication and live market-data connection.
3. Tick normalization and 1m/5m candle aggregation.
4. Market recorder to SQLite.
5. Indicators.
6. Scanner and ranking.
7. Strategy signals.
8. Risk engine.
9. Paper execution.
10. Replay/backtest.
11. Order adapter and reconciliation.
12. Live mode behind hard feature gates.
13. Monitoring/dashboard.
14. Only then consider deployment to a static-IP host.

## Acceptance gate before LIVE mode
Do not recommend enabling LIVE until:
- unit/integration tests pass;
- paper mode has been run across multiple market sessions;
- reconnect/restart tests pass;
- duplicate-order tests pass;
- stale-feed behavior is verified;
- rejected/partial order behavior is verified;
- daily loss kill switch is verified;
- position reconciliation against broker is verified;
- user explicitly chooses to enable LIVE.

## Current Angel One constraints to design for
As of the project bootstrap date, Angel One states that order/GTT requests require a registered static IPv4 for the static-IP API setup. Market-data/non-order APIs do not require static IP in the same way. Treat broker rules and rate limits as external configuration and re-check official documentation before live deployment.

## Codex behavior
When working on this repository:
- inspect existing code before changing architecture;
- make small testable commits/steps;
- run tests after meaningful changes;
- never enable LIVE mode on behalf of the user;
- never insert real credentials into files;
- update README when setup/behavior changes;
- explain any assumption affecting real-money behavior;
- if broker behavior is unclear, implement an interface/test double first rather than guessing.
