# Codex Task: Phase 1.5 Hardening Before Phase 2

Read `AGENTS.md` completely, inspect the current repository and commit history, and work from the existing Phase 1 implementation. Do not redesign the project and do not add live order placement.

The current code is a good paper-first foundation, but it needs the following hardening before we build the scanner/strategy.

## 1. Fix reconnect accounting for flapping connections

Current behavior resets the reconnect counter as soon as a WebSocket reaches `on_open`. That means a socket that repeatedly opens and then drops almost immediately can reconnect forever.

Implement deterministic bounded reconnect behavior.

Requirements:
- Inject/use a monotonic clock for connection-duration/retry logic.
- Add a configurable `WEBSOCKET_STABLE_CONNECTION_SECONDS` with a sensible default such as 30 or 60 seconds.
- Reaching `on_open` alone must NOT reset the consecutive failure count.
- Reset the failure streak only after a connection has remained healthy for the configured stability window, or an equivalently well-defined tested health criterion.
- A connection that repeatedly opens and drops before the stability threshold must exhaust bounded retries.
- Keep clean user-requested disconnect behavior unchanged.
- Add tests for:
  - failure before open;
  - open then immediate close repeatedly;
  - stable connection followed by a later disconnect;
  - user-requested disconnect;
  - callback/recorder failure.

## 2. Enforce TLS certificate verification

The pinned official SmartAPI `SmartWebSocketV2` implementation currently calls `run_forever(... sslopt={"cert_reqs": ssl.CERT_NONE})`.

Do not use an unverified TLS WebSocket transport in this project.

Requirements:
- Implement a narrowly scoped hardened SmartAPI WebSocket transport/factory that preserves the official protocol/parser/callback behavior but connects with normal server certificate and hostname verification.
- Prefer relying on `websocket-client` secure defaults or explicitly use `ssl.CERT_REQUIRED`.
- Do not globally disable SSL verification.
- Do not monkeypatch global SSL state.
- Add a regression test proving the production WebSocket path cannot pass `ssl.CERT_NONE`.
- Keep all secrets redacted.
- Document why the wrapper exists and pin it to the currently supported SmartAPI SDK version.
- If a safe verified connection cannot be implemented without guessing, fail closed and document the blocker. Do not weaken TLS just to make tests pass.

## 3. Enforce SmartAPI WebSocket quotas before connecting

Angel One documents:
- up to 3 concurrent WebSocket connections per client code;
- up to 1000 token-mode subscriptions per WebSocket session.

For this phase:
- support one WebSocket connection;
- fail before connecting if requested subscriptions exceed 1000;
- add tests for 1000 accepted and 1001 rejected;
- keep the architecture capable of sharding later, but do not add unnecessary multi-socket complexity now.

## 4. Make ingestion safe for hundreds of symbols

The WebSocket callback currently performs a synchronous SQLite transaction per tick. We eventually want a liquid NSE universe of several hundred symbols, so the callback must remain lightweight.

Implement:
- a bounded in-memory tick ingestion queue;
- a dedicated SQLite writer thread/service;
- batch inserts inside transactions;
- configurable queue capacity and batch size;
- clean shutdown that drains the queue within a bounded timeout;
- writer exceptions must mark ingestion unhealthy and stop the feed;
- queue overflow must NEVER silently discard ticks;
- if the queue is full or writer falls behind beyond limits, transition to an unsafe/degraded state and suppress downstream use;
- preserve idempotent inserts;
- tests for batching, clean drain, overflow, writer failure and duplicate ticks.

Do not prematurely optimize. Keep the implementation simple, deterministic and testable.

## 5. Add automated CI

Add `.github/workflows/ci.yml`.

Run on push and pull request:
- Python 3.11;
- Ubuntu latest;
- Windows latest;
- `python -m pip install --upgrade pip`;
- `pip install -e ".[dev]"`;
- `python -m ruff check .`;
- `python -m pytest -q`.

No credentials are required in CI.

## 6. Add broker/instrument constraints needed for the future scanner

Do not build the scanner yet, but add/document the boundary for the next phase:
- Angel One instrument-master data must be cached locally with a last-known-good copy;
- never depend on downloading the entire instrument master at trading time;
- validate symbol token, exchange, tick size and trading symbol from the cached master;
- future equity candidates must be intersected with the broker's currently intraday-allowed NSE list before live order eligibility;
- instrument-master failure must not silently substitute guessed tokens.

A small interface/model is fine if it naturally fits the existing architecture. Do not overbuild it in this task.

## 7. Preserve all existing safety gates

Do not:
- add place/modify/cancel order methods;
- add live execution;
- enable `LIVE_TRADING_ENABLED`;
- add averaging-down or martingale logic;
- hard-code credentials;
- log access tokens/TOTP/PIN/API keys;
- silently drop ticks;
- treat stale or incomplete data as trustworthy.

PAPER must remain the default and only practical mode.

## 8. Tests and documentation

Before finishing:
- run all tests;
- run Ruff;
- update README with the new architecture and Windows commands;
- explain the TLS hardening;
- explain the reconnect stability rule;
- explain the ingestion queue failure behavior;
- update `.env.example` for new settings;
- keep `AGENTS.md` aligned with the implementation.

## Deliverable

Commit the changes with a clear message and push them to the repository.

At the end, report:
1. files changed;
2. test count/results;
3. Ruff result;
4. how TLS verification is guaranteed;
5. how reconnect flapping is bounded;
6. how queue overflow/writer failure fails closed;
7. anything that still blocks Phase 2.

Do not proceed to Phase 2 scanner/strategy implementation in this task.
