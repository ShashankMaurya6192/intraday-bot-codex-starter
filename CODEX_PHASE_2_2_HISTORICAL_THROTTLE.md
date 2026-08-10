# Codex Task: Phase 2.2 Historical API Throttle Handling

Read these files completely before changing code:

- `AGENTS.md`
- `README.md`
- `CODEX_PHASE_2_MARKET_INTELLIGENCE.md`
- `CODEX_PHASE_2_1_CORRECTNESS.md`
- current Phase 2.1 implementation

Work from commit:

`1561c1a03edeb8605461d053001bde49db56d1ce`

Do not proceed to Phase 3 in this task.

Do not add:
- live broker order placement;
- modify/cancel order support;
- paper fills;
- position sizing;
- risk execution;
- any automatic trading path.

Keep:
- `TRADING_MODE=PAPER`
- `LIVE_TRADING_ENABLED=false`

## Real broker failure observed

On 2026-08-10, SmartAPI authentication succeeded, but the very first NSE Historical API candle request failed with a non-JSON response from Angel:

`Access denied because of exceeding access rate`

The SmartAPI SDK raised a `DataException` because it attempted to JSON-decode the plain-text body.

The user waited about 15 minutes and repeated one single request. Login again succeeded, and the first historical call again failed with the same plain-text access-rate response.

This means Phase 2.2 must treat broker throttling as a distinct broker condition rather than wrapping it as generic `BrokerReferenceDataError` / `HistoricalBootstrapError`.

## 1. Add explicit broker historical-error taxonomy

Create clear exception types, for example:

- `BrokerHistoricalDataError`
- `BrokerHistoricalRateLimitError`
- `BrokerHistoricalResponseError`

Exact names may differ if the repository already has a cleaner hierarchy.

Requirements:
- rate-limit/throttle conditions must be distinguishable programmatically;
- malformed/invalid historical responses must remain distinguishable from throttling;
- authentication errors must not be misclassified as rate-limit errors;
- exceptions must never include API key, client code, password, TOTP seed, JWT, refresh token, feed token, or request headers.

## 2. Preserve the real sanitized broker error

Current `AngelOneMarketDataAdapter.fetch_historical_candles()` collapses the useful SDK exception into:

`Angel historical-candle request failed`

Fix this.

When the underlying SDK raises an exception whose sanitized message/body contains a throttle/access-rate condition such as:

- `Access denied because of exceeding access rate`
- a documented SmartAPI throttle/rate-limit error code if present;
- HTTP 403 plus an unambiguous historical rate-limit body if that information is available through the SDK exception;

raise the dedicated historical rate-limit exception.

The exception exposed to CLI/service code should retain a safe message such as:

`Angel Historical API rate limit/throttle rejection: Access denied because of exceeding access rate`

Do not expose the SDK's full request object or authentication material.

For other historical failures, retain enough sanitized detail to diagnose:
- broker status/message/errorcode;
- malformed non-JSON broker response;
- transport/server failure;
while avoiding secrets.

## 3. Stop the bootstrap globally on broker throttling

Current bootstrap iterates symbol-by-symbol and catches broad exceptions per symbol.

Change semantics:

- ordinary symbol-specific data validation failure may mark that symbol `FAILED` and continue;
- broker historical throttle/rate-limit failure is a **global infrastructure condition**;
- on a rate-limit failure, stop issuing historical requests immediately;
- do not consume remaining retry attempts for the same symbol after an unambiguous rate-limit rejection;
- do not move on to the next 5/20/500 symbols;
- do not hammer the endpoint.

Return/raise a structured result that clearly indicates:
- bootstrap aborted;
- reason category `RATE_LIMITED` or equivalent;
- symbol being processed when it happened;
- number completed before abort;
- number skipped because of abort;
- operator action/cooldown required.

Do not invent a precise retry time unless Angel supplied one.

## 4. Preserve completed work and resume safely

If 200 symbols completed successfully and symbol 201 gets rate-limited:

- the first 200 valid historical datasets must remain committed;
- symbol 201 must not replace an older valid dataset with partial/malformed data;
- symbols 202+ must not be requested;
- the next normal `bootstrap-history` run must skip already-current successful symbols and resume remaining work;
- `--force` may intentionally refresh current symbols, but it must still stop globally on a rate-limit rejection.

Add tests proving this behavior.

## 5. Improve retry policy

Retries are appropriate only for transient failures where another request is useful.

Requirements:
- do not immediately retry an explicit rate-limit/throttle rejection;
- retain bounded exponential backoff for genuine transient transport/server failures;
- response validation failures should normally fail that symbol rather than retry identical malformed data repeatedly;
- do not exceed configured maximum attempts;
- keep request spacing conservative.

Add a small explicit setting only if needed, for example:
- `HISTORICAL_RATE_LIMIT_COOLDOWN_SECONDS`

If adding one:
- it is advisory/operator-facing only unless the CLI explicitly sleeps;
- do not make a command silently sleep for very long periods;
- prefer aborting with a clear message so an operator can retry later.

