# Changelog

## 0.1.1 (2026-11-06)

- A backend whose health check reports failed validation runs is skipped even if its state is "ok".
- Tested: classification falls through to the next backend when one is overloaded.


## 0.1.0 (2026-11-06)

First release: facts and rules engine (first match wins, fast and cleanup passes), a config-defined category
list with a generated classifier prompt, OpenAI-compatible and Anthropic backends with health checks and a
per-email cache, delegated Microsoft 365 sign-in (device code, no secret), plan/apply with guardrails, lint,
explain, eval, and an audit log.
