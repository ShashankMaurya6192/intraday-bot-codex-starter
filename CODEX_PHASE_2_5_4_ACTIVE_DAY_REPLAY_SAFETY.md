# Codex Task: Phase 2.5.4 Active-Day Replay Persistence Safety

Work from commit `56c24ccd8806c150047d35db3ea5402c49ad5267`.

Read `AGENTS.md`, `README.md`, all Phase 2 task files, and the current replay/CLI/storage code.

Do not implement Phase 3, paper execution, positions, risk sizing, or any Angel order path. PAPER only.

## Bug

The CLI `replay --date YYYY-MM-DD` still calls deterministic replay with `persist=True` unconditionally.

For the current trading date, running this command before the NSE cash session is complete can persist partial current-session candles and/or incomplete RVOL baseline state. That can poison later historical state.

The normal `scan` path is already safe because it is read-only and uses the incremental projection. This patch is only about the manual `replay` command.

## Required behavior

1. A manual replay of a completed prior session may persist exactly as today.
2. A manual replay of the current IST date must never persist an incomplete active session.
3. Before 15:30 IST on the current session date, prefer rejecting the command with a clear non-zero error such as `current_session_not_complete`. An explicitly analysis-only replay with `persist=False` is acceptable if clearly surfaced.
4. At/after NSE cash-session close, current-date persistence is allowed only under a documented deterministic-completion rule.
5. Never silently persist partial baselines.
6. Any explicit as-of replay remains ephemeral and non-persistent.
7. Do not modify incremental scanner projection architecture.
8. Preserve Phase 2.3 recovery and Phase 2.4 qualification semantics.
9. No execution/order code.

## Tests

Add at minimum:
- current date before 15:30 cannot persist
- current date at/after session close follows the documented safe rule
- prior completed session still persists
- explicit as-of replay remains non-persistent
- partial current-session replay cannot write RVOL baseline state
- partial current-session replay cannot overwrite canonical completed candles
- existing replay tests remain green
- full suite passes
- Ruff passes
- no execution/order code

## Acceptance

- full pytest green
- Ruff green
- no manual command can persist an unfinished current trading session
- prior-session replay remains backward compatible
- scanner/projection behavior unchanged
- PAPER only
- no order code

Commit and push. Report SHA, files changed, test count/Ruff, exact current-day replay rule, and confirmation no execution/order code was added.
