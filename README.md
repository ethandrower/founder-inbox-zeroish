# Founder Inbox Zeroish

Rules-based inbox triage for Microsoft 365, for founders who get more cold pitches than real email.

Cold outreach and promotions leave the inbox within minutes. Notifications, digests and threads you've already
answered get filed later. Anything that needs you stays put. One LLM call per email at most, and declarative rules
you can read, lint and explain. Prompts stay out of the rules, so they can't drift into conflicting instructions.

```
email ──► facts ──────────────────────────► rules (first match wins) ──► keep | move to folder
          sender:  priority/internal/correspondent/unknown      ▲
          age_h, read, replied: never/before_latest/after_latest │
          content: ONE classifier call, only if a rule needs it ┘
```

## Why "ish"

It moves mail into folders. It never sends, replies, deletes or creates folders, and it never touches Deleted
Items, Junk, Drafts, Sent or Outbox. Anything matching your sensitive list (HR, payroll, legal...) is never sent to an
LLM, moved or listed. Every move is undone by dragging the email back to the Inbox, and a new reply lands in the
Inbox by itself.

## Rules

A rule is conditions plus an action. Conditions are **facts**, never prompt text:

| Condition | Values |
|---|---|
| `category` | one or more of your configured categories (the only LLM-derived fact) |
| `min_confidence` | defaults to `classifier.min_confidence` |
| `sender` | `priority`, `internal`, `correspondent` (you've emailed them), `unknown` |
| `trusted` | `true` for priority/internal/correspondent |
| `min_age_h`, `max_age_h` | hours since it arrived |
| `read` | opened or not |
| `replied` | `never`, `before_latest` (they wrote again after your reply), `after_latest` (you replied last) |

`then` is `"keep"` or a folder path. `pass` is `fast` (run every few minutes), `cleanup` (run daily) or `both`.

```json
{"id": "cold-outreach", "when": {"category": ["vendor_pitch", "capital_pitch"], "trusted": false},
 "then": "Cold Outreach", "pass": "fast"}
```

**Keeping rules sane as they grow:**
- **Need a new distinction? Add a category, not a prompt.** A category is one `means` line, plus an optional
  `not` tie-breaker. The classifier prompt is generated from the categories (`zeroish lint --show-prompt`).
- **Order is the precedence.** Put `keep` rules (priority senders, action needed) at the top.
- **`zeroish lint`** catches unknown categories and conditions, rules that can never match because an earlier
  rule already catches everything they would, misplaced priority rules, and (with `--online`) missing folders.
- **`zeroish eval --labels yours.jsonl`** measures classifier accuracy on emails you've labelled. Run it after
  changing categories or models.
- **`zeroish explain --id <message id>`** shows every fact and every rule's verdict for one email.

## Quick start

1. Register an Entra app ([docs/entra-setup.md](docs/entra-setup.md)): delegated `Mail.ReadWrite`, public client
   flows on. There's no client secret: zeroish acts as you, so it can only reach mailboxes you can open.
2. `uv tool install founder-inbox-zeroish`, or clone the repo and `uv tool install .`
3. Copy `examples/config.example.json` to `~/.config/zeroish/config.json` and fill in owner, auth, senders and
   classifier backends. Any OpenAI-compatible server works (Ollama, vLLM, llama.cpp...), as does Anthropic.
4. `zeroish auth login`, then `zeroish lint --online`.
5. `zeroish run --pass fast` shows what *would* move. Add `--apply` once you agree.
6. Schedule it: `zeroish run --pass fast --apply` every 10–15 minutes, and `zeroish run --pass cleanup --apply`
   daily.

## Guardrails

- Runs only as the mailbox owner named in the config; a token for anyone else is refused and never saved.
- Plans never move anything; `--apply` re-checks every email (still in the Inbox? still not sensitive?) at move time.
- Never a destination: Deleted Items, Junk, Drafts, Sent, Outbox, recoverable items, anything beneath them, or a
  sensitive folder.
- `limits.max_moves_per_run` caps the damage of a bad rule.
- If no classifier answers, nothing that needs content moves.
- Audit log: one JSON line per plan and per move, with rule ids, never subject lines or bodies.

## Develop

```bash
uv run python -m unittest discover -s tests
```
Standard library only, Python 3.10+. MIT licensed.
