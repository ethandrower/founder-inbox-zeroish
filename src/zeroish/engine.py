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


def plan(cfg: dict, mbx, pass_name: str, since_iso: str, limit: int, classifier=None,
         now: datetime | None = None, workers: int = 2) -> dict:
    """Decide every inbox email since `since_iso`. Moves nothing."""
    now = now or datetime.now(timezone.utc)
    msgs = mbx.inbox(since_iso, limit)
    sent = facts_mod.SentIndex(mbx.sent_history(cfg["limits"]["sent_lookback"]))
    classifier = classifier or clf.Classifier(cfg, clf.Cache(cfg["classifier"]["cache_path"]))

    def one(m):
        if cfg["sensitive"].reason(m, "Inbox"):
            return m, None, None
        f = facts_mod.compute(m, cfg, sent, now)
        text = clf.email_text(m)
        d = rules_mod.evaluate(cfg["rules"], f, pass_name, lambda: classifier(m["id"], text))
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
    out = {"pass": pass_name, "scanned": len(msgs), "withheld_sensitive": withheld,
           "priority": [r for r in rows if r["sender"] == "priority"],
           "to_move": [r for r in rows if r["action"] == "move"],
           "kept": [r for r in rows if r["action"] == "keep"]}
    if unclassified:
        out["warning"] = f"{unclassified} email(s) not classified (no LLM answered); they stay in the inbox"
    audit(cfg, {"cmd": "plan", "pass": pass_name, "scanned": len(msgs), "withheld": withheld,
                "to_move": len(out["to_move"]), "unclassified": unclassified})
    return out


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
    classifier = classifier or clf.Classifier(cfg, clf.Cache(cfg["classifier"]["cache_path"]))
    text = clf.email_text(m)
    d = rules_mod.evaluate(cfg["rules"], f, pass_name, lambda: classifier(m["id"], text))
    return {**_row(m), "facts": {"sender": f.sender_kind, "age_h": round(f.age_h, 1), "read": f.read,
                                 "replied": f.replied, "content": f.content},
            "decision": {"action": d.action, "rule": d.rule, "to": d.to, "why": d.why},
            "trace": [{"rule": rid, "result": res} for rid, res in d.trace]}


def evaluate_labels(cfg: dict, path: str, classifier=None) -> dict:
    """Accuracy of the classifier on labelled emails (JSONL: from, subject, body, expect)."""
    classifier = classifier or clf.Classifier(cfg)  # no cache: measure the model, not old answers
    total, right, errors, misses = 0, 0, 0, []
    with open(path) as fh:
        for i, line in enumerate(fh):
            if not line.strip():
                continue
            ex = json.loads(line)
            m = {"from": {"emailAddress": {"address": ex["from"], "name": ex.get("name", "")}},
                 "subject": ex.get("subject", ""), "bodyPreview": ex.get("body", "")}
            total += 1
            try:
                got = classifier(f"label-{i}", clf.email_text(m))["category"]
            except clf.Unavailable as e:
                errors += 1
                misses.append({"line": i + 1, "expect": ex["expect"], "got": None, "error": str(e)[:120]})
                continue
            if got == ex["expect"]:
                right += 1
            else:
                misses.append({"line": i + 1, "subject": ex.get("subject"), "expect": ex["expect"], "got": got})
    scored = total - errors
    return {"total": total, "scored": scored, "correct": right,
            "accuracy": round(right / scored, 3) if scored else None, "unavailable": errors, "misses": misses}
