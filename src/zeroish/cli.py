"""ABOUTME: `zeroish` command line — auth, lint, run (plan / --apply), explain, eval, folders.
ABOUTME: Output is JSON. Exit 0 ok, 1 error, 2 refused by a guardrail or rule problem."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, classify, config, engine, facts, graph, rules


def _mailbox(cfg) -> graph.Mailbox:
    a = cfg["auth"]
    return graph.Mailbox(graph.Auth(a["tenant"], a["client_id"], a["token_path"]))


def cmd_auth(cfg, a):
    au = graph.Auth(cfg["auth"]["tenant"], cfg["auth"]["client_id"], cfg["auth"]["token_path"])
    if a.action == "login":
        return {"signed_in_as": au.login({cfg["owner"]["mailbox"]}, graph.whoami)}
    if a.action == "logout":
        return {"removed": au.logout()}
    _, user, _ = au.access_token()  # proves the stored sign-in still works
    return {"signed_in": True, "user": user, "matches_config": user == cfg["owner"]["mailbox"]}


def cmd_lint(cfg, a):
    folders = None
    if a.online:
        folders = {f["path"] for f in _mailbox(cfg).folders().values()}
    problems = rules.lint(cfg["rules"], set(cfg["categories"]), folders)
    return {"config": cfg["_path"], "rules": len(cfg["rules"]), "categories": len(cfg["categories"]),
            "problems": problems, "prompt": classify.build_prompt(cfg) if a.show_prompt else None}


def cmd_run(cfg, a):
    mbx = _mailbox(cfg)
    if mbx.user != cfg["owner"]["mailbox"]:
        raise PermissionError(f"signed in as {mbx.user}, config is for {cfg['owner']['mailbox']}")
    lane = "sweep" if a.sweep else "new"
    with engine.RunLock(cfg) as got:
        if not got:
            engine.audit(cfg, {"cmd": "skip", "lane": lane, "why": "previous run still going"})
            return {"skipped": "a previous run is still going"}
        started = time.time()
        try:
            planned = engine.plan(cfg, mbx, a.pass_name, None if a.sweep else facts.parse_since(a.since),
                                  a.limit or cfg["limits"]["scan_limit"], sweep=a.sweep)
            applied = engine.apply(cfg, mbx, planned) if a.apply else None
            if a.apply or not a.sweep:
                engine.commit_sweep(cfg, planned)  # a dry sweep doesn't advance the bookmark
        except Exception as e:  # noqa: BLE001 - recorded for `zeroish status`, then re-raised
            engine.audit(cfg, {"cmd": "run", "lane": lane, "pass": a.pass_name, "ok": False,
                               "error": str(e)[:200], "duration_s": round(time.time() - started, 1)})
            raise
        engine.audit(cfg, {"cmd": "run", "lane": lane, "pass": a.pass_name, "ok": True, "apply": a.apply,
                           "scanned": planned["scanned"], "to_move": len(planned["to_move"]),
                           "moved": (applied or {}).get("moved", 0), "refused": len((applied or {}).get("refused", [])),
                           "unclassified": planned["unclassified"], "withheld": planned["withheld_sensitive"],
                           "duration_s": round(time.time() - started, 1)})
    if applied is not None:
        planned["applied"] = applied
    else:
        planned["next"] = "review to_move; re-run with --apply to move exactly what the rules decide"
    if a.brief:
        planned["kept"] = [{k: r[k] for k in ("from", "subject", "rule", "why")} for r in planned["kept"]]
    return planned


def cmd_status(cfg, a):
    out = engine.status(cfg, a.hours, a.max_gap_min)
    a._exit = 0 if out["healthy"] else 3
    return out


def cmd_explain(cfg, a):
    return engine.explain(cfg, _mailbox(cfg), a.id, a.pass_name)


def cmd_eval(cfg, a):
    return engine.evaluate_labels(cfg, a.labels)


def cmd_folders(cfg, a):
    mbx = _mailbox(cfg)
    protected = mbx.protected_ids()
    return {"folders": [{"path": f["path"], "move_target": not mbx.is_protected(fid, protected)}
                        for fid, f in sorted(mbx.folders().items(), key=lambda kv: kv[1]["path"].lower())
                        if not cfg["sensitive"].folder_blocked(f["name"])]}


def build_parser():
    p = argparse.ArgumentParser(prog="zeroish", description="Founder Inbox Zeroish: rules-based inbox triage "
                                                           "for Microsoft 365.")
    p.add_argument("--version", action="version", version=f"zeroish {__version__}")
    p.add_argument("--config", help="config file (default $ZEROISH_CONFIG or ~/.config/zeroish/config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("auth", help="sign in as the mailbox owner (device code)")
    au.add_argument("action", choices=["login", "status", "logout"])
    au.set_defaults(fn=cmd_auth)
    li = sub.add_parser("lint", help="check config and rules for conflicts")
    li.add_argument("--online", action="store_true", help="also check every target folder exists")
    li.add_argument("--show-prompt", action="store_true", help="print the classifier prompt built from config")
    li.set_defaults(fn=cmd_lint)
    ru = sub.add_parser("run", help="plan (default) or --apply")
    ru.add_argument("--pass", dest="pass_name", choices=["fast", "cleanup", "all"], default="all")
    ru.add_argument("--since", default="2d", help="'2d', '12h' or YYYY-MM-DD")
    ru.add_argument("--limit", type=int)
    ru.add_argument("--apply", action="store_true", help="move what the rules decide")
    ru.add_argument("--brief", action="store_true", help="shorter 'kept' list")
    ru.add_argument("--sweep", action="store_true",
                    help="backlog lane: oldest first from a saved bookmark, --limit per run")
    ru.set_defaults(fn=cmd_run)
    st = sub.add_parser("status", help="health from the audit log; exit 3 when unhealthy")
    st.add_argument("--hours", type=float, default=24)
    st.add_argument("--max-gap-min", type=float, default=25, help="alert if no new-mail run for this long")
    st.set_defaults(fn=cmd_status)
    ex = sub.add_parser("explain", help="facts and every rule's verdict for one email")
    ex.add_argument("--id", required=True)
    ex.add_argument("--pass", dest="pass_name", choices=["fast", "cleanup", "all"], default="all")
    ex.set_defaults(fn=cmd_explain)
    ev = sub.add_parser("eval", help="classifier accuracy on labelled emails (JSONL)")
    ev.add_argument("--labels", required=True)
    ev.set_defaults(fn=cmd_eval)
    sub.add_parser("folders", help="mail folders and which can be move targets").set_defaults(fn=cmd_folders)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        cfg = config.load(a.config)
        out = a.fn(cfg, a)
        code = 2 if a.fn is cmd_lint and any(not x.startswith("categories with no rule") for x in out["problems"]) \
            else getattr(a, "_exit", 0)
    except (config.ConfigError, PermissionError) as e:
        out, code = {"refused": str(e)}, 2
    except (graph.GraphError, classify.Unavailable, OSError) as e:
        out, code = {"error": str(e)}, 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