## 6. Improve CLI output

`python -m trading_bot bootstrap-history`

must no longer show six useless lines like:

`reason=HistoricalBootstrapError`

when Angel actually returned an access-rate rejection.

Expected style:

```text
Historical bootstrap: ABORTED_RATE_LIMIT
requested=6 completed=0 failed=1 skipped_after_abort=5

SBIN-EQ: RATE_LIMITED
reason=Angel Historical API rate limit/throttle rejection:
Access denied because of exceeding access rate

No further Historical API requests were made.
Retry later after the broker-side throttle clears.
Existing valid historical datasets were preserved.
```

Keep output concise and deterministic.

JSON output should expose machine-readable fields such as:

```json
{
  "status": "ABORTED_RATE_LIMIT",
  "requested_symbols": 6,
  "completed_symbols": 0,
  "failed_symbols": 1,
  "skipped_after_abort": 5,
  "rate_limited_symbol": "SBIN-EQ",
  "reason_category": "RATE_LIMITED",
  "order_placement_available": false
}
```

Exact schema may differ, but it must preserve this information.

## 7. Add a safe diagnostic command

Add an explicit diagnostic command, for example:

`python -m trading_bot test-historical --symbol SBIN --date 2026-08-07`

Purpose:
- issue exactly one Historical API request;
- resolve token from the authoritative local instrument cache;
- never accept a manually guessed token;
- authenticate normally;
- request one completed session only;
- print broker status/message/errorcode, row count, first timestamp, last timestamp;
- never print credentials/tokens used for authentication;
- no automatic retry;
- no loop across symbols;
- return non-zero on throttle/rejection.

If adding this command makes the CLI too complex, an equivalent dedicated diagnostic module is acceptable, but it must be documented and tested.

## 8. Historical request batching remains conservative

Do not redesign all history acquisition unless required.

Keep one-minute canonical historical input.

Keep:
- prior-session 1m persistence;
- deterministic local 5m derivation;
- RVOL baselines only for equities;
- no RVOL baseline for NIFTY;
- atomic replacement per validated symbol dataset;
- restartability.

For now, keep the configured historical lookback conservative.

If the implementation discovers that a requested date range can exceed a broker record limit:
- split by date/session boundaries deterministically;
- validate and combine chunks before replacing the symbol's dataset;
- throttle between chunks;
- stop globally if any chunk receives a rate-limit rejection.

Do not assume a 30-day request always fits.

## 9. Tests

Add offline/synthetic tests covering at least:

1. SDK-style exception containing:
   `Access denied because of exceeding access rate`
   becomes `BrokerHistoricalRateLimitError` or equivalent.

2. Generic network error remains a generic historical broker error.

3. Authentication error remains authentication error.

4. Throttle on first symbol:
   - one broker call only;
   - bootstrap aborts;
   - remaining symbols are not called.

5. Throttle after two successful symbols:
   - first two are persisted;
   - third is not partially persisted;
   - fourth/fifth are not requested;
   - rerun skips first two and resumes remaining symbols.

6. `--force` still aborts globally on rate limit.

7. No secret values appear in exception strings, CLI output, JSON output, or logs.

8. Diagnostic single-symbol historical command makes exactly one historical request and no retry.

9. Existing malformed/partial historical-response tests still pass.

10. All Phase 1.5 TLS/reconnect/queue tests and Phase 2/2.1 tests continue passing.

## 10. Logging

Use safe structured/logged messages where useful.

Do not log:
- credentials;
- TOTP;
- access/refresh/feed tokens;
- full auth response;
- request headers carrying auth;
- `.env` contents.

It is okay to log:
- trading symbol;
- exchange;
- historical date range;
- interval;
- sanitized broker error category;
- attempt count;
- abort reason.

## 11. README

Document:

- Historical API can be broker-throttled independently of normal login and live WebSocket market data.
- A historical throttle aborts the bootstrap immediately.
- Previously completed symbol history is preserved.
- Retry should be operator-initiated later.
- `record-scanner-market` remains usable even if historical bootstrap is temporarily blocked, but scanner indicator readiness may remain incomplete until history is available.
- how to run the one-request historical diagnostic command.

## Acceptance gate

Before finishing:

- `python -m ruff check .` passes.
- complete `pytest` suite passes.
- no live/paper order execution code is added.
- no secrets are committed.
- throttle error is visible as a distinct sanitized category.
- one explicit throttle response causes no additional Historical API calls in that run.
- previously completed symbols survive a later throttle.
- rerun resumes rather than redownloading already-current symbols.
- existing valid history is never replaced by a failed/partial response.
- `bootstrap-history` produces useful human and JSON diagnostics.
- single-symbol diagnostic performs exactly one request with zero retries.

Commit and push with a clear message.

At the end report:
1. commit SHA;
2. files changed;
3. pytest result/count;
4. Ruff result;
5. historical exception taxonomy;
6. rate-limit abort behavior;
7. resume/preservation behavior;
8. diagnostic command usage;
9. any remaining broker-side limitation.

Do not implement Phase 3.
