# Changelog

## 0.2.0 (2026-11-06)

- **New-customer protection:** two new sender kinds, `known` (from `senders.known_file`, written by your CRM sync) and `team_thread` (a colleague is on To/Cc). Both are trusted, so they are never cold.
- **`stale` backstop:** past N days, move rules apply with `read` relaxed, keep rules stop protecting, and anything unmatched goes to the stale folder.
- **Backlog lane:** `zeroish run --sweep` goes oldest first from a saved bookmark and wraps around at the present.
- **Run lock:** overlapping cron ticks are skipped and logged.
- **`zeroish status`:** health from the audit log (last runs, moves per rule and folder, failures, classifier availability). It exits 3 when unhealthy.
- Every run writes a `run` summary line, including failures.


## 0.1.1 (2026-11-06)

- A backend whose health check reports failed validation runs is skipped even if its state is "ok".
- Tested: classification falls through to the next backend when one is overloaded.


## 0.1.0 (2026-11-06)

First release: facts and rules engine (first match wins, fast and cleanup passes), a config-defined category
list with a generated classifier prompt, OpenAI-compatible and Anthropic backends with health checks and a
per-email cache, delegated Microsoft 365 sign-in (device code, no secret), plan/apply with guardrails, lint,
explain, eval, and an audit log.
