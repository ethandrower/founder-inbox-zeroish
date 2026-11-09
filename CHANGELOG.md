# Changelog

## 0.3.0 (2026-11-09)

- **`zeroish ui`:** optional local web page to edit rules, categories, senders, backstop and models; a simulator that
  shows which rule fires for any combination of facts; run health; Microsoft sign-in. Saves are checked like
  `zeroish lint`, backed up, and refused on a concurrent edit. Local only, one-time key per launch.
- **The backlog sweep always runs every rule.** Run as `--pass cleanup` it never applied the fast rules, so pitches the
  new-mail lane missed stayed in the inbox.
- **`zeroish status` catches a stuck or leaky sweep:** no sweep run recently, no full pass in 24 hours, or mail kept
  past the backstop.
- Cut-off or dropped Graph replies are retried instead of crashing a run.
- `Auth.device_start` / `device_poll` split the device-code sign-in so other front ends can drive it.
- Licence changed from MIT to **FSL-1.1-MIT**: free to use, modify and self-host; no competing commercial product or service; each release becomes MIT two years after it ships. Copies obtained under MIT before this change keep their MIT terms.


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
