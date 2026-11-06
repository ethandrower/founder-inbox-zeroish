"""ABOUTME: Plan (facts -> rules -> decision per inbox email), apply moves through the guardrails, explain, eval.
ABOUTME: Mailbox-agnostic: takes any object with the Mailbox methods, so tests run against a fake."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import classify as clf
from . import facts as facts_mod
from . import rules as rules_mod


def audit(cfg: dict, event: dict):
    """One JSON line per action: what, which rule, counts. Never subject lines or bodies."""
    path = os.path.expanduser(cfg["audit_log"])
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}) + "\n")
    except OSError:
        pass


def _row(m: dict) -> dict:
    s = (m.get("from") or {}).get("emailAddress") or {}
    return {"id": m["id"], "from": f"{s.get('name', '')} <{s.get('address', '')}>".strip(),
            "subject": m.get("subject"), "received": m.get("receivedDateTime")}


def plan(cfg: dict, mbx, pass_name: str, since_iso: str | None, limit: int, classifier=None,
         now: datetime | None = None, workers: int = 2, sweep: bool = False) -> dict:
    """Decide inbox email. Default: newest first since `since_iso`. sweep=True: oldest first from the saved
    bookmark, so a small batch per run works through the whole backlog over time. Moves nothing."""
    now = now or datetime.now(timezone.utc)
    bookmark = read_state(cfg).get("sweep_after") if sweep else None
    msgs = mbx.inbox_oldest(bookmark, limit) if sweep else mbx.inbox(since_iso, limit)
    sent = facts_mod.SentIndex(mbx.sent_history(cfg["limits"]["sent_lookback"]))
    classifier = classifier or clf.Classifier(cfg, clf.Cache(cfg["classifier"]["cache_path"], version=clf.prompt_version(cfg)))

    def one(m):
        if cfg["sensitive"].reason(m, "Inbox"):
            return m, None, None
        f = facts_mod.compute(m, cfg, sent, now)
        text = clf.email_text(m, sender_kind=f.sender_kind)
        d = rules_mod.evaluate(cfg["rules"], f, pass_name, lambda: classifier(m["id"], text), cfg.get("stale"))
        return m, f, d

    with ThreadPoolExecutor(max_workers=workers) as pool:
        decided = list(pool.map(one, msgs))
    if getattr(classifier, "cache", None):
        classifier.cache.save()

    rows, withheld, unclassified = [], 0, 0
    for m, f, d in decided:
        if d is None:
            withheld += 1
            continue
        if d.rule is None and d.why.startswith("not classified"):
            unclassified += 1
        rows.append({**_row(m), "action": d.action, "rule": d.rule, "to": d.to, "why": d.why,
                     "category": (f.content or {}).get("category"), "sender": f.sender_kind,
                     "read": f.read, "replied": f.replied, "age_h": round(f.age_h, 1)})
    out = {"pass": pass_name, "lane": "sweep" if sweep else "new", "scanned": len(msgs),
           "withheld_sensitive": withheld,
           "priority": [r for r in rows if r["sender"] == "priority"],
           "to_move": [r for r in rows if r["action"] == "move"],
           "kept": [r for r in rows if r["action"] == "keep"]}
    if unclassified:
        out["warning"] = f"{unclassified} email(s) not classified (no LLM answered); they stay in the inbox"
    out["unclassified"] = unclassified
    if sweep:
        # Short page = reached the present: start again from the oldest next time (age rules keep ripening).
        out["sweep_from"] = bookmark
        out["sweep_next"] = msgs[-1]["receivedDateTime"] if len(msgs) >= limit else None
    return out


def commit_sweep(cfg: dict, planned: dict):
    """Advance the sweep bookmark. Call only after the batch was applied (or deliberately skipped)."""
    if planned.get("lane") == "sweep":
        state = read_state(cfg)
        state["sweep_after"] = planned.get("sweep_next")
        write_state(cfg, state)


# ── state, lock, health ───────────────────────────────────────────────
def _state_path(cfg: dict, name: str) -> str:
    return os.path.join(os.path.dirname(os.path.expanduser(cfg["audit_log"])) or ".", name)


def read_state(cfg: dict) -> dict:
    try:
        with open(_state_path(cfg, "state.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(cfg: dict, state: dict):
    path = _state_path(cfg, "state.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)


class RunLock:
    """One run at a time per mailbox. A slow run (busy LLM) is never overlapped by the next cron tick."""

    def __init__(self, cfg: dict):
        self.path = _state_path(cfg, "run.lock")
        self.fh = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
        return self.fh is not None

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def status(cfg: dict, hours: float = 24, max_gap_min: float = 25, now: datetime | None = None) -> dict:
    """Health from the audit log: is it running, is it moving mail, is anything failing. For humans and cron."""
    now = now or datetime.now(timezone.utc)
    since = now.timestamp() - hours * 3600
    runs, moves = [], []
    try:
        with open(os.path.expanduser(cfg["audit_log"])) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                if e.get("cmd") == "run":
                    runs.append((ts, e))
                elif e.get("cmd") == "move" and ts >= since:
                    moves.append(e)
    except OSError:
        pass
    problems, lanes = [], {}
    for ts, e in runs:
        lanes[e.get("lane", "new")] = (ts, e)
    if not runs:
        problems.append("no runs recorded yet")
    for lane, (ts, e) in sorted(lanes.items()):
        if not e.get("ok"):
            problems.append(f"last {lane} run failed: {e.get('error', '?')}")
    last_new = lanes.get("new")
    if last_new and (now.timestamp() - last_new[0]) / 60 > max_gap_min:
        problems.append(f"no new-mail run for {(now.timestamp() - last_new[0]) / 60:.0f} min (expected every 10)")
    recent = [e for ts, e in runs if ts >= since]
    fails = [e for e in recent if not e.get("ok")]
    unclassified = sum(e.get("unclassified", 0) for e in recent if e.get("ok"))
    if recent and unclassified > 0.5 * max(1, sum(e.get("scanned", 0) for e in recent if e.get("ok"))):
        problems.append(f"{unclassified} emails unclassified in {hours:g}h — classifier mostly unavailable")
    by_rule: dict[str, int] = {}
    by_folder: dict[str, int] = {}
    for m in moves:
        by_rule[m.get("rule", "?")] = by_rule.get(m.get("rule", "?"), 0) + 1
        by_folder[m.get("to", "?")] = by_folder.get(m.get("to", "?"), 0) + 1
    return {
        "healthy": not problems, "problems": problems, "window_h": hours,
        "last_run": {lane: {"at": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
                            "ok": e.get("ok"), "scanned": e.get("scanned"), "moved": e.get("moved")}
                     for lane, (ts, e) in sorted(lanes.items())},
        "runs": len(recent), "failed_runs": len(fails), "moved": len(moves), "by_rule": by_rule,
        "by_folder": by_folder, "unclassified": unclassified,
        "recent_errors": [e.get("error") for e in fails[-3:]],
        "sweep_bookmark": read_state(cfg).get("sweep_after"),
    }


def apply(cfg: dict, mbx, planned: dict) -> dict:
    """Move what `plan` proposed, re-checking every guardrail at the moment of the move."""
    protected = mbx.protected_ids()
    index = mbx.folders()
    cap = cfg["limits"]["max_moves_per_run"]
    moved, refused = [], []
    for r in planned["to_move"][:cap]:
        try:
            fid = mbx.resolve(r["to"])
            chain_names = []
            cur, seen = fid, set()
            while cur and cur in index and cur not in seen:
                seen.add(cur)
                chain_names.append(index[cur]["name"])
                cur = index[cur]["parent"]
            if mbx.is_protected(fid, protected):
                raise PermissionError("protected folder (deleted, junk, drafts, sent, outbox, recoverable)")
            if any(cfg["sensitive"].folder_blocked(n) for n in chain_names):
                raise PermissionError("sensitive folder")
            m = mbx.message(r["id"])  # re-read: it may have changed or been moved since the plan
            if m.get("parentFolderId") != index_id_for_inbox(mbx):
                raise PermissionError("no longer in the inbox")
            if cfg["sensitive"].reason(m, "Inbox"):
                raise PermissionError("sensitive")
            new_id = mbx.move(r["id"], fid)
            moved.append({"id": r["id"], "to": r["to"], "rule": r["rule"]})
            audit(cfg, {"cmd": "move", "id": r["id"], "new_id": new_id, "to": r["to"], "rule": r["rule"]})
        except Exception as e:  # noqa: BLE001 - one bad item never stops the rest
            refused.append({"id": r["id"], "to": r["to"], "error": str(e)[:200]})
    skipped = max(0, len(planned["to_move"]) - cap)
    return {"moved": len(moved), "by_folder": _count(moved, "to"), "by_rule": _count(moved, "rule"),
            "refused": refused, "skipped_over_cap": skipped}


def index_id_for_inbox(mbx) -> str:
    if not hasattr(mbx, "_inbox_id"):
        mbx._inbox_id = mbx.get("/me/mailFolders/inbox", params={"$select": "id"})["id"]
    return mbx._inbox_id


def _count(items, key):
    out: dict[str, int] = {}
    for i in items:
        out[i[key]] = out.get(i[key], 0) + 1
    return out


def explain(cfg: dict, mbx, msg_id: str, pass_name: str = "all", classifier=None,
            now: datetime | None = None) -> dict:
    """Every fact and every rule's verdict for one email, in order. The answer to 'why did this move?'"""
    m = mbx.message(msg_id)
    reason = cfg["sensitive"].reason(m, None)
    if reason:
        return {"id": msg_id, "withheld_sensitive": reason}
    sent = facts_mod.SentIndex(mbx.sent_history(cfg["limits"]["sent_lookback"]))
    f = facts_mod.compute(m, cfg, sent, now)
    classifier = classifier or clf.Classifier(cfg, clf.Cache(cfg["classifier"]["cache_path"], version=clf.prompt_version(cfg)))
    text = clf.email_text(m, sender_kind=f.sender_kind)
    d = rules_mod.evaluate(cfg["rules"], f, pass_name, lambda: classifier(m["id"], text), cfg.get("stale"))
    return {**_row(m), "facts": {"sender": f.sender_kind, "age_h": round(f.age_h, 1), "read": f.read,
                                 "replied": f.replied, "content": f.content},
            "decision": {"action": d.action, "rule": d.rule, "to": d.to, "why": d.why},
            "trace": [{"rule": rid, "result": res} for rid, res in d.trace]}


