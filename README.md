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
| `sender` | `priority`, `internal`, `known` (your CRM's customers via `senders.known_file`), `correspondent` (you've emailed them), `team_thread` (a colleague is on To/Cc), `unknown` |
| `trusted` | `true` for anything but `unknown`, so a new customer whose email CCs your team is never "cold" |
| `min_age_h`, `max_age_h` | hours since it arrived |
| `read` | opened or not |
| `replied` | `never`, `before_latest` (they wrote again after your reply), `after_latest` (you replied last) |

`then` is `"keep"` or a folder path. `pass` is `fast` (run every few minutes), `cleanup` (run daily) or `both`.

```json
{"id": "cold-outreach", "when": {"category": ["vendor_pitch", "capital_pitch"], "trusted": false},
 "then": "Cold Outreach", "pass": "fast"}
```

**Inbox zero backstop (`stale`).** After `stale.after_days` (e.g. 30), nothing stays. The first move rule that
fits sends the email to its folder, with `relax`ed conditions such as `read` ignored. Keep rules stop protecting it.
Anything unmatched goes to `stale.then` (e.g. Archive). This applies in the cleanup pass only.

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
6. Schedule two lanes every 10 minutes, each with a small batch. Runs never overlap, thanks to a lock.
   ```cron
   */10 * * * *  zeroish run --pass fast --since 2h --limit 25 --apply             # new mail: pitches and promos out
   5-59/10 * * * * zeroish run --sweep --limit 25 --apply                         # backlog, every rule, bookmarked
   ```
7. Watch it: `zeroish status` reports last runs, moves per rule and folder, failures, and whether the classifier is
   answering. It exits 3 when unhealthy (no recent run, a failed run, most mail unclassified, a backlog sweep that
   stopped or hasn't finished a pass in 24 hours, or mail kept past the backstop), so a heartbeat check or chat bot
   can alert on it.

## Web page (optional)

`zeroish ui` opens a local page for the same config file: reorder and edit rules, try any email against them
before saving ("unknown sender, newsletter, 2 days old, unread: which rule fires?"), edit categories and the
classifier prompt they build, senders, the backstop, classifier models, run health, and the Microsoft sign-in.

- Saving runs the same checks as `zeroish lint` and refuses a config with an unreachable rule. It keeps the last 10
  versions as `config.json.bak-*`, and refuses to overwrite the file if it changed on disk meanwhile.
- The JSON file stays the source of truth. Editing it by hand works exactly as before; the page is never required.
- It listens on 127.0.0.1 only, and every request needs the one-time key in the URL it prints.
- Config on a server? `ssh -L 8765:127.0.0.1:8765 server zeroish ui --no-browser`, then open the printed URL
  locally.

## Guardrails

- Runs only as the mailbox owner named in the config; a token for anyone else is refused and never saved.
- Plans never move anything; `--apply` re-checks every email (still in the Inbox? still not sensitive?) at move time.
- Never a destination: Deleted Items, Junk, Drafts, Sent, Outbox, recoverable items, anything beneath them, or a
  sensitive folder.
- `limits.max_moves_per_run` caps the damage of a bad rule.
- If no classifier answers, nothing that needs content moves.
- Audit log: one JSON line per plan and per move, with rule ids, never subject lines or bodies.

## Where this is going

Inbox triage is the first job of a founder's **chief of staff**: deciding what deserves your attention and keeping
the rest out of the way. The same pieces carry over to the next jobs: facts about people and threads, one content
classifier, declarative rules, guardrails, and an audit trail.

- **Now:** inbox zero(ish). Cold outreach and promotions out within minutes; notifications, digests and handled
  threads filed; a backlog sweep; nothing older than 30 days left in the inbox.
- **Next:** a daily brief covering what needs you today (action-needed mail, priority senders, unanswered
  customer threads), and follow-up nudges when a thread you care about goes quiet.
- **Later:** calendar triage (accept, decline or propose based on rules about who and what), meeting prep from
  the thread history, and Google Workspace alongside Microsoft 365.

Each step keeps the same principle: an LLM judges content, and you write the rules in plain, checkable terms.

## Develop

```bash
uv run python -m unittest discover -s tests
```
Standard library only, Python 3.10+.

## Licence

[FSL-1.1-MIT](LICENSE.md) (the Functional Source License). It's free to use, modify and self-host, including for
your own company's inbox and for consulting work, but you can't offer it as a competing commercial product or
service. Each release becomes plain **MIT** two years after it ships. This is
[fair source](https://fair.io), not OSI open source.