def evaluate_labels(cfg: dict, path: str, classifier=None) -> dict:
    """Accuracy of the classifier on labelled emails (JSONL: from, subject, body, expect)."""
    classifier = classifier or clf.Classifier(cfg)  # no cache: measure the model, not old answers
    total, right, errors, misses, same_outcome, harmful = 0, 0, 0, [], 0, 0
    with open(path) as fh:
        for i, line in enumerate(fh):
            if not line.strip():
                continue
            ex = json.loads(line)
            m = {"from": {"emailAddress": {"address": ex["from"], "name": ex.get("name", "")}},
                 "subject": ex.get("subject", ""), "bodyPreview": ex.get("body", "")}
            total += 1
            try:
                got = classifier(f"label-{i}", clf.email_text(m, sender_kind=ex.get("sender")))["category"]
            except clf.Unavailable as e:
                errors += 1
                misses.append({"line": i + 1, "expect": ex["expect"], "got": None, "error": str(e)[:120]})
                continue
            want_to, got_to = _outcome(cfg, ex, ex["expect"]), _outcome(cfg, ex, got)
            if got == ex["expect"]:
                right += 1
            if got_to == want_to:
                same_outcome += 1
            if got != ex["expect"]:
                harm = _harmful(cfg, want_to, got_to)
                harmful += bool(harm)
                misses.append({"line": i + 1, "subject": ex.get("subject"), "expect": ex["expect"], "got": got,
                               "should_go": want_to, "would_go": got_to, "harmful": harm})
    scored = total - errors
    return {"total": total, "scored": scored, "label_accuracy": round(right / scored, 3) if scored else None,
            "outcome_accuracy": round(same_outcome / scored, 3) if scored else None, "harmful": harmful,
            "unavailable": errors, "misses": misses}


def _outcome(cfg: dict, ex: dict, category: str) -> str:
    """Where an email ends up under the real rules, given a category. Read, 2 days old, no reply."""
    f = rules_mod.Facts(sender_kind=ex.get("sender", "unknown"), age_h=48.0, read=True, replied="never")
    d = rules_mod.evaluate(cfg["rules"], f, "all",
                           lambda: {"category": category, "confidence": 1.0, "reason": ""}, cfg.get("stale"))
    return d.to if d.action == "move" else "Inbox"


def _harmful(cfg: dict, want: str, got: str) -> str | None:
    """A miss is harmful if mail that should stay leaves, or lands in a fast-pass folder (out within minutes)."""
    fast_folders = {r.then for r in cfg["rules"] if not r.keeps and r.passes == ("fast",)}
    if want == "Inbox" and got != "Inbox":
        return f"should stay in the inbox but would go to {got}"
    if got in fast_folders and want not in fast_folders:
        return f"would be filed into {got} straight away instead of {want}"
    return None
